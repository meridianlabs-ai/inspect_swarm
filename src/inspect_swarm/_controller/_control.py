import contextvars
from collections import deque
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

import anyio
from anyio.abc import TaskGroup
from inspect_ai.agent import Agent, AgentState
from inspect_ai.log._samples import sample_active
from inspect_ai.model import ChatMessage, ChatMessageUser, ModelOutput
from inspect_ai.util import Limit, current_span_id, sample_limits
from inspect_ai.util._store import suspend_store_events

from .._budget import (
    Budget,
    BudgetState,
    CostPair,
    Lever,
    MemberContext,
    SwarmNode,
    current_budget,
    is_bridged,
)
from .._evidence import MemberTimelines, evidence, write_ledger, write_metrics
from .._final import finalise, first, settle
from .._ledger import LedgerInterval, MemberAccount
from .._member import (
    Member,
    MemberOutcome,
    classify_errors,
    member_nodes,
    member_sandbox,
    run_member,
)
from .._metrics import (
    GateChecks,
    MemberTimes,
    MemberWiring,
    first_scorer,
    gate_checks,
    own_steps,
    swarm_metrics,
)
from .._record import MemberResult, MemberState

if TYPE_CHECKING:
    from .._channel._channel import Channel


class MemberHandle(Protocol):
    """A member as a controller sees it: its name, role, state and status, never its text."""

    @property
    def name(self) -> str:
        """The member's name."""
        ...

    @property
    def role(self) -> str | None:
        """The member's role, if it has one."""
        ...

    @property
    def state(self) -> MemberState:
        """The member's lifecycle state."""
        ...

    @property
    def status(self) -> str | None:
        """How the member ended, once it has: `submitted`, `unsubmitted`, `stopped`, `share`, `member_limit:<type>`, `swarm_cap`, `cancelled` or `errored`."""
        ...

    @property
    def submitted(self) -> bool:
        """Whether the member ended with a submission (status `submitted`)."""
        ...


@dataclass(frozen=True)
class MemberEnded:
    """A member ended."""

    member: str
    """The member's name."""

    status: str
    """How it ended, as `MemberHandle.status`."""

    submitted: bool
    """Whether it ended with a submission."""


SwarmEvent = MemberEnded
"""An event a controller receives from `SwarmControl.events()`. Later versions add kinds, so test an event's type before reading its fields."""


class SwarmControl(Protocol):
    """What a controller's `run` works through: the swarm's members and their events, for one sample."""

    @property
    def members(self) -> Sequence[MemberHandle]:
        """The members, in roster order."""
        ...

    def member(self, name: str) -> MemberHandle:
        """The member with this name.

        Args:
            name: The member's name.

        Raises:
            KeyError: If no member has this name.
        """
        ...

    def start(
        self, member: str | MemberHandle, input: str | list[ChatMessage] | None = None
    ) -> None:
        """Start a member, and return at once.

        The member runs in its own task, span and tool-state scope, with its limits. Its input is `input`, or the swarm's input when `input` is `None`, followed by the member's preamble: its name, role and roster size, how the budget is split, and each channel's instructions.

        Args:
            member: The member, or its name.
            input: The member's input. `None` gives it the swarm's input.

        Raises:
            KeyError: If no member has this name.
            RuntimeError: If the member has already started (a member runs once), or the swarm is stopping.
        """
        ...

    def events(self) -> AsyncGenerator[SwarmEvent, None]:
        """The swarm's events, in order from the swarm's start.

        Each event is yielded once. The iterator ends when no member is running and no event is waiting; a member started later is reported by a new `events()` call. Use one iterator at a time: an iterator holds the events from its first step until it ends or is closed (`aclose()`, or leaving a `contextlib.aclosing()` block).

        Raises:
            RuntimeError: If another iterator is still open.
        """
        ...


# Reasons a controller may return as they are; any other is recorded as controller:<reason>.
_TOPOLOGY_REASONS = frozenset({"all_done", "verified", "quiescence"})


# start()'s refusal once a stop has begun. A controller that resumed before the stop's
# cancellation reached it (an event already queued, an await already complete) gets it
# from its next start(), and ends as the cancellation would have ended it.
class _Stopping(RuntimeError):
    pass


