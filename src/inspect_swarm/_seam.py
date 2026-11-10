import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal, TypeAlias

import anyio
from inspect_ai._util.json import to_json_safe
from inspect_ai._util.working import known_work
from inspect_ai.agent import AgentChannel, AgentState
from inspect_ai.model import (
    ChatMessage,
    ChatMessageAssistant,
    ChatMessageSystem,
    ChatMessageTool,
    ChatMessageUser,
    ModelOutput,
)
from inspect_ai.util import suspend_working_limit
from pydantic import JsonValue

from ._budget import Lever, MemberContext, SwarmTimeNode
from ._evidence import seam_boundary, seam_changed

BoundaryAfter: TypeAlias = Literal["start", "tools", "reply", "user", "wake"]


@dataclass(frozen=True)
class Boundary:
    """A member at a turn boundary of its own agent loop, as a boundary callback receives it."""

    member: str
    """The member's name."""

    index: int
    """The boundary's place among the boundaries of the member's own loop, from 0 before its first generation."""

    activation: int
    """The member's activation: 0 until a persistent member is first woken, then one more at each wake."""

    after: BoundaryAfter
    """What the conversation ends with: `start` at index 0, `tools` after a tool result, `reply` after an assistant message with no tool calls, `wake` after a persistent member's wake input, and `user` after anything else."""

    messages: list[ChatMessage]
    """A copy of the conversation: the member's at the first callback, and as the callbacks before it left it at each later one. Changing it changes nothing; return the conversation instead."""

    output: ModelOutput
    """A copy of the output of the member's last generation."""


BoundaryCallback: TypeAlias = Callable[
    [Boundary], Awaitable[list[ChatMessage] | Literal["end"] | None]
]
"""A boundary callback: called with a `Boundary`, it returns `None` to continue with the messages it received, a list of messages to continue with that conversation, or `"end"` to end the member."""

# an origin as the evidence names it; `swarm` is the runtime's own
_ORIGIN = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
_RUNTIME = "swarm"

Outcome = Literal["continue", "end", "stop", "error", "rejected", "cancelled"]


def check_origin(origin: object, call: str) -> str:
    if not isinstance(origin, str) or not _ORIGIN.fullmatch(origin):
        raise ValueError(
            f"{call}: origin must be 1 to 64 letters, digits, '_', '-', '.' or ':', got {origin!r}."
        )
    if origin == _RUNTIME:
        raise ValueError(
            f"{call}: the origin {_RUNTIME!r} is reserved for the swarm's own callbacks."
        )
    return origin


def ended_reason(origin: str) -> str:
    return f"ended by {origin}"


# why a withdrawable callback was withdrawn, as the operator lists it
STARTS_BACKGROUND = "it starts background work, which would go on while it was held"
NO_STATE = (
    "its agent loop does not pass its state, so it cannot be held at a turn boundary"
)
LOOP_ENDED = "its agent loop has ended, and a later loop cannot be held"
BRIDGED = "it is bridged: its CLI runs its own turns, and opens no agent channel"

_BACKGROUND_RUNNING = (
    "has background work running, so it cannot be held at a turn boundary"
)


# The SHA-256 of a message as the log serializes it, its id excluded: through
# Inspect's safe serializer, which writes a value JSON cannot hold as null and escapes
# text UTF-8 cannot encode, such as a lone surrogate.
def message_hash(message: ChatMessage) -> str:
    data = json.loads(to_json_safe(message, exclude={"id"}, indent=None))
    text = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(eq=False)
class _Registration:
    callback: BoundaryCallback
    origin: str
    # the runtime's own registration for the operator's pause: withdrawn, with the
    # reason, wherever the seam would otherwise raise into the member because callbacks
    # are registered
    withdraw: Callable[[str], None] | None = None
    withdrawn: bool = False


