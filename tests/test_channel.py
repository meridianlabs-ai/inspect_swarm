import re
from pathlib import Path
from typing import Any

import pytest
from inspect_ai import Task, eval  # pyright: ignore[reportUnknownVariableType]
from inspect_ai._util.registry import registry_info
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
from inspect_swarm._budget import current_member
from inspect_swarm._channel.filesystem import FilesystemChannel

from .swarm_runtime import run_swarm
from .test_bus import bus_test_locks


@agent
def preamble_recorder(preambles: dict[str, str]) -> Agent:
    async def execute(state: AgentState) -> AgentState:
        context = current_member.get()
        assert context is not None
        preambles[context.name] = state.messages[-1].text
        return state

    return execute


class _Locks(Channel):
    def instructions(self, member: MemberInfo) -> str | None:
        return f"Take a lock in /shared/locks before editing a file, {member.name}."


@swarm_channel
def channel_test_locks() -> Channel:
    return _Locks()


class _Filesystem(FilesystemChannel):
    pass


@swarm_channel
def channel_test_filesystem() -> Channel:
    return _Filesystem("/shared", "notes.md", "/scratch/{member}", False)


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


def _choose(size: int) -> str:
    # the first paragraph of a preamble for a member that chooses its own name
    return (
        f"You are one of {size} agents in a swarm working on this task at the same time. Choose a name for yourself and tell the other members what it is."
        if size > 1
        else "You are the only agent in this swarm. Choose a name for yourself."
    )


@pytest.mark.anyio
@pytest.mark.parametrize("count", [2, 1])
@pytest.mark.parametrize("role", [None, "tester"])
@pytest.mark.parametrize(
    "channels,scratch",
    [
        (("filesystem",), "/scratch/<your name>"),
        ([filesystem(scratch="/tmp/{member}/work")], "/tmp/<your name>/work"),
        ([], None),
    ],
)
async def test_a_member_that_chooses_its_name_is_told_no_members_name(
    count: int, role: str | None, channels: Any, scratch: str | None
) -> None:
    preambles: dict[str, str] = {}
    await run_swarm(
        swarm(
            member(
                preamble_recorder,
                args={"preambles": preambles},
                name="zeta",
                role=role,
                count=count,
                choose_name=True,
            ),
            channels=channels,
        )
    )
    assert len(preambles) == count
    intro = _choose(count) + ("" if role is None else f"\n\nYour role: {role}.")
    for preamble in preambles.values():
        assert preamble.startswith(intro)
        assert "zeta" not in preamble
        if scratch is None:
            assert preamble == intro
        else:
            assert preamble.endswith(
                f"Keep your own work in {scratch}, a directory named with the name you chose; check that no other member uses the same one."
            )


@pytest.mark.anyio
async def test_named_members_beside_members_that_choose_are_told_their_directory_may_be_taken() -> (
    None
):
    preambles: dict[str, str] = {}
    await run_swarm(
        swarm(
            [
                member(preamble_recorder, args={"preambles": preambles}, name="lead"),
                member(
                    preamble_recorder,
                    args={"preambles": preambles},
                    name="zeta",
                    count=2,
                    choose_name=True,
                ),
            ]
        )
    )
    lead = preambles.pop("lead")
    assert lead.startswith(
        "You are lead, one of 3 agents in a swarm working on this task at the same time."
    )
    assert lead.endswith(
        "Keep your own work in /scratch/lead; some other members choose their own names and name their directories after them, so check that none of them uses the same one."
    )
    assert "which no other member uses" not in lead
    assert sorted(preambles) == ["zeta-1", "zeta-2"]
    for preamble in preambles.values():
        assert preamble.startswith(_choose(3))
        assert "zeta" not in preamble and "lead" not in preamble
        assert preamble.endswith(
            "Keep your own work in /scratch/<your name>, a directory named with the name you chose; check that no other member uses the same one."
        )


@pytest.mark.parametrize(
    "channel",
    [channel_test_locks, bus_test_locks, channel_test_filesystem],
)
def test_swarm_refuses_other_channels_beside_a_member_that_chooses_its_name(
    channel: Any,
) -> None:
    channels = [filesystem(), channel()]
    swarm(member(preamble_recorder, args={"preambles": {}}, count=2), channels=channels)
    name = registry_info(channels[1]).name
    with pytest.raises(
        ValueError,
        match=re.escape(
            f"member 'preamble_recorder-1' chooses its own name (choose_name=True), which the swarm supports only with the filesystem channel or channels=[]; {name} is not supported with it."
        ),
    ):
        swarm(
            member(
                preamble_recorder, args={"preambles": {}}, count=2, choose_name=True
            ),
            channels=channels,
        )


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
