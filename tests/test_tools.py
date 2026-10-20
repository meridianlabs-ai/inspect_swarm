import re
from dataclasses import astuple
from typing import Any
from uuid import uuid4

import pytest
from inspect_ai.agent import Agent, AgentState, agent
from inspect_ai.model import ChatMessageUser, Model
from inspect_ai.util import store

from inspect_swarm import (
    Budget,
    BudgetUsage,
    StopReason,
    SwarmBudget,
    SwarmControl,
    member,
    swarm,
    swarm_budget,
    swarm_resources,
)
from inspect_swarm._budget import MemberContext, current_member
from inspect_swarm._evidence import LEDGER_KEY
from inspect_swarm._ledger import SwarmLedger

from .swarm_runtime import (
    TOKENS_PER_CALL,
    controller,
    generator,
    run_swarm,
    sample,
    scripted_model,
)


@agent
def budget_reader(
    model: Model, calls: int, seen: dict[str, tuple[SwarmBudget, str]]
) -> Agent:
    # generates `calls` times, then reads the budget through the function and the tool
    async def execute(state: AgentState) -> AgentState:
        name = state.messages[-1].text.split(",")[0].removeprefix("You are ")
        state = await generator(model, calls=calls)(state)
        text = await swarm_resources()()
        seen[name] = (swarm_budget(), str(text))
        return state

    return execute


def _one_at_a_time() -> Any:
    # each member runs alone, so what it reads does not depend on its siblings' timing
    async def run(control: SwarmControl) -> StopReason:
        for m in control.members:
            control.start(m)
            async for _ in control.events():
                pass
        return "all_done"

    return controller(run, f"tools_test_{uuid4().hex}")


async def _run_readers(
    budget: Budget,
) -> tuple[dict[str, tuple[SwarmBudget, str]], SwarmLedger]:
    # members a and b, one at a time; a makes one call and b two, each $1 and
    # TOKENS_PER_CALL tokens
    seen: dict[str, tuple[SwarmBudget, str]] = {}
    agent_swarm = swarm(
        [
            member(budget_reader(scripted_model(cost=1.0), 1, seen), name="a"),
            member(budget_reader(scripted_model(cost=1.0), 2, seen), name="b"),
        ],
        controller=_one_at_a_time(),
        channels=[],
        budget=budget,
    )
    async with sample():
        await agent_swarm(AgentState(messages=[ChatMessageUser(content="task")]))
        ledger = SwarmLedger.model_validate(store().get(LEDGER_KEY))
    return seen, ledger


T = TOKENS_PER_CALL


@pytest.mark.anyio
@pytest.mark.parametrize(
    "budget,a,b",
    [
        pytest.param(
            Budget(cost=10.0, tokens=20 * T, time=600.0),
            (BudgetUsage(10.0, 1.0), BudgetUsage(20 * T, T)),
            (BudgetUsage(10.0, 3.0), BudgetUsage(20 * T, 3 * T)),
            id="pool",
        ),
        pytest.param(
            Budget(cost=10.0, tokens=20 * T, time=600.0, split="equal"),
            (BudgetUsage(10.0, 1.0, 5.0, 1.0), BudgetUsage(20 * T, T, 10 * T, T)),
            (
                BudgetUsage(10.0, 3.0, 5.0, 2.0),
                BudgetUsage(20 * T, 3 * T, 10 * T, 2 * T),
            ),
            id="equal",
        ),
        pytest.param(
            Budget(cost=8.0, tokens=20 * T, time=600.0, split=(1, 3)),
            (BudgetUsage(8.0, 1.0, 2.0, 1.0), BudgetUsage(20 * T, T, 5 * T, T)),
            (
                BudgetUsage(8.0, 3.0, 6.0, 2.0),
                BudgetUsage(20 * T, 3 * T, 15 * T, 2 * T),
            ),
            id="weights",
        ),
    ],
)
async def test_budget_reports_caps_pool_usage_and_shares_consistent_with_the_ledger(
    budget: Budget,
    a: tuple[BudgetUsage, BudgetUsage],
    b: tuple[BudgetUsage, BudgetUsage],
) -> None:
    seen, ledger = await _run_readers(budget)

    for name, (cost, tokens) in (("a", a), ("b", b)):
        view, _ = seen[name]
        assert view.split == budget.split
        assert view.cost is not None
        assert astuple(view.cost) == pytest.approx(astuple(cost))
        assert view.tokens == tokens
        assert view.token_type == "all"
        assert view.time is not None and view.time.cap == 600.0
        assert 0 <= view.time.used < 600.0

    # the caps are the ledger's, and b read last, after every call the swarm made
    last, _ = seen["b"]
    assert last.cost is not None and last.tokens is not None
    assert ledger.caps == {
        "cost": last.cost.cap,
        "tokens": last.tokens.cap,
        "time": 600,
    }
    assert last.cost.used == pytest.approx(ledger.swarm.cost)
    assert last.tokens.used == ledger.swarm.usage.total_tokens
    # each member read its share after its last call: its usage is its ledger meter
    if budget.split != "pool":
        for m in ledger.members:
            view, _ = seen[m.name]
            assert view.cost is not None and view.tokens is not None
            assert view.cost.share_used == pytest.approx(m.meter.cost)
            assert view.tokens.share_used == m.meter.usage.total_tokens


