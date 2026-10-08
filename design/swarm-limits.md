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
- **An Inspect cost node can miss spend repeatedly**, not just for calls in flight: every call that trips any token node below it is never charged to it.

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
- `apply_limits()` matches only a bare error: an owned limit error wrapped in an `ExceptionGroup` (by a task group inside the agent) escapes `run()` as if it were foreign (`_limit.py:179-186`; reproduced by the round-1 review on both backends).
- Every `Limit` object is single-use: entering it twice raises `RuntimeError` (`_limit.py:141-147`). deepagent copies its subagents' limits per dispatch for this reason (`src/inspect_ai/agent/_deepagent/agent_tool.py:559-560`).
- A node's limit can be changed while it is open through its `limit` setter, without a check (`_limit.py:1066-1074`, `:1237-1245`). The next check uses the new value.

### Each limit kind

| Kind | Recorded | Checked | Below the sample |
|---|---|---|---|
| token | after each completed call, before the check (`src/inspect_ai/model/_model.py:3082-3089`) | before dispatch with `>=` (`_model.py:3036-3044`, called at `:1570` and on each attempt at `:1625`), and after the call with `>` | Works. A reached limit refuses every later dispatch in its subtree. |
| cost | after each completed priced call, but only if the token check passed (`_model.py:3091-3095`) | as token | Unreliable as a budget. A call that trips *any* token node never records its cost into cost nodes. This repeats: *spike:* three sequential \$10 calls under `cost_limit(15)`, each inside a child `run()` whose own token limit tripped, made all 3 calls and charged \$0 to the cost node; the sample recorded \$30. |
| turn | once per top-level `generate()`, after it completes (`_model.py:1776-1783`) | after recording, with `>` (`_limit.py:1186-1192`); never before dispatch | Works, but `turn_limit(N)` lets the (N+1)th generation run and raises after it. *Spike:* `turn_limit(3)` on a looping agent made 4 generations. |
| message | not recorded | the leaf only, against the calling conversation's length (`_limit.py:743-757`, `:1336-1357`), before each generate with `>=` (`_model.py:1027-1029`) and on every append to an `AgentState` or `TaskState` message list (`src/inspect_ai/util/_limited_conversation.py:8-24`) | Per conversation. A member's own `message_limit` replaces the sample's for that member; a node above the members is never checked when members have their own. |
| time | wall clock from entry | a cancel scope with a deadline; on expiry everything inside is cancelled and the error is raised where the `with` was opened (`_limit.py:1384-1436`) | Works. Calls in flight are cancelled. |
| working | waiting time is reported by whichever task ends a sample-wide wait, into that task's working node and its ancestors (`src/inspect_ai/_util/working.py:41-48`, `:88-113`) | only by `monitor_working_limit()`, which polls the sample's root node once a second from the sample's task group (`_limit.py:912-947`, `src/inspect_ai/_eval/task/run.py:2824`), and at checkpoint resume | **Never enforced.** *Spike:* `run(agent, limits=[working_limit(0.5)])` ran six 0.2-second calls (1.2 s) and returned no error. |

Two more facts about working time:

- **It collapses under contention.** Sample waiting time counts wall clock during which *at least one* task waits for a connection, counting overlaps once (`_util/working.py:88-113`). *Spike:* three members sharing `max_connections=1`, five 0.2-second calls each, ran for 3.04 s of wall clock and logged 0.19 s of working time; one member alone logged 1.32 s of 1.40 s. A sample `working_limit` therefore barely advances while members queue for connections.
- **A sample working limit ends the sample by cancellation.** The monitor calls `ActiveSample.limit_exceeded()`, which cancels the sample's task group (`src/inspect_ai/log/_samples.py:432-440`); the runner records the limit (`run.py:2894-2922`). Code inside the sample sees only a cancellation.

### Overshoot and connection slots

- A call takes a connection slot before its first pre-dispatch check (`_model.py:1059-1068` acquires the slot around `_generate`, which checks at `:1570` and `:1625`). A call waiting for a slot is checked only once it holds one, so once a token or cost limit is reached only calls already holding a slot can add to it.
- So a token or cost node is overshot by at most the calls holding a connection slot when it is reached. The number is at most the model's configured maximum connections (`max_connections`, or the adaptive controller's maximum, not its current ceiling: a resize can leave calls admitted under a higher ceiling still running, `src/inspect_ai/util/_concurrency.py:97-111`), which every sample shares, summed over the models the swarm uses. This holds for token nodes and for the swarm's cost cap ([Swarm nodes](#swarm-nodes)), not for Inspect's cost nodes, which can miss calls entirely.
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
- `bridge_generate()` marks its `model.generate()` call with a ContextVar, read by `in_bridge_model_generate()` (`src/inspect_ai/agent/_bridge/util.py:349-387`, `:715-728`). Bridge compaction and a bridge filter run *before* that mark, on every request including the first (`util.py:660-708`). So the mark cannot tell whether a check is running for a bridged agent: the round-1 review reproduced a limit error from initial native compaction with the mark false. Nothing else in the bridge or the sandbox service sets a context marker.

### What the nodes record

