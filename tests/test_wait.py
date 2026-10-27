import asyncio
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from typing import Any, cast

import anyio
import pytest
from anyio.abc import TaskGroup
from inspect_ai import Task, eval_async  # pyright: ignore[reportUnknownVariableType]
from inspect_ai.agent import Agent, AgentState, agent, react
from inspect_ai.agent._channel import agent_channel, current_agent_channel
from inspect_ai.dataset import Sample
from inspect_ai.event import InfoEvent, ToolEvent
from inspect_ai.log import EvalSample
from inspect_ai.log._transcript import Transcript
from inspect_ai.model import ChatMessageAssistant, ModelOutput
from inspect_ai.tool import Tool, ToolCall, ToolDef, ToolError, tool
from inspect_ai.util import Limit, time_limit
from inspect_ai.util._span import span

from inspect_swarm import (
    Budget,
    Member,
    SwarmControl,
    member,
    shared_memory,
    swarm,
    swarm_tools,
)
from inspect_swarm._budget import current_member
from inspect_swarm._bus._bus import Bus
from inspect_swarm._channel.shared_memory import (
    _SharedMemoryState,  # pyright: ignore[reportPrivateUsage]
)
from inspect_swarm._fence import utf8_bytes

from .swarm_runtime import (
    Script,
    controller,
    evidence,
    run_swarm,
    sample,
    scripted_model,
    sleeper,
)
from .test_shared_memory import _Member  # pyright: ignore[reportPrivateUsage]

ROOT = "/shared_memory"


def _p(path: str) -> str:
    return f"{ROOT}/{path}"


# A run's bus with the shared memory over stand-in members, called as a member from
# one of its loops: "top" is the member's own loop, any other name a nested loop. Each
# loop is an agent span named `<member>/<loop>`, and each call a tool span in it.
class _Run:
    def __init__(self, members: Sequence[str], **channel: Any) -> None:
        self.members = {name: _Member(name) for name in members}
        for m in self.members.values():
            # the own loop is the one outside any agent channel
            m.context.loop = current_agent_channel()
        self.bus = Bus(
            list(self.members.values()),
            {name: m.context for name, m in self.members.items()},
            [shared_memory(rate=None, **channel)],
        )
        self.bus.waits.start = anyio.current_time()
        self.state = cast(_SharedMemoryState, self.bus.states["shared_memory"])
        [self._memory] = self.state.tools()
        wait = self.bus.waits.tool()
        assert wait is not None
        self._wait = wait

    async def sm(
        self, member: str, command: str, loop: str = "top", **args: Any
    ) -> str:
        return await self._call(
            member, loop, "shared_memory", self._memory, {"command": command, **args}
        )

    async def wait(self, member: str, loop: str = "top", **args: Any) -> str:
        return await self._call(member, loop, "swarm_wait", self._wait, args)

    def end(self, member: str, status: str = "submitted") -> None:
        # as the runtime ends a member
        m = self.members[member]
        m.state, m.status = "ended", status
        self.bus.member_ended(member, status)

    async def _call(
        self, member: str, loop: str, name: str, tool: Tool, args: dict[str, Any]
    ) -> str:
        token = current_member.set(self.members[member].context)
        try:
            async with span("agent", type="agent", id=f"{member}/{loop}"):
                async with nullcontext() if loop == "top" else agent_channel():
                    async with span(name, type="tool"):
                        return str(await tool(**args))
        finally:
            current_member.reset(token)


@asynccontextmanager
async def run(
    members: Sequence[str] = ("w-1", "w-2"), **channel: Any
) -> AsyncGenerator[tuple[_Run, Transcript]]:
    async with sample() as transcript:
        with anyio.fail_after(10):
            yield _Run(members, **channel), transcript


class Pending:
    # a wait in flight, and its result once it has returned
    def __init__(self) -> None:
        self.result: str | None = None
        self._done = anyio.Event()

    async def value(self) -> str:
        await self._done.wait()
        assert self.result is not None
        return self.result


