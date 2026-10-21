from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal, overload

import pytest
from inspect_ai import Task, eval  # pyright: ignore[reportUnknownVariableType]
from inspect_ai.agent import Agent, AgentState, agent
from inspect_ai.dataset import Sample
from inspect_ai.scorer import Score, Scorer, Target, scorer
from inspect_ai.solver import TaskState
from inspect_ai.tool._sandbox_tools_utils import sandbox as sandbox_tools
from inspect_ai.util import (
    ExecResult,
    SandboxEnvironment,
    SandboxEnvironmentConfigType,
    SandboxEnvironmentSpec,
    sandbox,
    sandbox_with,
    sandboxenv,
    store,
)
from inspect_ai.util._sandbox.context import (
    sandbox_default_context_var,
    sandbox_environments_context_var,
    sandbox_with_environments_context_var,
)
from pydantic import BaseModel

from inspect_swarm import Member, filesystem, member, swarm
from inspect_swarm._record import RECORD_KEY

from .swarm_runtime import evidence, run_swarm


class StubConfig(BaseModel, frozen=True):
    names: tuple[str, ...]


class StubSandbox(SandboxEnvironment):
    """A sandbox environment that keeps files in memory, one for each of its config's names."""

    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name
        self.files: dict[str, str | bytes] = {}

    async def exec(
        self,
        cmd: list[str],
        input: str | bytes | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        user: str | None = None,
        timeout: int | None = None,
        timeout_retry: bool = True,
        concurrency: bool = True,
    ) -> ExecResult[str]:
        return ExecResult(success=False, returncode=1, stdout="", stderr="stub")

    async def write_file(self, file: str, contents: str | bytes) -> None:
        self.files[file] = contents

    @overload
    async def read_file(self, file: str, text: Literal[True] = True) -> str: ...

    @overload
    async def read_file(self, file: str, text: Literal[False]) -> bytes: ...

    async def read_file(self, file: str, text: bool = True) -> str | bytes:
        if file not in self.files:
            raise FileNotFoundError(file)
        return self.files[file]

    @classmethod
    async def sample_init(
        cls,
        task_name: str,
        config: SandboxEnvironmentConfigType | None,
        metadata: dict[str, str],
    ) -> dict[str, SandboxEnvironment]:
        assert isinstance(config, StubConfig)
        return {name: cls(name) for name in config.names}

    @classmethod
    def config_deserialize(cls, config: dict[str, Any]) -> BaseModel:
        return StubConfig.model_validate(config)

    @classmethod
    async def sample_cleanup(
        cls,
        task_name: str,
        config: SandboxEnvironmentConfigType | None,
        environments: dict[str, SandboxEnvironment],
        interrupted: bool,
    ) -> None:
        pass


sandboxenv(name="swarm_test_stub")(StubSandbox)


def _name(environment: SandboxEnvironment | None) -> str:
    return "none" if environment is None else environment.as_type(StubSandbox).name


@contextmanager
def environments(*names: str) -> Generator[dict[str, StubSandbox], None, None]:
    """A sample's stub sandbox environments, the first its default."""
    stubs = {name: StubSandbox(name) for name in names}
    tokens = (
        sandbox_environments_context_var.set(dict(stubs)),
        sandbox_with_environments_context_var.set({}),
        sandbox_default_context_var.set(names[0] if names else "default"),
    )
    try:
        yield stubs
    finally:
        sandbox_default_context_var.reset(tokens[2])
        sandbox_with_environments_context_var.reset(tokens[1])
        sandbox_environments_context_var.reset(tokens[0])


def _member_name(state: AgentState) -> str:
    # from the preamble, the last input message
    return state.messages[-1].text.split(",")[0].removeprefix("You are ")


@agent
def prober(seen: dict[str, dict[str, str]], names: list[str]) -> Agent:
    """Record the member's preamble, its default sandbox, which of `names` it reaches and where `sandbox_with()` finds `marker`."""

    async def execute(state: AgentState) -> AgentState:
        reach = {"preamble": state.messages[-1].text, "sandbox()": _name(sandbox())}
        for name in names:
            try:
                reach[name] = _name(sandbox(name))
            except ValueError:
                reach[name] = "refused"
        reach["marker"] = _name(await sandbox_with("marker"))
        seen[_member_name(state)] = reach
        return state

    return execute


