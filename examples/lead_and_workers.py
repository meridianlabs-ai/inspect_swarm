"""A lead that hands out work on the shared memory to persistent workers.

The lead splits a count into tasks and appends each one to `/shared_memory/tasks.md`, naming the worker that should do it. The workers are persistent members: each answers once per turn and then waits idle. The controller, `lead_and_workers()`, wakes an idle worker when the lead writes to the board (a `MemberNews` event sent by the lead), and stops the swarm when the lead ends, with the lead's submission as the answer.

The worker protocol, in `WORKER_PROMPT`:

- On every turn, do each task for you in `tasks.md` that you have not done, and append its result to `/shared_memory/results.md`.
- Before you submit, view every file the board's status line names, until it names none. A file you leave unviewed stays news to you, so the swarm would report it (`MemberIdle.news`) and you would be woken again for it.
- When there is no task for you, submit `no task` at once. Every worker is woken when the lead writes, whichever worker the task is for.
"""

from contextlib import aclosing

from inspect_ai import Task, task
from inspect_ai.agent import Agent, agent, react
from inspect_ai.dataset import Sample
from inspect_ai.scorer import match

from inspect_swarm import (
    Controller,
    MemberEnded,
    MemberIdle,
    MemberNews,
    StopReason,
    SwarmControl,
    member,
    shared_memory,
    swarm,
    swarm_controller,
    swarm_tools,
)

QUESTION = "How many prime numbers are there below 3000? Answer with the number only."

LEAD_PROMPT = """\
You lead {workers} workers, named worker-1 to worker-{workers}. You do not count primes yourself.

Split the numbers from 1 to 2999 into {tasks} ranges of about the same size, numbered 1 to {tasks}. Give each worker one range at a time: append a line `<worker>: <task number>: count the primes from <first> to <last>` to /shared_memory/tasks.md. A worker appends `<worker>: <task number>: <count>` to /shared_memory/results.md when it is done. Wait for results with swarm_wait on /shared_memory/results.md, view the file, and give each worker that has finished its task the next range. When all {tasks} results are in, add them up and submit the total as a number only.
"""

WORKER_PROMPT = """\
You are a worker. The lead gives you tasks as lines in /shared_memory/tasks.md that start with your name. Each time you get a turn:

1. View /shared_memory/tasks.md. For each task for you that you have not done, do it and append one line to /shared_memory/results.md: `<your name>: <task number>: <result>`.
2. Before you submit, call shared_memory with command `changes` and view each file it names, by its id, until it names none. If tasks.md had a new task for you, do it first.
3. Submit the numbers of the tasks you did on this turn, or `no task` if there were none.

You will get another turn when the lead writes to the board.
"""

WAKE = "The lead wrote to the shared memory. Check for a new task."


@agent
def lead_member(workers: int = 2, tasks: int = 6) -> Agent:
    """The lead: splits the count into tasks, hands them out and adds up the results."""
    return react(
        prompt=LEAD_PROMPT.format(workers=workers, tasks=tasks),
        tools=[swarm_tools()],
    )


@agent
def worker_member() -> Agent:
    """A worker: does the tasks the lead gives it, one turn at a time."""
    return react(prompt=WORKER_PROMPT, tools=[swarm_tools()])


@swarm_controller
def lead_and_workers(lead: str = "lead") -> Controller:
    """Start every member, wake idle workers when the lead writes, and stop when the lead ends.

    A worker is woken on a `MemberNews` event sent by the lead, and on a `MemberIdle` event whose `news` is set: a write that landed while it was working and that it had not looked at. The lead's submission is the swarm's answer, chosen with `choose_answer()`, since the workers' answers are only reports on their own tasks.

    Args:
        lead: The name of the lead member.
    """

    async def run(swarm: SwarmControl) -> StopReason:
        for m in swarm.members:
            swarm.start(m)
        async with aclosing(swarm.events()) as events:
            async for event in events:
                if isinstance(event, MemberEnded) and event.member == lead:
                    if event.submitted:
                        swarm.choose_answer(lead, reason="the lead's answer")
                        return "submitted"
                    return "lead_ended"
                woken = (isinstance(event, MemberNews) and event.sender == lead) or (
                    isinstance(event, MemberIdle) and event.news
                )
                # an earlier event may have woken the member already
                if woken and swarm.member(event.member).state == "idle":
                    swarm.wake(event.member, WAKE)
        return "all_done"

    return Controller(run)


@task
def lead_and_workers_count(k: int = 2, tasks: int = 6) -> Task:
    return Task(
        dataset=[Sample(input=QUESTION, target="430")],
        solver=swarm(
            members=[
                member(lead_member, name="lead", args={"workers": k, "tasks": tasks}),
                member(worker_member, name="worker", count=k, persistent=True),
            ],
            controller=lead_and_workers(),
            channels=[shared_memory()],
        ),
        scorer=match(),
        token_limit=1_000_000,
    )
