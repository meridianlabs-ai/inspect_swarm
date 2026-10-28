"""Budgets as task parameters: agent count, total budget and split.

The task sets the sample's cost limit, and the swarm derives its cap from it, holding back 5% for calls in flight when it stops. Each member can check the budget with the `swarm_resources()` tool. For example, eight members with $1 each:

    inspect eval examples/budget_sweep.py --model openai/gpt-5-mini -T k=8 -T budget=8 -T split=equal

A cost cap needs cost data for every member's model. Inspect has it for many models; configure the rest with `--model-cost-config`.
"""

from typing import Literal

from inspect_ai import Task, task
from inspect_ai.agent import react
from inspect_ai.scorer import match
from questions import QUESTIONS

from inspect_swarm import Budget, member, swarm, swarm_resources


@task
def budget_sweep(
    k: int = 4, budget: float = 1.0, split: Literal["pool", "equal"] = "pool"
) -> Task:
    return Task(
        dataset=QUESTIONS,
        solver=swarm(
            members=member(react, args={"tools": [swarm_resources()]}, count=k),
            channels=[],
            budget=Budget(split=split),
        ),
        scorer=match(),
        cost_limit=budget,
    )
