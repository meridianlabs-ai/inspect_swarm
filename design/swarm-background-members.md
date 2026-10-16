# Inspect Swarm: background deepagent members

Status: proposed, 2026-10-16. Issue: none. Author: agent (Claude), reviewed by Codex; see the PR.

## Overview

Each member's background agents belong to that member. They run alongside the member and spend against its budget. They stop with it: when the member ends, or when the swarm stops, they get the swarm's grace period to finish what they are doing, and whatever is left is then cancelled. So a member is never reported as ended while its background agents are still working, and the ledger and member statuses count their work as the member's. Members built with `deepagent(background=...)`, which M1 refuses, are accepted.

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
- On the M1-4 branch, the member wrapper `run_member()` sets the member ContextVar, enters `tool_state_scope(name)` and the member's cancel scope, builds the agent, and calls `run(agent, input, limits=nodes, name=name)`, where `nodes` are the member's lever and limits (`_member.py:291-324`, at `ede24f8`). An error is split into cap, owned and foreign leaves by `classify_errors()` (`:338-356`): a cap stops the swarm, an owned leaf gives a status, a foreign one is raised. A cancelled member scope gives `stop_cause` or `cancelled`.
- `SwarmNode.exceeded()` raises the node's `LimitExceededError` for a native member. For a bridged member it instead sets the stop cause, cancels the member's scope and, for a cap, calls `stop_swarm("swarm_cap")` at once, because a raise there would end the sample (`_budget.py:250-275`, at `ede24f8`).
- `SwarmRun.stop()` closes the lever of every running member and the runtime lever (`_controller/_control.py:255-268`); `_supervise()` waits for running members up to the effective grace, cancels the scopes of those still running, and waits again (`:323-347`); `_grace_now()` clips grace to half the sample's remaining time (`:349-354`). `_run_member()` calls `_end()` in its `finally`, which sets the state `ended` and the status together and queues `MemberEnded` (`:300-317`). The caps are entered once around both task groups (`:237-247`).
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
            ├── run(agent, limits=nodes)                    ← the member's agent (top loop)
            └── tasks background() starts anywhere in the member:
                deepagent children, custom workers
```

1. The member's agent returns (or ends on its own limits, the lever or a cap). Its status, output and time are recorded, and the provisional answer is updated.
2. Drain: close the member's lever, wait for the owner's tasks up to the effective grace, leave the owner (cancelling what is left).
3. The member ends: state `ended`, `MemberEnded`.

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
2. It raises a bare `LimitExceededError` whose `source` is one of `contain`. The task ends alone; its siblings and the member's agent continue, and the error goes no further. The member passes its own nodes (lever, share, member limits) and the swarm's caps, so a child stopped by any of them ends alone. `classify_errors()` uses the same two sets.
3. It raises anything else: a limit from outside `contain` (the sample's), `TerminateSampleError`, `ModelRefusalError` under `fail_on_refusal`, `SentinelFailure`, a custom worker's crash, or any exception group. The task group cancels the member's agent and the other tasks, and the error is raised from the block, as such an error from a task in the sample's task group ends the sample today. The member wrapper treats it as foreign.

deepagent re-raises the expected control-flow errors bare (`agent_tool.py:773-791`), so cases 2 and 3 meet them in the shape they are raised in. A grouped error that holds a contained limit, which no supported member produces, falls under 3, and the member is `errored` ([Not this design](#not-this-design)).

Why rule 2: the swarm stops members by limits (the lever at a soft stop and at the drain, a reached cap, a member limit). If the first child refused by the lever cancelled its siblings, every stop and every member end would turn the member's other in-flight calls into unrecorded spend ([Why](#why)). A limit from outside `contain` bounds more than the member, so it propagates as today.

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

The member wrapper enters the owner inside the member's tool-state scope and cancel scope, and runs the member's agent inside it:

```python
# src/inspect_swarm/_member.py, run_member(), M2-5 (on M1-4's structure)
token = current_member.set(context)
try:
    with tool_state_scope(name), context.scope:
        async with member_background(contain=[*nodes, *caps]) as owner:
            try:
                agent = build_member(record)
                if owner.started:                 # construction started background work
                    raise RuntimeError(...)       # refused; the owner cancels it
                _, limit_error = await run(agent, input, limits=nodes, name=name)
            except Exception as ex:
                cap, owned, foreign = classify_errors(ex, caps, nodes)
                if foreign is not None:
                    raise foreign                 # no drain: the owner cancels the children
                ...                               # cap: stop("swarm_cap"); owned: its status
            decided(status, ...)                  # status, output and time recorded; provisional answer
            await drain(owner, lever, grace())
    ...                                           # errors from the owner's exit, below