async def start(
    tg: TaskGroup, run: _Run, member: str, loop: str = "top", **args: Any
) -> Pending:
    # a wait, run until everything blocks: registered unless it returned
    pending = Pending()

    async def go() -> None:
        pending.result = await run.wait(member, loop, **args)
        pending._done.set()  # pyright: ignore[reportPrivateUsage]

    tg.start_soon(go)
    await anyio.wait_all_tasks_blocked()
    return pending


def waited(transcript: Transcript) -> list[dict[str, Any]]:
    return evidence(transcript, "waited")


def changed(labels: str, writers: str, first: str, count: int = 1) -> str:
    files = "1 shared memory file" if count == 1 else f"{count} shared memory files"
    return f'{files} changed where you waited ({labels}; by {writers}). View one with shared_memory(command="view", file_id="{first}").'


STOPPING = "The swarm is stopping."
ALL_WAITING = (
    "Nothing can change: every member is waiting, has ended or has not started."
)


# Waking


@pytest.mark.anyio
async def test_a_peer_write_under_path_ends_the_wait() -> None:
    async with run() as (r, transcript):
        async with anyio.create_task_group() as tg:
            # the directory does not exist yet
            pending = await start(tg, r, "w-1", path=_p("results"))
            # outside the path, and the waiter's own write, do not wake it
            await r.sm("w-2", "create", path=_p("other.md"), file_text="x")
            await r.sm("w-1", "create", path=_p("results/own.md"), file_text="x")
            await anyio.wait_all_tasks_blocked()
            assert pending.result is None
            await r.sm("w-2", "create", path=_p("results/a.md"), file_text="secret")
            assert await pending.value() == changed("f-3 new", "w-2", "f-3")
        [record] = waited(transcript)
        assert (record["outcome"], record["woken_by"], record["files"]) == (
            "news",
            [3],
            [{"file": "f-3", "version": 1, "deleted": False}],
        )
        # the wait has no status line: the next shared_memory call names what it left
        assert (await r.sm("w-1", "view", path=_p("results/a.md"))).endswith(
            "\nShared memory: 1 file changed since you last viewed it (f-1 new)."
        )


@pytest.mark.anyio
@pytest.mark.parametrize("existing", [False, True], ids=["new", "existing"])
@pytest.mark.parametrize("watched", ["results/log.md", "results"])
async def test_a_peers_append_ends_a_wait_on_its_file_or_directory(
    existing: bool, watched: str
) -> None:
    async with run() as (r, _):
        log = _p("results/log.md")
        if existing:
            await r.sm("w-2", "create", path=log, file_text="a")
            await r.sm("w-1", "view", path=log)
        async with anyio.create_task_group() as tg:
            pending = await start(tg, r, "w-1", path=_p(watched))
            await r.sm("w-2", "append", path=log, append_text="b")
            label = "f-1" if existing else "f-1 new"
            assert await pending.value() == changed(label, "w-2", "f-1")


@pytest.mark.anyio
async def test_a_subagents_write_wakes_its_members_wait() -> None:
    async with run() as (r, _):
        async with anyio.create_task_group() as tg:
            pending = await start(tg, r, "w-1")
            await r.sm("w-1", "create", "child", path=_p("a.md"), file_text="x")
            assert await pending.value() == changed("f-1 new", "w-1", "f-1")


@pytest.mark.anyio
async def test_a_file_id_follows_the_file_through_a_rename() -> None:
    async with run() as (r, transcript):
        await r.sm("w-2", "create", path=_p("a.md"), file_text="x")
        await r.sm("w-2", "create", path=_p("b.md"), file_text="x")
        await r.sm("w-1", "view", file_id="f-1")
        async with anyio.create_task_group() as tg:
            pending = await start(tg, r, "w-1", file_id="f-1")
            # another file does not wake it
            await r.sm("w-2", "append", path=_p("b.md"), append_text="y")
            await anyio.wait_all_tasks_blocked()
            assert pending.result is None
            await r.sm("w-2", "rename", old_path=_p("a.md"), new_path=_p("b/a.md"))
            assert await pending.value() == changed("f-1", "w-2", "f-1")
        [record] = waited(transcript)
        assert (record["path"], record["file_id"]) == (None, "f-1")


