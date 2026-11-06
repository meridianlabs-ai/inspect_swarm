import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Literal, cast

from inspect_ai.tool import Tool, ToolDef, ToolError, ToolParams
from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

from .._bus._address import MEMBER_NAME
from .._bus._record import Draft, Record
from .._bus._storm import StormControls
from .._controller._controller import MemberInfo
from .._fence import fence, pack, utf8_bytes, with_lines
from ._channel import (
    Channel,
    ChannelRun,
    ChannelState,
    WaitNews,
    WaitSpec,
    swarm_channel,
)
from ._params import check_bound, check_rate
from .messages import check_member_names, own_caller, unread_messages

if TYPE_CHECKING:
    from .._bus._bus import SwarmBus

CHANNEL = "topics"
TAG = "topic_post"

# the longest record id a fence can name: the prefix and a number of up to 14 digits
_MAX_ID = "p-" + "9" * 14

# the largest count a header, remainder or other-topics line can hold
_MAX_COUNT = 10**14 - 1

# names of the form swarm-assigned ids take, kept for topics members may create later
_RESERVED = re.compile(r"c-[0-9]+")

_POST = "Post a message to a topic. Every member that reads the topic can read it with read_topics. Returns the post's id, such as p-3, which a reply names in reply_to."

_READ = (
    "Read your unread posts, oldest first, and mark them read: those in every topic you read, or in one topic. "
    "Each post is shown between <topic_post> tags whose topic attribute names the topic and whose from attribute names the poster. "
    "One call returns as many whole posts as fit, and says how many remain unread. "
    "It never waits: to wait for a post, call swarm_wait."
)

_LIST = "List the topics you can see, with who reads and who posts in each, whether you read or post in it, and how many posts you have unread in it."


# post_topic's and read_topics's signatures, reflected once for their parameters. Each
# run's tools take them in full, so they are not reflected: Inspect's reflection cache
# would keep them, and with them the run.
async def _post_topic(topic: str, message: str, reply_to: str | None = None) -> str:
    """Post to a topic.

    Args:
        topic: A topic you post to, such as orders or #orders.
        message: The text to post.
        reply_to: The id of a post this one answers, such as p-3. Optional.
    """
    ...


async def _read_topics(topic: str | None = None) -> str:
    """Read your unread posts.

    Args:
        topic: A topic you read, such as orders or #orders, to read only its posts. Optional: without it, the posts of every topic you read.
    """
    ...


_POST_PARAMETERS = ToolDef(_post_topic, description=_POST).parameters
_READ_PARAMETERS = ToolDef(_read_topics, description=_READ).parameters


class Topic(BaseModel):
    """A topic: a room that its readers read and its writers post to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    """The topic's name, addressed as `#<name>`: a lowercase letter, then at most 47 lowercase letters, digits, `_` or `-`, and not `c-` followed only by digits, which is reserved for swarm-assigned ids."""

    readers: Literal["all"] | list[str] = "all"
    """The members that read it: `"all"`, every member of the swarm, or a list of member names."""

    writers: Literal["all"] | list[str] = "all"
    """The members that post to it, in the same form."""

    private: bool = False
    """Whether only its readers and writers know it exists."""

    wake: bool = False
    """Whether a post ends its readers' `swarm_wait` and is news for an idle reader."""


