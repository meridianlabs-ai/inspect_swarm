import importlib
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from typing import Any, cast
from unittest.mock import patch

import anyio
import pytest
from inspect_ai.event import CommunicationRejection
from inspect_ai.log._transcript import Transcript
from inspect_ai.model import (
    ChatMessageTool,
    ChatMessageUser,
    ModelOutput,
    execute_tools,
)
from inspect_ai.tool import ToolDef, ToolError

from inspect_swarm import (
    Channel,
    Draft,
    Topic,
    messages,
    shared_memory,
    topics,
)
from inspect_swarm._channel.topics import TopicsState
from inspect_swarm._fence import utf8_bytes
from inspect_swarm._ledger import EventLog
from inspect_swarm._metrics import _comm_metrics  # pyright: ignore[reportPrivateUsage]

from .swarm_runtime import comm_events, data, delivery, sample, send
from .swarm_runtime import wait as wait_of
from .test_messages import _Run  # pyright: ignore[reportPrivateUsage]

STOPPING = "The swarm is stopping."

# a hub and spokes: the lead gives each worker its orders in a private topic, which
# wakes it, and reads every report
HUB = [
    Topic(
        name="orders-w-1", readers=["w-1"], writers=["lead"], private=True, wake=True
    ),
    Topic(
        name="orders-w-2", readers=["w-2"], writers=["lead"], private=True, wake=True
    ),
    Topic(name="reports", readers=["lead"], writers=["w-1", "w-2"], wake=True),
    Topic(name="general"),
]


@asynccontextmanager
async def run(
    members: Sequence[str] = ("lead", "w-1", "w-2"),
    channel: Channel | None = None,
    *others: Channel,
) -> AsyncGenerator[tuple[_Run, Transcript]]:
    async with sample() as transcript:
        with anyio.fail_after(10):
            yield (
                _Run(members, [channel or topics(HUB, rate=None), *others]),
                transcript,
            )


async def post(r: _Run, member: str, topic: str, message: str, **args: Any) -> str:
    return await r.call(member, "post_topic", topic=topic, message=message, **args)


async def read(r: _Run, member: str, topic: str | None = None) -> str:
    return await r.call(member, "read_topics", topic=topic)


def _state(r: _Run) -> TopicsState:
    return cast(TopicsState, r.bus.states["topics"])


def _rejected(transcript: Transcript) -> list[str | None]:
    return [
        (rejected := send(s).rejected) and rejected.control
        for s in comm_events(transcript, "sent")
    ]


# Posting and reading


@pytest.mark.anyio
async def test_a_post_reaches_its_readers_and_is_read_once_fenced() -> None:
    async with run() as (r, transcript):
        assert await post(r, "w-1", "#general", "hello") == (
            "Posted p-1 to #general, read by lead, w-2."
        )
        assert await read(r, "w-2") == (
            "1 post.\n\n"
            '<topic_post id="p-1" topic="#general" from="w-1">\nhello\n</topic_post>'
        )
        assert await read(r, "w-2") == "You have no unread posts."
        # the poster is not a reader of its own post
        assert await read(r, "w-1") == "You have no unread posts."
        [sent] = comm_events(transcript, "sent")
        record = send(sent).record
        assert (record.kind, record.to, record.recipients) == (
            "post",
            ["#general"],
            ["lead", "w-2"],
        )
        assert data(sent)["record"]["routed_out"] == []
        [delivered] = comm_events(transcript, "delivered")
        assert delivery(delivered).recipients == ["lead", "w-2"]
        [shown] = comm_events(transcript, "read")
        assert (shown.agent, shown.channel, shown.record_ids) == (
            "w-2",
            "topics",
            ["p-1"],
        )


@pytest.mark.anyio
async def test_readers_and_writers_are_named_in_roster_order() -> None:
    channel = topics([Topic(name="t", readers=["b", "a"], writers=["lead", "a"])])
    async with run(("a", "b", "lead"), channel) as (r, transcript):
        assert await post(r, "lead", "t", "x") == "Posted p-1 to #t, read by a, b."
        assert send(comm_events(transcript, "sent")[0]).record.recipients == [
            "a",
            "b",
        ]
        assert await r.call("b", "list_topics") == (
            "#t: readers a, b; writers a, lead; you read. 1 unread.\n"
            "Unread posts: #t 1.\n"
            "You have 1 unread post."
        )