@pytest.mark.anyio
async def test_swarm_resources_reports_the_budget_as_text() -> None:
    seen, _ = await _run_readers(
        Budget(cost=8.0, tokens=f"output:{20 * T}", time=600.0, split=(1, 3))
    )
    _, text = seen["b"]
    *lines, time = text.splitlines()
    assert lines == [
        "Cost: the swarm has used $3.0000 of its cap of $8.0000; $5.0000 left.",
        "Cost, your share: you have used $2.0000 of $6.0000; $4.0000 left.",
        "Tokens (output): the swarm has used 150 of its cap of 1,000; 850 left.",
        "Tokens (output), your share: you have used 100 of 750; 650 left.",
    ]
    # elapsed time depends on the runner, so only its shape and sum are fixed
    match = re.fullmatch(
        r"Time: the swarm has used (\d+) seconds of its cap of 600 seconds; (\d+) seconds left\.",
        time,
    )
    assert match is not None
    assert abs(int(match[1]) + int(match[2]) - 600) <= 1


@pytest.mark.anyio
async def test_swarm_resources_without_caps() -> None:
    seen, _ = await _run_readers(Budget())
    view, text = seen["a"]
    assert (view.cost, view.tokens, view.time) == (None, None, None)
    assert text == "The swarm has no cost, token or time cap."


@pytest.mark.anyio
async def test_swarm_budget_gives_no_share_outside_the_runs_own_members() -> None:
    # the controller has no member; another run's member of the same name has its own
    # context, and must not read this run's share
    views: list[SwarmBudget] = []

    async def run(control: SwarmControl) -> StopReason:
        for context in (None, MemberContext("a", bridged=False)):
            token = current_member.set(context)
            try:
                views.append(swarm_budget())
            finally:
                current_member.reset(token)
        return "all_done"

    agent_swarm = swarm(
        [
            member(generator(scripted_model(), calls=0), name="a"),
            member(generator(scripted_model(), calls=0), name="b"),
        ],
        controller=controller(run, f"tools_test_{uuid4().hex}"),
        channels=[],
        budget=Budget(tokens=100, split="equal"),
    )
    async with sample():
        await agent_swarm(AgentState(messages=[ChatMessageUser(content="task")]))
    assert [view.tokens for view in views] == [BudgetUsage(100, 0)] * 2


def test_swarm_budget_outside_a_swarm_raises() -> None:
    with pytest.raises(RuntimeError, match="outside a swarm"):
        swarm_budget()


@agent
def split_recorder(model: Model, preambles: dict[str, str]) -> Agent:
    async def execute(state: AgentState) -> AgentState:
        preamble = state.messages[-1].text
        preambles[preamble.split(",")[0].removeprefix("You are ")] = preamble
        return state

    return execute


@pytest.mark.anyio
@pytest.mark.parametrize(
    "budget,count,expected",
    [
        (
            Budget(cost=10.0),
            2,
            ["The budget is shared by all members."] * 2,
        ),
        (
            Budget(cost=10.0, tokens=1000, split="equal"),
            2,
            [
                "The swarm's cost and token budget is split equally between the members; you stop when your share is used up."
            ]
            * 2,
        ),
        (
            Budget(tokens=1000, split=(1, 3)),
            2,
            [
                "The swarm's token budget is split between the members in fixed shares; yours is 25% of it, and you stop when it is used up.",
                "The swarm's token budget is split between the members in fixed shares; yours is 75% of it, and you stop when it is used up.",
            ],
        ),
        # no cap, or no one to share it with
        (Budget(), 2, [None, None]),
        (Budget(cost=10.0), 1, [None]),
    ],
)
async def test_preamble_states_the_split(
    budget: Budget, count: int, expected: list[str | None]
) -> None:
    preambles: dict[str, str] = {}
    names = ["w"] if count == 1 else [f"w-{i + 1}" for i in range(count)]
    await run_swarm(
        swarm(
            [
                member(split_recorder(scripted_model(cost=1.0), preambles), name=name)
                for name in names
            ],
            channels=[],
            budget=budget,
        )
    )
    for name, sentence in zip(names, expected, strict=True):
        head = (
            f"You are {name}, the only agent in this swarm."
            if count == 1
            else f"You are {name}, one of {count} agents in a swarm working on this task at the same time."
        )
        assert preambles[name] == (
            head if sentence is None else f"{head}\n\n{sentence}"
        )
