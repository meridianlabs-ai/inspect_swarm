import contextvars
import functools
from collections import deque
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, cast

import anyio
from anyio.abc import TaskGroup
from inspect_ai._util.error import PrerequisiteError
from inspect_ai.agent import Agent, AgentState
from inspect_ai.log._samples import sample_active
from inspect_ai.model import ChatMessage, ChatMessageUser, ModelOutput
from inspect_ai.util import Limit, current_span_id, sample_limits
from inspect_ai.util._checkpoint.checkpointer_noop import (
    _NoopCheckpointer,  # pyright: ignore[reportPrivateUsage]
)
from inspect_ai.util._store import suspend_store_events
from shortuuid import uuid

from .._budget import (
    Budget,
    BudgetState,
    CostPair,
    Lever,
    MemberContext,
    SwarmNode,
    SwarmTimeNode,
    current_budget,
    is_bridged,
)
from .._evidence import MemberTimelines, swarm_event, write_ledger, write_metrics
from .._final import FinalName, Pick, answer, finalise, settle
from .._ledger import LedgerInterval, MemberAccount
from .._member import (
    Member,
    MemberOutcome,
    as_group,
    classify_errors,
    member_nodes,
    member_sandbox,
    run_member,
    seam_refusal,
)
from .._metrics import (
    GateChecks,
    MemberTimes,
    MemberWiring,
    first_scorer,
    gate_checks,
    swarm_metrics,
)
from .._record import (
    MemberResult,
    MemberState,
    SwarmResultRecord,
    _swarm_ended,  # pyright: ignore[reportPrivateUsage]
    _swarm_started,  # pyright: ignore[reportPrivateUsage]
    write_record,
    write_start,
)
from .._seam import BoundaryCallback, MemberGate, check_origin

if TYPE_CHECKING:
    from .._bus._bus import Bus
    from .._bus._record import Record
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
    def choose_name(self) -> bool:
        """Whether the member chooses its own name and is not told `name`."""
        ...

    @property
    def state(self) -> MemberState:
        """The member's lifecycle state: `pending`, `running` or `ended`, and for a persistent member also `idle` (held after an answer until `SwarmControl.wake()`) and `ending` (its ending has begun, and its `MemberEnded` is still to come)."""
        ...

    @property
    def status(self) -> str | None:
        """How the member ended, once it has: `submitted`, `unsubmitted`, `stopped`, `share`, `member_limit:<type>`, `swarm_cap`, `cancelled` or `errored`."""
        ...

    @property
    def submitted(self) -> bool:
        """Whether the member ended with a submission (status `submitted`)."""
        ...

    @property
    def held(self) -> bool:
        """Whether the member is held at a turn boundary: its boundary callbacks are running, or it is idle."""
        ...

    @property
    def persistent(self) -> bool:
        """Whether the member is persistent (`member(..., persistent=True)`)."""
        ...

    @property
    def answers(self) -> int:
        """How many answers a persistent member has given so far; 0 for other members."""
        ...

    def on_boundary(
        self, callback: BoundaryCallback, *, origin: str
    ) -> Callable[[], None]:
        """Register a boundary callback, called at every turn boundary of the member's own agent loop.

        A boundary comes before the loop's first generation and before each one after it, with no model or tool call of the loop in flight. The callbacks run there in the order they were registered, in the member's task, while the member is held: its time limit pauses, `swarm_wait` does not count it as active, and a stop ends it at once. Each receives a `Boundary` and returns `None` to continue, a list of messages to continue with that conversation (it must hold a user message), or `"end"` to end the member `stopped`. Registered on a pending member, the callback sees boundary 0, before the first generation; on a running one, from its next boundary.

        A callback's model and tool calls count towards the member's limits and the sample's working time, and so does the rest of its time: wrap an idle wait in `suspend_working_limit()` to count it as waiting. An exception a callback raises ends the member `errored`. Every change a callback makes is recorded as a `changed` event, with `origin`.

        Args:
            callback: The callback.
            origin: Names the caller in the evidence, such as `orbit` or `operator`: 1 to 64 letters, digits, `_`, `-`, `.` or `:`. `swarm` is reserved.

        Returns:
            A function that unregisters the callback, from the next boundary.

        Raises:
            ValueError: If `origin` is invalid, or the member cannot be held: a bridged member, or `deepagent()` with background dispatch.
            RuntimeError: If the member has ended, or the swarm is stopping.
        """
        ...

    def end(self, *, origin: str) -> None:
        """End the member `stopped`, with the reason `ended by <origin>`.

        A held member ends at once: the waiting callback is cancelled, later ones do not run, and no change of theirs is applied. A running member's next model call is refused and it ends at its next boundary. A pending member ends at its first boundary once started. If the member is still running when the swarm's grace period has passed since the request (or since a pending member started), its turn or its cleanup included, it is cancelled and ends `cancelled`. A member whose agent returns after the request ends `stopped`, whatever its output. A second call, or a call for a member that has ended, changes nothing. The other members and the swarm go on.

        Args:
            origin: Names the caller in the evidence, as for `on_boundary()`.

        Raises:
            ValueError: If `origin` is invalid.
        """
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