class _Topics(Channel):
    name = CHANNEL
    prefix = "p"
    # a read marks posts read, so a member's topics have one reader: its own loop
    reach = "loop"

    def __init__(
        self,
        topics: Sequence[Topic],
        max_bytes: int,
        rate: tuple[int, float] | None,
        max_wait: int,
    ) -> None:
        self.topics = tuple(topics)
        self.storm = StormControls(max_bytes=max_bytes, rate=rate)
        self.max_bytes = max_bytes
        self.max_wait = max_wait

    def check(self, roster: Sequence[MemberInfo]) -> None:
        check_member_names(CHANNEL, roster)
        names = [info.name for info in roster]
        for topic in self.topics:
            for field in ("readers", "writers"):
                listed = cast(Literal["all"] | list[str], getattr(topic, field))
                given = names if listed == "all" else listed
                unknown = [n for n in given if n not in names]
                if unknown:
                    raise ValueError(
                        f"topics(): topic {topic.name} lists {unknown[0]!r} in its {field}, which is not a member of the swarm; the members are {', '.join(names)}."
                    )
            readers = _members(topic.readers, names)
            if not any(r != w for w in _members(topic.writers, names) for r in readers):
                raise ValueError(
                    f"topics(): topic {topic.name} has no writer with a reader other than itself, so no post to it could reach anyone."
                )

    def open(self, run: ChannelRun) -> ChannelState | None:
        return TopicsState(self, run)

    def instructions(self, member: MemberInfo) -> str | None:
        names = [member.name]
        both: list[str] = []
        reads: list[str] = []
        posts: list[str] = []
        wakes: list[str] = []
        for topic in self.topics:
            reader = member.name in _members(topic.readers, names)
            writer = member.name in _members(topic.writers, names)
            if reader and writer:
                both.append(topic.name)
            elif reader:
                reads.append(topic.name)
            elif writer:
                posts.append(topic.name)
            if reader and topic.wake:
                wakes.append(topic.name)
        access = [
            *([f"read and post in {_tags(both)}"] if both else []),
            *([f"read {_tags(reads)}"] if reads else []),
            *([f"post in {_tags(posts)}"] if posts else []),
        ]
        parts = [
            "You can post to the swarm's topics with `post_topic`, read the posts you have not read with `read_topics`, and list the topics you can see, with who reads and posts in each, with `list_topics`.",
            "Posts are shown between `<topic_post>` tags whose `from` attribute names the poster.",
            f"You {'; you '.join(access)}." if access else "You are in no topic.",
        ]
        if wakes:
            parts.append(
                f"A post in {_tags(wakes, ' or ')} ends `swarm_wait`, so call it to wait for one rather than polling."
            )
        parts.append(
            "While you have unread posts, the results of the swarm's built-in tools end with a line that counts them."
        )
        return " ".join(parts)