def _without_refusals(error: Exception) -> Exception | None:
    if isinstance(error, ExceptionGroup):
        return cast(ExceptionGroup[Exception], error).split(_Stopping)[1]
    return None if isinstance(error, _Stopping) else error


# A member in one run of the swarm: what its handle shows, and the nodes its wrapper
# enters. Its lever and limits are created here, once per run, so that a stop can close
# the lever of a member that has started but whose task has not yet entered it.
class MemberRun:
    def __init__(self, record: Member, name: str, share: Sequence[Limit]) -> None:
        self.record = record
        self.name = name
        self.role = record.role
        self.sandbox = member_sandbox(record, name)
        self.state: MemberState = "pending"
        self.status: str | None = None
        # the state the member runs on, kept for its output however it ends; its
        # conversation is built inside the member's limits
        self.agent_state = AgentState(messages=[])
        # what its wrapper leaves: the state its agent returned and its background counts
        self.outcome = MemberOutcome()
        # swarm times of its start and its end
        self.started: float | None = None
        self.time: float | None = None
        # counted when it ends
        self.gates = GateChecks()
        # read from the record, before any agent is built
        agent = record.agent if record.agent is not None else record.factory
        self.context = MemberContext(
            name,
            bridged=is_bridged(cast(Agent, agent), record.bridged),
            delivery=record.delivery,
        )
        self.lever, self.nodes = member_nodes(record, name, share)

    @property
    def submitted(self) -> bool:
        return self.status == "submitted"

    @property
    def cancelled(self) -> bool:
        # its agent loop or its background drain was cancelled: the hard stop, the
        # bridged branch, a member time limit, or a cancellation from outside the swarm
        return self.context.scope.cancel_called or self.status in (
            "cancelled",
            "member_limit:time",
        )

    def result(self) -> MemberResult:
        return MemberResult(
            name=self.name,
            role=self.role,
            state=self.state,
            status=self.status,
            submitted=self.submitted,
            time=self.time,
            sandbox=self.sandbox,
            gate_checks=self.gates.checks,
            gate_passes=self.gates.passes,
            first_pass_time=self.gates.first_pass_time,
        )