@pytest.mark.anyio
async def test_an_unviewed_change_returns_every_wait_at_once_until_it_is_viewed() -> (
    None
):
    async with run() as (r, transcript):
        await r.sm("w-2", "create", path=_p("a.md"), file_text="x")
        for _ in range(2):
            assert await r.wait("w-1") == changed("f-1 new", "w-2", "f-1")
        await r.sm("w-1", "view", file_id="f-1")
        async with anyio.create_task_group() as tg:
            pending = await start(tg, r, "w-1")
            assert pending.result is None
            r.bus.close()
            assert await pending.value() == STOPPING
        assert [w["outcome"] for w in waited(transcript)] == [
            "news",
            "news",
            "stopping",
        ]


@pytest.mark.anyio
async def test_status_lines_and_notices_mark_nothing_for_the_wait() -> None:
    # the same writes and calls with and without waits give the same status lines and
    # notices, and a file they named but the loop did not view still ends a wait
    async def script(with_waits: bool) -> list[str]:
        async with run() as (r, _):
            seen: list[str] = []
            await r.sm("w-2", "create", path=_p("d/a.md"), file_text="x")
            seen.append(await r.sm("w-1", "view", path=ROOT))
            if with_waits:
                assert await r.wait("w-1", path=_p("d")) == changed(
                    "f-1 new", "w-2", "f-1"
                )
            seen.extend(line.text for line in r.state.notice_lines("w-1"))
            await r.sm("w-2", "create", path=_p("d/b.md"), file_text="x")
            if with_waits:
                assert await r.wait("w-1", path=_p("d")) == changed(
                    "f-1 new, f-2 new", "w-2", "f-1", count=2
                )
            seen.extend(line.text for line in r.state.notice_lines("w-1"))
            seen.append(await r.sm("w-1", "view", path=ROOT))
            return seen

    assert await script(True) == await script(False)


@pytest.mark.anyio
async def test_eleven_files_are_named_ten_and_one_more_by_each_wait() -> None:
    async with run() as (r, transcript):
        for n in range(11):
            await r.sm("w-2", "create", path=_p(f"{n}.md"), file_text="x")
        ids = ", ".join(f"f-{n} new" for n in range(1, 11))
        expected = changed(f"{ids}, and 1 more", "w-2", "f-1", count=11)
        assert await r.wait("w-1") == expected
        assert await r.wait("w-1") == expected
        assert [len(w["files"]) for w in waited(transcript)] == [10, 10]


@pytest.mark.anyio
async def test_a_deletion_ends_a_wait_only_for_a_file_the_loop_viewed() -> None:
    async with run() as (r, _):
        await r.sm("w-2", "create", path=_p("a.md"), file_text="x")
        await r.sm("w-1", "view", file_id="f-1")
        async with anyio.create_task_group() as tg:
            pending = await start(tg, r, "w-1")
            # never seen by w-1, and gone before it could be
            await r.sm("w-2", "create", path=_p("b.md"), file_text="x")
            await r.sm("w-2", "view", file_id="f-2")
            await r.sm("w-2", "delete", path=_p("b.md"))
            await anyio.wait_all_tasks_blocked()
            assert pending.result is None
            await r.sm("w-2", "delete", path=_p("a.md"))
            assert await pending.value() == changed("f-1 deleted", "w-2", "f-1")


@pytest.mark.anyio
@pytest.mark.parametrize("gone", ["rename", "delete"])
async def test_a_release_whose_news_is_gone_at_resume_sleeps_again(gone: str) -> None:
    async with run(max_wait=1) as (r, transcript):
        async with anyio.create_task_group() as tg:
            pending = await start(tg, r, "w-1", path=_p("r"))
            # nothing in these calls yields, so the waiter resumes after both
            await r.sm("w-2", "create", path=_p("r/a.md"), file_text="x")
            if gone == "rename":
                await r.sm("w-2", "rename", old_path=_p("r/a.md"), new_path=_p("a.md"))
            else:
                await r.sm("w-2", "delete", path=_p("r/a.md"))
            assert await pending.value() == "Nothing you waited for happened in 1 s."
        [record] = waited(transcript)
        assert (record["outcome"], record["woken_by"], record["files"]) == (
            "timeout",
            [1],
            [],
        )