@agent
def idle() -> Agent:
    async def execute(state: AgentState) -> AgentState:
        return state

    return execute


_NAMES = ["agent-1", "agent-2", "comparator", "default"]


def _probers(seen: dict[str, dict[str, str]], **kwargs: Any) -> Member:
    return member(
        prober,
        args={"seen": seen, "names": _NAMES},
        name="agent",
        count=2,
        sandbox="{member}",
        **kwargs,
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "visible,comparator", [((), "refused"), (("comparator",), "comparator")]
)
async def test_members_reach_only_their_own_sandbox_and_the_visible_ones(
    visible: Sequence[str], comparator: str
) -> None:
    seen: dict[str, dict[str, str]] = {}
    with environments("default", "agent-1", "agent-2", "comparator") as stubs:
        # found before the swarm, as by a setup step: no member may be handed it
        stubs["agent-2"].files["marker"] = "x"
        assert _name(await sandbox_with("marker")) == "agent-2"
        outcome = await run_swarm(
            swarm(_probers(seen, visible_sandboxes=visible), channels=[])
        )
    for name, other in (("agent-1", "agent-2"), ("agent-2", "agent-1")):
        reach = seen[name]
        # "default" is the member's own sandbox too
        assert reach["sandbox()"] == reach["default"] == reach[name] == name
        assert reach[other] == "refused"
        assert reach["comparator"] == comparator
    assert seen["agent-1"]["marker"] == "none"
    assert seen["agent-2"]["marker"] == "agent-2"
    assert [(m.name, m.sandbox) for m in outcome.record.members] == [
        ("agent-1", "agent-1"),
        ("agent-2", "agent-2"),
    ]
    assert sorted(
        (e["member"], e["sandbox"])
        for e in evidence(outcome.transcript, "member_result")
    ) == [("agent-1", "agent-1"), ("agent-2", "agent-2")]


@agent
def tools_prober(targets: dict[str, tuple[str, str]]) -> Agent:
    """Record where Inspect's injected tools run without a sandbox name (as `text_editor()`, `bash_session()`, sandbox MCP servers and the agent bridge ask) and with the visible service's name."""

    async def execute(state: AgentState) -> AgentState:
        unnamed = await sandbox_tools.sandbox_with_injected_tools()
        named = await sandbox_tools.sandbox_with_injected_tools(
            sandbox_name="comparator"
        )
        targets[_member_name(state)] = (_name(unnamed), _name(named))
        return state

    return execute


@pytest.mark.anyio
async def test_injected_tools_without_a_name_stay_in_the_members_own_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Inspect's tools are installed only in the visible service, which Inspect would
    # otherwise pick as the environment needing no injection
    installed = {"comparator"}
    injected: list[str] = []

    async def has_tools(environment: SandboxEnvironment) -> bool:
        return _name(environment) in installed

    async def inject(environment: SandboxEnvironment) -> None:
        injected.append(_name(environment))
        installed.add(_name(environment))

    monkeypatch.setattr(sandbox_tools, "_sandbox_tools_installed", has_tools)
    monkeypatch.setattr(sandbox_tools, "_inject_container_tools_code", inject)
    targets: dict[str, tuple[str, str]] = {}
    with environments("default", "agent-1", "agent-2", "comparator"):
        await run_swarm(
            swarm(
                member(
                    tools_prober,
                    args={"targets": targets},
                    name="agent",
                    count=2,
                    sandbox="{member}",
                    visible_sandboxes=["comparator"],
                ),
                channels=[],
            )
        )
    assert targets == {
        "agent-1": ("agent-1", "comparator"),
        "agent-2": ("agent-2", "comparator"),
    }
    assert sorted(injected) == ["agent-1", "agent-2"]