@pytest.mark.anyio
async def test_a_post_reaches_readers_whatever_their_state() -> None:
    members = ("lead", "w-1", "w-2", "w-3")
    channel = topics([Topic(name="orders", writers=["lead"])], rate=None)
    async with run(members, channel) as (r, transcript):
        r.members["w-1"].state = "pending"
        r.members["w-2"].state = "idle"
        r.members["w-3"].state, r.members["w-3"].status = "ended", "submitted"
        assert await post(r, "lead", "orders", "go") == (
            "Posted p-1 to #orders, read by w-1, w-2, w-3."
        )
        assert delivery(comm_events(transcript, "delivered")[0]).recipients == [
            "w-1",
            "w-2",
            "w-3",
        ]
        # a reader that starts later finds it unread
        r.members["w-1"].state = "running"
        assert "go" in await read(r, "w-1")
        # a poster that has ended is refused, with evidence
        r.members["lead"].state, r.members["lead"].status = "ended", "submitted"
        reason = "lead has ended, so its post was not delivered."
        with pytest.raises(ToolError) as raised:
            await post(r, "lead", "orders", "late")
        assert raised.value.message == reason
        assert send(comm_events(transcript, "sent")[-1]).rejected == (
            CommunicationRejection(control="liveness", reason=reason)
        )
        assert len(comm_events(transcript, "delivered")) == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "member,tool,args,error",
    [
        pytest.param(
            "w-1",
            "post_topic",
            {"topic": "reports-x", "message": "x"},
            "no topic #reports-x; your topics are #orders-w-1, #reports, #general.",
            id="unknown",
        ),
        pytest.param(
            "w-1",
            "post_topic",
            {"topic": "orders-w-2", "message": "x"},
            "no topic #orders-w-2; your topics are #orders-w-1, #reports, #general.",
            id="private-not-in",
        ),
        pytest.param(
            "w-1",
            "read_topics",
            {"topic": "orders-w-2"},
            "no topic #orders-w-2; your topics are #orders-w-1, #reports, #general.",
            id="read-private-not-in",
        ),
        pytest.param(
            "w-1",
            "post_topic",
            {"topic": "orders-w-1", "message": "x"},
            "you cannot post to #orders-w-1; its writers are lead.",
            id="not-a-writer",
        ),
        pytest.param(
            "w-1",
            "read_topics",
            {"topic": "#reports"},
            "you do not read #reports.",
            id="not-a-reader",
        ),
        pytest.param(
            "w-1",
            "post_topic",
            {"topic": "general", "message": ""},
            "post is empty.",
            id="empty",
        ),
        pytest.param(
            "w-1",
            "post_topic",
            {"topic": "general", "message": "x", "reply_to": "p-1"},
            "reply_to must be the id of a post you have posted or read, such as p-3.",
            id="reply-to-unknown",
        ),
    ],
)
async def test_a_refused_post_or_read_leaves_no_record(
    member: str, tool: str, args: dict[str, Any], error: str
) -> None:
    async with run() as (r, transcript):
        with pytest.raises(ToolError) as raised:
            await r.call(member, tool, **args)
        assert raised.value.message == error
        assert comm_events(transcript, "sent") == []
        assert comm_events(transcript, "read") == []


@pytest.mark.anyio
@pytest.mark.parametrize("tool", ["post_topic", "read_topics"])
@pytest.mark.parametrize("name", ["\ud800", "#\udfff", "or\ud800ders"])
async def test_a_topic_name_that_is_not_valid_text_is_shown_escaped(
    tool: str, name: str
) -> None:
    # model output can hold a lone surrogate, which no request to a provider can carry
    args = {"topic": name, **({"message": "x"} if tool == "post_topic" else {})}
    async with run() as (r, transcript):
        with pytest.raises(ToolError) as raised:
            await r.call("w-1", tool, **args)
        error = raised.value.message
        error.encode("utf-8")
        shown = repr(name if name.startswith("#") else f"#{name}")
        assert error == (
            f"no topic {shown}; your topics are #orders-w-1, #reports, #general."
        )
        assert comm_events(transcript, "sent") == []