# Ends


@pytest.mark.anyio
async def test_an_end_wakes_every_other_members_waits_and_is_reported_once_per_loop() -> (
    None
):
    async with run(("w-1", "w-2", "w-3")) as (r, transcript):
        async with anyio.create_task_group() as tg:
            top = await start(tg, r, "w-1", path=_p("x"))
            child = await start(tg, r, "w-1", "child")
            peer = await start(tg, r, "w-2")
            r.end("w-3", "member_limit:cost")
            line = "w-3 ended (member_limit:cost)."
            for pending in (top, child, peer):
                assert await pending.value() == line
        assert [w["ended"] for w in waited(transcript)] == [
            [{"member": "w-3", "status": "member_limit:cost"}]
        ] * 3
        # reported once to each loop; a loop whose wait has not named it gets it at once
        r.end("w-2")
        assert await r.wait("w-1") == "w-2 ended (submitted)."
        assert await r.wait("w-1", "other") == (
            "w-3 ended (member_limit:cost). w-2 ended (submitted)."
        )


@pytest.mark.anyio
async def test_a_cancelled_wait_names_nothing_and_marks_no_end() -> None:
    async with run() as (r, transcript):
        async with anyio.create_task_group() as tg:
            await start(tg, r, "w-1")
            r.end("w-2")
            # cancelled after its release, before it returns
            tg.cancel_scope.cancel()
        [record] = waited(transcript)
        assert (record["outcome"], record["files"], record["ended"]) == (
            "cancelled",
            [],
            [],
        )
        assert await r.wait("w-1") == "w-2 ended (submitted)."


# Releases


@pytest.mark.anyio
@pytest.mark.parametrize(
    "others",
    [
        pytest.param({"w-2": "running"}, id="two-waiting"),
        pytest.param({}, id="lone"),
        pytest.param({"w-2": "pending", "w-3": "ended"}, id="pending-and-ended"),
    ],
)
async def test_no_member_active_releases_every_wait(others: dict[str, str]) -> None:
    async with run(("w-1", *others)) as (r, transcript):
        for name, state in others.items():
            r.members[name].state = state  # pyright: ignore[reportAttributeAccessIssue]
        async with anyio.create_task_group() as tg:
            waits = [await start(tg, r, "w-1")]
            if others.get("w-2") == "running":
                waits.append(await start(tg, r, "w-2"))
            for pending in waits:
                assert await pending.value() == ALL_WAITING
        assert {w["outcome"] for w in waited(transcript)} == {"all_waiting"}
        if "running" not in others.values():
            # a wait called in that state returns at once
            assert await r.wait("w-1") == ALL_WAITING


@pytest.mark.anyio
async def test_an_active_member_or_a_subagents_wait_keeps_the_others_waiting() -> None:
    async with run() as (r, transcript):
        async with anyio.create_task_group() as tg:
            first = await start(tg, r, "w-1")
            # w-2 is running and not in a wait of its own loop: in a long tool call, or
            # waiting in a subagent
            child = await start(tg, r, "w-2", "child")
            assert (first.result, child.result) == (None, None)
            # its own loop's wait makes it waiting
            top = await start(tg, r, "w-2")
            for pending in (first, child, top):
                assert await pending.value() == ALL_WAITING
        assert sorted(w["nested"] for w in waited(transcript)) == [False, False, True]


@pytest.mark.anyio
async def test_the_stop_releases_every_wait_and_later_waits_return_at_once() -> None:
    async with run() as (r, _):
        async with anyio.create_task_group() as tg:
            first = await start(tg, r, "w-1")
            child = await start(tg, r, "w-2", "child")
            r.bus.close()
            for pending in (first, child):
                assert await pending.value() == STOPPING
        assert await r.wait("w-1") == STOPPING