@swarm_channel
def topics(
    topics: Sequence[Topic | Mapping[str, object]] = (Topic(name="general"),),
    max_bytes: int = 8_000,
    rate: tuple[int, float] | list[int | float] | None = (20, 60.0),
    max_wait: int = 120,
) -> Channel:
    """Topics: named rooms that members post to and read.

    Each topic has readers, the members that read it, and writers, the members that post to it. A post reaches every reader of its topic but the poster, whether the reader is running, has not started yet, is held idle or has ended: a reader that starts later finds the post unread. With no arguments, the channel has one topic, `#general`, that every member reads and writes.

    Each member's own loop gets three tools. `post_topic` posts to a topic the member writes and returns the post's id, such as `p-3`. `read_topics` returns the member's unread posts, oldest first, in every topic it reads or in one, each inside a `<topic_post>` fence that names its id, topic and poster, and marks them read. `list_topics` lists the topics the member can see, with their readers and writers. While a member has unread posts, the results of `post_topic`, `list_topics`, `send_message`, `list_members`, `shared_memory` and `swarm_wait` in its own loop end with a line that counts them by topic, then a line with the totals of unread messages and posts. Nested loops, such as a `deepagent()` member's subagents, get none of these tools.

    A private topic exists only for its readers and writers: no other member is told its name, in its preamble, lists, counts or errors. A public topic is listed to every member. Topics do not restrict the messages channel, the shared memory or a shared sandbox.

    Every post goes through the swarm's bus as a record from the member that made it. A post is refused when the topic is not one the member can see or writes, when it has no reader but the poster, when it is empty or longer than `max_bytes`, or when the poster is over its `rate`.

    One read always returns at least the oldest unread post whole: the read is bounded by the largest fence and count lines the topics and roster allow plus `max_bytes`, and Inspect's tool output limit does not apply to it.

    A fence is a label for the reader, not a protection: a member can write text that looks like a fence, another member's post or a status line.

    The member's own loop also gets `swarm_wait`. A post in a topic with `wake=True` ends its readers' waits and is news for a reader held idle; posts in other topics are counted when a wait returns for another reason. A wait lasts up to `max_wait` seconds.

    The channel needs member names that start with a lowercase letter, followed by at most 47 lowercase letters, digits, `_` or `-`, other than `all` and `swarm`. Bridged members and members that choose their own names cannot be in a swarm with this channel.

    Args:
        topics: The topics, as `Topic`s or mappings of their fields. Each name is given once.
        max_bytes: The most UTF-8 bytes in one post, at least 1.
        rate: The most posts one member may make in a sliding window of seconds, across topics, as `(count, seconds)`; `None` for no limit.
        max_wait: The channel's longest `swarm_wait` in seconds, at least 1. With other channels that wake waits, the longest of theirs applies.

    Returns:
        The channel.

    Raises:
        ValueError: If there is no topic, a topic is malformed, a name is outside the grammar, reserved or given twice, a topic's `readers` or `writers` is an empty list or names a member twice, a bound is below its minimum, or `rate` is not a positive count and a finite, positive window.
    """
    check_bound(CHANNEL, "max_bytes", max_bytes, 1)
    check_bound(CHANNEL, "max_wait", max_wait, 1)
    given = cast(object, topics)
    if not isinstance(given, list | tuple):
        raise ValueError(
            f"topics(): topics must be a list of Topic or mappings of their fields, got a {type(given).__name__}."
        )
    checked = [_topic(item) for item in cast(Sequence[object], given)]
    if not checked:
        raise ValueError("topics(): give at least one topic.")
    seen: set[str] = set()
    for topic in checked:
        if topic.name in seen:
            raise ValueError(f"topics(): topic {topic.name} is given more than once.")
        seen.add(topic.name)
    return _Topics(
        checked,
        max_bytes,
        None if rate is None else check_rate(CHANNEL, rate),
        max_wait,
    )


def _topic(item: object) -> Topic:
    if isinstance(item, Topic):
        # rerun the checks: a model can be built without them
        topic = Topic.model_validate(item.model_dump())
    elif isinstance(item, Mapping):
        fields = dict(cast(Mapping[str, object], item))
        try:
            topic = Topic.model_validate(fields)
        except ValidationError as ex:
            problems = "; ".join(
                f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in ex.errors()
            )
            raise ValueError(
                f"topics(): topic {fields.get('name')!r} is not valid: {problems}."
            ) from None
    else:
        raise ValueError(
            f"topics(): each topic must be a Topic or a mapping of its fields, got a {type(item).__name__}."
        )
    if not MEMBER_NAME.fullmatch(topic.name):
        raise ValueError(
            f"topics(): topic name {topic.name!r} must start with a lowercase letter, followed by at most 47 lowercase letters, digits, _ or -."
        )
    if _RESERVED.fullmatch(topic.name):
        raise ValueError(
            f"topics(): topic name {topic.name} is reserved for swarm-assigned ids."
        )
    for field in ("readers", "writers"):
        listed = cast(Literal["all"] | list[str], getattr(topic, field))
        if listed == "all":
            continue
        if not listed:
            raise ValueError(
                f'topics(): topic {topic.name}: {field} must be "all" or a non-empty list of member names.'
            )
        repeated = next((n for i, n in enumerate(listed) if n in listed[:i]), None)
        if repeated is not None:
            raise ValueError(
                f"topics(): topic {topic.name}: {field} names {repeated!r} more than once."
            )
    return topic


