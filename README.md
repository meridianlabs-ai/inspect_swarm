# Inspect Swarm

> [!WARNING]
> **Experimental.** Inspect Swarm is under active development. It has no stable API, no releases, and may change or be withdrawn without notice.

Inspect Swarm runs a swarm of agents on one sample of an [Inspect](https://inspect.aisi.org.uk) task. A swarm is an Inspect agent, so it replaces the task's solver and the task's scorer scores the swarm's answer.

## Installation

Inspect Swarm has no releases yet. Install it from GitHub:

```bash
pip install git+https://github.com/meridianlabs-ai/inspect_swarm
```

or, in a uv project:

```bash
uv add "inspect_swarm @ git+https://github.com/meridianlabs-ai/inspect_swarm"
```

For now it depends on inspect_ai's `main` branch, which has features no inspect_ai release has yet. Installing Inspect Swarm therefore installs inspect_ai from GitHub `main`, replacing a released inspect_ai in that environment. It needs Python 3.11 or later.

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

Each sample runs four `react()` members, `react-1` to `react-4`, at the same time. Each starts with the sample's input and a short preamble: its name, the number of members, and how to share files. The swarm ends when all members have ended, and its answer is the first submission. The task's scorer scores it as it would score a single agent.

`--solver` and `eval(solver=...)` replace the task's whole solver, including any system message or tools it set up, so give the members their tools and prompt. Members use the eval's model unless they are given one. The task's `setup` solver still runs first.

## Members

A member is an agent registered with `@agent`, such as `react()` or `deepagent()`. Give it in one of two forms:

- **Factory form**: the factory and its arguments, `member(react, args={...})`. The swarm calls the factory once for each member in each sample, so every member gets its own agent. `count` makes several members from one record.
- **Built form**: an agent you have built, `member(react(...))`, or the agent alone. It is one member. The same agent object cannot be in two members.

```python
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

This swarm has four members: `solver-1` to `solver-3` and `reviewer`. The role is added to the member's preamble.

- **Names.** A member is named after its agent (`react`, `deepagent`) unless you give `name`. With `count` above 1 the members are `name-1` to `name-k`. Names must be unique in the swarm. They cannot be `init`, `solvers` or `scorers`, and cannot contain `/` or `#`.
- **Limits.** `limits` takes token, cost, turn, message and time limits for that member alone. Working limits are refused.
- **A swarm of one.** `swarm(members=react(), channels=[])` is a single agent run under the swarm's rules, with the same records. Use it as the single-agent arm when you compare a swarm with one agent. Unlike a plain agent, it returns an empty output when its member does not submit.

Each member's events are in a timeline named after it, so the log viewer can show one member's trajectory.

### Sandboxes

Members share the sample's sandbox by default. With `sandbox`, a member works in a sandbox environment of its own, which becomes its default, so `sandbox()` in its tools returns it. `{member}` stands for the member's name, so `sandbox="{member}"` gives each copy of a counted member its own:

```python
from inspect_ai.agent import react
from inspect_ai.tool import bash

from inspect_swarm import member, swarm

solver = swarm(
    members=member(
        react, args={"tools": [bash()]}, name="agent", count=2, sandbox="{member}"
    ),
    channels=[],
)
```

`agent-1` and `agent-2` run in the environments of those names. The sample's sandbox configuration provides them (for Docker, a compose service for each), and the swarm checks that they exist when it starts.

- When one member has its own sandbox, every member must have one, and no two can have the same one. The `filesystem` channel describes a shared sandbox, so it is refused: pass `channels=[]`.
- A member cannot see the sample's other environments, except those in its `visible_sandboxes`, such as a shared service. It reaches those only by name. Any member that lists a service can use it, so list only services every member may use.
- The task's scorer runs outside every member, where `sandbox()` is the sample's `default`. A scorer that grades the members' work names each member's sandbox, as in `sandbox("agent-1")`. The result record's `sandbox` field says which member used which.

### How a member ends

A member's status says how it ended:

| Status | Meaning |
|---|---|
| `submitted` | Its agent returned normally. Its output is its answer. |
| `unsubmitted` | Its agent returned with a `content_filter` or `model_length` stop reason, as `react()` does after repeated refusals or a context overflow. |
| `stopped` | The swarm stopped it, for example when the swarm's time cap was reached. |
| `share` | It used up its share of a split budget. |
| `member_limit:<type>` | It reached one of its own `limits`. |
| `member_limit:custom` | Its agent stopped itself by raising `LimitExceededError("custom", ...)` with no `source`, for example from `react(on_continue=...)`. The other members continue. |
| `swarm_cap` | Its call reached one of the swarm's caps. |
| `cancelled` | It was cancelled, after the grace period of a stop or from outside the swarm. |
| `errored` | It raised any other error. The error ends the sample. |

## Controllers and channels

`controller=` decides which members start and when the swarm is done. The only built-in controller is `leaderless`, the default. It starts every member with the sample's input and ends the swarm when all have ended.

`channels=` decides how members share information. The only built-in channel is `filesystem`, the default. It adds no tools. It tells each member, in its preamble, to share files in `/shared`, to read and append to `/shared/notes.md`, and to keep its own work in `/scratch/<member>`. It does not create these directories. Change the paths with `filesystem(...)`:

```python
from inspect_ai.agent import react

from inspect_swarm import filesystem, member, swarm

solver = swarm(
    members=member(react, count=4),
    channels=[filesystem(shared="/work/shared", scratch="/work/{member}")],
)
```

`channels=[]` gives members no channel instructions. Members share the sample's sandbox whatever the channels, unless they have their own ([Sandboxes](#sandboxes)).

You can write your own with `@swarm_controller` and `@swarm_channel`. See their docstrings.

## Budget

By default the swarm takes its budget from the sample's cost, token and time limits. It stops its members at 95% of each, holding back 5% for calls already in flight. After a stop, members have up to `grace` seconds (default 120) to finish calls already in flight. If the sample has a time limit, this period is capped at half its remaining time. Members still running are then cancelled.

Set the caps yourself with `Budget`:

```python
from inspect_ai.agent import react

from inspect_swarm import Budget, member, swarm, swarm_resources

solver = swarm(
    members=member(react, args={"tools": [swarm_resources()]}, count=4),
    budget=Budget(cost=20.0, time=1800, split="equal"),
)
```

`split` divides the cost and token caps between members: `"pool"` (default) shares one budget, `"equal"` gives each member an equal share, and a tuple of weights gives one share per member in roster order. A member that uses up its share stops, and its unused share is not given to the others.

When the swarm has a cap, each member's preamble says how the budget is split. The `swarm_resources()` tool, as in the example, lets a member check the swarm's caps, how much all members have used, and its own share. Code inside a member can read the same with `swarm_budget()`. A tool that reads `sample_limits()` shows the sample's limits, not the swarm's caps.

A cost cap needs cost data for every member's model. The swarm checks this when it starts and raises an error naming the models that have none. Configure prices with `set_model_cost()` or `--model-cost-config`.

See [docs/budget.qmd](docs/budget.qmd) for splits and for sweeping agent count and budget as task parameters.

## The answer and the result record

The swarm's answer is the **first** submission: the member that submitted earliest, by the swarm's clock, with ties broken by roster order. A member that ended any other way is never chosen. When no member submitted, the answer is empty.

The swarm's output is the chosen member's `ModelOutput`, unchanged, so the scorer sees what it would have seen had that member run alone. The sample's messages are its input followed by the answer's message, whose `metadata["inspect_swarm"]` names the chosen member. Each member's own conversation is in its timeline.

The swarm keeps a record of what it did in the sample store under `inspect_swarm:result`:

```json
{
  "version": 1,
  "members": [
    {"name": "react-1", "role": null, "state": "ended", "status": "submitted", "submitted": true, "time": 41.2, "sandbox": null},
    {"name": "react-2", "role": null, "state": "ended", "status": "swarm_cap", "submitted": false, "time": 57.9, "sandbox": null}
  ],
  "final": {"mode": "first", "member": "react-1", "reason": null, "provisional": false}
}
```

A member's `state` is `pending` (never started), `running` or `ended`. `time` is seconds from the swarm's start to the member's end, and `sandbox` is the member's own sandbox, or `null` when it shares the sample's. With no answer, `final.member` is `null` and `final.reason` is `"no_submissions"`. The record is updated as members start and end. If a limit or error ends the sample before the swarm finishes, the record keeps the first submission so far with `provisional: true`, and that submission is the sample's output.

## Metrics and analysis

When the swarm ends it writes two more store entries, each also logged as an `InfoEvent` with source `inspect_swarm`:

- `inspect_swarm:ledger`: the swarm's accounting. Its budget, caps, the usage of each member, the controller and the final step, model calls that errored or reported no usage, unpriced models, what stopped the swarm, and whether the sample resumed from a checkpoint.
- `inspect_swarm:metrics`: tokens, cost and wall-clock time for the swarm, and tokens, cost and running time for each member. Also members with no completed model call (`never_acted`), counts of completed model calls for the chosen member (`steps_to_answer`) and the busiest member (`longest_member_steps`), and the longest run of identical tool calls by each member (`null` for a bridged member, see [Restrictions](#restrictions)). These call counts include cache replays and exclude failed or cancelled attempts.

`inspect_swarm.analysis.SwarmColumns` adds each sample's realized cost and ledger data to Inspect's `samples_df()`. `samples_df()` needs pandas and pyarrow, which neither install above includes:

```bash
pip install "pandas>=2.1.0" "pyarrow>=10.0.1"
```

or, in a uv project:

```bash
uv add "pandas>=2.1.0" "pyarrow>=10.0.1"
```

This example uses a task with a scorer named `match`. Replace `score_match` with your task's score column (`score_<scorer name>`):

```python
from inspect_ai.analysis import SampleSummary, samples_df
from inspect_ai.scorer import value_to_float

from inspect_swarm.analysis import SwarmColumns

df = samples_df("logs", columns=SampleSummary + SwarmColumns)
df = df[~df["swarm_resumed"]]
df["score"] = df["score_match"].map(value_to_float(), na_action="ignore")
df.groupby("log")[["score", "swarm_cost"]].mean()
```

`swarm_cost` is the sample's recorded priced cost, including scorer calls. `swarm_complete` requires the swarm's ledger to mark its interval complete and all recorded model usage to be priced. It does not check for missing usage outside the swarm, including scorer calls. The `SwarmColumns` docstring lists every column.

## Logs and the store

Spans inside a swarm log no `StoreEvent`s. The swarm's span logs the net change to the store, which says what changed but not which member changed it. Members' `ToolEvent`s are the record of what they did, except for bridged members ([Restrictions](#restrictions)).

If an error, limit or cancellation escapes the swarm and ends the sample, the log may have no `StoreEvent` for the swarm's writes. `sample.store` is always the record of the final store.

## Restrictions

- **No background subagents.** A `deepagent()` member with `background=True` or an integer is refused. So are ACP agents.
- **Tool state is per member.** Each member runs in its own tool-state scope. Built-in tools that keep state in the store (`memory()`, `bash_session()`, `web_browser()`, the skill tool) give each member its own state when their `instance` is left at the default.
  - Tool state that a setup solver prepared at the default instance is not carried into the members. They start with fresh state.
  - Tasks whose scorers read a member's tool state from the store are not supported. The keys inside a member's scope differ from the default ones.
- **Memory is private by default.** The default `memory()`, and `deepagent()`'s memory, is private to each member. For a board members share, give the tool an explicit instance, as in `react(tools=[memory(instance="board")])`. A shared instance is a channel between members outside the swarm's channels: members' `ToolEvent`s record its use, and nothing else does.
- **Bridged members leave no tool events.** A bridged member runs a CLI agent in the sandbox through Inspect's agent bridge. `inspect_swe` agents count as bridged by default; pass `member(..., bridged=True)` for other agents that use the bridge. The CLI's own tool calls do not emit Inspect `ToolEvent`s, so the tool-event evidence described here does not cover them, and `longest_identical_tool_run` is `null` for bridged members.
- **Shared files are observed, not audited.** Tool calls are evidence that members may have communicated through the filesystem, not a complete record. Processes a member starts in the sandbox can read and write files between logged tool calls, and after the member has ended. An experiment that attributes communication to the filesystem must state this limit, or add its own instrumentation, such as filesystem snapshots or audit logs in the sandbox.
- **The result record belongs to the swarm.** Only the swarm writes `inspect_swarm:result`. Task code (tools, member builders, controllers, scorers) must not write it. Any member's code can read it while the swarm runs, so a tool that reads it gives members a view of each other's state, status and time. That is a channel between members outside the swarm's channels, and the swarm records no evidence of it.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md). The designs behind the swarm are in [design/](design/).
