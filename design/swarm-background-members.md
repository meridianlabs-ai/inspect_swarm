# Inspect Swarm: background deepagent members

Status: proposed, 2026-10-16. Issue: none. Author: agent (Claude), reviewed by Codex; see the PR.

## Overview

Each member's background agents belong to that member. They run alongside the member and spend against its budget. They stop with it: when the member ends, or when the swarm stops, they get the swarm's grace period to finish what they are doing, and whatever is left is then cancelled. The member's result is final, and its controller hears of it, as soon as the member's own agent ends. The swarm still waits for the member's background agents to finish before it finishes, so the ledger counts all their work as the member's. Members built with `deepagent(background=...)`, which M1 refuses, are accepted.

Today a background agent belongs to the sample, not to the member that started it. It keeps running after its member ends, and a limit it reaches ends the whole sample.

Two pieces make the change. inspect_ai gains a small private hook that says where `background()` starts work. When nothing sets it, `background()` works exactly as it does today. The swarm sets the hook for each member to an owner that lives in inspect_swarm. The owner runs the member's background work. A child stopped by one of the member's limits or the swarm's caps ends alone, without stopping its siblings or the member. The owner drains the work when the member ends. deepagent's background subagents start through `background()`, so they land in their member's owner. A swarm cap reached anywhere in a member starts the swarm's stop at once.

## Why

Both of Epoch's LeanOpenProblems configs run `deepagent(background=64)` members ([swarm-lean-open-problems.md Recommendation 3](swarm-lean-open-problems.md#3-m2-background-deepagent-members); their `apn/solver.py:37`, `:145`, at `0aff742d6`). M1 refuses such members (`src/inspect_swarm/_member.py:299-304`). Without them, a port has to run `deepagent()` synchronously: each subagent call blocks its member until the subagent finishes, instead of running alongside the member's other work. That changes the experiment, so the port's results would not be comparable to Epoch's.

M1 refuses them because of where background work runs. `background()` starts its task in the sample's task group. The task copies the member's context (its limits, its identity, its tool state), but it is not in the member's task tree. So nothing the swarm controls can stop it, wait for it or catch its errors ([swarm.md Members](swarm.md#members)). Two things go wrong, both measured here (spikes in [Current behaviour](#current-behaviour)):

- **Children outlive their member.** deepagent does not wait for its children when it returns; inspect_ai's design calls this "abandon on parent exit". A member whose agent collects every child before returning is fine. Most swarm members do not end that way, though. They are stopped: by the swarm's soft stop when another member has solved the problem or the controller is done, by a cap, by the time cap, or by their own share or limits. A stopped agent returns at its next model call, without collecting its children. A member can also submit while children still work (one child found the answer), or fail with an error. In each case its children run on, spending against the member's limits, after the member is reported ended. The swarm cannot say when it is done or what each member spent, and its grace period and hard stop never reach them.
- **Their limit errors reach the sample.** A child that reaches one of its member's limits or a swarm cap raises the limit error into the sample's task group. Inspect records it as the sample's limit and ends the sample. In Spike 2 a member-level token limit of 20,000 around `run(deepagent(background=True))` ended the sample with `limit: token 20000`, and the code after `run()` never ran. Closing a member's lever is how the swarm stops a member, so the first child refused by it would end the whole sample.

Plain Inspect does not hit these. There a deepagent is normally the solver's last step, so when it returns, sample teardown cancels the leftover children at once (`src/inspect_ai/_eval/task/run.py:2947-2950`). Its limits are usually the sample's own, and a child that reaches one should end the sample.

