from collections import UserDict, UserList
from collections.abc import Callable, Sequence
from typing import Any

import pytest
from inspect_ai._util.registry import registry_kwargs
from inspect_ai.agent import Agent, AgentState, agent, deepagent, react
from inspect_ai.tool import bash, python
from inspect_ai.util import cost_limit, time_limit, token_limit, working_limit

from inspect_swarm import (
    Budget,
    Channel,
    Controller,
    Member,
    MemberInfo,
    StopReason,
    SwarmControl,
    member,
    swarm,
    swarm_channel,
    swarm_controller,
)


async def _run(control: SwarmControl) -> StopReason:
    return "all_done"


@swarm_controller
def member_test_controller() -> Controller:
    return Controller(_run)


@swarm_channel
def member_test_channel() -> Channel:
    return Channel()


@agent
def member_worker(prompt: str = "work") -> Agent:
    return react(prompt=prompt)


@agent
def needs_prompt(prompt: str) -> Agent:
    return react(prompt=prompt)


@agent
def outside_sample(prompt: str = "work") -> Agent:
    raise RuntimeError("only inside a sample")


def _swarm(members: Any, **kwargs: Any) -> Agent:
    kwargs.setdefault("controller", member_test_controller())
    kwargs.setdefault("channels", [])
    return swarm(members, **kwargs)


def _roster(members: Any) -> list[str]:
    roster: list[MemberInfo] = []

    @swarm_controller(name="member_test_recorder")
    def recorder() -> Controller:
        return Controller(_run, check=roster.extend)

    _swarm(members, controller=recorder())
    return [m.name for m in roster]


_REACT_A = react(prompt="a")


@pytest.mark.parametrize(
    "members,names",
    [
        (lambda: react(prompt="a"), ["react"]),
        (
            lambda: member(member_worker, count=3),
            ["member_worker-1", "member_worker-2", "member_worker-3"],
        ),
        (lambda: member(deepagent, name="w", count=2), ["w-1", "w-2"]),
        (lambda: member(react(prompt="a"), name="lead"), ["lead"]),
        (lambda: [react(prompt="a"), react(prompt="b")], ["react-1", "react-2"]),
        (
            lambda: [
                react(prompt="a"),
                member(react, count=2),
                member(react(prompt="b"), name="lead"),
            ],
            ["react-1", "react-2", "react-3", "lead"],
        ),
        (
            lambda: [member(react, count=2), react(prompt="a")],
            ["react-1", "react-2", "react-3"],
        ),
        (lambda: [member(member_worker, name="w"), react(prompt="a")], ["w", "react"]),
    ],
)
def test_member_names(members: Callable[[], Any], names: list[str]) -> None:
    assert _roster(members()) == names


@pytest.mark.parametrize(
    "members",
    [
        lambda: [member(react(prompt="a"), name="react"), react(prompt="b")],
        lambda: [
            member(member_worker, name="w-1"),
            member(member_worker, name="w", count=2),
        ],
        lambda: [member(react(prompt="a"), name="x"), member(member_worker, name="x")],
    ],
)
def test_member_name_collision_raises(members: Callable[[], Any]) -> None:
    with pytest.raises(ValueError, match="unique"):
        _swarm(members())


@pytest.mark.parametrize(
    "members",
    [
        lambda: deepagent,
        lambda: [deepagent, react],
        lambda: [member(deepagent), react],
    ],
)
def test_bare_factory_raises(members: Callable[[], Any]) -> None:
    with pytest.raises(TypeError, match=r"member\(\w+, args="):
        _swarm(members())


@pytest.mark.parametrize(
    "members",
    [
        lambda: [_REACT_A, _REACT_A],
        lambda: [_REACT_A, member(_REACT_A, name="x")],
    ],
)
def test_same_agent_in_two_members_raises(members: Callable[[], Any]) -> None:
    with pytest.raises(ValueError, match="same agent object"):
        _swarm(members())


def _unregistered() -> Agent:
    async def execute(state: AgentState) -> AgentState:
        return state

    return execute


@agent(name="member_test_shadowed")
def shadowed() -> Agent:
    return react()


@agent(name="member_test_shadowed")
def shadowing() -> Agent:
    return react()


@pytest.mark.parametrize(
    "make,error,match",
    [
        (
            lambda: member(deepagent(), count=4),
            TypeError,
            r"member\(deepagent, args=\{\.\.\.\}, count=4\)",
        ),
        (
            lambda: member(deepagent(), args={"todo_write": False}),
            TypeError,
            "args are the factory's arguments",
        ),
        (lambda: member(lambda: react()), TypeError, "@agent"),  # pyright: ignore[reportArgumentType]
        (lambda: member(_unregistered()), TypeError, "@agent"),
        (lambda: member(_unregistered), TypeError, "@agent"),
        (lambda: member(shadowed), TypeError, "@agent"),
        (lambda: member(member_worker, args={"promt": "x"}), TypeError, "promt"),
        (lambda: member(needs_prompt), TypeError, "prompt"),
        (lambda: member(member_worker, count=0), ValueError, "count"),
        (
            lambda: member(member_worker, limits=[working_limit(10)]),
            ValueError,
            "working limit",
        ),
        (lambda: member(deepagent(background=True)), ValueError, "background"),
        (lambda: member(deepagent(background=64)), ValueError, "background"),
        (
            lambda: member(deepagent, args={"background": True}),
            ValueError,
            "background",
        ),
        (lambda: member(deepagent, args={"background": 64}), ValueError, "background"),
        (lambda: _swarm(deepagent(background=64)), ValueError, "background"),
    ],
)
def test_member_refusals(
    make: Callable[[], object], error: type[Exception], match: str
) -> None:
    with pytest.raises(error, match=match):
        make()


