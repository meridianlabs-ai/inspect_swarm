from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import anyio
from inspect_ai.tool import Tool, ToolDef, ToolError
from inspect_ai.util._span import current_agent_span_id
from pydantic import JsonValue

from .._budget import MemberContext
from .._channel._channel import WaitingState, WaitSpec
from .._evidence import comm_waited
from ._record import Record

if TYPE_CHECKING:
    from ._bus import Bus

Outcome = Literal[
    "news", "all_waiting", "stopping", "member_ending", "timeout", "cancelled"
]

_DESCRIPTION = (
    "Wait until there is something for you to look at in the swarm: a shared memory file where you watch has a change you have not viewed (another agent's write, including your own subagents'), or another member has ended. "
    "Returns at once if that is already so, and keeps returning while a changed file there stays unviewed, so view what it names, or narrow `path`. "
    "It also returns when every member is waiting, has ended or has not started, when the swarm is stopping, or after `timeout` seconds. "
    "It returns file ids and member names, never file content. "
    "Use it instead of polling with sleep."
)

_PATH = "A shared memory file or directory to watch, such as `/shared_memory/results`, or a file id such as `f-3`. Without it, any change to shared memory wakes you."

_ALL_WAITING = (
    "Nothing can change: every member is waiting, has ended or has not started."
)
_STOPPING = "The swarm is stopping."


# One call of swarm_wait while it is registered: its event and release reason, and the
# commits that released it for news. A waiter is released when its reason is set; it
# goes back to sleep with its reason cleared and a new event. Its own task, when it
# resumes, decides how the wait ended: the bus does not look at its timeout or
# cancellation, so a release or the all-waiting check can count a waiter whose timeout
# or cancellation has fired but whose task has not yet resumed, and its idle time can run
# that one scheduling step long.
@dataclass(eq=False)
class _Waiter:
    spec: WaitSpec
    # the waiting channels the loop reaches
    channels: list[str]
    event: anyio.Event = field(default_factory=anyio.Event)
    reason: Outcome | None = None
    woken_by: list[int] = field(default_factory=list[int])
    # the idle clock: the seconds its member counted as waiting on it, across returns
    # to sleep, and when the current such stretch began
    idle: float = 0.0
    idle_since: float | None = None