@dataclass(frozen=True)
class MemberIdle:
    """A persistent member answered and is idle, held until `SwarmControl.wake()` gives it more work."""

    member: str
    """The member's name."""

    answer: int
    """The index of the answer it just gave, from 0."""

    news: bool
    """Whether a channel already has news for the member's own loop: something that would end a `swarm_wait` of that loop that watches everything, such as a file another agent wrote that the member has not viewed. Later writes are reported as `MemberNews`, so a controller that wakes members on news also wakes on this."""


@dataclass(frozen=True)
class MemberNews:
    """A record was delivered that is news to an idle persistent member: it would end a `swarm_wait` of the member's own loop that watches everything.

    The controller decides whether to wake the member. Two records give two events, and an earlier event's handling may have woken the member, so check that it is still idle before waking it.
    """

    member: str
    """The idle member the record is news to."""

    channel: str
    """The name of the channel that delivered the record."""

    record: str
    """The record's id."""

    sender: str
    """The name of the member that sent the record."""


ControlEvent = MemberEnded | MemberIdle | MemberNews
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
        self,
        member: str | MemberHandle,
        input: str | list[ChatMessage] | None = None,
        *,
        preamble: str | Literal[False] | None = None,
    ) -> None:
        """Start a member, and return at once.

        The member runs in its own task, span and tool-state scope, with its limits. Its input is `input`, or the swarm's input when `input` is `None`, followed by the member's preamble as a user message. The swarm's preamble gives the member's name unless `choose_name=True`, its role and the roster size, how the budget is split, and each channel's instructions.

        A controller that does not run its members at the same time can replace the swarm's preamble or leave it out. Text passed as `preamble` replaces all of it, channel instructions included, and is used as given, also for a member with `choose_name=True`.

        Args:
            member: The member, or its name.
            input: The member's input. `None` gives it the swarm's input.
            preamble: The member's preamble, a user message after its input. `None` gives it the swarm's preamble; a string replaces the swarm's preamble with that text; `False` leaves it out.

        Raises:
            KeyError: If no member has this name.
            TypeError: If `preamble` is not a string, `False` or `None`.
            ValueError: If `preamble` is an empty or blank string, or is `False` and the input holds no user message.
            RuntimeError: If the member has already started (a member runs once; `wake()` gives an idle persistent member more work), or the swarm is stopping.
        """
        ...

    def wake(
        self, member: str | MemberHandle, input: str | list[ChatMessageUser]
    ) -> None:
        """Give an idle member its next piece of work, and return at once.

        The member continues its conversation with `input` appended: a string as one user message, a list of user messages as given. It keeps its tool state, its limits and what it has seen. Each message gets a new id and no `source`; the messages passed are not changed, so the same list can be passed again.

        An event can be stale when the controller reads it, so check that `member(name).state == "idle"` before waking a member.

        Args:
            member: The member, or its name.
            input: The member's next input.

        Raises:
            KeyError: If no member has this name.
            ValueError: If `input` is an empty string, an empty list, or a list holding any message that is not a `ChatMessageUser`.
            RuntimeError: If the member is not idle (pending members start with `start()`; a running member is working or has not yet been held since its answer; an ending or ended member is ending), or the swarm is stopping.
        """
        ...

    def events(self) -> AsyncGenerator[ControlEvent, None]:
        """The swarm's events, in order from the swarm's start.

        Each event is yielded once. The iterator ends when no member is running or ending and no event is waiting: every member is pending, idle or ended. A member started or woken later is reported by a new `events()` call. Use one iterator at a time: an iterator holds the events from its first step until it ends or is closed (`aclose()`, or leaving a `contextlib.aclosing()` block).

        Raises:
            RuntimeError: If another iterator is still open.
        """
        ...

    def choose_answer(
        self, member: str | MemberHandle, reason: str | None = None
    ) -> None:
        """Make this member's submission the swarm's answer, in place of the controller's `final` rule.

        The choice is the provisional answer from now on and the final answer when the swarm ends. The result record and the `final` event give the mode `controller`, the member and `reason`. Choose once, from a member that has ended with a submission, or a persistent member that has answered: its latest answer at the time of the call, kept if it answers again.

        Args:
            member: The member, or its name.
            reason: Why the controller chose it, for the record.

        Raises:
            KeyError: If no member has this name.
            RuntimeError: If the member has not ended with a submission (a persistent member: has not answered yet), or the controller has already chosen.
        """
        ...