# The posts of one swarm run, kept on the run's bus: each member's unread posts and the
# ids its replies may name.
class TopicsState:
    def __init__(self, channel: _Topics, run: ChannelRun) -> None:
        # the run's own bus, whose members beyond the public `Bus` the built-in
        # channels use
        self._bus = cast("SwarmBus", run.bus)
        names = [info.name for info in run.roster]
        # the topics in declaration order, with their readers and writers in roster
        # order
        self._topics = {t.name: t for t in channel.topics}
        self._readers = {t.name: _members(t.readers, names) for t in channel.topics}
        self._writers = {t.name: _members(t.writers, names) for t in channel.topics}
        # member -> its unread posts, in commit order
        self._unread: dict[str, list[Record]] = {name: [] for name in names}
        # member -> the ids of the posts it has posted or read, which its replies may
        # name
        self._known: dict[str, set[str]] = {name: set() for name in names}
        # the largest result of one read: its header, the largest fence the topics and
        # roster allow, a payload at the limit, and its remainder and other-topics lines
        # at the largest counts
        longest = max(self._topics, key=len)
        envelope = utf8_bytes(
            fence(
                TAG,
                [
                    ("id", _MAX_ID),
                    ("topic", f"#{longest}"),
                    ("from", max(names, key=len)),
                    ("reply_to", _MAX_ID),
                ],
                "",
            )
        )
        self._header_max = utf8_bytes(_header(_MAX_COUNT))
        self._trailer_max = (
            max(
                utf8_bytes(_remainder(_MAX_COUNT, None)),
                utf8_bytes(_remainder(_MAX_COUNT, longest)),
            )
            + len("\n")
            + utf8_bytes(_others([(name, _MAX_COUNT) for name in self._topics]))
        )
        self.budget = (
            self._header_max
            + len("\n\n")
            + envelope
            + channel.max_bytes
            + len("\n")
            + self._trailer_max
        )
        self._tools = [self._post_tool(), self._read_tool(), self._list_tool()]

    def tools(self) -> list[Tool]:
        return list(self._tools)

    # the bus calls it in place of the member resolver: a post is addressed to its
    # topic and reaches the topic's readers, whatever their state
    def resolve(self, sender: str, to: tuple[str, ...]) -> tuple[str, ...]:
        if len(to) != 1 or not to[0].startswith("#"):
            raise ToolError("post to one topic at a time, such as #orders.")
        name = self._visible(sender, to[0][1:])
        writers = self._writers[name]
        if sender not in writers:
            raise ToolError(
                f"you cannot post to #{name}; its writers are {', '.join(writers)}."
            )
        return tuple(r for r in self._readers[name] if r != sender)

    def validate(self, record: Record) -> None:
        if record.kind != "post" or record.op is not None:
            raise ToolError("a topics record must be a post with no operation")
        if not record.payload:
            raise ToolError("post is empty.")
        if not record.recipients:
            raise ToolError(f"{record.to[0]} has no reader but you.")
        if record.reply_to is not None and record.reply_to not in self._known.get(
            record.sender, set()
        ):
            raise ToolError(
                "reply_to must be the id of a post you have posted or read, such as p-3."
            )

    def admit(self, record: Record) -> None:
        return None

    def apply(self, record: Record) -> tuple[list[str], Mapping[str, JsonValue] | None]:
        for name in record.recipients:
            self._unread[name].append(record)
        self._known[record.sender].add(record.id)
        return list(record.recipients), None

    # waiting: the member's own loop's wait returns while it has an unread post in a
    # topic that wakes; nothing here marks anything

    def wait_check(self, spec: WaitSpec) -> None:
        # the wait's filters are the shared memory's
        return None

    def wakes(self, spec: WaitSpec, record: Record) -> bool:
        return spec.own and spec.member in record.recipients

    def news(self, spec: WaitSpec) -> WaitNews | None:
        unread = self._unread.get(spec.member, []) if spec.own else []
        if not any(r.wake for r in unread):
            return None
        topics = [name for name, _ in self._counts(unread)]
        posters = ", ".join(sorted({r.sender for r in unread}))
        return WaitNews(
            lines=[
                f"{_posts(len(unread))} in {_tags(topics)} (from {posters}). Call read_topics() to read them."
            ],
            files=[{"record": r.id, "from": r.sender} for r in unread],
            # the line counts every unread post and names every topic and poster
            depends_on=[r.id for r in unread],
        )

    # the status line

    def unread_count(self, member: str) -> int:
        return len(self._unread[member])

    def status(self, member: str, messages: int | None) -> str | None:
        # the per-topic counts, then the totals, with `messages` the member's unread
        # messages when the swarm has the messages channel; None without unread posts.
        # Counts and author-given names only, never a payload.
        counts = self._counts(self._unread[member])
        if not counts:
            return None
        total = sum(count for _, count in counts)
        detail = ", ".join(f"#{name} {count}" for name, count in counts)
        summary = (
            f"You have {unread_messages(messages)} and {_posts(total)}."
            if messages
            else f"You have {_posts(total)}."
        )
        return f"Unread posts: {detail}.\n{summary}"

    # the tools

    def _post_tool(self) -> Tool:
        async def execute(topic: str, message: str, reply_to: str | None = None) -> str:
            # an ended member's post reaches the bus; if it passes the initial checks,
            # the bus refuses it with liveness evidence
            context = own_caller(self._bus, "post_topic", running=False)
            name = _name(topic)
            # the bus refuses a topic that does not exist when it resolves the address
            known = self._topics.get(name)
            delivery = await self._bus.deliver(
                Draft(
                    CHANNEL,
                    "post",
                    to=(f"#{name}",),
                    payload=message,
                    wake=known is not None and known.wake,
                    reply_to=reply_to,
                )
            )
            record = delivery.record
            return with_lines(
                f"Posted {record.id} to #{name}, read by {', '.join(record.recipients)}.",
                self._bus.status(context),
            )

        return ToolDef(
            execute,
            name="post_topic",
            description=_POST,
            parameters=_POST_PARAMETERS.model_copy(deep=True),
            parallel=True,
        ).as_tool()

    def _read_tool(self) -> Tool:
        async def execute(topic: str | None = None) -> str:
            context = own_caller(self._bus, "read_topics")
            name = None
            if topic is not None:
                name = self._visible(context.name, _name(topic))
                if context.name not in self._readers[name]:
                    raise ToolError(f"you do not read #{name}.")
            return self._read(context.name, name)

        return ToolDef(
            execute,
            name="read_topics",
            description=_READ,
            parameters=_READ_PARAMETERS.model_copy(deep=True),
            parallel=True,
            # the tool bounds its own result by the read budget, so that no limit cuts
            # through a fence
            max_output=0,
        ).as_tool()

    def _list_tool(self) -> Tool:
        async def execute() -> str:
            """List the topics you can see."""
            context = own_caller(self._bus, "list_topics")
            member = context.name
            counts = dict(self._counts(self._unread[member]))
            lines: list[str] = []
            for name, topic in self._topics.items():
                if not self._sees(member, name):
                    continue
                reader = member in self._readers[name]
                writer = member in self._writers[name]
                access = (
                    "you read and post"
                    if reader and writer
                    else "you read"
                    if reader
                    else "you post"
                    if writer
                    else "you are not in it"
                )
                unread = counts.get(name, 0)
                lines.append(
                    f"#{name}{' (private)' if topic.private else ''}: readers {_listed(topic.readers, self._readers[name])}; writers {_listed(topic.writers, self._writers[name])}; {access}."
                    + (f" {unread} unread." if unread else "")
                )
            return with_lines(
                "\n".join(lines) or "You are in no topic.",
                self._bus.status(context),
            )

        return ToolDef(
            execute,
            name="list_topics",
            description=_LIST,
            parameters=ToolParams(),
            parallel=True,
        ).as_tool()

    def _sees(self, member: str, name: str) -> bool:
        return (
            not self._topics[name].private
            or member in self._readers[name]
            or member in self._writers[name]
        )

    def _visible(self, member: str, name: str) -> str:
        # the topic, if the member can see it; a private topic it is not in gets the
        # same error as a name that does not exist, which names only what it can see
        if name in self._topics and self._sees(member, name):
            return name
        visible = [t for t in self._topics if self._sees(member, t)]
        listed = (
            f"your topics are {_tags(visible)}" if visible else "you are in no topic"
        )
        # a name outside the grammar is model output that may not be valid text, so it
        # is shown escaped
        shown = f"#{name}" if MEMBER_NAME.fullmatch(name) else repr(f"#{name}")
        raise ToolError(f"no topic {shown}; {listed}.")

    def _counts(self, records: Sequence[Record]) -> list[tuple[str, int]]:
        # the unread posts by topic, in declaration order, for topics with any
        counts = dict.fromkeys(self._topics, 0)
        for record in records:
            counts[record.to[0][1:]] += 1
        return [(name, count) for name, count in counts.items() if count]

    def _read(self, member: str, topic: str | None) -> str:
        # synchronous from the unread posts to the mark, so what is shown is what is
        # marked
        unread = self._unread[member]
        selected = [r for r in unread if topic is None or r.to[0] == f"#{topic}"]
        if selected:
            size = self._header_max + len("\n\n") + len("\n") + self._trailer_max
            shown = pack([_fenced(r) for r in selected], size, self.budget)
            read = selected[: len(shown)]
            ids = {r.id for r in read}
            unread[:] = [r for r in unread if r.id not in ids]
            self._known[member].update(ids)
            self._bus.read(CHANNEL, [r.id for r in read])
            text = _header(len(read)) + "\n\n" + "\n".join(shown)
            left = len(selected) - len(read)
            if left:
                text += "\n" + _remainder(left, topic)
        else:
            text = "You have no unread posts" + (
                f" in #{topic}." if topic is not None else "."
            )
        if topic is not None:
            others = [(n, c) for n, c in self._counts(unread) if n != topic]
            if others:
                text += "\n" + _others(others)
        return text


