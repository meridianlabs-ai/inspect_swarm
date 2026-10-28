from collections.abc import Callable, Sequence
from typing import Any

import pytest
from inspect_ai._util.registry import registry_kwargs
from inspect_ai.agent import Agent, AgentState, agent, deepagent, react
from inspect_ai.tool import bash, python
from inspect_ai.util import (
    cost_limit,
    time_limit,
    token_limit,
    working_limit,
)

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
from inspect_swarm._member import build_member


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
        build_member(record)


def test_build_member_gives_the_agent_or_a_new_one_per_call() -> None:
    built = react(prompt="a")
    assert build_member(member(built)) is built
    record = member(member_worker, args={"prompt": "x"})
    assert build_member(record) is not build_member(record)


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
        # background members are accepted, in both forms, and round-trip
        lambda: member(deepagent(background=True), name="b"),
        lambda: member(deepagent(background=64), name="b"),
        lambda: member(deepagent, args={"background": True}, count=2),
        lambda: member(deepagent, args={"background": 64}),
    ],
)
def test_member_validates_from_its_logged_dict(record: Callable[[], Member]) -> None:
    original = record()
    logged = original.model_dump()
    # replay rebuilds nested registry dicts (agents, tools) before validating
    replayed = Member.model_validate(registry_kwargs(**logged))
    assert replayed.model_dump() == logged


def test_a_member_given_as_data_without_choose_name_is_told_its_name() -> None:
    record = Member.model_validate({"factory": "member_worker"})
    assert record.choose_name is False


def test_member_unknown_factory_name_raises() -> None:
    with pytest.raises(ValueError, match="not a registered agent"):
        Member.model_validate({"factory": "no_such_factory", "args": {}})


@pytest.mark.parametrize(
    "kwargs,error,match",
    [
        ({"members": []}, ValueError, "at least one member"),
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


@agent(name="member_test_replaced")
def to_be_replaced() -> Agent:
    return react()


def _replaced_factory() -> Member:
    record = member(to_be_replaced)

    @agent(name="member_test_replaced")
    def replacement() -> Agent:
        return react()

    return record


@pytest.mark.parametrize(
    "record,error,match",
    [
        (_replaced_factory, TypeError, "@agent"),
    ],
)
def test_swarm_rechecks_member_records(
    record: Callable[[], Member], error: type[Exception], match: str
) -> None:
    # a record's args or the registry can change after member() checked it
    with pytest.raises(error, match=match):
        _swarm(record())


@pytest.mark.parametrize("kind", ["token", "cost", "turn", "message", "time"])
@pytest.mark.parametrize(
    "fields,valid",
    [({}, False), ({"vale": 2}, False), ({"value": None}, True), ({"value": 2}, True)],
)
def test_limit_dict_needs_a_value(
    kind: str, fields: dict[str, Any], valid: bool
) -> None:
    logged = {"factory": "react", "limits": [{"limit": kind, **fields}]}
    if valid:
        record = Member.model_validate(logged)
        assert record.model_dump()["limits"][0]["value"] == fields["value"]
    else:
        with pytest.raises(ValueError, match="needs a 'value' key"):
            Member.model_validate(logged)


@pytest.mark.parametrize(
    "limit,match",
    [
        ({"limit": "token", "value": 2, "typ": "output"}, "unknown 'typ'"),
        (
            {"limit": "time", "value": 2, "secs": 5, "note": ""},
            "unknown 'secs', 'note'",
        ),
        *[
            ({"limit": kind, "value": 2, "type": "output"}, "unknown 'type'")
            for kind in ["cost", "turn", "message", "time"]
        ],
    ],
)
def test_limit_dict_refuses_unknown_keys(limit: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        Member.model_validate({"factory": "react", "limits": [limit]})