# One member's turn gate, for one run: installed by the member wrapper around the agent
# call with inspect_ai's turn_gate(), it binds the member's own loop (the first loop to
# reach it in the member's own task), ends the member at a boundary once the swarm is
# stopping or an end was requested, and holds it there while its boundary callbacks run.
# For a persistent member it also records the own loop's answers and holds the member
# idle at the boundary after each one, until a wake gives it its next input.
@dataclass(eq=False)
class MemberGate:
    name: str
    context: MemberContext
    lever: Lever
    # the member's time limits, which pause while it is held
    time_nodes: list[SwarmTimeNode] = field(default_factory=list[SwarmTimeNode])
    # set by the runtime: the swarm's stop reason once it is stopping, the bus's
    # all-waiting check, the model calls in flight now (the ledger's event ids), and
    # what an end request starts (the grace timer that bounds the member's ending)
    stopping: Callable[[], str | None] = lambda: None
    recheck: Callable[[], None] = lambda: None
    in_flight: Callable[[], list[str]] = lambda: []
    ending: Callable[[], None] = lambda: None
    # called when the gate binds the member's own loop, after its stop and end check
    # and before its callbacks run, with whether the loop passed its state: the
    # operator's hook
    loop_bound: Callable[[AgentChannel, bool], None] = lambda channel, state: None
    # a persistent member's: set by the runtime, whether the swarm has a channel that
    # wakes waits, what records an answer (the output given), what sets the member
    # `idle` and publishes it, and what sets it `ending` when its hold is refused
    persistent: bool = False
    waiting: bool = False
    record_answer: Callable[[ModelOutput], None] = lambda output: None
    publish_idle: Callable[[], None] = lambda: None
    refuse_hold: Callable[[], None] = lambda: None
    # its current activation ended with an answer; a wake clears it
    answered: bool = False
    # activations begun after the first, and the seconds it was idle
    activation: int = 0
    awaiting_work_time: float = 0.0
    # the member's own task, set by the member wrapper
    task: int | None = None
    # a callback was registered for this run, unregistered since or not
    registered: bool = False
    # the member's own loop reached the gate
    reached: bool = False
    # the state the own or successor loop passed at its last boundary, None when it
    # passed none, for the member's output when its agent raises
    state: AgentState | None = None
    # the origin of the first end request
    end_origin: str | None = None
    # set by the member wrapper while the member's background work drains after its
    # agent returned, a phase the drain's own grace bounds
    draining: bool = False
    # set where the end request decides how the member ends: its first boundary or a
    # closed lever, a normal return or the drain after it, the request consumed before
    # setup, or its grace timer's cancellation. A request that something else beat
    # (a limit, the swarm's stop, a cancellation from outside) leaves it unset.
    honoured: bool = False
    # the gate ended the member for the end request, at a boundary or before a turn,
    # whichever stop closed the lever first
    end_raised: bool = False
    holds: int = 0
    held_time: float = 0.0
    # the model calls in flight when a stop or an end cancelled a chain, so the ledger
    # can find the member's calls the cancellation interrupted
    cancelled_calls: list[str] = field(default_factory=list[str])
    _callbacks: list[_Registration] = field(default_factory=list[_Registration])
    _turn_closed: bool = False
    # the top-level loop open after the own loop closed, whose own nested loops pass
    # through as the own loop's do
    _successor: AgentChannel | None = None
    _index: int = 0
    _chain: anyio.CancelScope | None = None
    # the callback the chain is awaiting while the member is held
    _holding: _Registration | None = None
    # the idle member's wake, and the input it brings
    _woken: anyio.Event | None = None
    _wake_input: list[ChatMessage] = field(default_factory=list[ChatMessage])

    @property
    def held(self) -> bool:
        return self.context.held

    # the index of the own loop's latest boundary, None before its first
    @property
    def boundary_index(self) -> int | None:
        return self._index - 1 if self._index else None

    # the own loop has bound and its block has exited
    @property
    def turn_closed(self) -> bool:
        return self._turn_closed

    # Whether callbacks were registered, or the member is persistent, but the member's
    # own loop never reached the gate, so they never ran.
    def unsupervised(self) -> bool:
        return (self.registered or self.persistent) and not self.reached

    def unsupervised_error(self) -> RuntimeError:
        if self.persistent and not self.registered:
            return RuntimeError(
                f"Member {self.name!r} is persistent, but it never reached a turn boundary of an agent loop, so it could not be held after an answer."
            )
        return RuntimeError(
            f"Member {self.name!r} had boundary callbacks, but they never ran: it never reached a turn boundary of an agent loop. Only members whose agent runs an Inspect agent loop such as react() can be held at a boundary."
        )

    # From the runtime's wake(), for a member held idle: the answered mark cleared, the
    # activation counted, and the input given to the waiting boundary.
    def wake(self, messages: list[ChatMessage]) -> None:
        assert self._woken is not None
        self.answered = False
        self.activation += 1
        self._wake_input = messages
        self._woken.set()

    def register(
        self,
        callback: BoundaryCallback,
        origin: str,
        *,
        withdraw: Callable[[str], None] | None = None,
    ) -> Callable[[], None]:
        entry = _Registration(callback, origin, withdraw)
        self._callbacks.append(entry)
        self.registered = True

        def unregister() -> None:
            if entry in self._callbacks:
                self._callbacks.remove(entry)

        return unregister

    # Withdraws the withdrawable callbacks, telling each its reason: they are
    # unregistered and never run again, one waiting is woken by its own withdraw.
    def withdraw(self, reason: str) -> None:
        withdrawn = [r for r in self._callbacks if r.withdraw is not None]
        for registration in withdrawn:
            self._callbacks.remove(registration)
            registration.withdrawn = True
        for registration in withdrawn:
            assert registration.withdraw is not None
            registration.withdraw(reason)

    # From the background owner as the member starts background work: whether a held
    # member may start it, which only a withdrawable callback holding it allows. The
    # withdrawable callbacks are withdrawn either way.
    def background_starting(self) -> bool:
        holder = self._holding
        admitted = self.held and holder is not None and holder.withdraw is not None
        self.withdraw(STARTS_BACKGROUND)
        return admitted

    # Whether one of the member's time limits has run out: it ends at its next
    # checkpoint, and is never held again.
    def overdue(self) -> bool:
        return any(node.overdue() for node in self.time_nodes)

    # From the member's nodes when they first find it bridged, as when it runs a
    # bridged subagent: the withdrawable callbacks are withdrawn, one holding the member
    # included.
    def bridge_found(self) -> None:
        self.withdraw(BRIDGED)

    def request_end(self, origin: str) -> None:
        # the first request wins; it closes the lever, so the member's next model call
        # is refused, cancels the chain if the member is held, and starts the grace that
        # bounds the rest of its ending
        if self.end_origin is not None:
            return
        self.end_origin = origin
        self.lever.close(ended_reason(origin))
        self.cancel_chain()
        self.ending()

    def cancel_chain(self) -> None:
        if self._chain is not None and not self._chain.cancel_called:
            self.cancelled_calls.extend(self.in_flight())
            self._chain.cancel()

    # TurnGate

    async def after_answer(self, channel: AgentChannel, state: AgentState) -> bool:
        # synchronous apart from its signature: nothing awaits between the answer and
        # the loop's next boundary
        if (
            not self.persistent
            or self.task is None
            or anyio.get_current_task().id != self.task
            or channel is not self.context.turn_loop
            or self._turn_closed
        ):
            # a nested loop's answer returns to its caller
            return False
        self._check_reader()
        if self.end_origin is not None:
            # the loop returns, and the member ends `stopped`
            return False
        # deferred: the member wrapper imports this module
        from ._member import output_status

        if output_status(state.output) != "submitted":
            # the loop returns from the exit it was at, and the member ends
            # `unsubmitted`
            return False
        self.record_answer(state.output)
        self.answered = True
        return True

    def closed(self, channel: AgentChannel) -> None:
        if channel is self.context.turn_loop:
            self._turn_closed = True
            # no later loop can be held
            self.withdraw(LOOP_ENDED)
        elif channel is self._successor:
            self._successor = None

    async def before_turn(
        self,
        channel: AgentChannel,
        messages: Sequence[ChatMessage],
        state: AgentState | None,
    ) -> bool:
        # a loop in another task (background work, a task group a tool starts) is never
        # the member's own
        if self.task is None or anyio.get_current_task().id != self.task:
            return False
        loop = self.context.turn_loop
        bound = loop is None
        if bound:
            self.context.turn_loop = channel
        elif loop is not channel:
            if not self._turn_closed:
                # a nested loop inside one of the own loop's turns
                return False
            if self._successor is None:
                self._successor = channel
            elif channel is not self._successor:
                # a nested loop inside one of the successor's turns
                return False
            # a second top-level loop, which the seam does not hold
            if self._callbacks:
                self.withdraw(LOOP_ENDED)
            if self._callbacks:
                raise RuntimeError(
                    f"Member {self.name!r} started a second agent loop after its first one ended. A member with boundary callbacks may run only one agent loop."
                )
            # None for a loop that does not pass its state, so that an earlier loop's
            # state is not kept for this one's output
            self.state = state
            self._end_if_requested()
            return False
        self.state = state
        self.reached = True
        index = self._index
        self._index += 1
        if self.answered:
            # the boundary after an answer, where the member is held idle
            return await self._idle(index, messages, state)
        self._check_reader()
        self._end_if_requested()
        if bound:
            self.loop_bound(channel, state is not None)
        if self._callbacks and state is None:
            self.withdraw(NO_STATE)
        if self._callbacks and self.context.background_running:
            self.withdraw(STARTS_BACKGROUND)
        if not self._callbacks:
            return False
        if state is None:
            raise RuntimeError(
                f"Member {self.name!r} has boundary callbacks, but its agent loop does not pass its state to before_turn(), so a callback could not be given its output."
            )
        if self.context.background_running:
            raise RuntimeError(f"Member {self.name!r} {_BACKGROUND_RUNNING}.")
        return await self._hold(index, messages, state)

    # A persistent member's board must be read by the loop the gate holds, when the
    # swarm has a channel that gives news: no other loop lives across its activations.
    def _reader_error(self) -> RuntimeError | None:
        loop = self.context.loop
        if (
            not self.persistent
            or not self.waiting
            or loop is None
            or loop is self.context.turn_loop
        ):
            return None
        return RuntimeError(
            f"Member {self.name!r} is persistent and reads the swarm's channels only through a subagent. A persistent member that reads the swarm's channels must have swarm_tools() in its own tools, not only in its subagents'."
        )

    def _check_reader(self) -> None:
        error = self._reader_error()
        if error is not None:
            raise error

    # The boundary after a persistent member's answer: the admission (the stop and end
    # checks, background work, the reader rule, then the time limit in the hold), each
    # refusal setting the member `ending` before it ends it, then the idle hold.
    async def _idle(
        self,
        index: int,
        messages: Sequence[ChatMessage],
        state: AgentState | None,
    ) -> bool:
        pending = self._pending()
        if pending is not None:
            self.refuse_hold()
            self._end(pending)
        if state is None:
            self.refuse_hold()
            raise RuntimeError(
                f"Member {self.name!r} is persistent, but its agent loop does not pass its state to before_turn(), so it cannot be held after an answer."
            )
        if self.context.background_running:
            self.refuse_hold()
            raise RuntimeError(f"Member {self.name!r} {_BACKGROUND_RUNNING}.")
        error = self._reader_error()
        if error is not None:
            self.refuse_hold()
            raise error
        return await self._hold(index, messages, state, idle=True)

    # ending the member

    def _pending(self) -> Literal["stop", "end"] | None:
        if self.end_origin is not None:
            return "end"
        if self.stopping() is not None:
            return "stop"
        return None

    def _end_if_requested(self) -> None:
        pending = self._pending()
        if pending is not None:
            self._end(pending)

    def _end(self, pending: Literal["stop", "end"]) -> None:
        # through the member's lever, whose error the member wrapper classifies as
        # `stopped`; the lever keeps the first reason it was closed with
        if pending == "end":
            assert self.end_origin is not None
            reason = ended_reason(self.end_origin)
            self.end_raised = True
        else:
            reason = self.stopping() or "stopped"
        raise self.lever.stop_error(reason)

    # the hold

    async def _hold(
        self,
        index: int,
        messages: Sequence[ChatMessage],
        state: AgentState,
        *,
        idle: bool = False,
    ) -> bool:
        live = list(messages)
        live_hashes = [message_hash(m) for m in live]
        after = _after(index, live)
        registrations = list(self._callbacks)
        output = state.output
        staged = live
        origins: list[str] = []
        changes: list[tuple[str, dict[str, JsonValue]]] = []
        stale: list[str] = []
        # a cancellation from outside the swarm leaves it as it is
        outcome: Outcome = "cancelled"
        detail: dict[str, JsonValue] = {}
        held_for = 0.0
        # checked after preparing the boundary, which may have used the member's last
        # time, and with nothing between here and the pause that yields
        for node in self.time_nodes:
            if node.overdue():
                if idle:
                    # a refused hold: the member is ending before its limit ends it
                    self.refuse_hold()
                # its own limit ends the member, through Inspect's path
                await node.expire()
        try:
            # 4. hold
            self.context.held = True
            self.holds += 1
            start = anyio.current_time()
            for node in self.time_nodes:
                node.pause()
            if idle:
                # with no await since the pause: the member is `idle`, and the
                # controller hears it, only once its hold is admitted
                self._woken = anyio.Event()
                self.publish_idle()
            else:
                # the first callback holds the member from the start, for the waits
                self._set_holder(registrations[0], recheck=False)
            self.recheck()
            ended: str | None = None
            try:
                with anyio.CancelScope() as chain:
                    self._chain = chain
                    if idle:
                        staged = await self._await_wake(staged)
                        # the runtime's change, recorded before the callbacks' own
                        wake_change = _diff(live, staged)
                        assert wake_change is not None
                        changes.append((_RUNTIME, wake_change))
                        after = "wake"
                        # callbacks registered while the member was idle run too
                        registrations = list(self._callbacks)
                    for registration in registrations:
                        if registration.withdrawn:
                            continue
                        if self.context.background_running:
                            # background work admitted while a withdrawable callback
                            # held the member: the next holder meets the seam's
                            # background rule, as at the start of a chain
                            self.withdraw(STARTS_BACKGROUND)
                            if registration.withdrawn:
                                continue
                            outcome = "error"
                            detail = {
                                "origin": _RUNTIME,
                                "error": RuntimeError.__name__,
                            }
                            raise RuntimeError(
                                f"Member {self.name!r} {_BACKGROUND_RUNNING}."
                            )
                        self._set_holder(registration)
                        origins.append(registration.origin)
                        boundary = Boundary(
                            member=self.name,
                            index=index,
                            activation=self.activation,
                            after=after,
                            messages=[m.model_copy(deep=True) for m in staged],
                            output=output.model_copy(deep=True),
                        )
                        try:
                            # its time counts as working time unless it marks its own
                            # waits, whatever other callbacks do
                            with known_work():
                                result = await registration.callback(boundary)
                            if self._pending() is not None:
                                break
                            if result is None:
                                continue
                            if isinstance(result, str) and result == "end":
                                ended = registration.origin
                                break
                            checked = _checked(result, self.name, registration.origin)
                        except Exception as ex:
                            outcome = "error"
                            detail = {
                                "origin": registration.origin,
                                "error": type(ex).__name__,
                            }
                            raise
                        change = _diff(staged, checked)
                        if change is not None:
                            changes.append((registration.origin, change))
                            staged = checked
            finally:
                # 5. release
                self._chain = None
                self._woken = None
                self._set_holder(None, recheck=False)
                for node in self.time_nodes:
                    node.resume()
                self.context.held = False
                held_for = anyio.current_time() - start
                self.held_time += held_for
                self.recheck()
            pending = self._pending()
            if pending is not None:
                outcome = pending
                if pending == "end":
                    assert self.end_origin is not None
                    detail = {"origin": self.end_origin}
                self._end(pending)
            if ended is not None:
                outcome = "end"
                detail = {"origin": ended}
                self.request_end(ended)
                self._end("end")
            if _same(staged, live, live_hashes):
                outcome = "continue"
                return False
            append = _appended(staged, live, live_hashes)
            if not append:
                staged, stale = _without_stale_summaries(staged, live, live_hashes)
                if not any(isinstance(m, ChatMessageUser) for m in staged):
                    outcome = "error"
                    detail = {"origin": _RUNTIME, "error": ValueError.__name__}
                    raise ValueError(
                        f"Member {self.name!r}: once the compaction summaries the boundary callbacks' changes made stale were removed, the conversation had no user message left, and a conversation must keep one."
                    )
            try:
                if not isinstance(messages, list):
                    raise TypeError(
                        f"Member {self.name!r}: a boundary callback changed the conversation, but the member's agent loop did not pass its own message list to before_turn(), so the change cannot be applied. A callback that changes the conversation needs the loop's own message list."
                    )
                if append:
                    messages.extend(staged[len(live) :])
                else:
                    messages[:] = staged
            except Exception as ex:
                outcome = "rejected"
                detail = {"error": type(ex).__name__}
                raise
            outcome = "continue"
            return not append
        finally:
            # synchronous, so that it is written however the chain ends
            applied = outcome == "continue"
            seam_boundary(
                self.name,
                boundary=index,
                after=after,
                origins=origins,
                held_for=held_for,
                outcome=outcome,
                messages=(len(live), len(messages)),
                detail=detail,
            )
            for origin, change in changes:
                seam_changed(
                    self.name,
                    boundary=index,
                    origin=origin,
                    change=change,
                    applied=applied,
                )
            if stale:
                seam_changed(
                    self.name,
                    boundary=index,
                    origin=_RUNTIME,
                    change={"removed": list[JsonValue](stale)},
                    applied=applied,
                )

    # The callback the chain awaits now, and for the bus whether it is the operator's
    # pause, the only withdrawable registration: a member that pause holds is active, so
    # the waits are rechecked when that changes.
    def _set_holder(
        self, registration: _Registration | None, *, recheck: bool = True
    ) -> None:
        before = self.context.operator_hold
        self._holding = registration
        self.context.operator_hold = (
            registration is not None and registration.withdraw is not None
        )
        if recheck and self.context.operator_hold != before:
            self.recheck()

    # The idle wait, in the chain's cancel scope, so that a stop or an end cancels it;
    # counted as waiting for the sample's working limit. Returns the staged
    # conversation with the wake's input appended.
    async def _await_wake(self, staged: list[ChatMessage]) -> list[ChatMessage]:
        assert self._woken is not None
        since = anyio.current_time()
        try:
            with suspend_working_limit():
                await self._woken.wait()
        finally:
            self.awaiting_work_time += anyio.current_time() - since
        woken, self._wake_input = self._wake_input, []
        return [*staged, *woken]