def test_member_does_not_call_factory() -> None:
    record = member(outside_sample, args={"prompt": "x"}, count=2)
    with pytest.raises(RuntimeError, match="only inside a sample"):
        record.build()


def test_build_gives_the_agent_or_a_new_one_per_call() -> None:
    built = react(prompt="a")
    assert member(built).build() is built
    record = member(member_worker, args={"prompt": "x"})
    assert record.build() is not record.build()


@pytest.mark.parametrize(
    "record",
    [
        lambda: member(
            react(prompt="a", tools=[bash()]),
            name="x",
            role="lead",
            limits=[cost_limit(2.0), time_limit(60)],
        ),
        lambda: member(
            deepagent,
            args={"tools": [bash(), python()]},
            name="w",
            count=4,
            limits=[token_limit(100, type="output")],
        ),
    ],
)
def test_member_validates_from_its_logged_dict(record: Callable[[], Member]) -> None:
    original = record()
    logged = original.model_dump()
    # replay rebuilds nested registry dicts (agents, tools) before validating
    replayed = Member.model_validate(registry_kwargs(**logged))
    assert replayed.model_dump() == logged


def test_member_unknown_factory_name_raises() -> None:
    with pytest.raises(ValueError, match="not a registered agent"):
        Member.model_validate({"factory": "no_such_factory", "args": {}})


@pytest.mark.parametrize(
    "kwargs,error,match",
    [
        ({"members": []}, ValueError, "at least one member"),
        # Inspect logs other containers as their type name, losing their contents
        (
            {"members": UserList([react(prompt="a")])},
            TypeError,
            "members= takes a list or tuple",
        ),
        (
            {"channels": UserList([member_test_channel()])},
            TypeError,
            "channels= takes a list or tuple",
        ),
        ({"members": ["react"]}, TypeError, "got a str"),
        (
            {"channels": [member_test_channel(), "member_test_channel"]},
            ValueError,
            "given more than once",
        ),
        ({"channels": "no_such_channel"}, ValueError, "not a registered swarm_channel"),
        (
            {"controller": "no_such_controller"},
            ValueError,
            "not a registered swarm_controller",
        ),
        ({"budget": 10.0}, TypeError, "Budget"),
    ],
)
def test_swarm_argument_refusals(
    kwargs: dict[str, Any], error: type[Exception], match: str
) -> None:
    kwargs.setdefault("members", react(prompt="a"))
    with pytest.raises(error, match=match):
        _swarm(**kwargs)


def test_swarm_controller_check_refuses_roster() -> None:
    def check(roster: Sequence[MemberInfo]) -> None:
        raise ValueError(f"no lead in {[m.name for m in roster]}")

    @swarm_controller(name="member_test_checking")
    def checking() -> Controller:
        return Controller(_run, check=check)

    with pytest.raises(ValueError, match=r"no lead in \['react'\]"):
        _swarm(react(prompt="a"), controller=checking())


def test_swarm_accepts_budget_dict() -> None:
    # replay passes the budget back as its logged fields
    _swarm(react(prompt="a"), budget=Budget(cost=5.0).model_dump())


_FACTORY_DICT: dict[str, Any] = {"factory": "react", "args": {"prompt": "a"}}


@pytest.mark.parametrize(
    "kwargs,match",
    [
        ({"members": UserDict(_FACTORY_DICT)}, "logged form as a dict"),
        ({"members": [UserDict(_FACTORY_DICT)]}, "logged form as a dict"),
        ({"members": (UserDict(_FACTORY_DICT),)}, "logged form as a dict"),
        (
            {"members": {**_FACTORY_DICT, "factory": deepagent}},
            "factory must be a registry name",
        ),
        (
            {"members": {**_FACTORY_DICT, "args": UserDict(prompt="a")}},
            "args must be a dict",
        ),
        (
            {
                "members": {
                    **_FACTORY_DICT,
                    "limits": [UserDict(limit="cost", value=1.0)],
                }
            },
            "limits must be a list of limit dicts",
        ),
        (
            {"members": {**_FACTORY_DICT, "limits": [cost_limit(1.0)]}},
            "limits must be a list of limit dicts",
        ),
        ({"budget": UserDict(cost=1.0)}, "Budget or its logged dict"),
        ({"budget": {"split": UserList([1.0])}}, "split must be a list or tuple"),
    ],
)
def test_swarm_refuses_logged_forms_inspect_cannot_log(
    kwargs: dict[str, Any], match: str
) -> None:
    # Inspect logs swarm()'s arguments as passed, and these shapes without their contents
    kwargs.setdefault("members", react(prompt="a"))
    with pytest.raises(TypeError, match=match):
        _swarm(**kwargs)
