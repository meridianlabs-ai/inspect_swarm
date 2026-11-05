import json
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, cast

from inspect_ai._util.json import to_json_safe
from inspect_ai.agent._channel import current_agent_channel
from inspect_ai.model._call_tools import MAX_TOOL_CALL_ARGUMENTS_DEPTH
from inspect_ai.tool import ToolError
from inspect_ai.util._span import current_agent_span_id, current_span_id
from pydantic import JsonValue

from .._budget import MemberContext, current_member
from .._channel._channel import (
    Channel,
    ChannelRun,
    ChannelState,
    WaitingState,
    WaitSpec,
)
from .._channel.messages import MessagesState
from .._controller._controller import MemberInfo
from .._evidence import READ_KEYS, comm_delivered, comm_read, comm_sent
from ._address import resolve
from ._record import (
    Draft,
    FrozenJson,
    Record,
    current_via,
    freeze_json,
    thaw_json,
)
from ._storm import Storm
from ._wait import Waits

if TYPE_CHECKING:
    from .._controller._control import MemberHandle

_STOPPING = "the swarm is stopping; nothing more is delivered."


@dataclass(frozen=True)
class Delivery:
    """What `Bus.deliver()` returns for a delivered record."""

    record: Record
    """The record as delivered."""

    seq: int
    """The record's commit sequence number, across the run's channels. It is `delivery.seq` in the record's `delivered` evidence."""

    notify: list[str]
    """The members whose own loop the record is news to, as the channel's `apply()` returned them."""

    result: Mapping[str, JsonValue] | None
    """What the channel's `apply()` returned for the evidence."""


@dataclass(frozen=True)
class Caller:
    """The member a channel's tool acts for, from `calling_member()`."""

    member: str
    """The member's name."""

    nested: bool
    """Whether the calling loop is not the member's own, such as a subagent's."""

    loop_span_id: str | None
    """The agent span of the calling loop."""


class Bus(Protocol):
    """A swarm run's bus, as `ChannelRun.bus` gives it to a channel.

    Every record of a channel goes through `deliver()`, and a tool whose result shows the channel's records reports them with `read()`. Call both from the channel's tools: they act for the calling member.
    """

    @property
    def stopping(self) -> bool:
        """Whether the swarm has begun to stop. Once it has, every `deliver()` is refused."""
        ...

    def running(self, name: str) -> bool:
        """Whether a member has started and not ended.

        Args:
            name: The member's name.

        Raises:
            KeyError: If no member of the swarm has the name.
        """
        ...

    async def deliver(self, draft: Draft) -> Delivery:
        """Make a record from a draft and deliver it through the channel.

        The bus binds the sender from the calling member, resolves the addresses, and calls the channel's `validate()`. It then checks the sender is still running and the channel's storm controls, calls `admit()`, writes the `sent` evidence, calls `apply()` and writes the `delivered` evidence, with no `await` between the checks and `apply()`.

        Args:
            draft: The record to make.

        Returns:
            The delivered record, its commit sequence number and what `apply()` returned.

        Raises:
            ToolError: If the swarm is stopping, an address is refused, or a check refuses the record. The calling tool's result is the error.
            ValueError: If no channel of the swarm has `draft.channel` as its `name`.
            RuntimeError: If called outside a member of this swarm.
        """
        ...

    def read(
        self,
        channel: str,
        records: Sequence[str],
        *,
        depends_on: Sequence[str] | None = None,
        listing: bool = False,
        fields: Mapping[str, JsonValue] | None = None,
    ) -> str:
        """Write the `read` evidence of a tool result that shows the channel's records.

        The evidence names the calling member and loop, the records, and the read's id. The records the calling loop has not depended on before are written as its `depends_on`, which the swarm's critical path links.

        Args:
            channel: The channel's `name`.
            records: The ids of the records the result concerns.
            depends_on: The ids of the records whose content or news the result showed, when they differ from `records`: a result that tells the loop a record was committed, without its content, depends on it, and a result built from several records depends on them all.
            listing: Whether the result showed what exists rather than its content. The swarm's metrics count listings apart from reads.
            fields: More data for the evidence, written beside the bus's own.

        Returns:
            The read's id (`"r-5"`), numbered across the run's channels.

        Raises:
            ValueError: If no channel of the swarm has the name, an id is not a record the channel committed in this run, or a key of `fields` is one the bus writes (`version`, `read`, `tool_span_id`, `depends_on`, `listing`, `via`).
            RuntimeError: If called outside a member of this swarm.
        """
        ...