# The runtime for one sample: the controller surface, member tasks and the stop
# procedure. One is created for each run of the swarm.
class SwarmRun:
    def __init__(
        self,
        records: Sequence[tuple[Member, str]],
        *,
        state: AgentState,
        input: list[ChatMessage],
        preambles: dict[str, str],
        caps: list[Limit],
        time: float | None,
        grace: float,
        budget: Budget,
        shares: Mapping[str, Sequence[Limit]] | None = None,
        channels: Sequence["Channel"] = (),
    ) -> None:
        # deferred: the bus imports the channels, which import this module's package
        from .._bus._bus import Bus

        self._shares = shares or {}
        self._members = [
            MemberRun(record, name, self._shares.get(name, ()))
            for record, name in records
        ]
        # the channel states open here, before any member starts
        self.bus = Bus(
            self._members,
            {m.name: m.context for m in self._members},
            channels,
        )
        for node in caps:
            if isinstance(node, SwarmNode):
                node.stop_swarm = self.stop
        self._by_name = {m.name: m for m in self._members}
        self._state = state
        self._input = list(input)
        self._preambles = preambles
        self._caps = caps
        self._time = time
        self._grace = grace
        self._runtime_lever = Lever(owner=None)
        self._budget = budget
        # the ledger's meters: the swarm meter around everything the swarm runs, and the
        # final meter around its final step
        self._swarm_meter = CostPair(owner=None, role="meter")
        self._final_meter = CostPair(owner=None, role="meter")
        self._controller_scope = anyio.CancelScope()
        self._members_tg: TaskGroup | None = None
        self._member_ctx: contextvars.Context | None = None
        self._queue: deque[SwarmEvent] = deque()
        self._changed = anyio.Event()
        self._iterating = False
        self._stopped = anyio.Event()
        self._effective_grace = grace
        self._start = 0.0
        self._span_id: str | None = None
        self._interval: LedgerInterval | None = None
        # the scorer whose intermediate scores are gate checks
        self._scorer: str | None = None
        self.stop_reason: str | None = None

    # SwarmControl

    @property
    def members(self) -> Sequence[MemberHandle]:
        return list(self._members)

    def member(self, name: str) -> MemberHandle:
        return self._lookup(name)

    def start(
        self, member: str | MemberHandle, input: str | list[ChatMessage] | None = None
    ) -> None:
        m = self._lookup(member if isinstance(member, str) else member.name)
        if self.stop_reason is not None:
            raise _Stopping(
                f"start({m.name!r}): the swarm is stopping ({self.stop_reason}), so no member can start."
            )
        if m.state != "pending":
            raise RuntimeError(
                f"start({m.name!r}): the member has already started, and a member runs once."
            )
        assert self._members_tg is not None and self._member_ctx is not None
        messages: list[ChatMessage] = (
            list(self._input)
            if input is None
            else [ChatMessageUser(content=input)]
            if isinstance(input, str)
            else list(input)
        )
        messages.append(ChatMessageUser(content=self._preambles[m.name]))
        m.state = "running"
        m.started = anyio.current_time() - self._start
        self._settle()
        # from the saved context, so the member's lever is a child of the caps and never
        # of the runtime lever, whichever task starts it
        self._member_ctx.copy().run(
            self._members_tg.start_soon, self._run_member, m, messages, name=m.name
        )
        self._notify()

    async def events(self) -> AsyncGenerator[SwarmEvent, None]:
        # an iterator holds the events from its first step until it ends or is closed,
        # so a second one cannot take events from its stream
        if self._iterating:
            raise RuntimeError(
                "events(): another iterator is still open; use one at a time, and let it end or close it first."
            )
        self._iterating = True
        try:
            while True:
                if self._queue:
                    yield self._queue.popleft()
                elif not self._running():
                    return
                else:
                    await self._changed.wait()
        finally:
            self._iterating = False

    # the runtime

    async def run(self, controller: Callable[["SwarmControl"], Awaitable[str]]) -> None:
        from .._bus._bus import current_bus

        timelines = MemberTimelines([m.name for m in self._members])
        interval = self._interval = LedgerInterval()
        self._scorer = first_scorer()
        self._start = anyio.current_time()
        self.bus.waits.start = self._start
        # the swarm's span
        self._span_id = current_span_id()
        self._settle()
        error: BaseException | None = None
        bus_token = current_bus.set(self.bus)
        # for swarm_budget(), in every member's context and the controller's
        budget_token = current_budget.set(
            BudgetState(
                budget=self._budget,
                caps=self._caps,
                time=self._time,
                start=self._start,
                members={
                    m.name: (m.context, self._shares.get(m.name, ()))
                    for m in self._members
                },
            )
        )
        ended = False
        try:
            # first: spans inside the swarm, members' included, log no StoreEvent;
            # the swarm's span, entered before, logs the net change
            with suspend_store_events(), ExitStack() as meter:
                for node in self._swarm_meter.nodes:
                    meter.enter_context(node)
                try:
                    with ExitStack() as caps:
                        for node in self._caps:
                            caps.enter_context(node)
                        async with anyio.create_task_group() as members_tg:
                            self._members_tg = members_tg
                            self._member_ctx = contextvars.copy_context()
                            async with anyio.create_task_group() as runtime_tg:
                                runtime_tg.start_soon(self._supervise, runtime_tg)
                                if self._time is not None:
                                    runtime_tg.start_soon(self._time_cap, self._time)
                                runtime_tg.start_soon(self._run_controller, controller)
                except (Exception, BaseExceptionGroup) as ex:
                    # a foreign error from a member or the controller, or on trio a
                    # cancellation grouped with errors, which the wrappers do not
                    # catch. The swarm's own leaves are removed here, so that none
                    # reaches the runner as the sample's limit; a cancellation stays,
                    # and skips the final step. Only the swarm's own leaves would leave
                    # nothing to raise.
                    _, _, error = classify_errors(ex, self._nodes(), [])
                if error is None:
                    # the final step, outside the caps: only the sample's limits apply
                    with ExitStack() as final:
                        for node in self._final_meter.nodes:
                            final.enter_context(node)
                        finalise(
                            self._state,
                            self._input,
                            self._results(),
                            self._outputs(),
                            span_id=self._span_id,
                            stop_reason=self.stop_reason,
                        )
                    ended = True
        except Exception as ex:
            # from the final step, under the sample's limits
            error = ex
            raise
        finally:
            # synchronous, so that it runs under cancellation too; the provisional
            # answer is already written, as each change to the record writes it
            wall_clock = anyio.current_time() - self._start
            ledger = interval.ledger(
                budget=self._budget,
                caps=self._caps,
                time_cap=self._time,
                members=[
                    MemberAccount(
                        name=m.name,
                        status=m.status,
                        bridged=m.context.bridged,
                        lever=m.lever,
                        span_id=m.context.span_id,
                        cancelled=m.cancelled,
                        background_started=m.outcome.background_started,
                        background_cancelled=m.outcome.background_cancelled,
                    )
                    for m in self._members
                ],
                runtime=self._runtime_lever,
                final=self._final_meter,
                swarm=self._swarm_meter,
                stop_reason=self.stop_reason,
                ended_by=None if ended else _exhaustion(error),
                interrupted=not ended,
                # M1's final step does not await, so nothing can cancel it
                final_cancelled=False,
            )
            write_ledger(ledger)
            spans = {
                m.name: m.context.span_id
                for m in self._members
                if m.context.span_id is not None
            }
            places = interval.places(spans)
            # the record's answer, final or provisional
            chosen = first(self._results())
            write_metrics(
                swarm_metrics(
                    ledger,
                    interval.events,
                    places,
                    [MemberTimes(m.name, m.started, m.time) for m in self._members],
                    [m.gates for m in self._members],
                    [
                        MemberWiring(
                            m.context.delivery_effective,
                            tools=m.context.loop is not None,
                            hooked=m.context.hooked,
                            news=m.context.news,
                            own_steps=0
                            if m.context.span_id is None
                            else own_steps(interval.events, m.context.span_id),
                        )
                        for m in self._members
                    ],
                    list(self.bus.states),
                    self.bus.waits.max_wait is not None,
                    wall_clock,
                    chosen.name if chosen is not None else None,
                )
            )
            timelines.fill(places)
            # last, so that the bus and the budget are current through the final step
            # and the ledger
            current_budget.reset(budget_token)
            current_bus.reset(bus_token)
        if error is not None:
            raise error

    def stop(self, reason: str) -> None:
        # the first call wins; everything before the grace period is synchronous, so
        # no member can start once a stop has begun
        if self.stop_reason is not None:
            return
        # first, so that no record commits once a stop has begun
        self.bus.close()
        self.stop_reason = reason
        # a no-op when the controller has returned
        self._controller_scope.cancel()
        for m in self._members:
            if m.state == "running":
                m.lever.close(reason)
        self._runtime_lever.close(reason)
        self._effective_grace = self._grace_now()
        self._stopped.set()

    async def _run_controller(
        self, controller: Callable[["SwarmControl"], Awaitable[str]]
    ) -> None:
        error: Exception | None = None
        reason: object = None
        with ExitStack() as lever:
            for node in self._runtime_lever.nodes:
                lever.enter_context(node)
            with self._controller_scope:
                try:
                    reason = await controller(self)
                except Exception as ex:
                    error = ex
            if self._controller_scope.cancelled_caught:
                return
        if error is not None:
            rest = _without_refusals(error)
            if rest is None:
                # the stop is under way
                return
            # a cap began the swarm's stop when it was reached
            _, _, foreign = classify_errors(rest, self._caps, self._runtime_lever.nodes)
            if foreign is not None:
                raise foreign
            return
        if not isinstance(reason, str):
            raise TypeError(
                f"A controller's run must return its stop reason as a str, got {type(reason).__name__}."
            )
        self.stop(reason if reason in _TOPOLOGY_REASONS else f"controller:{reason}")

    async def _run_member(self, m: MemberRun, input: list[ChatMessage]) -> None:
        def end(status: str, state: AgentState) -> None:
            m.agent_state = state
            self._end(m, status)

        await run_member(
            m.record,
            m.name,
            m.context,
            m.lever,
            m.nodes,
            self._caps,
            input,
            m.agent_state,
            # called when the member's drain begins, so that its grace is clipped to
            # the sample's remaining time then
            self._grace_now,
            m.outcome,
            end,
        )

    def _end(self, m: MemberRun, status: str) -> None:
        # the member counts as running until its event is queued
        m.state = "ended"
        m.status = status
        # releases the other members' waits
        self.bus.member_ended(m.name, status)
        m.time = anyio.current_time() - self._start
        if m.context.span_id is not None:
            assert self._interval is not None
            m.gates = gate_checks(
                self._interval.events, m.context.span_id, self._scorer, self._start
            )
        # in the member's span, unless it ended before entering it
        evidence(
            "result",
            "member_result",
            member=m.name,
            span_id=m.context.span_id,
            status=status,
            time=m.time,
            sandbox=m.sandbox,
        )
        self._settle()
        self._queue.append(MemberEnded(m.name, status, m.submitted))
        self._notify()

    # the record and the provisional answer, after each change
    def _settle(self) -> None:
        settle(self._state, self._results(), self._outputs(), provisional=True)

    def _results(self) -> list[MemberResult]:
        return [m.result() for m in self._members]

    # the ended members' outputs, kept here rather than in the record
    def _outputs(self) -> dict[str, ModelOutput]:
        return {
            m.name: m.agent_state.output for m in self._members if m.state == "ended"
        }

    async def _time_cap(self, seconds: float) -> None:
        await anyio.sleep(seconds)
        self.stop("swarm_time")

    async def _supervise(self, runtime_tg: TaskGroup) -> None:
        await self._stopped.wait()
        # grace: members finish the calls they have in flight and are refused the next
        with anyio.move_on_after(self._effective_grace):
            await self._until_quiet()
        # hard stop
        for m in self._members:
            if m.state == "running":
                m.context.scope.cancel()
        await self._until_quiet()
        # ends the time cap's timer
        runtime_tg.cancel_scope.cancel()

    def _grace_now(self) -> float:
        # the final step keeps at least half the sample's remaining time
        time = sample_limits().time
        if time.limit is None:
            return self._grace
        return min(self._grace, max(0.0, (time.limit - time.usage) / 2))

    # every node this run created, the members' included
    def _nodes(self) -> list[Limit]:
        return [
            *self._swarm_meter.nodes,
            *self._caps,
            *self._runtime_lever.nodes,
            *self._final_meter.nodes,
            *(node for m in self._members for node in m.nodes),
        ]

    def _lookup(self, name: str) -> MemberRun:
        try:
            return self._by_name[name]
        except KeyError:
            raise KeyError(
                f"No member named {name!r}; the members are {', '.join(self._by_name)}."
            ) from None

    def _running(self) -> bool:
        return any(m.state == "running" for m in self._members)

    async def _until_quiet(self) -> None:
        while self._running():
            await self._changed.wait()

    def _notify(self) -> None:
        self._changed.set()
        self._changed = anyio.Event()