@pytest.mark.anyio
async def test_the_preamble_says_whether_members_share_a_sandbox() -> None:
    own = "You work in your own sandbox, which no other member can reach."
    seen: dict[str, dict[str, str]] = {}
    with environments("default", "agent-1", "agent-2"):
        await run_swarm(swarm(_probers(seen), channels=[]))
        assert all(own in reach["preamble"] for reach in seen.values())
        seen.clear()
        shared = await run_swarm(
            swarm(
                member(prober, args={"seen": seen, "names": []}, name="agent", count=2),
                channels=[],
            )
        )
    assert seen and not any(own in reach["preamble"] for reach in seen.values())
    assert [m.sandbox for m in shared.record.members] == [None, None]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "channel,shared,notes",
    [
        (filesystem(shared_volume=True), "/shared", "/shared/notes.md"),
        (
            filesystem(shared="/mnt/team", notes="log.md", shared_volume=True),
            "/mnt/team",
            "/mnt/team/log.md",
        ),
    ],
)
async def test_the_filesystem_channel_describes_a_shared_volume_beside_own_sandboxes(
    channel: Any, shared: str, notes: str
) -> None:
    seen: dict[str, dict[str, str]] = {}
    with environments("default", "agent-1", "agent-2"):
        await run_swarm(swarm(_probers(seen), channels=[channel]))
    assert sorted(seen) == ["agent-1", "agent-2"]
    for reach in seen.values():
        assert reach["preamble"].endswith(
            "You work in your own sandbox, which no other member can reach, except for the shared directory described below.\n\n"
            f"{shared} is the same directory in every member's sandbox. "
            "Share files with the other members there. They use it at the same time as you, so files they change there can change under you. "
            f"Read {notes} before you start and again as you work, and add what you learn or decide to the end of it, without changing what others wrote."
        )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "names,visible,missing",
    [
        (
            ("default", "agent-1"),
            (),
            "member 'agent-2' names the sandbox 'agent-2'",
        ),
        (
            ("default", "agent-1", "agent-2"),
            ("comparator",),
            "member 'agent-1' names the sandbox 'comparator'",
        ),
        ((), (), "The sample's sandboxes are: none"),
    ],
)
async def test_a_sandbox_the_sample_lacks_is_refused_at_swarm_start(
    names: tuple[str, ...], visible: tuple[str, ...], missing: str
) -> None:
    seen: dict[str, dict[str, str]] = {}
    members = _probers(seen, visible_sandboxes=visible)
    with environments(*names):
        with pytest.raises(ValueError, match=missing):
            await run_swarm(swarm(members, channels=[]))
    # before any member started
    assert seen == {}


