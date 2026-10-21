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

- **Names.** A member is named after its agent (`react`, `deepagent`) unless you give `name`. With `count` above 1 the members are `name-1` to `name-k`. Default names are numbered across records ([Members that differ](#members-that-differ)). Names must be unique in the swarm. They cannot be `init`, `solvers` or `scorers`, and cannot contain `/` or `#`.
- **Limits.** `limits` takes token, cost, turn, message and time limits for that member alone. Working limits are refused.
- **A swarm of one.** `swarm(members=react(), channels=[])` is a single agent run under the swarm's rules, with the same records. Use it as the single-agent arm when you compare a swarm with one agent. Unlike a plain agent, it returns an empty output when its member does not submit.

### Members that differ

Members left on their default name are numbered across the whole roster, not per record. So one member with a different prompt and model takes two records:

```python
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

```python
prompts = ["Try algebra.", "Try brute force.", "Try a proof by cases."]
solver = swarm(members=[member(react, args={"prompt": p}) for p in prompts])
```

These are `react-1` to `react-3`. Names you give with `name` are not numbered across records, so give each record its own.

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

- When one member has its own sandbox, every member must have one, and no two can have the same one. The `filesystem` channel describes a shared sandbox, so it is refused: pass `channels=[]`, or `channels=[shared_memory()]` for a board the members share ([The shared-memory channel](#the-shared-memory-channel)). If the members' sandboxes each mount one shared volume, pass `channels=[filesystem(shared_volume=True)]` instead (see [Controllers and channels](#controllers-and-channels)).
- A member cannot see the sample's other environments, except those in its `visible_sandboxes`, such as a shared service. It reaches those only by name. Any member that lists a service can use it, so list only services every member may use.
- The task's scorer runs outside every member, where `sandbox()` is the sample's `default`. A scorer that grades the members' work names each member's sandbox, as in `sandbox("agent-1")`. The result record's `sandbox` field says which member used which.

### Background work

Native members support `deepagent(background=True)` and integer background limits. Work started with `background()` belongs to its member and uses its limits and sandbox. A member limit or swarm cap ends the background task that reached it. A swarm cap also starts the swarm's soft stop; other work gets the effective grace period to finish calls already in flight.

When the member's agent ends, its background work gets the swarm's grace period to finish work in flight. The swarm then cancels the rest and waits for its cleanup before the member ends. `Budget.grace=0` skips the grace wait.

Start background work from the running agent, not its factory. A factory that starts it makes the member `errored`.

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

`channels=` decides how members share information. There are two built-in channels: `filesystem`, the default, and `shared_memory` ([The shared-memory channel](#the-shared-memory-channel)). `filesystem` adds no tools. It tells each member, in its preamble, to share files in `/shared`, to read and append to `/shared/notes.md`, and to keep its own work in `/scratch/<member>`. It does not create these directories. Change the paths with `filesystem(...)`:

```python
from inspect_ai.agent import react

from inspect_swarm import filesystem, member, swarm

solver = swarm(
    members=member(react, count=4),
    channels=[filesystem(shared="/work/shared", scratch="/work/{member}")],
)
```

When members have their own sandboxes and each sandbox mounts one shared volume, for example a named volume in each member's compose service, use `filesystem(shared_volume=True)`. Set `shared=` to the absolute path where the volume is mounted; `notes=` must be inside it. The preamble then says that `shared` and the notes file in it are the same in every member's sandbox, and gives no scratch directory, since each sandbox is already private. The swarm cannot check that the volume is mounted, and it refuses `shared_volume=True` when members share the sample's sandbox.

```python
solver = swarm(
    members=member(react, name="agent", count=2, sandbox="{member}"),
    channels=[filesystem(shared_volume=True)],
)
```

`channels=[]` gives members no channel instructions. Members share the sample's sandbox whatever the channels, unless they have their own ([Sandboxes](#sandboxes)).

You can write your own with `@swarm_controller` and `@swarm_channel`. See their docstrings.

### The shared-memory channel

`shared_memory()` gives the swarm one board of files that every member reads and writes with a `shared_memory` tool. The tool has the commands of Inspect's `memory()` tool, over files under `/shared_memory`. A view shows what other members wrote, marked as information from a peer, and each result names the files others have changed since the member last viewed them. Each sample has its own board, which is gone when the swarm ends.

Members get the tool from `swarm_tools()`, which gives a member the tools of the swarm's channels. Put it in the member's tools, in the factory or the built form:

```python
from inspect_ai.agent import deepagent, react
from inspect_ai.tool import bash

from inspect_swarm import filesystem, member, shared_memory, swarm, swarm_tools

solver = swarm(
    members=[
        member(react, args={"tools": [bash(), swarm_tools()]}, name="solver", count=3),
        member(deepagent(tools=[bash(), swarm_tools()]), name="reviewer"),
    ],
    channels=[filesystem(), shared_memory()],
)
```

Any agent that takes a `ToolSource` in its tools can take `swarm_tools()`. It gives no tools when the swarm has no channel with tools, or outside a swarm, so one member definition serves every arm of an experiment. A member's synchronous subagents that have the member's tools, such as `deepagent()`'s `general()` subagent, use the board as the member.

This swarm keeps the `filesystem` channel beside the board. `channels=[shared_memory()]`, or `channels=["shared_memory"]`, replaces it: members still share the sample's sandbox, but their preamble no longer mentions `/shared`. Members with their own sandboxes can use the board with no shared volume. Bridged members cannot use the channel.

The number and size of files and each member's write rate are limited. See `shared_memory()`'s docstring for the limits, their defaults and the rules members follow.

The swarm logs the members' writes and views as `InfoEvent`s with source `inspect_swarm` and type `comm`, so the log keeps the board's whole history. The [metrics](#metrics-and-analysis) count these events for each member.

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

- `inspect_swarm:ledger`: the swarm's accounting. Its budget, caps, the usage of each member, the controller and the final step, model calls that errored or reported no usage, unpriced models, what stopped the swarm, and whether the sample resumed from a checkpoint. See [The ledger](#the-ledger).
- `inspect_swarm:metrics`: tokens, cost and wall-clock time for the swarm, and tokens, cost and running time for each member. Also members with no completed model call (`never_acted`), counts of completed model calls for the chosen member (`steps_to_answer`) and the busiest member (`longest_member_steps`), and the longest run of identical tool calls by each member (`null` for a bridged member, see [Restrictions](#restrictions)). These call counts include cache replays and exclude failed or cancelled attempts.

Each member's metrics also have `comm`, by channel name, for each channel with records, such as `shared_memory`: `records` and `record_bytes`, its writes delivered to the channel and their size; `rejections`, its writes the bus refused, by the control that refused them, such as `rate`; and `reads` and `listings`, its views of files and of directories. A member's subagents count as the member. Every member has an entry for every such channel the swarm opened, zeros included. A swarm with none (`filesystem` only, or `channels=[]`) has `comm` `{}` for every member: communication through files is observed, not recorded, so it is not reported as zero.

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

The columns:

- `swarm_cost`: the sample's priced cost in dollars, from its `model_usage`, including scorer calls. Tokens are in `SampleSummary`'s `total_tokens`.
- `swarm_complete`: no call is known to be missing from `swarm_cost`. The ledger marks the swarm's interval complete, and every model in the sample's `model_usage` is priced. Calls outside the swarm, including scorer calls, are not checked. `False` for a sample with no ledger.
- `swarm_members`: members in the roster.
- `swarm_resumed`: the sample resumed from a checkpoint, so the swarm restarted its caps from zero. `False` for a sample with no ledger.
- `swarm_cost_cap`, `swarm_token_cap`, `swarm_time_cap`: the swarm's caps. The time cap is in seconds.
- `swarm_errored`, `swarm_no_usage`, `swarm_unpriced`: model calls in the swarm that errored or were cancelled (retries included), that completed with no usage, and whose usage has no cost. Each makes the cost a lower bound.
- `swarm_unpriced_models`: models with tokens and no cost, from the ledger and the sample's `model_usage`, as a JSON list.

The columns read from the ledger are null for a sample with no ledger. A sample that runs two swarms keeps only the last one's ledger, while its `model_usage` covers both. The columns read the sample's `store` and `model_usage`, so do not exclude either with `samples_df(exclude_fields=...)`.

The columns hold no per-member data. `swarm_ledger()` reads a sample's whole ledger as a `SwarmLedger`, whose `members` are `MemberLedger`s with a `Meter` and `Calls` each. All are in `inspect_swarm.analysis`, and `SwarmLedger.model_json_schema()` describes every field. For example, each member's cost:

```python
from inspect_ai.log import read_eval_log

from inspect_swarm.analysis import swarm_ledger

log = read_eval_log("logs/my-eval.eval")
for sample in log.samples or []:
    ledger = swarm_ledger(sample)
    if ledger is not None:
        for m in ledger.members:
            print(sample.id, sample.epoch, m.name, m.meter.cost)
```

### The ledger

The ledger of a two-member swarm with a cost cap of $1 and a time cap of 600 seconds:

```json
{
  "version": 1,
  "budget": {"cost": 1.0, "tokens": null, "time": 600.0, "reserve": 0.05, "split": "pool", "grace": 120.0},
  "caps": {"cost": 1.0, "tokens": null, "time": 600.0},
  "sample_limits": {"token": null, "cost": null, "message": null, "turn": null, "working": null, "time": null},
  "members": [
    {
      "name": "react-1",
      "status": "submitted",
      "bridged": false,
      "meter": {"usage": {"input_tokens": 1200, "output_tokens": 300, "total_tokens": 1500, "input_tokens_cache_write": null, "input_tokens_cache_read": null, "reasoning_tokens": null, "total_cost": 0.0081}, "cost": 0.0081, "suspended_cost": 0.0},
      "calls": {"calls": 1, "cache_replays": 0, "errored": 0, "no_usage": 0, "unpriced": 0},
      "cancelled": false,
      "background_started": 0,
      "background_cancelled": 0
    },
    {
      "name": "react-2",
      "status": "submitted",
      "bridged": false,
      "meter": {"usage": {"input_tokens": 1150, "output_tokens": 280, "total_tokens": 1430, "input_tokens_cache_write": null, "input_tokens_cache_read": null, "reasoning_tokens": null, "total_cost": 0.00765}, "cost": 0.00765, "suspended_cost": 0.0},
      "calls": {"calls": 1, "cache_replays": 0, "errored": 0, "no_usage": 0, "unpriced": 0},
      "cancelled": false,
      "background_started": 0,
      "background_cancelled": 0
    }
  ],
  "runtime": {"usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "input_tokens_cache_write": null, "input_tokens_cache_read": null, "reasoning_tokens": null, "total_cost": null}, "cost": 0.0, "suspended_cost": 0.0},
  "final": {"usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "input_tokens_cache_write": null, "input_tokens_cache_read": null, "reasoning_tokens": null, "total_cost": null}, "cost": 0.0, "suspended_cost": 0.0},
  "swarm": {"usage": {"input_tokens": 2350, "output_tokens": 580, "total_tokens": 2930, "input_tokens_cache_write": null, "input_tokens_cache_read": null, "reasoning_tokens": null, "total_cost": 0.01575}, "cost": 0.01575, "suspended_cost": 0.0},
  "calls": {"calls": 2, "cache_replays": 0, "errored": 0, "no_usage": 0, "unpriced": 0},
  "unpriced_calls_recorded": 0,
  "sample_delta": {"input_tokens": 2350, "output_tokens": 580, "total_tokens": 2930, "input_tokens_cache_write": null, "input_tokens_cache_read": null, "reasoning_tokens": null, "total_cost": 0.01575},
  "unattributed": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "input_tokens_cache_write": null, "input_tokens_cache_read": null, "reasoning_tokens": null, "total_cost": 0.0},
  "unpriced_models": [],
  "native_compaction_classes": [],
  "overshoot": {},
  "stop_reason": "all_done",
  "ended_by": null,
  "interrupted": false,
  "final_cancelled": false,
  "resumed": false,
  "total_complete": true,
  "attribution_complete": true
}
```

- `budget` is the `Budget` as given. `caps` are the caps the swarm applied, derived from the sample's limits or given: `cost` in dollars, `tokens`, and `time` in seconds, `null` for no cap. `sample_limits` are the sample's limits when the swarm started.
- `members` has one entry per member, in roster order. `status` is as in [How a member ends](#how-a-member-ends), `null` for a member never started. `cancelled` means the swarm or the sample cancelled the member, so calls it had in flight are unknown. A hard stop during a background drain also sets `cancelled`; the agent's returned status is kept. `background_started` counts the tasks the member started with `background()`, and `background_cancelled` those cancelled before they finished, whose calls in flight are unknown too.
- `meter` is the usage and cost of what a member did. `suspended_cost` is the part spent while token counting was suspended, such as approval calls.
- `calls` counts model calls: `calls` completed ones other than cache replays, `cache_replays` replayed from cache at no charge, `errored` attempts that errored or were cancelled (retries included), `no_usage` completed with no usage, and `unpriced` completed with usage but no cost.
- `runtime` meters the controller's own calls, `final` the final step's (the default first-submission answer makes none), and `swarm` everything in the swarm: members, runtime and final step. The top-level `calls` counts every call in the swarm. `unpriced_calls_recorded` counts the unpriced calls the swarm's meter recorded.
- `sample_delta` is the sample's usage while the swarm ran. `unattributed` is `sample_delta` minus `swarm`: usage no meter holds, such as the tokens of approval calls.
- `unpriced_models` are models with tokens but no cost. `native_compaction_classes` are loaded model classes that can compact a conversation natively, which logs nothing, so their usage may be missing.
- `overshoot` is how far usage went past each cap, for caps it passed. Calls in flight at a stop can carry usage past a cap.
- `stop_reason` is why the swarm stopped itself: a controller's `all_done` (as `leaderless` returns), `verified` or `quiescence`, `controller:<reason>` for any other reason a controller returns, `swarm_cap` or `swarm_time`. `ended_by` is what ended it from outside: `sample_limit:<type>`, `operator` for an interrupt, or `unknown`. Each is `null` when it did not happen. Both are set when a sample limit ends the swarm during its own stop.
- `interrupted` means an error (a member's, the controller's or the final step's) or a cancellation ended the swarm before it finished, `final_cancelled` that the final step was cancelled, and `resumed` that the sample resumed from a checkpoint.
- `total_complete` means no call is known to be missing from the totals: nothing was cancelled or interrupted, every call reported priced usage, and no model class can compact natively. Otherwise the totals are lower bounds. `attribution_complete` adds that `unattributed` is zero.

## Logs and the store

Spans inside a swarm log no `StoreEvent`s. The swarm's span logs the net change to the store, which says what changed but not which member changed it. Members' `ToolEvent`s are the record of what they did, except for bridged members ([Restrictions](#restrictions)).

If an error, limit or cancellation escapes the swarm and ends the sample, the log may have no `StoreEvent` for the swarm's writes. `sample.store` is always the record of the final store.

## Restrictions

- **No background work in bridged members.** A member cannot combine background work with the agent bridge. `background()` raises in a bridged member, and a member that uses the bridge after starting background work ends `errored`.
- **No ACP agents.** ACP agents wait for their caller to drive them, so they are refused.
- **Tool state is per member.** Each member runs in its own tool-state scope. Built-in tools that keep state in the store (`memory()`, `bash_session()`, `web_browser()`, the skill tool) give each member its own state when their `instance` is left at the default.
  - Tool state that a setup solver prepared at the default instance is not carried into the members. They start with fresh state.
  - Tasks whose scorers read a member's tool state from the store are not supported. The keys inside a member's scope differ from the default ones.
- **Memory is private by default.** The default `memory()`, and `deepagent()`'s memory, is private to each member. For a board members share, use the `shared_memory()` channel ([The shared-memory channel](#the-shared-memory-channel)): the swarm logs its writes and views as `comm` events and counts them in the metrics. A `memory()` tool with an explicit instance, as in `react(tools=[memory(instance="board")])`, is also shared. Such an instance is a channel between members outside the swarm's channels: members' `ToolEvent`s record its use, and nothing else does.
- **Bridged members leave no tool events.** A bridged member runs a CLI agent in the sandbox through Inspect's agent bridge. `inspect_swe` agents count as bridged by default; pass `member(..., bridged=True)` for other agents that use the bridge. The CLI's own tool calls do not emit Inspect `ToolEvent`s, so the tool-event evidence described here does not cover them, and `longest_identical_tool_run` is `null` for bridged members.
- **Shared files are observed, not audited.** Tool calls are evidence that members may have communicated through the filesystem, not a complete record. Processes a member starts in the sandbox can read and write files between logged tool calls, and after the member has ended. An experiment that attributes communication to the filesystem must state this limit, or add its own instrumentation, such as filesystem snapshots or audit logs in the sandbox.
- **The result record belongs to the swarm.** Only the swarm writes `inspect_swarm:result`. Task code (tools, member builders, controllers, scorers) must not write it. Any member's code can read it while the swarm runs, so a tool that reads it gives members a view of each other's state, status and time. That is a channel between members outside the swarm's channels, and the swarm records no evidence of it.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md). The designs behind the swarm are in [design/](design/).
