from collections.abc import Callable
from typing import Any

import pytest
from inspect_ai import Task
from inspect_ai._eval.evalset import EvalSetArgsInTaskIdentifier, task_identifier
from inspect_ai._eval.loader import resolve_tasks
from inspect_ai._eval.task.log import plan_to_eval_plan
from inspect_ai._util.registry import (
    create_registry_object,
    registry_info,
    registry_kwargs,
    registry_params,
)
from inspect_ai.agent import Agent, agent, as_solver, deepagent, react
from inspect_ai.dataset import Sample
from inspect_ai.model import GenerateConfig, get_model
from inspect_ai.solver import Plan
from inspect_ai.tool import Tool, bash, python
from inspect_ai.util import (
    cost_limit,
    message_limit,
    time_limit,
    token_limit,
    turn_limit,
)

from inspect_swarm import (
    Controller,
    Member,
    StopReason,
    SwarmControl,
    member,
    swarm,
    swarm_controller,
)
from inspect_swarm._member import build_member


async def _run(control: SwarmControl) -> StopReason:
    return "all_done"


@swarm_controller
def log_test_controller() -> Controller:
    return Controller(_run)


def _never_built() -> Agent:
    raise AssertionError("a factory-form member was built outside a sample")


# a factory in a package keeps its namespace in the log
_never_built.__module__ = "inspect_swarm._member"
never_built = agent(name="log_test_never_built")(_never_built)

CONTROLLER: dict[str, Any] = {
    "type": "swarm_controller",
    "name": "log_test_controller",
    "params": {},
}


def _swarm(members: Any) -> Agent:
    return swarm(members, controller=log_test_controller(), channels=[])


def _record(**fields: Any) -> dict[str, Any]:
    return {"name": None, "role": None, "count": 1, "limits": [], **fields}


def _agent_dict(name: str, **params: Any) -> dict[str, Any]:
    return {"type": "agent", "name": name, "params": params}


def _agent_tool(name: str) -> dict[str, Any]:
    return {"type": "tool", "name": name, "params": {}}


@pytest.mark.parametrize(
    "members,logged",
    [
        pytest.param(
            lambda: react(prompt="a"),
            _agent_dict("react", prompt="a"),
            id="bare-agent",
        ),
        pytest.param(
            lambda: [react(prompt="a"), deepagent(todo_write=False)],
            [
                _agent_dict("react", prompt="a"),
                _agent_dict("deepagent", todo_write=False),
            ],
            id="list-of-agents",
        ),
        pytest.param(
            lambda: member(never_built, name="w", count=4),
            _record(
                factory="inspect_swarm/log_test_never_built", args={}, name="w", count=4
            ),
            id="single-record",
        ),
        pytest.param(
            lambda: [
                react(prompt="a"),
                member(deepagent, args={"tools": [bash(), python()]}, count=2),
                member(react(prompt="b"), name="lead", role="coordinator"),
                member(never_built),
            ],
            [
                _agent_dict("react", prompt="a"),
                _record(
                    factory="deepagent",
                    args={"tools": [_agent_tool("bash"), _agent_tool("python")]},
                    count=2,
                ),
                _record(
                    agent=_agent_dict("react", prompt="b"),
                    name="lead",
                    role="coordinator",
                ),
                _record(factory="inspect_swarm/log_test_never_built", args={}),
            ],
            id="mixed-list",
        ),
    ],
)
def test_logged_members(members: Callable[[], Any], logged: Any) -> None:
    step = _plan_step(_swarm(members()))
    assert step.params_passed == {
        "members": logged,
        "controller": CONTROLLER,
        "channels": [],
    }
    assert step.params["members"] == logged


def test_factory_args_log_as_the_built_agents_params() -> None:
    step = _plan_step(_swarm(member(deepagent, args={"tools": [bash(), python()]})))
    assert step.params_passed["members"]["args"] == registry_params(
        deepagent(tools=[bash(), python()])
    )


@pytest.mark.parametrize(
    "members",
    [
        pytest.param(lambda: react(prompt="a"), id="bare-agent"),
        pytest.param(
            lambda: [react(prompt="a"), react(prompt="b")], id="list-of-agents"
        ),
        pytest.param(
            lambda: member(never_built, name="w", count=4), id="single-record"
        ),
        pytest.param(
            lambda: [
                react(prompt="a"),
                member(deepagent, args={"tools": [bash(), python()]}, count=2),
                member(react(prompt="b"), name="lead"),
                member(never_built),
            ],
            id="mixed-list",
        ),
        pytest.param(
            lambda: [
                member(react(prompt="a"), limits=[cost_limit(0.5), time_limit(60)]),
                member(
                    never_built,
                    limits=[
                        token_limit(100, type="output"),
                        turn_limit(3),
                        message_limit(20),
                    ],
                ),
            ],
            id="limits",
        ),
    ],
)
def test_replay_rebuilds_the_swarm(members: Callable[[], Any]) -> None:
    original = _swarm(members())
    params = registry_params(original)
    # never_built raises if replay builds a factory-form member
    replayed = create_registry_object("agent", registry_info(original).name, params)
    assert registry_params(replayed) == params


_received: list[dict[str, Any]] = []


@agent(name="log_test_recording")
def recording(
    tools: list[Tool] | None = None,
    temperature: float = 0.0,
    config: dict[str, Any] | None = None,
) -> Agent:
    _received.append({"tools": tools, "temperature": temperature, "config": config})
    return react()


def test_replay_builds_members_with_the_same_inputs() -> None:
    _received.clear()
    record = member(
        recording, args={"tools": [bash()], "temperature": 0.5, "config": {"depth": 2}}
    )
    logged = registry_params(_swarm(record))["members"]
    replayed = Member.model_validate(registry_kwargs(member=logged)["member"])
    build_member(record)
    build_member(replayed)
    original, rebuilt = _received
    # a tool comes back as a tool, and data as the same data
    assert [registry_info(t).name for t in rebuilt["tools"]] == ["inspect_ai/bash"]
    assert rebuilt["temperature"] == original["temperature"] == 0.5
    assert rebuilt["config"] == original["config"] == {"depth": 2}


def _plan_step(solver: Agent) -> Any:
    return plan_to_eval_plan(Plan([as_solver(solver)]), GenerateConfig()).steps[0]


def _identifier(members: Any) -> str:
    task = Task(dataset=[Sample(input="x")], solver=_swarm(members), name="arm")
    [resolved] = resolve_tasks(
        task,
        task_args={},
        model=get_model("mockllm/model"),
        model_roles=None,
        sandbox=None,
        sample_shuffle=None,
    )
    return task_identifier(
        resolved, EvalSetArgsInTaskIdentifier(config=GenerateConfig())
    )


def _built_arm(prompt: Any) -> Any:
    return react(prompt=prompt)


def _factory_arm(prompt: Any) -> Any:
    return member(react, args={"prompt": prompt}, count=2)


def _cap_arm(cap: Any) -> Any:
    return member(react(prompt="a"), limits=[cost_limit(cap)])


@pytest.mark.parametrize(
    "arm,a,b",
    [
        pytest.param(_built_arm, "a", "b", id="built-agent-params"),
        pytest.param(_factory_arm, "a", "b", id="factory-args"),
        pytest.param(_cap_arm, 0.5, 1.5, id="limit-cap"),
    ],
)
def test_eval_set_arms_are_distinct(arm: Callable[[Any], Any], a: Any, b: Any) -> None:
    assert _identifier(arm(a)) == _identifier(arm(a))
    assert _identifier(arm(a)) != _identifier(arm(b))