def _after(index: int, messages: Sequence[ChatMessage]) -> BoundaryAfter:
    if index == 0:
        return "start"
    last = messages[-1] if messages else None
    if isinstance(last, ChatMessageTool):
        return "tools"
    if isinstance(last, ChatMessageAssistant) and not last.tool_calls:
        return "reply"
    return "user"


# A callback's result, checked and copied: a list of messages, each with an id of its
# own, at least one of them a user message.
def _checked(result: object, member: str, origin: str) -> list[ChatMessage]:
    where = f"Member {member!r}: the boundary callback with origin {origin!r}"
    if not isinstance(result, list):
        raise TypeError(
            f"{where} returned {type(result).__name__}; a callback returns a list of messages, 'end' or None."
        )
    messages: list[ChatMessage] = []
    for item in result:  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(
            item,
            ChatMessageSystem
            | ChatMessageUser
            | ChatMessageAssistant
            | ChatMessageTool,
        ):
            raise TypeError(
                f"{where} returned a list holding {type(item).__name__}; a callback returns a list of chat messages."  # pyright: ignore[reportUnknownArgumentType]
            )
        # detached, so that nothing the callback keeps can change the conversation
        messages.append(item.model_copy(deep=True))
    seen: set[str] = set()
    for message in messages:
        if message.id is None:
            raise ValueError(f"{where} returned a message with no id.")
        if message.id in seen:
            raise ValueError(
                f"{where} returned two messages with the id {message.id!r}."
            )
        seen.add(message.id)
    if not any(isinstance(m, ChatMessageUser) for m in messages):
        raise ValueError(
            f"{where} returned a conversation with no user message; a conversation must keep one."
        )
    return messages