@pytest.mark.anyio
@pytest.mark.parametrize("peer_ends", [False, True])
async def test_a_stop_before_the_waiter_resumes_keeps_what_released_it(
    peer_ends: bool,
) -> None:
    async with run(("w-1", "w-2", "w-3")) as (r, transcript):
        async with anyio.create_task_group() as tg:
            pending = await start(tg, r, "w-1", path=_p("r"))
            # nothing here yields, so the waiter resumes after all of it
            await r.sm("w-2", "create", path=_p("r/a.md"), file_text="x")
            r.bus.close()
            if peer_ends:
                r.end("w-3")
            lines = [changed("f-1 new", "w-2", "f-1")]
            if peer_ends:
                lines.append("w-3 ended (submitted).")
            assert await pending.value() == "\n".join([*lines, STOPPING])
        [record] = waited(transcript)
        assert (record["outcome"], record["woken_by"], record["files"]) == (
            "stopping",
            [1],
            [{"file": "f-1", "version": 1, "deleted": False}],
        )


@pytest.mark.anyio
async def test_the_drain_releases_its_members_waits_and_later_ones_at_once() -> None:
    drained = "w-1 is ending: its own agent has finished."
    async with run(("w-1", "w-2", "w-3")) as (r, transcript):
        async with anyio.create_task_group() as tg:
            child = await start(tg, r, "w-1", "child")
            peer = await start(tg, r, "w-2")
            r.bus.member_ending("w-1")
            # before the child resumes
            r.end("w-3")
            assert await child.value() == f"w-3 ended (submitted).\n{drained}"
            # the other member's wait was released by the end alone
            assert await peer.value() == "w-3 ended (submitted)."
        assert await r.wait("w-1", "child") == drained
        # the child's waits, in order
        assert [
            (w["outcome"], w["ended"])
            for w in waited(transcript)
            if w["member"] == "w-1"
        ] == [
            ("member_ending", [{"member": "w-3", "status": "submitted"}]),
            ("member_ending", []),
        ]


@pytest.mark.anyio
@pytest.mark.parametrize("timeout", [None, 5, 0])
async def test_the_timeout_is_clamped_to_one_and_max_wait(timeout: int | None) -> None:
    async with run(max_wait=1) as (r, transcript):
        args: dict[str, Any] = {} if timeout is None else {"timeout": timeout}
        result = await r.wait("w-1", **args)
        assert result == "Nothing you waited for happened in 1 s."
        [record] = waited(transcript)
        assert record["timeout"] == {"requested": timeout, "effective": 1}
        assert record["duration"] >= 1


@pytest.mark.anyio
async def test_a_cancelled_wait_no_longer_counts_its_member_as_waiting() -> None:
    async with run() as (r, transcript):
        with anyio.CancelScope() as scope:
            async with anyio.create_task_group() as tg:
                await start(tg, r, "w-1")
                scope.cancel()
        assert [w["outcome"] for w in waited(transcript)] == ["cancelled"]
        # w-1 is running and not waiting, so w-2's wait is not released
        async with anyio.create_task_group() as tg:
            pending = await start(tg, r, "w-2")
            assert pending.result is None
            r.bus.close()


@pytest.mark.anyio
@pytest.mark.parametrize("returns", ["news", "stop", "drain"])
async def test_a_wait_called_with_a_cancellation_pending_names_nothing(
    returns: str,
) -> None:
    # the wait would return at once, but its caller is already cancelled
    async with run(("w-1", "w-2", "w-3")) as (r, transcript):
        r.end("w-3")
        if returns == "stop":
            r.bus.close()
        elif returns == "drain":
            r.bus.member_ending("w-1")
        with anyio.CancelScope() as scope:
            scope.cancel()
            await r.wait("w-1")
        [record] = waited(transcript)
        assert (record["outcome"], record["files"], record["ended"]) == (
            "cancelled",
            [],
            [],
        )
        # the end is still reported to the loop's next wait
        assert (await r.wait("w-1")).startswith("w-3 ended (submitted).")


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
@pytest.mark.parametrize("returns", ["news", "stop", "drain"])
async def test_a_wait_called_with_its_task_cancelled_names_nothing(
    returns: str,
) -> None:
    # asyncio's own Task.cancel(), which no AnyIO scope knows of
    async with run(("w-1", "w-2", "w-3")) as (r, transcript):
        r.end("w-3")
        if returns == "stop":
            r.bus.close()
        elif returns == "drain":
            r.bus.member_ending("w-1")

        async def cancelled() -> None:
            task = asyncio.current_task()
            assert task is not None
            task.cancel()
            await r.wait("w-1")

        with pytest.raises(asyncio.CancelledError):
            await asyncio.create_task(cancelled())
        [record] = waited(transcript)
        assert (record["outcome"], record["files"], record["ended"]) == (
            "cancelled",
            [],
            [],
        )
        assert (await r.wait("w-1")).startswith("w-3 ended (submitted).")


