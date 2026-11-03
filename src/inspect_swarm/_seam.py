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
from pydantic import JsonValue

from ._budget import Lever, MemberContext, SwarmTimeNode
from ._evidence import seam_boundary, seam_changed

BoundaryAfter: TypeAlias = Literal["start", "tools", "reply", "user"]


@dataclass(frozen=True)
class Boundary:
    """A member at a turn boundary of its own agent loop, as a boundary callback receives it."""

    member: str
    """The member's name."""

    index: int
    """The boundary's place among the boundaries of the member's own loop, from 0 before its first generation."""

    after: BoundaryAfter
    """What the conversation ends with: `start` at index 0, `tools` after a tool result, `reply` after an assistant message with no tool calls, and `user` after anything else."""

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


# One member's turn gate, for one run: installed by the member wrapper around the agent
# call with inspect_ai's turn_gate(), it binds the member's own loop (the first loop to
# reach it in the member's own task), ends the member at a boundary once the swarm is
# stopping or an end was requested, and holds it there while its boundary callbacks run.
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

    @property
    def held(self) -> bool:
        return self.context.held

    # Whether callbacks were registered but the member's own loop never reached the
    # gate, so they never ran.
    def unsupervised(self) -> bool:
        return self.registered and not self.reached

    def unsupervised_error(self) -> RuntimeError:
        return RuntimeError(
            f"Member {self.name!r} had boundary callbacks, but they never ran: it never reached a turn boundary of an agent loop. Only members whose agent runs an Inspect agent loop such as react() can be held at a boundary."
        )

    def register(self, callback: BoundaryCallback, origin: str) -> Callable[[], None]:
        entry = _Registration(callback, origin)
        self._callbacks.append(entry)
        self.registered = True

        def unregister() -> None:
            if entry in self._callbacks:
                self._callbacks.remove(entry)

        return unregister

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
        # an answering loop returns, as it would with no gate
        return False

    def closed(self, channel: AgentChannel) -> None:
        if channel is self.context.turn_loop:
            self._turn_closed = True
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
        if loop is None:
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
        self._end_if_requested()
        if not self._callbacks:
            return False
        if state is None:
            raise RuntimeError(
                f"Member {self.name!r} has boundary callbacks, but its agent loop does not pass its state to before_turn(), so a callback could not be given its output."
            )
        if self.context.background_running:
            raise RuntimeError(
                f"Member {self.name!r} has background work running, so it cannot be held at a turn boundary."
            )
        return await self._hold(index, messages, state)

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
        else:
            reason = self.stopping() or "stopped"
        raise self.lever.stop_error(reason)

    # the hold

    async def _hold(
        self, index: int, messages: Sequence[ChatMessage], state: AgentState
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
                # its own limit ends the member, through Inspect's path
                await node.expire()
        try:
            # 4. hold
            self.context.held = True
            self.holds += 1
            start = anyio.current_time()
            for node in self.time_nodes:
                node.pause()
            self.recheck()
            ended: str | None = None
            try:
                with anyio.CancelScope() as chain:
                    self._chain = chain
                    for registration in registrations:
                        origins.append(registration.origin)
                        boundary = Boundary(
                            member=self.name,
                            index=index,
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
