from collections.abc import Mapping, Sequence

from inspect_ai.tool import Tool, ToolDef, ToolError, ToolParams
from inspect_ai.util._span import current_agent_span_id, current_span_id
from pydantic import JsonValue

from .._bus._address import MEMBER_NAME, RESERVED
from .._bus._record import Draft, Record
from .._bus._storm import StormControls
from .._controller._controller import MemberInfo
from .._evidence import comm_read
from .._fence import fence, utf8_bytes, with_lines
from ._channel import (
    Channel,
    ChannelRun,
    ChannelState,
    ToolDeclaration,
    WaitNews,
    WaitSpec,
    swarm_channel,
)
from ._params import check_bound, check_rate

CHANNEL = "messages"
TAG = "peer_message"

# the longest record id a fence can name: the prefix and a number of up to 14 digits
_MAX_ID = "m-" + "9" * 14

_INSTRUCTIONS = (
    "You can send messages to the other members of the swarm with `send_message`, read the messages sent to you with `read_messages`, and see who is in the swarm with `list_members`. "
    "Messages are shown between `<peer_message>` tags whose `from` attribute names the sender. "
    "While you have unread messages, the results of `send_message`, `swarm_wait` and the swarm's other built-in tools end with a line that counts them, and `list_members` always says how many you have. "
    "To wait for a message, call `swarm_wait` rather than polling."
)

_SEND = "Send a message to other members of the swarm. Returns the message's id, such as m-3, which a reply names in reply_to. The recipients read it with read_messages."

_READ = (
    "Read your unread messages, oldest first, and mark them read. "
    "Each message is shown between <peer_message> tags whose from attribute names the sender. "
    "One call returns as many whole messages as fit, and says how many remain unread. "
    "It never waits: to wait for a message, call swarm_wait."
)

_LIST = (
    "List the members of the swarm, in order: each one's name, its role if it has one, its state (pending, running or ended) and, once it has ended, how it ended. Marks which one is you. "
    "Also says how many unread messages you have, so call it to check for messages."
)


# send_message's signature, reflected once for its parameters. Each run's tool takes
# them in full, so it is not reflected: Inspect's reflection cache would keep it, and
# with it the run.
async def _send_message(
    to: str | list[str],
    message: str,
    reply_to: str | None = None,
    wake: bool = True,
) -> str:
    """Send a message.

    Args:
        to: A member name, a list of member names, or "all" for every other running member.
        message: The text to send.
        reply_to: The id of a message this one answers, such as m-3. Optional.
        wake: Whether the message ends a recipient's swarm_wait. Default true.
    """
    ...


_SEND_PARAMETERS = ToolDef(_send_message, description=_SEND).parameters


# the tools as each run builds them, for a bridged member's tools
def declarations() -> list[ToolDeclaration]:
    return [
        ToolDeclaration("send_message", _SEND, _SEND_PARAMETERS.model_copy(deep=True)),
        ToolDeclaration("read_messages", _READ, ToolParams(), max_output=0),
        ToolDeclaration("list_members", _LIST, ToolParams()),
    ]


class _Messages(Channel):
    name = CHANNEL
    prefix = "m"
    # a read marks messages read, so an inbox has one reader: the member's own loop
    reach = "loop"

    def __init__(
        self,
        max_bytes: int,
        max_unread: int,
        rate: tuple[int, float] | None,
        dedupe: bool,
        max_wait: int,
    ) -> None:
        self.storm = StormControls(max_bytes=max_bytes, rate=rate)
        self.max_bytes = max_bytes
        self.max_unread = max_unread
        self.dedupe = dedupe
        self.max_wait = max_wait

    def check(self, roster: Sequence[MemberInfo]) -> None:
        for info in roster:
            if not MEMBER_NAME.fullmatch(info.name) or info.name in RESERVED:
                raise ValueError(
                    f"messages(): member {info.name!r} has a name the channel cannot use: names must start with a lowercase letter, followed by at most 47 lowercase letters, digits, _ or -, and must not be {' or '.join(RESERVED)}. Give the member such a name with member(..., name=...)."
                )

    def open(self, run: ChannelRun) -> ChannelState | None:
        return MessagesState(self, run)

    def instructions(self, member: MemberInfo) -> str | None:
        return _INSTRUCTIONS