finally:
    current_member.reset(token)
```

- **Placement.** Inside `context.scope`, so the hard stop cancels the children with the member. Inside `tool_state_scope(name)` and the member ContextVar, so children keep the member's tool state and identity (they would anyway, by inheriting the context at dispatch). Around `run()`, so the member's agent and every task it starts run under the owner. Sample limits are not in `contain`, so a child that reaches one ends the sample at once, as it would outside a swarm.
- **One owner per member, every member.** A member with no background work pays a ContextVar set and an empty task group. Bridged members never call `background()`.
- **Background work is started by the agent, not while it is built.** A factory-form member's agent is built inside the block, but before `run()` enters the member's nodes and opens its span (`src/inspect_ai/agent/_run.py:97-101`). A task started then would keep the context of its dispatch: the caps but not the member's lever, share or limits, and no member span, so the drain could not stop its next call and the member's call counts would miss it (a probe on asyncio and trio metered such a task's tokens to the cap and none to the member node, even when the task called the model after the node was entered). No factory does this: deepagent, `react()` and Epoch's builder start background work only from the running agent ([Who calls `background()`](#who-calls-background)). So the wrapper refuses it: if `owner.started` is not 0 after `build_member()`, it raises `RuntimeError` naming the member and saying that background work must be started by the agent when it runs, not by its factory. The owner's exit cancels the task; the error is foreign, so the member is `errored` and the error propagates, as for any factory error (M1-4).

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

- **Soft stop.** `stop()` closes the levers of running members. A member that is draining is still running, so its lever is closed already (first reason wins) and its children are already being refused. In a member whose agent is still running, the agent and its children reach the closed lever at their next calls; the children end alone, the agent returns `stopped`, and the drain then waits for children with calls in flight.
- **Grace.** `_supervise()` waits for running members, and a draining member is running, so grace covers the children. A member's drain wait and the swarm's grace run side by side: a drain that began before the stop may end first, and one that has not ended when the swarm's grace does is cut short by the hard stop.
- **Hard stop.** `_supervise()` cancels the scope of every member still running. The owner is inside that scope, so its children are cancelled with the member's agent, or with its drain. A member whose status was already decided keeps it (below); its ledger line says `cancelled`.
- **Time cap.** It calls `stop("swarm_time")`, so it is the soft stop above.
- **A cap reached inside a child.** The cap node calls `stop("swarm_cap")` as it detects the cap ([Cap notification at the cap node](#cap-notification-at-the-cap-node)), whatever the member's agent is doing: running, blocked in `agent_wait` on a sibling in a long tool call, or draining. The cap is in `contain`, so the child that raised ends alone and its siblings' calls in flight finish. Without containment, the first child to call the model after the cap was reached (cap checks run root to leaf, ahead of the lever) would cancel every sibling's call in flight at the moment the whole swarm is being soft-stopped.

### Statuses, ended, and the record

**A member ends when its drain ends.** Its `MemberHandle.state` stays `running` until the owner's block has been left; then it becomes `ended` and `MemberEnded` is queued. Reasons:

- The swarm's guarantee is that it owns and drains all of a member's work. A member reported `ended` while its children spend would break it.
- `events()` ends, and `leaderless` returns `all_done`, when no member is running. They must not fire while children still spend: the swarm would finalise and write its ledger with work still running.
- The stop procedure waits for and cancels *running* members. Keeping draining members running is what makes grace and the hard stop reach their children with no extra state.

**Its status is decided when its agent returns.** The status comes from how the agent ended, exactly as in M1: `submitted`, `unsubmitted`, `stopped`, `share`, `member_limit:<type>`, `swarm_cap`, or from a cancelled scope `cancelled` (and `errored` for a foreign error). The runtime records the member's status, output and time in its `MemberResult` and updates the provisional answer at that moment, before the drain; `MemberHandle.status` and `submitted` read the decided status from then on. Reasons:

- `first` is "the submission with the earliest swarm time" ([swarm-scoring.md](swarm-scoring.md#the-final-answer-first)). A member that submits at t=10 and drains until t=130 submitted before one that submits and ends at t=20. `MemberResult.time` is therefore the time the member's agent returned.
- The provisional answer exists so that an outer limit ending the sample early leaves the first submission as the output ([swarm-scoring.md](swarm-scoring.md#the-final-answer-first)). A sample limit during a drain must find the submission already recorded.

So `MemberHandle.status` reads "how the member's agent ended, once it has", and is set while `state` may still be `running` for the length of a drain. The closed status set does not change.

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
- **Member limits** (token, cost, turn, message) meter children as they meter a member's synchronous subagents. Token, cost and turn limits are member-wide: children's calls count towards them, and once one is reached the member's agent ends on it at its next call. A member message limit bounds each loop's own conversation, so a child that reaches it ends alone and the member can still go on and submit, as with a synchronous subagent; the swarm adds no member-wide message count. A member's time limit is an Inspect `time_limit` entered by `run()`; it cancels the member's agent only, and the drain then stops the children within the effective grace. `member()` documents it: the member's time limit bounds its agent; after the agent returns, its background children receive the effective grace period before cancellation, and cancellation cleanup is awaited.
- **The lever** records every child's usage, so the member's ledger line includes its children's spend. A child's span is a descendant of the member's span (Spike 1), so the member's call counts include its children's model events.

`MemberLedger` ([swarm-limits.md The ledger](swarm-limits.md#the-ledger)) gains two fields, from the member's owner:

```python
class MemberLedger(BaseModel):
    ...
    background_started: int     # tasks background() started in the member's owner
    background_cancelled: int    # of those, still running when the drain gave up, cancelled by the owner