- `record_and_check_model_usage()` prices a call and stores the cost on its `ModelUsage` *before* recording it into token nodes (`_model.py:3064-3066`, `:3082`). A token node's accumulated `ModelUsage` therefore carries the cost of every call it recorded, including one that then trips a token limit. *Spike:* at 1,050 tokens of overshoot the swarm's cost node read \$1.00, while the per-member token nodes' `ModelUsage.total_cost` summed to \$1.05, equal to the sample's `model_usage`.
- `ModelUsage.__add__` treats a missing cost as absent, so a sum that includes an unpriced call still has a number (`src/inspect_ai/core/_model_output.py:41-66`). A sum cannot say whether it is complete.
- Response-cache hits return before any usage is recorded (`_model.py:1537-1564`), so nodes count only newly incurred usage.
- Approval and review model calls run under `suspend_token_limit()` and `suspend_turn_limit()` (`src/inspect_ai/approval/_apply.py:50`, `src/inspect_ai/review/_apply.py:59`). They are missing from token and turn nodes, but still recorded into cost nodes and the sample's `model_usage`.
- Native compaction records usage through the same path with no model event (`_model.py:1356-1359`). Summary compaction is an ordinary `model.generate()` (`src/inspect_ai/model/_compaction/summary.py:124`), so it has a model event, counts as a turn, and checks the message limit against the summarisation input. `CompactionAuto` (deepagent's default) falls back to summary compaction when native compaction raises any exception, a limit error included (`_compaction/auto.py:102-112`).
- A call cancelled in flight records nothing and leaves an error event with no usage (`_model.py:1658-1674`). A native compaction cancelled in flight leaves neither an event nor usage (round-1 review probe). A successful response whose provider reported no usage records nothing either (`_model.py:1733-1736`).
- `record_model_cost()` is called only for priced calls (`_model.py:3092`), so cost nodes never see unpriced ones. Token nodes see every recorded call, priced or not, one `ModelUsage` at a time, before it is summed.
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
└─ swarm meter                meter pair, no limit: the swarm's total (ledger)
   └─ swarm caps              cost cap pair and/or token node: the stopping rule
      └─ member lever         meter pair, no limit until stopped: the member's
         │                    meter (ledger) and the controller's stop lever
         └─ member share      split shares (cost pair / token node), when split != "pool"
            └─ member limits  the member's own limits, re-created per invocation
               └─ the member's work: generations, subagents, compaction, bridged calls
   └─ final meter             meter pair, no limit: the final step, outside the caps
```

A *pair* is two nodes, one in Inspect's token tree and one in its cost tree, that share one accumulator ([Swarm nodes](#swarm-nodes)). Everything below is inspect_swarm code in `src/inspect_swarm/_budget.py`, called from the controller in `_swarm.py` and the member wrapper in `_member.py`.

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

**Validation.** `Budget.__post_init__` raises `ValueError`, naming the field, unless:
- `cost` and `time` are `None` or finite and greater than zero;
- `tokens` is `None` or resolves (through `resolve_token_limit()`) to a whole number greater than zero, with a valid metering type;
- `reserve` is finite and `0 <= reserve < 1`;
- `grace` is finite and `>= 0`;
- `split` is `"pool"`, `"equal"` or a non-empty tuple of finite weights greater than zero.

Checks that need the roster or the sample run at swarm start: the weight count must equal the member count, and caps and shares must come out positive (below).

**Deriving caps.** At start the swarm reads `sample_limits()` (public, `_limit.py:225-248`). For each of cost, tokens and time where the field is `None` and the sample has a limit `L` with usage `u` so far, the cap is `L * (1 - reserve) - u`:
- tokens: rounded down to a whole number, with the sample node's metering `type`;
- time: in seconds from swarm start. Grace is not subtracted; instead every stop's grace is clipped when a sample time limit exists ([Stopping](#stopping)), so a short time limit never fails at start. For example, a 60-second sample limit gives a 57-second cap, and a stop at that point gets at most 1.5 seconds of grace.

A derived cap at or below zero is an error at start (`ValueError` naming the limit and the usage already spent). An explicit cap larger than the sample's remaining limit is allowed, with a warning that the sample's limit will end the swarm first, without a final step.

This is what makes the cap one sample-level number: a task sets `cost_limit=B` once, and the single-agent arm, the epochs arm and the swarm arm all read it. The swarm stops at `B * (1 - reserve)` and keeps the rest for overshoot and the final step. The other limit kinds a task can set do not derive caps:

- a sample **turn limit** already applies swarm-wide, because turns are recorded on every ancestor;
- a sample **message limit** applies to each member's conversation separately (leaf-only);
- a sample **working limit** is accepted but is unreliable under a swarm ([Each limit kind](#each-limit-kind)), so the swarm logs a warning at start when one is set.

**Cost data.** When a cost cap applies, the swarm checks at start that every member's configured model has cost data (`_get_model_info_direct(model).cost`), mirroring Inspect's own check for the task's models, and raises `PrerequisiteError` otherwise. A model a member resolves at run time (a subagent's model, for example) cannot be checked in advance. Its calls are counted as unpriced ([The ledger](#the-ledger)) and never charged to a cost cap.

### Swarm nodes

The swarm never uses Inspect's own node classes for its stopping rules; it subclasses them. `_SwarmTokenNode(_TokenLimit)` is the token-tree node. For re-created member limits there are also `_SwarmTurnNode(_TurnLimit)` and `_SwarmMessageNode(_MessageLimit)`. Each overrides only the method that raises: `_check_self`, or `check` for the message node, which has no ancestors to check. A member time limit stays an Inspect `time_limit()`, created fresh per invocation: it raises where it was entered, in the member wrapper, not in the sandbox service.

**Cost is metered in the token tree.** An Inspect cost node can miss calls repeatedly ([Each limit kind](#each-limit-kind)). So every swarm node that limits or meters cost is a *pair* that shares one accumulator:

- A `_SwarmTokenNode` in the token tree. Its `record(usage)` runs for every call recorded in its context before any token check can raise (`_model.py:3082-3089`), adds `usage.total_cost` to the accumulator, and counts the call as unpriced when `total_cost` is `None`. Its `_check_self` compares the accumulator with the cost limit. So the cost check runs in the token check, root to leaf, ahead of every descendant token node. *Spike:* the sequential-child case above, with the \$15 cap held this way, raised after the second call (\$20 recorded) instead of charging nothing.
- A `_SwarmCostNode(_CostLimit)` in the cost tree. Its `record(cost)` adds to the accumulator only while the token tree is suspended (`token_limit_tree.is_suspended()`, true for approval and review calls, `src/inspect_ai/approval/_apply.py:50`). Its `_check_self` compares the same accumulator. Calls outside a suspension are therefore counted once, by the token-tree node, and suspended calls once, by the cost-tree node. Pre-dispatch checks run both `check_token_limit()` and `check_cost_limit()` (`_model.py:3036-3044`), so a reached pair refuses calls on either path.

The swarm meter, the cost cap, each lever, each cost share, the final meter and each re-created member cost limit are pairs. The token cap and token shares are single `_SwarmTokenNode`s with a token limit; a pair's token-tree node also meters tokens for the ledger. Unpriced calls add nothing to the accumulator and are counted separately, so a cost cap never meters them (the ledger reports them).

Swarm nodes differ from Inspect's in three more ways:

- **Ownership.** Each has an owner: the swarm, or one member. The controller recognises its own errors by `error.source` identity against the nodes it created, split into *shared* nodes (swarm meter, caps, final meter) and *member-owned* nodes (that member's lever, shares and re-created limits).
- **Messages.** When exceeded for a native member they raise `LimitExceededError` as usual, with the kind's `type`, except the lever, which raises `type="custom"`. The message names the swarm and the reason ("Swarm cost cap reached", "Stopped by the swarm: verified answer"). They write no `SampleLimitEvent`, which the viewer would show as a sample limit; the controller writes an `InfoEvent` instead ([Ledger](#the-ledger)).
- **Bridged members.** A node exceeded while checking for a bridged member does not raise, because a raise in the sandbox service ends the sample ([Bridged generations](#bridged-generations)). It records why the member must stop and cancels that member's cancel scope synchronously; if the node is a swarm cap, it also calls the controller's `stop("swarm_cap")`. The bridged agent's pending call is cancelled at its next await, before or just after dispatch.

**Knowing a member is bridged.** A member is bridged from its first instruction, so the swarm decides before the member starts rather than inferring it from a generation (bridge compaction and filters run before the bridge's mark, [Bridged generations](#bridged-generations)):

- `member(agent, bridged=None)` takes an explicit `bridged: bool | None`.
- With `None` (the default), the member counts as bridged when its agent's registry name is in the `inspect_swe/` namespace (`registry_info(agent).name`; extension packages register as `package/name`, `src/inspect_ai/_util/registry.py:252-259`). That covers all of inspect_swe's agents, headless and interactive. Every other agent counts as native.
- A node checked inside `in_bridge_model_generate()` for a member counted as native marks it bridged and takes the bridged branch, which catches a bridged agent the rule missed at its first marked generation. Before that point (an initial bridge compaction or filter generation) such an agent can still end the sample. This is documented in `member()`, which names `bridged=True` as the remedy, and the ledger records the cause.

The wrapper sets a `_current_member` ContextVar so a node knows which member it is checking for.

The lever is a pair with no limit. It never raises until the controller stops the member by setting `lever.limit = int(lever.usage)` (tokens). Its accumulated `ModelUsage`, cost and call counts are the member's meter for the ledger. The ledger reads the token-tree node's accumulated `ModelUsage` from `_TokenLimit._usage`.

This keeps the sample's own nodes untouched: a sample limit reached inside a bridged member still ends the sample, as it should.

### What each kind does in a swarm

| Kind | Swarm cap | Member limit | Split | Notes |
|---|---|---|---|---|
| cost | yes (`Budget.cost`, a pair) | yes | yes | Recommended for comparisons, and required when members use different models. |
| token | yes (`Budget.tokens`, any metering type) | yes | yes | For single-model swarms or unpriced models. |
| time | yes (`Budget.time`, a controller timer) | yes | no | A member time limit cancels the member, so its in-flight calls are unknown. |
| turn | no | yes, with Inspect's N+1 semantics | no | A sample turn limit is swarm-wide. With no pre-dispatch check, a swarm turn cap would let every active member make one more paid call. |
| message | no | yes | no | Per conversation. A node above members has no meaning. |
| working | no | rejected (`ValueError`) | no | Never enforced below the sample, and collapses under contention. |

- **Member limits** come from `member(agent, limits=[...])`. The swarm reads each object's value (and metering type, for tokens) and creates fresh nodes from them on every invocation: a pair for cost; a `_SwarmTokenNode`, `_SwarmTurnNode` or `_SwarmMessageNode` for tokens, turns and messages; and a new Inspect `time_limit(value)` for time. The time limit keeps its cancel-scope semantics, so it stops a bridged member without going through the sandbox service.
- **The task's objects are never entered.** Limits are single-use ([Limit trees](#limit-trees)), so entering the task's objects would fail on a second member from `member(..., count=k)`, on the next sample, or on a second invocation. Re-creating them per invocation is what deepagent does for its subagents.

### Splits

`Budget.split` divides each capped kind among the k members:

- `"pool"` (default): no share nodes. Members draw on one budget, and a member may spend all of it. This is the team@k arm of swarm.md's first eval question.
- `"equal"`: every member has weight 1.
- A tuple of weights, one per member in roster order.

Allocation, with weights `w_i` summing to `W`:
- **Cost:** member i's share is `cap * w_i / W` dollars (a pair).
- **Tokens:** shares are whole numbers that sum exactly to the cap. Each member first gets `floor(cap * w_i / W)`. The remaining tokens go one at a time to the members with the largest fractional parts, ties broken by roster order.
- **Zero shares:** a token share of zero (a cap smaller than the roster can split) is a `ValueError` at start, naming the cap and the member count. A cost share cannot be zero because weights and caps are positive.

A member that exhausts its share stops (its status is `share`), and its unused share is not given to others, so the split is a fixed experimental condition. A split with no capped kind is a `ValueError`. Shares nest under the swarm cap, so the cap still binds when shares add up to more than the cap because of overshoot.

### Stopping

Every stop goes through one controller method, `stop(reason)`. Reasons are a closed set: `all_done`, `verified`, `swarm_cap`, `swarm_time`, and later `quiescence`.

1. **Soft stop.** For every member still running, set its lever to its current usage. A native member raises at its next pre-dispatch check (or after its in-flight call completes), so calls already in flight finish and are recorded, and no new call starts. A bridged member is cancelled at its next check instead (see [Swarm nodes](#swarm-nodes)).
2. **Grace.** Wait until every member has returned, or the effective grace has passed. Effective grace is `Budget.grace`. When the sample has a time limit, it is clipped to half the sample's remaining time at the moment of the stop, so the final step always keeps the other half.
3. **Hard stop.** Cancel the cancel scope of every member still running. Calls they have in flight are cancelled, and the ledger then reports the totals as incomplete ([The ledger](#the-ledger)).

Why a soft stop:

- It keeps in-flight usage known: the soft-stop spike recorded every call, where cancelling would have left 3 of 9 unknown.
- It bounds overshoot the same way a reached cap does, by the calls holding a connection slot ([Overshoot](#overshoot-and-connection-slots)), plus at most one native compaction per running agent loop.
- It needs nothing from inspect_ai beyond the limit setter.

Grace bounds members that are inside a long tool call (a test suite in the sandbox) when the stop arrives. A member stopped while a tool call is running sees, at worst, a `limit` tool error from a nested model call, and then its next generate refuses. Grace does not bound a bridged member's cleanup after cancellation: the bridge kills its proxy in a shielded scope (`src/inspect_ai/agent/_bridge/sandbox/bridge.py:271-275`), which the hard stop waits for.

Triggers:

- **Token and cost caps** trip themselves. The first member to raise a swarm cap error has its wrapper call `stop("swarm_cap")`. The other members would trip the reached cap at their next dispatch anyway, and the soft stop makes that uniform. A cap reached inside a bridged member calls `stop()` from the node ([Swarm nodes](#swarm-nodes)).
- **The time cap** is a timer task in the controller's task group, not an Inspect `time_limit`. It calls `stop("swarm_time")`, so the time cap gets the same soft stop. The sample's own time limit stays a hard stop around everything.
- **Topology stops** (`verified`, and later `quiescence`) come from the controller ([Controller](swarm.md#controller-topology-termination-final-answer)).

**Member wrapper and statuses.** `run()` returns a bare owned limit error but lets a grouped one escape ([Limit trees](#limit-trees)), so the wrapper classifies every leaf itself:

```python
async def _run_member(m: Member) -> None:
    token = _current_member.set(m)
    nodes = m.create_nodes()  # lever, shares, re-created limits: fresh per invocation
    try:
        with anyio.CancelScope() as m.scope:
            _, err = await run(m.agent, m.input, limits=nodes, name=m.name)
        if m.scope.cancelled_caught:
            m.status = m.stop_cause or "cancelled"  # hard stop, or the bridged branch
        else:
            m.status = _status_for(err)  # submitted, stopped, share, member_limit:<type>
    except Exception as ex:
        group = ex if isinstance(ex, BaseExceptionGroup) else ExceptionGroup("member", [ex])
        caps, rest = group.split(lambda e: _source_in(e, swarm.cap_nodes))
        owned, foreign = (rest.split(lambda e: _source_in(e, nodes))
                          if rest is not None else (None, None))
        if foreign is not None:
            m.status = "errored"
            raise _unwrap(foreign)  # a single leaf bare, several as a group
        if caps is not None:
            m.status = "swarm_cap"
            swarm.stop("swarm_cap")
        else:
            m.status = _status_for(_first_leaf(owned))
    finally:
        _current_member.reset(token)
```

- `_source_in(e, ns)` is true for a `LimitExceededError` whose `source` is one of `ns`.
- A group containing only the member's own limits (a member limit, its share, or its lever during a soft stop) gives the same status as the bare error would.
- A group with any foreign leaf re-raises only the foreign leaves.
- Swarm cap leaves are removed from what propagates. A cap leaf with no foreign leaves stops the swarm.

Member statuses are a closed set: `submitted`, `stopped` (soft stop), `share`, `member_limit:<type>`, `swarm_cap`, `cancelled`, `errored`. Each member's status and stop cause go into its evidence and the ledger.

### Exhaustion and errors

The controller runs members in its own task group inside the swarm nodes. Its handling, in order:

1. **Own nodes.** No error whose source is a swarm node leaves a member wrapper, bare or grouped. Shared caps become a `stop()`, and member-owned nodes become a status. This also covers a cap reached after a tool or handoff turned it into a message, since the member then raises at its next generate.
2. **Foreign errors** (a sample limit, `TerminateSampleError`, a member's crash) propagate out of the wrapper, already stripped of swarm leaves. The task group cancels the other members, so their in-flight calls become unknown. The controller removes any swarm leaves from the task group's exception again with `ExceptionGroup.split()`, so that a sample limit is not recorded as a swarm cap. It re-raises a single remaining exception bare and several as a group, as `collect()` does. The runner then records a sample limit as the sample's limit, as for any agent.
3. **Cancellation from outside** (the sample's time or working limit, the sample's cost or token limit surfacing from a bridged member, an operator interrupt) arrives as a cancellation. The controller does no awaiting work in that path.
4. **On every exit path** the controller writes the ledger and the output in a synchronous `finally`. `transcript().info()` and store writes are synchronous (`src/inspect_ai/log/_transcript.py:544`), so they run even during cancellation. When the cause is not an exception it can see, it reads it from `sample_active().limit_exceeded_error` (a working limit or a bridged sample limit), from the sample's time node, or from the active sample's interrupt action, and records `sample_limit:<type>` or `operator`.

Member limits are not exhaustion. The wrapper records them as statuses, and the member's siblings continue.

The controller keeps the swarm's own `AgentState.messages` to the task input plus the final assistant message. Member conversations live in their spans and timelines. A concatenation of members' conversations would be checked against the sample's message limit when `as_solver()` copies it ([How errors reach the runner](#how-errors-reach-the-runner)).

### The final step and the reserve

After the members have stopped, the controller leaves the cap nodes and runs the final step ([Final answer](swarm.md#controller-topology-termination-final-answer)) inside the final meter. Only the sample's limits apply to it, so the reserve is whatever the sample's limits have left.

- **What the reserve is for.** Of the final modes, only `synthesize` (and a later integration member) makes model calls. `verify`, `vote`, `first` and `reporter` pick among existing submissions, so for them the reserve is headroom for overshoot. It keeps the sample's limit from tripping during the stop, so the stop stays orderly: members soft-stopped, ledger complete, stop reason `swarm_cap` rather than a sample limit.
- **Provisional answer.** For every mode except `synthesize`, the provisional answer the controller keeps current ([Accounting](swarm.md#observer-evidence-accounting-and-metrics)) is the same answer the final step would pick. A sample limit that ends the swarm early therefore costs these modes the orderly stop, not the answer.
- **When the reserve holds.** The reserve covers the stop if it is at least the overshoot plus the final step's cost. The swarm's caps see every recorded call before any descendant check can raise, cost included, so a reached cap admits only calls that already hold a connection slot ([Overshoot](#overshoot-and-connection-slots)), plus one native compaction per running agent loop (member or subagent). Each call is at most the model's context window of input plus its `max_tokens` of output.
  - With prices, that is a dollar bound. It depends on the models' configured maximum connections and call size, not on the member count, which refines swarm.md's per-member bound.
  - Fan-out inside a member (a synchronous deepagent's parallel subagent calls) is covered too, because those calls also hold connection slots. So are approval calls, which the cost pair meters through the cost tree, but not unpriced calls, which no cost cap meters.
  - The bound is loose: with 10 connections and 200,000-token contexts it is 2 million tokens. For the budgets users can afford, the reserve remains best effort.
- **Default.** 5% of each sample limit, recorded with its use in the ledger so users can tune it. It is a guess, chosen to be small next to the budgets the scaling experiments use and large enough for a few calls.

### The ledger

The ledger is what analysis compares across arms (swarm.md: "caps are stopping rules; comparisons use realized cost"). It is written once, in the controller's `finally`, as a versioned `InfoEvent` (`source="inspect_swarm"`) and as a `StoreModel` under `inspect_swarm:ledger`, so scorers and the analysis helpers read the same record.

```python
class Meter(BaseModel):
    usage: ModelUsage          # token-tree node: every non-suspended recorded call
    cost: float                # the pair's accumulator: priced calls, suspended ones included
    recorded_calls: int        # calls recorded into the token-tree node
    unpriced_calls: int        # recorded calls whose ModelUsage had no cost, counted per call
    suspended_cost: float      # cost recorded while token metering was suspended

class MemberLedger(BaseModel):
    name: str
    status: str                # closed set, see Stopping
    bridged: bool
    meter: Meter               # the member's lever
    cache_replays: int         # events with cache="read": charged zero
    cancelled_calls: int       # events cancelled in flight: usage unknown
    no_usage_calls: int        # events completed with output but no usage
    cancelled: bool            # the member's scope was cancelled (hard stop, time limit, bridged branch)

class SwarmLedger(BaseModel):
    version: int = 1
    budget: Budget             # as configured
    caps: dict[str, float | None]           # derived or explicit, per kind
    sample_limits: dict[str, float | None]  # at swarm start
    members: list[MemberLedger]
    final: Meter               # the final meter
    final_cancelled_calls: int
    final_no_usage_calls: int
    swarm: Meter               # the swarm meter: members, final step, anything else the swarm ran
    sample_delta: ModelUsage   # the sample's model usage over the swarm's lifetime
    unattributed: ModelUsage   # tokens: sample_delta minus swarm.usage; cost: sample_delta's cost minus swarm.cost
    unpriced_models: list[str] # models in sample_delta with tokens but no cost
    overshoot: dict[str, float]  # per capped kind: usage at the end minus the cap, if positive
    stop_reason: str           # closed set, see Stopping and Exhaustion
    interrupted: bool          # a foreign error or outside cancellation ended the swarm
    resumed: bool              # the sample resumed from a checkpoint (see Compatibility)
    total_complete: bool
    attribution_complete: bool
```

Sources and coverage:

- **Per-member usage comes from the member's lever**, not from summing model events. The lever's token-tree node records every call made in the member's context:
  - the member's own generations;
  - its synchronous subagents and any tool that calls a model;
  - summary and native compaction;
  - bridged generations;
  - background children once they are supported, since they inherit the member's context.

  Its cost-tree node adds the cost of approval and review calls, which are suspended from token metering. Pricing is checked per call as it is recorded, before aggregation, because `ModelUsage.__add__` hides a missing cost ([What the nodes record](#what-the-nodes-record)). The lever excludes cache replays (they record nothing) and calls with no usage (nothing to record). This attributes native compaction, which swarm.md had to report as unattributed, and avoids the token-node/cost-node disagreement.
- **Counts come from model events** in the member's and the final step's spans: cache replays (`cache="read"`, `src/inspect_ai/event/_model.py:135`), calls cancelled in flight, and calls completed with output but no usage. Native compaction has no event. A compaction cancelled in flight is therefore invisible, so completeness does not rely on counting it (below).
- **The sample delta** is the sample's model usage at the end minus at the start. `unattributed` is what it holds beyond the swarm meter: the tokens of approval and review calls (their cost is in the meter, through the cost-tree node), plus anything that ran in the sample during the swarm outside its contexts. `unpriced_models` lists the models whose delta has tokens but no cost. That catches unpriced calls on paths no node records, such as an unpriced approval model. A model priced for some calls and not others (a served-model fallback) still shows a cost and is caught only by the per-call count.
- **Totals per arm** stay as swarm.md set them: Inspect's sample `model_usage`, which every arm has. The ledger reconciles with it: `unattributed` must not be negative in tokens or, beyond a relative tolerance of `1e-9` of the delta's cost, in cost. The tolerance absorbs summation order, since the sample sums per model and the meters sum per call. A negative remainder would mean the swarm counted a call twice, so the implementation logs an error and tests pin it.

Claims. Completeness is judged over the whole interval the ledger covers (members, final step and auxiliary calls), and is conservative where calls cannot be observed:

- `total_complete` requires all of:
  - no agent loop was cancelled: no member `cancelled`, no `interrupted`, and the final step not cancelled. A cancelled loop may have had a native compaction in flight that left no trace.
  - no cancelled calls and no no-usage calls among members or the final step;
  - zero `unpriced_calls` in the swarm meter;
  - `unpriced_models` empty.

  Otherwise the arm's total is a lower bound, reported with the counts that cleared the flag. These calls are missing from the sample's `model_usage` too, so the flag applies to the arm's total, not only to the swarm's figures.
- `attribution_complete`: `total_complete`, and `unattributed` has zero tokens and its cost is within the tolerance of zero. Otherwise per-member figures are lower bounds, and the arm's total is unaffected. Approval or review model calls always leave their tokens unattributed (their cost is attributed), so runs that use them report per-member token figures as lower bounds.

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
- Bridged generations are metered by the member's nodes. The swarm's nodes stop a bridged member by cancelling it, not by raising ([Swarm nodes](#swarm-nodes)), so swarm caps, shares, member limits and soft stops work without ending the sample. This needs the member to be known as bridged before it starts: inspect_swe agents are detected, and any other bridged agent must pass `bridged=True`.
- Its in-flight call at that moment is cancelled and unknown.
- Every sample limit still ends the sample when reached inside a bridged generation, as it should. The ledger records the cause.
- A vendor swarm as one member (Codex multi-agent, Claude Code teams) is metered as one member: all its internal agents' generations go through its bridge. Its member limits are the vendor swarm's budget.
- A user-supplied bridged host tool that calls a model and reaches a swarm node raises inside the sandbox service, which ends the sample. The swarm's own tools make no model calls.

**Compaction.**
- Summary compaction is a generation: metered, counted as a turn, refused before dispatch once a node is reached. It checks the message limit against the summarisation input, which can trip a member's message limit one message early.
- Native compaction is metered by the lever but has no pre-dispatch check, so each agent loop may dispatch one native compaction after a stop. Under `CompactionAuto`, a native compaction that trips a node is caught. Auto logs "Native compaction failed" and falls back to summary compaction, which is refused before dispatch, and the error reaches the member. That costs one misleading warning, not an extra call.
- Compaction thresholds are about context windows, not budgets. A member near its share still compacts when its context fills.

**Approvals and monitors.** Approval-policy model calls are suspended from token and turn nodes, so they never trip a token cap, a token share or a turn limit. Their cost is metered by the cost-tree half of every pair, so they count against cost caps and cost shares and are attributed to the member in the ledger. Their tokens appear as unattributed. Whether inspect_sentinel's monitors will run under the same suspension is for sentinel to settle; the ledger reports whatever they record.

**Sample limit overrides.** An operator can retune the sample's time, token and message limits while it runs (`src/inspect_ai/util/_limit_overrides.py`). The swarm's derived caps are fixed at start, so a retune moves the reserve, not the cap. A cut below the swarm's cap means the sample limit ends the swarm first.

## Changes to swarm.md

This branch makes small edits to swarm.md, all on this topic:

- [Limits and cost](swarm.md#limits-and-cost): two caveats added (working limits; bridged members) and a link here.
- [Entry point](swarm.md#entry-point): the sketch's `budget=cost_limit(40.0)` becomes `budget=Budget(cost=40.0)`.
- [Accounting](swarm.md#observer-evidence-accounting-and-metrics): the cost cap is held in the token tree, because Inspect's cost nodes can miss calls; the ledger's per-member figures come from token meter nodes rather than cap nodes; per-member attribution from the member's meter node, so native compaction is attributed and no longer the unattributed remainder; the reserve and exhaustion bullets refined (derived caps, soft stop, the connection-slot bound, own errors removed from mixed groups); the claims bullet aligned with this document's completeness rules; a link here.
- [Testing](swarm.md#testing) and [Implementation plan](swarm.md#implementation-plan): the bullets that named the unattributed remainder updated.

The overview is unchanged: no main decision moves. Its parenthetical listing native compaction among the calls the ledger cannot see is now conservative rather than wrong.

## Alternatives considered

**Ledger from model events (swarm.md's M1 plan).** Sum each member's `ModelEvent` usage, charging cache replays zero.
- No private fields.
- It misses native compaction, which then needs an inspect_ai change (usage on `CompactionEvent`) to attribute, and it needs every call to have an event in the right span.
- The lever is a node the swarm needs anyway for stopping, so its meter comes free. Events remain the source for counts.

**Inspect's `cost_limit()` as the swarm's cost cap** (swarm.md's sketch).
- No subclassing.
- It never sees a call that trips a token node below it, and with child token limits that happens on every call ([Each limit kind](#each-limit-kind)). A cost cap that can be bypassed indefinitely cannot be a stopping rule, so the cap is metered in the token tree.

**Detect bridged members from the bridge's generate mark alone.**
- No declaration and no registry lookup.
- Bridge compaction and filters run before the mark, so a limit reached there would end the sample ([Bridged generations](#bridged-generations)). The mark is kept only as a fallback.

**Stop by cancellation only.** Cancel members' scopes on every stop.
- Simplest, and fastest to stop.
- Every call in flight becomes unknown, so most capped runs would report lower bounds. The soft stop costs only a lever per member and a grace period.

**One shared stop node instead of a lever per member.** Lower a single node above all members.
- Equivalent for native members.
- A lever per member also meters each member, and can stop one member (a bridged one by cancelling) without touching the others.

**Fix the bridge in inspect_ai instead of special-casing bridged members.** Route a `LimitExceededError` from a bridged generation through `bridge.request_fail()` (as `ModelRefusalError` already is) instead of the sandbox service's `limit_exceeded()`. It would then unwind the bridged agent and reach `run()` or the controller like a native member's error.
- Cleaner, and it fixes `run(claude_code(), limits=[...])` for everyone, which today ends the sample.
- It changes inspect_ai behaviour that its tests pin. The swarm would still need its own nodes for messages and the soft stop.
- Not chosen as a dependency; listed under [Not this design](#not-this-design) as an inspect_ai fix worth proposing on its own. If it lands, the swarm's bridged branch, the `bridged=` declaration and the registry rule become redundant and can be removed.

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
  - `_TokenLimit`, `_CostLimit`, `_TurnLimit`, `_MessageLimit` and `_TokenLimit._usage`;
  - `token_limit_tree.is_suspended()`;
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
  - a token node's `ModelUsage` carries the cost of a call that trips a token limit, while a cost node does not, and sequential child token exhaustion bypasses a `cost_limit()` indefinitely;
  - a grouped owned limit error escapes `run()`;
  - a `Limit` object cannot be entered twice;
  - bridge compaction runs before `in_bridge_model_generate()` is set;
  - a limit reached inside a tool becomes a `limit` tool error and the member raises at its next generate;
  - lowering a node's limit stops callers at their next call without cancelling in-flight calls.
- **Budget validation:** `ValueError` for each invalid field: NaN, infinite or non-positive caps; a reserve outside `[0, 1)`; negative or non-finite grace; non-positive or non-finite weights; a weight count that differs from the roster.
- **Derivation:**
  - caps from the sample's cost, token (with metering type, rounded down) and time limits, minus the reserve and usage so far;
  - an error when a derived cap is not positive; a 60-second sample time limit derives a cap and does not fail;
  - a warning for an explicit cap above the sample's remaining limit, and for a sample working limit;
  - `PrerequisiteError` for a member model without cost data under a cost cap.
- **Nodes:**
  - swarm nodes raise with their own messages and write no `SampleLimitEvent`;
  - the cost cap pair stops the sequential child-exhaustion case after the call that crosses it, and charges approval calls (made under `suspend_token_limit()`) exactly once;
  - inside `bridge_model_generate()`, an exceeded swarm node cancels the current member's scope instead of raising, and a cap also calls `stop()`. This tests the bridged branch without a sandbox;
  - a member declared `bridged=True` is cancelled, not raised, when its first request's bridge compaction reaches a swarm node. The test goes through inspect_ai's `bridge_generate()` helper with a test bridge whose compaction calls a mock `compact()`, so it runs in portable CI;
  - inspect_swe registry names are detected as bridged, and other agents as native;
  - a member's token, cost, turn, message and time limits are re-created per invocation with the same values and metering type. They work for `member(..., count=3)`, across two samples and on a second invocation, and stop the member with the right status.
- **Stopping:**
  - soft stop records every in-flight call and leaves no unknown calls;
  - grace expiry cancels a member stuck in a slow tool, counts its in-flight call as unknown and clears `total_complete`;
  - grace is clipped to half the sample's remaining time;
  - the time cap stops members softly and leaves the final step its time;
  - each stop reason and member status is recorded.
- **Exhaustion:**
  - the swarm cap recovered when it arrives bare, in a group, or after a tool error;
  - a member limit, a share and a lever stop raised inside a custom agent's task group (grouped) give the same status as when bare, and siblings continue;
  - a group mixing a member's own limit with a foreign error re-raises only the foreign leaf;
  - sample limits, `TerminateSampleError` and crashes propagate, with the swarm's own errors removed from mixed groups;
  - a sample time limit and a working-limit cancellation leave the ledger and the provisional answer written;
  - the swarm's returned messages stay within a small sample message limit.
- **Splits:**
  - equal and weighted shares, and a member stopping on its share while others continue;
  - token shares that do not divide evenly sum exactly to the cap, with remainders by largest fraction then roster order; a token cap smaller than the roster is an error;
  - shares never redistributed;
  - errors for a split with no capped kind or wrong weights.
- **Ledger:**
  - members' levers sum with the final meter to the swarm meter;
  - `unattributed` is non-negative and within tolerance for a run mixing two priced models;
  - an approval call's cost is attributed to its member, and its tokens appear as unattributed;
  - cache replays counted and charged zero;
  - native compaction (a test `ModelAPI` implementing `compact()`) is attributed to the member;
  - each of these clears `total_complete`: an unpriced call mixed with priced ones in one member; an unpriced model used only for native compaction; an unpriced approval model; a cancelled native compaction (no event); a response with no usage; a cancelled `synthesize` call; a member hard-stopped by grace.
- **deepagent:** a synchronous deepagent member with parallel subagent calls overshoots by its fan-out, within the connection-slot bound computed from the mock's `max_connections`.
- **Bridged members, end to end** (Claude Code or Codex through inspect_swe, a swarm cap and a soft stop) need Docker and provider keys. They are marked slow and run by hand or on a schedule, never in PR CI.

## Implementation plan

All in inspect_swarm, as part of M1 ([Implementation plan](swarm.md#implementation-plan)). Each step is one PR, reviewed before the next.

1. **Swarm nodes and the budget parameter.** `Budget` with validation, cap derivation, the swarm node classes and cost pairs (ownership, messages, per-call pricing, the bridged branch), bridged detection, and the pinned-behaviour tests. Files: `src/inspect_swarm/_budget.py`, `tests/test_budget.py`.
2. **Members and stopping.** The member wrapper with lever, shares, re-created member limits and leaf-by-leaf error classification; `stop()` with soft stop, grace and hard stop; the time-cap timer; member statuses and stop reasons. Files: `src/inspect_swarm/_member.py`, `_swarm.py`, `tests/test_swarm_limits.py`.
3. **Exhaustion and the final step.** Recovery of own caps, propagation with own errors removed, the synchronous `finally`, the final meter and the provisional answer. Files: `_swarm.py`, `_final.py`, tests.
4. **The ledger.** `MemberLedger`, `SwarmLedger`, event counting, reconciliation and claims, written as an `InfoEvent` and a store entry. Files: `_budget.py` (or `_ledger.py` if it grows), `_evidence.py`, tests.
5. **Splits and the sweep example.** Share nodes and validation, and a task example in the docs showing the three sweeps. Files: `_budget.py`, tests, `README.md`.

## Open questions

1. **Should the swarm's cap default to deriving from the sample's limits, minus a 5% reserve?** The alternative is no default cap: the swarm stops only on sample limits unless `Budget` sets one.
   - (a) Derive by default, so arms share one number and the swarm stops in an orderly way at 95%.
   - (b) No default, so a swarm arm spends to the same sample limit as the single-agent arm, at the cost of ending on a sample limit with no final step and no soft stop.

   Recommendation: (a). The orderly stop is what keeps the ledger complete, and analysis uses realized cost, so the 5% gap does not bias comparisons. With a sample time limit, (a) also stops the members at 95% of it. Grace is clipped to half the remaining time, so a short limit does not fail at start (a 60-second limit stops at 57 seconds with up to 1.5 seconds of grace).
2. **Propose the bridge fix to inspect_ai now?** The swarm does not need it, but `run(bridged_agent, limits=[...])` ending the sample is a general bug. The fix would let the swarm drop its bridged branch and the `bridged=` declaration, which a custom bridged agent outside inspect_swe must otherwise remember. Recommendation: file it as an inspect_ai issue now, and propose the PR when bridged members are first used. It is in [Not this design](#not-this-design) either way.

## Not this design

Adjacent problems noticed and left out, for Ransom to file if wanted:

- **Nested `working_limit()` is never enforced** (inspect_ai). Only the sample's root node is polled (`_limit.py:912-947`), so `run(agent, limits=[working_limit(t)])` silently does nothing.
- **Sample working time under concurrency** (inspect_ai). Waiting counts whenever any task waits, so concurrent agents queueing for connections drive working time towards zero. This affects deepagent's background subagents as well as swarms.
- **`turn_limit(N)` pays for N+1 generations** (inspect_ai). There is no pre-dispatch turn check, unlike token and cost.
- **A limit reached inside a bridged agent ends the sample whatever its source** (inspect_ai). Route it through `bridge.request_fail()` so agent-scoped limits on bridged agents behave as on native ones ([Alternatives](#alternatives-considered), [open question 2](#open-questions)).
- **Native compaction has no pre-dispatch limit check and no model event** (inspect_ai), so it can dispatch after a limit is reached, and a cancelled one leaves no trace.
- **Cost nodes miss every call that trips a token node** (inspect_ai), repeatably with child token limits (`_model.py:3089-3095`). The swarm meters cost in the token tree instead; a general fix would record cost before the token check.
- **`run()` does not catch grouped owned limit errors** (inspect_ai, `_limit.py:179-186`). The swarm's wrapper classifies leaves itself.
- **`CompactionAuto` swallows a `LimitExceededError` from native compaction** (inspect_ai) and logs it as a compaction failure before falling back.
- **Checkpoint and resume of nested limit nodes** (inspect_ai and inspect_swarm). Only sample roots are restored.
- **Per-member waiting time** for the latency metrics (inspect_swarm metrics work).