@pytest.mark.parametrize(
    "members,channels,match",
    [
        (
            lambda: member(idle, count=2, sandbox="{member}"),
            ["filesystem"],
            r"the filesystem channel describes a sandbox the members share, .* use filesystem\(shared_volume=True\)",
        ),
        (
            lambda: member(idle, count=2),
            [filesystem(shared_volume=True)],
            r"filesystem\(shared_volume=True\) describes members with their own sandboxes",
        ),
        (
            lambda: [member(idle, name="a", sandbox="a"), member(idle, name="b")],
            [],
            "member 'a' has its own sandbox and 'b' does not",
        ),
        (
            lambda: member(idle, count=2, sandbox="box"),
            [],
            "members 'idle-1' and 'idle-2' both have the sandbox 'box'",
        ),
        (
            lambda: [
                member(idle, name="a", sandbox="a", visible_sandboxes=["b"]),
                member(idle, name="b", sandbox="b"),
            ],
            [],
            "which is the sandbox of member 'b'",
        ),
    ],
)
def test_swarm_refuses_rosters_whose_members_do_not_each_have_their_own_sandbox(
    members: Callable[[], Any], channels: list[Any], match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        swarm(members(), channels=channels)


@pytest.mark.parametrize(
    "kwargs,match",
    [
        ({"sandbox": ""}, "non-empty sandbox name"),
        ({"sandbox": "agent-{i}"}, "no other braces"),
        ({"sandbox": "{member}-{{x}}"}, "no other braces"),
        ({"visible_sandboxes": ["comparator"]}, "give the member sandbox= as well"),
        (
            {"sandbox": "a", "visible_sandboxes": ["default"]},
            "cannot name 'default'",
        ),
        ({"sandbox": "a", "visible_sandboxes": [""]}, "cannot have an empty name"),
        ({"sandbox": "a", "visible_sandboxes": "comparator"}, "valid list"),
    ],
)
def test_member_refuses_bad_sandbox_names(kwargs: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        member(idle, **kwargs)


@agent
def worker() -> Agent:
    """Leave the member's name in its own sandbox."""

    async def execute(state: AgentState) -> AgentState:
        await sandbox().write_file("work.txt", _member_name(state))
        return state

    return execute


@scorer(metrics=[])
def members_work() -> Scorer:
    """Read each member's work from the sandbox the result record names for it."""

    async def score(state: TaskState, target: Target) -> Score:
        work = {
            m["name"]: await sandbox(m["sandbox"]).read_file("work.txt")
            for m in store().get(RECORD_KEY)["members"]
        }
        return Score(value=1, metadata={"default": _name(sandbox()), "work": work})

    return score


def test_a_scorer_after_the_swarm_sees_default_and_names_each_members_sandbox(
    tmp_path: Path,
) -> None:
    [log] = eval(
        Task(
            dataset=[Sample(input="x")],
            solver=swarm(
                member(worker, name="agent", count=2, sandbox="{member}"),
                channels=[],
            ),
            scorer=members_work(),
            sandbox=SandboxEnvironmentSpec(
                "swarm_test_stub", StubConfig(names=("default", "agent-1", "agent-2"))
            ),
        ),
        model="mockllm/model",
        log_dir=str(tmp_path),
        display="none",
    )
    assert log.status == "success", log.error
    assert log.samples is not None and log.samples[0].scores is not None
    [score] = log.samples[0].scores.values()
    assert score.metadata == {
        "default": "default",
        "work": {"agent-1": "agent-1", "agent-2": "agent-2"},
    }


_COMPOSE = """\
services:
  agent-1:
    image: alpine:latest
    command: tail -f /dev/null
    network_mode: none
    x-default: true
  agent-2:
    image: alpine:latest
    command: tail -f /dev/null
    network_mode: none
"""


@agent
def docker_worker() -> Agent:
    """Leave the member's name in /tmp/who in its own container, and check that the other container is out of reach."""

    async def execute(state: AgentState) -> AgentState:
        name = _member_name(state)
        other = "agent-2" if name == "agent-1" else "agent-1"
        with pytest.raises(ValueError):
            sandbox(other)
        result = await sandbox().exec(["sh", "-c", f"echo {name} > /tmp/who"])
        assert result.success, result.stderr
        return state

    return execute


@scorer(metrics=[])
def docker_work() -> Scorer:
    async def score(state: TaskState, target: Target) -> Score:
        work = {
            name: (await sandbox(name).read_file("/tmp/who")).strip()
            for name in ("agent-1", "agent-2")
        }
        return Score(value=1, metadata=work)

    return score


@pytest.mark.slow
def test_two_members_in_two_docker_services(tmp_path: Path) -> None:
    compose = tmp_path / "compose.yaml"
    compose.write_text(_COMPOSE)
    [log] = eval(
        Task(
            dataset=[Sample(input="x")],
            solver=swarm(
                member(docker_worker, name="agent", count=2, sandbox="{member}"),
                channels=[],
            ),
            scorer=docker_work(),
            sandbox=("docker", str(compose)),
        ),
        model="mockllm/model",
        log_dir=str(tmp_path / "logs"),
        display="none",
    )
    assert log.status == "success", log.error
    assert log.samples is not None and log.samples[0].scores is not None
    [score] = log.samples[0].scores.values()
    assert score.metadata == {"agent-1": "agent-1", "agent-2": "agent-2"}
