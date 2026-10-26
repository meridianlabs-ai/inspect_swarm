"""Writing a controller: stop the swarm when the first member ends with a checked answer.

`until_proved()` starts every member and stops the swarm at the first member that ends `submitted`, recording the stop reason `controller:proved`. That is right only when a member's normal return means its answer passed a check. `react()` also returns normally when it runs out of attempts, after repeated refusals, or after a context overflow, so `checked_solver()` raises a member-local custom limit when its agent returns without a passing check. The member then ends `member_limit:custom`, and the other members continue.

The check is the task's own scorer, which `react(attempts=...)` runs on each submission but the last, so `attempts` must be at least 2. Run it with `-T stop=all_done` to let every member run to its end instead.
"""

from contextlib import aclosing
from typing import Literal

from inspect_ai import Task, task
from inspect_ai.agent import Agent, AgentAttempts, AgentState, agent, react
from inspect_ai.scorer import Value, match, value_to_float
from inspect_ai.util import LimitExceededError
from questions import QUESTIONS

from inspect_swarm import (
    Controller,
    StopReason,
    SwarmControl,
    leaderless,
    member,
    swarm,
    swarm_controller,
)


@agent
def checked_solver(attempts: int = 5) -> Agent:
    """A `react()` member that returns normally only when one of its submissions passed the task's scorer."""
    # the swarm calls this builder once for each member in each sample, so `passed`
    # belongs to one member
    passed = False
    to_float = value_to_float()

    def score_value(value: Value) -> float:
        nonlocal passed
        result = to_float(value)
        passed = passed or result == 1.0
        return result

    inner = react(attempts=AgentAttempts(attempts, score_value=score_value))

    async def execute(state: AgentState) -> AgentState:
        state = await inner(state)
        if not passed:
            # with no `source`, this ends only this member, as member_limit:custom
            raise LimitExceededError(
                "custom",
                value=0,
                limit=0,
                message="returned without a passing check",
            )
        return state

    return execute


@swarm_controller
def until_proved() -> Controller:
    """Start every member, and stop the swarm when the first one ends `submitted`.

    Valid only when a member's normal return means a passed check, as `checked_solver()` makes it.
    """

    async def run(swarm: SwarmControl) -> StopReason:
        for m in swarm.members:
            swarm.start(m)
        async with aclosing(swarm.events()) as events:
            async for event in events:
                if event.submitted:
                    return "proved"
        return "all_done"

    return Controller(run)


@task
def stop_on_proof(
    k: int = 3,
    attempts: int = 5,
    stop: Literal["first_proof", "all_done"] = "first_proof",
) -> Task:
    # react() does not check its last submission, so one attempt is never checked
    if attempts < 2:
        raise ValueError(f"stop_on_proof: attempts must be at least 2, got {attempts}.")
    return Task(
        dataset=QUESTIONS,
        solver=swarm(
            members=member(checked_solver, args={"attempts": attempts}, count=k),
            controller=until_proved()
            if stop == "first_proof"
            else leaderless(stop_on_submission=False),
            channels=[],
        ),
        scorer=match(),
        token_limit=300_000,
    )
