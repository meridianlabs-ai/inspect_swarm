"""The shared-memory channel: members split a count, share their parts on a board, and wait for each other.

Each member counts the primes in its own range, appends its count to `/shared_memory/counts.md`, and calls `swarm_wait` until every member's line is there. `counter()` is a member builder registered with `@agent`: `swarm_tools()` gives its loop the `shared_memory` and `swarm_wait` tools, and `swarm_on_continue()` adds a notice to its conversation when other members change the board.
"""

from inspect_ai import Task, task
from inspect_ai.agent import Agent, agent, react
from inspect_ai.dataset import Sample
from inspect_ai.scorer import match

from inspect_swarm import (
    member,
    shared_memory,
    swarm,
    swarm_on_continue,
    swarm_tools,
)

PROMPT = """\
How many prime numbers are there below 3000?

Work as a team. Split the numbers from 1 to 2999 into equal ranges, one for each member, in member order: counter-1 takes the first range. Count the primes in your range, then append one line to /shared_memory/counts.md with your name, your range and your count. Call swarm_wait until every member's line is in the file, view it, add the counts up and submit the total as a number only.
"""


@agent
def counter() -> Agent:
    """A `react()` member with the swarm's channel tools and notices."""
    return react(tools=[swarm_tools()], on_continue=swarm_on_continue())


@task
def shared_memory_count(k: int = 3) -> Task:
    return Task(
        dataset=[Sample(input=PROMPT, target="430")],
        solver=swarm(
            members=member(counter, name="counter", count=k),
            channels=[shared_memory()],
        ),
        scorer=match(),
        token_limit=500_000,
    )