# Reasons a controller may return as they are; any other is recorded as controller:<reason>.
_TOPOLOGY_REASONS = frozenset({"all_done", "submitted", "verified", "quiescence"})


# start()'s refusal once a stop has begun. A controller that resumed before the stop's
# cancellation reached it (an event already queued, an await already complete) gets it
# from its next start(), and ends as the cancellation would have ended it.
class _Stopping(RuntimeError):
    pass


def _without_refusals(error: Exception) -> Exception | None:
    if isinstance(error, ExceptionGroup):
        return cast(ExceptionGroup[Exception], error).split(_Stopping)[1]
    return None if isinstance(error, _Stopping) else error


# One answer of a persistent member: its output, a detached copy, and the swarm time
# it was given.
@dataclass(frozen=True)
class _Answer:
    output: ModelOutput
    time: float


# A member in one run of the swarm: what its handle shows, and the nodes its wrapper
# enters. Its lever and limits are created here, once per run, so that a stop can close
# the lever of a member that has started but whose task has not yet entered it.
class MemberRun:
    def __init__(self, record: Member, name: str, share: Sequence[Limit]) -> None:
        self.record = record
        self.name = name
        self.role = record.role
        self.choose_name = record.choose_name
        self.sandbox = member_sandbox(record, name)
        self.persistent = record.persistent
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
        # a persistent member's answers, in order
        self.answer_list: list[_Answer] = []
        # counted when it ends
        self.gates = GateChecks()
        # read from the record, before any agent is built
        agent = record.agent if record.agent is not None else record.factory
        self.context = MemberContext(
            name,
            bridged=is_bridged(cast(Agent, agent), record.bridged),
        )
        self.lever, self.nodes = member_nodes(record, name, share)
        self.gate = MemberGate(
            name,
            self.context,
            self.lever,
            time_nodes=[n for n in self.nodes if isinstance(n, SwarmTimeNode)],
            persistent=record.persistent,
        )

    @property
    def submitted(self) -> bool:
        return self.status == "submitted"

    @property
    def held(self) -> bool:
        return self.gate.held

    @property
    def answers(self) -> int:
        return len(self.answer_list)

    # started and not ended: what the stop procedure waits for and cancels
    @property
    def started_not_ended(self) -> bool:
        return self.state not in ("pending", "ended")

    def on_boundary(
        self, callback: BoundaryCallback, *, origin: str
    ) -> Callable[[], None]:
        call = f"on_boundary({self.name!r})"
        check_origin(origin, call)
        refusal = seam_refusal(self.record, self.context)
        if refusal is not None:
            raise ValueError(
                f"{call}: the member cannot be held at a turn boundary: {refusal}."
            )
        if self.state == "ended":
            raise RuntimeError(f"{call}: the member has ended.")
        stopping = self.gate.stopping()
        if stopping is not None:
            raise _Stopping(
                f"{call}: the swarm is stopping ({stopping}), so no callback can be registered."
            )
        return self.gate.register(callback, origin)

    def end(self, *, origin: str) -> None:
        check_origin(origin, f"end({self.name!r})")
        if self.state == "ended":
            return
        if self.state == "idle":
            # its ending begins now, and its held wait is cancelled
            self.state = "ending"
        self.gate.request_end(origin)

    @property
    def cancelled(self) -> bool:
        # its agent loop or its background drain was cancelled: the hard stop, end()'s
        # grace timer, the bridged branch, a member time limit, or a cancellation from
        # outside the swarm
        return self.context.scope.cancel_called or self.status in (
            "cancelled",
            "member_limit:time",
        )

    def result(self) -> MemberResult:
        return MemberResult(
            name=self.name,
            role=self.role,
            choose_name=self.choose_name,
            state=self.state,
            status=self.status,
            submitted=self.submitted,
            time=self.time,
            sandbox=self.sandbox,
            gate_checks=self.gates.checks,
            gate_passes=self.gates.passes,
            first_pass_time=self.gates.first_pass_time,
            persistent=self.persistent,
            answer_times=[a.time for a in self.answer_list],
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
        final: FinalName | None = None,
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
        self.bus.news = self._news
        for m in self._members:
            m.gate.stopping = lambda: self.stop_reason
            m.gate.recheck = self.bus.recheck
            m.gate.in_flight = self._in_flight
            m.gate.ending = functools.partial(self._ending, m)
            m.gate.waiting = self.bus.waits.max_wait is not None
            m.gate.record_answer = functools.partial(self._answer, m)
            m.gate.publish_idle = functools.partial(self._idle, m)
            m.gate.refuse_hold = functools.partial(self._refuse_hold, m)
        for node in caps:
            if isinstance(node, SwarmNode):
                node.stop_swarm = self.stop
        self._by_name = {m.name: m for m in self._members}
        self._state = state
        self._input = list(input)
        self._final: FinalName | None = final
        # the controller's choice of the answer, once it has made one
        self._pick: Pick | None = None
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
        self._runtime_tg: TaskGroup | None = None
        self._member_ctx: contextvars.Context | None = None
        self._queue: deque[ControlEvent] = deque()
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
        self,
        member: str | MemberHandle,
        input: str | list[ChatMessage] | None = None,
        *,
        preamble: str | Literal[False] | None = None,
    ) -> None:
        m = self._lookup(member if isinstance(member, str) else member.name)
        call = f"start({m.name!r})"
        # checked before anything changes, so a refused call leaves the member pending
        _check_preamble(preamble, call)
        messages: list[ChatMessage] = (
            list(self._input)
            if input is None
            else [ChatMessageUser(content=input)]
            if isinstance(input, str)
            else list(input)
        )
        if preamble is False and not any(
            isinstance(message, ChatMessageUser) for message in messages
        ):
            raise ValueError(
                f"{call}: the input has no user message, and preamble=False adds none; pass an input with a user message, or a preamble."
            )
        if self.stop_reason is not None:
            raise _Stopping(
                f"{call}: the swarm is stopping ({self.stop_reason}), so no member can start."
            )
        if m.state != "pending":
            raise RuntimeError(
                f"{call}: the member has already started, and a member runs once."
            )
        assert self._members_tg is not None and self._member_ctx is not None
        if preamble is not False:
            messages.append(
                ChatMessageUser(
                    content=self._preambles[m.name] if preamble is None else preamble
                )
            )
        m.state = "running"
        m.started = anyio.current_time() - self._start
        self._settle()
        # from the saved context, so the member's lever is a child of the caps and never
        # of the runtime lever, whichever task starts it
        self._member_ctx.copy().run(
            self._members_tg.start_soon, self._run_member, m, messages, name=m.name
        )
        if m.gate.end_origin is not None:
            # ended while pending: it ends at its first boundary, its grace counted from
            # now
            self._end_timer(m)
        self._notify()

    def wake(
        self, member: str | MemberHandle, input: str | list[ChatMessageUser]
    ) -> None:
        m = self._lookup(member if isinstance(member, str) else member.name)
        call = f"wake({m.name!r})"
        messages = _wake_messages(input, call)
        if self.stop_reason is not None:
            raise _Stopping(
                f"{call}: the swarm is stopping ({self.stop_reason}), so no member can be woken."
            )
        if m.state != "idle":
            raise RuntimeError(f"{call}: {_not_idle(m)}")
        m.state = "running"
        m.gate.wake(messages)
        self._settle()
        self._notify()

    async def events(self) -> AsyncGenerator[ControlEvent, None]:
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
                elif not self._live():
                    return
                else:
                    await self._changed.wait()
        finally:
            self._iterating = False

    def choose_answer(
        self, member: str | MemberHandle, reason: str | None = None
    ) -> None:
        m = self._lookup(member if isinstance(member, str) else member.name)
        if self._pick is not None:
            raise RuntimeError(
                f"choose_answer({m.name!r}): the controller has already chosen {self._pick.member!r}, and it chooses once."
            )
        if m.persistent:
            if not m.answer_list:
                raise RuntimeError(
                    f"choose_answer({m.name!r}): the member has not answered yet (its state is {m.state}, its status {m.status}); only an answer can be the swarm's answer."
                )
            self._pick = Pick(m.name, reason, answer=len(m.answer_list) - 1)
        elif not m.submitted:
            raise RuntimeError(
                f"choose_answer({m.name!r}): the member has not ended with a submission (its state is {m.state}, its status {m.status}); only a submission can be the answer."
            )
        else:
            self._pick = Pick(m.name, reason)
        self._settle()

    # the runtime

    async def run(self, controller: Callable[["SwarmControl"], Awaitable[str]]) -> None:
        from .._bus._bus import current_bus

        _refuse_checkpointing()
        timelines = MemberTimelines([m.name for m in self._members])
        interval = self._interval = LedgerInterval(
            roster=[m.name for m in self._members]
        )
        self._scorer = first_scorer()
        self._start = anyio.current_time()
        self.bus.waits.start = self._start
        # the swarm's span
        self._span_id = current_span_id()
        self._settle()
        error: BaseException | None = None
        # what escaped the swarm, which the finally below must not replace
        escaping: BaseException | None = None
        started = False
        ended = False
        bus_token: "contextvars.Token[Bus | None] | None" = None
        budget_token: contextvars.Token[BudgetState | None] | None = None
        running: int | None = None
        try:
            # in the swarm's span, before any member can start
            write_start()
            started = True
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
            # from here until the end of the finally below, swarm_result() refuses
            # reads in this sample
            running = _swarm_started()
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
                                self._runtime_tg = runtime_tg
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
                            self._final,
                            self._pick,
                            span_id=self._span_id,
                            stop_reason=self.stop_reason,
                            answers=self._answers(),
                        )
                    ended = True
        except Exception as ex:
            # from the start event or the final step, under the sample's limits
            error = escaping = ex
            raise
        except BaseException as ex:
            escaping = ex
            raise
        finally:
            try:
                # synchronous, so that it runs under cancellation too; for a swarm that
                # started, the record first, provisional unless the final step
                # completed, then the accounting
                if started:
                    results = self._results()
                    record = SwarmResultRecord(
                        members=results,
                        final=answer(
                            results, self._final, self._pick, provisional=not ended
                        ),
                    )
                    failures: list[Exception] = []
                    try:
                        write_record(record)
                    except Exception as ex:
                        failures.append(ex)
                    failures += self._account(
                        interval, timelines, record, ended=ended, error=error
                    )
                    # the error or cancellation that ended the swarm stays the one
                    # raised, else the first failure here; the others become its notes
                    # `is not None`, as an exception's truthiness is its own to define
                    first = (
                        escaping
                        if escaping is not None
                        else error
                        if error is not None
                        else failures[0]
                        if failures
                        else None
                    )
                    for failure in failures:
                        if failure is not first and first is not None:
                            first.add_note(
                                f"When the swarm ended: {type(failure).__name__}: {failure}"
                            )
                    if failures and first is failures[0]:
                        raise failures[0]
            finally:
                # last, so that the bus and the budget are current through the final
                # step and the ledger, and restored however the swarm ends
                interval.close()
                if budget_token is not None:
                    current_budget.reset(budget_token)
                if bus_token is not None:
                    current_bus.reset(bus_token)
                if running is not None:
                    _swarm_ended(running)
        if error is not None:
            raise error

    # The ledger and the metrics of a swarm that started, written however it ends, even
    # when its record could not be; returns what failed rather than raising it, so that
    # the error that ended the swarm stays the one raised.
    def _account(
        self,
        interval: LedgerInterval,
        timelines: MemberTimelines,
        record: SwarmResultRecord,
        *,
        ended: bool,
        error: BaseException | None,
    ) -> list[Exception]:
        wall_clock = anyio.current_time() - self._start
        try:
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
                        cancelled_calls=list(m.gate.cancelled_calls),
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
        except Exception as ex:
            return [ex]
        failures: list[Exception] = []
        try:
            write_ledger(ledger)
        except Exception as ex:
            failures.append(ex)
        try:
            spans = {
                m.name: m.context.span_id
                for m in self._members
                if m.context.span_id is not None
            }
            places = interval.places(spans)
            write_metrics(
                swarm_metrics(
                    ledger,
                    interval.events,
                    places,
                    [
                        MemberTimes(
                            m.name,
                            m.started,
                            m.time,
                            holds=m.gate.holds,
                            held_time=m.gate.held_time,
                            activations=0
                            if not m.persistent or m.started is None
                            else m.gate.activation + 1,
                            answers=m.answers,
                            awaiting_work_time=m.gate.awaiting_work_time,
                        )
                        for m in self._members
                    ],
                    [m.gates for m in self._members],
                    [
                        MemberWiring(tools=m.context.loop is not None)
                        for m in self._members
                    ],
                    list(self.bus.states),
                    self.bus.waits.max_wait is not None,
                    wall_clock,
                    record.final.member,
                    record.final.answer,
                )
            )
            timelines.fill(places)
        except Exception as ex:
            failures.append(ex)
        return failures

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
            if m.state in ("running", "idle"):
                m.lever.close(reason)
            if m.state == "idle":
                # its ending begins now
                m.state = "ending"
            # a held member ends at once, at its boundary
            m.gate.cancel_chain()
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
            if m.answer_list and status == "submitted":
                # a persistent member ended after an answer: its last answer
                state.output = m.answer_list[-1].output
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
            m.gate,
            end,
        )

    # A persistent member's answer, from its gate: recorded with its swarm time, as
    # evidence in the member's span, and settled, since it may now be the answer.
    def _answer(self, m: MemberRun, output: ModelOutput) -> None:
        index = len(m.answer_list)
        m.answer_list.append(
            _Answer(
                output=output.model_copy(deep=True),
                time=anyio.current_time() - self._start,
            )
        )
        swarm_event("answer", member=m.name, answer=index)
        self._settle()

    # A persistent member's hold after its answer was admitted: `idle` and its event
    # together, in one synchronous step.
    def _idle(self, m: MemberRun) -> None:
        m.state = "idle"
        self._queue.append(
            MemberIdle(m.name, len(m.answer_list) - 1, news=self.bus.has_news(m.name))
        )
        self._notify()

    # A delivered record that is news to an idle member, from the bus.
    def _news(self, member: str, record: "Record") -> None:
        self._queue.append(MemberNews(member, record.channel, record.id, record.sender))
        self._notify()

    # A persistent member's hold after its answer was refused: its ending begins, so
    # it is never available for a wake, and it stays live until its `MemberEnded`.
    def _refuse_hold(self, m: MemberRun) -> None:
        if m.state == "running":
            m.state = "ending"
            self._notify()

    def _end(self, m: MemberRun, status: str) -> None:
        # the member counts as live until its event is queued
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
        swarm_event(
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

    # the provisional answer, after each change
    def _settle(self) -> None:
        settle(
            self._state,
            self._results(),
            self._outputs(),
            self._final,
            self._pick,
            provisional=True,
            answers=self._answers(),
        )

    def _results(self) -> list[MemberResult]:
        return [m.result() for m in self._members]

    # the ended non-persistent members' outputs, kept here rather than in the record
    def _outputs(self) -> dict[str, ModelOutput]:
        return {
            m.name: m.agent_state.output
            for m in self._members
            if m.state == "ended" and not m.persistent
        }

    # the persistent members' answers, in order
    def _answers(self) -> dict[str, list[ModelOutput]]:
        return {
            m.name: [a.output for a in m.answer_list]
            for m in self._members
            if m.persistent
        }

    # An end request, from end() or a callback's "end", for a running member: a turn may
    # not reach its next boundary soon, and the agent's cleanup after the gate ends it
    # (a wrapper's `finally`, react's MCP teardown) may run long, so either way it gets
    # the effective grace. A pending member's starts when it is started.
    def _ending(self, m: MemberRun) -> None:
        if m.state in ("running", "ending"):
            self._end_timer(m)

    # end()'s grace: the member is cancelled, as the hard stop cancels one, if it is
    # still running when the effective grace has passed. The deadline is fixed now, so
    # work that blocks the event loop before the timer's task runs counts against it.
    def _end_timer(self, m: MemberRun) -> None:
        assert self._runtime_tg is not None
        deadline = anyio.current_time() + self._grace_now()
        self._runtime_tg.start_soon(self._end_grace, m, deadline)

    async def _end_grace(self, m: MemberRun, deadline: float) -> None:
        with anyio.CancelScope(deadline=deadline):
            while m.started_not_ended:
                await self._changed.wait()
        if m.started_not_ended:
            m.context.scope.cancel()

    # the model calls in flight now, for the turn gate's cancelled chains
    def _in_flight(self) -> list[str]:
        return [] if self._interval is None else self._interval.pending_calls()

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
            if m.started_not_ended:
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

    # live for the controller's iterator: a member working, or one whose ending has
    # begun and whose `MemberEnded` is still to come; an idle member is not
    def _live(self) -> bool:
        return any(m.state in ("running", "ending") for m in self._members)

    async def _until_quiet(self) -> None:
        while any(m.started_not_ended for m in self._members):
            await self._changed.wait()

    def _notify(self) -> None:
        self._changed.set()
        self._changed = anyio.Event()


def _check_preamble(preamble: object, call: str) -> None:
    if preamble is None or preamble is False:
        return
    if not isinstance(preamble, str):
        raise TypeError(
            f"{call}: preamble must be a string, False or None, not {type(preamble).__name__}."
        )
    if not preamble.strip():
        raise ValueError(
            f"{call}: preamble is empty or blank; pass preamble=False to leave the preamble out."
        )


# A wake's input as the messages to append: a string as one new user message, and each
# message of a list as a detached copy with a new id and no `source`, so that a list
# passed twice never repeats an id the member's conversation holds and no wake message
# reads as the starting input.
def _wake_messages(input: object, call: str) -> list[ChatMessage]:
    if isinstance(input, str):
        if not input:
            raise ValueError(f"{call}: input is an empty string.")
        return [ChatMessageUser(content=input)]
    if not isinstance(input, list) or not input:
        raise ValueError(
            f"{call}: input must be a non-empty string or a non-empty list of user messages."
        )
    messages: list[ChatMessage] = []
    for item in cast(list[object], input):
        if not isinstance(item, ChatMessageUser):
            raise ValueError(
                f"{call}: input holds a {type(item).__name__}; a wake's input is user messages only."
            )
        messages.append(
            item.model_copy(update={"id": uuid(), "source": None}, deep=True)
        )
    return messages


def _not_idle(m: MemberRun) -> str:
    if not m.persistent:
        return "the member is not persistent, so it is never idle."
    match m.state:
        case "pending":
            return "the member has not started; start it with start()."
        case "running":
            return "the member is running: it is working, or has not yet been held since its answer."
        case _:
            return f"the member is {m.state}."


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
    return as_group(error).split(anyio.get_cancelled_exc_class())[1]


# A resumed sample restores each member's history and store and the sample's limits,
# but not the bus, the board or the swarm's caps, so a resumed swarm would come back
# inconsistent. inspect_ai gives a sample without checkpointing the no-op checkpointer.
def _refuse_checkpointing() -> None:
    active = sample_active()
    if active is not None and not isinstance(active.checkpointer, _NoopCheckpointer):
        raise PrerequisiteError(
            "Checkpoint and resume are not supported for swarms yet. Set `checkpoint=False` on `Task()` or `eval()`, or remove every checkpoint setting, including `--checkpoint` and `INSPECT_EVAL_CHECKPOINT`."
        )


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
