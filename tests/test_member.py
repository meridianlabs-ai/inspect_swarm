import math
from collections import UserDict, UserList, UserString
from collections.abc import Callable, Sequence
from decimal import Decimal
from fractions import Fraction
from typing import Any

import pytest
from inspect_ai._util.registry import (
    registry_kwargs,
    registry_params,
    set_registry_params,
)
from inspect_ai.agent import Agent, AgentState, agent, deepagent, react
from inspect_ai.tool import bash, python
from inspect_ai.util import (
    Limit,
    TokenLimit,
    cost_limit,
    message_limit,
    time_limit,
    token_limit,
    turn_limit,
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

_LIMITS: list[Callable[[Any], Limit]] = [
    token_limit,
    cost_limit,
    turn_limit,
    message_limit,
    time_limit,
]


def _edited_cap() -> Member:
    limit = cost_limit(1.0)
    record = member(react, limits=[limit])
    limit.limit = Fraction(1, 2)  # pyright: ignore[reportAttributeAccessIssue]
    return record


@pytest.mark.parametrize(
    "kwargs,path",
    [
        ({"members": UserList([react(prompt="a")])}, "members"),
        ({"members": UserDict(_FACTORY_DICT)}, "members"),
        ({"members": [UserDict(_FACTORY_DICT)]}, r"members\[0\]"),
        ({"members": (UserDict(_FACTORY_DICT),)}, r"members\[0\]"),
        ({"members": {**_FACTORY_DICT, "factory": deepagent}}, "members.factory"),
        ({"members": {**_FACTORY_DICT, "args": UserDict(prompt="a")}}, "members.args"),
        (
            {"members": {**_FACTORY_DICT, "args": {1: "a"}}},
            "members.args has the int key",
        ),
        (
            {"members": {**_FACTORY_DICT, "limits": [UserDict(limit="cost", value=1)]}},
            r"members.limits\[0\]",
        ),
        (
            {"members": {**_FACTORY_DICT, "limits": [cost_limit(1.0)]}},
            r"members.limits\[0\]",
        ),
        (
            {
                "members": {
                    **_FACTORY_DICT,
                    "limits": [{"limit": "token", "value": TokenLimit(tokens=100)}],
                }
            },
            r"members.limits\[0\].value",
        ),
        ({"members": {**_FACTORY_DICT, "count": Fraction(2)}}, "members.count"),
        (
            {"members": member(member_worker, args={"prompt": UserString("a")})},
            "members.args.prompt",
        ),
        *[
            (
                {"members": [react(prompt="a"), member(form, limits=[limit(cap)])]},
                r"members\[1\].limits\[0\].value",
            )
            for limit in _LIMITS
            for form in (react, react(prompt="b"))
            for cap in (Fraction(3, 2), Decimal("1.5"))
        ],
        ({"members": _edited_cap()}, r"members.limits\[0\].value"),
        ({"channels": UserList([member_test_channel()])}, "channels"),
        ({"budget": UserDict(cost=1.0)}, "budget"),
        ({"budget": {"split": UserList([1.0])}}, "budget.split"),
        ({"budget": {"split": [Fraction(1, 2)]}}, r"budget.split\[0\]"),
        *[
            ({"budget": {field: Fraction(1, 2)}}, f"budget.{field}")
            for field in ("cost", "tokens", "time", "reserve", "grace")
        ],
    ],
)
def test_swarm_refuses_values_inspect_cannot_log(
    kwargs: dict[str, Any], path: str
) -> None:
    # Inspect logs swarm()'s arguments as passed, and other values without their contents
    kwargs.setdefault("members", react(prompt="a"))
    with pytest.raises(TypeError, match=rf"^{path} .*cannot log faithfully"):
        _swarm(**kwargs)


def test_member_serializer_refuses_values_inspect_cannot_log() -> None:
    record = member(react, limits=[cost_limit(Fraction(1, 2))])  # pyright: ignore[reportArgumentType]
    with pytest.raises(ValueError, match=r"member.limits\[0\].value is a Fraction"):
        record.model_dump()


_TOOL_MARKER: dict[str, Any] = {"type": "tool", "name": "bash", "params": {}}
_AGENT_MARKER: dict[str, Any] = {"type": "agent", "name": "react", "params": {}}
_MODEL_MARKER: dict[str, Any] = {
    "model": "mockllm/model",
    "config": {},
    "base_url": None,
    "model_args": {},
}


@pytest.mark.parametrize(
    "kwargs,path",
    [
        *[
            (
                {"members": member(member_worker, args={"prompt": v})},
                "members.args.prompt",
            )
            for v in (math.inf, -math.inf, math.nan)
        ],
        (
            {"members": {**_FACTORY_DICT, "args": {"prompt": math.inf}}},
            "members.args.prompt",
        ),
        *[
            (
                {"members": [react(prompt="a"), member(form, limits=[limit(cap)])]},
                r"members\[1\].limits\[0\].value",
            )
            for limit in _LIMITS
            for form in (react, react(prompt="b"))
            for cap in (math.inf, math.nan)
        ],
        *[
            (
                {"members": member(member_worker, args={"prompt": m})},
                "members.args.prompt",
            )
            for m in (_TOOL_MARKER, _AGENT_MARKER, _MODEL_MARKER)
        ],
        (
            {"members": {**_FACTORY_DICT, "args": {"prompt": [_TOOL_MARKER]}}},
            r"members.args.prompt\[0\]",
        ),
    ],
)
def test_swarm_refuses_values_inspect_logs_as_something_else(
    kwargs: dict[str, Any], path: str
) -> None:
    # a non-finite float logs as null; a dict in a logged object's form replays as that object
    kwargs.setdefault("members", react(prompt="a"))
    with pytest.raises(ValueError, match=rf"^{path} is "):
        _swarm(**kwargs)


@agent(name="member_test_replaced")
def to_be_replaced() -> Agent:
    return react()


def _mutated_factory_args() -> Member:
    record = member(deepagent)
    record.args["background"] = 64
    return record


def _mutated_agent_params() -> Member:
    built = deepagent()
    record = member(built)
    set_registry_params(built, {**registry_params(built), "background": 64})
    return record


def _replaced_factory() -> Member:
    record = member(to_be_replaced)

    @agent(name="member_test_replaced")
    def replacement() -> Agent:
        return react()

    return record


@pytest.mark.parametrize(
    "record,error,match",
    [
        (_mutated_factory_args, ValueError, "background"),
        (_mutated_agent_params, ValueError, "background"),
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