Owning the children is not enough on its own; how they are stopped matters too. A model call cancelled in flight records no usage ([swarm-limits.md What the nodes record](swarm-limits.md#what-the-nodes-record)). In Epoch's configs the children do most of the work, so at any stop most of a member's calls in flight are its children's. If children were cancelled at once when the member ends, or if the first child refused by the closed lever cancelled its siblings, those calls would go unrecorded and the ledger would be incomplete. The swarm already stops members softly for this reason: in its soft-stop spike, cancelling would have left 3 of 9 calls unknown ([swarm-limits.md Stopping](swarm-limits.md#stopping)). This design gives children the same soft stop: a child stopped by a limit ends alone, and children get the grace period before they are cancelled.

M2's test is that Epoch can port their swarm ([swarm-implementation-plan.md M2](swarm-implementation-plan.md#m2-ready-for-external-usage)). This design specifies the inspect_ai change (AI-5) and the swarm side (M2-5).

## Goals and non-goals

Goals:

- A private seam in inspect_ai through which a caller decides where `background()` starts work, with deepagent's dispatch using it, and no change for any caller that does not set it.
- An owner in inspect_swarm, one per member, set as that seam's target: its lifetime, cancellation, drain, error handling and counts.
- Draining it at member end and at the swarm's stop (soft stop, grace, hard stop, time cap), with the M1 refusal lifted.
- How the ledger, member statuses, stop reasons, member limits and the swarm budget count background children, and how a background `general()` child reaches the shared-memory channel.
- The follow-up edits this implies for the plan and the other designs.

Non-goals:

- Public API in inspect_ai. The seam is private and inspect_swarm reaches into it, as it already reaches into inspect_ai's private limit classes (decision: Ransom, 2026-10-16: "I'm not worried about the public API surface - I think this should just be private if needed in inspect and swarm can reach into internal private methods").
- Changing deepagent outside the swarm. In plain Inspect, a scoped limit reached in a background child still ends the sample (Spike 2), and children are still abandoned when the deepagent returns. Ransom chose to leave both as they are until customers ask (decision: Ransom, 2026-10-16: "We'll do if we receive customer feedback"); AI-5 does not propose it.
- Unusual error shapes. The design covers the expected cases: a bare limit error, or a bare control-flow error, reaching the member's owner. Grouped errors, and the tool pipeline's flattening of a tool's exception group, stay as inspect_ai has them; they are known limitations, fixed if real use hits them (decision: Ransom, 2026-10-16: "I'd like to avoid going far down inspect error-paths"; listed under [Not this design](#not-this-design)).
- Changing what deepagent's children do or how its lifecycle tools report them. Statuses, the `AGENT-N` handles, `max_background` and the reminder hook stay as they are.
- Owning work started outside `background()`: raw `asyncio.create_task`, inspect_ai's internal `run_in_background()` (eval-level logging, batching and throttling), processes left running in a sandbox. [Security](#security) says what this leaves open.
- Background work started by a controller. No controller does; see [Not this design](#not-this-design).

## Current behaviour

Checked against inspect_ai `main` at `e31f6e9ed` (2026-10-16), this repository's `main` at `609a870`, the M1-4 branch `m1-4-runtime-core` at `ede24f8` (not merged), and inspect_ai 0.3.281 installed here for the spikes.

### `background()`

`background(func, *args)` (`src/inspect_ai/util/_background.py:12-74`, exported at `src/inspect_ai/util/__init__.py:38`):

- requires an active, started sample, raising `RuntimeError` otherwise (`:42-50`);
- wraps `func` so that an exception other than `LimitExceededError`, `TerminateSampleError` and `ModelRefusalError` is logged as "Background worker error", and re-raises every exception (`:53-71`);
- starts it with `sample.tg.start_soon(run)` (`:74`). anyio copies the caller's context into the new task, so the task sees the caller's ContextVars: its limit nodes, span, tool-state scope and, in a swarm, the member.

The sample's task group is created around the solver (`src/inspect_ai/_eval/task/run.py:2977-2978`), handed to the active sample (`:2786`, `src/inspect_ai/log/_samples.py:300-302`) and cancelled when the solver finishes, "so that any coroutines created w/ background() are cancelled" (`run.py:2947-2950`). An exception from a background task fails that task group, which ends the solver; a `LimitExceededError` reaching the runner is recorded as the sample's limit (`run.py:3008-3015`).

`sample.tg` is one attribute of the sample, shared by every member. The swarm cannot redirect `background()` from outside: there is nothing per member, in the context, for it to replace.

### deepagent's background dispatch

- `deepagent(background=True | int)` resolves to a cap of running children (`src/inspect_ai/agent/_deepagent/deepagent.py:255-278`) and runs its inner `react()` inside a `BackgroundRegistry` held in a ContextVar and the registry's `owned_task_group()` (`deepagent.py:242-250`). The registry is created inside the deepagent call, so the swarm cannot reach it from outside either.
- `owned_task_group()` does nothing while the sample's task group is live, and otherwise (a deepagent started in a scorer) opens a task group of its own, cancels it when the deepagent exits, waits for the children, and unwraps a single-exception group so the caller sees what was raised (`agent_tool.py:141-181`, `_live_sample_task_group()` at `:184-191`). That path came with inspect_ai#5718.
- `_dispatch_background()` registers an `AgentFuture` and starts `_run_background()` in the registry's group if it has one, else with `background()` (`agent_tool.py:631-686`, the branch at `:681-684`).
- `_run_background()` runs the child in its own cancel scope and span, with the subagent's own `limits` applied with `catch_errors=True` (`:744-745`), so the child's own limits end it with a "Subagent ... stopped" result. Cancellation is recorded as `cancelled` and re-raised (`:767-772`). A bare `LimitExceededError`, `TerminateSampleError`, `ModelRefusalError` or `SentinelFailure` is recorded as `cancelled` and re-raised, because "any LimitExceededError that reaches here belongs to an outer (sample/parent) scope" (`:773-791`). Any other exception, an exception group included, is captured on the future as `errored` and swallowed (`:792-802`).
- So, with a live sample task group, children are abandoned when the deepagent returns and run on until the sample's task group is cancelled. inspect_ai's own design says so ("Lifetime semantics: abandon on parent exit", `design/deepagent-background.md`), and `TestAbandonOnExit` and `test_solver_child_outlives_parent_until_sample_teardown` pin it (`tests/agent/deepagent/test_deepagent_background.py:1930-1950`, `:2044`). `TestNoLiveSampleTaskGroup` pins the scorer path (`:1996-2316`).

### What a child inherits, measured

Two spikes, on mockllm, not committed:

- **Spike 1.** A solver opens `span("member")`, enters `token_limit(None)`, calls `background(child)` and leaves the limit; the child sleeps, then calls the model in `span("child")`. After the child finished, the exited limit's usage was 34 tokens, the child's call, and the call's span chain was `child → member → s → solvers`. A token node records into its ancestors (`src/inspect_ai/util/_limit.py:1094-1098`), and the leaf of each limit tree is a ContextVar (`:975-1005`, the trees at `:1016-1021`), so a child keeps metering and checking the nodes its caller had entered, even after the caller left them. This is what [swarm-limits.md The ledger](swarm-limits.md#the-ledger) relies on ("background children once they are supported, since they inherit the member's context").
- **Spike 2.** A solver runs `run(deepagent(background=True, subagents=[general()]), "go", limits=[token_limit(20_000)])` under a 100,000-token sample limit; the top loop dispatches two children and waits for them, and the children generate until the limit is reached. The sample ended with `limit: type='token' limit=20000.0`, a `SampleLimitEvent` for the scoped limit, and the solver's line after `run()` never printed. A scoped limit reached in a background child goes through the sample's task group to the runner and is recorded as the sample's.

### Limit errors inside tool calls

A limit error raised inside a tool call does not leave the tool call. The tool pipeline maps it to a `limit` tool error the model sees (`src/inspect_ai/model/_call_tools.py:210-216`, applied at `:351-356`), and a stage of parallel tool calls waits for all its calls (`:727-739`). So a child whose tool call reaches a swarm cap (a synchronous subagent inside the tool, or a nested model call) does not end there: it ends at its own next model call, which may be after a sibling tool call that runs for a long time. A parent waiting in `agent_wait(mode="all")` waits for every selected future in turn (`src/inspect_ai/agent/_deepagent/lifecycle_tools.py:307-310`), so it waits too. The tool pipeline also reduces a tool's exception group to one leaf with `inner_exception()` (`_call_tools.py:347-349`, `:738-739`, and in `_sentinel_exception()` at `:228-235`).

### Who calls `background()`

Surveyed in inspect_ai `main` and in the checkouts of inspect_swe (`963523e`), inspect_evals (`d2d3aac91`), inspect_scout (`78a5cbd4`), inspect_sentinel (`35fc75d`), inspect_flow (`851ed610`), inspect_petri (`6266a62`), inspect_harbor (`f3a622a`), inspect_steward (`e18064a`), petri_bloom (`97a0a21`), petri_dish (`d7c6f35`) and inspect_ranges (`b587630`):

- **inspect_ai source:** only deepagent (`agent_tool.py:684`). The sample's task group has two other users that do not go through `background()`: the working-limit monitor (`util/_limit.py:936-965`) and the hooks' event loop (`hooks/_hooks.py:778-805`).
- **inspect_ai tests:** `tests/util/test_background.py:49`, `tests/test_cancellation_logging.py:471`, `tests/util/sandbox/test_sandbox_service.py:106`.
- **inspect_ai docs:** "Background Work" and "Sandbox Service" in `docs/agent-custom.qmd:447-505`, both as solver-level, sample-lifetime workers.
- **Satellites:** inspect_ranges' goal poller (`src/inspect_ranges/_challenges/poller.py:92-94`), started from a setup solver (`_challenges/setup.py:212`). No other satellite calls it.
- **Epoch's branch:** only through `deepagent(background=64)`, inside their member wrapper.

So every real caller is either deepagent or a solver-level worker meant to live as long as the sample. Solver-level workers run where nothing sets the seam and keep today's behaviour. In a member, the only shape in use is deepagent's dispatch, whose children end on a bare limit or control-flow error, return, or are cancelled.

### The swarm today

- `member()` refuses a built `deepagent` whose params, or a `deepagent` factory whose `args`, set `background` to anything but `False` (`src/inspect_swarm/_member.py:281-282`, `:295-296`, `:299-304`), and its docstring says so (`:163`, `:178`).
- On the M1-4 branch, the member wrapper `run_member()` sets the member ContextVar, enters `tool_state_scope(name)` and the member's cancel scope, builds the agent, and calls `run(agent, input, limits=nodes, name=name)`, where `nodes` are the member's lever and limits (`_member.py:291-324`, at `ede24f8`). An error is split into cap, owned and foreign leaves by `classify_errors()` (`:338-356`): a cap stops the swarm, an owned leaf gives a status, a foreign one is raised. A cancelled member scope gives `stop_cause` or `cancelled`. The branch has since moved on: at `6de9419` the wrapper builds the agent, enters the nodes itself in an `ExitStack`, and calls a limit-less `run(agent, input, name=name)` inside it, so that a member message limit covers the member's conversation from its first message (`_member.py:304-327` at `6de9419`). The placement below is written against that shape.
- `SwarmNode.exceeded()` raises the node's `LimitExceededError` for a native member. For a bridged member it instead sets the stop cause, cancels the member's scope and, for a cap, calls `stop_swarm("swarm_cap")` at once, because a raise there would end the sample (`_budget.py:250-275`, at `ede24f8`).
- `SwarmRun.stop()` closes the lever of every running member and the runtime lever (`_controller/_control.py:255-268`); `_supervise()` waits for running members up to the effective grace, cancels the scopes of those still running, and waits again (`:323-347`); `_grace_now()` clips grace to half the sample's remaining time (`:349-354`). `_run_member()` calls `_end()` in its `finally`, which sets the state `ended` and the status together and queues `MemberEnded` (`:300-317`); an exception from the wrapper sets the status `errored` first. The caps are entered once around both task groups (`:237-247`). At `6de9419`, `events()` yields queued events and waits for the next while any member is running (`_controller/_control.py:226-241`), so a controller acts on a member only when its `MemberEnded` is queued, and `_run_member()` is at `:317-327`.
- `Lever.close(reason)` sets the lever's token limit to its usage, so the next pre-dispatch check raises, with the message "Stopped by the swarm: <reason>" (`_budget.py:418-422`, `:288-291`, at `ede24f8`).

## Design

### At a glance

```
sample task group (unchanged; background() lands here when the seam is unset)
└── swarm runtime: caps entered once
    └── member task
        tool_state_scope(name), member cancel scope
        └── member_background(contain=[*nodes, *caps])   ← inspect_swarm, one per member;
            │                                               sets inspect_ai's seam to itself
            ├── ExitStack: the member's nodes (lever, share, limits, time limit)
            │   └── run(agent, input)                       ← the member's agent (top loop)
            └── tasks background() starts anywhere in the member:
                deepagent children, custom workers
```

1. The member's agent returns (or ends on its own limits, the lever or a cap). The member is decided: its status, output and time are recorded, the provisional answer is updated, its state becomes `ended` and its `MemberEnded` is queued, so its controller can act at once.
2. Drain: close the member's lever, wait for the owner's tasks up to the effective grace, leave the owner (cancelling what is left).
3. The drain ends: the owner's counts are recorded and the runtime stops waiting for the member. The swarm finishes only once every drain has ended.

A swarm cap reached anywhere in the member, by its agent or by a child, inside a tool call or not, calls `stop("swarm_cap")` from the cap node as it is detected.

### The seam (AI-5, inspect_ai)

In `src/inspect_ai/util/_background.py`, private (not exported from `inspect_ai.util`, not documented):

```python
class BackgroundTarget(Protocol):
    def start_soon(
        self, func: Callable[[Unpack[PosArgsT]], Awaitable[Any]], *args: Unpack[PosArgsT]
    ) -> None: ...


# Where background() starts work, when set. Unset, it starts in the sample's task group.
background_target: ContextVar[BackgroundTarget | None] = ContextVar(
    "background_target", default=None
)
```

- **`background()`** builds its logging wrapper as today, then reads `background_target`. If it is set, it calls `target.start_soon(run)` and returns; the sample checks are skipped, since the target, not the sample, owns the work. If it is unset, it runs today's code unchanged: the same sample checks, the same `sample.tg.start_soon(run)`.
- **`BackgroundRegistry.owned_task_group()`** steps aside when `background_target` is set: it does nothing, as it does while the sample's task group is live. `_dispatch_background()` then finds no registry task group and starts `_run_background()` with `background()`, which starts it in the target. Nothing else in deepagent changes: `_run_background()`, its statuses and its error branches are as today.

That is the whole inspect_ai change: one ContextVar, one branch in `background()`, one condition in `owned_task_group()`. The target sets and resets the ContextVar itself. With it unset, every line runs as today. The docstrings of `background()` and `_dispatch_background()` mention the target in a sentence; the public docs do not change.

A deepagent nested anywhere in a member (a custom subagent that is itself a deepagent) also steps aside, so its children are the member's too, and are drained with the member rather than when the nested call returns.

### The owner (M2-5, inspect_swarm)

A new private module, `src/inspect_swarm/_background.py`:

```python
class MemberBackground:
    # the target of inspect_ai's background_target while the member runs
    started: int     # tasks background() has started in this owner
    running: int     # of those, not finished
    cancelled: int   # still running when the owner ended and cancelled by it; 0 until then

    def start_soon(self, func, *args) -> None: ...
    async def wait(self) -> None: ...   # returns once none is running, including tasks started while waiting


@asynccontextmanager
async def member_background(contain: Sequence[Limit]) -> AsyncIterator[MemberBackground]: ...
```

**Entering and leaving.** `member_background()` opens an anyio task group, sets `background_target` to the owner, and yields it. On the way out it resets the ContextVar, cancels the task group's scope (recording `running` as `cancelled`), and waits for the tasks, so their `finally` blocks and cancellation handlers run before the block is left. It then raises as `owned_task_group()` does: a single exception bare, several as an `ExceptionGroup`, and a cancellation from outside (the hard stop) re-raised as that cancellation.

**Starting a task.** `start_soon()` counts the task in `started` and `running` and starts it in the task group, wrapped so that `running` drops when it ends. After the owner has ended, `start_soon()` raises `RuntimeError` ("the member's background work has ended"), so a context saved inside the member and used later cannot start work nothing owns. A task started while the owner is cancelling starts cancelled, as anyio does.

**How a task ends.** Only the expected cases are specified:

1. It returns, or is cancelled (by the owner, the member's scope, or deepagent's `agent_cancel`). Neither is an error.
2. It raises a bare `LimitExceededError` whose `source` is one of `contain`. The task ends alone; its siblings and the member's agent continue, and the error goes no further. The member passes its own nodes (lever, share, member limits) and the swarm's caps, so a child stopped by any of them ends alone. `classify_errors()` uses the same two sets. From M2-2, a bare `LimitExceededError` with `type="custom"` and no `source`, which an agent raises to stop itself (inspect_boltons' loop limits), is handled the same way: the child that raised it ends alone, and the member's agent and its other children continue. That is what the same error from a synchronous subagent does: `apply_limits()` re-raises a sourceless error (`src/inspect_ai/util/_limit.py:181-184`), and the tool pipeline turns it into a `limit` tool error the parent sees and carries on from (`_call_tools.py:210-216`). M2-2's rule, that such an error ends the member `member_limit:custom`, applies when it leaves the member's own agent, which is the agent saying it is stuck. Raised in a child during the drain, it ends that child and changes nothing else; the member's decided status stays. Before M2-2 merges, a sourceless custom error is foreign, as [swarm-limits.md Stopping](swarm-limits.md#stopping) says.
3. It raises anything else: a limit from outside `contain` (the sample's, or a sourceless one that is not `custom`), `TerminateSampleError`, `ModelRefusalError` under `fail_on_refusal`, `SentinelFailure`, a custom worker's crash, or any exception group. The task group cancels the member's agent and the other tasks, and the error is raised from the block, as such an error from a task in the sample's task group ends the sample today. The member wrapper treats it as foreign.

deepagent re-raises the expected control-flow errors bare (`agent_tool.py:773-791`), so cases 2 and 3 meet them in the shape they are raised in. A grouped error that holds a contained limit, which no supported member produces, falls under 3, and the member is `errored` ([Not this design](#not-this-design)).

Why rule 2: the swarm stops members by limits (the lever at a soft stop and at the drain, a reached cap, a member limit). If the first child refused by the lever cancelled its siblings, every stop and every member end would turn the member's other in-flight calls into unrecorded spend ([Why](#why)). The limits outside `contain` that a supported member's child can reach are the sample's, which bound more than the member, so they propagate as today. A limit the member's agent enters itself around part of its work (a custom agent wrapping a nested deepagent in its own `token_limit`) is also outside `contain` and is treated as foreign, although it bounds less than the member; no supported member does this ([Not this design](#not-this-design)).

The code inside the block reaches the shared nodes too: token, cost and turn nodes count every call in the member, so the member's agent meets a reached member limit or cap at its next check, which is how a synchronous subagent's limit error reaches its parent through the tool result. A message limit is different: it checks the conversation of the loop that calls (`check_message_limit(count, ...)`, `src/inspect_ai/util/_limit.py:744-758`, `:1328-1375`), with no shared counter, so a child whose longer conversation reaches the member's message limit ends alone and the member's agent, with a shorter conversation, can go on. That is the same as for a synchronous subagent today.

**No deadline on exit.** A task's cancellation cleanup is bounded by the task's own code, as in any anyio task group; a deadline would leave work running outside any structure, which is what the owner exists to prevent. The drain bounds how long tasks keep working with `wait()` under `anyio.move_on_after()`, before leaving the block. A task that shields its cleanup from cancellation holds the block until that cleanup ends, as a bridged agent's shielded proxy shutdown holds the swarm's hard stop today ([swarm-limits.md Stopping](swarm-limits.md#stopping)).

### Cap notification at the cap node

When a swarm cap is reached, the swarm must start its stop at once, whoever reached it. Waiting for the error to leave a task is not enough with background children: a cap reached inside a child's tool call becomes a tool result, and the child ends only at its next model call, after the rest of its tool stage ([Limit errors inside tool calls](#limit-errors-inside-tool-calls)). Meanwhile the member's agent may be waiting in `agent_wait(mode="all")` on that child, so nothing escapes and no stop begins. With 64 children per member, this is an expected case.

So `SwarmNode.exceeded()` notifies the stop from the node, for native members as it already does for bridged ones. Before it raises, if the node's role is `cap` and a member context is current, it calls `member.stop_swarm("swarm_cap")`:

```python
# src/inspect_swarm/_budget.py, SwarmNode.exceeded(), native branch (M2-5)
if self.role == "cap" and member is not None:
    member.stop_swarm("swarm_cap")   # a no-op once a stop has begun
raise LimitExceededError(...)
```

`stop()` is synchronous up to its grace and its first call wins (`_control.py:255-268` at `ede24f8`), so calling it inside a model call's limit check is safe and costs nothing after the first time. It runs in the task that detected the cap, before any tool pipeline sees the error. The soft stop then closes every running member's lever, and grace starts. The child whose tool call reached the cap finishes its tool stage within grace, and so do its siblings; each is refused its next call by the closed lever or the cap and ends alone (rule 2). `agent_wait` returns, and the member's agent reaches the cap or the lever at its next call. The hard stop bounds a tool call longer than grace.

The member wrapper's own `stop("swarm_cap")` for a cap leaf from `classify_errors()` stays, as a no-op by then. The controller's caps are unchanged: the controller runs no background work, and its wrapper stops the swarm when a cap error leaves it.

### Entering the owner

The member wrapper enters the owner inside the member's tool-state scope and cancel scope, and enters the member's nodes inside the owner:

```python
# src/inspect_swarm/_member.py, run_member(), M2-5 (on M1-4's shape at 6de9419)
token = current_member.set(context)
try:
    with tool_state_scope(name), context.scope:
        try:
            async with member_background(contain=[*nodes, *caps]) as owner:
                status, state = None, None
                try:
                    agent = build_member(record)
                    if owner.started:                 # construction started background work
                        raise RuntimeError(...)       # refused; the owner cancels it
                    with ExitStack() as entered:      # the member's nodes, inside the owner
                        for node in nodes:
                            entered.enter_context(node)
                        state = await run(agent, input, name=name)
                    status = "submitted" (or stop_cause / cancelled, as M1-4)
                except Exception as ex:
                    cap, owned, foreign = classify_errors(ex, caps, nodes)
                    if foreign is not None:
                        raise foreign                 # no drain: the owner cancels the children
                    status = ...                      # cap: stop("swarm_cap"); owned: its status
                decide(status, state)                 # the runtime publishes the result and MemberEnded
                await drain(owner, lever, grace())
        finally:
            counts.started, counts.cancelled = owner.started, owner.cancelled
    ...                                               # errors from the owner's exit, below
finally:
    current_member.reset(token)
```

- **Placement.** Inside `context.scope`, so the hard stop cancels the children with the member. Inside `tool_state_scope(name)` and the member ContextVar, so children keep the member's tool state and identity (they would anyway, by inheriting the context at dispatch). Around the node stack, with the `try` that classifies the agent's errors inside the owner, so the member's agent and every task it starts run under the owner, and the owner's task group is outside the cancel scope of the member's time limit. Sample limits are not in `contain`, so a child that reaches one ends the sample at once, as it would outside a swarm.
- **One owner per member, every member.** A member with no background work pays a ContextVar set and an empty task group. Bridged members never call `background()`.
- **Background work is started by the agent, not while it is built.** A factory-form member's agent is built inside the block, but before the member's nodes are entered and before `run()` opens its span (`src/inspect_ai/agent/_run.py:97-101`). A task started then would keep the context of its dispatch: the caps but not the member's lever, share or limits, and no member span, so the drain could not stop its next call and the member's call counts would miss it (a probe on asyncio and trio metered such a task's tokens to the cap and none to the member node, even when the task called the model after the node was entered). No factory does this: deepagent, `react()` and Epoch's builder start background work only from the running agent ([Who calls `background()`](#who-calls-background)). So the wrapper refuses it: if `owner.started` is not 0 after `build_member()`, it raises `RuntimeError` naming the member and saying that background work must be started by the agent when it runs, not by its factory. The owner's exit cancels the task; the error is foreign, so the member is `errored` and the error propagates, as for any factory error (M1-4).

### Wiring the decision and the counts

The drain runs inside `run_member()`, but the decision must reach the runtime before it, so `run_member()` gains two parameters:

- **`decide: Callable[[str, AgentState | None], None]`**, called synchronously, at most once, with the status and the state `run()` returned (`None` when the agent did not return one). The runtime binds it to `SwarmRun._decide(m, status, state)`, which records the status, the output from `state` as M1-5's record specifies, and the swarm time in `MemberRun` and its `MemberResult`; freezes `MemberRun.calls_to_decision` ([Steps to the answer](#budget-member-limits-and-the-ledger)); updates the provisional answer; sets `state = "ended"` and `draining = True`; and queues `MemberEnded` (after M2, once the verdict is taken). It is M1-4's `_end()` minus the liveness.
- **`counts: BackgroundCounts`**, a small mutable record on `MemberRun` (`started`, `cancelled`) that the wrapper fills in its `finally`.

`_run_member()` keeps its shape, with its fallback made to respect a published decision. It sets `status = "errored"` on an exception and calls `_end()` in its `finally`; M2-5's `_end()` becomes the drain's end: if the member was not decided (the agent was still running, or the error came from the factory), it decides it first with that status; it then sets `draining = False`, copies the counts for the ledger and notifies, so `events()` and `_supervise()` see the member leave. A member decided before the error keeps its status. When the hard stop cancels the member's scope before the decision (its agent still running), the wrapper decides `stop_cause` or `cancelled` after the scope, as M1-4 returns them today; after the decision, the scope's cancellation only ends the drain.

### The drain at member end

When the member's agent has returned (normally, or on the lever, its share, a member limit or a cap), the wrapper drains the owner:

1. Close the member's lever with the reason `member_ended`: every child's next model call is refused, with "Stopped by the swarm: member_ended". If a stop already closed it, the lever keeps that reason. Only children can still make calls in the member's context: its own loop has returned.
2. Wait for the owner's tasks with `anyio.move_on_after(grace())`, where `grace()` is `SwarmRun._grace_now()`: `Budget.grace`, clipped to half the sample's remaining time. Children finish the calls and tool calls they have in flight, then reach the lever at their next check and end alone (rule 2).
3. Leave the block. The owner cancels the children still running, waits for them, and records the count in `owner.cancelled`.

Why a soft drain rather than cancelling at once: calls in flight finish and are recorded, so the member's spend stays known ([Why](#why)). A throwaway prototype of the owner (patched in for `background()` around today's deepagent, not committed) showed it: a top loop dispatched two children that each started a 0.5 s tool call, then submitted; after the lever closed, both children finished their tool calls, were refused their next call and ended alone, with `cancelled=0`, two lever errors held by the owner, and no errored model event. With a 0.1 s grace the owner cancelled both. With the lever outside the contained set, the first child's error ended the block and was recorded as the sample's limit, as rule 3 says.

Why stop the children at member end at all: the swarm owns and drains all of a member's work ([swarm.md Goals](swarm.md#goals)), and the plan asks for the drain on member end ([D-2](swarm-implementation-plan.md#designs)). A member's children work on the member's attempt; once it has submitted or been stopped, their further spend serves nothing the swarm records. Epoch's members leave theirs running until the solver ends, which the Lean analysis lists as a cost they pay today ([Observations for Epoch](swarm-lean-open-problems.md#not-this-design)). The port keeps background deepagents with up to 64 children and drops that waste.

`Budget.grace` is reused rather than adding a separate drain setting: it already means "how long work in flight gets to finish once it may not start more", and the drain is that rule applied to one member.

### At the swarm's stop

Nothing new is needed; the drain composes with [Stopping](swarm-limits.md#stopping):

- **Soft stop.** `stop()` closes the levers of running members. A draining member's lever is closed already, and its children are already being refused. In a member whose agent is still running, the agent and its children reach the closed lever at their next calls; the children end alone, the agent returns `stopped`, and the drain then waits for children with calls in flight.
- **Grace.** `_supervise()` waits for running and draining members, so grace covers the children. A member's drain wait and the swarm's grace run side by side: a drain that began before the stop may end first, and one that has not ended when the swarm's grace does is cut short by the hard stop.
- **Hard stop.** `_supervise()` cancels the scope of every member still running or draining. The owner is inside that scope, so its children are cancelled with the member's agent, or with its drain. A member whose status was already decided keeps it (below); its ledger line says `cancelled`.
- **Time cap.** It calls `stop("swarm_time")`, so it is the soft stop above.
- **A cap reached inside a child.** The cap node calls `stop("swarm_cap")` as it detects the cap ([Cap notification at the cap node](#cap-notification-at-the-cap-node)), whatever the member's agent is doing: running, blocked in `agent_wait` on a sibling in a long tool call, or draining. The cap is in `contain`, so the child that raised ends alone and its siblings' calls in flight finish. Without containment, the first child to call the model after the cap was reached (cap checks run root to leaf, ahead of the lever) would cancel every sibling's call in flight at the moment the whole swarm is being soft-stopped.

### Statuses, ended, and the record

**A member is decided when its agent returns, and drained after.** At the decision the runtime records the member's status, output and time in its `MemberResult`, updates the provisional answer, sets `MemberHandle.state` to `ended` and queues `MemberEnded`, exactly as M1 does when a member ends. The status comes from how the agent ended, exactly as in M1: `submitted`, `unsubmitted`, `stopped`, `share`, `member_limit:<type>`, `swarm_cap`, or from a cancelled scope `cancelled` (and `errored` for a foreign error). Then the drain runs. Reasons for deciding first:

- A controller acts on `MemberEnded`. Epoch's `until_proved` returns on the first `MemberEnded` whose status is `submitted` ([swarm-lean-open-problems.md](swarm-lean-open-problems.md#could-inspect_swarm-express-their-experiment)), and the later `stop_on_verified`, `coordinator` and `escalate` act on a `MemberEnded` too ([swarm-api.md Controllers](swarm-api.md#controllers)). If the event waited for the drain, the swarm would stop the other members up to one effective grace (normally 120 seconds) after the proof arrived, and they would spend all that time. Every controller acts on the same event it does today, at the same moment: when the member's agent ends.
- `first` is "the submission with the earliest swarm time" ([swarm-scoring.md](swarm-scoring.md#the-final-answer-first)). A member that submits at t=10 and drains until t=130 submitted before one that submits and ends at t=20. `MemberResult.time` is therefore the time the member's agent returned.
- The provisional answer exists so that an outer limit ending the sample early leaves the first submission as the output ([swarm-scoring.md](swarm-scoring.md#the-final-answer-first)). A sample limit during a drain must find the submission already recorded.

**The runtime waits for every drain.** A member whose drain is running is `ended` to its controller, but the runtime still counts it as live (`MemberRun.draining`):

- `events()` ends, and `leaderless` returns `all_done`, only when no member is running or draining and the queue is empty. They must not end while children still spend: the swarm would finalise and write its ledger with work still running.
- The stop procedure's grace waits for draining members as for running ones, and the hard stop cancels their scopes. That is how grace and the hard stop reach the children.
- `stop()` closes the levers of running members; a draining member's lever is closed already.

So `ended` means what it means in M1 to a controller: the member's agent has ended and its result is final; the member cannot be started again. Its background work may still be draining, and the swarm finishes only after it has. The closed status set and `MemberState` do not change.

**Verdicts (after M2).** When the task supplies a verifier, the runtime takes the verdict before it queues `MemberEnded` ([swarm-scoring.md When verdicts are taken](swarm-scoring.md#the-final-answer-chain)). The submission is complete at the decision, so verification starts then, alongside the drain, and does not wait for it: the verdict and its `MemberEnded` are actionable as soon as the verifier returns, whether or not the drain has ended. A member awaiting its verdict also counts as live for `events()`, as swarm-api.md already says.

**What the drain can still change.** Nothing about the status. A decided status is final: the record, the handle and the provisional answer published when the agent returned stay as they are whatever the drain meets.

| What happens during the drain | Effect |
|---|---|
| Children reach the closed lever, the member's share or a member limit | Expected: that is how they stop. Ignored. |
| A child reaches a swarm cap | The cap node calls `stop("swarm_cap")` if no stop has begun; status unchanged |
| The swarm's hard stop cancels the member scope | Status unchanged; ledger `cancelled` |
| A child raises a foreign error (a sample limit, `TerminateSampleError`, a refusal under `fail_on_refusal`, a sentinel failure, a custom worker's crash) | Status unchanged; the owner cancels the other children, and the error is raised without the swarm's leaves, so the swarm ends as for any foreign error. The ledger says `interrupted` |

Why a foreign error in the drain keeps the submission: the submission was complete before the error, and the swarm already treats an outer limit that ends the sample early this way. The provisional answer is on the `AgentState` the swarm was passed, which `as_solver()` copies to the sample's output even when an exception ends the agent (`src/inspect_ai/agent/_as_solver.py:65-80`), and the record says `provisional: true` ([swarm-scoring.md](swarm-scoring.md#the-final-answer-first)). Turning the member `errored` instead would leave a provisional `FinalRecord.member` and output naming a member whose record no longer says `submitted`, or would need the runtime to re-select before the error propagates, with a different answer only for members that had background children. So `MemberHandle.status`, `MemberResult.status` and `submitted`, `FinalRecord.member` and the passed `AgentState.output` all keep what the agent's return set, and the error then propagates as M1-7 specifies for foreign errors, with the ledger and the record written in the runtime's synchronous `finally`. A sample limit is then the sample's limit and its output the provisional answer; any other foreign error fails the sample, as the same error does from a member's agent. Only a member whose status was not yet decided (its agent still running) becomes `errored`.

Before the drain (while the agent runs) the same events are handled as M1-4 handles errors from the agent: a member-node limit reached by a child ends the child, and for the lever, the share and token, cost or turn limits the agent reaches the same node at its next check (a member message limit, per conversation, ends only the child); a cap reached by a child stops the swarm from the cap node and ends the child; a foreign error from a child cancels the agent and the other children and makes the member `errored`.

**Errors from children that do not reach the owner.** deepagent captures a child's ordinary exception on its future (`errored`) and the parent reads it with `agent_status` or `agent_wait`. It does not affect the member's status; the member's agent decides what to do with it, as it decides about a failed tool call.

**Stop reasons** are unchanged. A cap reached in a child is `swarm_cap`; nothing a child does is a new reason.

### Budget, member limits and the ledger

Children run in the member's context, so every node they reach is reached by their calls as by the member's own (Spike 1):

- **Swarm caps and the member's share** meter children's calls. Overshoot stays bounded by connection slots, which children hold like any caller, plus one native compaction per agent loop, which [swarm-limits.md](swarm-limits.md#the-final-step-and-the-reserve) already counts per member or subagent. With 64 children a member can hold many slots; the bound counts slots, not members.
- **Member limits** (token, cost, turn, message) meter children as they meter a member's synchronous subagents. Token, cost and turn limits are member-wide: children's calls count towards them, and once one is reached the member's agent ends on it at its next call. A member message limit bounds each loop's own conversation, so a child that reaches it ends alone and the member can still go on and submit, as with a synchronous subagent; the swarm adds no member-wide message count. A member's time limit is an Inspect `time_limit` among the member's nodes, entered in the node stack inside the owner. Its cancel scope covers the member's agent and not the owner's task group, so it cancels the agent only, and the drain then stops the children within the effective grace. (Were the owner inside the node stack, the time limit would cancel the children at once with the agent; the round-6 review's probe showed both placements on asyncio and trio.) `member()` documents it: the member's time limit bounds its agent; after the agent returns, its background children receive the effective grace period before cancellation, and cancellation cleanup is awaited.
- **The lever** records every child's usage, so the member's ledger line includes its children's spend. A child's span is a descendant of the member's span (Spike 1), so the member's call counts include its children's model events.

`MemberLedger` ([swarm-limits.md The ledger](swarm-limits.md#the-ledger)) gains three fields:

```python
class MemberLedger(BaseModel):
    ...
    background_started: int     # tasks background() started in the member's owner
    background_cancelled: int   # of those, still running when the drain gave up, cancelled by the owner
    calls_to_decision: int      # completed calls and cache replays in the member's spans, finalized by its decision
```

**The owner's counts.** They count every task `background()` started anywhere in the member, nested deepagents' children included, since deepagent steps aside under the owner. The wrapper writes them to the member's `BackgroundCounts` ([Wiring](#wiring-the-decision-and-the-counts)) in a `finally` around the owner's block, so they are recorded on every path, a foreign error and the hard stop included. `total_complete` additionally requires `background_cancelled == 0` for every member, for the same reason it requires no member `cancelled`: a cancelled agent loop may have had work in flight that left no trace. Calls a cancelled child had in flight are counted as `errored`, as anywhere. A child the model cancelled with `agent_cancel` is its own decision; its in-flight call, if any, is an errored event and already clears `total_complete`. Evidence of which children ran is their spans; the swarm adds no event of its own.

**Steps to the answer.** M1-11's `steps_to_answer` is the selected member's steps up to its submission, read today as its whole `calls.calls + calls.cache_replays`, because under `first` "the selected member ends at its submission" ([swarm-implementation-plan.md M1-11](swarm-implementation-plan.md#m1-pull-requests)). With background children that is no longer so: a child's call can complete during the drain, after the member submitted, and it rightly counts in the ledger's usage and calls. So the ledger also counts the calls that had completed by the decision, from a snapshot taken then. `_decide()` runs the ledger's own counting, synchronously, over the transcript's events at that moment: `calls.calls + calls.cache_replays` with the same span rule, over the `ModelEvent`s already finalized (not `pending`). It stores the integer in `MemberRun.calls_to_decision` and keeps no reference to the events. A call still in flight at the decision is pending then, so it is not in the snapshot; it counts in the full ledger when it completes. The snapshot does not use `ModelEvent.completed`: Inspect finalizes a call's event, with its output, and runs the post-call token, cost and turn checks before `generate()` sets `completed` (`src/inspect_ai/model/_model.py:1697-1704`, `:1742-1748`, `:1037-1057`). A call stopped by such a check, such as a deepagent child's own limit, which `apply_limits(..., catch_errors=True)` turns into a completed future its parent can collect before it submits (`agent/_deepagent/agent_tool.py:744-760`), is therefore finalized with usage but never gets `completed`. The snapshot counts it, as the ledger does. Being synchronous, the scan runs with no `await` between the agent's return and the count, so no child call can finalize in between; it costs one pass over the transcript per member. `steps_to_answer` reads `calls_to_decision`; `longest_member_steps` keeps the full count, and every call of the drain stays in the ledger's usage and counts. For a member with no background work the two counts are equal.

### Lifting the refusal

M2-5 deletes `_check_background()` and its two calls. `deepagent(background=True)`, `deepagent(background=64)` and `member(deepagent, args={"background": 64})` are accepted, and so is any agent, wrapped or custom, that calls `background()` while it runs: the owner catches them all, so nothing depends on reading registry params. A factory that calls `background()` while building the agent is refused when the member starts ([Entering the owner](#entering-the-owner)). `member()`'s docstring replaces the refusal sentence with the contract:

> Background work belongs to the member. Work its agent starts with `background()`, such as `deepagent(background=...)` subagents, spends through the member's limits and share, and is the member's in the ledger. When the member's agent returns, its status is decided; the swarm then stops the background work from starting model calls, gives it the swarm's grace period to finish calls in flight, and cancels what is left. The member ends after that. Background work must be started by the agent while it runs; a factory that starts it while building the agent makes the member `errored`. Work started outside `background()` (an `asyncio` task, a process left running in the sandbox) is not stopped.

### The shared-memory channel

A background `general()` child reaches the board exactly as a synchronous one does ([swarm-memory-channel.md Descendants](swarm-memory-channel.md#descendants)):

- It inherits the member ContextVar at dispatch, so it is the member: its writes are records whose `sender` is the member.
- deepagent hands `general()` the parent's tools, including `swarm_tools()`. The child resolves it in its own agent channel, which is not the member's bound loop (the member's loop resolved first, before it could dispatch), so `Channel.reach = "member"` gives it the `shared_memory` tool and not the bound loop's notices or `"loop"`-reach tools.
- Its writes carry its own agent span as `sender_span_id` and `nested: true`; its views are `read` events in its own tool spans.
- It shares the member's `rate`: one member's 64 children cannot multiply its write volume.
- **While the member's agent runs**, a child's views and writes are the member's, as above. Once a swarm stop begins, writes are refused (`bus.close()` at step 0); views still work while the member is running ([swarm-memory-channel.md](swarm-memory-channel.md#concurrency-and-compare-and-swap)).
- **During the drain** the member is `ended`, so the `shared_memory` tool refuses a child's command ("`worker-1` has ended"), as swarm-memory-channel.md already specifies for a background child that outlives its member, and a write already past the policy is rejected by the bus's sender-liveness check at step 2. The child sees a tool error and reaches the closed lever at its next call. Its member's attempt is over, so nothing it would write could reach the member's answer, and other members never see a write from an ended member.

The test that a background `general()` child reads and writes the board as the member lands with whichever of M2-5 and M2-8 merges second ([Testing](#testing)).

### What depends on how M1-4 lands

M1-4 is on its branch; the citations above are at `ede24f8`, and the wrapper's newer shape at `6de9419`. This design depends on:

- the wrapper's structure: member ContextVar, then `tool_state_scope(name)` and `context.scope`, then the member's nodes entered around `run(agent, input)` with the lever among them, and `classify_errors()` (the owner goes between the scopes and the node stack, with the classifying `try` inside it);
- `SwarmNode.exceeded()` with `MemberContext.stop_swarm`, where the native branch gains the cap notification;
- `stop()` closing the levers of running members, `_supervise()` waiting for and cancelling running members (extended to draining ones), `events()` waiting while any member is running (likewise), and `_grace_now()`;
- `_end()` setting the state and the status together, and `_run_member()`'s `errored` fallback. M2-5 splits `_end()` into `_decide()` at the agent's return and the drain's end ([Wiring](#wiring-the-decision-and-the-counts)).

If M1-4 merges with a different shape, M2-5 keeps the same placements: the owner inside the member's cancel scope and tool-state scope and outside the member's nodes (so the member's time limit cancels only the agent), the member's nodes and the caps as its contained set, the decision published before the drain, and the cap notification at the node that detects the cap. M1-5's record, which M2-5 also changes, is not built yet ([Changes to other designs and the plan](#changes-to-other-designs-and-the-plan)).

## Alternatives considered

**A public owner in inspect_ai** (this design's first five rounds): `inspect_ai.util.background_owner(contain=, on_limit=)` with the drain, containment and counts in inspect_ai. It would let other callers own background work. But no caller outside the swarm needs it, and every rule it holds (which limits are contained, how long the drain waits, what is counted) is the swarm's, so it is better as swarm code that inspect_swarm owns and tests. Ransom chose the private seam and an owner in the swarm (decision: Ransom, 2026-10-16: "owner in the swarm - yes").

**No inspect_ai change: redirect from the swarm.** The swarm would swap `sample.tg` or wrap deepagent's registry. Neither is possible per member: `sample.tg` is one attribute shared by every member, and the registry is created inside each deepagent call ([Current behaviour](#current-behaviour)). The seam is the smallest change that gives the swarm a per-member, per-context hook.

**An owner with plain task-group semantics**, where any exception from a task, a limit error included, cancels the block and its other tasks. Simplest, and it is what `owned_task_group()` does. But the swarm stops members by limits: the lever at a soft stop and at the drain, a reached cap, a member limit. Under plain semantics the first child refused by the lever cancels every sibling's call in flight, so every stop and every member end would turn a member's in-flight children into unknown spend. With 32 members and up to 64 children each, that is most of the calls in flight at the cap. The prototype's case with the lever outside the contained set showed it: the first child refused ended the whole block.

**Contain every `LimitExceededError` in the owner**, from the sample's limits too. Simpler classification. But a sample limit reached in a child would then stop only that child, and the sample would end only when the member's agent reached the limit or the member ended. For shared token, cost and turn nodes that is the next call; for a sample message limit, which a child's own conversation can reach without its parent's, it is the end of the member.

**Containment in deepagent instead of the owner.** `_run_background()` could end the child alone for some limit errors. It does not know which limits are the member's and which the sample's, and custom agents that call `background()` would not get it. The owner is where the boundary is, and it is swarm code.

**Notify caps from the owner**, with a callback when a child ends on a cap (rounds 1 to 5). It misses the expected case: a cap reached inside a child's tool call becomes a tool result and the child does not end, so no callback fires while the parent waits in `agent_wait` ([Limit errors inside tool calls](#limit-errors-inside-tool-calls)). The cap node is the first point that sees the cap, and it already notifies for bridged members.

**Cancel children at member end, with no grace.** Simpler drain, a member ends at once. It loses every call in flight at each member's end and makes `total_complete` false for nearly every background member.

**Let children run until the swarm stops**, and drain only at the swarm's stop. This keeps Epoch's current behaviour exactly. But it keeps spending on attempts that have already submitted or been stopped, makes `all_done` wait on children doing nothing the swarm records, for as long as they choose to run, and leaves a member's spend open until the very end. The plan asks for the drain at member end.

**End the member only when its drain ends** (this design's first six rounds): `state` stays `running` and `MemberEnded` is queued after the drain. It keeps "ended" meaning "no more spend", but it delays every controller's reaction to a member's result by up to one effective grace: Epoch's `until_proved` would stop the other members up to 120 seconds after the proof, and they would spend that whole time.

**A separate decision event** (`MemberDecided`) before a later `MemberEnded`. It keeps both moments visible, but every controller that acts on a result (`until_proved`, `stop_on_verified`, `coordinator`, `escalate`) would have to switch events, and a second event per member adds to `events()`'s contract. Publishing `MemberEnded` at the decision keeps every controller as written; the drain is the runtime's business, and the ledger records it.

**Wait for children to finish without closing the lever**, up to a deadline. Children would keep starting new calls on an attempt that is over, so the drain's length would be the deadline every time.

**An owner that drains with a deadline on exit.** It would leave tasks running after the block, outside any structure. The deadline belongs to the drain, with `wait()` and `move_on_after()`.

**Owning `asyncio` tasks and `run_in_background()` too.** No member uses them, and `run_in_background()` serves eval-level machinery that must outlive any sample.

## Compatibility and migration

**inspect_ai, with the seam unset: no change.** `background()` reads one ContextVar and, finding none, runs today's code: same checks, same logging, same task group, same errors. `owned_task_group()` adds one condition that is false. Every caller in [Who calls `background()`](#who-calls-background) runs with it unset: solver-level workers in the docs and tests, inspect_ranges' poller, and deepagent in solvers and scorers. So `TestAbandonOnExit`, `test_solver_child_outlives_parent_until_sample_teardown` and `TestNoLiveSampleTaskGroup` pass unchanged. The working-limit monitor and the hooks loop do not use `background()`.

**inspect_ai API.** None public. `background_target` and `BackgroundTarget` are private names in a private module; no log, schema, event or viewer type changes; nothing in ts-mono.

**inspect_swarm.** No released API until M2-12. Accepted members widen (background deepagents, agents that call `background()`). `MemberLedger` gains three fields under its existing `version: 1`, and `MemberResult.time` is the time the member's agent returned; neither has shipped. `MemberHandle.state`, `status` and `MemberEnded` keep their M1 meaning and timing (the member's agent has ended); `events()` additionally waits for drains before it ends. `steps_to_answer` reads `calls_to_decision`, which equals the full count for a member with no background work. A native member's cap now starts the stop when the cap node detects it, rather than when the error leaves the member's agent; the reason and the members' statuses are the same. `pyproject.toml`'s `inspect-ai` floor rises to the first release containing AI-5, since `src/inspect_swarm/_background.py` imports `background_target`. That import is private, as the swarm's imports of inspect_ai's limit classes are (`_budget.py` subclasses `_TokenLimit` and the others); the owner's tests, run against each inspect_ai release the floor allows, catch a change to it.

**Epoch's port.** Their builder keeps `background=64`; their member wrapper needs no change, because the owner does not depend on recognising deepagent. Their pinned inspect_ai (0.3.273) must move to a release with AI-1, AI-2 and AI-5, as the plan already says (M2-12).

## Security

What reaches the new code, and what it changes about who can outlive a member:

- **Model output decides how much background work there is.** A member's model chooses when to dispatch children, up to deepagent's `max_background`, and what they do. The owner bounds their lifetime to the member's and the lever and caps bound their spend. The effective grace limits how long children may continue normal work before cancellation. The hard stop and the sample time limit request cancellation; shielded cleanup can delay exit, which waits for every owned task. Before this design, the only bound on a child's lifetime was the sample's.
- **A model cannot make work outlive its member through `background()`.** The target is a ContextVar in the member's task tree. No tool or model output names a target, and a child cannot reach another member's owner, since member tasks are siblings started from the runtime's saved context.
- **What escapes the owner.** Raw `asyncio` tasks started by agent or tool code, and processes a model leaves running in the sandbox (`nohup`, `&`), are not owned. Agent and tool code is the eval author's, trusted as in any Inspect eval; a sandbox process can keep changing a shared or per-member sandbox after its member ended, as [swarm-lean-open-problems.md](swarm-lean-open-problems.md#8-after-m2-per-member-sandboxes-in-the-scoring-contract) notes for scoring. `member()` documents both.
- **Exception text** from children, which can contain model-influenced strings, is logged as today (`background()`'s wrapper, deepagent's warning) and, for foreign errors, raised as today. The owner adds no text of its own beyond the two `RuntimeError` messages, which contain no input.
- **The cap notification** runs `stop()` inside a model call's limit check. `stop()` is synchronous and reads only the swarm's own state; it takes no input from the call.
- **The shared-memory channel** gains no new writer: background children were already designed in as the member's descendants ([swarm-memory-channel.md Security](swarm-memory-channel.md#security)), and the existing liveness rules refuse a draining child's commands once its member has ended; the owner guarantees that no child outlives the drain.

## Testing

### inspect_ai (AI-5)

Async tests on the suite's anyio backends, mockllm where a model is needed, no network or Docker. A test target is a small class over an anyio task group, defined in the test.

`tests/util/test_background.py`:

- Unset: the existing tests pass unchanged, and `background()` outside a sample still raises.
- Set: `background()` calls the target's `start_soon` once, without an active sample; the started function runs with the caller's context; an ordinary exception from it is logged as "Background worker error" and re-raised, and a `LimitExceededError` is re-raised without logging, as today. Resetting the ContextVar restores today's path.

`tests/agent/deepagent/test_deepagent_background.py`:

- Set, in a solver: a deepagent that submits while a blocking child runs returns with the child still running in the target's task group, not the sample's; cancelling the target's group cancels the child, and the child's future says `cancelled`.
- Set, with no live sample task group (a scorer): `owned_task_group()` opens no task group, and children go to the target.
- A nested deepagent under a set target sends its children to the same target.
- `TestAbandonOnExit`, `test_solver_child_outlives_parent_until_sample_teardown` and `TestNoLiveSampleTaskGroup` pass unchanged.

### inspect_swarm (M2-5)

Runtime tests on mockllm with scripted outputs and controlled tool latency (anyio events and short sleeps, grace in tenths of a second), on asyncio and trio, no Docker.

`tests/test_background.py`, the owner on its own, without a swarm (inside an eval only where `background()` needs one):

- A task started through `background()` runs in the owner; leaving the block cancels a running task, runs its `finally` before the block is left, and sets `cancelled`; `started` and `running` count; `wait()` returns when the tasks finish, including one started while waiting, and at once with none.
- Limits (table-driven): a task that raises a bare limit error from a node in `contain` ends alone, a sibling keeps running, and the block's code continues; a limit from a node outside `contain` (a sample limit) cancels the sibling and the block's code and is raised; a `LimitExceededError` with `source=None` and a type other than `custom` is raised; one with `type="custom"` and `source=None` ends its task alone once M2-2 is in, and is raised before it (the case lands with whichever of M2-2 and M2-5 merges second).
- Errors: a task's `RuntimeError` cancels the block's code and a sibling and is raised bare; two task errors are an `ExceptionGroup`; the block's own exception cancels the tasks and is raised bare; a cancellation from outside is re-raised as a cancellation; a task whose cleanup is shielded holds the block until the cleanup ends.
- `start_soon()` after the owner has ended raises `RuntimeError`; the ContextVar is reset on every exit path.

`tests/test_background_members.py`, the swarm:

- **Drain at member end** (the plan's required case, both outcomes): a `deepagent(background=True)` member submits while one child is in a tool call that finishes within grace and another in one that does not. The first finishes its call, is refused its next one and ends; the second is cancelled by the owner's exit and awaited before the member ends. The member's status is `submitted`, and its `MemberEnded` is queued when its agent returns, before either child has ended; `events()` and the run end only after both children's spans have ended; its ledger line's usage includes both children's completed calls; `background_started == 2`, `background_cancelled == 1`, `total_complete` is false. With grace long enough for both, `background_cancelled == 0` and no model event is errored.
- **A controller acts at the decision** (B1 of round 6): a controller shaped like Epoch's `until_proved` starts two members; member A submits while one of its children is held in a tool call. The controller receives A's `MemberEnded` (status `submitted`, `state == "ended"`) while that child is still held, returns, and the stop closes member B's lever then, not one grace later; A's child then finishes within grace and is recorded, and the run ends after A's drain.
- **Steps to the answer**: the selected member has one child call that completes before its submission; a second child whose call reaches the child's own token limit in its post-call check (finalized with usage, `completed` unset), whose future the parent collects before submitting; a held child call that finishes during the drain; and a cache replay before the submission. `calls_to_decision` counts the first call, the limit-stopped call and the replay, and not the held call; `steps_to_answer` equals it; the ledger's `calls` and `cache_replays`, and `longest_member_steps`, count all four.
- **Member time limit** (a member `time_limit`): the agent is cancelled at the limit and ends `member_limit:time`; a child in a tool call is not cancelled with it, finishes within grace and is recorded; a child still running at the end of grace is cancelled.
- **Custom limit in a child** (with whichever of M2-2 and M2-5 merges second): a child that raises `LimitExceededError("custom", ...)` with no source ends alone, its sibling and the member's agent continue, and the member submits; raised in a child during the drain, the member keeps `submitted`.
- **Soft stop** (the controller's `all_done`, and the time cap): children with calls in flight finish them, none is cancelled, every call is recorded.
- **Cap reached in a child** (token and cost caps, table-driven): siblings' calls in flight finish (no errored events); the stop reason is `swarm_cap`; the member is `swarm_cap` when its agent then reaches the cap, and keeps `submitted` when the cap was reached only during its drain.
- **Cap reached in a child's tool call while the parent is blocked** (round 5's case): the member's agent waits in `agent_wait(mode="all")` on two children, with no time cap. One child runs a tool stage of two parallel calls: one reaches the cap through a nested model call (mapped to a `limit` tool result), the other blocks on an event the test holds. `stop("swarm_cap")` has been called by the time the capped call's tool result exists (the stop's evidence and the other members' closed levers show it while the blocked call is still held). The test then releases the call within grace: the child is refused its next call and ends alone, the second child likewise, the agent ends `swarm_cap`, and no call in flight is cancelled. With the blocked call held past grace, the hard stop cancels it and the run ends.
- **Cap reached by a native member's own tool call** (no background work): the same early `stop("swarm_cap")`, and the member's status is `swarm_cap` as in M1.
- **Member limit reached in a child** (a member token limit): the child ends alone, the member ends `member_limit:token`, other members continue.
- **Member message limit reached in a child**: a child whose conversation passes the member's message limit ends alone; the member's agent, with a shorter conversation, keeps going and submits (`submitted`), and other children are unaffected.
- **Sample limit reached in a child while the member's agent runs**: recorded as the sample's limit, not a swarm cap, the member `errored`, as for the same deepagent outside a swarm.
- **Hard stop during a drain**: a child stuck past the swarm's grace is cancelled; the member keeps `submitted` and its ledger line says `cancelled`.
- **Foreign error during the drain of the chosen member** (a sample token limit reached by a child's in-flight call, and a custom worker's `RuntimeError`): member A submitted first and is the provisional answer. Without another submission, and with member B having submitted after A, the sample's output is A's, `FinalRecord.member` is A with `provisional: true`, A's `MemberResult` still says `submitted`, and the ledger says `interrupted`; the sample limit is recorded as the sample's, and the `RuntimeError` fails the sample. With B having submitted before A, B stays the answer.
- **`first` across a drain**: member A submits at t1 and drains until t3, member B submits and ends at t2 (t1 < t2 < t3); the provisional answer is A's from t1 and the final answer is A's; A's `MemberResult.time` is t1, and A's `MemberEnded` precedes B's.
- **Isolation**: two background members' drains do not wait for each other's children; a child's tool state is its member's (`memory()` default instance), with `tests/test_tool_state.py`.
- **A custom agent that calls `background()` directly**: its task is cancelled at member end, not at sample end, and counted in `background_started`.
- **Construction-time background work is refused**: a factory that calls `background()` while building its agent makes the member `errored` with the `RuntimeError` naming the member, and the task it started is cancelled before the member ends.
- **Construction** (`tests/test_member.py`): the four refusal cases become acceptance cases, and the logged member dict with `background` round-trips in both forms.
- **Shared memory** (with whichever of M2-5 and M2-8 merges second, in `tests/test_shared_memory.py`): a background `general()` child writes and views a file; the write's `sender` is the member, its `sender_span_id` the child's agent span, `nested: true`; the member's other loops see it as the member's. A child's command during its member's drain is refused with "has ended", and the board is unchanged.

## Implementation plan

1. **AI-5 (inspect_ai), one pull request.** `BackgroundTarget` and `background_target` in `src/inspect_ai/util/_background.py`, with the branch in `background()`; the condition in `BackgroundRegistry.owned_task_group()` in `src/inspect_ai/agent/_deepagent/agent_tool.py`, and the docstrings of both. Tests as above in `tests/util/test_background.py` and `tests/agent/deepagent/test_deepagent_background.py`. No public API and no docs, so not a `feat:` (`refactor:`). Depends on nothing.
2. **M2-5 (inspect_swarm), one pull request**, after a PyPI release of inspect_ai containing AI-5, and after M1-4, M1-5 and M1-6. `src/inspect_swarm/_background.py` (new: `MemberBackground`, `member_background()`); `src/inspect_swarm/_member.py` (the owner in `run_member()` around the node stack, the `decide` and `counts` parameters, the drain, `_check_background()` deleted, `member()`'s docstring); `src/inspect_swarm/_budget.py` (the cap notification in `SwarmNode.exceeded()`); `src/inspect_swarm/_controller/_control.py` (the wrapper receives `self._caps` as before and `self._grace_now`; `_decide()` publishes the result and `MemberEnded` at the agent's return; `_end()` ends the drain, and `_run_member()`'s `errored` fallback applies only to an undecided member; `MemberRun.draining`, `calls_to_decision` (the snapshot, using the ledger module's counting) and `BackgroundCounts`; `events()`, `_until_quiet()` and the hard stop include draining members); the ledger module from M1-6 (`_budget.py` or `_ledger.py`) for the three fields and the completeness rule; `src/inspect_swarm/_metrics.py` (M1-11's `steps_to_answer` reads `calls_to_decision`); the record module from M1-5 (`_record.py`) for when `MemberResult` is written; `pyproject.toml` (the floor) and `uv.lock`. `_swarm.py` changes only if M1-4 lands the wrapper there. Tests as above, in `tests/test_background.py`, `tests/test_background_members.py`, `tests/test_member.py`, `tests/test_tool_state.py` and `tests/test_metrics.py`.
3. **The shared-memory background case** goes into whichever of M2-5 and M2-8 merges second.
4. **The custom-limit case** (a sourceless `custom` limit from a child ends the child alone) goes into whichever of M2-2 and M2-5 merges second: the owner's rule in `src/inspect_swarm/_background.py` and its tests.

## Changes to other designs and the plan

Follow-ups, not edits made here:

- **swarm-implementation-plan.md.** D-2: done, with this document's path, once merged. AI-5's title, "A scoped owner for `background()`" (`:309`, and the diagram at `:274`), becomes "A private seam for where `background()` starts work", with the files and tests of [Implementation plan](#implementation-plan) step 1 and a `refactor:` title. M2-5: "enters AI-5's owner for each member" (`:350`) becomes "enters its own owner for each member, set as AI-5's seam", and the cap notification joins it, with the files and tests of step 2 (adding `_background.py`, `_budget.py`, `_controller/_control.py`, the ledger and record modules and `pyproject.toml`; `_swarm.py` only if the wrapper lives there), and "Depends on" gains M1-5 and an inspect_ai release containing AI-5. M2-8's sentence on the background-child test stands. M2-2 gains: a sourceless `custom` limit raised in a background child ends that child alone, with the owner's rule landing in whichever of M2-2 and M2-5 merges second. M1-11: "Under `first` the selected member ends at its submission, so its steps to the answer are all its steps" becomes: `steps_to_answer` reads the selected member's `calls_to_decision`, the calls completed by its decision, since background children can complete calls during its drain; `longest_member_steps` keeps the full count. M2-5's files add `_metrics.py`. Compatibility (`:452`): "AI-5 changes nothing outside an owner scope, as D-2 must confirm" becomes "AI-5 changes nothing while its seam is unset", confirmed, linking [Compatibility and migration](#compatibility-and-migration). Security: D-2's section is [Security](#security). Wording at `:70` and `:95`: replace "runs a different arm" / "the port is a different arm" with what happens: without background members a port must run `deepagent()` synchronously, so each subagent call blocks its member, which changes the experiment and makes its results not comparable to Epoch's.
- **swarm.md** [Members](swarm.md#members): "Not supported in M1" says M2 supports members that use `background()`, through the member's owner, and links here; "Lifting the restriction" says the inspect_ai change is a private seam for where `background()` starts work and the owner is swarm code, and links here. Goals ("Until the swarm can do that ...") and the Testing bullet that rejects `background` in `args` say M1, and M2-5 replaces that test with the acceptance cases here. The current-behaviour section on deepagent's background subagents stays as a description of inspect_ai with the seam unset.
- **swarm-api.md** [Members](swarm-api.md#members): the refusal paragraph says M2-5 removes it and links here; Testing's `test_member.py` refusal cases are replaced by acceptance cases in M2-5. `MemberHandle.state`, `status` and `MemberEnded` are set when the member's agent ends, as now; a member's background work may still be draining after its `MemberEnded`, and `events()` ("A member counts as running until the runtime has taken its submission's verdict and queued its `MemberEnded`", `:399`) also keeps going until every drain has ended. Controllers (`until_proved`, `stop_on_verified` at `:628`, `coordinator` at `:671`, `escalate` at `:699`) act on `MemberEnded` unchanged, at the agent's return.
- **swarm-limits.md.** [Stopping](swarm-limits.md#stopping): a member's background work is drained at member end by the procedure here, and the soft stop, grace and hard stop reach children through the member's owner; the member wrapper sketch gains the owner around the node stack, the construction refusal, the decision before the drain, and the drain; grace and the hard stop cover draining members. The cap trigger ("The first member to raise a swarm cap error has its wrapper call `stop("swarm_cap")`") becomes: the cap node calls `stop("swarm_cap")` when it detects the cap, for native members as for bridged ones. [The ledger](swarm-limits.md#the-ledger): `background_started`, `background_cancelled` and `calls_to_decision`, the extra `total_complete` condition, and "background children once they are supported" becomes "background children". [Interactions](swarm-limits.md#interactions): the background-subagents bullet links here and adds that a child stopped by a member node or a cap ends alone. Member wrapper and statuses (`:355`): a sourceless `custom` limit from a background child ends the child, not the member.
- **swarm-scoring.md** [What the swarm records](swarm-scoring.md#what-the-swarm-records): `MemberResult.time` is the swarm time the member's agent returned, and a member's result (status, output, time), its state `ended` and its `MemberEnded` are written then; the swarm's finalisation waits for every member's drain. [When verdicts are taken](swarm-scoring.md#the-final-answer-chain): verification starts at the decision, alongside the drain, and the verdict and `MemberEnded` do not wait for the drain. A decided status is final: a foreign error during a member's drain leaves its submission, the provisional `FinalRecord` and the output as they were, and propagates with `provisional: true` ([Statuses, ended, and the record](#statuses-ended-and-the-record)).
- **swarm-lean-open-problems.md** [Recommendation 3](swarm-lean-open-problems.md#3-m2-background-deepagent-members): Change links here and says the inspect_ai part is a private seam. The port sketch's comment that `"deep"` must not set `background` before Rec. 3, "What it cannot do yet", and the line under [Could inspect_swarm express their experiment?](swarm-lean-open-problems.md#could-inspect_swarm-express-their-experiment) that needs a synchronous deepagent say the builder keeps `background=64` once M2-5 lands, with children stopped at their member's end. `until_proved` (`:230-240`) works unchanged: it acts on `MemberEnded`, which is queued at the proving member's decision, so the other members are stopped then, not after that member's drain. Wording at `:287`: "the two are different arms" becomes "these are different experimental conditions: the observed shared memory tool keeps today's behaviour, while the bus channel adds bounded files, checked writes and delivery evidence". Wording at `:146`: "that is a different arm: subagent calls block the member instead of running beside it" becomes "each subagent call would then block its member until the subagent finishes, instead of running alongside the member's other work, which changes the experiment, so the results would not be comparable to Epoch's".
- **swarm-memory-channel.md** [Descendants](swarm-memory-channel.md#descendants): no change of design; it can note that a background child's commands during its member's drain are refused by the existing `running` check, and that no child outlives the drain. Wording: at `:264` and `:341`, "would make the port a different arm" becomes "would change the experiment: the children, which do most of the proving, would lose the board, so the port's results would not be comparable to Epoch's"; at `:322`, "the two are different arms" becomes "the two are different experimental conditions: one memory tool shared through the board, or a board plus a private notebook per member".

## Open questions

None. Ransom settled the public API question on 2026-10-16: the owner mechanism is private ([Non-goals](#goals-and-non-goals)).

## Not this design

Adjacent problems noticed and left out. The first four are known limitations of the expected-cases scope, to fix if real use hits them (decision: Ransom, 2026-10-16).

- **Grouped control flow in a background child is swallowed** (inspect_ai). `_run_background()` matches only bare control-flow errors (`agent_tool.py:773-802`), so an exception group holding a limit error or `TerminateSampleError` (a custom subagent or tool that runs its own anyio task group) becomes an `errored` child instead of reaching the member's owner or the sample.
- **A tool's exception group is reduced to one leaf** (inspect_ai). The tool pipeline applies `inner_exception()` to a tool's error (`_call_tools.py:347-349`, `:738-739`, `:228-235`), so a group of a cap and another error loses one of them before anything sees it: a cap with a `RuntimeError` can become only a mapped cap result, or only the `RuntimeError`, and a cap with a `TerminateSampleError` can lose the termination. The cap node's notification still fires, since it runs before the tool pipeline.
- **A grouped contained limit reaching the member's owner** (inspect_swarm). The owner contains only bare limit errors; a group, even of contained leaves only, is foreign and makes the member `errored`. No supported member raises one.
- **A limit the member's agent enters itself, reached in a child** (inspect_swarm). The owner contains a fixed set, the member's nodes and the caps. A custom agent that wraps a nested deepagent in its own `token_limit` creates a node outside that set, although it bounds less than the member, so a child reaching it is foreign and makes the member `errored`. No supported member does this; containing nodes entered inside the owner would need the owner to read inspect_ai's limit trees.
- **`background()` on a cancelled sample task group** (inspect_ai). In a scorer, `background()` starts work on a task group that is already cancelled, so the work is cancelled at once with no error. deepagent works around it (inspect_ai#5718); `background()` itself could refuse.
- **deepagent's status for a child ended by a limit** (inspect_ai). Such a child reads `cancelled`, with no message; showing the limit's message, as `errored` shows its error, would tell the parent why.
- **Background work started by a controller.** No controller calls `background()`; an owner around the controller's `run`, drained at the stop, would extend the guarantee to controllers.
- **Unowned concurrency in members.** Raw `asyncio` tasks and detached sandbox processes; a harness-validity check could report sandbox processes still running when a member ends.