@swarm_channel
def messages(
    max_bytes: int = 8_000,
    max_unread: int = 50,
    rate: tuple[int, float] | list[int | float] | None = (20, 60.0),
    dedupe: bool = True,
    max_wait: int = 120,
) -> Channel:
    """Direct messages between the members of the swarm.

    Each member's own loop gets three tools. `send_message` sends a message to a member, to several, or to `"all"`, every other running member, and returns its id, such as `m-3`. `read_messages` returns the member's unread messages, oldest first, each inside a `<peer_message>` fence that names its id, sender and recipients, and marks them read. `list_members` lists the roster with each member's role, state and, once it has ended, its status. While a member has unread messages, the results of `send_message`, `list_members`, `shared_memory` and `swarm_wait` in its own loop end with a line that counts them; `list_members` also says when there are none. The tools of other channels carry no count. Nested loops, such as a `deepagent()` member's subagents, get none of these tools, so a member's inbox has one reader.

    Every send goes through the swarm's bus as a record from the member that made it. A send is refused when an address is not a running member other than the sender, when the message is empty or longer than `max_bytes`, when a recipient already has `max_unread` unread messages, when the sender is over its `rate`, or, with `dedupe`, when a recipient still has the same text from the same sender unread. A refused send reaches no recipient.

    One read always returns at least the oldest unread message whole: the read is bounded by the largest fence the roster allows plus `max_bytes`, and Inspect's tool output limit does not apply to it.

    A fence is a label for the reader, not a protection: a member can write text that looks like a fence, another member's message or a status line.

    The member's own loop also gets `swarm_wait`, which returns while it has unread messages sent with `wake=True` (the default), and otherwise blocks as `shared_memory()`'s does, up to `max_wait` seconds. A wait is wall-clock time inside a tool call: it counts towards the member's time limit, the swarm's time cap, and the sample's time and working limits.

    The channel needs member names that start with a lowercase letter, followed by at most 47 lowercase letters, digits, `_` or `-`, other than `all` and `swarm`. Bridged members get its tools from `swarm_bridged_tools()`; all of a bridged member's conversations share its inbox.

    Args:
        max_bytes: The most UTF-8 bytes in one message, at least 1.
        max_unread: The most unread messages one member may have, at least 1. A send that would pass it is refused.
        rate: The most messages one member may send in a sliding window of seconds, as `(count, seconds)`; `None` for no limit. A message to several members counts once.
        dedupe: Whether to refuse a message whose text a recipient still has unread from the same sender.
        max_wait: The channel's longest `swarm_wait` in seconds, at least 1. With other channels that wake waits, the longest of theirs applies.

    Returns:
        The channel.

    Raises:
        ValueError: If a bound is below its minimum, or `rate` is not a positive count and a finite, positive window.
    """
    check_bound(CHANNEL, "max_bytes", max_bytes, 1)
    check_bound(CHANNEL, "max_unread", max_unread, 1)
    check_bound(CHANNEL, "max_wait", max_wait, 1)
    return _Messages(
        max_bytes,
        max_unread,
        None if rate is None else check_rate(CHANNEL, rate),
        bool(dedupe),
        max_wait,
    )


