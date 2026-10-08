# Inspect Swarm: limits and budgets

Status: proposed, 2026-10-08. Issue: none. Author: agent (Claude), reviewed by Codex; see the PR.

This is a deeper-dive design on one topic of [swarm.md](swarm.md), the high-level design: how Inspect's limits apply to a swarm and its members, how the swarm stops when its budget runs out, and how it records what was actually spent. It replaces the sketches in swarm.md's [Limits and cost](swarm.md#limits-and-cost) and [Observer: accounting](swarm.md#observer-evidence-accounting-and-metrics) where the two differ, and swarm.md links here.

It respects the decisions Ransom took on swarm.md (2026-10-07). Those that bear on this topic:
- inspect_ai limits stay soft, and nothing here depends on making them hard;
- inspect_ai internals may be used;
- Python 3.11+;
- M1, then M2, then the rest in any order;
- members share the sample's sandbox by default.

Three sibling deep dives run in parallel and are referenced, not designed, here: the ORBIT relationship, scoring (the result contract and per-member scores), and inter-agent communication (the bus, delivery, storm controls).

Code references are to inspect_ai `main` at `aa20052a` (2026-10-06), the commit swarm.md cites, and to inspect_swe at `a54461e7`. The spikes below ran against inspect_ai `fccfb298` (2026-10-07), installed in this repository's environment; its only source change from `aa20052a` is in `log/_convert.py`, so the line numbers hold for both. Paths are relative to the inspect_ai root unless noted.

## Why

swarm.md settled the principles: a swarm-wide cap set the same way as a single agent's limit, caps as soft stopping rules, a realized-cost ledger for comparisons, a best-effort reserve for the final answer. It left the mechanics open, and checking them against the code turned up behaviour the high-level design did not account for:

- **Two limit kinds behave unlike token and cost.** A nested `working_limit()` is never checked, and the sample's working time collapses when members wait on each other's model connections. A `turn_limit(N)` pays for N+1 generations.
- **A limit hit inside a bridged agent ends the whole sample**, whichever node it came from. A member limit or a swarm cap on a Claude Code member would end its siblings too.
- **A limit hit inside a tool does not stop the member.** It becomes a tool error, and the member stops at its next model call.
- **Cancelling members to stop them loses their in-flight usage.** A cancelled call records nothing, so the ledger can only mark it unknown.
- **Per-member attribution from model events misses native compaction**, which swarm.md had to leave unattributed.

Each of these changes what the swarm's budget code must do. The users who need it are the capability-scaling experiments (swarm.md's first user group): their comparisons rest on the cap being set the same way in every arm and on the ledger being right.

## Goals and non-goals

### Goals

- Define, for each Inspect limit kind (token, cost, turn, message, time, working), what it means for a swarm and for a member, and which the swarm accepts as a cap, a member limit or a split.
- One budget parameter whose default derives the swarm's cap from the sample's own limits, so a swarm arm and a single-agent arm differ only in the solver.
- A stopping procedure that keeps in-flight usage known where it can, bounds overshoot, and works the same for native and bridged members.
- An exhaustion policy: which errors the swarm recovers from, which it propagates, and how.
- A reserve for the final step, with the conditions under which it holds.
- A ledger whose per-member figures cover everything a member's work spends, with stated coverage rules and claims.
- Budget splits (shared pool, equal shares, weights) as a sweepable task parameter.
- The interactions with deepagent subagents, bridged members and compaction, verified against the code.
- No inspect_ai changes.

### Non-goals

