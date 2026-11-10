import contextvars
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, ParamSpec, TypeVar

from inspect_ai._control.operator import (
    OperatorAction,
    OperatorAgent,
    OperatorAgentInfo,
    OperatorGroupInfo,
    OperatorOutcome,
)
from inspect_ai.agent import AgentChannel
from inspect_ai.agent._channel import Cancel, UserMessage
from inspect_ai.agent._channel.channel import TurnState
from inspect_ai.agent._channel.items import ChannelItem
from inspect_ai.model import ChatMessageUser

from ._evidence import swarm_event

if TYPE_CHECKING:
    from ._controller._control import MemberRun

_P = ParamSpec("_P")
_T = TypeVar("_T")

# the origin of the operator's end requests
OPERATOR = "operator"

# a stop once an error or a cancellation has begun to end the swarm's runtime
_ENDED = "The swarm is ending after an error or a cancellation, and its members still running are being cancelled."

# pause and resume come with the operator's turn-seam callback
_NO_PAUSE = "Pausing is not supported yet."

# the stop reasons the list shows as they are; any other is a controller's own
_STOP_CATEGORIES = frozenset(
    {
        "operator",
        "all_done",
        "submitted",
        "verified",
        "quiescence",
        "swarm_cap",
        "swarm_time",
    }
)


# One message the swarm accepted for a member and has not seen drained: its swarm id,
# and what the evidence says of its sending.
@dataclass(frozen=True)
class _Accepted:
    id: str
    message: ChatMessageUser
    interrupt: bool
    in_turn: bool


# The swarm as an operator group (`swarm`, or `swarm:<member>` for a swarm run in a
# member's work): one agent per roster member, under its roster name. The control
# server calls these methods synchronously in its own context, so each runs its body in
# the swarm's context, saved by the run once its bus and budget are current.
class SwarmOperator:
    def __init__(
        self,
        members: Sequence["MemberRun"],
        stop_reason: Callable[[], str | None],
        stop: Callable[[str], None],
        in_wait: Callable[[str], bool],
        active: Callable[[], bool],
    ) -> None:
        self._stop_reason = stop_reason
        self._stop = stop
        # whether the swarm's runtime can take a stop: false once an error or a
        # cancellation has begun to end it, while it unwinds its members
        self.active = active
        self.in_wait = in_wait
        # set by the run when it registers the group
        self.name = "swarm"
        self.context: contextvars.Context | None = None
        # messages accepted in this run, for their `op-<n>` ids
        self._messages = 0
        self.members = {m.name: MemberOperator(self, m) for m in members}

    def run(self, func: Callable[_P, _T], *args: _P.args, **kwargs: _P.kwargs) -> _T:
        assert self.context is not None
        return self.context.copy().run(func, *args, **kwargs)

    def next_id(self) -> str:
        self._messages += 1
        return f"op-{self._messages}"

    @property
    def stop_reason(self) -> str | None:
        return self._stop_reason()

    # OperatorGroup

    @property
    def agents(self) -> Mapping[str, OperatorAgent]:
        return self.members

    def info(self) -> OperatorGroupInfo:
        return self.run(self._info)

    def pause(self, *, dry_run: bool) -> OperatorOutcome:
        return _refused(_NO_PAUSE)

    def resume(self, *, dry_run: bool) -> OperatorOutcome:
        return _refused(_NO_PAUSE)

    def stop(self, *, dry_run: bool) -> OperatorOutcome:
        return self.run(self._stop_swarm, dry_run)

    def _info(self) -> OperatorGroupInfo:
        reason = self.stop_reason
        return OperatorGroupInfo(
            name=self.name,
            state="running" if reason is None else "stopping",
            stop=None
            if reason is None
            else reason
            if reason in _STOP_CATEGORIES
            else "controller",
            paused=False,
            agents=[m.info_now() for m in self.members.values()],
        )

    def _stop_swarm(self, dry_run: bool) -> OperatorOutcome:
        if self.stop_reason is not None:
            return OperatorOutcome(
                changed=False, detail=_dry(dry_run, "The swarm is already stopping.")
            )
        if not self.active():
            return OperatorOutcome(
                changed=False,
                detail=_dry(dry_run, _ENDED),
            )
        if not dry_run:
            self._stop(OPERATOR)
        return OperatorOutcome(
            changed=True,
            detail=_dry(
                dry_run,
                "The swarm is stopping: its members end, then it chooses its answer.",
            ),
        )


