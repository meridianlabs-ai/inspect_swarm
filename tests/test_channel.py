from pathlib import Path
from typing import Any

import pytest
from inspect_ai import Task, eval  # pyright: ignore[reportUnknownVariableType]
from inspect_ai.agent import Agent, AgentState, agent
from inspect_ai.dataset import Sample
from inspect_ai.util import sandbox

from inspect_swarm import (
    Channel,
    Controller,
    MemberInfo,
    StopReason,
    SwarmControl,
    filesystem,
    member,
    swarm,
    swarm_channel,
    swarm_controller,
)

from .swarm_runtime import run_swarm


@agent
def preamble_recorder(preambles: dict[str, str]) -> Agent:
    async def execute(state: AgentState) -> AgentState:
        preamble = state.messages[-1].text
        name = preamble.split(",")[0].removeprefix("You are ")
        preambles[name] = preamble
        return state

    return execute


class _Locks(Channel):
    def instructions(self, member: MemberInfo) -> str | None:
        return f"Take a lock in /shared/locks before editing a file, {member.name}."


@swarm_channel
def channel_test_locks() -> Channel:
    return _Locks()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "channels,expected",
    [
        (
            ("filesystem",),
            [
                "Share files with them in /shared.",
                "Read /shared/notes.md",
                "Keep your own work in /scratch/{member},",
            ],
        ),
        (
            [filesystem(shared="/work", notes="/work/log.md", scratch="/tmp/{member}")],
            [
                "Share files with them in /work.",
                "Read /work/log.md",
                "Keep your own work in /tmp/{member},",
            ],
        ),
        (
            [channel_test_locks()],
            ["Take a lock in /shared/locks before editing a file, {member}."],
        ),
        ([], []),
    ],
)
async def test_each_member_gets_each_channels_instructions(
    channels: Any, expected: list[str]
) -> None:
    preambles: dict[str, str] = {}
    await run_swarm(
        swarm(
            member(
                preamble_recorder,
                args={"preambles": preambles},
                name="w",
                role="tester",
                count=2,
            ),
            channels=channels,
        )
    )
    for name in ("w-1", "w-2"):
        intro = f"You are {name}, one of 2 agents in a swarm working on this task at the same time.\n\nYour role: tester."
        assert preambles[name].startswith(intro)
        rest = preambles[name].removeprefix(intro)
        for text in expected:
            assert text.format(member=name) in rest
        if not expected:
            assert rest == ""


@pytest.mark.parametrize(
    "scratch",
    [
        "/scratch/{name}",
        "/scratch/{member[0]}",
        "/scratch/{member.upper}",
        "/scratch/{member!r}",
        "/scratch/{member:>9}",
        "/scratch/{}",
        "/scratch/{member",
        # no placeholder: every member would be given the same directory
        "/scratch",
        "/scratch/{{member}}",
    ],
)
def test_filesystem_refuses_a_scratch_template_without_exactly_the_member_placeholder(
    scratch: str,
) -> None:
    with pytest.raises(ValueError, match="must use the {member} placeholder"):
        filesystem(scratch=scratch)


def test_filesystem_refuses_a_scratch_with_a_shared_volume() -> None:
    with pytest.raises(ValueError, match="scratch is not used with shared_volume=True"):
        filesystem(scratch="/tmp/{member}", shared_volume=True)


_OUTSIDE = "notes must be inside shared"
_RELATIVE = "shared must be an absolute path"


@pytest.mark.parametrize(
    "shared,notes,shared_volume,refused",
    [
        ("/shared", "notes.md", True, None),
        ("/shared", "team/notes.md", True, None),
        ("/shared", "/shared/notes.md", True, None),
        ("/shared", "../notes.md", True, _OUTSIDE),
        ("/shared", "/tmp/notes.md", True, _OUTSIDE),
        ("/shared", ".", True, _OUTSIDE),
        # "../../notes.md" would start with "../"
        ("..", "../notes.md", True, _RELATIVE),
        ("team", "notes.md", True, _RELATIVE),
        # without a shared volume the whole sandbox is shared
        ("/shared", "/tmp/notes.md", False, None),
        ("team", "notes.md", False, None),
    ],
)
def test_filesystem_with_a_shared_volume_refuses_paths_outside_it(
    shared: str, notes: str, shared_volume: bool, refused: str | None
) -> None:
    if refused is None:
        filesystem(shared=shared, notes=notes, shared_volume=shared_volume)
    else:
        with pytest.raises(ValueError, match=refused):
            filesystem(shared=shared, notes=notes, shared_volume=shared_volume)


@agent
def notes_writer(notes: str) -> Agent:
    async def execute(state: AgentState) -> AgentState:
        await sandbox().write_file(notes, "the answer is near\n")
        return state

    return execute


@agent
def notes_reader(notes: str, read: list[str]) -> Agent:
    async def execute(state: AgentState) -> AgentState:
        read.append(await sandbox().read_file(notes))
        return state

    return execute


@swarm_controller
def writer_first() -> Controller:
    async def run(control: SwarmControl) -> StopReason:
        for name in ("writer", "reader"):
            control.start(name)
            async for _ in control.events():
                pass
        return "all_done"

    return Controller(run)


def test_members_share_the_samples_sandbox(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    notes = str(shared / "notes.md")
    read: list[str] = []
    [log] = eval(
        Task(
            dataset=[Sample(input="x")],
            solver=swarm(
                [
                    member(notes_writer(notes), name="writer"),
                    member(notes_reader(notes, read), name="reader"),
                ],
                controller=writer_first(),
                channels=[filesystem(shared=str(shared))],
            ),
            sandbox="local",
        ),
        model="mockllm/model",
        log_dir=str(tmp_path / "logs"),
        display="none",
    )
    assert log.status == "success"
    assert read == ["the answer is near\n"]
