import asyncio
import contextvars
import functools
import math
from collections import deque
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, cast

import anyio
import anyio.lowlevel
from anyio import TaskInfo
from anyio.abc import TaskGroup
from inspect_ai._control.operator import operator_group
from inspect_ai._util.error import PrerequisiteError
from inspect_ai._util.working import known_work
from inspect_ai.agent import Agent, AgentState
from inspect_ai.log._samples import sample_active
from inspect_ai.model import ChatMessage, ChatMessageUser, ModelOutput
from inspect_ai.solver._task_state import sample_state
from inspect_ai.util import Limit, current_span_id, sample_limits
from inspect_ai.util._checkpoint.checkpointer_noop import (
    _NoopCheckpointer,  # pyright: ignore[reportPrivateUsage]
)
from inspect_ai.util._store import suspend_store_events
from pydantic import JsonValue
from shortuuid import uuid

from .._background import cancellation
from .._budget import (
    Budget,
    BudgetState,
    CostPair,
    Lever,
    MemberContext,
    SwarmNode,
    SwarmTimeNode,
    current_budget,
    current_member,
    is_bridged,
)
from .._evidence import (
    SwarmTimeline,
    claim_member_names,
    isolation_event,
    swarm_event,
    write_ledger,
    write_metrics,
)
from .._final import (
    FinalMode,
    FinalName,
    Pick,
    Synthesis,
    answer,
    chosen_output,
    finalise,
    settle,
    synthesize_answer,
)
from .._isolation import IsolationSetup, MemberIdentity, reap_member, take_census
from .._ledger import LedgerInterval, MemberAccount, SpanPlaces
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
    foreign_execs,
    gate_checks,
    swarm_metrics,
)
from .._operator import OPERATOR, SwarmOperator
from .._record import (
    AnswerResult,
    FinalRecord,
    MemberReap,
    MemberResult,
    MemberState,
    SwarmResultRecord,
    _refuse_author_objects,  # pyright: ignore[reportPrivateUsage]
    _swarm_ended,  # pyright: ignore[reportPrivateUsage]
    _swarm_started,  # pyright: ignore[reportPrivateUsage]
    write_record,
    write_start,
)
from .._result import ResultSpec, Verdict, Verifier, key_answer, verifier_label
from .._seam import BoundaryCallback, MemberGate, check_origin

if TYPE_CHECKING:
    from .._bus._bus import SwarmBus
    from .._bus._record import Record
    from .._channel._channel import Channel


@dataclass(frozen=True)
class VerdictSummary:
    """A verifier's verdict as a controller sees it: whether the submission passed and its graded value, never the explanation."""

    passed: bool
    """Whether the submission passed."""

    value: float | None
    """The verdict's graded value, if the verifier gave one; higher is better."""


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
    def verdict(self) -> VerdictSummary | None:
        """The verdict on the member's submission, when the swarm's result contract has a verifier; for a persistent member, the verdict on its latest answer. `None` without one, before the verdict is taken, or when the swarm's stop left the submission or answer unverified."""
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
            RuntimeError: If the member has ended, or its agent and work have ended and only its end is still to be recorded (under users isolation, while its leftover processes are stopped), or the swarm is stopping.
        """
        ...

    def end(self, *, origin: str) -> None:
        """End the member `stopped`, with the reason `ended by <origin>`.

        A held member ends at once: the waiting callback is cancelled, later ones do not run, and no change of theirs is applied. A running member's next model call is refused and it ends at its next boundary. A pending member ends as soon as it is started, before its setup and with no model call. If the member is still running when the swarm's grace period has passed since the request, its turn, setup or cleanup included, it is cancelled and ends `cancelled`. A member whose agent returns after the request ends `stopped`, whatever its output. A member whose background work is draining after its agent returned keeps the status its return gave (the drain keeps its own grace period), except that a return without a submission (`unsubmitted`) ends `stopped`. A second call, or a call for a member that has ended, changes nothing. The other members and the swarm go on.

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

    verdict: VerdictSummary | None = None
    """The verdict on its submission, as `MemberHandle.verdict`. With a verifier, the event waits for the verdict, so it has one unless the swarm's stop left the submission unverified. For a persistent member, however it ended, the verdict on its latest answer, which the event waits for in the same way."""


@dataclass(frozen=True)
class MemberIdle:
    """A persistent member answered and is idle, held until `SwarmControl.wake()` gives it more work.

    When the swarm's result contract has a verifier, the member stays `running` until the answer's verdict is taken, and the event carries it.
    """

    member: str
    """The member's name."""

    answer: int
    """The index of the answer it just gave, from 0."""

    news: bool
    """Whether a channel already has news for the member's own loop: something that would end a `swarm_wait` of that loop that watches everything, such as a file another agent wrote that the member has not viewed. Later writes are reported as `MemberNews`, so a controller that wakes members on news also wakes on this."""

    verdict: VerdictSummary | None = None
    """The verdict on the answer it just gave, when the swarm's result contract has a verifier. `None` without one, or when the answer was left unverified."""


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
            RuntimeError: If the member is not idle (pending members start with `start()`; a running member is working, has not yet been held since its answer, or its answer is still being verified; an ending or ended member is ending), or the swarm is stopping.
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