# The built-in topics channel's state among a run's open channel states, by its type
# rather than its name, which a custom channel may take; None without one.
def builtin_topics(states: Mapping[str, ChannelState]) -> TopicsState | None:
    return next((s for s in states.values() if isinstance(s, TopicsState)), None)


def _members(listed: Literal["all"] | list[str], roster: Sequence[str]) -> list[str]:
    # a topic's readers or writers, in roster order however they were listed
    return [name for name in roster if listed == "all" or name in listed]


def _name(topic: str) -> str:
    # the tools take a topic with or without its #
    return topic[1:] if topic.startswith("#") else topic


def _listed(listed: Literal["all"] | list[str], members: Sequence[str]) -> str:
    # a topic's readers or writers as configured, "all" or its members in roster order
    return "all members" if listed == "all" else ", ".join(members)


def _tags(names: Sequence[str], last: str = ", ") -> str:
    tags = [f"#{name}" for name in names]
    if len(tags) < 2:
        return "".join(tags)
    return ", ".join(tags[:-1]) + last + tags[-1]


def _fenced(record: Record) -> str:
    attributes = [
        ("id", record.id),
        ("topic", record.to[0]),
        ("from", record.sender),
    ]
    if record.reply_to is not None:
        attributes.append(("reply_to", record.reply_to))
    return fence(TAG, attributes, record.payload)


def _posts(count: int) -> str:
    return "1 unread post" if count == 1 else f"{count} unread posts"


def _header(count: int) -> str:
    if count == 1:
        return "1 post."
    return f"{count} posts, oldest first."


def _remainder(count: int, topic: str | None) -> str:
    more = "1 more unread post" if count == 1 else f"{count} more unread posts"
    if topic is None:
        return f"{more}; call read_topics() again."
    return f'{more} in #{topic}; call read_topics(topic="{topic}") again.'


def _others(counts: Sequence[tuple[str, int]]) -> str:
    total = sum(count for _, count in counts)
    detail = ", ".join(f"#{name} {count}" for name, count in counts)
    return f"You have {_posts(total)} in other topics ({detail})."