# The bus's waits: the registered waiters, the ends each loop has been told of, the
# members draining and the all-waiting check. Everything but `wait()`
# is synchronous, so a release, a registration and the checks between them never
# interleave with a commit or an end.
class Waits:
    def __init__(
        self,
        bus: "Bus",
        contexts: Mapping[str, MemberContext],
        states: Mapping[str, WaitingState],
        max_wait: int | None,
    ) -> None:
        self._bus = bus
        self._contexts = dict(contexts)
        self._roster = list(contexts)
        self._states = dict(states)
        self.max_wait = max_wait
        self._waiters: dict[str, _Waiter] = {}
        self._count = 0
        # the members that have ended, with their statuses, in the order they ended
        self._ended: list[tuple[str, str]] = []
        # loop agent span -> the ends a wait result of that loop has named
        self._reported: dict[str | None, set[str]] = {}
        self._draining: set[str] = set()
        # the members whose own loop has a wait in progress, from its acceptance to its
        # end, sleeping or not
        self._own_waits: set[str] = set()
        self._tool: Tool | None = None
        # the swarm's start on the runtime's clock, for the evidence's swarm times
        self.start = 0.0

    def tool(self) -> Tool | None:
        if self.max_wait is None:
            return None
        if self._tool is None:
            self._tool = self._make_tool()
        return self._tool

    def reaches(self, own: bool) -> bool:
        # whether a loop gets swarm_wait: some waiting channel reaches it
        return bool(self._reachable(own))

    # release sources

    def delivered(self, record: Record, seq: int) -> None:
        # step 4 of a delivery, after the channel applied the record
        state = self._states.get(record.channel)
        if state is None or not record.wake:
            return
        for waiter in self._unreleased():
            if record.channel in waiter.channels and state.wakes(waiter.spec, record):
                waiter.woken_by.append(seq)
                self._release(waiter, "news")

    def member_ended(self, name: str, status: str) -> None:
        self._ended.append((name, status))
        for waiter in self._unreleased():
            if waiter.spec.member != name:
                self._release(waiter, "news")
        self.recheck()

    def member_ending(self, name: str) -> None:
        # the member's own agent has returned: its waits serve nothing more
        self._draining.add(name)
        for waiter in self._waiters.values():
            if waiter.spec.member == name and waiter.reason != "stopping":
                self._release(waiter, "member_ending")

    def close(self) -> None:
        for waiter in self._waiters.values():
            self._release(waiter, "stopping")

    def recheck(self) -> None:
        # the all-waiting check, run whenever a member can have become waiting or its
        # background work started
        for waiter in self._waiters.values():
            self._clock(waiter)
        unreleased = self._unreleased()
        if not unreleased:
            return
        if any(
            self._bus.running(name) and not self._waiting(name) for name in self._roster
        ):
            return
        for waiter in unreleased:
            self._release(waiter, "all_waiting")

    # the wait

    async def wait(self, path: str | None, timeout: int | None) -> str:
        assert self.max_wait is not None
        context = self._bus.member()
        if context is None:
            raise RuntimeError(
                "The swarm_wait tool was called outside a member of its swarm."
            )
        name = context.name
        if not self._bus.running(name):
            raise ToolError(f"{name} has ended.")
        own = not self._bus.nested(context)
        if own and name in self._own_waits:
            # the idle clock counts one wait of the own loop at a time. Checked against
            # every wait in progress, not just the registered ones: a wait that found
            # news has not registered, and goes to sleep if the news is gone when it
            # resumes
            raise ToolError(
                f"{name} is already waiting: its own loop has a swarm_wait in progress."
            )
        channels = self._reachable(own)
        effective = (
            self.max_wait if timeout is None else min(max(timeout, 1), self.max_wait)
        )
        spec = WaitSpec(
            id=f"x-{self._count + 1}",
            member=name,
            loop=current_agent_span_id(),
            own=own,
            path=path,
        )
        for channel in channels:
            self._states[channel].wait_check(spec)
        # the id is given once the wait is accepted, so that a refusal skips none
        self._count += 1

        if own:
            self._own_waits.add(name)
        started = anyio.current_time()
        waiter = _Waiter(spec, channels)
        outcome: Outcome | None = None
        files: list[JsonValue] = []
        ended: list[JsonValue] = []
        try:
            deadline = started + effective
            while True:
                # what ended the wait, unless the stop or the drain did
                reason: Outcome = "news"
                if not (self._finished(name) or self._reportable(waiter)):
                    if anyio.current_time() >= deadline:
                        reason = "timeout"
                    else:
                        waiter.reason = None
                        waiter.event = anyio.Event()
                        self._register(waiter)
                        with anyio.move_on_after(deadline - anyio.current_time()):
                            await waiter.event.wait()
                        reason = waiter.reason or "timeout"
                # delivers a pending cancellation, an AnyIO one or a task's own, so that
                # the wait ends as cancelled before it names or marks anything for a
                # result that is discarded
                await anyio.lowlevel.checkpoint()
                if reason != "news" or self._finished(name) or self._reportable(waiter):
                    break
                # released for news that is gone by now: back to sleep, until the
                # wait's own deadline
            outcome = (
                "stopping"
                if self._bus.stopping
                else "member_ending"
                if name in self._draining
                else reason
            )
            lines: list[str] = []
            for channel in channels:
                news = self._states[channel].news(spec)
                if news is not None:
                    lines.extend(news.lines)
                    files.extend(news.files)
            reported = self._reported.setdefault(spec.loop, set())
            ends = [
                (member, status)
                for member, status in self._ended
                if member != name and member not in reported
            ]
            if ends:
                lines.append(_ends_line(ends))
            release = _release_line(outcome, name, effective)
            if release is not None:
                lines.append(release)
            # named, so that this loop's later waits do not report them again
            reported.update(member for member, _ in ends)
            ended = [{"member": member, "status": status} for member, status in ends]
            return "\n".join(lines)
        except anyio.get_cancelled_exc_class():
            outcome = "cancelled"
            files, ended = [], []
            raise
        finally:
            # synchronous, so that it runs under cancellation too
            self._unregister(waiter)
            if own:
                self._own_waits.discard(name)
            if outcome is not None:
                comm_waited(
                    context.span_id,
                    spec,
                    nested=not own,
                    channels=channels,
                    timeout={"requested": timeout, "effective": effective},
                    started=started - self.start,
                    duration=anyio.current_time() - started,
                    idle=waiter.idle,
                    outcome=outcome,
                    woken_by=list(waiter.woken_by),
                    files=files,
                    ended=ended,
                )

    def _make_tool(self) -> Tool:
        async def execute(path: str | None = None, timeout: int | None = None) -> str:
            """Wait for other members.

            Args:
                path: A shared memory file or directory to watch.
                timeout: Seconds to wait at most.
            """
            return await self.wait(path, timeout)

        return ToolDef(
            execute,
            name="swarm_wait",
            description=_DESCRIPTION,
            parameters={
                "path": _PATH,
                "timeout": f"Seconds to wait at most (default and maximum: {self.max_wait}).",
            },
            # alone in its loop's turn, so a loop in a wait is doing nothing else
            parallel=False,
            # results are fixed lines of ids and names, which nothing should cut
            max_output=0,
        ).as_tool()

    # the registry

    def _reachable(self, own: bool) -> list[str]:
        reach = self._bus.reach
        return [c for c in self._states if own or reach[c] == "member"]

    def _finished(self, name: str) -> bool:
        return self._bus.stopping or name in self._draining

    def _reportable(self, waiter: _Waiter) -> bool:
        # something to report to the waiting loop now: news under its filter, or an end
        # no wait of the loop has named
        reported = self._reported.get(waiter.spec.loop, set())
        return any(
            self._states[c].news(waiter.spec) is not None for c in waiter.channels
        ) or any(
            member != waiter.spec.member and member not in reported
            for member, _ in self._ended
        )

    def _register(self, waiter: _Waiter) -> None:
        self._waiters[waiter.spec.id] = waiter
        self.recheck()

    def _unregister(self, waiter: _Waiter) -> None:
        self._waiters.pop(waiter.spec.id, None)
        self._clock(waiter)

    def _unreleased(self) -> list[_Waiter]:
        return [w for w in self._waiters.values() if w.reason is None]

    def _release(self, waiter: _Waiter, reason: Outcome) -> None:
        # a released waiter no longer makes its member waiting
        waiter.reason = reason
        waiter.event.set()
        self._clock(waiter)

    def _waiting(self, name: str) -> bool:
        # the member's own loop has an unreleased waiter, and none of the member's
        # background work is running
        return any(
            self._idle(w) for w in self._waiters.values() if w.spec.member == name
        )

    def _idle(self, waiter: _Waiter) -> bool:
        # whether this waiter makes its member waiting: registered in the member's own
        # loop, not released, and none of the member's background work running
        return (
            waiter.spec.own
            and self._waiters.get(waiter.spec.id) is waiter
            and waiter.reason is None
            and self._contexts[waiter.spec.member].background_running == 0
        )

    # the idle clock. A waiter's clock runs exactly while `_idle()` holds for it. Every
    # change `_idle()` reads ends in `_clock()` for the waiters it can change: a
    # registration or return to sleep and the member's background work rising from or
    # dropping to none (through `recheck()`), a release and an unregistration. Nothing
    # else touches `idle_since`. The own loop's one wait in progress at a time (`wait()`
    # refuses a second) keeps a member's clocks from overlapping.

    def _clock(self, waiter: _Waiter) -> None:
        now = anyio.current_time()
        if self._idle(waiter):
            if waiter.idle_since is None:
                waiter.idle_since = now
        elif waiter.idle_since is not None:
            waiter.idle += now - waiter.idle_since
            waiter.idle_since = None


def _ends_line(ends: Sequence[tuple[str, str]]) -> str:
    return " ".join(f"{member} ended ({status})." for member, status in ends)


def _release_line(outcome: Outcome, name: str, effective: int) -> str | None:
    match outcome:
        case "all_waiting":
            return _ALL_WAITING
        case "stopping":
            return _STOPPING
        case "member_ending":
            return f"{name} is ending: its own agent has finished."
        case "timeout":
            return f"Nothing you waited for happened in {effective} s."
        case _:
            return None