# One member as an operator agent. Messages go to the member's own loop, the one the
# turn gate holds; until that loop binds they are kept, and posted when it does.
class MemberOperator:
    def __init__(self, swarm: SwarmOperator, member: "MemberRun") -> None:
        self._swarm = swarm
        self._member = member
        # accepted and not drained, in the order accepted
        self._accepted: dict[str, _Accepted] = {}
        # accepted before the loop bound, to post when it does
        self._kept: list[ChatMessageUser] = []
        # the boundary whose turn has started, as the turn-state observer saw it
        self._turn_started: int | None = None
        self._unsubscribe: list[Callable[[], None]] = []

    # OperatorAgent

    def info(self) -> OperatorAgentInfo:
        return self._swarm.run(self.info_now)

    def message(self, text: str, *, interrupt: bool, dry_run: bool) -> OperatorOutcome:
        return self._swarm.run(self._message, text, interrupt, dry_run)

    def pause(self, *, dry_run: bool) -> OperatorOutcome:
        return _refused(_NO_PAUSE)

    def resume(self, *, dry_run: bool) -> OperatorOutcome:
        return _refused(_NO_PAUSE)

    def stop(self, *, dry_run: bool) -> OperatorOutcome:
        return self._swarm.run(self._stop, dry_run)

    # the list

    def info_now(self) -> OperatorAgentInfo:
        m = self._member
        actions: dict[OperatorAction, str | None] = {
            "message": self._message_refusal(),
            "pause": _NO_PAUSE,
            "resume": _NO_PAUSE,
            "stop": None,
        }
        return OperatorAgentInfo(
            name=m.name,
            state=m.state,
            status=m.status,
            activity=self._activity(),
            paused=False,
            actions=actions,
        )

    def _activity(self) -> str:
        m = self._member
        if m.state == "pending":
            return "not_started"
        if m.state == "ended":
            return "ended"
        if m.held:
            return "held"
        loop = m.context.turn_loop
        if loop is None or m.gate.turn_closed:
            # setup, a bridged member, an agent with no agent loop, or its loop has
            # returned
            return "unknown"
        return "in_turn" if loop.turn_active else "between_turns"

    # messages

    def _message_refusal(self) -> str | None:
        m = self._member
        if m.state == "ended":
            return "The member has ended."
        if m.context.bridged:
            return "The member is bridged: its CLI reads no agent channel, so it takes only stop."
        if m.state == "pending":
            return "The member has not started."
        if m.gate.turn_closed:
            return "The member's agent loop has ended."
        # the gate ends an ending member at its next boundary, before it drains a message
        if m.gate.end_origin is not None:
            return "The member is ending: it takes no more turns."
        if self._swarm.stop_reason is not None:
            return "The swarm is stopping: its members take no more turns."
        if not self._swarm.active():
            return _ENDED
        return None

    def _message(self, text: str, interrupt: bool, dry_run: bool) -> OperatorOutcome:
        refusal = self._message_refusal()
        if refusal is not None:
            return _refused(refusal)
        m = self._member
        loop = m.context.turn_loop
        in_turn = loop is not None and loop.turn_active
        detail = self._message_detail(loop, interrupt)
        if dry_run:
            return OperatorOutcome(changed=True, detail=_dry(True, detail))
        id = self._swarm.next_id()
        message = ChatMessageUser(
            content=text,
            source="operator",
            metadata={"inspect_swarm": {"version": 1, "type": "operator", "id": id}},
        )
        assert message.id is not None
        self._accepted[message.id] = _Accepted(id, message, interrupt, in_turn)
        if loop is None:
            self._kept.append(message)
        else:
            ref = loop._ref()  # pyright: ignore[reportPrivateUsage]
            # posted first, in the same step, so the interrupted loop's recovery always
            # finds its redirect and never waits
            ref.post(UserMessage(message))
            if interrupt and in_turn:
                ref.interrupt(Cancel(reason="user_cancel"))
        return OperatorOutcome(changed=True, detail=detail)

    def _message_detail(self, loop: AgentChannel | None, interrupt: bool) -> str:
        if loop is None:
            detail = "Kept: the member takes it at its first turn, once its agent loop starts."
            if interrupt:
                detail += " It has no turn to interrupt yet."
            return detail
        if self._member.held:
            return "Posted: the member is held at a turn boundary and takes it when the hold ends."
        if not loop.turn_active:
            detail = "Posted: the member takes it at its next turn."
            if interrupt:
                detail += " It is between turns, so there was nothing to interrupt."
            return detail
        waiting = self._swarm.in_wait(self._member.name)
        if interrupt:
            if waiting:
                return "Posted, and the member's swarm_wait was interrupted: it takes the message now."
            return "Posted, and the member's turn was interrupted: it takes the message now."
        if waiting:
            return "Posted: the member is in swarm_wait and takes it when the wait returns."
        return "Posted: the member takes it at its next turn, once this turn's tool calls finish."

    # From the turn gate, in the member's task, when it binds the member's own loop:
    # after its stop and end check, so never once the swarm is stopping or an end was
    # requested, and before its callbacks run, so kept messages are drained at this
    # first boundary.
    def loop_bound(self, channel: AgentChannel) -> None:
        if self._swarm.stop_reason is not None:
            return
        self._unsubscribe = [
            channel.subscribe_drained(self._drained),
            channel.subscribe_turn_state(self._turn_state),
        ]
        ref = channel._ref()  # pyright: ignore[reportPrivateUsage]
        for message in self._kept:
            ref.post(UserMessage(message))
        self._kept.clear()

    # observers on the own loop, synchronous in the loop's task, so in the member's span

    def _turn_state(self, state: TurnState) -> None:
        if state == "started":
            self._turn_started = self._member.gate.boundary_index

    def _drained(self, items: list[ChannelItem]) -> None:
        latest = self._member.gate.boundary_index
        for item in items:
            if not isinstance(item, UserMessage) or item.message.id is None:
                continue
            accepted = self._accepted.pop(item.message.id, None)
            if accepted is None:
                continue
            if latest is not None and self._turn_started == latest:
                # after_cancel() drained it, after the latest boundary's turn was
                # interrupted: the loop goes on to the next boundary
                boundary, recovery = latest + 1, True
            else:
                # before_turn() drained it at the latest boundary, after its callbacks
                boundary, recovery = latest, False
            self._event(accepted, "drained", boundary, recovery)

    # From the runtime when the member ends, before its `member_result`: every message
    # still kept or queued, never drained.
    def ended(self) -> None:
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe = []
        self._kept.clear()
        for accepted in self._accepted.values():
            self._event(accepted, "not_drained", None, None)
        self._accepted.clear()

    def _event(
        self,
        accepted: _Accepted,
        outcome: str,
        boundary: int | None,
        recovery: bool | None,
    ) -> None:
        swarm_event(
            "operator_message",
            member=self._member.name,
            id=accepted.id,
            message_id=accepted.message.id,
            interrupt=accepted.interrupt,
            in_turn=accepted.in_turn,
            outcome=outcome,
            boundary=boundary,
            recovery=recovery,
        )

    # stop

    def _stop(self, dry_run: bool) -> OperatorOutcome:
        m = self._member
        if m.state == "ended":
            return OperatorOutcome(
                changed=False, detail=_dry(dry_run, "The member has ended.")
            )
        if m.gate.end_origin is not None:
            return OperatorOutcome(
                changed=False, detail=_dry(dry_run, "The member is already ending.")
            )
        if m.state == "pending" and self._swarm.stop_reason is not None:
            # no member starts once the swarm is stopping, however late in its ending
            return OperatorOutcome(
                changed=False,
                detail=_dry(
                    dry_run,
                    "The member has not started, and the swarm is stopping, so it never will.",
                ),
            )
        if not self._swarm.active():
            return OperatorOutcome(changed=False, detail=_dry(dry_run, _ENDED))
        # before the request, which sets an idle member `ending`
        detail = _stop_detail(m)
        if not dry_run:
            m.end(origin=OPERATOR)
        return OperatorOutcome(changed=True, detail=_dry(dry_run, detail))


# What a stop does to the member, by where it is. A stop ends work only: answers the
# member has already given stay eligible (decision: Ransom, 2026-11-04).
def _stop_detail(m: "MemberRun") -> str:
    if m.state == "pending":
        return "The member ends when it is started."
    if m.state == "idle":
        return "The member is idle: it ends, and its answers are kept."
    if m.gate.draining:
        return "The member's agent has returned: it ends once its background work drains, and keeps a submission it returned."
    return "The member ends at its next turn boundary, or is cancelled when the swarm's grace period runs out."


def _refused(reason: str) -> OperatorOutcome:
    return OperatorOutcome(changed=False, detail=reason, refused=reason)


def _dry(dry_run: bool, detail: str) -> str:
    return f"Dry run: {detail}" if dry_run else detail
