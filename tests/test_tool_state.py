import re
from pathlib import Path
from typing import Any

import pytest
from inspect_ai import Task, eval_async  # pyright: ignore[reportUnknownVariableType]
from inspect_ai.agent import Agent, AgentState, agent, deepagent, general
from inspect_ai.dataset import Sample
from inspect_ai.log import EvalLog
from inspect_ai.model import ChatMessage, ChatMessageTool, ChatMessageUser, ModelOutput
from inspect_ai.tool import Skill, Tool, ToolDef
from inspect_ai.tool._tools import _bash_session as bash_session_module
from inspect_ai.tool._tools._web_browser import _web_browser as web_browser_module
from inspect_ai.util import tool_state_instance

from inspect_swarm import member, swarm

from .swarm_runtime import Script, sample, scripted_model


# A deepagent member's conversation, scripted: the parent's tool calls in order, then
# its child's when the parent dispatches the `general` subagent with "child of <name>".
# Every tool result each conversation receives is kept, by speaker and step.
class _Plan:
    def __init__(
        self,
        parent: list[tuple[str, dict[str, Any]]],
        child: list[tuple[str, dict[str, Any]]],
    ) -> None:
        self.parent = parent
        self.child = child
        self.results: dict[tuple[str, int], str] = {}

    def __call__(self, input: list[ChatMessage]) -> ModelOutput:
        users = "\n".join(m.text for m in input if isinstance(m, ChatMessageUser))
        child = re.search(r"child of (\S+)", users)
        if child is not None:
            name, speaker, plan = (
                child.group(1),
                f"child of {child.group(1)}",
                self.child,
            )
        else:
            found = re.search(r"You are (\S+),", users)
            assert found is not None
            name = found.group(1)
            speaker, plan = name, self.parent
        results = [m for m in input if isinstance(m, ChatMessageTool)]
        step = len(results)
        if results:
            self.results[(speaker, step)] = results[-1].text
        if step < len(plan):
            function, arguments = plan[step]
            return ModelOutput.for_tool_call(
                "mockllm",
                function,
                {
                    k: v.format(name=name) if isinstance(v, str) else v
                    for k, v in arguments.items()
                },
            )
        return ModelOutput.for_tool_call("mockllm", "submit", {"answer": "done"})


@pytest.mark.anyio
async def test_deepagent_copies_have_their_own_memory_shared_with_a_readwrite_subagent(
    tmp_path: Path,
) -> None:
    plan = _Plan(
        parent=[
            (
                "memory",
                {"command": "create", "path": "/memories/{name}.md", "file_text": "x"},
            ),
            ("agent", {"prompt": "child of {name}"}),
            ("memory", {"command": "view", "path": "/memories"}),
        ],
        child=[("memory", {"command": "view", "path": "/memories"})],
    )
    model = scripted_model(Script(respond=plan))
    await _eval(
        swarm(
            member(
                deepagent,
                args={
                    "model": model,
                    "subagents": [general(memory="readwrite")],
                    "todo_write": False,
                },
                count=2,
            ),
            channels=[],
        ),
        tmp_path,
    )
    for name, other in (("deepagent-1", "deepagent-2"), ("deepagent-2", "deepagent-1")):
        # the parent's own view, after its child's, and the readwrite child's view
        for listing in (plan.results[(name, 3)], plan.results[(f"child of {name}", 1)]):
            assert f"{name}.md" in listing and f"{other}.md" not in listing


@pytest.mark.anyio
async def test_deepagent_members_general_subagents_have_their_own_installed_skills(
    tmp_path: Path,
) -> None:
    plan = _Plan(
        parent=[("agent", {"prompt": "child of {name}"})],
        child=[("skill", {"command": "example"})],
    )
    model = scripted_model(Script(respond=plan))
    example = Skill(name="example", description="Example.", instructions="Do it.")
    log = await _eval(
        swarm(
            member(
                deepagent,
                args={
                    "model": model,
                    "subagents": [general()],
                    "skills": [example],
                    "todo_write": False,
                },
                count=2,
            ),
            channels=[],
        ),
        tmp_path,
        sandbox="local",
    )
    assert log.samples is not None
    keys = {
        k
        for k in log.samples[0].store
        if k.startswith("InstalledSkills:") and k.endswith(":skills")
    }
    # each member's subagent installed the skill under its own private instance
    assert keys == {
        "InstalledSkills:@deepagent-1#general:skills",
        "InstalledSkills:@deepagent-2#general:skills",
    }
    for name in ("deepagent-1", "deepagent-2"):
        assert "Do it." in plan.results[(f"child of {name}", 1)]