# The change from `before` to `after`, by message id, or None when they are the same.
def _diff(
    before: Sequence[ChatMessage], after: Sequence[ChatMessage]
) -> dict[str, JsonValue] | None:
    before_hashes = {m.id: message_hash(m) for m in before}
    after_hashes = [message_hash(m) for m in after]
    after_ids = [m.id for m in after]
    kept = set(after_ids)
    added: list[JsonValue] = [
        {"id": m.id, "hash": h, "index": i}
        for i, (m, h) in enumerate(zip(after, after_hashes, strict=True))
        if m.id not in before_hashes
    ]
    removed: list[JsonValue] = [m.id for m in before if m.id not in kept]
    replaced: list[JsonValue] = [
        {"id": m.id, "before": before_hashes[m.id], "after": h}
        for m, h in zip(after, after_hashes, strict=True)
        if m.id in before_hashes and before_hashes[m.id] != h
    ]
    kept_before = [m.id for m in before if m.id in kept]
    kept_after = [i for i in after_ids if i in before_hashes]
    reordered = kept_before != kept_after
    if not (added or removed or replaced or reordered):
        return None
    change: dict[str, JsonValue] = {
        "added": added,
        "removed": removed,
        "replaced": replaced,
    }
    if reordered:
        change["order"] = list(after_ids)
    return change