- Hard limits, or reserving budget for calls in flight (decision: Ransom, 2026-10-07).
- Per-member waiting-time and latency metrics. They belong to the metrics work in M1 ([Metrics](swarm.md#observer-evidence-accounting-and-metrics)); this design says only why a working limit cannot stand in for them.
- Reconciling against provider billing.
- Message and storm budgets on the bus (the communication deep dive), and per-member scores (the scoring deep dive).
- Checkpoint and resume of a swarm's budget state ([Not this design](#not-this-design)).

## Current behaviour

This section covers only what the design depends on. Claims marked *spike* were checked by running code against inspect_ai `fccfb298` with `mockllm` returning 50 tokens per call; the scripts are summarised with each claim and become tests ([Testing](#testing)).

### Limit trees

- Each limit kind is a tree of nodes held in a ContextVar; a `with` block pushes a child of the current leaf (`src/inspect_ai/util/_limit.py:957-1003`). A task spawned inside the block copies the context, so it sees the same leaf. That is how a node opened around a group of members meters all of them, as swarm.md's spike showed.
- Token, cost and turn usage is recorded on the leaf and every ancestor, and checked root to leaf, so when several nodes are exceeded the outermost raises (`_limit.py:1076-1091`, `:1163-1178`, `:1247-1262`).
- `apply_limits()` (used by `run()`) catches a `LimitExceededError` only when its `source` is one of the limits it applied (`_limit.py:150-186`, `src/inspect_ai/agent/_run.py:96-111`). Everything else propagates.
- A node's limit can be changed while it is open through its `limit` setter, without a check (`_limit.py:1066-1074`, `:1237-1245`). The next check uses the new value.

### Each limit kind

| Kind | Recorded | Checked | Below the sample |
|---|---|---|---|
| token | after each completed call, before the check (`src/inspect_ai/model/_model.py:3082-3089`) | before dispatch with `>=` (`_model.py:3036-3044`, called at `:1570` and on each attempt at `:1625`), and after the call with `>` | Works. A reached limit refuses every later dispatch in its subtree. |
| cost | after each completed call, but only if the token check passed (`_model.py:3091-3095`) | as token | Works, but a call that trips a token limit never records its cost into cost nodes (swarm.md's round-1 finding). |
| turn | once per top-level `generate()`, after it completes (`_model.py:1776-1783`) | after recording, with `>` (`_limit.py:1186-1192`); never before dispatch | Works, but `turn_limit(N)` lets the (N+1)th generation run and raises after it. *Spike:* `turn_limit(3)` on a looping agent made 4 generations. |
| message | not recorded | the leaf only, against the calling conversation's length (`_limit.py:743-757`, `:1336-1357`), before each generate with `>=` (`_model.py:1027-1029`) and on every append to an `AgentState` or `TaskState` message list (`src/inspect_ai/util/_limited_conversation.py:8-24`) | Per conversation. A member's own `message_limit` replaces the sample's for that member; a node above the members is never checked when members have their own. |
| time | wall clock from entry | a cancel scope with a deadline; on expiry everything inside is cancelled and the error is raised where the `with` was opened (`_limit.py:1384-1436`) | Works. Calls in flight are cancelled. |
| working | waiting time is reported by whichever task ends a sample-wide wait, into that task's working node and its ancestors (`src/inspect_ai/_util/working.py:41-48`, `:88-113`) | only by `monitor_working_limit()`, which polls the sample's root node once a second from the sample's task group (`_limit.py:912-947`, `src/inspect_ai/_eval/task/run.py:2824`), and at checkpoint resume | **Never enforced.** *Spike:* `run(agent, limits=[working_limit(0.5)])` ran six 0.2-second calls (1.2 s) and returned no error. |

Two more facts about working time:

- **It collapses under contention.** Sample waiting time counts wall clock during which *at least one* task waits for a connection, counting overlaps once (`_util/working.py:88-113`). *Spike:* three members sharing `max_connections=1`, five 0.2-second calls each, ran for 3.04 s of wall clock and logged 0.19 s of working time; one member alone logged 1.32 s of 1.40 s. A sample `working_limit` therefore barely advances while members queue for connections.
- **A sample working limit ends the sample by cancellation.** The monitor calls `ActiveSample.limit_exceeded()`, which cancels the sample's task group (`src/inspect_ai/log/_samples.py:432-440`); the runner records the limit (`run.py:2894-2922`). Code inside the sample sees only a cancellation.

### Overshoot and connection slots

- A call takes a connection slot before its first pre-dispatch check (`_model.py:1059-1068` acquires the slot around `_generate`, which checks at `:1570` and `:1625`). A call waiting for a slot is checked only once it holds one, so once a token or cost limit is reached only calls already holding a slot can add to it.
- So a token or cost node is overshot by at most the calls holding a connection slot when it is reached. The number is at most the model's connection limit (`max_connections`, or the adaptive controller's current ceiling), which every sample shares, summed over the models the swarm uses.
- `Model.compact()` (native compaction) has its own pool of 10 slots per model and **no pre-dispatch check**: it records and checks only after the call (`_model.py:1281-1361`). Each agent loop (a member or a subagent) can dispatch one native compaction after a limit is reached.
- *Spike (overshoot):* three members of 0.05-second calls under a 1,000-token swarm node finished at 1,050 tokens.

### Limit errors inside tools and handoffs

- `execute_tools()` maps a `LimitExceededError` raised inside a tool to a `ToolCallError("limit", ...)` and the member continues (`src/inspect_ai/model/_call_tools.py:208-215`, `:338-345`). A handoff catches any `LimitExceededError` and appends a user message saying the agent exceeded its limit (`_call_tools.py:1062-1104`).
- So a limit tripped by a synchronous deepagent subagent (which runs inside deepagent's `agent` tool), or by any tool that calls a model, does not end the member at once. Because token and cost checks are sticky (a reached node refuses every later dispatch), the member's next `generate()` raises before dispatch.
- *Spike:* a `react()` member whose tool made model calls under a 200-token swarm node got a `limit` tool error, then raised the swarm node's error at its next generate, after 4 calls and exactly 200 tokens.

### Bridged generations

- `sandbox_agent_bridge()` starts the model service task inside its own task group, which is created inside the bridged agent, so bridged generations run in the member's context and see its limit nodes (`src/inspect_ai/agent/_bridge/sandbox/bridge.py:196-264`; inferred from the code, not run, since it needs a sandbox).
- `_forward_provider_errors()` re-raises a `LimitExceededError` from a bridged generation (`src/inspect_ai/agent/_bridge/sandbox/service.py:42-67`), and the sandbox service dispatcher answers any bare `LimitExceededError` with `ActiveSample.limit_exceeded()`, which cancels the whole sample (`src/inspect_ai/util/_sandbox/service.py:540-560`). It does not look at the error's `source`. inspect_ai's own tests pin both halves (`tests/agent/test_bridge_provider_errors.py:178-190`, `tests/util/sandbox/test_sandbox_service.py:579-655`).
- So today `run(claude_code(), limits=[token_limit(N)])` ends the sample when the agent's own limit is reached, and a swarm cap reached by a bridged member ends the sample, not the member.
- `bridge_generate()` marks its `model.generate()` call with a ContextVar, read by `in_bridge_model_generate()` (`src/inspect_ai/agent/_bridge/util.py:349-387`, `:715-728`). The bridge's own compaction runs outside that mark (`util.py:662-664`).

### What the nodes record

- `record_and_check_model_usage()` prices a call and stores the cost on its `ModelUsage` *before* recording it into token nodes (`_model.py:3064-3066`, `:3082`). A token node's accumulated `ModelUsage` therefore carries the cost of every call it recorded, including one that then trips a token limit. *Spike:* at 1,050 tokens of overshoot the swarm's cost node read \$1.00, while the per-member token nodes' `ModelUsage.total_cost` summed to \$1.05, equal to the sample's `model_usage`.
- `ModelUsage.__add__` treats a missing cost as absent, so a sum that includes an unpriced call still has a number (`src/inspect_ai/core/_model_output.py:41-66`). A sum cannot say whether it is complete.
- Response-cache hits return before any usage is recorded (`_model.py:1537-1564`), so nodes count only newly incurred usage.
- Approval and review model calls run under `suspend_token_limit()` and `suspend_turn_limit()` (`src/inspect_ai/approval/_apply.py:50`, `src/inspect_ai/review/_apply.py:59`). They are missing from token and turn nodes, but still recorded into cost nodes and the sample's `model_usage`.
- Native compaction records usage through the same path with no model event (`_model.py:1356-1359`). Summary compaction is an ordinary `model.generate()` (`src/inspect_ai/model/_compaction/summary.py:124`), so it has a model event, counts as a turn, and checks the message limit against the summarisation input. `CompactionAuto` (deepagent's default) falls back to summary compaction when native compaction raises any exception, a limit error included (`_compaction/auto.py:102-112`).
- A call cancelled in flight records nothing and leaves an error event with no usage (`_model.py:1658-1674`).
- *Spike (soft stop):* three members of 0.1-second calls ran under a common `token_limit(None)` node. After 0.35 s the node's limit was set to its current usage (300 tokens, 9 calls started). Each member raised the node's error at its next call. The 3 calls in flight completed and were recorded (450 tokens, equal to the sample's usage), and nothing was cancelled.

### How errors reach the runner

- anyio task groups raise an `ExceptionGroup` even for one failure; `collect()` unwraps a single one (`src/inspect_ai/util/_collect.py:35-48`).
- The runner flattens an escaping exception to its first leaf (`run.py:2970-2972`) and records any `LimitExceededError` as the sample's limit, whatever its source (`run.py:2992-2999`). A swarm cap error that escaped would be logged as a sample limit.
- `as_solver()` copies the agent's messages and non-empty output to the `TaskState` in a `finally`, so it survives errors and cancellation (`src/inspect_ai/agent/_as_solver.py:65-80`). The message copy is checked against the sample's message limit (`_as_solver.py:76`, `_limited_conversation.py:14-20`).
- A sample cost limit requires cost data for the task's model and role models, checked before the eval starts (`src/inspect_ai/model/_util.py:67-99`). Models a member resolves for itself are not checked.

## Design

### Overview

The swarm opens its own nodes between the sample's and each member's:

```
sample limits (Inspect)       token · cost · turn · message · time · working
└─ swarm meter                _SwarmTokenNode, no limit: the swarm's total (ledger)
   └─ swarm caps              _SwarmTokenNode and/or _SwarmCostNode: the stopping rule
      └─ member lever         _SwarmTokenNode, no limit until stopped: the member's
         │                    meter (ledger) and the controller's stop lever
         └─ member share      split share nodes (token/cost), when split != "pool"
            └─ member limits  the member's own limits (re-created as swarm nodes, except time)
               └─ the member's work: generations, subagents, compaction, bridged calls
   └─ final meter             _SwarmTokenNode, no limit: the final step, outside the caps
```

Everything below is inspect_swarm code in `src/inspect_swarm/_budget.py`, called from the controller in `_swarm.py` and the member wrapper in `_member.py`.

### The budget parameter

```python
@dataclass(frozen=True)
class Budget:
    cost: float | None = None
    """Swarm cost cap in dollars. None derives it from the sample's cost limit."""

    tokens: int | str | TokenLimit | None = None
    """Swarm token cap. A string is parsed by `parse_token_limit()` (e.g. "output:1m").
    None derives it from the sample's token limit, keeping its metering type."""

    time: float | None = None
    """Seconds after which the swarm stops its members. None derives it from the
    sample's time limit."""

    reserve: float = 0.05
    """Fraction of each sample limit held back when a cap is derived."""

    split: Literal["pool", "equal"] | tuple[float, ...] = "pool"
    """How the cost and token caps are divided between members."""

    grace: float = 120.0
    """Seconds members are given to finish in-flight work after a stop."""
```

`swarm(..., budget=Budget())` is the default. Every field is a plain value, so a task can expose it with `-T`. swarm.md's sketch `budget=cost_limit(40.0)` becomes `budget=Budget(cost=40.0)`.

**Deriving caps.** At start the swarm reads `sample_limits()` (public, `_limit.py:225-248`). For each of cost, tokens and time where the field is `None` and the sample has a limit `L` with usage `u` so far:

- cost: cap = `L * (1 - reserve) - u`;
- tokens: the same, with the sample node's metering `type`;
- time: cap = `L * (1 - reserve) - u - grace`, so that the hard stop at cap plus grace still leaves the reserve.

A derived cap at or below zero is an error at start (`ValueError` naming the limit and the usage already spent). An explicit cap larger than the sample's remaining limit is allowed, with a warning that the sample's limit will end the swarm first, without a final step.

This is what makes the cap one sample-level number: a task sets `cost_limit=B` once, and the single-agent arm, the epochs arm and the swarm arm all read it. The swarm stops at `B * (1 - reserve)` and keeps the rest for overshoot and the final step. The other limit kinds a task can set do not derive caps:

- a sample **turn limit** already applies swarm-wide, because turns are recorded on every ancestor;
- a sample **message limit** applies to each member's conversation separately (leaf-only);
- a sample **working limit** is accepted but is unreliable under a swarm ([Each limit kind](#each-limit-kind)), so the swarm logs a warning at start when one is set.

**Cost data.** When a cost cap applies, the swarm checks at start that every member's configured model has cost data (`_get_model_info_direct(model).cost`), mirroring Inspect's own check for the task's models, and raises `PrerequisiteError` otherwise. A model a member resolves at run time (a subagent's model, for example) cannot be checked in advance. Its calls appear in the ledger as unpriced.

### Swarm nodes

Every node the swarm opens is a subclass of one of Inspect's private node classes: `_SwarmTokenNode(_TokenLimit)`, `_SwarmCostNode(_CostLimit)`, and, for re-created member limits only, `_SwarmTurnNode(_TurnLimit)` and `_SwarmMessageNode(_MessageLimit)`. Each is entered with `with` like any limit, and overrides only the method that raises (`_check_self`, or `check` for the message node, which has no ancestors to check). They differ from Inspect's nodes in three ways:

- **Ownership.** Each has an owner: the swarm, or one member. The controller recognises its own errors by `error.source` identity against the set of nodes it created.
- **Messages.** When exceeded outside a bridged member they raise `LimitExceededError` as usual, with the kind's `type`, except the lever, which raises `type="custom"`. The message names the swarm and the reason ("Swarm cost cap reached", "Stopped by the swarm: verified answer"). They write no `SampleLimitEvent`, which the viewer would show as a sample limit; the controller writes an `InfoEvent` instead ([Ledger](#the-ledger)).
- **Bridged members.** Before raising, a node asks whether it is being checked for a bridged member: `in_bridge_model_generate()` is true, or the current member is already marked bridged. The member wrapper sets a `_current_member` ContextVar, and a node marks the member bridged the first time it records usage inside a bridged generation. A bridged member's first generation always comes before any bridge compaction, so the mark is set by the time it is needed. If the member is bridged, the node does not raise, because a raise there would end the sample ([Bridged generations](#bridged-generations)). Instead it records why the member must stop and cancels that member's cancel scope synchronously. If the node is a swarm cap, it also calls the controller's `stop("swarm_cap")`. The bridged agent's pending call is cancelled at its next await, before or just after dispatch.

The lever is a `_SwarmTokenNode` with `limit=None`. It never raises until the controller stops the member by setting `lever.limit = int(lever.usage)`. Its usage is the member's meter for the ledger. Exposing `_TokenLimit._usage` (the accumulated `ModelUsage`) as `model_usage` is the one other private access.

This keeps the sample's own nodes untouched: a sample limit reached inside a bridged member still ends the sample, as it should.

### What each kind does in a swarm

| Kind | Swarm cap | Member limit | Split | Notes |
|---|---|---|---|---|
| cost | yes (`Budget.cost`) | yes | yes | Recommended for comparisons, and required when members use different models. |
| token | yes (`Budget.tokens`, any metering type) | yes | yes | For single-model swarms or unpriced models. |
| time | yes (`Budget.time`, a controller timer) | yes (Inspect's `time_limit`) | no | A member time limit cancels the member, so its in-flight calls are unknown. |
| turn | no | yes, with Inspect's N+1 semantics | no | A sample turn limit is swarm-wide. With no pre-dispatch check, a swarm turn cap would let every active member make one more paid call. |
| message | no | yes | no | Per conversation. A node above members has no meaning. |
| working | no | rejected (`ValueError`) | no | Never enforced below the sample, and collapses under contention. |

- **Member limits** come from `member(agent, limits=[...])`. The swarm re-creates a member's `token_limit`, `cost_limit`, `turn_limit` and `message_limit` as swarm nodes with the same value (and metering type, for tokens), so they behave the same on native and bridged members. It passes a member's `time_limit` to `run()` unchanged: a time limit is a cancel scope, which stops a bridged member without going through the sandbox service.
- **Limit objects are read, not entered.** The swarm reads `limit` and `type` from the objects the task passed, which are never entered, so the task's objects stay unused and reusable.

### Splits

`Budget.split` divides each capped kind among members:

- `"pool"` (default): no share nodes. Members draw on one budget, and a member may spend all of it. This is the team@k arm of swarm.md's first eval question.
- `"equal"`: each of the k members gets a share node of `cap / k` for cost and tokens.
- A tuple of weights: shares proportional to the weights; its length must equal the member count, and every weight must be positive.

A member that exhausts its share stops (its status is `share`), and its unused share is not given to others, so the split is a fixed experimental condition. A split with no capped kind is a `ValueError`. Shares nest under the swarm cap, so the cap still binds when shares add up to more than the cap because of overshoot.

### Stopping

Every stop goes through one controller method, `stop(reason)`. Reasons are a closed set: `all_done`, `verified`, `swarm_cap`, `swarm_time`, and later `quiescence`.

1. **Soft stop.** For every member still running, set its lever to its current usage. A native member raises at its next pre-dispatch check (or after its in-flight call completes), so calls already in flight finish and are recorded, and no new call starts. A bridged member is cancelled at its next check instead (see [Swarm nodes](#swarm-nodes)).
2. **Grace.** Wait until every member has returned, or `grace` seconds have passed.
3. **Hard stop.** Cancel the cancel scope of every member still running. Calls they have in flight are cancelled and counted as unknown in the ledger.

Why a soft stop:

- It keeps in-flight usage known: the soft-stop spike recorded every call, where cancelling would have left 3 of 9 unknown.
- It bounds overshoot the same way a reached cap does, by the calls holding a connection slot ([Overshoot](#overshoot-and-connection-slots)), plus at most one native compaction per running agent loop.
- It needs nothing from inspect_ai beyond the limit setter.

Grace bounds members that are inside a long tool call (a test suite in the sandbox) when the stop arrives. A member stopped while a tool call is running sees, at worst, a `limit` tool error from a nested model call, and then its next generate refuses.

Triggers:

- **Token and cost caps** trip themselves. The first member to raise a swarm cap error has its wrapper call `stop("swarm_cap")`. The other members would trip the reached cap at their next dispatch anyway, and the soft stop makes that uniform. A cap reached inside a bridged member calls `stop()` from the node ([Swarm nodes](#swarm-nodes)).
- **The time cap** is a timer task in the controller's task group, not an Inspect `time_limit`. It calls `stop("swarm_time")`, so the time cap gets the same soft stop. The sample's own time limit stays a hard stop around everything.
- **Topology stops** (`verified`, and later `quiescence`) come from the controller ([Controller](swarm.md#controller-topology-termination-final-answer)).

**Member wrapper and statuses.** Each member runs as:

```python
async def _run_member(m: Member, lever: _SwarmTokenNode, nodes: list[Limit]) -> None:
    token = _current_member.set(m)
    err: LimitExceededError | None = None
    try:
        with anyio.CancelScope() as m.scope:
            _, err = await run(m.agent, m.input, limits=[lever, *nodes], name=m.name)
        if m.scope.cancelled_caught:
            m.status = m.stop_cause or "cancelled"  # hard stop, or a bridged member stopped
        else:
            m.status = _status_for(err, lever)  # submitted, stopped, share, member_limit:<type>
    except LimitExceededError as ex:
        if ex.source not in swarm.cap_nodes:
            m.status = "errored"  # foreign: a sample limit
            raise
        m.status = "swarm_cap"
        swarm.stop("swarm_cap")
    except Exception:
        m.status = "errored"
        raise
    finally:
        _current_member.reset(token)
```

Member statuses are a closed set: `submitted`, `stopped` (soft stop), `share`, `member_limit:<type>`, `swarm_cap`, `cancelled`, `errored`. Each member's status and stop cause go into its evidence and the ledger.

### Exhaustion and errors

The controller runs members in its own task group inside the swarm nodes. Its handling, in order:

1. **Own caps.** A swarm cap error never leaves a member wrapper; it becomes a status and a `stop()`. This covers the error arriving bare, in a member's own `ExceptionGroup`, or after a tool or handoff turned it into a message, since in every case the member raises at its next generate. A wrapper uses `except*` when the member's agent can raise groups (a bridged agent's task group), with the same `source` test on each leaf.
2. **Foreign errors** (a sample limit, `TerminateSampleError`, a member's crash) propagate out of the wrapper. The task group cancels the other members, so their in-flight calls become unknown. The controller then removes any of its own cap errors from the group with `ExceptionGroup.split()`, so that a sample limit is not recorded as a swarm cap. It re-raises a single remaining exception bare and several as a group, as `collect()` does. The runner then records a sample limit as the sample's limit, as for any agent.
3. **Cancellation from outside** (the sample's time or working limit, the sample's cost or token limit surfacing from a bridged member, an operator interrupt) arrives as a cancellation. The controller does no awaiting work in that path.
4. **On every exit path** the controller writes the ledger and the output in a synchronous `finally`. `transcript().info()` and store writes are synchronous (`src/inspect_ai/log/_transcript.py:544`), so they run even during cancellation. When the cause is not an exception it can see, it reads it from `sample_active().limit_exceeded_error` (a working limit or a bridged sample limit), from the sample's time node, or from the active sample's interrupt action, and records `sample_limit:<type>` or `operator`.

Member limits are not exhaustion. `run()` catches them, the member's status records them, and its siblings continue.

The controller keeps the swarm's own `AgentState.messages` to the task input plus the final assistant message. Member conversations live in their spans and timelines. A concatenation of members' conversations would be checked against the sample's message limit when `as_solver()` copies it ([How errors reach the runner](#how-errors-reach-the-runner)).

### The final step and the reserve

After the members have stopped, the controller leaves the cap nodes and runs the final step ([Final answer](swarm.md#controller-topology-termination-final-answer)) inside the final meter. Only the sample's limits apply to it, so the reserve is whatever the sample's limits have left.

- **What the reserve is for.** Of the final modes, only `synthesize` (and a later integration member) makes model calls. `verify`, `vote`, `first` and `reporter` pick among existing submissions, so for them the reserve is headroom for overshoot. It keeps the sample's limit from tripping during the stop, so the stop stays orderly: members soft-stopped, ledger complete, stop reason `swarm_cap` rather than a sample limit.
- **Provisional answer.** For every mode except `synthesize`, the provisional answer the controller keeps current ([Accounting](swarm.md#observer-evidence-accounting-and-metrics)) is the same answer the final step would pick. A sample limit that ends the swarm early therefore costs these modes the orderly stop, not the answer.
- **When the reserve holds.** The reserve covers the stop if it is at least the overshoot plus the final step's cost. Overshoot is at most the calls that hold a connection slot when the cap is reached ([Overshoot](#overshoot-and-connection-slots)), plus one native compaction per running agent loop (member or subagent), each at most the model's context window of input plus its `max_tokens` of output. With prices, that is a dollar bound. It depends on the model's connection limit and call size, not on the member count, which refines swarm.md's per-member bound. Fan-out inside a member (a synchronous deepagent's parallel subagent calls) is covered too, because those calls also hold connection slots. The bound is loose: with 10 connections and 200,000-token contexts it is 2 million tokens. A reserve that small budgets can afford remains best effort.
- **Default.** 5% of each sample limit, recorded with its use in the ledger so users can tune it. It is a guess, chosen to be small next to the budgets the scaling experiments use and large enough for a few calls.

### The ledger

The ledger is what analysis compares across arms (swarm.md: "caps are stopping rules; comparisons use realized cost"). It is written once, in the controller's `finally`, as a versioned `InfoEvent` (`source="inspect_swarm"`) and as a `StoreModel` under `inspect_swarm:ledger`, so scorers and the analysis helpers read the same record.

```python
class MemberLedger(BaseModel):
    name: str
    status: str                    # closed set, see Stopping
    usage: ModelUsage              # the member's lever: everything recorded in its context
    calls: int                     # model events in the member's spans (excluding replays)
    cache_replays: int             # events with cache="read": charged zero
    cancelled_calls: int           # events cancelled in flight: usage unknown
    unpriced_models: list[str]     # models seen in its events with no cost data

class SwarmLedger(BaseModel):
    version: int = 1
    budget: Budget                 # as configured
    caps: dict[str, float | None]  # derived or explicit, per kind
    sample_limits: dict[str, float | None]  # at swarm start
    members: list[MemberLedger]
    final: ModelUsage              # the final meter
    swarm: ModelUsage              # the swarm meter: members, final step, anything else the swarm ran
    sample_delta: ModelUsage       # the sample's model usage over the swarm's lifetime
    unattributed: ModelUsage       # sample_delta minus swarm
    overshoot: dict[str, float]    # per capped kind: usage at the end minus the cap, if positive
    stop_reason: str               # closed set, see Stopping and Exhaustion
    resumed: bool                  # the sample resumed from a checkpoint (see Compatibility)
    total_complete: bool
    attribution_complete: bool
```

Sources and coverage:

- **Per-member usage comes from the member's lever**, not from summing model events. The lever records every call made in the member's context with its cost:
  - the member's own generations;
  - its synchronous subagents and any tool that calls a model;
  - summary and native compaction;
  - bridged generations;
  - background children once they are supported, since they inherit the member's context.

  It excludes cache replays (they record nothing) and calls cancelled in flight (no usage exists). This attributes native compaction, which swarm.md had to report as unattributed, and it avoids the token-node/cost-node disagreement, because cost is on the `ModelUsage` before the token check.
- **Counts come from model events** in the member's spans: calls, replays (`cache="read"`, `src/inspect_ai/event/_model.py:135`), cancelled calls, and the models used, to detect unpriced ones. Events are used for counts only, so missing events (native compaction) cannot make a total wrong.
- **The sample delta** is the sample's model usage at the end minus at the start. It includes approval and review calls, which are suspended from token nodes. `unattributed` is therefore approval and review spend, plus anything that ran in the sample during the swarm outside its contexts.
- **Totals per arm** stay as swarm.md set them: Inspect's sample `model_usage`, which every arm has. The ledger's figures must reconcile with it: `swarm + unattributed = sample_delta`.

Claims:

- `total_complete`: no member has cancelled calls and no unpriced models were used. Otherwise the arm's total is a lower bound, reported with the count of unknown calls. A cancelled call is missing from the sample's `model_usage` too, so the flag applies to the arm's total, not only to the swarm's figures.
- `attribution_complete`: `total_complete` and `unattributed` is zero. Otherwise per-member figures are lower bounds; the arm's total is unaffected.

### Budget splits as a sweep axis

Agent count, budget and split are task parameters. The task sets the sample limit once, and the swarm derives its cap from it:

```python
@task
def research_math(
    members: int = 4, budget: float = 40.0, split: Literal["pool", "equal"] = "pool"
) -> Task:
    agent = swarm(
        members=member(deepagent(...), count=members),
        budget=Budget(split=split),
    )
    return Task(dataset=..., solver=as_solver(agent), scorer=..., cost_limit=budget)
```

- **Matched cap (swarm.md's first question).** Sweep `members` at a fixed `budget`, with `split="pool"`. A run with `members=1` is the single-agent arm with the same accounting.
- **Ord's grid (agent count × per-agent budget b).** Sweep `members` and `b` with `budget = members * b` and `split="equal"`; each member's share is `b * (1 - reserve)`.
- **Split as an ablation.** Hold `members` and `budget` fixed and sweep `split`. Pool and equal differ in whether one member can starve the others, which is a coordination effect worth measuring.

In every arm, analysis uses realized cost from the ledger and the sample, not the cap; the cap is recorded only as the condition.

### Interactions

**deepagent subagents.**
- Synchronous subagents run inside deepagent's `agent` tool, in the member's context, so the member's lever, share and limits meter them. A subagent's own `Subagent.limits` nest below, and its own limit errors come back to the member as a "Subagent stopped" result (`src/inspect_ai/agent/_deepagent/agent_tool.py:551-569`).
- A swarm cap or lever reached inside a subagent becomes a `limit` tool error, and the member stops at its next generate ([Limit errors inside tools](#limit-errors-inside-tools-and-handoffs)).
- Parallel-safe subagent calls run concurrently, so one deepagent member can hold several connection slots. The overshoot bound counts slots, not members, so it covers this.
- Background subagents stay rejected in M1 (swarm.md). If a scoped owner for `background()` is added, children already inherit the member's context, so the lever stops them at their next call. The drain would then await them through that owner.

**Bridged members.**
- Bridged generations are metered by the member's nodes. The swarm's nodes stop a bridged member by cancelling it, not by raising ([Swarm nodes](#swarm-nodes)), so swarm caps, shares, member limits and soft stops work without ending the sample.
- Its in-flight call at that moment is cancelled and unknown.
- Every sample limit still ends the sample when reached inside a bridged generation, as it should. The ledger records the cause.
- A vendor swarm as one member (Codex multi-agent, Claude Code teams) is metered as one member: all its internal agents' generations go through its bridge. Its member limits are the vendor swarm's budget.
- A user-supplied bridged host tool that calls a model and reaches a swarm node raises inside the sandbox service, which ends the sample. The swarm's own tools make no model calls.

**Compaction.**
- Summary compaction is a generation: metered, counted as a turn, refused before dispatch once a node is reached. It checks the message limit against the summarisation input, which can trip a member's message limit one message early.
- Native compaction is metered by the lever but has no pre-dispatch check, so each agent loop may dispatch one native compaction after a stop. Under `CompactionAuto`, a native compaction that trips a node is caught. Auto logs "Native compaction failed" and falls back to summary compaction, which is refused before dispatch, and the error reaches the member. That costs one misleading warning, not an extra call.
- Compaction thresholds are about context windows, not budgets. A member near its share still compacts when its context fills.

**Approvals and monitors.** Approval-policy model calls are suspended from token and turn nodes, so they never trip a token cap, a lever or a turn limit. They do count against cost caps and shares, and they appear in the ledger as unattributed. Whether inspect_sentinel's monitors will run under the same suspension is for sentinel to settle; the ledger reports whatever they record.

**Sample limit overrides.** An operator can retune the sample's time, token and message limits while it runs (`src/inspect_ai/util/_limit_overrides.py`). The swarm's derived caps are fixed at start, so a retune moves the reserve, not the cap. A cut below the swarm's cap means the sample limit ends the swarm first.

## Changes to swarm.md

This branch makes small edits to swarm.md, all on this topic:

- [Limits and cost](swarm.md#limits-and-cost): two caveats added (working limits; bridged members) and a link here.
- [Entry point](swarm.md#entry-point): the sketch's `budget=cost_limit(40.0)` becomes `budget=Budget(cost=40.0)`.
- [Accounting](swarm.md#observer-evidence-accounting-and-metrics): the ledger's per-member figures come from token meter nodes rather than cap nodes; per-member attribution from the member's meter node, so native compaction is attributed and no longer the unattributed remainder; the reserve and exhaustion bullets refined (derived caps, soft stop, the connection-slot bound, own errors removed from mixed groups); a link here.
- [Testing](swarm.md#testing) and [Implementation plan](swarm.md#implementation-plan): the bullets that named the unattributed remainder updated.

The overview is unchanged: no main decision moves. Its parenthetical listing native compaction among the calls the ledger cannot see is now conservative rather than wrong.

## Alternatives considered

**Ledger from model events (swarm.md's M1 plan).** Sum each member's `ModelEvent` usage, charging cache replays zero.
- No private fields.
- It misses native compaction, which then needs an inspect_ai change (usage on `CompactionEvent`) to attribute, and it needs every call to have an event in the right span.
- The lever is a node the swarm needs anyway for stopping, so its meter comes free. Events remain the source for counts.

**Stop by cancellation only.** Cancel members' scopes on every stop.
- Simplest, and fastest to stop.
- Every call in flight becomes unknown, so most capped runs would report lower bounds. The soft stop costs only a lever per member and a grace period.

**One shared stop node instead of a lever per member.** Lower a single node above all members.
- Equivalent for native members.
- A lever per member also meters each member, and can stop one member (a bridged one by cancelling) without touching the others.

**Fix the bridge in inspect_ai instead of special-casing bridged members.** Route a `LimitExceededError` from a bridged generation through `bridge.request_fail()` (as `ModelRefusalError` already is) instead of the sandbox service's `limit_exceeded()`. It would then unwind the bridged agent and reach `run()` or the controller like a native member's error.
- Cleaner, and it fixes `run(claude_code(), limits=[...])` for everyone, which today ends the sample.
- It changes inspect_ai behaviour that its tests pin. The swarm would still need its own nodes for messages and the soft stop.
- Not chosen as a dependency; listed under [Not this design](#not-this-design) as an inspect_ai fix worth proposing on its own. If it lands, the swarm's bridged branch becomes redundant and can be removed.

**Use Inspect's `time_limit` for the swarm's time cap.**
- Exact, with no timer task.
- It cancels members outright, losing in-flight usage. The timer gives the time cap the same soft stop as the other caps.

**A swarm turn cap.**
- Natural for frameworks whose caps are turn caps.
- Turns have no pre-dispatch check, so a reached cap stops nobody until each member's next call completes. Turn counts are also not comparable across arms with different per-call sizes. Not offered: use cost or tokens, or a sample turn limit, which already counts swarm-wide.

**Redistribute an exhausted member's unused share.**
- Uses the budget fully.
- It makes the split a dynamic policy rather than a fixed condition, and reintroduces the pool's starvation effects. Not chosen; a user who wants that wants `"pool"`.

## Compatibility and migration

- **inspect_swarm** has no released API.
- **inspect_ai internals used:**
  - `_TokenLimit`, `_CostLimit` and `_TokenLimit._usage`;
  - `in_bridge_model_generate()`;
  - `_get_model_info_direct()`;
  - `sample_active()`.

  This is allowed (decision: Ransom, 2026-10-07). They are pinned by tests in this repository ([Testing](#testing)), so an inspect_ai change that breaks them fails CI here.
- **No inspect_ai changes.** The bridge fix in [Alternatives](#alternatives-considered) is optional.
- **Eval logs.** The ledger is an `InfoEvent` with a `version` field and a store entry; no new event types. Swarm nodes write no `SampleLimitEvent`, so the log's sample-limit record and `SampleLimitEvent`s keep meaning a sample limit.
- **Checkpoint and resume.** inspect_ai restores only the sample's root nodes on resume (`src/inspect_ai/util/_checkpoint/sample_runtime.py:147-225`). Swarm nodes restart from zero, so a resumed swarm would overspend its cap. Resume is out of scope for swarms (swarm.md's [Not this design](swarm.md#not-this-design)); until it is designed, the ledger records `resumed: true` when the sample was resumed, and analysis excludes such samples from matched-cost comparisons.

## Security

- **What reaches this code.** Model usage numbers and prices from providers, member names from the task's roster, and limit values from task parameters. Model output and tool arguments do not reach it; the ledger records numbers, statuses and model names, not content.
- **A member cannot raise its own budget.** Nodes, levers and shares live in the host process. A member's tools, including bridged tools and code in the sandbox, have no handle on them, and stop causes are set only by the controller.
- **Spending to deny the others.** Under `split="pool"` one member can spend the whole budget; that is the condition being measured. `"equal"` or weights bound each member when that is unwanted.
- **Log contents.** The ledger is JSON data in an `InfoEvent`, rendered generically.

## Testing

All runtime tests use mockllm with scripted outputs, usage and `ModelCost` set through `set_model_info()`. They need no network or Docker and run on asyncio and trio. They go in `tests/test_budget.py` (nodes, derivation, ledger) and `tests/test_swarm_limits.py` (controller behaviour through `swarm()`).

- **Pinned inspect_ai behaviour** (each spike above, as a test, so a change upstream fails here):
  - nested `working_limit` is not enforced;
  - `turn_limit(N)` makes N+1 generations;
  - a token node's `ModelUsage` carries the cost of a call that trips a token limit, while a cost node does not;
  - a limit reached inside a tool becomes a `limit` tool error and the member raises at its next generate;
  - lowering a node's limit stops callers at their next call without cancelling in-flight calls.
- **Derivation:**
  - caps from the sample's cost, token (with metering type) and time limits, minus the reserve and usage so far;
  - an error when a derived cap is not positive;
  - a warning for an explicit cap above the sample's remaining limit, and for a sample working limit;
  - `PrerequisiteError` for a member model without cost data under a cost cap.
- **Nodes:**
  - swarm nodes raise with their own messages and write no `SampleLimitEvent`;
  - inside `bridge_model_generate()`, an exceeded swarm node cancels the current member's scope instead of raising, and a cap also calls `stop()`. This tests the bridged branch without a sandbox;
  - a member marked bridged is cancelled by a node checked outside a bridged generation (bridge compaction);
  - a member's token, cost, turn and message limits are re-created as swarm nodes with the same values and metering type, and stop the member through `run()`.
- **Stopping:**
  - soft stop records every in-flight call and leaves no unknown calls;
  - grace expiry cancels a member stuck in a slow tool and counts its in-flight call as unknown;
  - the time cap stops members softly and leaves the final step its time;
  - each stop reason and member status is recorded.
- **Exhaustion:**
  - the swarm cap recovered when it arrives bare, in a group, or after a tool error;
  - sample limits, `TerminateSampleError` and crashes propagate, with the swarm's own errors removed from mixed groups;
  - a sample time limit and a working-limit cancellation leave the ledger and the provisional answer written;
  - the swarm's returned messages stay within a small sample message limit.
- **Splits:**
  - equal and weighted shares, and a member stopping on its share while others continue;
  - shares never redistributed;
  - errors for a split with no capped kind or wrong weights.
- **Ledger:**
  - members' levers sum with the final meter to the swarm meter;
  - `swarm + unattributed = sample_delta`;
  - an approval call appears as unattributed;
  - cache replays counted and charged zero;
  - an unpriced member model clears `total_complete`;
  - native compaction (a test `ModelAPI` implementing `compact()`) is attributed to the member.
- **deepagent:** a synchronous deepagent member with parallel subagent calls overshoots by its fan-out, within the connection-slot bound computed from the mock's `max_connections`.
- **Bridged members, end to end** (Claude Code or Codex through inspect_swe, a swarm cap and a soft stop) need Docker and provider keys. They are marked slow and run by hand or on a schedule, never in PR CI.

## Implementation plan

All in inspect_swarm, as part of M1 ([Implementation plan](swarm.md#implementation-plan)). Each step is one PR, reviewed before the next.

1. **Swarm nodes and the budget parameter.** `Budget`, cap derivation, `_SwarmTokenNode` and `_SwarmCostNode` (ownership, messages, the bridged branch), and the pinned-behaviour tests. Files: `src/inspect_swarm/_budget.py`, `tests/test_budget.py`.
2. **Members and stopping.** The member wrapper with lever, shares and re-created member limits; `stop()` with soft stop, grace and hard stop; the time-cap timer; member statuses and stop reasons. Files: `src/inspect_swarm/_member.py`, `_swarm.py`, `tests/test_swarm_limits.py`.
3. **Exhaustion and the final step.** Recovery of own caps, propagation with own errors removed, the synchronous `finally`, the final meter and the provisional answer. Files: `_swarm.py`, `_final.py`, tests.
4. **The ledger.** `MemberLedger`, `SwarmLedger`, event counting, reconciliation and claims, written as an `InfoEvent` and a store entry. Files: `_budget.py` (or `_ledger.py` if it grows), `_evidence.py`, tests.
5. **Splits and the sweep example.** Share nodes and validation, and a task example in the docs showing the three sweeps. Files: `_budget.py`, tests, `README.md`.

## Open questions

1. **Should the swarm's cap default to deriving from the sample's limits, minus a 5% reserve?** The alternative is no default cap: the swarm stops only on sample limits unless `Budget` sets one.
   - (a) Derive by default, so arms share one number and the swarm stops in an orderly way at 95%.
   - (b) No default, so a swarm arm spends to the same sample limit as the single-agent arm, at the cost of ending on a sample limit with no final step and no soft stop.

   Recommendation: (a). The orderly stop is what keeps the ledger complete, and analysis uses realized cost, so the 5% gap does not bias comparisons.
2. **Propose the bridge fix to inspect_ai now?** The swarm does not need it, but `run(bridged_agent, limits=[...])` ending the sample is a general bug, and the fix would let the swarm drop its bridged branch. Recommendation: file it as an inspect_ai issue now, and propose the PR when bridged members are first used. It is in [Not this design](#not-this-design) either way.

## Not this design

Adjacent problems noticed and left out, for Ransom to file if wanted:

- **Nested `working_limit()` is never enforced** (inspect_ai). Only the sample's root node is polled (`_limit.py:912-947`), so `run(agent, limits=[working_limit(t)])` silently does nothing.
- **Sample working time under concurrency** (inspect_ai). Waiting counts whenever any task waits, so concurrent agents queueing for connections drive working time towards zero. This affects deepagent's background subagents as well as swarms.
- **`turn_limit(N)` pays for N+1 generations** (inspect_ai). There is no pre-dispatch turn check, unlike token and cost.
- **A limit reached inside a bridged agent ends the sample whatever its source** (inspect_ai). Route it through `bridge.request_fail()` so agent-scoped limits on bridged agents behave as on native ones ([Alternatives](#alternatives-considered), [open question 2](#open-questions)).
- **Native compaction has no pre-dispatch limit check** (inspect_ai), so it can dispatch after a limit is reached.
- **`CompactionAuto` swallows a `LimitExceededError` from native compaction** (inspect_ai) and logs it as a compaction failure before falling back.
- **Checkpoint and resume of nested limit nodes** (inspect_ai and inspect_swarm). Only sample roots are restored.
- **Per-member waiting time** for the latency metrics (inspect_swarm metrics work).