def calling_member() -> Caller | None:
    """The member of the current swarm run that the caller runs in or descends from.

    A channel's tool calls it to learn which member it acts for. The answer comes from the runtime, never from model output.

    Returns:
        The member, or `None` outside a member of a swarm run.
    """
    bus = current_bus.get()
    context = None if bus is None else bus.member()
    if bus is None or context is None:
        return None
    return Caller(
        member=context.name,
        nested=bus.nested(context),
        loop_span_id=current_agent_span_id(),
    )


# The communication bus of one swarm run, which implements `Bus`. Every sanctioned
# communication is a record that a channel's tool hands to `deliver()`; nothing else
# changes channel state. The bus, and the channel states it opens, live for one run and
# are reached through `current_bus`, so concurrent samples never share them.
class SwarmBus:
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
        # record id -> the channel that committed it
        self._committed: dict[str, str] = {}
        # loop agent span -> the records already in its results' `depends_on`
        self._depended: dict[str | None, set[str]] = {}
        self._states: dict[str, ChannelState] = {}
        self._storms: dict[str, Storm] = {}
        self._prefixes: dict[str, str] = {}
        self._reach: dict[str, Literal["loop", "member"]] = {}
        # the states of the channels that wake waits, and their longest wait
        waiting: dict[str, WaitingState] = {}
        max_wait: int | None = None
        run = ChannelRun(
            bus=self,
            roster=[
                MemberInfo(name=m.name, role=m.role, choose_name=m.choose_name)
                for m in members
            ],
        )
        for channel in channels:
            state = channel.open(run)
            if channel.max_wait is not None and not all(
                callable(getattr(state, method, None))
                for method in ("wait_check", "wakes", "news")
            ):
                raise TypeError(
                    f"{type(channel).__name__} sets max_wait, but the state its open() returned does not implement WaitingState."
                )
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
            if channel.max_wait is not None:
                waiting[channel.name] = cast(WaitingState, state)
                max_wait = max(max_wait or 0, channel.max_wait)
        self._waiting = waiting
        self._waits = Waits(self, self._contexts, waiting, max_wait)
        # the runtime's news callback: a delivered record is news to an idle member
        self.news: Callable[[str, Record], None] = lambda member, record: None

    @property
    def states(self) -> Mapping[str, ChannelState]:
        # the opened channel states, by channel name
        return dict(self._states)

    @property
    def reach(self) -> Mapping[str, Literal["loop", "member"]]:
        # each opened channel's reach, by channel name
        return dict(self._reach)

    @property
    def roster(self) -> Sequence["MemberHandle"]:
        # the members' handles, in roster order
        return list(self._members)

    @property
    def stopping(self) -> bool:
        return self._stopping

    @property
    def waits(self) -> Waits:
        return self._waits

    def close(self) -> None:
        # the first act of the swarm's stop: records not yet committed are refused, and
        # every wait is released
        self._stopping = True
        self._waits.close()

    async def wait(
        self,
        path: str | None,
        file_id: str | None,
        timeout: int | None,
        channels: Sequence[str] | None = None,
        cap: int | None = None,
    ) -> str:
        # the swarm_wait tool's body; a bridged member's waits only on `channels`, those
        # its tools reach, and at most `cap` seconds
        return await self._waits.wait(path, file_id, timeout, channels, cap)

    def member_ended(self, name: str, status: str) -> None:
        # from the runtime, right after it marks the member ended
        self._waits.member_ended(name, status)

    def member_ending(self, name: str) -> None:
        # from the member's drain, once its own agent has returned
        self._waits.member_ending(name)

    def recheck(self) -> None:
        # from a member's background owner, when none of its work is running
        self._waits.recheck()

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
        recipients = resolve(sender, draft.to, self._by_name)
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
            wake=draft.wake,
            reply_to=draft.reply_to,
            sender_span_id=current_agent_span_id(),
            tool_span_id=current_span_id(),
            via=current_via.get(),
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
        self._committed[record.id] = record.channel
        comm_delivered(member_span, record, nested, self._seq, notify, result)
        self._waits.delivered(record, self._seq)
        self._idle_news(record)
        return Delivery(record, self._seq, list(notify), result)

    def read(
        self,
        channel: str,
        records: Sequence[str],
        *,
        depends_on: Sequence[str] | None = None,
        listing: bool = False,
        fields: Mapping[str, JsonValue] | None = None,
    ) -> str:
        # a result of the channel's tool concerned `records` and showed the calling
        # loop the content or news of `depends_on` (by default `records`): writes its
        # `read` evidence for the member and loop calling, as deliver() binds the sender
        context = self.member()
        if context is None:
            raise RuntimeError(
                "Bus.read() was called outside a member of this swarm; channel tools report reads for the member that calls them."
            )
        self._state(channel)
        taken = [key for key in fields or {} if key in READ_KEYS]
        if taken:
            raise ValueError(
                f"{channel}: a read's fields cannot use {', '.join(taken)}, which the bus writes itself."
            )
        loop = current_agent_span_id()
        self._check_committed(channel, records)
        depends = self.depends(
            channel, loop, records if depends_on is None else depends_on
        )
        read = self.read_id()
        comm_read(
            context.span_id,
            context.name,
            channel,
            read,
            loop,
            self.nested(context),
            current_span_id(),
            list(records),
            depends,
            listing,
            fields,
        )
        return read

    def depends(
        self, channel: str, loop: str | None, depends_on: Sequence[str]
    ) -> list[str]:
        # a result's dependencies, each a record the channel committed in this run:
        # those not yet written for the loop, in order and once each, now marked
        # written. A loop's value in the critical path never decreases, so a commit an
        # earlier result of the loop linked adds nothing when linked again.
        self._check_committed(channel, depends_on)
        depended = self._depended.setdefault(loop, set())
        new = [r for r in dict.fromkeys(depends_on) if r not in depended]
        depended.update(new)
        return new

    def running(self, name: str) -> bool:
        # whether the member has started and not yet ended
        return self._by_name[name].state == "running"

    def idle(self, name: str) -> bool:
        # whether the member is a persistent member held idle after an answer
        return self._by_name[name].state == "idle"

    def idle_spec(self, name: str) -> WaitSpec | None:
        # what a wait of the member's own loop that watches the whole board would
        # watch; None when no loop of the member has bound swarm_tools(), since it then
        # cannot read the channels
        context = self._contexts[name]
        if context.loop is None:
            return None
        return WaitSpec(
            id=f"idle-{name}", member=name, loop=context.loop_span, own=True, path=None
        )

    def has_news(self, name: str) -> bool:
        # whether a waiting channel has news for the member's own loop now
        spec = self.idle_spec(name)
        return spec is not None and any(
            state.news(spec) is not None for state in self._waiting.values()
        )

    def _idle_news(self, record: Record) -> None:
        # step 4 of a delivery, after the waits: a record that would end a whole-board
        # wait of an idle member's own loop is news the runtime reports for it
        state = self._waiting.get(record.channel)
        if state is None or not record.wake:
            return
        for m in self._members:
            if m.name == record.sender or m.state != "idle":
                continue
            spec = self.idle_spec(m.name)
            if spec is not None and state.wakes(spec, record):
                self.news(m.name, record)

    def nested(self, context: MemberContext) -> bool:
        # whether the caller runs in a loop other than the member's own, such as a
        # subagent's. A bridged member binds no loop, so its CLI is one loop, its own,
        # whichever of its conversations calls
        return context.loop is not None and current_agent_channel() is not context.loop

    def status(self, context: MemberContext) -> str | None:
        # the line that ends the results of the swarm's own tools in the member's own
        # loop, after the channel's own: its unread messages, with the messages channel;
        # nested loops cannot read them, so they are not told. Other channels' tools
        # carry no count (decision: Ransom, 2026-10-30).
        if self.nested(context):
            return None
        for state in self._states.values():
            if isinstance(state, MessagesState):
                return state.unread_line(context.name)
        return None

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

    def _check_committed(self, channel: str, ids: Sequence[str]) -> None:
        # a channel's result names only records it committed; anything else is a bug in
        # the channel's code
        for record in ids:
            if self._committed.get(record) != channel:
                raise ValueError(
                    f"{channel}: a result names {record}, which is not a record this channel committed in this run."
                )

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


current_bus: ContextVar[SwarmBus | None] = ContextVar(
    "inspect_swarm_current_bus", default=None
)