@pytest.mark.anyio
async def test_news_that_keeps_vanishing_does_not_outlast_the_timeout() -> None:
    # a peer's file moves into the watched directory and out again before each resume
    async with run(max_wait=1) as (r, _):
        await r.sm("w-2", "create", path=_p("a.md"), file_text="x")
        async with anyio.create_task_group() as tg:
            pending = await start(tg, r, "w-1", path=_p("r"))
            started = anyio.current_time()
            while pending.result is None and anyio.current_time() < started + 3:
                await r.sm("w-2", "rename", old_path=_p("a.md"), new_path=_p("r/a.md"))
                await r.sm("w-2", "rename", old_path=_p("r/a.md"), new_path=_p("a.md"))
                await anyio.sleep(0)
            if pending.result is None:
                r.bus.close()
            assert await pending.value() == "Nothing you waited for happened in 1 s."
            assert anyio.current_time() < started + 2


@pytest.mark.anyio
@pytest.mark.parametrize(
    "args,message",
    [
        pytest.param(
            {"path": "/elsewhere"},
            "Invalid path: paths are /shared_memory followed by",
            id="invalid-path",
        ),
        pytest.param(
            {"path": "f-1"},
            "f-1 is a file id, not a path; view and swarm_wait take it as file_id, and a view of the file shows its path.",
            id="file-id-as-path",
        ),
        pytest.param(
            {"file_id": "/elsewhere"},
            "Invalid file_id: file ids are f- followed by a number, such as f-3.",
            id="invalid-file_id",
        ),
        pytest.param(
            {"path": _p("a"), "file_id": "f-1"},
            "swarm_wait takes path or file_id, not both.",
            id="both",
        ),
    ],
)
async def test_refusals(args: dict[str, Any], message: str) -> None:
    async with run() as (r, transcript):
        with pytest.raises(ToolError) as refused:
            await r.wait("w-1", **args)
        assert str(refused.value).startswith(message)
        r.end("w-1")
        with pytest.raises(ToolError, match="w-1 has ended"):
            await r.wait("w-1")
        assert waited(transcript) == []


@pytest.mark.anyio
async def test_a_members_own_loop_waits_once_at_a_time() -> None:
    async with run() as (r, transcript):
        async with anyio.create_task_group() as tg:
            await start(tg, r, "w-1")
            # as an agent calling the tool from two tasks of its own loop would
            with pytest.raises(ToolError, match="w-1 is already waiting"):
                await r.wait("w-1")
            # nested loops' waits, beside each other and the own loop's
            await start(tg, r, "w-1", "child-1")
            await start(tg, r, "w-1", "child-2")
            await r.sm("w-2", "create", path=_p("a.md"), file_text="x")
        # sorted: the released waits resume in any order
        assert sorted(
            (d["wait"], d["loop"], d["outcome"]) for d in waited(transcript)
        ) == [
            ("x-1", "w-1/top", "news"),
            ("x-2", "w-1/child-1", "news"),
            ("x-3", "w-1/child-2", "news"),
        ]