# The inboxes of one swarm run, kept on the run's bus.
class MessagesState:
    def __init__(self, channel: _Messages, run: ChannelRun) -> None:
        self._bus = run.bus
        self._max_unread = channel.max_unread
        self._dedupe = channel.dedupe
        names = [info.name for info in run.roster]
        # member -> its unread messages, oldest first
        self._unread: dict[str, list[Record]] = {name: [] for name in names}
        # member -> the ids of the messages it has sent or read, which its replies may
        # name
        self._known: dict[str, set[str]] = {name: set() for name in names}
        # the largest result of one read: its header and remainder lines at the most
        # unread messages, the largest fence this roster allows, and a payload at the
        # limit
        envelope = utf8_bytes(
            fence(
                TAG,
                [
                    ("id", _MAX_ID),
                    ("from", max(names, key=len)),
                    ("to", ", ".join(names)),
                    ("reply_to", _MAX_ID),
                ],
                "",
            )
        )
        self._header_max = utf8_bytes(_header(channel.max_unread))
        self._trailer_max = utf8_bytes(_trailer(channel.max_unread))
        self.budget = (
            self._header_max
            + len("\n\n")
            + envelope
            + channel.max_bytes
            + len("\n")
            + self._trailer_max
        )
        self._tools = [self._send_tool(), self._read_tool(), self._list_tool()]

    def tools(self) -> list[Tool]:
        return list(self._tools)

    def validate(self, record: Record) -> None:
        if record.kind != "message" or record.op is not None:
            raise ToolError("a messages record must be a message with no operation")
        if not record.payload:
            raise ToolError("message is empty.")
        if not record.recipients:
            raise ToolError("a message needs at least one recipient.")
        if record.reply_to is not None and record.reply_to not in self._known.get(
            record.sender, set()
        ):
            raise ToolError(
                "reply_to must be the id of a message you have sent or read, such as m-3."
            )

    def admit(self, record: Record) -> None:
        # all or nothing: a recipient that refuses the message refuses it for all
        for name in record.recipients:
            unread = self._unread[name]
            if len(unread) >= self._max_unread:
                raise ToolError(
                    f"inbox full: {name} has {len(unread)} unread messages."
                )
            if self._dedupe:
                same = next(
                    (
                        r
                        for r in unread
                        if r.sender == record.sender and r.payload == record.payload
                    ),
                    None,
                )
                if same is not None:
                    raise ToolError(f"duplicate of unread {same.id} to {name}.")

    def apply(self, record: Record) -> tuple[list[str], Mapping[str, JsonValue] | None]:
        for name in record.recipients:
            self._unread[name].append(record)
        self._known[record.sender].add(record.id)
        return list(record.recipients), None

    # waiting: the member's own loop's wait returns while it has an unread message
    # that wakes; nothing here marks anything

    def wait_check(self, spec: WaitSpec) -> None:
        # the wait's filters are the shared memory's
        return None

    def wakes(self, spec: WaitSpec, record: Record) -> bool:
        return spec.own and spec.member in record.recipients

    def news(self, spec: WaitSpec) -> WaitNews | None:
        unread = self._unread.get(spec.member, []) if spec.own else []
        if not any(r.wake for r in unread):
            return None
        senders = ", ".join(sorted({r.sender for r in unread}))
        return WaitNews(
            lines=[
                f"{_count(len(unread))} (from {senders}). Call read_messages() to read them."
            ],
            files=[{"record": r.id, "from": r.sender} for r in unread],
        )

    # the tools

    def _send_tool(self) -> Tool:
        async def execute(
            to: str | list[str],
            message: str,
            reply_to: str | None = None,
            wake: bool = True,
        ) -> str:
            # an ended member's send reaches the bus; if it passes the initial checks,
            # the bus refuses it with liveness evidence
            name = self._caller("send_message", running=False)
            addresses = (to,) if isinstance(to, str) else tuple(to)
            delivery = await self._bus.deliver(
                Draft(
                    CHANNEL,
                    "message",
                    to=addresses,
                    payload=message,
                    wake=wake,
                    reply_to=reply_to,
                )
            )
            record = delivery.record
            return with_lines(
                f"Sent {record.id} to {', '.join(record.recipients)}.",
                self.unread_line(name),
            )

        return ToolDef(
            execute,
            name="send_message",
            description=_SEND,
            parameters=_SEND_PARAMETERS.model_copy(deep=True),
            parallel=True,
        ).as_tool()

    def _read_tool(self) -> Tool:
        async def execute() -> str:
            """Read your unread messages."""
            return self._read(self._caller("read_messages"))

        return ToolDef(
            execute,
            name="read_messages",
            description=_READ,
            parameters=ToolParams(),
            parallel=True,
            # the tool bounds its own result by the read budget, so that no limit cuts
            # through a fence
            max_output=0,
        ).as_tool()

    def _list_tool(self) -> Tool:
        async def execute() -> str:
            """List the members of the swarm."""
            name = self._caller("list_members")
            lines = [
                f"{m.name}{' (you)' if m.name == name else ''}"
                f"{f', role {m.role}' if m.role else ''}: {m.state}"
                f"{f' ({m.status})' if m.status is not None else ''}"
                for m in self._bus.roster
            ]
            return with_lines(
                "\n".join(lines),
                self.unread_line(name) or "You have no unread messages.",
            )

        return ToolDef(
            execute,
            name="list_members",
            description=_LIST,
            parameters=ToolParams(),
            parallel=True,
        ).as_tool()

    def _caller(self, tool: str, running: bool = True) -> str:
        context = self._bus.member()
        if context is None:
            raise RuntimeError(
                f"The {tool} tool was called outside a member of its swarm."
            )
        if self._bus.nested(context):
            raise ToolError(
                f"{tool} is for {context.name}'s own loop; a subagent cannot use it."
            )
        if running and not self._bus.running(context.name):
            raise ToolError(f"{context.name} has ended.")
        return context.name

    def _read(self, name: str) -> str:
        # synchronous from the inbox to the mark, so what is shown is what is marked
        unread = self._unread[name]
        if not unread:
            return "You have no unread messages."
        size = self._header_max + len("\n\n") + len("\n") + self._trailer_max
        shown: list[str] = []
        for record in unread:
            fenced = _fenced(record)
            grown = size + utf8_bytes(fenced) + (len("\n") if shown else 0)
            if shown and grown > self.budget:
                break
            shown.append(fenced)
            size = grown
        read = unread[: len(shown)]
        del unread[: len(shown)]
        self._known[name].update(r.id for r in read)
        context = self._bus.member()
        assert context is not None
        comm_read(
            context.span_id,
            name,
            CHANNEL,
            self._bus.read_id(),
            current_agent_span_id(),
            False,
            current_span_id(),
            None,
            False,
            records=[r.id for r in read],
        )
        text = _header(len(read)) + "\n\n" + "\n".join(shown)
        if unread:
            text += "\n" + _trailer(len(unread))
        return text

    def unread_line(self, member: str) -> str | None:
        # the status line: a count, never a payload
        count = len(self._unread[member])
        return f"You have {_count(count)}." if count else None


# The record name of the built-in messages channel among a run's open channel states, by
# its state's type rather than its name, which a custom channel may take; None without
# one.
def builtin_messages(states: Mapping[str, ChannelState]) -> str | None:
    return next(
        (name for name, s in states.items() if isinstance(s, MessagesState)), None
    )


def _fenced(record: Record) -> str:
    attributes = [
        ("id", record.id),
        ("from", record.sender),
        ("to", ", ".join(record.recipients)),
    ]
    if record.reply_to is not None:
        attributes.append(("reply_to", record.reply_to))
    return fence(TAG, attributes, record.payload)


def _count(count: int) -> str:
    return "1 unread message" if count == 1 else f"{count} unread messages"


def _header(count: int) -> str:
    if count == 1:
        return "1 message."
    return f"{count} messages, oldest first."


def _trailer(count: int) -> str:
    more = "1 more unread message" if count == 1 else f"{count} more unread messages"
    return f"{more}; call read_messages() again."