@pytest.mark.anyio
async def test_a_post_is_addressed_to_one_topic_with_a_reader() -> None:
    channel = topics([Topic(name="solo", readers=["a"], writers=["a", "b"])])
    async with run(("a", "b"), channel) as (r, transcript):
        with pytest.raises(ToolError) as raised:
            await post(r, "a", "solo", "x")
        assert raised.value.message == "#solo has no reader but you."
        async with r._as("a", "top", "post_topic"):  # pyright: ignore[reportPrivateUsage]
            for to in (("solo",), ("#solo", "#solo"), ()):
                with pytest.raises(ToolError) as raised:
                    await r.bus.deliver(Draft("topics", "post", to=to, payload="x"))
                assert raised.value.message == (
                    "post to one topic at a time, such as #orders."
                )
        assert comm_events(transcript, "sent") == []
        assert await post(r, "b", "solo", "x") == "Posted p-1 to #solo, read by a."


@pytest.mark.anyio
async def test_reply_to_names_a_post_the_poster_posted_or_read() -> None:
    async with run() as (r, transcript):
        await post(r, "w-1", "general", "question")
        await read(r, "w-2")
        await post(r, "w-2", "general", "answer", reply_to="p-1")
        await post(r, "w-1", "general", "follow-up", reply_to="p-1")
        assert '<topic_post id="p-2" topic="#general" from="w-2" reply_to="p-1">' in (
            await read(r, "w-1")
        )
        assert [send(s).record.reply_to for s in comm_events(transcript, "sent")] == [
            None,
            "p-1",
            "p-1",
        ]


@pytest.mark.anyio
async def test_a_private_topic_is_hidden_from_members_outside_it() -> None:
    async with run() as (r, _):
        await post(r, "lead", "orders-w-1", "build")
        # w-2 sees its own orders, not w-1's
        assert await r.call("w-2", "list_topics") == (
            "#orders-w-2 (private): readers w-2; writers lead; you read.\n"
            "#reports: readers lead; writers w-1, w-2; you post.\n"
            "#general: readers all members; writers all members; you read and post."
        )
        assert await r.call("w-1", "list_topics") == (
            "#orders-w-1 (private): readers w-1; writers lead; you read. 1 unread.\n"
            "#reports: readers lead; writers w-1, w-2; you post.\n"
            "#general: readers all members; writers all members; you read and post.\n"
            "Unread posts: #orders-w-1 1.\n"
            "You have 1 unread post."
        )
        assert (await r.call("lead", "list_topics")).startswith(
            "#orders-w-1 (private): readers w-1; writers lead; you post.\n"
            "#orders-w-2 (private): readers w-2; writers lead; you post.\n"
        )


@pytest.mark.anyio
async def test_a_member_in_no_topic_is_told_so() -> None:
    channel = topics(
        [Topic(name="pair", readers=["a"], writers=["b"], private=True)], rate=None
    )
    async with run(("a", "b", "c"), channel) as (r, _):
        assert await r.call("c", "list_topics") == "You are in no topic."
        with pytest.raises(ToolError) as raised:
            await post(r, "c", "pair", "x")
        assert raised.value.message == "no topic #pair; you are in no topic."


@pytest.mark.anyio
async def test_reads_follow_commit_order_and_one_topic_can_be_read_alone() -> None:
    async with run() as (r, _):
        await post(r, "lead", "orders-w-1", "one")
        await post(r, "w-2", "general", "two")
        await post(r, "lead", "orders-w-1", "three")
        await post(r, "lead", "general", "four")
        # a read of one topic is followed by the count of the others
        assert await read(r, "w-1", "general") == (
            "2 posts, oldest first.\n\n"
            '<topic_post id="p-2" topic="#general" from="w-2">\ntwo\n</topic_post>\n'
            '<topic_post id="p-4" topic="#general" from="lead">\nfour\n</topic_post>\n'
            "You have 2 unread posts in other topics (#orders-w-1 2)."
        )
        assert await read(r, "w-1", "#general") == (
            "You have no unread posts in #general.\n"
            "You have 2 unread posts in other topics (#orders-w-1 2)."
        )
        await post(r, "w-2", "general", "five")
        result = await read(r, "w-1")
        assert [line for line in result.split("\n") if "<topic_post" in line] == [
            '<topic_post id="p-1" topic="#orders-w-1" from="lead">',
            '<topic_post id="p-3" topic="#orders-w-1" from="lead">',
            '<topic_post id="p-5" topic="#general" from="w-2">',
        ]
        assert (
            await read(r, "w-1", "general") == "You have no unread posts in #general."
        )