@pytest.mark.anyio
async def test_a_wait_that_found_news_counts_as_the_own_loops_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with run() as (r, transcript):
        await r.sm("w-2", "create", path=_p("a.md"), file_text="x")
        # holds the first wait at the checkpoint before it returns its news
        resume = anyio.Event()

        async def held() -> None:
            await resume.wait()

        async with anyio.create_task_group() as tg:
            with monkeypatch.context() as m:
                m.setattr(anyio.lowlevel, "checkpoint", held)
                first = await start(tg, r, "w-1")
            # another task of the loop views the news, so the first wait will sleep
            # when it resumes, and then waits itself
            await r.sm("w-1", "view", path=_p("a.md"))
            with pytest.raises(ToolError, match="w-1 is already waiting"):
                await r.wait("w-1")
            resume.set()
            await anyio.wait_all_tasks_blocked()
            await r.sm("w-2", "create", path=_p("b.md"), file_text="x")
            assert "f-2 new" in await first.value()
        [data] = waited(transcript)
        assert (data["wait"], data["woken_by"]) == ("x-1", [2])


@pytest.mark.anyio
async def test_a_long_result_stays_within_a_fixed_bound() -> None:
    # the most a result can name: ten ids with markers and a count, writers, ends and
    # a release line, for names of the longest the channel takes
    names = ["a" * 48, "b" * 48, "c" * 48]
    async with run(names) as (r, _):
        a, b, c = names
        for n in range(11):
            await r.sm(b, "create", path=_p(f"{n}.md"), file_text="x")
            await r.sm(a, "view", file_id=f"f-{n + 1}")
        await r.sm(b, "delete", path=ROOT + "/0.md")
        for n in range(1, 11):
            await r.sm(b, "delete", path=_p(f"{n}.md"))
        r.end(c, "member_limit:operator")
        r.bus.close()
        result = await r.wait(a)
        assert result.count("\n") == 2
        assert ", and 1 more; by " in result
        assert utf8_bytes(result) <= 1_024


# In a running swarm


async def resolve() -> dict[str, Tool]:
    # the swarm tools of the calling loop, by name
    return {ToolDef(t).name: t for t in await swarm_tools().tools()}


@asynccontextmanager
async def nested_loop(name: str) -> AsyncGenerator[dict[str, Tool]]:
    # a loop of the member other than its own, with its swarm tools
    async with span(name, type="agent"), agent_channel():
        yield await resolve()


@agent
def tool_lister(found: dict[str, list[str]]) -> Agent:
    """Record the swarm tools of the member's own loop and of a nested loop."""

    async def execute(state: AgentState) -> AgentState:
        found["own"] = sorted(await resolve())
        async with nested_loop("child") as tools:
            found["nested"] = sorted(tools)
        return state

    return execute


@pytest.mark.anyio
@pytest.mark.parametrize(
    "channels,expected",
    [
        (["shared_memory"], ["shared_memory", "swarm_wait"]),
        (["filesystem"], []),
        ([], []),
    ],
)
async def test_swarm_wait_comes_with_a_channel_that_wakes_waits(
    channels: list[str], expected: list[str]
) -> None:
    found: dict[str, list[str]] = {}
    await run_swarm(swarm(tool_lister(found), channels=channels))
    assert found == {"own": expected, "nested": expected}


def _turn(*calls: tuple[str, dict[str, Any]]) -> ModelOutput:
    # one turn calling these tools
    output = ModelOutput.for_tool_call("mockllm", calls[0][0], calls[0][1])
    message = output.choices[0].message
    assert isinstance(message, ChatMessageAssistant)
    message.tool_calls = [
        ToolCall(id=f"call-{n}", function=function, arguments=arguments)
        for n, (function, arguments) in enumerate(calls)
    ]
    return output


def _react(
    name: str,
    *turns: ModelOutput,
    tools: Sequence[Any] = (),
    limits: Sequence[Limit] = (),
) -> Member:
    # a react member that takes these turns, then submits
    submit = ModelOutput.for_tool_call("mockllm", "submit", {"answer": "done"})
    model = scripted_model(Script([*turns, submit]))
    return member(
        react(model=model, tools=[swarm_tools(), *tools]), name=name, limits=limits
    )


def _wait_call(**args: Any) -> ModelOutput:
    return ModelOutput.for_tool_call("mockllm", "swarm_wait", args)