```

The wrapper copies `owner.started` and `owner.cancelled` to the member's run record (M1-4's `MemberRun`) after the block, on every path, and the ledger reads them there. They count every task `background()` started anywhere in the member, nested deepagents' children included, since deepagent steps aside under the owner. `total_complete` additionally requires `background_cancelled == 0` for every member, for the same reason it requires no member `cancelled`: a cancelled agent loop may have had work in flight that left no trace. Calls a cancelled child had in flight are counted as `errored`, as anywhere. A child the model cancelled with `agent_cancel` is its own decision; its in-flight call, if any, is an errored event and already clears `total_complete`. Evidence of which children ran is their spans; the swarm adds no event of its own.

### Lifting the refusal

M2-5 deletes `_check_background()` and its two calls. `deepagent(background=True)`, `deepagent(background=64)` and `member(deepagent, args={"background": 64})` are accepted, and so is any agent, wrapped or custom, that calls `background()` while it runs: the owner catches them all, so nothing depends on reading registry params. A factory that calls `background()` while building the agent is refused when the member starts ([Entering the owner](#entering-the-owner)). `member()`'s docstring replaces the refusal sentence with the contract:

> Background work belongs to the member. Work its agent starts with `background()`, such as `deepagent(background=...)` subagents, spends through the member's limits and share, and is the member's in the ledger. When the member's agent returns, its status is decided; the swarm then stops the background work from starting model calls, gives it the swarm's grace period to finish calls in flight, and cancels what is left. The member ends after that. Background work must be started by the agent while it runs; a factory that starts it while building the agent makes the member `errored`. Work started outside `background()` (an `asyncio` task, a process left running in the sandbox) is not stopped.

### The shared-memory channel

A background `general()` child reaches the board exactly as a synchronous one does ([swarm-memory-channel.md Descendants](swarm-memory-channel.md#descendants)):

- It inherits the member ContextVar at dispatch, so it is the member: its writes are records whose `sender` is the member.
- deepagent hands `general()` the parent's tools, including `swarm_tools()`. The child resolves it in its own agent channel, which is not the member's bound loop (the member's loop resolved first, before it could dispatch), so `Channel.reach = "member"` gives it the `shared_memory` tool and not the bound loop's notices or `"loop"`-reach tools.
- Its writes carry its own agent span as `sender_span_id` and `nested: true`; its views are `read` events in its own tool spans.
- It shares the member's `rate`: one member's 64 children cannot multiply its write volume.
- **During the drain** the member is still running, so the bus accepts the writes and reads of a child finishing a tool call. Once stopping begins, writes are refused (`bus.close()` at step 0); views still work while the member is running, as for every running member ([swarm-memory-channel.md](swarm-memory-channel.md#concurrency-and-compare-and-swap)). A child never writes after its member has ended, because the owner has ended it by then; without the owner, a child outliving its member would have met the bus's sender-liveness check.

The test that a background `general()` child reads and writes the board as the member lands with whichever of M2-5 and M2-8 merges second ([Testing](#testing)).

### What depends on how M1-4 lands

M1-4 is on its branch at `ede24f8`. This design depends on:

- the wrapper's structure: member ContextVar, then `tool_state_scope(name)` and `context.scope`, then `run(agent, input, limits=nodes)` with the lever among `nodes`, and `classify_errors()` (the owner goes between the scopes and `run()`);
- `SwarmNode.exceeded()` with `MemberContext.stop_swarm`, where the native branch gains the cap notification;
- `stop()` closing the levers of running members, `_supervise()` waiting for and cancelling running members, and `_grace_now()`;
- `_end()` setting the state and the status together. M2-5 splits it: the status, output and time are recorded when the agent returns, the state `ended` and `MemberEnded` when the drain ends.

If M1-4 merges with a different shape, M2-5 keeps the same placements: the owner inside the member's cancel scope and tool-state scope and around `run()`, the member's nodes and the caps as its contained set, and the cap notification at the node that detects the cap. M1-5's record, which M2-5 also changes, is not built yet ([Changes to other designs and the plan](#changes-to-other-designs-and-the-plan)).

## Alternatives considered

**A public owner in inspect_ai** (this design's first five rounds): `inspect_ai.util.background_owner(contain=, on_limit=)` with the drain, containment and counts in inspect_ai. It would let other callers own background work. But no caller outside the swarm needs it, and every rule it holds (which limits are contained, how long the drain waits, what is counted) is the swarm's, so it is better as swarm code that inspect_swarm owns and tests. Ransom chose the private seam and an owner in the swarm (decision: Ransom, 2026-10-16: "owner in the swarm - yes").

**No inspect_ai change: redirect from the swarm.** The swarm would swap `sample.tg` or wrap deepagent's registry. Neither is possible per member: `sample.tg` is one attribute shared by every member, and the registry is created inside each deepagent call ([Current behaviour](#current-behaviour)). The seam is the smallest change that gives the swarm a per-member, per-context hook.

**An owner with plain task-group semantics**, where any exception from a task, a limit error included, cancels the block and its other tasks. Simplest, and it is what `owned_task_group()` does. But the swarm stops members by limits: the lever at a soft stop and at the drain, a reached cap, a member limit. Under plain semantics the first child refused by the lever cancels every sibling's call in flight, so every stop and every member end would turn a member's in-flight children into unknown spend. With 32 members and up to 64 children each, that is most of the calls in flight at the cap. The prototype's case with the lever outside the contained set showed it: the first child refused ended the whole block.

**Contain every `LimitExceededError` in the owner**, from the sample's limits too. Simpler classification. But a sample limit reached in a child would then stop only that child, and the sample would end only when the member's agent reached the limit or the member ended. For shared token, cost and turn nodes that is the next call; for a sample message limit, which a child's own conversation can reach without its parent's, it is the end of the member.

**Containment in deepagent instead of the owner.** `_run_background()` could end the child alone for some limit errors. It does not know which limits are the member's and which the sample's, and custom agents that call `background()` would not get it. The owner is where the boundary is, and it is swarm code.

**Notify caps from the owner**, with a callback when a child ends on a cap (rounds 1 to 5). It misses the expected case: a cap reached inside a child's tool call becomes a tool result and the child does not end, so no callback fires while the parent waits in `agent_wait` ([Limit errors inside tool calls](#limit-errors-inside-tool-calls)). The cap node is the first point that sees the cap, and it already notifies for bridged members.

**Cancel children at member end, with no grace.** Simpler drain, a member ends at once. It loses every call in flight at each member's end and makes `total_complete` false for nearly every background member.

**Let children run until the swarm stops**, so a member is "ended" while its children run, and drain only at the swarm's stop. This keeps Epoch's current behaviour exactly. But it breaks the guarantee that `ended` means no more spend from the member, makes `all_done` wait on children no member can see, and keeps spending on attempts that have already submitted or been stopped. The plan asks for the drain at member end.

**Wait for children to finish without closing the lever**, up to a deadline. Children would keep starting new calls on an attempt that is over, so the drain's length would be the deadline every time.

**An owner that drains with a deadline on exit.** It would leave tasks running after the block, outside any structure. The deadline belongs to the drain, with `wait()` and `move_on_after()`.

**Owning `asyncio` tasks and `run_in_background()` too.** No member uses them, and `run_in_background()` serves eval-level machinery that must outlive any sample.

## Compatibility and migration

**inspect_ai, with the seam unset: no change.** `background()` reads one ContextVar and, finding none, runs today's code: same checks, same logging, same task group, same errors. `owned_task_group()` adds one condition that is false. Every caller in [Who calls `background()`](#who-calls-background) runs with it unset: solver-level workers in the docs and tests, inspect_ranges' poller, and deepagent in solvers and scorers. So `TestAbandonOnExit`, `test_solver_child_outlives_parent_until_sample_teardown` and `TestNoLiveSampleTaskGroup` pass unchanged. The working-limit monitor and the hooks loop do not use `background()`.

**inspect_ai API.** None public. `background_target` and `BackgroundTarget` are private names in a private module; no log, schema, event or viewer type changes; nothing in ts-mono.

**inspect_swarm.** No released API until M2-12. Accepted members widen (background deepagents, agents that call `background()`). `MemberLedger` gains two fields under its existing `version: 1`, and `MemberResult.time` becomes the time the member's agent returned; neither has shipped. `MemberHandle.status` can be set while `state` is still `running`. A native member's cap now starts the stop when the cap node detects it, rather than when the error leaves the member's agent; the reason and the members' statuses are the same. `pyproject.toml`'s `inspect-ai` floor rises to the first release containing AI-5, since `src/inspect_swarm/_background.py` imports `background_target`. That import is private, as the swarm's imports of inspect_ai's limit classes are (`_budget.py` subclasses `_TokenLimit` and the others); the owner's tests, run against each inspect_ai release the floor allows, catch a change to it.

**Epoch's port.** Their builder keeps `background=64`; their member wrapper needs no change, because the owner does not depend on recognising deepagent. Their pinned inspect_ai (0.3.273) must move to a release with AI-1, AI-2 and AI-5, as the plan already says (M2-12).

## Security

What reaches the new code, and what it changes about who can outlive a member:

- **Model output decides how much background work there is.** A member's model chooses when to dispatch children, up to deepagent's `max_background`, and what they do. The owner bounds their lifetime to the member's and the lever and caps bound their spend. The effective grace limits how long children may continue normal work before cancellation. The hard stop and the sample time limit request cancellation; shielded cleanup can delay exit, which waits for every owned task. Before this design, the only bound on a child's lifetime was the sample's.
- **A model cannot make work outlive its member through `background()`.** The target is a ContextVar in the member's task tree. No tool or model output names a target, and a child cannot reach another member's owner, since member tasks are siblings started from the runtime's saved context.
- **What escapes the owner.** Raw `asyncio` tasks started by agent or tool code, and processes a model leaves running in the sandbox (`nohup`, `&`), are not owned. Agent and tool code is the eval author's, trusted as in any Inspect eval; a sandbox process can keep changing a shared or per-member sandbox after its member ended, as [swarm-lean-open-problems.md](swarm-lean-open-problems.md#8-after-m2-per-member-sandboxes-in-the-scoring-contract) notes for scoring. `member()` documents both.
- **Exception text** from children, which can contain model-influenced strings, is logged as today (`background()`'s wrapper, deepagent's warning) and, for foreign errors, raised as today. The owner adds no text of its own beyond the two `RuntimeError` messages, which contain no input.
- **The cap notification** runs `stop()` inside a model call's limit check. `stop()` is synchronous and reads only the swarm's own state; it takes no input from the call.
- **The shared-memory channel** gains no new writer: background children were already designed in as the member's descendants ([swarm-memory-channel.md Security](swarm-memory-channel.md#security)), and the owner only guarantees that none writes after its member ended.

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
- Limits (table-driven): a task that raises a bare limit error from a node in `contain` ends alone, a sibling keeps running, and the block's code continues; a limit from a node outside `contain` (a sample limit) cancels the sibling and the block's code and is raised; a `LimitExceededError` with `source=None` is raised.
- Errors: a task's `RuntimeError` cancels the block's code and a sibling and is raised bare; two task errors are an `ExceptionGroup`; the block's own exception cancels the tasks and is raised bare; a cancellation from outside is re-raised as a cancellation; a task whose cleanup is shielded holds the block until the cleanup ends.
- `start_soon()` after the owner has ended raises `RuntimeError`; the ContextVar is reset on every exit path.

`tests/test_background_members.py`, the swarm:

- **Drain at member end** (the plan's required case, both outcomes): a `deepagent(background=True)` member submits while one child is in a tool call that finishes within grace and another in one that does not. The first finishes its call, is refused its next one and ends; the second is cancelled by the owner's exit and awaited before the member ends. The member's status is `submitted`; `MemberEnded` comes after both children's spans have ended; its ledger line's usage includes both children's completed calls; `background_started == 2`, `background_cancelled == 1`, `total_complete` is false. With grace long enough for both, `background_cancelled == 0` and no model event is errored.
- **Soft stop** (the controller's `all_done`, and the time cap): children with calls in flight finish them, none is cancelled, every call is recorded.
- **Cap reached in a child** (token and cost caps, table-driven): siblings' calls in flight finish (no errored events); the stop reason is `swarm_cap`; the member is `swarm_cap` when its agent then reaches the cap, and keeps `submitted` when the cap was reached only during its drain.
- **Cap reached in a child's tool call while the parent is blocked** (round 5's case): the member's agent waits in `agent_wait(mode="all")` on two children, with no time cap. One child runs a tool stage of two parallel calls: one reaches the cap through a nested model call (mapped to a `limit` tool result), the other blocks on an event the test holds. `stop("swarm_cap")` has been called by the time the capped call's tool result exists (the stop's evidence and the other members' closed levers show it while the blocked call is still held). The test then releases the call within grace: the child is refused its next call and ends alone, the second child likewise, the agent ends `swarm_cap`, and no call in flight is cancelled. With the blocked call held past grace, the hard stop cancels it and the run ends.
- **Cap reached by a native member's own tool call** (no background work): the same early `stop("swarm_cap")`, and the member's status is `swarm_cap` as in M1.
- **Member limit reached in a child** (a member token limit): the child ends alone, the member ends `member_limit:token`, other members continue.
- **Member message limit reached in a child**: a child whose conversation passes the member's message limit ends alone; the member's agent, with a shorter conversation, keeps going and submits (`submitted`), and other children are unaffected.
- **Sample limit reached in a child while the member's agent runs**: recorded as the sample's limit, not a swarm cap, the member `errored`, as for the same deepagent outside a swarm.
- **Hard stop during a drain**: a child stuck past the swarm's grace is cancelled; the member keeps `submitted` and its ledger line says `cancelled`.
- **Foreign error during the drain of the chosen member** (a sample token limit reached by a child's in-flight call, and a custom worker's `RuntimeError`): member A submitted first and is the provisional answer. Without another submission, and with member B having submitted after A, the sample's output is A's, `FinalRecord.member` is A with `provisional: true`, A's `MemberResult` still says `submitted`, and the ledger says `interrupted`; the sample limit is recorded as the sample's, and the `RuntimeError` fails the sample. With B having submitted before A, B stays the answer.
- **`first` across a drain**: member A submits at t1 and drains until t3, member B submits and ends at t2 (t1 < t2 < t3); the provisional answer is A's from t1 and the final answer is A's; A's `MemberResult.time` is t1.
- **Isolation**: two background members' drains do not wait for each other's children; a child's tool state is its member's (`memory()` default instance), with `tests/test_tool_state.py`.
- **A custom agent that calls `background()` directly**: its task is cancelled at member end, not at sample end, and counted in `background_started`.
- **Construction-time background work is refused**: a factory that calls `background()` while building its agent makes the member `errored` with the `RuntimeError` naming the member, and the task it started is cancelled before the member ends.
- **Construction** (`tests/test_member.py`): the four refusal cases become acceptance cases, and the logged member dict with `background` round-trips in both forms.
- **Shared memory** (with whichever of M2-5 and M2-8 merges second, in `tests/test_shared_memory.py`): a background `general()` child writes and views a file; the write's `sender` is the member, its `sender_span_id` the child's agent span, `nested: true`; the member's other loops see it as the member's.

## Implementation plan

1. **AI-5 (inspect_ai), one pull request.** `BackgroundTarget` and `background_target` in `src/inspect_ai/util/_background.py`, with the branch in `background()`; the condition in `BackgroundRegistry.owned_task_group()` in `src/inspect_ai/agent/_deepagent/agent_tool.py`, and the docstrings of both. Tests as above in `tests/util/test_background.py` and `tests/agent/deepagent/test_deepagent_background.py`. No public API and no docs, so not a `feat:` (`refactor:`). Depends on nothing.
2. **M2-5 (inspect_swarm), one pull request**, after a PyPI release of inspect_ai containing AI-5, and after M1-4, M1-5 and M1-6. `src/inspect_swarm/_background.py` (new: `MemberBackground`, `member_background()`); `src/inspect_swarm/_member.py` (the owner in `run_member()`, the drain, `_check_background()` deleted, `member()`'s docstring); `src/inspect_swarm/_budget.py` (the cap notification in `SwarmNode.exceeded()`); `src/inspect_swarm/_controller/_control.py` (the wrapper receives `self._caps` as before and `self._grace_now`; `_run_member()` records the decided status before the drain and calls `_end()` after it; `MemberHandle.status`'s docstring); the ledger module from M1-6 (`_budget.py` or `_ledger.py`) for the two fields and the completeness rule; the record module from M1-5 (`_record.py`) for when `MemberResult` is written; `pyproject.toml` (the floor) and `uv.lock`. `_swarm.py` changes only if M1-4 lands the wrapper there. Tests as above, in `tests/test_background.py`, `tests/test_background_members.py`, `tests/test_member.py` and `tests/test_tool_state.py`.
3. **The shared-memory background case** goes into whichever of M2-5 and M2-8 merges second.

## Changes to other designs and the plan

Follow-ups, not edits made here:

- **swarm-implementation-plan.md.** D-2: done, with this document's path, once merged. AI-5's title, "A scoped owner for `background()`" (`:309`, and the diagram at `:274`), becomes "A private seam for where `background()` starts work", with the files and tests of [Implementation plan](#implementation-plan) step 1 and a `refactor:` title. M2-5: "enters AI-5's owner for each member" (`:350`) becomes "enters its own owner for each member, set as AI-5's seam", and the cap notification joins it, with the files and tests of step 2 (adding `_background.py`, `_budget.py`, `_controller/_control.py`, the ledger and record modules and `pyproject.toml`; `_swarm.py` only if the wrapper lives there), and "Depends on" gains M1-5 and an inspect_ai release containing AI-5. M2-8's sentence on the background-child test stands. Compatibility (`:452`): "AI-5 changes nothing outside an owner scope, as D-2 must confirm" becomes "AI-5 changes nothing while its seam is unset", confirmed, linking [Compatibility and migration](#compatibility-and-migration). Security: D-2's section is [Security](#security). Wording at `:70` and `:95`: replace "runs a different arm" / "the port is a different arm" with what happens: without background members a port must run `deepagent()` synchronously, so each subagent call blocks its member, which changes the experiment and makes its results not comparable to Epoch's.
- **swarm.md** [Members](swarm.md#members): "Not supported in M1" says M2 supports members that use `background()`, through the member's owner, and links here; "Lifting the restriction" says the inspect_ai change is a private seam for where `background()` starts work and the owner is swarm code, and links here. Goals ("Until the swarm can do that ...") and the Testing bullet that rejects `background` in `args` say M1, and M2-5 replaces that test with the acceptance cases here. The current-behaviour section on deepagent's background subagents stays as a description of inspect_ai with the seam unset.
- **swarm-api.md** [Members](swarm-api.md#members): the refusal paragraph says M2-5 removes it and links here; Testing's `test_member.py` refusal cases are replaced by acceptance cases in M2-5. `MemberHandle.status` is "how the member's agent ended, once it has", which can precede `state == "ended"` for a member draining background work.
- **swarm-limits.md.** [Stopping](swarm-limits.md#stopping): a member's background work is drained at member end by the procedure here, and the soft stop, grace and hard stop reach children through the member's owner; the member wrapper sketch gains the owner, the construction refusal, the drain and the decided-then-ended split. The cap trigger ("The first member to raise a swarm cap error has its wrapper call `stop("swarm_cap")`") becomes: the cap node calls `stop("swarm_cap")` when it detects the cap, for native members as for bridged ones. [The ledger](swarm-limits.md#the-ledger): `background_started` and `background_cancelled`, the extra `total_complete` condition, and "background children once they are supported" becomes "background children". [Interactions](swarm-limits.md#interactions): the background-subagents bullet links here and adds that a child stopped by a member node or a cap ends alone.
- **swarm-scoring.md** [What the swarm records](swarm-scoring.md#what-the-swarm-records): `MemberResult.time` is the swarm time the member's agent returned, and a member's result (status, output, time) is written then, with its state `ended` written after the drain. A decided status is final: a foreign error during a member's drain leaves its submission, the provisional `FinalRecord` and the output as they were, and propagates with `provisional: true` ([Statuses, ended, and the record](#statuses-ended-and-the-record)).
- **swarm-lean-open-problems.md** [Recommendation 3](swarm-lean-open-problems.md#3-m2-background-deepagent-members): Change links here and says the inspect_ai part is a private seam. The port sketch's comment that `"deep"` must not set `background` before Rec. 3, "What it cannot do yet", and the line under [Could inspect_swarm express their experiment?](swarm-lean-open-problems.md#could-inspect_swarm-express-their-experiment) that needs a synchronous deepagent say the builder keeps `background=64` once M2-5 lands, with children stopped at their member's end. Wording at `:146`: "that is a different arm: subagent calls block the member instead of running beside it" becomes "each subagent call would then block its member until the subagent finishes, instead of running alongside the member's other work, which changes the experiment, so the results would not be comparable to Epoch's".
- **swarm-memory-channel.md** [Descendants](swarm-memory-channel.md#descendants): no change of design; it can note that a background child's writes during its member's drain are accepted and that none follows the member's end. Wording: at `:264` and `:341`, "would make the port a different arm" becomes "would change the experiment: the children, which do most of the proving, would lose the board, so the port's results would not be comparable to Epoch's"; at `:322`, "the two are different arms" becomes "the two are different experimental conditions: one memory tool shared through the board, or a board plus a private notebook per member".

## Open questions

None. Ransom settled the public API question on 2026-10-16: the owner mechanism is private ([Non-goals](#goals-and-non-goals)).

## Not this design

Adjacent problems noticed and left out. The first three are known limitations of the expected-cases scope, to fix if real use hits them (decision: Ransom, 2026-10-16).

- **Grouped control flow in a background child is swallowed** (inspect_ai). `_run_background()` matches only bare control-flow errors (`agent_tool.py:773-802`), so an exception group holding a limit error or `TerminateSampleError` (a custom subagent or tool that runs its own anyio task group) becomes an `errored` child instead of reaching the member's owner or the sample.
- **A tool's exception group is reduced to one leaf** (inspect_ai). The tool pipeline applies `inner_exception()` to a tool's error (`_call_tools.py:347-349`, `:738-739`, `:228-235`), so a group of a cap and another error loses one of them before anything sees it: a cap with a `RuntimeError` can become only a mapped cap result, or only the `RuntimeError`, and a cap with a `TerminateSampleError` can lose the termination. The cap node's notification still fires, since it runs before the tool pipeline.
- **A grouped contained limit reaching the member's owner** (inspect_swarm). The owner contains only bare limit errors; a group, even of contained leaves only, is foreign and makes the member `errored`. No supported member raises one.
- **`background()` on a cancelled sample task group** (inspect_ai). In a scorer, `background()` starts work on a task group that is already cancelled, so the work is cancelled at once with no error. deepagent works around it (inspect_ai#5718); `background()` itself could refuse.
- **deepagent's status for a child ended by a limit** (inspect_ai). Such a child reads `cancelled`, with no message; showing the limit's message, as `errored` shows its error, would tell the parent why.
- **Background work started by a controller.** No controller calls `background()`; an owner around the controller's `run`, drained at the stop, would extend the guarantee to controllers.
- **Unowned concurrency in members.** Raw `asyncio` tasks and detached sandbox processes; a harness-validity check could report sandbox processes still running when a member ends.