@pytest.mark.anyio
async def test_a_read_that_does_not_fit_says_how_many_remain() -> None:
    async with run(channel=topics(HUB, rate=None, max_bytes=100)) as (r, _):
        for n in range(3):
            await post(r, "lead", "general", f"{n}" * 100)
        await post(r, "lead", "orders-w-1", "go")
        first = await read(r, "w-1")
        assert first.startswith("1 post.\n\n")
        assert first.endswith("\n3 more unread posts; call read_topics() again.")
        second = await read(r, "w-1", "general")
        assert second.startswith("1 post.\n\n")
        assert second.endswith(
            '\n1 more unread post in #general; call read_topics(topic="general") again.\n'
            "You have 1 unread post in other topics (#orders-w-1 1)."
        )
        # the read draining the topic still counts the others
        assert (await read(r, "w-1", "general")).endswith(
            "</topic_post>\nYou have 1 unread post in other topics (#orders-w-1 1)."
        )


@pytest.mark.anyio
@pytest.mark.parametrize("filtered", [False, True])
async def test_every_post_fits_in_one_read_within_the_budget(filtered: bool) -> None:
    # the longest member and topic names, every topic in the other-topics line, and
    # payloads at the limit of four-byte characters with a reply_to
    names = [c * 48 for c in "abcdefghijklmnop"]
    declared = [Topic(name=c * 48) for c in "qrstuvwxyz"]
    channel = topics(declared, rate=None, max_bytes=8_000)
    async with run(names, channel) as (r, _):
        poster, reader = names[0], names[1]
        target = declared[0].name
        await post(r, reader, target, "?")
        await read(r, poster)
        for topic in declared[1:]:
            await post(r, poster, topic.name, "x")
        for _ in range(3):
            await post(r, poster, target, "😀" * 2_000, reply_to="p-1")
        state = _state(r)
        assert state.budget < 10_200
        results: list[str] = []
        while sum(result.count("</topic_post>") for result in results) < 3 + (
            0 if filtered else 9
        ):
            results.append(await read(r, reader, target if filtered else None))
        for result in results:
            assert utf8_bytes(result) <= state.budget
            assert result.count("</topic_post>") >= 1
        if filtered:
            assert results[-1].endswith(
                "other topics ("
                + ", ".join(f"#{t.name} 1" for t in declared[1:])
                + ")."
            )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "channel,post_tool,read_tool,args",
    [
        pytest.param(
            topics(rate=None), "post_topic", "read_topics", {"topic": "general"}
        ),
        pytest.param(
            messages(rate=None, dedupe=False),
            "send_message",
            "read_messages",
            {"to": "b"},
        ),
    ],
    ids=["topics", "messages"],
)
async def test_a_read_formats_no_more_of_a_backlog_than_it_shows(
    channel: Channel, post_tool: str, read_tool: str, args: dict[str, Any]
) -> None:
    # each read shows one record of a long backlog, and renders at most the next
    module = importlib.import_module(f"inspect_swarm._channel.{channel.name}")
    rendered: list[str] = []
    original = module._fenced

    def counted(record: Any) -> str:
        rendered.append(record.id)
        return original(record)

    async with run(("a", "b"), channel) as (r, _):
        for _ in range(20):
            await r.call("a", post_tool, message="x" * 6_000, **args)
        with patch.object(module, "_fenced", counted):
            for _ in range(2):
                rendered.clear()
                assert (await r.call("b", read_tool)).startswith("1 ")
                assert len(rendered) == 2


@pytest.mark.anyio
async def test_a_read_is_not_cut_by_the_tool_output_limit() -> None:
    async with run() as (r, _):
        await post(r, "lead", "general", "x" * 500)
        tools = list((await r.resolve("w-1")).values())
        call = ModelOutput.for_tool_call("mockllm", "read_topics", {}).message
        async with r._as("w-1", "top", None):  # pyright: ignore[reportPrivateUsage]
            result = await execute_tools(
                [ChatMessageUser(content="task"), call], tools, max_output=50
            )
        [message] = [m for m in result.messages if isinstance(m, ChatMessageTool)]
        assert "x" * 500 in message.text