def _results(events: Sequence[Any], function: str) -> list[str]:
    return [
        str(e.result)
        for e in events
        if isinstance(e, ToolEvent) and e.function == function
    ]


async def _sample(solver: Agent, tmp_path: Path, **config: Any) -> EvalSample:
    # the one sample of an eval of the swarm
    [log] = await eval_async(
        Task(dataset=[Sample(input="task")], solver=solver),
        model="mockllm/model",
        log_dir=str(tmp_path),
        **config,
    )
    assert log.status == "success", log.error
    assert log.samples is not None
    return log.samples[0]


def _waited(sample: EvalSample) -> list[dict[str, Any]]:
    return [
        e.data
        for e in sample.events
        if isinstance(e, InfoEvent)
        and isinstance(e.data, dict)
        and e.data.get("kind") == "waited"
    ]


@pytest.mark.anyio
async def test_react_members_that_all_wait_are_released(tmp_path: Path) -> None:
    sample = await _sample(
        swarm(
            [_react("p", _wait_call()), _react("q", _wait_call())],
            channels=["shared_memory"],
        ),
        tmp_path,
    )
    # the second to wait releases both
    assert _results(sample.events, "swarm_wait") == [ALL_WAITING] * 2


@tool
def slow() -> Tool:
    async def execute() -> str:
        """Work for a while."""
        await anyio.sleep(0.2)
        return "done"

    return execute


@pytest.mark.anyio
async def test_a_wait_runs_alone_in_its_turn(
    tmp_path: Path,
) -> None:
    # p and q write and end before r starts; r's one turn calls slow and swarm_wait
    async def run_controller(control: SwarmControl) -> str:
        for name in ("p", "q"):
            control.start(name)
        async for _ in control.events():
            pass
        control.start("r")
        async for _ in control.events():
            pass
        return "all_done"

    def write(path: str) -> ModelOutput:
        return ModelOutput.for_tool_call(
            "mockllm",
            "shared_memory",
            {"command": "create", "path": _p(path), "file_text": "x"},
        )

    sample = await _sample(
        swarm(
            [
                _react("p", write("a.md"), write("b.md")),
                _react("q", write("c.md")),
                _react("r", _turn(("slow", {}), ("swarm_wait", {})), tools=[slow()]),
            ],
            controller=controller(run_controller, "wait_test_sequence"),
            channels=["shared_memory"],
        ),
        tmp_path,
    )
    events = sample.events
    [result] = _results(events, "swarm_wait")
    files, ends = result.split("\n")
    assert files == changed("f-1 new, f-2 new, f-3 new", "p, q", "f-1", count=3)
    # p and q end in either order
    assert ends in [
        "p ended (submitted). q ended (submitted).",
        "q ended (submitted). p ended (submitted).",
    ]
    [work] = [e for e in events if isinstance(e, ToolEvent) and e.function == "slow"]
    [wait] = [
        e for e in events if isinstance(e, ToolEvent) and e.function == "swarm_wait"
    ]
    assert work.completed is not None and work.completed <= wait.timestamp


@pytest.mark.anyio
async def test_the_time_cap_releases_a_wait(tmp_path: Path) -> None:
    sample = await _sample(
        swarm(
            [_react("p", _wait_call()), member(sleeper(), name="s")],
            channels=["shared_memory"],
            budget=Budget(time=0.3, grace=0.1),
        ),
        tmp_path,
    )
    [record] = _waited(sample)
    assert record["outcome"] == "stopping"
    assert record["duration"] < 1
    assert _results(sample.events, "swarm_wait") == [STOPPING]


@pytest.mark.anyio
async def test_a_member_time_limit_cancels_its_wait(tmp_path: Path) -> None:
    sample = await _sample(
        swarm(
            [
                _react("p", _wait_call(), limits=[time_limit(0.2)]),
                member(sleeper(0.5), name="s"),
            ],
            channels=["shared_memory"],
        ),
        tmp_path,
    )
    [record] = _waited(sample)
    assert (record["member"], record["outcome"], record["files"]) == (
        "p",
        "cancelled",
        [],
    )
