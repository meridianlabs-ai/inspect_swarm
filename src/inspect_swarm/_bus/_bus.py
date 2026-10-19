import logging
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

import anyio
from inspect_ai._util.exception import TerminateSampleError
from inspect_ai.tool import ToolError
from inspect_ai.util._span import current_agent_span_id, current_span_id
from pydantic import JsonValue

from .._budget import MemberContext, current_member
from .._channel._channel import Channel, ChannelRun, ChannelState
from .._controller._controller import MemberInfo
from .._evidence import comm_delivered, comm_sent
from ._policy import CommDecision, CommPolicy
from ._record import Draft, FrozenJson, Record, freeze_json
from ._storm import Storm

if TYPE_CHECKING:
    from .._controller._control import MemberHandle

logger = logging.getLogger(__name__)

_STOPPING = "the swarm is stopping; nothing more is delivered."


# What `Bus.deliver()` returns for a delivered record: the record as delivered, its
# commit sequence number, and what the channel's `apply()` returned.
@dataclass(frozen=True)
class Delivery:
    record: Record
    seq: int
    notify: list[str]
    result: Mapping[str, JsonValue] | None


# The communication bus of one swarm run. Every sanctioned communication is a record
# that a channel's tool hands to `deliver()`; nothing else changes channel state. The
# bus, and the channel states it opens, live for one run and are reached through
# `current_bus`, so concurrent samples never share them.
class Bus:
    def __init__(
        self,
        members: Sequence["MemberHandle"],
        contexts: Mapping[str, MemberContext],
        channels: Sequence[Channel],
        policy: CommPolicy | None,
    ) -> None:
        self._members = list(members)
        self._by_name = {m.name: m for m in self._members}
        # the context the runtime runs each member in, which its descendants inherit;
        # a member of another run with the same name has a context of its own
        self._contexts = dict(contexts)
        self._policy = policy
        self._stopping = False
        self._records: list[Record] = []
        self._seq = 0
        self._ids: dict[str, int] = {}
        self._states: dict[str, ChannelState] = {}
        self._storms: dict[str, Storm] = {}
        self._prefixes: dict[str, str] = {}
        run = ChannelRun(
            bus=self, roster=[MemberInfo(name=m.name, role=m.role) for m in members]
        )
        for channel in channels:
            state = channel.open(run)
            if state is None:
                continue
            if channel.name is None or channel.prefix is None:
                raise TypeError(
                    f"{type(channel).__name__}.open() returned a state, but the channel has no name or prefix for its records."
                )
            self._states[channel.name] = state
            self._storms[channel.name] = Storm(channel.storm)
            self._prefixes[channel.name] = channel.prefix

    @property
    def states(self) -> Mapping[str, ChannelState]:
        # the opened channel states, by channel name
        return dict(self._states)

    @property
    def stopping(self) -> bool:
        return self._stopping

    def close(self) -> None:
        # the first act of the swarm's stop: records not yet committed are refused
        self._stopping = True

    # SwarmView

    @property
    def members(self) -> Sequence["MemberHandle"]:
        return list(self._members)

    def member(self, name: str) -> "MemberHandle":
        try:
            return self._by_name[name]
        except KeyError:
            raise KeyError(
                f"No member named {name!r}; the members are {', '.join(self._by_name)}."
            ) from None

    @property
    def records(self) -> Sequence[Record]:
        return tuple(self._records)

    async def deliver(self, draft: Draft) -> Delivery:
        # step 0: bind and resolve, before a record exists
        sender = self._sender()
        if self._stopping:
            raise ToolError(_STOPPING)
        state = self._state(draft.channel)
        recipients = self._resolve(sender, draft.to)
        number = self._ids.get(draft.channel, 0) + 1
        record = Record(
            id=f"{self._prefixes[draft.channel]}-{number}",
            channel=draft.channel,
            kind=draft.kind,
            origin="member",
            sender=sender,
            to=draft.to,
            recipients=recipients,
            routed_out=(),
            payload=draft.payload,
            # detached from the tool's own dict, and read-only at every depth
            op=None if draft.op is None else _frozen_op(draft),
            wake=False,
            reply_to=None,
            via="tool",
            sender_span_id=current_agent_span_id(),
            tool_span_id=current_span_id(),
        )
        state.validate(record)
        # the id is given once the record exists, so that a refusal above skips none
        self._ids[draft.channel] = number

        # step 1: the policy, the only await
        decision: CommDecision | None = None
        original: str | None = None
        if self._policy is not None:
            try:
                decision = await self._policy(record, self)
            except anyio.get_cancelled_exc_class():
                # a stop's hard cancellation, or the sender's own: the attempt reached
                # the bus, so it is evidenced, with no decision
                comm_sent(
                    record,
                    None,
                    None,
                    ("cancelled", f"the {record.kind} was cancelled during the policy"),
                )
                raise
            except Exception as ex:
                # a limit its model calls reached, or an error of its own: raised as it
                # is, after the attempt's evidence
                comm_sent(
                    record,
                    None,
                    None,
                    ("policy_error", f"the swarm's policy raised {type(ex).__name__}"),
                )
                raise
        if decision is not None:
            if decision.action == "modify":
                assert decision.payload is not None
                original = record.payload
                record = replace(record, payload=decision.payload)
            elif decision.action in ("reject", "terminate"):
                reason = decision.message or (
                    f"the swarm's policy rejected this {record.kind}"
                    if decision.action == "reject"
                    else f"the swarm's policy ended the sample at {record.id}"
                )
                comm_sent(record, decision, original, ("policy", reason))
                if decision.action == "reject":
                    raise ToolError(reason)
                raise TerminateSampleError(reason)
            elif decision.action == "escalate":
                # as sentinel's root does: there is nobody to escalate to
                logger.warning(
                    f"The swarm's policy escalated {record.id}, and nothing handles an escalation; it is delivered."
                )

        # steps 2 to 4 run with no await, so nothing changes between check and commit
        storm = self._storms[record.channel]
        rejection = (
            self._liveness(record) or _unencodable(record) or storm.check(record)
        )
        if rejection is None:
            try:
                state.admit(record)
            except ToolError as ex:
                comm_sent(record, decision, original, ("admit", str(ex)))
                raise
        if rejection is not None:
            comm_sent(record, decision, original, rejection)
            raise ToolError(rejection[1])

        # step 3: evidence
        comm_sent(record, decision, original, None)

        # step 4: commit and apply
        self._seq += 1
        notify, result = state.apply(record)
        storm.commit(record)
        self._records.append(record)
        comm_delivered(record, self._seq, notify, result)
        return Delivery(record, self._seq, list(notify), result)

    def _sender(self) -> str:
        context = current_member.get()
        if context is None or self._contexts.get(context.name) is not context:
            raise RuntimeError(
                "Bus.deliver() was called outside a member of this swarm; channel tools deliver records for the member that calls them."
            )
        return context.name

    def _state(self, channel: str) -> ChannelState:
        try:
            return self._states[channel]
        except KeyError:
            raise ValueError(
                f"No channel named {channel!r} is open in this swarm; the open channels are {', '.join(self._states) or 'none'}."
            ) from None

    def _resolve(self, sender: str, to: tuple[str, ...]) -> tuple[str, ...]:
        # member names only; an unaddressed record resolves to nobody
        recipients: list[str] = []
        for name in to:
            m = self._by_name.get(name)
            if m is None:
                raise ToolError(
                    f"{name!r} is not a member of the swarm; the members are {', '.join(self._by_name)}."
                )
            if name == sender:
                raise ToolError(f"{name} is you; you cannot send to yourself.")
            if m.state == "pending":
                raise ToolError(f"{name} has not started.")
            if m.state == "ended":
                raise ToolError(f"{name} has ended.")
            if name not in recipients:
                recipients.append(name)
        return tuple(recipients)

    def _liveness(self, record: Record) -> tuple[str, str] | None:
        # the sender or a recipient may have ended, or a stop begun, during the policy
        if self._stopping:
            return ("liveness", _STOPPING)
        for name in (record.sender, *record.recipients):
            if self._by_name[name].state != "running":
                return (
                    "liveness",
                    f"{name} ended before the {record.kind} was delivered.",
                )
        return None


def _frozen_op(draft: Draft) -> Mapping[str, FrozenJson]:
    # refused here, before any evidence: a lone surrogate from model output would be
    # rewritten in the log, or stop the log being written when it is in a key
    try:
        frozen = freeze_json(draft.op)
    except UnicodeEncodeError as ex:
        raise ToolError(
            f"the {draft.kind} is not valid text: its operation data has a string with an unpaired surrogate at character {ex.start}"
        ) from None
    assert isinstance(frozen, Mapping)
    return frozen


def _unencodable(record: Record) -> tuple[str, str] | None:
    # model output can hold a lone surrogate, which no UTF-8 reader can take
    try:
        record.payload.encode("utf-8")
    except UnicodeEncodeError as ex:
        return (
            "payload",
            f"the {record.kind} is not valid text: it has an unpaired surrogate at character {ex.start}",
        )
    return None


current_bus: ContextVar[Bus | None] = ContextVar(
    "inspect_swarm_current_bus", default=None
)