@pytest.mark.anyio
async def test_the_storm_controls_refuse_a_post() -> None:
    channel = topics(max_bytes=8, rate=(2, 60.0))
    async with run(("a", "b"), channel) as (r, transcript):
        await post(r, "a", "general", "one")
        with pytest.raises(ToolError) as raised:
            await post(r, "a", "general", "😀😀😀")
        assert raised.value.message == "post is 12 bytes; the limit is 8"
        await post(r, "a", "general", "two")
        with pytest.raises(ToolError, match="^rate limit: retry in"):
            await post(r, "a", "general", "three")
        assert _rejected(transcript) == [None, "max_bytes", None, "rate"]


@pytest.mark.anyio
async def test_a_subagent_gets_no_topic_tools() -> None:
    async with run() as (r, transcript):
        assert {"post_topic", "read_topics", "list_topics", "swarm_wait"} <= set(
            await r.resolve("w-1")
        )
        assert set(await r.resolve("w-1", "child")) == set()
        await post(r, "lead", "general", "x")
        for name, args in (
            ("read_topics", {}),
            ("post_topic", {"topic": "general", "message": "y"}),
            ("list_topics", {}),
        ):
            with pytest.raises(ToolError, match="a subagent cannot use it"):
                await r.call("w-1", name, "child", **args)
        assert len(comm_events(transcript, "sent")) == 1


@pytest.mark.anyio
async def test_posts_are_counted_per_member_by_the_metrics() -> None:
    async with run() as (r, transcript):
        events = EventLog()
        transcript._subscribe(events)  # pyright: ignore[reportPrivateUsage]
        await post(r, "lead", "general", "abc")
        await post(r, "lead", "orders-w-1", "de")
        await read(r, "w-1")
        await r.call("w-1", "list_topics")
        comm = _comm_metrics(events, ["lead", "w-1"], ["topics"])
        assert (
            comm["lead"]["topics"].records,
            comm["lead"]["topics"].record_bytes,
        ) == (
            2,
            5,
        )
        # a list shows no posts, so it is no read
        assert (comm["w-1"]["topics"].reads, comm["w-1"]["topics"].listings) == (1, 0)


# The status line


def _two(messages_first: bool) -> list[Channel]:
    channels: list[Channel] = [messages(rate=None), topics(HUB, rate=None)]
    return channels if messages_first else channels[::-1]


@pytest.mark.anyio
@pytest.mark.parametrize("messages_first", [True, False])
async def test_the_swarms_tools_end_with_the_unread_posts_and_their_totals(
    messages_first: bool,
) -> None:
    async with run(("lead", "w-1", "w-2"), *_two(messages_first), shared_memory()) as (
        r,
        _,
    ):
        await post(r, "lead", "orders-w-1", "a")
        await post(r, "lead", "general", "b")
        await post(r, "lead", "orders-w-1", "c")
        await r.send("w-2", "w-1", "d", wake=False)
        end = (
            "\nUnread posts: #orders-w-1 2, #general 1.\n"
            "You have 1 unread message and 3 unread posts."
        )
        results = {
            "post_topic": await post(r, "w-1", "reports", "e"),
            "list_topics": await r.call("w-1", "list_topics"),
            "send_message": await r.send("w-1", "lead", "f"),
            "list_members": await r.call("w-1", "list_members"),
            "shared_memory": await r.call("w-1", "shared_memory", command="changes"),
            # the news of the waking posts, then the totals, since a message is unread
            "swarm_wait": await r.call("w-1", "swarm_wait"),
        }
        for tool, result in results.items():
            assert result.endswith(end), tool
        assert results["swarm_wait"] == (
            "3 unread posts in #orders-w-1, #general (from lead). Call read_topics() to read them."
            + end
        )
        # with no unread message, the totals count posts alone
        await r.call("w-1", "read_messages")
        assert (await r.call("w-1", "list_members")).endswith(
            "\nUnread posts: #orders-w-1 2, #general 1.\nYou have 3 unread posts."
        )
        # a subagent's results count nothing
        assert (await r.call("w-1", "shared_memory", "child", command="changes")) == (
            "Shared memory: no files changed since you last viewed them."
        )
        # with nothing unread, every result is as it is without the topics channel
        await read(r, "w-1")
        assert (await r.call("w-1", "list_members")).endswith(
            "\nYou have no unread messages."
        )
        assert await r.send("w-1", "lead", "g") == "Sent m-3 to lead."


