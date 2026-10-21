import json
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from inspect_ai._util.json import to_json_safe
from inspect_ai.agent._channel import current_agent_channel
from inspect_ai.model._call_tools import MAX_TOOL_CALL_ARGUMENTS_DEPTH
from inspect_ai.tool import ToolError
from inspect_ai.util._span import current_agent_span_id, current_span_id
from pydantic import JsonValue

from .._budget import MemberContext, current_member
from .._channel._channel import Channel, ChannelRun, ChannelState
from .._controller._controller import MemberInfo
from .._evidence import comm_delivered, comm_sent
from ._record import Draft, FrozenJson, Record, freeze_json, thaw_json
from ._storm import Storm

if TYPE_CHECKING:
    from .._controller._control import MemberHandle

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
    ) -> None:
        self._members = list(members)
        self._by_name = {m.name: m for m in self._members}
        # the context the runtime runs each member in, which its descendants inherit;
        # a member of another run with the same name has a context of its own
        self._contexts = dict(contexts)
        self._stopping = False
        self._seq = 0
        self._ids: dict[str, int] = {}
        self._reads = 0
        self._states: dict[str, ChannelState] = {}
        self._storms: dict[str, Storm] = {}
        self._prefixes: dict[str, str] = {}
        self._reach: dict[str, Literal["loop", "member"]] = {}
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
            self._reach[channel.name] = channel.reach

    @property
    def states(self) -> Mapping[str, ChannelState]:
        # the opened channel states, by channel name
        return dict(self._states)

    @property
    def reach(self) -> Mapping[str, Literal["loop", "member"]]:
        # each opened channel's reach, by channel name
        return dict(self._reach)

    @property
    def stopping(self) -> bool:
        return self._stopping

    def close(self) -> None:
        # the first act of the swarm's stop: records not yet committed are refused
        self._stopping = True

    async def deliver(self, draft: Draft) -> Delivery:
        # step 0: bind and resolve, before a record exists
        context = self._sender()
        sender = context.name
        # the member's own span, for the evidence; the record keeps the writing loop's
        member_span = context.span_id
        nested = self.nested(context)
        if self._stopping:
            raise ToolError(_STOPPING)
        state = self._state(draft.channel)
        recipients = self._resolve(sender, draft.to)
        number = self._ids.get(draft.channel, 0) + 1
        record = Record(
            id=f"{self._prefixes[draft.channel]}-{number}",
            channel=draft.channel,
            kind=draft.kind,
            sender=sender,
            to=draft.to,
            recipients=recipients,
            routed_out=(),
            payload=draft.payload,
            # detached from the tool's own dict, and read-only at every depth
            op=None if draft.op is None else _frozen_op(draft),
            wake=False,
            reply_to=None,
            sender_span_id=current_agent_span_id(),
            tool_span_id=current_span_id(),
        )
        state.validate(record)
        # the id is given once the record exists, so that a refusal above skips none
        self._ids[draft.channel] = number

        # step 1, the bus hook, is later work; steps 2 to 4 follow step 0 with no await,
        # so nothing changes between check and commit
        storm = self._storms[record.channel]
        rejection = (
            self._liveness(record) or _unencodable(record) or storm.check(record)
        )
        if rejection is None:
            try:
                state.admit(record)
            except ToolError as ex:
                comm_sent(member_span, record, nested, ("admit", str(ex)))
                raise
        if rejection is not None:
            comm_sent(member_span, record, nested, rejection)
            raise ToolError(rejection[1])

        # step 3: evidence
        comm_sent(member_span, record, nested, None)

        # step 4: commit and apply
        self._seq += 1
        notify, result = state.apply(record)
        storm.commit(record)
        comm_delivered(member_span, record, nested, self._seq, notify, result)
        return Delivery(record, self._seq, list(notify), result)

    def running(self, name: str) -> bool:
        # whether the member has started and not yet ended
        return self._by_name[name].state == "running"

    def nested(self, context: MemberContext) -> bool:
        # whether the caller runs in a loop other than the member's own, such as a
        # subagent's
        return context.loop is not None and current_agent_channel() is not context.loop

    def read_id(self) -> str:
        # the id of a read's evidence, numbered across the run's channels
        self._reads += 1
        return f"r-{self._reads}"

    def member(self) -> MemberContext | None:
        # the member the caller runs in or descends from, if it is one of this run's
        context = current_member.get()
        if context is None or self._contexts.get(context.name) is not context:
            return None
        return context

    def _sender(self) -> MemberContext:
        context = self.member()
        if context is None:
            raise RuntimeError(
                "Bus.deliver() was called outside a member of this swarm; channel tools deliver records for the member that calls them."
            )
        return context

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
        # step 0 checked the recipients and the stopping flag, and nothing has awaited
        # since; the sender is checked here, with evidence, since a task it left behind
        # can still run in its context after it ends
        if not self.running(record.sender):
            return (
                "liveness",
                f"{record.sender} has ended, so its {record.kind} was not delivered.",
            )
        return None


def _frozen_op(draft: Draft) -> Mapping[str, FrozenJson]:
    # refused here, before any evidence: data nested deeper than Inspect takes in a
    # tool call's arguments would be cut short in the log, and data the log cannot
    # write and read back as sent (a lone surrogate, an integer too long to read) would
    # be changed in it or stop it being read
    try:
        frozen = freeze_json(draft.op, MAX_TOOL_CALL_ARGUMENTS_DEPTH)
    except ValueError:
        raise ToolError(
            f"the {draft.kind}'s operation data is nested more than {MAX_TOOL_CALL_ARGUMENTS_DEPTH} levels deep"
        ) from None
    assert isinstance(frozen, Mapping)
    if not _logged_as_sent(thaw_json(frozen)):
        raise ToolError(f"the {draft.kind}'s operation data can't be logged as sent")
    return frozen


def _logged_as_sent(op: JsonValue) -> bool:
    # the log's own round trip, Inspect's writer and a JSON reader; compared as JSON
    # text, so that NaN, which the reader takes, matches itself
    try:
        read = json.loads(to_json_safe(op))
        return json.dumps(read) == json.dumps(op)
    except ValueError:
        return False


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
