# Inspect Swarm

> **Experimental.** Everything in Inspect Swarm is experimental and may change in any release without notice, including `swarm()`, `member()`, `Budget`, the built-in controller and channel names, and the record and evidence formats. No support is guaranteed.

Inspect Swarm runs a swarm of agents on one sample of an [Inspect](https://inspect.aisi.org.uk) task. A swarm is an Inspect agent, so it replaces the task's solver and the task's scorer scores the swarm's answer.

Documentation: <https://meridianlabs-ai.github.io/inspect_swarm>. Runnable examples are in [`examples/`](https://github.com/meridianlabs-ai/inspect_swarm/tree/main/examples).

## Installation

Install it from PyPI:

```bash
pip install inspect_swarm
```

or, in a uv project:

```bash
uv add inspect_swarm
```

To install the latest development version from GitHub:

```bash
pip install git+https://github.com/meridianlabs-ai/inspect_swarm
```

or, in a uv project:

```bash
uv add "inspect_swarm @ git+https://github.com/meridianlabs-ai/inspect_swarm"
```

It needs inspect_ai 0.3.283 or later and Python 3.11 or later.

## Run a swarm on an existing task

Pass `swarm()` as the solver. From Python:

```python
from inspect_ai import eval
from inspect_ai.agent import react

from inspect_swarm import member, swarm

from my_tasks import my_task

eval(
    my_task(),
    solver=swarm(members=member(react, count=4)),
    model="anthropic/claude-sonnet-4-5",
)
```

From the command line:

```bash
inspect eval my_tasks.py --solver inspect_swarm/swarm -S 'members={factory: react, count: 4}'
```

Each sample runs four `react()` members, `react-1` to `react-4`, at the same time. Each starts with the sample's input and a short preamble: its name, the number of members, and how to share files. By default the swarm stops at the first submission and uses it as its answer. It stops the remaining members. To let every member run to its end, use `leaderless(stop_on_submission=False)`. The task's scorer scores it as it would score a single agent.

`--solver` and `eval(solver=...)` replace the task's whole solver, including any system message or tools it set up, so give the members their tools and prompt. Members use the eval's model unless they are given one. The task's `setup` solver still runs first.

## What a swarm has

- Members: agents registered with `@agent`, given as `member()` records with a name, role, count, limits and sandbox. A member builder, an `@agent` function that builds the member's agent, is where it gets the swarm's tools. See [Members](https://meridianlabs-ai.github.io/inspect_swarm/members.html).
- A controller: which members start and when the swarm stops. The default, `leaderless`, starts every member and stops at the first submission. See [Controllers](https://meridianlabs-ai.github.io/inspect_swarm/controllers.html), which also covers writing one.
- Channels: how members share information. `filesystem`, the default, describes a shared directory in the sample's sandbox. `shared_memory` is a board of files with notices and `swarm_wait`. See [Channels](https://meridianlabs-ai.github.io/inspect_swarm/channels.html).
- A budget: caps on cost, tokens and time, taken from the sample's limits by default, shared or split between members. See [Budgets](https://meridianlabs-ai.github.io/inspect_swarm/budget.html).
- Sandboxes: members share the sample's sandbox, or each works in its own. See [Sandboxes](https://meridianlabs-ai.github.io/inspect_swarm/sandboxes.html).

## The shared-memory channel

`shared_memory()` gives the swarm one board of files that every member reads and writes with a `shared_memory` tool, and a `swarm_wait` tool to wait for other members instead of polling. Members get the tools from `swarm_tools()`, and notices of other members' changes between turns from `swarm_on_continue()`. Call both inside a member builder registered with `@agent`, and pass the builder to `member()` without calling it. The log records a builder by name and arguments, so the swarm can be rebuilt from the log; it cannot rebuild `swarm_tools()` or `swarm_on_continue()` passed in `args` or in an agent you built.

```python
from inspect_ai.agent import Agent, agent, react
from inspect_ai.tool import bash

from inspect_swarm import member, shared_memory, swarm, swarm_on_continue, swarm_tools


@agent
def worker() -> Agent:
    return react(tools=[bash(), swarm_tools()], on_continue=swarm_on_continue())


solver = swarm(members=member(worker, name="w", count=3), channels=[shared_memory()])
```

At each turn boundary where other agents have changed files since its last notice, a member gets one notice, such as:

```
[Swarm notice] 1 shared memory file changed since you last viewed it (f-1 new; by w-1). View one with `shared_memory(command="view", file_id="f-1")`, or list them with `path="/shared_memory"`.
```

The swarm logs every write, view, notice and wait. See [Shared Memory](https://meridianlabs-ai.github.io/inspect_swarm/shared-memory.html) for the tool's commands, waiting, subagents, limits, monitoring with Inspect Sentinel, and migrating from `memory(instance=...)`.

## Results

By default the swarm's answer is the first submission. `leaderless(stop_on_submission=False, final="last")` makes it the latest, and a controller of your own can choose it. The swarm's output is the chosen member's `ModelOutput`, unchanged. The swarm keeps three records in the sample store:

- `inspect_swarm:result`: each member's state and status, and which member's output is the answer. Only the swarm writes it.
- `inspect_swarm:ledger`: the swarm's caps and each member's usage and cost.
- `inspect_swarm:metrics`: tokens, cost and time, and counts of how each member acted, communicated and waited.

`inspect_swarm.analysis` adds the swarm's cost and ledger data to Inspect's `samples_df()`. See [Scoring](https://meridianlabs-ai.github.io/inspect_swarm/scoring.html), [Analysis](https://meridianlabs-ai.github.io/inspect_swarm/analysis.html) and [Restrictions](https://meridianlabs-ai.github.io/inspect_swarm/restrictions.html).

## For agents

The documentation is also published as Markdown for agents:

- <https://meridianlabs-ai.github.io/inspect_swarm/llms.txt>: an index of the pages, with a line on what each covers.
- <https://meridianlabs-ai.github.io/inspect_swarm/llms-guide.txt>: the user guide as one Markdown file.
- <https://meridianlabs-ai.github.io/inspect_swarm/llms-full.txt>: the user guide and the API reference as one Markdown file.

## Development

See [CONTRIBUTING.md](https://github.com/meridianlabs-ai/inspect_swarm/blob/main/CONTRIBUTING.md). The documentation's source is in [docs/](https://github.com/meridianlabs-ai/inspect_swarm/tree/main/docs); `make docs` builds it. The designs behind the swarm are in [design/](https://github.com/meridianlabs-ai/inspect_swarm/tree/main/design).