@pytest.mark.anyio
@pytest.mark.parametrize("messages_first", [True, False])
@pytest.mark.parametrize(
    "tool,args",
    [
        ("post_topic", {"topic": "c" * 48, "message": "x"}),
        ("list_topics", {}),
        ("send_message", {"to": "lead", "message": "x"}),
        ("list_members", {}),
        ("shared_memory", {"command": "changes"}),
        ("swarm_wait", {}),
    ],
)
async def test_truncation_keeps_both_totals(
    messages_first: bool, tool: str, args: dict[str, Any]
) -> None:
    # the longest topic names: the detail line is long, the totals line short
    declared = [Topic(name=c * 48) for c in "abc"]
    channels: list[Channel] = [messages(rate=None), topics(declared, rate=None)]
    if not messages_first:
        channels.reverse()
    async with run(("lead", "w-1"), *channels, shared_memory()) as (r, _):
        for topic in declared:
            await post(r, "lead", topic.name, "x")
        await r.send("lead", "w-1", "y", wake=False)
        tools = list((await r.resolve("w-1")).values())
        call = ModelOutput.for_tool_call("mockllm", tool, args).message
        if tool == "swarm_wait":
            r.bus.close()
        async with r._as("w-1", "top", None):  # pyright: ignore[reportPrivateUsage]
            result = await execute_tools(
                [ChatMessageUser(content="task"), call], tools, max_output=100
            )
        [message] = [m for m in result.messages if isinstance(m, ChatMessageTool)]
        assert "truncated" in message.text
        assert "You have 1 unread message and 3 unread posts." in message.text


# Waiting


@pytest.mark.anyio
async def test_a_post_in_a_waking_topic_ends_its_readers_wait() -> None:
    async with run() as (r, transcript):
        results: dict[str, str] = {}

        async def wait(member: str) -> None:
            results[member] = await r.call(member, "swarm_wait")

        async with anyio.create_task_group() as tg:
            tg.start_soon(wait, "w-1")
            tg.start_soon(wait, "w-2")
            await anyio.wait_all_tasks_blocked()
            # a quiet topic wakes no one
            await post(r, "lead", "general", "quiet")
            await anyio.wait_all_tasks_blocked()
            assert results == {}
            await post(r, "lead", "orders-w-1", "go")
            await anyio.wait_all_tasks_blocked()
            assert results == {
                "w-1": "2 unread posts in #orders-w-1, #general (from lead). Call read_topics() to read them."
            }
            r.bus.close()
        # w-2's wait counts the quiet post when it returns for another reason
        assert results["w-2"] == (
            f"{STOPPING}\nUnread posts: #general 1.\nYou have 1 unread post."
        )
        waits = {e.agent: e for e in comm_events(transcript, "waited")}
        assert (wait_of(waits["w-1"]).outcome, data(waits["w-1"])["depends_on"]) == (
            "news",
            ["p-1", "p-2"],
        )
        assert data(waits["w-2"])["depends_on"] == []
        assert "topics" in wait_of(waits["w-1"]).channels


@pytest.mark.anyio
async def test_swarm_wait_is_described_for_topics() -> None:
    first = "Wait until there is something for you to look at in the swarm: "
    cases: list[tuple[list[Channel], str, list[str]]] = [
        (
            [topics(rate=None)],
            "an unread post in a topic that wakes its readers, or another member has ended.",
            ["timeout"],
        ),
        (
            [topics(rate=None), messages(rate=None)],
            "an unread message sent to you with wake=True, an unread post in a topic that wakes its readers, or another member has ended.",
            ["timeout"],
        ),
        (
            [topics(rate=None), shared_memory(rate=None)],
            "an unread post in a topic that wakes its readers, a shared memory file",
            ["path", "timeout", "file_id"],
        ),
    ]
    for channels, sources, parameters in cases:
        async with run(("a", "b"), *channels) as (r, _):
            own = r.bus.waits.tool(True)
            assert own is not None
            description = ToolDef(own).description
            assert description.startswith(first + sources)
            assert "read_topics" in description
            assert list(ToolDef(own).parameters.properties) == parameters