# How a sample limit or the operator ended the swarm, from direct evidence only: the
# raised error's sample-limit leaf, or for a cancellation what the sample shows.
# "unknown" for a cancellation the sample does not explain, and for a foreign error
# while the sample shows an outside cause, since which came first is not known. None
# for a foreign error with no outside cause.
def _exhaustion(error: BaseException | None) -> str | None:
    if error is not None:
        limits = sample_limits()
        roots = [
            limits.token,
            limits.cost,
            limits.message,
            limits.turn,
            limits.working,
            limits.time,
        ]
        _, limit, _ = classify_errors(error, [], roots)
        if limit is not None:
            return f"sample_limit:{limit.type}"
        if _errors(error) is not None:
            return None if _sample_cause() is None else "unknown"
    # a cancellation leaving the run is never the swarm's own: its scopes catch those
    return _sample_cause() or "unknown"


# An error without its cancellations, or None when it has nothing else.
def _errors(error: BaseException) -> BaseException | None:
    group = (
        cast(BaseExceptionGroup[BaseException], error)
        if isinstance(error, BaseExceptionGroup)
        else BaseExceptionGroup("swarm", [error])
    )
    return group.split(anyio.get_cancelled_exc_class())[1]


# The outside cause the sample shows, or None.
def _sample_cause() -> str | None:
    active = sample_active()
    if active is not None and active.interrupt_action is not None:
        return "operator"
    if active is not None and active.limit_exceeded_error is not None:
        return f"sample_limit:{active.limit_exceeded_error.type}"
    time = sample_limits().time
    if time.limit is not None and time.usage >= time.limit:
        return "sample_limit:time"
    return None