# The result contract's answer_key failed on a persistent member's answer: raised from
# the key's error in the member's loop, where a key's own sourceless custom limit would
# otherwise read as the member stopping itself. No classifier consumes it, so the
# member ends `errored` and the sample fails.
class AnswerKeyError(RuntimeError):
    pass


# One answer of a persistent member: its output, a detached copy, the swarm time it was
# given, its key, and the verdict on it with the swarm time that was taken.
@dataclass
class _Answer:
    output: ModelOutput
    time: float
    key: str | None = None
    verdict: Verdict | None = None
    verdict_time: float | None = None


# A member in one run of the swarm: what its handle shows, and the nodes its wrapper
# enters. Its lever and limits are created here, once per run, so that a stop can close
# the lever of a member that has started but whose task has not yet entered it.
class MemberRun:
    def __init__(
        self,
        record: Member,
        name: str,
        share: Sequence[Limit],
        identity: MemberIdentity | None = None,
    ) -> None:
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
        # under users isolation: its Linux user, and, once it has ended, what stopping
        # its leftover processes found and its commands run as another user
        self.identity = identity
        self.reap: MemberReap | None = None
        self.foreign_execs: int | None = None
        # its submission's key, and the verdict on it with the swarm time it was taken
        self.answer_key: str | None = None
        self.verified: Verdict | None = None
        self.verified_time: float | None = None
        # ended, with its submission, or its latest answer, waiting for its verdict
        self.verifying = False
        # a persistent member's latest answer is on the verification queue, and its hold
        # after that answer was admitted, waiting for the verdict to publish it idle
        self.checking = False
        self.admitted = False
        # its output held an object from task code, so it is never read again, and the
        # record holds no output for it
        self.refused = False
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
        self.context.background_starting = self.gate.background_starting

    @property
    def submitted(self) -> bool:
        return self.status == "submitted"

    # a persistent member's is its latest answer's, the one choose_answer() takes
    @property
    def verdict(self) -> VerdictSummary | None:
        if self.persistent:
            verdict = self.answer_list[-1].verdict if self.answer_list else None
        else:
            verdict = self.verified
        if verdict is None:
            return None
        return VerdictSummary(verdict.passed, verdict.value)

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
        if self.state == "ended" or self.gate.finished:
            # finished: its agent has ended, and only its end is still to be recorded
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
        # grace timer, a member time limit, or a cancellation from outside the swarm
        return self.context.scope.cancel_called or self.status in (
            "cancelled",
            "member_limit:time",
        )

    # the origin of the end request that decided how the member ended, once it has:
    # None when no end request did (a submission, a member limit, the swarm's stop)
    @property
    def end_origin(self) -> str | None:
        origin = self.gate.end_origin
        if origin is None or not self.gate.honoured or self.state != "ended":
            return None
        if self.status in ("stopped", "cancelled"):
            return origin
        if self.status == "submitted" and self.gate.answered:
            # a persistent member ended while idle keeps its last answer
            return origin
        return None

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
            # a copy, so that the record never shares the member's own output
            output=self.agent_state.output.model_copy()
            if self.state == "ended" and not self.refused
            else None,
            answer_key=self.answer_key,
            verdict=self.verified,
            verdict_time=self.verified_time,
            persistent=self.persistent,
            answer_times=[a.time for a in self.answer_list],
            answer_results=[
                AnswerResult(
                    answer_key=a.key, verdict=a.verdict, verdict_time=a.verdict_time
                )
                for a in self.answer_list
            ],
            user=None if self.identity is None else self.identity.user,
            uid=None if self.identity is None else self.identity.uid,
            home=None if self.identity is None else self.identity.home,
            reap=self.reap,
            foreign_execs=self.foreign_execs,
            end_origin=self.end_origin,
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
        chain: Sequence[FinalName | FinalMode] = ("first",),
        result: ResultSpec | None = None,
        isolation: IsolationSetup | None = None,
    ) -> None:
        # deferred: the bus imports the channels, which import this module's package
        from .._bus._bus import SwarmBus

        self._shares = shares or {}
        # the sandbox, its members' Linux users and the census roots, under users
        # isolation
        self._isolation = isolation
        identities = {} if isolation is None else isolation.identities
        self._members = [
            MemberRun(record, name, self._shares.get(name, ()), identities.get(name))
            for record, name in records
        ]
        # the channel states open here, before any member starts
        self.bus = SwarmBus(
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
        # the swarm as an operator group, registered for its run
        self.operator = SwarmOperator(
            self._members,
            lambda: self.stop_reason,
            self.stop,
            self.bus.waits.in_wait,
            self._active,
        )
        for m in self._members:
            m.gate.loop_bound = self.operator.members[m.name].loop_bound
            m.context.fatal = self._fail
        self._state = state
        self._input = list(input)
        # the final-answer chain, which check_chain() has checked against the result
        self._chain = list(chain)
        self._result = result
        # an artifact task's verdict on the environment after the stop, and whether the
        # final step, which awaits it and the submissions left unverified, began
        self._final_verdict: Verdict | None = None
        self._finalising = False
        # synthesize's outcome, once the final step has called it
        self._synthesis: Synthesis | None = None
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
        if not budget.derive:
            # the sample's limits end this swarm: its nodes charge the sample for a call
            # before they raise for it
            for node in self._nodes():
                if isinstance(node, SwarmNode):
                    node.charge_sample = True
        self._controller_scope = anyio.CancelScope()
        self._members_tg: TaskGroup | None = None
        self._runtime_tg: TaskGroup | None = None
        # set when the supervisor returns or is cancelled
        self._supervisor_ended = False
        # set as an error from a member or the controller leaves it, ending the swarm
        self._failed = False
        # the task running the swarm, set by run()
        self._host: TaskInfo | None = None
        self._member_ctx: contextvars.Context | None = None
        self._queue: deque[ControlEvent] = deque()
        # submissions and persistent members' answers waiting for their verdicts, in
        # the order they ended and were given (an answer by its index), and the scope
        # the hard stop cancels to end verification
        self._unverified: deque[tuple[MemberRun, int | None]] = deque()
        self._verifying = anyio.CancelScope()
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
        # a member ended while pending ends in its task before any setup
        self._member_ctx.copy().run(
            self._members_tg.start_soon, self._run_member, m, messages, name=m.name
        )
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
        # the operator group, refused before the swarm claims anything when another
        # swarm running in the sample has its name; nothing awaits between here and the
        # saving of its context below, so no action runs before it
        outer = current_member.get()
        self.operator.name = "swarm" if outer is None else f"swarm:{outer.name}"
        registration = ExitStack()
        registration.enter_context(operator_group(self.operator.name, self.operator))
        # until the try below, whose finally unregisters the group, any error does
        try:
            timeline = SwarmTimeline(current_span_id())
            claim_member_names([m.name for m in self._members])
            interval = self._interval = LedgerInterval(
                roster=[m.name for m in self._members]
            )
            self._scorer = first_scorer()
            self._start = anyio.current_time()
            self.bus.waits.start = self._start
            # the swarm's span
            self._span_id = current_span_id()
            self._settle()
            timeline.open()
        except BaseException:
            registration.close()
            raise
        error: BaseException | None = None
        # what escaped the swarm, which the finally below must not replace
        escaping: BaseException | None = None
        started = False
        ended = False
        bus_token: "contextvars.Token[SwarmBus | None] | None" = None
        budget_token: contextvars.Token[BudgetState | None] | None = None
        running: int | None = None
        try:
            # in the swarm's span, before any member can start
            write_start()
            started = True
            if self._isolation is not None:
                isolation_event("isolation", _isolation_data(self._isolation))
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
            # the context the operator's actions run in, as a member's or the
            # controller's calls do
            self.operator.context = contextvars.copy_context()
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
                            self._host = anyio.get_current_task()
                            try:
                                async with anyio.create_task_group() as runtime_tg:
                                    self._runtime_tg = runtime_tg
                                    runtime_tg.start_soon(self._supervise, runtime_tg)
                                    if self._time is not None:
                                        runtime_tg.start_soon(
                                            self._time_cap, self._time
                                        )
                                    # the controller and the verifier inherit the
                                    # runtime lever, which is entered once
                                    with ExitStack() as lever:
                                        for node in self._runtime_lever.nodes:
                                            lever.enter_context(node)
                                        runtime_tg.start_soon(
                                            self._controller_task, controller
                                        )
                                        verifier = self._verifier()
                                        if verifier is not None:
                                            runtime_tg.start_soon(
                                                self._verify, verifier
                                            )
                            finally:
                                # the runtime has ended: members still unwinding are
                                # being cancelled, and nothing more can be scheduled
                                self._runtime_tg = None
                except (Exception, BaseExceptionGroup) as ex:
                    # a foreign error from a member or the controller, or on trio a
                    # cancellation grouped with errors, which the wrappers do not
                    # catch. The swarm's own leaves are removed here, so that none
                    # reaches the runner as the sample's limit; a cancellation stays,
                    # and skips the final step. Only the swarm's own leaves would leave
                    # nothing to raise.
                    _, _, error = classify_errors(ex, self._nodes(), [])
                if error is None and self._isolation is not None:
                    await self._census(self._isolation)
                if error is None:
                    # the final step, outside the caps: only the sample's limits apply
                    with ExitStack() as final:
                        for node in self._final_meter.nodes:
                            final.enter_context(node)
                        self._finalising = True
                        await self._verify_left()
                        await self._take_final_verdict()
                        synthesis = await synthesize_answer(
                            self._input,
                            self._results(),
                            self._outputs(),
                            self._answers(),
                            self._chain,
                            self._pick,
                        )
                        # a model call that caught a cancellation returned: its
                        # answer is discarded, and the cancellation ends the final
                        # step here
                        await _end_if_cancelled()
                        self._synthesis = synthesis
                        finalise(
                            self._state,
                            self._input,
                            self._results(),
                            self._outputs(),
                            self._chain,
                            self._pick,
                            span_id=self._span_id,
                            stop_reason=self.stop_reason,
                            answers=self._answers(),
                            synthesis=self._synthesis,
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
                    failures: list[Exception] = []
                    chosen: FinalRecord | None = None
                    try:
                        results = self._results()
                        chosen = answer(
                            results,
                            self._chain,
                            self._pick,
                            provisional=not ended,
                            synthesis=self._synthesis,
                        )
                        write_record(
                            SwarmResultRecord(
                                members=results,
                                final=chosen,
                                kind=self._result.kind
                                if self._result is not None
                                else None,
                                verifier=verifier_label(self._result.verifier)
                                if self._result is not None
                                and self._result.verifier is not None
                                else None,
                                final_verdict=self._final_verdict,
                                isolation_incomplete=self._isolation_incomplete(),
                            )
                        )
                    except Exception as ex:
                        failures.append(ex)
                    # the record's own failure, which a sample limit must not hide
                    record_failure = failures[0] if failures else None
                    failures += self._account(
                        interval,
                        timeline,
                        chosen,
                        ended=ended,
                        error=error,
                        # the final step awaits the verifier and synthesize's model
                        # call, so a cancellation can end it
                        final_cancelled=self._finalising
                        and not ended
                        and isinstance(escaping, anyio.get_cancelled_exc_class()),
                        failed=not ended or bool(failures),
                    )
                    # the error or cancellation that ended the swarm
                    # `is not None`, as an exception's truthiness is its own to define
                    ending = escaping if escaping is not None else error
                    # the sample limit that was ending the run, when the record could
                    # not be written: the record's failure is raised instead, so that
                    # the sample fails rather than Inspect scoring it as a limit reached
                    limit = (
                        _exhaustion(ending)
                        if record_failure is not None and ending is not None
                        else None
                    )
                    if limit is not None and not limit.startswith("sample_limit:"):
                        limit = None
                    # else the error or cancellation that ended the swarm stays the one
                    # raised, else the first failure here; the others become its notes
                    first = (
                        record_failure
                        if limit is not None
                        else ending
                        if ending is not None
                        else failures[0]
                        if failures
                        else None
                    )
                    for failure in failures:
                        if failure is not first and first is not None:
                            first.add_note(
                                f"When the swarm ended: {type(failure).__name__}: {failure}"
                            )
                    if limit is not None and record_failure is not None:
                        record_failure.add_note(
                            f"When the swarm ended, the sample's {limit.removeprefix('sample_limit:')} limit was ending it."
                        )
                    if failures and first is failures[0]:
                        raise failures[0]
            finally:
                # last, so that the bus and the budget are current through the final
                # step and the ledger, and restored however the swarm ends
                registration.close()
                interval.close()
                if budget_token is not None:
                    current_budget.reset(budget_token)
                if bus_token is not None:
                    current_bus.reset(bus_token)
                if running is not None:
                    _swarm_ended(running)
                timeline.close()
        if error is not None:
            raise error

    # The ledger and the metrics of a swarm that started, written however it ends, even
    # when its record could not be (`chosen` is the record's final answer, `None` when
    # it could not be made); returns what failed rather than raising it, so that the
    # error that ended the swarm stays the one raised. `failed` says whether the run
    # already fails: it did not complete its final step, or its record was not written.
    def _account(
        self,
        interval: LedgerInterval,
        timeline: SwarmTimeline,
        chosen: FinalRecord | None,
        *,
        ended: bool,
        error: BaseException | None,
        final_cancelled: bool,
        failed: bool,
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
                        end_origin=m.end_origin,
                    )
                    for m in self._members
                ],
                runtime=self._runtime_lever,
                final=self._final_meter,
                swarm=self._swarm_meter,
                # the operator's stop is settled below, once the metrics write is known
                stop_reason=self.stop_reason,
                ended_by=None if ended else _exhaustion(error),
                interrupted=not ended,
                final_cancelled=final_cancelled,
            )
        except Exception as ex:
            return [ex]
        failures: list[Exception] = []
        spans: dict[str, str] = {}
        places: SpanPlaces | None = None
        # the metrics first, so that a failure to write them is known before the ledger
        # records how the run ended
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
                    chosen.member if chosen is not None else None,
                    chosen.answer if chosen is not None else None,
                    reap_complete=None
                    if (incomplete := self._isolation_incomplete()) is None
                    else not incomplete,
                )
            )
        except Exception as ex:
            failures.append(ex)
        if (failed or failures) and ledger.stop_reason == OPERATOR:
            # a run that failed or was cancelled keeps that ending: an operator's stop
            # is not recorded, as one can reach the run while it tears down, before the
            # stop guard knows (decision: Ransom, 2026-11-05). The metrics do not read
            # the stop reason.
            ledger = ledger.model_copy(update={"stop_reason": None})
        try:
            write_ledger(ledger)
        except Exception as ex:
            failures.append(ex)
        # last, so that the swarm's timeline holds its ledger and metrics
        if places is not None:
            try:
                timeline.fill(places, spans.values())
            except Exception as ex:
                failures.append(ex)
        return failures

    def stop(self, reason: str) -> None:
        # the first call wins; everything before the grace period is synchronous, so
        # no member can start once a stop has begun
        if self.stop_reason is not None:
            return
        # before anything changes, so that an error here leaves the swarm as it was
        grace = self._grace_now()
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
        self._effective_grace = grace
        self._stopped.set()

    # The controller's task: an error leaving it ends the swarm, so the stop guard closes
    # as it leaves, before the task group learns of it (on asyncio, a callback later).
    async def _controller_task(
        self, controller: Callable[["SwarmControl"], Awaitable[str]]
    ) -> None:
        try:
            await self._run_controller(controller)
        except BaseException as ex:
            if not cancellation(ex):
                self._fail()
            raise

    # An error from a member or the controller that ends the swarm is known: from now on
    # the stop guard refuses.
    def _fail(self) -> None:
        self._failed = True

    async def _run_controller(
        self, controller: Callable[["SwarmControl"], Awaitable[str]]
    ) -> None:
        error: Exception | None = None
        reason: object = None
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
            if status == "errored":
                # the member's error leaves its task and ends the swarm: the stop guard
                # closes before its result is published, if the member's work has not
                # already closed it
                self._fail()
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
            m.identity,
            None if m.identity is None else functools.partial(self._reap, m),
        )

    # Stops the processes a member left running, under users isolation; never raises.
    async def _reap(self, m: MemberRun) -> None:
        assert self._isolation is not None and m.identity is not None
        m.reap = await reap_member(self._isolation.environment, m.identity)

    # Users isolation's census, once every member has ended and been reaped: who owns
    # the files in the shares and the directories every member can write.
    async def _census(self, isolation: IsolationSetup) -> None:
        isolation_event("census", await take_census(isolation))

    # Under users isolation, whether a member that started may have left processes
    # running: its reaping did not end `ok`, or did not run because its task was
    # cancelled before it began; None without isolation.
    def _isolation_incomplete(self) -> bool | None:
        if self._isolation is None:
            return None
        return any(
            m.state != "pending" and (m.reap is None or m.reap.status != "ok")
            for m in self._members
        )

    # A persistent member's answer, from its gate: recorded with its swarm time, as
    # evidence in the member's span, and settled, since it may now be the answer; then,
    # as a submission is when its member ends, keyed and queued for its verdict.
    def _answer(self, m: MemberRun, output: ModelOutput) -> None:
        self._approve(m, output)
        index = len(m.answer_list)
        answer = _Answer(
            output=output.model_copy(deep=True),
            time=anyio.current_time() - self._start,
        )
        m.answer_list.append(answer)
        swarm_event("answer", member=m.name, answer=index)
        self._settle()
        key = self._result.answer_key if self._result is not None else None
        if key is not None:
            # task code, after the answer is recorded and settled, so that an error,
            # which fails the sample, leaves the answer and the record agreeing
            try:
                answer.key = key_answer(key, answer.output.completion, m.name)
            except Exception as ex:
                raise AnswerKeyError(
                    f"The result contract's answer_key failed on answer {index} of member {m.name!r}."
                ) from ex
            # the key may change the answer under vote
            self._settle()
        if self._verifier() is not None and not self._verifying.cancel_called:
            # the member is published idle, or its end, once its verdict is taken
            m.checking = True
            self._unverified.append((m, index))
            self._notify()

    # A persistent member's hold after its answer was admitted: published idle at once,
    # or, while its answer is checking, once the verification queue settles it.
    def _idle(self, m: MemberRun) -> None:
        if m.checking:
            m.admitted = True
        else:
            self._publish_idle(m)

    # `idle` and its event together, in one synchronous step; its idle time starts now.
    def _publish_idle(self, m: MemberRun) -> None:
        m.state = "idle"
        m.gate.idle_at = anyio.current_time()
        self._queue.append(
            MemberIdle(
                m.name,
                len(m.answer_list) - 1,
                news=self.bus.has_news(m.name),
                verdict=m.verdict,
            )
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
        refusal: TypeError | None = None
        if not m.refused:
            try:
                self._approve(m, m.agent_state.output)
            except TypeError as ex:
                refusal = ex
        if m.refused:
            # however it ended, a refused output ends it `errored`, before its ending
            # is published, settled or accounted; its own output is never read again,
            # by the answer or the record
            status = m.status = "errored"
            m.agent_state.output = ModelOutput()
        # releases the other members' waits
        self.bus.member_ended(m.name, status)
        m.time = anyio.current_time() - self._start
        if m.context.span_id is not None:
            assert self._interval is not None
            m.gates = gate_checks(
                self._interval.events, m.context.span_id, self._scorer, self._start
            )
        if m.identity is not None:
            assert self._interval is not None
            m.foreign_execs = (
                0
                if m.context.span_id is None
                else foreign_execs(
                    self._interval.events,
                    self._interval.places({m.name: m.context.span_id}),
                    m.name,
                    m.identity.user,
                )
            )
        # in the member's span, unless it ended before entering it: the operator's
        # messages it never drained, then its result
        self.operator.members[m.name].ended()
        swarm_event(
            "member_result",
            member=m.name,
            span_id=m.context.span_id,
            status=status,
            time=m.time,
            sandbox=m.sandbox,
            end_origin=m.end_origin,
        )
        self._settle()
        if refusal is not None:
            # after the member's end is recorded, as for a failing answer key
            raise refusal
        if m.persistent:
            # its answers were keyed and queued as they were given: its output is its
            # last answer, or no answer, and nothing is keyed or verified again
            m.admitted = False
            if m.checking:
                # counts as running until its latest answer's verdict is taken and its
                # event queued
                m.verifying = True
            else:
                self._queue.append(MemberEnded(m.name, status, m.submitted, m.verdict))
            self._notify()
            return
        key = self._result.answer_key if self._result is not None else None
        if m.submitted and key is not None:
            # task code, after the member's end is recorded and its output settled, so
            # that an error, which fails the sample, leaves the answer and the record
            # agreeing
            m.answer_key = key_answer(key, m.agent_state.output.completion, m.name)
            # the key may change the answer under vote
            self._settle()
        if (
            m.submitted
            and self._verifier() is not None
            and not self._verifying.cancel_called
        ):
            # counts as running until its verdict is taken and its event queued
            m.verifying = True
            self._unverified.append((m, None))
        else:
            self._queue.append(MemberEnded(m.name, status, m.submitted))
        self._notify()

    # An output from a member, checked before the runtime settles or records it (by
    # `_answer()` for a persistent member's answer, and `_end()` for its ending
    # output): only Inspect's output classes and model data. One that holds anything else fails the sample with a `TypeError`, and
    # the member's outputs are not read again. Task code is trusted, so this promises
    # the error and the partial record, not that none of the object's own methods ran
    # before it (decision: Ransom, 2026-11-04).
    def _approve(self, m: MemberRun, output: ModelOutput) -> None:
        try:
            _refuse_author_objects(output, m.name)
        except TypeError:
            m.refused = True
            raise

    def _verifier(self) -> Verifier | None:
        return self._result.verifier if self._result is not None else None

    # The submissions' and persistent members' answers' verdicts, one at a time in the
    # order they ended and were given, in the swarm's span and under the runtime lever,
    # each before its member's event is queued. The hard stop ends it, and a submission
    # or answer it has not verified by then, or whose verification the swarm's stop
    # refused, has no verdict. Once cancelled, by the hard stop or the sample's own
    # cancellation, it calls no further verifier, even when the one running caught the
    # cancellation and returned.
    async def _verify(self, verifier: Verifier) -> None:
        with self._verifying:
            while not _cancelled():
                while self._unverified and not _cancelled():
                    m, index = self._unverified[0]
                    await self._take_verdict(m, verifier, index)
                    self._unverified.popleft()
                    self._settled(m, index)
                if not _cancelled():
                    await self._changed.wait()
        while self._unverified:
            self._settled(*self._unverified.popleft())

    # The verdict on a member's submission, or with `index` on that answer of a
    # persistent member.
    async def _take_verdict(
        self, m: MemberRun, verifier: Verifier, index: int | None
    ) -> None:
        completion = (
            m.agent_state.output.completion
            if index is None
            else m.answer_list[index].output.completion
        )
        active = sample_state()
        metadata = active.metadata if active is not None else {}
        verdict: object = None
        error: Exception | None = None
        try:
            # its whole call counts as working time unless it marks its own waits,
            # also while a held member's wait marks the sample as waiting
            with known_work():
                verdict = await verifier(completion, metadata)
        except Exception as ex:
            error = ex
        if error is not None:
            # the lever closed by the stop, or a reached cap, refused its model call
            _, _, foreign = classify_errors(
                error, self._caps, self._runtime_lever.nodes
            )
            if foreign is not None:
                raise foreign
            return
        if _cancelled():
            # cancelled while the verifier ran, which caught the cancellation and
            # returned: the submission stays unverified
            return
        verdict = _checked_verdict(
            verdict,
            f"member {m.name!r}"
            if index is None
            else f"answer {index} of member {m.name!r}",
        )
        time = anyio.current_time() - self._start
        if index is None:
            m.verified, m.verified_time = verdict, time
        else:
            answer = m.answer_list[index]
            answer.verdict, answer.verdict_time = verdict, time
        # in the swarm's span; the explanation stays in the record
        swarm_event(
            "verdict",
            member=m.name,
            span_id=m.context.span_id,
            passed=verdict.passed,
            value=verdict.value,
            time=time,
            answer=index,
        )
        # the verdict may change the answer under verify
        self._settle()

    # In the final step: the submissions the run left unverified, which ended during
    # grace or whose verification the stop refused or ended, in the order they ended.
    # Their `MemberEnded` events were queued without a verdict, so only the record and
    # the answer see these.
    async def _verify_left(self) -> None:
        verifier = self._verifier()
        if verifier is None:
            return
        left = [
            m
            for m in self._members
            if m.submitted and not m.persistent and m.verified is None
        ]
        for m in sorted(left, key=lambda m: m.time or 0.0):
            await self._take_verdict(m, verifier, None)
            # a verifier that caught a cancellation returned, and its verdict was
            # discarded: the cancellation ends the final step here
            await _end_if_cancelled()

    # In the final step, for an artifact task with a verifier: the verdict on the
    # environment the task's scorer will grade, called with the final answer's
    # completion, or "" without one. An error from it fails the sample.
    async def _take_final_verdict(self) -> None:
        verifier = self._verifier()
        if verifier is None or self._result is None or self._result.kind != "artifact":
            return
        chosen = answer(self._results(), self._chain, self._pick, provisional=True)
        completion = (
            ""
            if chosen.member is None
            else chosen_output(chosen, self._outputs(), self._answers()).completion
        )
        active = sample_state()
        metadata = active.metadata if active is not None else {}
        verdict = await verifier(completion, metadata)
        # a verifier that caught a cancellation returned: its verdict is discarded, and
        # the cancellation ends the final step here
        await _end_if_cancelled()
        self._final_verdict = _checked_verdict(verdict, "the final environment")

    # A submission or an answer off the verification queue, with its verdict or without
    # one: a submission's `MemberEnded` queued; an answer's member no longer checking,
    # with its `MemberEnded` queued if it has ended, or published idle if its hold was
    # admitted and it is still held, with neither a stop nor an end request reaching it
    # (otherwise it is ending through its lever).
    def _settled(self, m: MemberRun, index: int | None) -> None:
        if index is None:
            self._verified(m)
            return
        m.checking = False
        if m.verifying:
            self._verified(m)
        elif m.admitted:
            m.admitted = False
            if m.held and m.gate.stopping() is None and m.gate.end_origin is None:
                self._publish_idle(m)

    def _verified(self, m: MemberRun) -> None:
        assert m.status is not None
        m.verifying = False
        self._queue.append(MemberEnded(m.name, m.status, m.submitted, m.verdict))
        self._notify()

    # the provisional answer, after each change
    def _settle(self) -> None:
        settle(
            self._state,
            self._results(),
            self._outputs(),
            self._chain,
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
        # a draining member's drain has a grace of its own, and once the runtime has
        # begun to end the members still unwinding are being cancelled
        if m.state in ("running", "ending") and not m.gate.draining and self._active():
            self._end_timer(m)

    # Whether the runtime can still take a stop: false before it starts, once it has
    # ended, and from the moment an error (a member's or the controller's) or a
    # cancellation from outside begins to tear it down, while its members unwind. A
    # stop then would start a timer in a task group that is closing. Best effort: a stop
    # it misses is accepted, and _account() keeps the failure in the ledger.
    def _active(self) -> bool:
        runtime = self._runtime_tg
        return (
            runtime is not None
            and not self._supervisor_ended
            and not self._failed
            and not runtime.cancel_scope.cancel_called
            # a cancellation that has reached the task running the swarm and that it has
            # not yet seen: a member's error, a scope around the swarm, or a cancel of
            # the task itself. An operator's action runs in another task, so it asks
            # the swarm's task rather than its own.
            and not (self._host is not None and self._host.has_pending_cancellation())
        )

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
        if m.started_not_ended and not m.gate.finished:
            if not m.context.scope.cancel_called:
                # the request's grace, not another cancellation, ends the member
                m.gate.honoured = True
            m.context.scope.cancel()

    # the model calls in flight now, for the turn gate's cancelled chains
    def _in_flight(self) -> list[str]:
        return [] if self._interval is None else self._interval.pending_calls()

    async def _time_cap(self, seconds: float) -> None:
        await anyio.sleep(seconds)
        self.stop("swarm_time")

    async def _supervise(self, runtime_tg: TaskGroup) -> None:
        try:
            await self._stopped.wait()
            # grace: members finish the calls they have in flight and are refused the
            # next
            with anyio.move_on_after(self._effective_grace):
                await self._until_quiet()
            # hard stop, which also ends verification; a member whose agent and work
            # have ended has nothing to cancel, and its cleanup (reaping, under users
            # isolation) must not read as cancelled work in the ledger
            for m in self._members:
                if m.started_not_ended and not m.gate.finished:
                    m.context.scope.cancel()
            self._verifying.cancel()
            await self._until_quiet()
            # ends the time cap's timer
            runtime_tg.cancel_scope.cancel()
        finally:
            # once every member has ended, or when a cancellation reaches the runtime,
            # whichever scope it comes from
            self._supervisor_ended = True

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

    # live for the controller's iterator: a member working, one whose ending has begun
    # and whose `MemberEnded` is still to come, or one waiting for its verdict; an idle
    # member is not
    def _live(self) -> bool:
        return any(
            m.state in ("running", "ending") or m.verifying for m in self._members
        )

    async def _until_quiet(self) -> None:
        while any(m.started_not_ended or m.verifying for m in self._members):
            await self._changed.wait()

    def _notify(self) -> None:
        self._changed.set()
        self._changed = anyio.Event()


# The isolation event's data: the sandbox, each member's user, uid and home, and the
# census roots.
def _isolation_data(isolation: IsolationSetup) -> dict[str, JsonValue]:
    return {
        "sandbox": isolation.sandbox,
        "members": [
            {"name": name, "user": i.user, "uid": i.uid, "home": i.home}
            for name, i in isolation.identities.items()
        ],
        "census_roots": [
            {"path": root.path, "kind": root.kind} for root in isolation.census_roots
        ],
        "census_roots_capped": isolation.census_roots_capped,
    }


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
            return "the member is running: it is working, has not yet been held since its answer, or its answer is still being verified."
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


# Whether the current task is cancelled: by its own scope or an enclosing one, or, on
# asyncio, by the task's own `cancel()` (as `asyncio.timeout()` and `wait_for()` do),
# which no AnyIO scope knows of and which stays requested after a callee caught it.
def _cancelled() -> bool:
    if anyio.current_effective_deadline() == -math.inf:
        return True
    try:
        task = asyncio.current_task()
    except RuntimeError:
        # not on asyncio
        return False
    return task is not None and task.cancelling() > 0


# Raises the current task's cancellation, when `_cancelled()`, after a callee that
# caught it returned.
async def _end_if_cancelled() -> None:
    await anyio.lowlevel.checkpoint_if_cancelled()
    if _cancelled():
        # asyncio's own cancellation, which the checkpoint does not deliver
        raise asyncio.CancelledError()


# A verifier's result, checked as returned rather than as annotated (task code), by
# its actual classes rather than the ones its values report; `subject` names what it
# judged, for the error.
def _checked_verdict(verdict: object, subject: str) -> Verdict:
    if not issubclass(type(verdict), Verdict):
        raise TypeError(
            f"The result contract's verifier must return a Verdict, got a {type(verdict).__name__} for {subject}."
        )
    verdict = cast(Verdict, verdict)
    passed = cast(object, verdict.passed)
    value = cast(object, verdict.value)
    explanation = cast(object, verdict.explanation)
    # the values themselves are not printed: an int too long to print, or a repr() that
    # raises, would replace this error
    if not (
        type(passed) is bool
        and (value is None or _exact_float(value))
        and (explanation is None or issubclass(type(explanation), str))
    ):
        raise TypeError(
            f"The result contract's verifier must return a Verdict whose passed is a bool, value a float (or an int a float holds exactly) or None and explanation a str or None, got passed of type {type(passed).__name__}, value of type {type(value).__name__} and explanation of type {type(explanation).__name__} for {subject}."
        )
    return verdict


# A number the record's float field holds exactly: a float, or an int a float equals.
def _exact_float(value: object) -> bool:
    cls = type(value)
    if issubclass(cls, float):
        return True
    if cls is bool or not issubclass(cls, int):
        return False
    try:
        return float(cast(int, value)) == value
    except OverflowError:
        return False