async def _eval(solver: Agent, tmp_path: Path, sandbox: str | None = None) -> EvalLog:
    # deepagent needs a running sample (its loop checkpoints)
    [log] = await eval_async(
        Task(dataset=[Sample(input="task")], solver=solver, sandbox=sandbox),
        model="mockllm/model",
        log_dir=str(tmp_path),
    )
    assert log.status == "success", log.error
    return log


@agent
def session_user(tool: Tool, arguments: dict[str, Any]) -> Agent:
    # calls the tool twice, as a member using its shell or browser would
    async def execute(state: AgentState) -> AgentState:
        for _ in range(2):
            await tool(**arguments)
        return state

    return execute


class _FakeTransport:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass


@pytest.mark.anyio
async def test_members_get_their_own_bash_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    new_sessions: list[str] = []
    used: list[tuple[str | None, object]] = []

    class FakeSandbox:
        _tools_user = None
        _tools_default_user = None

    async def fake_sandbox_with_injected_tools() -> FakeSandbox:
        return FakeSandbox()

    async def fake_exec_model_request(*args: Any, **kwargs: Any) -> Any:
        new_sessions.append(f"session-{len(new_sessions) + 1}")
        return bash_session_module.NewSessionResult(session_name=new_sessions[-1])

    async def fake_exec_scalar_request(*args: Any, **kwargs: Any) -> str:
        used.append((tool_state_instance(None), kwargs["params"]["session_name"]))
        return "ok"

    monkeypatch.setattr(
        bash_session_module,
        "sandbox_with_injected_tools",
        fake_sandbox_with_injected_tools,
    )
    monkeypatch.setattr(bash_session_module, "SandboxJSONRPCTransport", _FakeTransport)
    monkeypatch.setattr(
        bash_session_module, "exec_model_request", fake_exec_model_request
    )
    monkeypatch.setattr(
        bash_session_module, "exec_scalar_request", fake_exec_scalar_request
    )

    shell = bash_session_module.bash_session()
    async with sample():
        await swarm(
            member(
                session_user,
                args={
                    "tool": shell,
                    "arguments": {"action": "type_submit", "input": "ls"},
                },
                count=2,
            ),
            channels=[],
        )(AgentState(messages=[ChatMessageUser(content="task")]))
    _assert_one_session_per_member(new_sessions, used)


@pytest.mark.anyio
async def test_members_get_their_own_web_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    new_sessions: list[str] = []
    used: list[tuple[str | None, object]] = []

    async def fake_legacy_tool_support_sandbox(*args: Any) -> tuple[object, None]:
        return (object(), None)

    async def fake_exec_model_request(method: str, **kwargs: Any) -> Any:
        if method == "web_new_session":
            new_sessions.append(f"session-{len(new_sessions) + 1}")
            return web_browser_module.NewSessionResult(session_name=new_sessions[-1])
        session = kwargs["params"]["session_name"]
        used.append((tool_state_instance(None), session))
        return web_browser_module.CrawlerResult(
            web_url="https://example.com", web_at="[1] RootWebArea"
        )

    monkeypatch.setattr(
        web_browser_module,
        "legacy_tool_support_sandbox",
        fake_legacy_tool_support_sandbox,
    )
    monkeypatch.setattr(web_browser_module, "SandboxJSONRPCTransport", _FakeTransport)
    monkeypatch.setattr(
        web_browser_module, "exec_model_request", fake_exec_model_request
    )

    [go] = [t for t in web_browser_module.web_browser() if "go" in _tool_name(t)]
    async with sample():
        await swarm(
            member(
                session_user,
                args={"tool": go, "arguments": {"url": "https://example.com"}},
                count=2,
            ),
            channels=[],
        )(AgentState(messages=[ChatMessageUser(content="task")]))
    _assert_one_session_per_member(new_sessions, used)


def _tool_name(tool: Tool) -> str:
    return ToolDef(tool).name


def _assert_one_session_per_member(
    new_sessions: list[str], used: list[tuple[str | None, object]]
) -> None:
    # one session per member, reused for its second call
    assert len(new_sessions) == 2
    by_member: dict[str | None, set[object]] = {}
    for scope, session in used:
        by_member.setdefault(scope, set()).add(session)
    assert sorted(by_member, key=str) == ["@session_user-1", "@session_user-2"]
    assert [len(sessions) for sessions in by_member.values()] == [1, 1]
    assert {s for sessions in by_member.values() for s in sessions} == set(new_sessions)
