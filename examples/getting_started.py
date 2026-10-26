"""Getting started: `k` `react()` members answer each question at the same time.

The first member to submit gives the swarm's answer, and the task's `match()` scorer scores it as it would score one agent. The other members are stopped. `k=1` is a swarm of one, the single-agent arm to compare with.

The questions need no sandbox, so the swarm has no channels: `channels=[]` leaves out the default `filesystem` channel, which would tell members to share files in a sandbox.
"""

from inspect_ai import Task, task
from inspect_ai.agent import react
from inspect_ai.scorer import match
from questions import QUESTIONS

from inspect_swarm import member, swarm


@task
def getting_started(k: int = 3) -> Task:
    return Task(
        dataset=QUESTIONS,
        solver=swarm(members=member(react, count=k), channels=[]),
        scorer=match(),
        token_limit=200_000,
    )