def _same(
    messages: Sequence[ChatMessage],
    live: Sequence[ChatMessage],
    live_hashes: Sequence[str],
) -> bool:
    return len(messages) == len(live) and _prefix(messages, live, live_hashes) == len(
        live
    )


# The live messages unchanged and non-system messages added after them.
def _appended(
    messages: Sequence[ChatMessage],
    live: Sequence[ChatMessage],
    live_hashes: Sequence[str],
) -> bool:
    return (
        len(messages) > len(live)
        and _prefix(messages, live, live_hashes) == len(live)
        and not any(isinstance(m, ChatMessageSystem) for m in messages[len(live) :])
    )


# The length of the run of messages at the start of `messages` that match the live
# conversation one for one, by id and hash.
def _prefix(
    messages: Sequence[ChatMessage],
    live: Sequence[ChatMessage],
    live_hashes: Sequence[str],
) -> int:
    n = 0
    for message, current, current_hash in zip(
        messages, live, live_hashes, strict=False
    ):
        if message.id != current.id or message_hash(message) != current_hash:
            break
        n += 1
    return n


# A rewrite without the compaction summaries it made stale: those after the run of
# messages it left as they were that are themselves unchanged from the live
# conversation. A summary in that run, or one the callbacks added or replaced, stays.
def _without_stale_summaries(
    messages: list[ChatMessage],
    live: Sequence[ChatMessage],
    live_hashes: Sequence[str],
) -> tuple[list[ChatMessage], list[str]]:
    live_by_id = {m.id: h for m, h in zip(live, live_hashes, strict=True)}
    prefix = _prefix(messages, live, live_hashes)
    stale = [
        i
        for i, m in enumerate(messages)
        if i >= prefix
        and "summary" in (m.metadata or {})
        and m.id in live_by_id
        and live_by_id[m.id] == message_hash(m)
    ]
    removed = [m.id for i, m in enumerate(messages) if i in stale]
    kept = [m for i, m in enumerate(messages) if i not in stale]
    return kept, [i for i in removed if i is not None]
