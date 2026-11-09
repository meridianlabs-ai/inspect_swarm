# Members – Inspect Swarm

## Overview

A member is an agent registered with `@agent`, such as [react()](https://inspect.aisi.org.uk/reference/inspect_ai.agent.html#react) or [deepagent()](https://inspect.aisi.org.uk/reference/inspect_ai.agent.html#deepagent). Give it in one of two forms:

- The factory form: the factory and its arguments, `member(react, args={...})`. The swarm calls the factory once for each member in each sample, so every member gets its own agent. `count` makes several members from one record.
- The built form: an agent you have built, `member(react(...))`, or the agent alone. It is one member. The same agent object cannot be in two members.

``` python
from inspect_ai.agent import deepagent, react
from inspect_ai.tool import bash, python
from inspect_ai.util import token_limit

from inspect_swarm import member, swarm

solver = swarm(
    members=[
        member(
            react,
            args={"tools": [bash(), python()]},
            name="solver",
            role="solve the task",
            count=3,
        ),
        member(
            deepagent(tools=[bash()]),
            name="reviewer",
            role="check the solvers' work in /shared",
            limits=[token_limit(200_000)],
        ),
    ],
)
```

This swarm has four members: `solver-1` to `solver-3` and `reviewer`. The role is added to the member’s preamble and given to the controller.

## Names

A member is named after its agent (`react`, `deepagent`) unless you give `name`. With `count` above 1 the members are `name-1` to `name-k`. Names must be unique in the swarm. They cannot be `init`, `solvers` or `scorers`, and cannot contain `/` or `#`. The [shared-memory channel](./shared-memory.html.md) also needs names that start with a lowercase letter, followed by at most 47 lowercase letters, digits, `_` or `-`.

Members left on their default name are numbered across the whole roster, not per record. So one member with a different prompt and model takes two records:

``` python
from inspect_ai.agent import react

from inspect_swarm import member, swarm

solver = swarm(
    members=[
        member(react, args={"prompt": "Solve the task."}, count=6),
        member(
            react,
            args={
                "prompt": "Check the others' work in /shared.",
                "model": "openai/gpt-5",
            },
        ),
    ],
)
```

The first record makes `react-1` to `react-6` and the second `react-7`.

When every copy differs, give each its own record. A list comprehension builds them:

``` python
prompts = ["Try algebra.", "Try brute force.", "Try a proof by cases."]
solver = swarm(members=[member(react, args={"prompt": p}) for p in prompts])
```

These are `react-1` to `react-3`. Names you give with `name` are not numbered across records, so give each record its own.

### Members that choose their names

With `choose_name=True` a member is not told its name. The swarm’s preamble says how many agents are in the swarm and asks it to choose a name for itself and tell the other members what it is:

``` python
from inspect_ai.agent import react
from inspect_ai.tool import bash

from inspect_swarm import member, swarm

solver = swarm(
    members=member(react, args={"tools": [bash()]}, count=4, choose_name=True),
)
```

Each member starts with `You are one of 4 agents in a swarm working on this task at the same time. Choose a name for yourself and tell the other members what it is.` Its role, if it has one, is still stated. The log still names the members `react-1` to `react-4`: the swarm’s timelines, spans, tool state, budget shares and result record use those names. The name a member chose is in its transcript, not in the swarm’s records.

A roster can mix the two, for example a lead you name and workers that name themselves. The `filesystem` channel then tells every member that its scratch directory may be taken by another member ([Channels](./channels.html.md#the-filesystem-channel)).

A swarm in which any member chooses its name can use only the `filesystem` channel or no channel (`channels=[]`). Other channels, including [shared memory](./shared-memory.html.md) and your own, show members to each other by name, so [swarm()](./reference/inspect_swarm.html.md#swarm) refuses them.

The swarm does not change text you give the member yourself: the sample input, a controller’s `start(input=...)` and `start(preamble=...)`, the agent’s prompt, and the sandbox configuration. A controller’s `start(preamble=...)` uses its text as given, and `preamble=False` leaves the preamble out ([Controllers](./controllers.html.md)). Neither adds a request to choose a name.

## Limits

`limits` takes token, cost, turn, message and time limits for that member alone. Working limits are refused. A member that reaches one ends with status `member_limit:<type>`, and the others continue. The swarm’s own caps are set with `budget=` ([Budgets](./budget.html.md)).

A member time limit does not run while the member is held at a turn boundary by a controller’s callbacks ([Controllers](./controllers.html.md#boundary-callbacks)): it counts only the time the member was not held. A member whose time limit ran out before it reached the boundary ends `member_limit:time` there, without being held.

## Member builders

A member builder is a function registered with `@agent` that builds the member’s agent. Pass it to [member()](./reference/inspect_swarm.html.md#member) without calling it, with its arguments in `args`:

``` python
from inspect_ai.agent import Agent, agent, react
from inspect_ai.tool import bash

from inspect_swarm import member, shared_memory, swarm, swarm_tools


@agent
def worker() -> Agent:
    return react(tools=[bash(), swarm_tools()])


solver = swarm(members=member(worker, name="w", count=3), channels=[shared_memory()])
```

Use a builder for anything the swarm gives a member at run time, such as [swarm_tools()](./reference/inspect_swarm.html.md#swarm_tools) ([Shared Memory](./shared-memory.html.md)). The log records a builder by its name and arguments, so the swarm can be rebuilt from the log. It cannot rebuild [swarm_tools()](./reference/inspect_swarm.html.md#swarm_tools) passed in `args` or inside an agent you built.

A builder is also the place for anything each member must have its own of. Every member built from one record gets the same objects from `args`, and two built agents given the same tool object share it, across members and samples. Inspect’s built-in tools keep their state in the sample store or the sandbox, so this matters only for a custom tool or helper that keeps state in its Python object (a counter, a cache, a client). The swarm calls a builder once for each member in each sample, so what the builder creates belongs to one member. [`examples/stop_on_proof.py`](https://github.com/meridianlabs-ai/inspect_swarm/blob/main/examples/stop_on_proof.py) keeps a per-member flag this way.

## Tool state

Each member runs in its own tool-state scope, named after the member. Built-in tools that keep state in the sample store ([memory()](https://inspect.aisi.org.uk/reference/inspect_ai.tool.html#memory), [bash_session()](https://inspect.aisi.org.uk/reference/inspect_ai.tool.html#bash_session), the skill tool) give each member its own state when their `instance` is left at the default, so the default [memory()](https://inspect.aisi.org.uk/reference/inspect_ai.tool.html#memory) is private to each member.

An explicit `instance` is used as given: members that name the same instance share its state. A board shared on purpose was `memory(instance="board")` before the shared-memory channel; see [Shared Memory](./shared-memory.html.md#migrating-from-memoryinstance) for moving to the channel.

- The sample store itself is shared. State that custom tools or agent code keep in the store with no instance is shared unless they resolve their instance with [tool_state_instance()](https://inspect.aisi.org.uk/reference/inspect_ai.util.html#tool_state_instance).
- Tool state that a setup solver prepared at the default instance before the swarm runs is not visible to the members. They start with fresh state.

## Background work

Native members support `deepagent(background=True)` and integer background limits. Work started with [background()](https://inspect.aisi.org.uk/reference/inspect_ai.util.html#background) belongs to its member and uses its limits, share and sandbox. A member limit or swarm cap ends the background task that reached it. A swarm cap also starts the swarm’s soft stop; other work gets the effective grace period to finish calls already in flight.

When the member’s agent ends, its background work gets the swarm’s grace period to finish work in flight. The swarm then cancels the rest and waits for its cleanup before the member ends. `Budget.grace=0` skips the grace wait. A controller therefore sees a member’s end only after this drain.

Start background work from the running agent, not its factory. A factory that starts it makes the member `errored`. Work started outside [background()](https://inspect.aisi.org.uk/reference/inspect_ai.util.html#background) (an `asyncio` task, a process left running in the sandbox) is not stopped.

## How a member ends

A member’s status says how it ended:

| Status | Meaning |
|----|----|
| `submitted` | Its agent returned normally. Its output is its answer. A [persistent member](#persistent-members) is never `submitted` by a normal return: it ends `submitted` when it is stopped or ended while idle, or at the turn boundary after an answer, with its last answer as its output. |
| `unsubmitted` | Its agent returned with a `content_filter` or `model_length` stop reason, as [react()](https://inspect.aisi.org.uk/reference/inspect_ai.agent.html#react) does after repeated refusals or a context overflow. A persistent member also ends `unsubmitted` when its agent returns without an answer, for example when its `on_continue` returns `False` (a cut-off). |
| `stopped` | The swarm stopped it, for example when another member submitted or the swarm’s time cap was reached, or a controller ended it ([Controllers](./controllers.html.md#boundary-callbacks)). A member that reaches a turn boundary of its agent loop after the stop began ends there. |
| `share` | It used up its share of a split budget. |
| `member_limit:<type>` | It reached one of its own `limits`. |
| `member_limit:custom` | Its agent stopped itself by raising `LimitExceededError("custom", ...)` with no `source`, for example from `react(on_continue=...)`. The other members continue. |
| `swarm_cap` | Its call reached one of the swarm’s caps. |
| `cancelled` | It was cancelled, after the grace period of a stop or from outside the swarm. |
| `errored` | It raised any other error. The error ends the sample. |

For a member that is not persistent, a normal return counts as a submission for any agent other than [react()](https://inspect.aisi.org.uk/reference/inspect_ai.agent.html#react)’s two cases above. An agent that gives up in some other way and returns is treated as having answered. To make a normal return mean something stronger, such as a passed check, have the member’s builder raise a custom limit when it is not so ([Controllers](./controllers.html.md#stopping-on-the-first-proof)).

## Persistent members

A persistent member stays available after it answers, so a controller can give the same member several tasks with its context intact:

``` python
member(worker(), name="worker", persistent=True)
```

When a persistent member submits, or a `react(submit=False)` member gives a reply with no tool calls, the swarm records that as one of its *answers* and holds the member *idle* at its next turn boundary. The agent loop does not return. The controller hears [MemberIdle](./reference/inspect_swarm.html.md#memberidle) and gives the member its next task with `SwarmControl.wake()`, which appends the task to the member’s conversation ([Controllers](./controllers.html.md#persistent-members)). The member keeps its conversation, tool state and limits across these *activations*.

- Its limits span its whole life. Its member time limit does not run while it is idle.
- Every answer is a candidate for the swarm’s answer: `first` and `last` choose across all of them, and `choose_answer()` takes the member’s latest. An answer stays a candidate however the member ends later.
- A submission in a turn that also calls a [handoff()](https://inspect.aisi.org.uk/reference/inspect_ai.agent.html#handoff) is not an answer. Nor is a refusal (`content_filter`) or a reply cut off by the context window.
- A member stopped or ended while idle ends `submitted`. A member whose loop returns without an answer ends `unsubmitted`, its earlier answers still candidates.
- While it is idle, another member’s write that is news to it reaches the controller as [MemberNews](./reference/inspect_swarm.html.md#membernews), and the controller decides whether to wake it ([News for idle members](./controllers.html.md#news-for-idle-members)).

Only a member whose agent is a [react()](https://inspect.aisi.org.uk/reference/inspect_ai.agent.html#react), `react(submit=False)` or [deepagent()](https://inspect.aisi.org.uk/reference/inspect_ai.agent.html#deepagent) loop can persist, given directly or returned by a [member builder](#member-builders). The swarm refuses every other agent before it runs: a bridged member, a custom loop, [deepagent()](https://inspect.aisi.org.uk/reference/inspect_ai.agent.html#deepagent) with background dispatch, and an agent that calls a loop and then checks its result (`checked_solver()` in the examples). In a swarm with [shared_memory()](./reference/inspect_swarm.html.md#shared_memory), a persistent member must have [swarm_tools()](./reference/inspect_swarm.html.md#swarm_tools) in its own tools, not only in a subagent’s.

## A swarm of one

`swarm(members=react(), channels=[])` is a single agent run under the swarm’s rules, with the same records. Use it as the single-agent arm when you compare a swarm with one agent ([Scoring](./scoring.html.md#comparisons)). Unlike a plain agent, it returns an empty output when its member does not submit.

## Agents the swarm refuses

- ACP agents wait for their caller to drive them, so they are refused.
- Bridged members, which run a CLI agent in the sandbox through Inspect’s agent bridge, cannot have background work. See [Restrictions](./restrictions.html.md).
