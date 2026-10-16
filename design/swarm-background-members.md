# Inspect Swarm: background deepagent members

Status: proposed, 2026-10-16. Issue: none. Author: agent (Claude), reviewed by Codex; see the PR.

## Overview

inspect_ai gains `background_owner()`, an async context manager in `inspect_ai.util`. Inside its block, `background()` starts work in the owner instead of in the sample's task group; outside every owner it behaves exactly as today. When the block ends, the owner cancels whatever it still runs and waits for it to finish. A task that reaches a limit entered inside the block (or one the owner was told to contain) ends alone, so its siblings and the code in the block carry on, and the owner can tell its creator at once through a callback; any other error ends the block, as an error in the sample's task group ends the sample today. deepagent's background dispatch always goes through `background()`, so under an owner its children belong to that owner and can outlive the deepagent call until the block ends.

The swarm enters one owner per member, beside its tool-state scope, and contains its own caps there. When a member's agent returns, the member's status and submission are recorded at once, then the swarm drains the member's background work the way it stops members: it closes the member's lever so the children make no new model call, gives them the grace period to finish calls in flight, and cancels the rest. Only then does the member end. The swarm's own soft stop, grace and hard stop reach the children the same way. Children spend through the member's nodes, so their usage is the member's in the budget and the ledger, which also counts how many children each member started and how many the drain had to cancel. The rejection of `deepagent(background=...)` members is lifted, and any agent that calls `background()` while it runs is supported as a member; a factory that starts background work while building its agent is refused. Background `general()` children reach the shared-memory channel as the member, as synchronous ones do.

## Why

Both of Epoch's LeanOpenProblems configs run `deepagent(background=64)` members ([swarm-lean-open-problems.md Recommendation 3](swarm-lean-open-problems.md#3-m2-background-deepagent-members); their `apn/solver.py:37`, `:145`, at `0aff742d6`). M1 rejects such members (`src/inspect_swarm/_member.py:299-304`), because `background()` attaches work to the sample's task group, where nothing the swarm controls can stop, await or account for it ([swarm.md Members](swarm.md#members)). A port without them runs a different arm: subagent calls would block the member instead of running beside it.

What goes wrong today when a background deepagent runs under a scope of its own, as a swarm member would, was measured here (spikes in [Current behaviour](#current-behaviour)):

- A child keeps spending against the member's nodes after the member has returned, so a member "ends" while its children run on, and the swarm cannot say when it is done or what each member spent.
- A member's own scoped limit reached inside a background child is recorded as the *sample's* limit and ends the sample: a member-level token limit of 20,000 around `run(deepagent(background=True))` ended the sample with `limit: token 20000`, and the code after `run()` never ran.

M2's test is that Epoch can port their swarm ([swarm-implementation-plan.md M2](swarm-implementation-plan.md#m2-ready-for-external-usage)). This design specifies the inspect_ai change (AI-5) and the swarm side (M2-5) that make background members work.

## Goals and non-goals

Goals:

- An inspect_ai owner that `background()` attaches to when one encloses the caller, with its API, nesting, lifetime, cancellation, drain and error semantics, and no change for any caller outside an owner.
- deepagent's background dispatch going through it.
- The swarm entering one owner per member and draining it at member end and at the swarm's stop (soft stop, grace, hard stop, time cap), with the M1 rejection lifted.
- How the ledger, member statuses, stop reasons, member limits and the swarm budget count background children, and how a background `general()` child reaches the shared-memory channel.
- The follow-up edits this implies for the plan and the other designs.

Non-goals:

- Changing what deepagent's children do or how its lifecycle tools report them. Statuses, the `AGENT-N` handles, `max_background` and the reminder hook stay as they are.
- Owning work started outside `background()`: raw `asyncio.create_task`, inspect_ai's internal `run_in_background()` (eval-level logging, batching and throttling), processes left running in a sandbox. [Security](#security) says what this leaves open.
- Background work started by a controller. No controller does; see [Not this design](#not-this-design).
- Fixing deepagent's behaviour outside an owner, where a scoped limit reached in a background child ends the sample (listed under [Not this design](#not-this-design)).

## Current behaviour

Checked against inspect_ai `main` at `e31f6e9ed` (2026-10-16), this repository's `main` at `609a870`, the M1-4 branch `m1-4-runtime-core` at `ede24f8` (not merged), and inspect_ai 0.3.281 installed here for the spikes.

### `background()`

`background(func, *args)` (`src/inspect_ai/util/_background.py:12-74`, exported at `src/inspect_ai/util/__init__.py:38`):

- requires an active, started sample, raising `RuntimeError` otherwise (`:42-50`);
- wraps `func` so that an exception other than `LimitExceededError`, `TerminateSampleError` and `ModelRefusalError` is logged as "Background worker error", and re-raises every exception (`:53-71`);
- starts it with `sample.tg.start_soon(run)` (`:74`). anyio copies the caller's context into the new task, so the task sees the caller's ContextVars: its limit nodes, span, tool-state scope and, in a swarm, the member.

The sample's task group is created around the solver (`src/inspect_ai/_eval/task/run.py:2977-2978`), handed to the active sample (`:2786`, `src/inspect_ai/log/_samples.py:300-302`) and cancelled when the solver finishes, "so that any coroutines created w/ background() are cancelled" (`run.py:2947-2950`). An exception from a background task fails that task group, which ends the solver; a `LimitExceededError` reaching the runner is recorded as the sample's limit (`run.py:3008-3015`).

### deepagent's background dispatch

- `deepagent(background=True | int)` resolves to a cap of running children (`src/inspect_ai/agent/_deepagent/deepagent.py:255-278`) and runs its inner `react()` inside a `BackgroundRegistry` held in a ContextVar and the registry's `owned_task_group()` (`deepagent.py:242-250`).
- `owned_task_group()` does nothing while the sample's task group is live, and otherwise (a deepagent started in a scorer) opens a task group of its own, cancels it when the deepagent exits, waits for the children, and unwraps a single-exception group so the caller sees what was raised (`agent_tool.py:141-181`, `_live_sample_task_group()` at `:184-191`). That path came with inspect_ai#5718.
- `_dispatch_background()` registers an `AgentFuture` and starts `_run_background()` in the registry's group if it has one, else with `background()` (`agent_tool.py:631-686`, the branch at `:681-684`).
- `_run_background()` runs the child in its own cancel scope and span, with the subagent's own `limits` applied with `catch_errors=True` (`:744-745`), so the child's own limits end it with a "Subagent ... stopped" result. Cancellation is recorded as `cancelled` and re-raised (`:767-772`). `LimitExceededError`, `TerminateSampleError`, `ModelRefusalError` and `SentinelFailure` are recorded as `cancelled` and re-raised, because "any LimitExceededError that reaches here belongs to an outer (sample/parent) scope" (`:773-791`). Any other exception is captured on the future as `errored` and swallowed (`:792-802`).
- So, with a live sample task group, children are abandoned when the deepagent returns and run on until the sample's task group is cancelled. inspect_ai's own design says so ("Lifetime semantics: abandon on parent exit", `design/deepagent-background.md`), and `TestAbandonOnExit` and `test_solver_child_outlives_parent_until_sample_teardown` pin it (`tests/agent/deepagent/test_deepagent_background.py:1930-1950`, `:2044`). `TestNoLiveSampleTaskGroup` pins the scorer path (`:1996-2316`).

### What a child inherits, measured

Two spikes, on mockllm, not committed:

- **Spike 1.** A solver opens `span("member")`, enters `token_limit(None)`, calls `background(child)` and leaves the limit; the child sleeps, then calls the model in `span("child")`. After the child finished, the exited limit's usage was 34 tokens, the child's call, and the call's span chain was `child → member → s → solvers`. A token node records into its ancestors (`src/inspect_ai/util/_limit.py:1094-1098`), and the leaf of each limit tree is a ContextVar (`:975-1005`, the trees at `:1016-1021`), so a child keeps metering and checking the nodes its caller had entered, even after the caller left them. This is what [swarm-limits.md The ledger](swarm-limits.md#the-ledger) relies on ("background children once they are supported, since they inherit the member's context").
- **Spike 2.** A solver runs `run(deepagent(background=True, subagents=[general()]), "go", limits=[token_limit(20_000)])` under a 100,000-token sample limit; the top loop dispatches two children and waits for them, and the children generate until the limit is reached. The sample ended with `limit: type='token' limit=20000.0`, a `SampleLimitEvent` for the scoped limit, and the solver's line after `run()` never printed. A scoped limit reached in a background child goes through the sample's task group to the runner and is recorded as the sample's.

### Who calls `background()`

Surveyed in inspect_ai `main` and in the checkouts of inspect_swe (`963523e`), inspect_evals (`d2d3aac91`), inspect_scout (`78a5cbd4`), inspect_sentinel (`35fc75d`), inspect_flow (`851ed610`), inspect_petri (`6266a62`), inspect_harbor (`f3a622a`), inspect_steward (`e18064a`), petri_bloom (`97a0a21`), petri_dish (`d7c6f35`) and inspect_ranges (`b587630`):

- **inspect_ai source:** only deepagent (`agent_tool.py:684`). The sample's task group has two other users that do not go through `background()`: the working-limit monitor (`util/_limit.py:936-965`) and the hooks' event loop (`hooks/_hooks.py:778-805`).
- **inspect_ai tests:** `tests/util/test_background.py:49`, `tests/test_cancellation_logging.py:471`, `tests/util/sandbox/test_sandbox_service.py:106`.
- **inspect_ai docs:** "Background Work" and "Sandbox Service" in `docs/agent-custom.qmd:447-505`, both as solver-level, sample-lifetime workers.
- **Satellites:** inspect_ranges' goal poller (`src/inspect_ranges/_challenges/poller.py:92-94`), started from a setup solver (`_challenges/setup.py:212`). No other satellite calls it.
- **Epoch's branch:** only through `deepagent(background=64)`, inside their member wrapper.

So every real caller is either deepagent or a solver-level worker meant to live as long as the sample. Solver-level workers run outside any owner and keep today's behaviour.

### The swarm today

- `member()` refuses a built `deepagent` whose params, or a `deepagent` factory whose `args`, set `background` to anything but `False` (`src/inspect_swarm/_member.py:281-282`, `:295-296`, `:299-304`), and its docstring says so (`:163`, `:178`).
- On the M1-4 branch, the member wrapper `run_member()` sets the member ContextVar, enters `tool_state_scope(name)` and the member's cancel scope, builds the agent, and calls `run(agent, input, limits=nodes, name=name)`, where `nodes` are the member's lever and limits (`_member.py:291-324`, at `ede24f8`). An error is split into cap, owned and foreign leaves by `classify_errors()` (`:338-356`): a cap stops the swarm, an owned leaf gives a status, a foreign one is raised. A cancelled member scope gives `stop_cause` or `cancelled`.
- `SwarmRun.stop()` closes the lever of every running member and the runtime lever (`_controller/_control.py:255-268`); `_supervise()` waits for running members up to the effective grace, cancels the scopes of those still running, and waits again (`:323-347`); `_grace_now()` clips grace to half the sample's remaining time (`:349-354`). `_run_member()` calls `_end()` in its `finally`, which sets the state `ended` and the status together and queues `MemberEnded` (`:300-317`). The caps are entered once around both task groups (`:237-247`).
- `Lever.close(reason)` sets the lever's token limit to its usage, so the next pre-dispatch check raises, with the message "Stopped by the swarm: <reason>" (`_budget.py:418-422`, `:288-291`, at `ede24f8`).

## Design

### At a glance

```
sample task group (unchanged; background() outside any owner lands here)
└── swarm runtime: caps entered once
    └── member task
        tool_state_scope(name), member cancel scope
        └── background_owner(contain=caps)        ← new, one per member
            ├── run(agent, limits=[lever, limits]) ← the member's agent (top loop)
            └── tasks background() starts anywhere in the member:
                deepagent children, custom workers
```

1. The member's agent returns (or ends on its own limits, the lever or a cap). Its status, output and time are recorded, and the provisional answer is updated.
2. Drain: close the member's lever, wait for the owner's tasks up to the effective grace, leave the owner (cancelling what is left).
3. The member ends: state `ended`, `MemberEnded`.

### The owner (AI-5, inspect_ai)

```python
# src/inspect_ai/util/_background.py, both exported from inspect_ai.util

class BackgroundOwner:
    """The background work started inside one `background_owner()` block."""

    @property
    def started(self) -> int:
        """How many tasks `background()` has started in this owner."""

    @property
    def running(self) -> int:
        """How many of them have not finished."""

    @property
    def cancelled(self) -> int:
        """How many were still running when the block ended and were cancelled by the owner. 0 until the block ends."""

    async def wait(self) -> None:
        """Return once no task started in this owner is running, including tasks started while waiting. Returns at once when none is."""


@asynccontextmanager
async def background_owner(
    *,
    contain: Sequence[Limit] = (),
    on_limit: Callable[[LimitExceededError], None] | None = None,
) -> AsyncIterator[BackgroundOwner]:
    """Own the background work started inside the block.

    Inside the block, `background()` runs its function in this owner instead of the sample's task group. When the block ends, the owner cancels the tasks still running and waits for them to finish.

    Args:
        contain: Limits entered outside the block whose errors, raised in one of the owner's tasks, end only that task, as the errors of limits entered inside the block do.
        on_limit: Called with each limit error that ends only its task, synchronously, in that task, as it ends.
    """
```

**Attachment.** Entering the block sets a ContextVar to the owner and leaving it resets it. `background()` reads it first: if an owner is set, it starts the wrapped function (the same logging wrapper as today) in that owner's task group with the caller's context, and does not require an active sample. If none is set, it runs today's code unchanged. So:

- The nearest enclosing owner wins. A task started in an owner inherits the ContextVar, so `background()` called from inside an owner's task, or from any task that inherits the context of the block (a tool call running in a task group inside the agent), attaches to the same owner.
- An inner owner's block ends only its own tasks; the outer owner is unaffected and sees the inner block as ordinary code.
- An owner object is entered once; entering it again raises `RuntimeError`.
- `background()` in a context whose nearest owner has already exited (a context saved and reused after the block) raises `RuntimeError` saying the background owner has exited, instead of starting work nothing owns. Tasks started while the owner is ending (from a task's cancellation handler) are started cancelled, as anyio does.

**Limits and errors.** When the block is entered, the owner records the limit nodes active in the entering task: the leaf of each limit tree and its ancestors. A task can end in four ways:

1. It returns, or is cancelled (by the owner, an enclosing cancel scope, or code of its own such as deepagent's `agent_cancel`). Neither is an error.
2. It raises a bare `LimitExceededError` whose `source` was entered inside the block (any node that is not among the recorded ones) or is one of `contain`. The task ends; the other tasks and the block's own code continue. If `on_limit` was given, it is called with the error, synchronously, in the ending task, so the owner's creator hears of the limit at once rather than when the block's code next checks. The error is not raised again. An exception from `on_limit` is treated as the task's exception under 3.
3. It raises any other exception, including a `LimitExceededError` from a limit entered outside the block and not contained, one whose `source` is `None`, and an exception group. This cancels the block's code and the other tasks and is raised from the block, as an exception in a task group is, and as an exception in the sample's task group ends the sample today.
4. The block's own code raises. The tasks are cancelled and the exception is raised from the block.

The rule in 2 is the subagent rule generalised. A subagent's own limits end the subagent and not its parent (`apply_limits(..., catch_errors=True)` in `_run_background`); a limit entered inside the block bounds work the block contains, so reaching it ends that piece of work. A limit from outside bounds more than the block (the sample's limits, a caller's `run(..., limits=...)` around the owner), so it propagates as today. The child's limits are its caller's, so the code inside the block reaches the shared ones too: token, cost and turn nodes count every call in the tree, and the block's code reaches them at its next check, which is how a synchronous subagent's limit error reaches its parent through the tool result. A message limit is different: it checks the conversation of the loop that calls (`check_message_limit(count, ...)`, `src/inspect_ai/util/_limit.py:744-758`, `:1328-1375`), with no shared counter, so a child whose longer conversation reaches it ends alone and its parent's next check, on its own shorter conversation, can pass. That is the same as for a synchronous subagent today. `contain` exists for limits the owner's creator entered further out but wants treated the same way; the swarm passes its caps ([Entering the owner](#entering-the-owner)).

The block's code does not always reach the limit soon, though: it may be waiting on another task. deepagent's `agent_wait(mode="all")` waits for every selected future in turn (`src/inspect_ai/agent/_deepagent/lifecycle_tools.py:307-310`), so a parent waiting on one child stuck in a long tool call does not return when a sibling is stopped by a limit. `on_limit` is how a creator that must act on the limit at once, as the swarm must on a cap, is told without waiting for its code to check.

**Ending the block.** However the block ends, the owner cancels the tasks still running (recording how many in `cancelled`) and waits for them, so their `finally` blocks and cancellation handlers run before the block is left. There is no deadline on that wait: a task's cleanup is bounded by the task's own code, as in any anyio task group, and a deadline would leave work running outside any structure, which is what the owner exists to prevent. A caller who wants tasks to finish on their own first calls `wait()`, bounded by `anyio.move_on_after()`, before leaving the block; the swarm's drain does this. A caller's deadline therefore bounds how long tasks may keep working, not their cancellation cleanup: a task that shields its cleanup from cancellation holds the block until that cleanup ends, as a bridged agent's shielded proxy shutdown holds the swarm's hard stop today ([swarm-limits.md Stopping](swarm-limits.md#stopping)). The owner's tests pin that cleanup is awaited, and the swarm's docs say grace does not bound it.

Then it raises, as `owned_task_group()` does today: a single exception bare (the group the task group wraps it in is undone), several as an `ExceptionGroup`, and a cancellation from outside re-raised as that cancellation. Errors handled under 2 are never raised; `on_limit` is the only way they are reported.

### deepagent's dispatch (AI-5)

- `_dispatch_background()` always starts `_run_background()` with `background()`. The registry's own task group and its branch (`agent_tool.py:681-684`) go.
- `BackgroundRegistry.owned_task_group()` enters `background_owner()` when no owner encloses it and the sample's task group is not live (the scorer case of inspect_ai#5718); otherwise it does nothing. A private `_current_background_owner()` in `_background.py` answers the first question.
- `_run_background()` is unchanged. Its outer-limit branch now meets one of three cases: outside any owner the error reaches the sample's task group, as today; in deepagent's own scorer-path owner the only limits entered inside the block are the children's own, which `apply_limits` has already caught, so outer limits propagate as they do from the registry's task group today; under an enclosing owner such as a swarm member's, a limit entered inside it or contained by it ends the child alone (rule 2), and the future says `cancelled`.

So a deepagent under an enclosing owner does not drain its own children when it returns: they belong to the enclosing owner and run until its block ends. Outside any owner nothing changes.

Docs: `docs/agent-custom.qmd` "Background Work" gains a paragraph on `background_owner()`; `docs/deepagent.qmd` "Background Dispatch" says children run until the sample ends, or until the enclosing background owner's block ends; `docs/reference/inspect_ai.util.qmd` lists `background_owner` and `BackgroundOwner` after `background`.

### Entering the owner

The member wrapper enters the owner inside the member's tool-state scope and cancel scope, and runs the member's agent inside it:

```python
# src/inspect_swarm/_member.py, run_member(), M2-5 (on M1-4's structure)
token = current_member.set(context)
try:
    with tool_state_scope(name), context.scope:
        async with background_owner(contain=caps, on_limit=on_child_limit) as owner:
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

- **Placement.** Inside `context.scope`, so the hard stop cancels the children with the member. Inside `tool_state_scope(name)` and the member ContextVar, so children keep the member's tool state and identity (they would anyway, by inheriting the context at dispatch). Around `run()`, so the member's nodes (lever, share, member limits) are entered inside the block, and a child that reaches one ends alone (rule 2). The swarm's caps are entered outside, once for the whole swarm, so they are passed as `contain`. Sample limits are neither, so a child that reaches one ends the sample at once, as it would outside a swarm.
- **One owner per member, every member.** A member with no background work pays a ContextVar set and an empty task group. Bridged members never call `background()`.
- **`on_child_limit`** is the wrapper's callback for rule 2: when the error's source is one of the swarm's caps it calls `stop("swarm_cap")` (a no-op once a stop has begun; `stop()` is synchronous up to its grace, `_control.py:255-268` at `ede24f8`), and it ignores every other source (the member's lever, share and limits, and limits the member's agent entered itself), which stop only the child. So a cap reached by any child starts the swarm's soft stop at once, whatever the member's agent is doing.
- **Background work is started by the agent, not while it is built.** A factory-form member's agent is built inside the block, but before `run()` enters the member's nodes and opens its span (`src/inspect_ai/agent/_run.py:97-101`). A task started then would keep the context of its dispatch: the caps but not the member's lever, share or limits, and no member span, so the drain could not stop its next call and the member's call counts would miss it (a probe on asyncio and trio metered such a task's tokens to the cap and none to the member node, even when the task called the model after the node was entered). No factory does this: deepagent, `react()` and Epoch's builder start background work only from the running agent ([Who calls `background()`](#who-calls-background)). So the wrapper refuses it: if `owner.started` is not 0 after `build_member()`, it raises `RuntimeError` naming the member and saying that background work must be started by the agent when it runs, not by its factory. The owner's exit cancels the task; the error is foreign, so the member is `errored` and the error propagates, as for any factory error (M1-4).

### The drain at member end

When the member's agent has returned (normally, or on the lever, its share, a member limit or a cap), the wrapper drains the owner:

1. Close the member's lever with the reason `member_ended`: every child's next model call is refused, with "Stopped by the swarm: member_ended". If a stop already closed it, the lever keeps that reason. Only children can still make calls in the member's context: its own loop has returned.
2. Wait for the owner's tasks with `anyio.move_on_after(grace())`, where `grace()` is `SwarmRun._grace_now()`: `Budget.grace`, clipped to half the sample's remaining time. Children finish the calls and tool calls they have in flight, then reach the lever at their next check and end alone (rule 2).
3. Leave the block. The owner cancels the children still running, waits for them, and records the count in `owner.cancelled`.

Why a soft drain rather than cancelling at once: it is the soft stop's reason ([swarm-limits.md Stopping](swarm-limits.md#stopping)). Calls in flight finish and are recorded, so the member's spend stays known. The throwaway prototype of the owner (patched in for `background()` around today's deepagent, not committed) showed it: a top loop dispatched two children that each started a 0.5 s tool call, then submitted; after the lever closed, both children finished their tool calls, were refused their next call and ended alone, with `cancelled=0`, two lever errors held by the owner, and no errored model event. With a 0.1 s grace the owner cancelled both. With the lever entered outside the block, the first child's error ended the block and was recorded as the sample's limit, as rule 3 says.

Why stop the children at member end at all: the swarm owns and drains all of a member's work ([swarm.md Goals](swarm.md#goals)), and the plan asks for the drain on member end ([D-2](swarm-implementation-plan.md#designs)). A member's children work on the member's attempt; once it has submitted or been stopped, their further spend serves nothing the swarm records. Epoch's members leave theirs running until the solver ends, which the Lean analysis lists as a cost they pay today ([Observations for Epoch](swarm-lean-open-problems.md#not-this-design)). The port keeps the arm, background deepagents with up to 64 children, and drops that waste.

`Budget.grace` is reused rather than adding a separate drain setting: it already means "how long work in flight gets to finish once it may not start more", and the drain is that rule applied to one member.

### At the swarm's stop

Nothing new is needed; the drain composes with [Stopping](swarm-limits.md#stopping):

- **Soft stop.** `stop()` closes the levers of running members. A member that is draining is still running, so its lever is closed already (first reason wins) and its children are already being refused. In a member whose agent is still running, the agent and its children reach the closed lever at their next calls; the children end alone, the agent returns `stopped`, and the drain then waits for children with calls in flight.
- **Grace.** `_supervise()` waits for running members, and a draining member is running, so grace covers the children. A member's drain wait and the swarm's grace run side by side: a drain that began before the stop may end first, and one that has not ended when the swarm's grace does is cut short by the hard stop.
- **Hard stop.** `_supervise()` cancels the scope of every member still running. The owner is inside that scope, so its children are cancelled with the member's agent, or with its drain. A member whose status was already decided keeps it (below); its ledger line says `cancelled`.
- **Time cap.** It calls `stop("swarm_time")`, so it is the soft stop above.
- **A cap reached inside a child.** The cap is in `contain`, so the child ends alone and its siblings' calls in flight finish. The owner calls the wrapper's `on_child_limit` in the child as it ends, which calls `stop("swarm_cap")` at once: the soft stop closes every running member's lever and grace starts, exactly as when a member's own agent reaches the cap. This does not wait for the member's agent, which may be blocked in `agent_wait(mode="all")` on a sibling in a long tool call, or may already have returned and be draining. That sibling finishes its tool call within grace, is refused its next call, `agent_wait` returns, and the agent reaches the cap at its next call (status `swarm_cap`); the hard stop bounds it if not. Without `contain`, the first child to call the model after the cap was reached (cap checks run root to leaf, ahead of the lever) would cancel every sibling's call in flight at the moment the whole swarm is being soft-stopped.

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
| A child reaches a swarm cap (reported by `on_limit`) | `stop("swarm_cap")` if no stop has begun; status unchanged |
| The swarm's hard stop cancels the member scope | Status unchanged; ledger `cancelled` |
| A child raises a foreign error (a sample limit, `TerminateSampleError`, a refusal under `fail_on_refusal`, a sentinel failure, a custom worker's crash) | Status unchanged; the owner cancels the other children, and the error is raised without the swarm's leaves, so the swarm ends as for any foreign error. The ledger says `interrupted` |

Why a foreign error in the drain keeps the submission: the submission was complete before the error, and the swarm already treats an outer limit that ends the sample early this way. The provisional answer is on the `AgentState` the swarm was passed, which `as_solver()` copies to the sample's output even when an exception ends the agent (`src/inspect_ai/agent/_as_solver.py:65-80`), and the record says `provisional: true` ([swarm-scoring.md](swarm-scoring.md#the-final-answer-first)). Turning the member `errored` instead would leave a provisional `FinalRecord.member` and output naming a member whose record no longer says `submitted`, or would need the runtime to re-select before the error propagates, with a different answer only for members that had background children. So `MemberHandle.status`, `MemberResult.status` and `submitted`, `FinalRecord.member` and the passed `AgentState.output` all keep what the agent's return set, and the error then propagates as M1-7 specifies for foreign errors, with the ledger and the record written in the runtime's synchronous `finally`. A sample limit is then the sample's limit and its output the provisional answer; any other foreign error fails the sample, as the same error does from a member's agent. Only a member whose status was not yet decided (its agent still running) becomes `errored`.

Before the drain (while the agent runs) the same events are handled as M1-4 handles errors from the agent: an own-node limit reached by a child ends the child, and for the lever, the share and token, cost or turn limits the agent reaches the same node at its next check (a member message limit, per conversation, ends only the child); a cap reached by a child ends the child and stops the swarm at once through `on_limit`; a foreign error from a child cancels the agent and the other children and makes the member `errored`.

**Errors from children that do not reach the owner.** deepagent captures a child's ordinary exception on its future (`errored`) and the parent reads it with `agent_status` or `agent_wait`. It does not affect the member's status; the member's agent decides what to do with it, as it decides about a failed tool call.

**Stop reasons** are unchanged. A cap reached in a child is `swarm_cap`; nothing a child does is a new reason.

### Budget, member limits and the ledger

Children run in the member's context, so every node they reach is reached by their calls as by the member's own (Spike 1):

- **Swarm caps and the member's share** meter children's calls. Overshoot stays bounded by connection slots, which children hold like any caller, plus one native compaction per agent loop, which [swarm-limits.md](swarm-limits.md#the-final-step-and-the-reserve) already counts per member or subagent. With 64 children a member can hold many slots; the bound counts slots, not members.
- **Member limits** (token, cost, turn, message) meter children as they meter a member's synchronous subagents. Token, cost and turn limits are member-wide: children's calls count towards them, and once one is reached the member's agent ends on it at its next call. A member message limit bounds each loop's own conversation, so a child that reaches it ends alone and the member can still go on and submit, as with a synchronous subagent; the swarm adds no member-wide message count. A member's time limit is an Inspect `time_limit` entered by `run()`; it cancels the member's agent only, and the drain then stops the children within the effective grace. `member()` documents that a member's time limit bounds its agent and its children's drain, not its children's lifetime.
- **The lever** records every child's usage, so the member's ledger line includes its children's spend. A child's span is a descendant of the member's span (Spike 1), so the member's call counts include its children's model events.

`MemberLedger` ([swarm-limits.md The ledger](swarm-limits.md#the-ledger)) gains two fields, from the member's owner:

```python
class MemberLedger(BaseModel):
    ...
    background_started: int     # tasks background() attached to the member's owner
    background_cancelled: int    # of those, still running when the drain gave up, cancelled by the owner
```

The wrapper copies `owner.started` and `owner.cancelled` to the member's run record (M1-4's `MemberRun`) after the block, on every path, and the ledger reads them there. They count the tasks attached to the member's own owner. If the member's agent enters an owner of its own, the tasks attached there belong to that owner, which ends them before the agent returns, so nothing outlives the member; they are not in these counts, and a cancellation by that inner owner is not seen by the ledger. No supported shape nests an owner inside a member: deepagent enters one only when no owner encloses it. `member()` documents that `background_cancelled` and the completeness check below cover the member's own owner only. `total_complete` additionally requires `background_cancelled == 0` for every member (a claim about the member's owner, as above), for the same reason it requires no member `cancelled`: a cancelled agent loop may have had work in flight that left no trace. Calls a cancelled child had in flight are counted as `errored`, as anywhere. A child the model cancelled with `agent_cancel` is its own decision; its in-flight call, if any, is an errored event and already clears `total_complete`. Evidence of which children ran is their spans; the swarm adds no event of its own.

### Lifting the rejection

M2-5 deletes `_check_background()` and its two calls. `deepagent(background=True)`, `deepagent(background=64)` and `member(deepagent, args={"background": 64})` are accepted, and so is any agent, wrapped or custom, that calls `background()` while it runs: the owner catches them all, so nothing depends on reading registry params. A factory that calls `background()` while building the agent is refused when the member starts ([Entering the owner](#entering-the-owner)). `member()`'s docstring replaces the refusal sentence with the contract:

> Background work belongs to the member. Work its agent starts with `background()`, such as `deepagent(background=...)` subagents, spends through the member's limits and share, and is the member's in the ledger. When the member's agent returns, its status is decided; the swarm then stops the background work from starting model calls, gives it the swarm's grace period to finish calls in flight, and cancels what is left. The member ends after that. Background work must be started by the agent while it runs; a factory that starts it while building the agent makes the member `errored`. Work started outside `background()` (an `asyncio` task, a process left running in the sandbox) is not stopped.

### The shared-memory channel

A background `general()` child reaches the board exactly as a synchronous one does ([swarm-memory-channel.md Descendants](swarm-memory-channel.md#descendants)):

- It inherits the member ContextVar at dispatch, so it is the member: its writes are records whose `sender` is the member.
- deepagent hands `general()` the parent's tools, including `swarm_tools()`. The child resolves it in its own agent channel, which is not the member's bound loop (the member's loop resolved first, before it could dispatch), so `Channel.reach = "member"` gives it the `shared_memory` tool and not the bound loop's notices or `"loop"`-reach tools.
- Its writes carry its own agent span as `sender_span_id` and `nested: true`; its views are `read` events in its own tool spans.
- It shares the member's `rate`: one member's 64 children cannot multiply its write volume.
- **During the drain** the member is still running, so the bus accepts the writes and reads of a child finishing a tool call. After a stop, `bus.close()` at step 0 refuses them as it refuses everyone's. A child never writes after its member has ended, because the owner has ended it by then; without the owner, a child outliving its member would have met the bus's sender-liveness check.

The test that a background `general()` child reads and writes the board as the member lands with whichever of M2-5 and M2-8 merges second ([Testing](#testing)).

### What depends on how M1-4 lands

M1-4 is on its branch at `ede24f8`. This design depends on:

- the wrapper's structure: member ContextVar, then `tool_state_scope(name)` and `context.scope`, then `run(agent, input, limits=nodes)` with the lever among `nodes`, and `classify_errors()` (the owner goes between the scopes and `run()`);
- `stop()` closing the levers of running members, `_supervise()` waiting for and cancelling running members, and `_grace_now()`;
- `_end()` setting the state and the status together. M2-5 splits it: the status, output and time are recorded when the agent returns, the state `ended` and `MemberEnded` when the drain ends.

If M1-4 merges with a different shape, M2-5 keeps the same three placements: the owner inside the member's cancel scope and tool-state scope, the member's nodes entered inside the owner, and the caps passed to it as `contain`. M1-5's record, which M2-5 also changes, is not built yet ([Changes to other designs and the plan](#changes-to-other-designs-and-the-plan)).

## Alternatives considered

**An owner with plain task-group semantics**, where any exception from a task, a limit error included, cancels the block and its other tasks. Simplest, and it is what `owned_task_group()` does. But the swarm stops members by limits: the lever at a soft stop and at the drain, a reached cap, a member limit. Under plain semantics the first child refused by the lever cancels every sibling's call in flight, so every stop and every member end would turn a member's in-flight children into unknown spend. With 32 members and up to 64 children each, that is most of the calls in flight at the cap. The prototype's case with the lever entered outside the block showed it: the first child refused ended the whole block.

**Hold every `LimitExceededError` in the owner**, from limits outside the block too. No `contain` parameter is needed, and caps are handled. But a sample limit reached in a child would then stop only that child, and the sample would end only when the block's code reached the limit or the block ended. For shared token, cost and turn nodes that is the next call; for a sample message limit, which a child's own conversation can reach without its parent's, it is the end of the member. Rule 2 keeps outside limits propagating as today, and `contain` names the outside limits a creator wants treated otherwise.

**Containment in deepagent instead of the owner.** `_run_background()` could end the child alone for some limit errors. It does not know which limits are the member's and which the sample's, and custom agents that call `background()` would not get it. The owner is where the boundary is.

**Cancel children at member end, with no grace.** Simpler drain, a member ends at once. It loses every call in flight at each member's end and makes `total_complete` false for nearly every background arm.

**Let children run until the swarm stops**, so a member is "ended" while its children run, and drain only at the swarm's stop. This keeps Epoch's current behaviour exactly. But it breaks the guarantee that `ended` means no more spend from the member, makes `all_done` wait on children no member can see, and keeps spending on attempts that have already submitted or been stopped. The plan asks for the drain at member end.

**Wait for children to finish without closing the lever**, up to a deadline. Children would keep starting new calls on an attempt that is over, so the drain's length would be the deadline every time.

**Reporting contained limit errors when the block ends**, as a list the creator reads afterwards, instead of `on_limit`. Simpler, but the creator learns of a cap only when its own code next checks or the block ends, and a deepagent parent waiting in `agent_wait(mode="all")` on a sibling stuck in a long tool call does neither: the swarm's stop would wait on that tool call, and with no time cap indefinitely. An event or stream the creator awaits would need a task of its own in every member to watch it; a synchronous callback in the ending task needs nothing.

**An owner that drains with a deadline on exit.** It would leave tasks running after the block, outside any structure. The deadline belongs to the caller, with `wait()` and `move_on_after()`.

**Owning `asyncio` tasks and `run_in_background()` too.** No member uses them, and `run_in_background()` serves eval-level machinery that must outlive any sample.

## Compatibility and migration

**inspect_ai, outside an owner: no change.** `background()` reads one ContextVar and, finding none, runs today's code: same checks, same logging, same task group, same errors. Every caller in [Who calls `background()`](#who-calls-background) runs outside any owner: solver-level workers in the docs and tests, inspect_ranges' poller, and deepagent with a live sample task group. The working-limit monitor and the hooks loop do not use `background()`.

**deepagent.** With a live sample task group and no enclosing owner: unchanged, children are abandoned until sample teardown (`TestAbandonOnExit`, `test_solver_child_outlives_parent_until_sample_teardown`). Without a live sample task group (scorers): the registry's task group is replaced by `background_owner()`, with the same cancel-and-wait on exit and the same unwrapping; the children's own limits are caught before they reach it and outer limits propagate, so `TestNoLiveSampleTaskGroup` passes unchanged. Under an enclosing owner (new): children belong to it.

**inspect_ai API.** Two new public names, `background_owner` and `BackgroundOwner`, in `inspect_ai.util`. No log, schema, event or viewer type changes; nothing in ts-mono.

**inspect_swarm.** No released API until M2-12. Accepted members widen (background deepagents, agents that call `background()`). `MemberLedger` gains two fields under its existing `version: 1`, and `MemberResult.time` becomes the time the member's agent returned; neither has shipped. `MemberHandle.status` can be set while `state` is still `running`. `pyproject.toml`'s `inspect-ai` floor rises to the first release containing AI-5, since `src/` imports `background_owner` (`_member.py`).

**Epoch's port.** Their builder keeps `background=64`; their member wrapper needs no change, because the owner does not depend on recognising deepagent. Their pinned inspect_ai (0.3.273) must move to a release with AI-1, AI-2 and AI-5, as the plan already says (M2-12).

## Security

What reaches the new code, and what it changes about who can outlive a member:

- **Model output decides how much background work there is.** A member's model chooses when to dispatch children, up to deepagent's `max_background`, and what they do. The owner bounds their lifetime to the member's, the lever and caps bound their spend, the effective grace bounds how long a drain lasts, and the hard stop and the sample's own time limit bound it absolutely. Before this design, the only bound on a child's lifetime was the sample's.
- **A model cannot make work outlive its member through `background()`.** Attachment is by ContextVar in the member's task tree. No tool or model output names an owner, and a child cannot reach another member's owner, since member tasks are siblings started from the runtime's saved context.
- **What escapes the owner.** Raw `asyncio` tasks started by agent or tool code, and processes a model leaves running in the sandbox (`nohup`, `&`), are not owned. Agent and tool code is the eval author's, trusted as in any Inspect eval; a sandbox process can keep changing a shared or per-member sandbox after its member ended, as [swarm-lean-open-problems.md](swarm-lean-open-problems.md#8-after-m2-per-member-sandboxes-in-the-scoring-contract) notes for scoring. `member()` documents both.
- **Exception text** from children, which can contain model-influenced strings, is logged as today (`background()`'s wrapper, deepagent's warning) and, for foreign errors, raised as today. The owner adds no text of its own beyond the two `RuntimeError` messages, which contain no input.
- **The shared-memory channel** gains no new writer: background children were already designed in as the member's descendants ([swarm-memory-channel.md Security](swarm-memory-channel.md#security)), and the owner only guarantees that none writes after its member ended.

## Testing

### inspect_ai (AI-5)

Async tests on the suite's anyio backends, mockllm where a model is needed, no network or Docker.

`tests/util/test_background.py`:

- Outside an owner: the existing tests pass unchanged, and `background()` outside a sample still raises.
- Inside an owner, without a sample: a task runs in the owner; leaving the block cancels a running task, runs its `finally` before the block is left, and sets `cancelled`; `started` and `running` count; `wait()` returns when the tasks finish, including one started while waiting, and at once with none.
- Nesting: the nearest owner wins; an inner block ends only its tasks; `background()` called inside an owner's task attaches to that owner.
- Errors: a task's `RuntimeError` cancels the block's code and a sibling and is raised bare; two task errors are an `ExceptionGroup`; the block's own exception cancels the tasks and is raised bare; an outside cancellation is re-raised as a cancellation (the cases of `TestNoLiveSampleTaskGroup`, at the owner level).
- Limits (table-driven): a task that reaches a `token_limit` entered inside the block ends alone, a sibling keeps running, the block's code continues, and the error is passed to `on_limit`, in the ending task and before the block's code resumes, and is not raised; the same limit entered outside the block is raised and cancels the sibling; entered outside but in `contain`, it ends the task alone; a `LimitExceededError` with `source=None` is raised; an exception from `on_limit` is raised from the block as the task's.
- Entering an owner twice raises; `background()` in a context saved inside a finished block raises `RuntimeError`.
- Inside an eval: `background()` in a solver inside an owner leaves no task on the sample's task group after the block.

`tests/agent/deepagent/test_deepagent_background.py`:

- Under an enclosing owner, a deepagent that submits while a blocking child runs returns with the child still running; the child is cancelled when the owner's block ends, not at sample teardown, and the deepagent entered no owner of its own.
- Spike 2 inside an owner: `run(deepagent(background=True), limits=[token_limit(n)])` inside the block, with children that reach the limit, ends those children alone, the parent stops at its next call with the limit returned by `run()`, and no sample limit is recorded.
- `TestAbandonOnExit`, `test_solver_child_outlives_parent_until_sample_teardown` and `TestNoLiveSampleTaskGroup` pass unchanged.

### inspect_swarm (M2-5)

Runtime tests on mockllm with scripted outputs and controlled tool latency (anyio events and short sleeps, grace in tenths of a second), on asyncio and trio, no Docker. A new `tests/test_background_members.py`, plus changes to existing files:

- **Drain at member end** (the plan's required case, both outcomes): a `deepagent(background=True)` member submits while one child is in a tool call that finishes within grace and another in one that does not. The first finishes its call, is refused its next one and ends; the second is cancelled by the owner's exit and awaited before the member ends. The member's status is `submitted`; `MemberEnded` comes after both children's spans have ended; its ledger line's usage includes both children's completed calls; `background_started == 2`, `background_cancelled == 1`, `total_complete` is false. With grace long enough for both, `background_cancelled == 0` and no model event is errored.
- **Soft stop** (the controller's `all_done`, and the time cap): children with calls in flight finish them, none is cancelled, every call is recorded.
- **Cap reached in a child** (token and cost caps, table-driven): siblings' calls in flight finish (no errored events); the stop reason is `swarm_cap`; the member is `swarm_cap` when its agent then reaches the cap, and keeps `submitted` when the cap was reached only during its drain.
- **Cap reached in a child while the parent is blocked**: the member's agent waits in `agent_wait(mode="all")` on two children; one reaches the cap, the other is in a tool call longer than the test's patience. `stop("swarm_cap")` is called when the first child ends (the stopped evidence and other members' levers show it at once, not after the second child), the second child finishes its tool call within grace and is refused its next call, the agent ends `swarm_cap`, and no call in flight is cancelled. With the second child's tool call longer than grace, the hard stop cancels it and the run ends within grace.
- **Member limit reached in a child** (a member token limit): the child ends alone, the member ends `member_limit:token`, other members continue.
- **Member message limit reached in a child**: a child whose conversation passes the member's message limit ends alone; the member's agent, with a shorter conversation, keeps going and submits (`submitted`), and other children are unaffected.
- **Sample limit reached in a child while the member's agent runs**: recorded as the sample's limit, not a swarm cap, the member `errored`, as for the same deepagent outside a swarm.
- **Hard stop during a drain**: a child stuck past the swarm's grace is cancelled; the member keeps `submitted` and its ledger line says `cancelled`.
- **Foreign error during the drain of the chosen member** (a sample token limit reached by a child's in-flight call, and a custom worker's `RuntimeError`): member A submitted first and is the provisional answer. Without another submission, and with member B having submitted after A, the sample's output is A's, `FinalRecord.member` is A with `provisional: true`, A's `MemberResult` still says `submitted`, and the ledger says `interrupted`; the sample limit is recorded as the sample's, and the `RuntimeError` fails the sample. With B having submitted before A, B stays the answer.
- **`first` across a drain**: member A submits at t1 and drains until t3, member B submits and ends at t2 (t1 < t2 < t3); the provisional answer is A's from t1 and the final answer is A's; A's `MemberResult.time` is t1.
- **Isolation**: two background members' drains do not wait for each other's children; a child's tool state is its member's (`memory()` default instance), with `tests/test_tool_state.py`.
- **A custom agent that calls `background()` directly**: its task is cancelled at member end, not at sample end, and counted in `background_started`.
- **Construction-time background work is refused**: a factory that calls `background()` while building its agent makes the member `errored` with the `RuntimeError` naming the member, and the task it started is cancelled before the member ends, on asyncio and trio.
- **Construction** (`tests/test_member.py`): the four rejection cases become acceptance cases, and the logged member dict with `background` round-trips in both forms.
- **Shared memory** (with whichever of M2-5 and M2-8 merges second, in `tests/test_shared_memory.py`): a background `general()` child writes and views a file; the write's `sender` is the member, its `sender_span_id` the child's agent span, `nested: true`; the member's other loops see it as the member's.

## Implementation plan

1. **AI-5 (inspect_ai), one pull request.** `background_owner()`, `BackgroundOwner` and the ContextVar in `src/inspect_ai/util/_background.py`, with `background()` reading it first; the exports in `src/inspect_ai/util/__init__.py`; deepagent's dispatch and `owned_task_group()` in `src/inspect_ai/agent/_deepagent/agent_tool.py` (and its docstrings); `docs/agent-custom.qmd`, `docs/deepagent.qmd`, `docs/reference/inspect_ai.util.qmd`. Tests as above in `tests/util/test_background.py` and `tests/agent/deepagent/test_deepagent_background.py`. Titled `feat:`, since it adds public API. Depends on nothing.
2. **M2-5 (inspect_swarm), one pull request**, after a PyPI release of inspect_ai containing AI-5, and after M1-4, M1-5 and M1-6. `src/inspect_swarm/_member.py` (the owner in `run_member()`, the drain, `_check_background()` deleted, `member()`'s docstring); `src/inspect_swarm/_controller/_control.py` (the wrapper receives `self._caps` as before and `self._grace_now`; `_run_member()` records the decided status before the drain and calls `_end()` after it; `MemberHandle.status`'s docstring); the ledger module from M1-6 (`_budget.py` or `_ledger.py`) for the two fields and the completeness rule; the record module from M1-5 (`_record.py`) for when `MemberResult` is written; `pyproject.toml` (the floor) and `uv.lock`. `_swarm.py` changes only if M1-4 lands the wrapper there. Tests as above.
3. **The shared-memory background case** goes into whichever of M2-5 and M2-8 merges second.

## Changes to other designs and the plan

Follow-ups, not edits made here:

- **swarm-implementation-plan.md.** D-2: done, with this document's path, once merged. AI-5: files and tests as [Implementation plan](#implementation-plan) step 1, a `feat:` title. M2-5: files and tests as step 2 (adding `_controller/_control.py`, the ledger and record modules and `pyproject.toml`; `_swarm.py` only if the wrapper lives there), and "Depends on" gains M1-5 and an inspect_ai release containing AI-5. M2-8's sentence on the background-child test stands. Compatibility: "AI-5 changes nothing outside an owner scope, as D-2 must confirm" becomes confirmed, linking [Compatibility and migration](#compatibility-and-migration). Security: D-2's section is [Security](#security).
- **swarm.md** [Members](swarm.md#members): "Not supported in M1" says M2 supports members that use `background()`, through the member's owner, and links here; "Lifting the restriction" links here for the owner and the drain. Goals ("Until the swarm can do that ...") and the Testing bullet that rejects `background` in `args` say M1, and M2-5 replaces that test with the acceptance cases here. The current-behaviour section on deepagent's background subagents stays as a description of inspect_ai outside an owner.
- **swarm-api.md** [Members](swarm-api.md#members): the rejection paragraph says M2-5 removes it and links here; Testing's `test_member.py` rejection cases are replaced by acceptance cases in M2-5. `MemberHandle.status` is "how the member's agent ended, once it has", which can precede `state == "ended"` for a member draining background work.
- **swarm-limits.md.** [Stopping](swarm-limits.md#stopping): a member's background work is drained at member end by the procedure here, and the soft stop, grace and hard stop reach children through the member's owner; the member wrapper sketch gains the owner, the construction refusal, the drain and the decided-then-ended split; the cap trigger adds a cap reached in a background child, reported by the owner's `on_limit`. [The ledger](swarm-limits.md#the-ledger): `background_started` and `background_cancelled`, the extra `total_complete` condition, and "background children once they are supported" becomes "background children". [Interactions](swarm-limits.md#interactions): the background-subagents bullet links here and adds that caps are contained in each member's owner.
- **swarm-scoring.md** [What the swarm records](swarm-scoring.md#what-the-swarm-records): `MemberResult.time` is the swarm time the member's agent returned, and a member's result (status, output, time) is written then, with its state `ended` written after the drain. A decided status is final: a foreign error during a member's drain leaves its submission, the provisional `FinalRecord` and the output as they were, and propagates with `provisional: true` ([Statuses, ended, and the record](#statuses-ended-and-the-record)).
- **swarm-lean-open-problems.md** [Recommendation 3](swarm-lean-open-problems.md#3-m2-background-deepagent-members): Change links here. The port sketch's comment that `"deep"` must not set `background` before Rec. 3, "What it cannot do yet", and the line under [Could inspect_swarm express their experiment?](swarm-lean-open-problems.md#could-inspect_swarm-express-their-experiment) that needs a synchronous deepagent say the builder keeps `background=64` once M2-5 lands, with children stopped at their member's end.
- **swarm-memory-channel.md** [Descendants](swarm-memory-channel.md#descendants): no change of design; it can note that a background child's writes during its member's drain are accepted and that none follows the member's end.

## Open questions

1. **The public inspect_ai names.** `inspect_ai.util.background_owner()` returning a `BackgroundOwner`, with `contain=` and `on_limit=`. Recommendation: as named; "owner" is the word every swarm design uses, and the shape parallels `tool_state_scope()`. The names are upstream API, so they are Ransom's to settle before AI-5 opens.

## Not this design

Adjacent problems noticed and left out, for Ransom to file if wanted:

- **A scoped limit reached in a deepagent's background child ends the sample** (inspect_ai). Outside any owner, a limit from `run(deepagent(background=True), limits=[...])` reached in a child is recorded as the sample's limit (Spike 2), because `_run_background()` treats every outer limit as the sample's. Callers can avoid it with `background_owner()`; making deepagent enter one whenever its caller entered limits would change its abandon semantics, which is inspect_ai's call.
- **`background()` on a cancelled sample task group** (inspect_ai). In a scorer, `background()` starts work on a task group that is already cancelled, so the work is cancelled at once with no error. deepagent works around it (inspect_ai#5718); `background()` itself could refuse or point at `background_owner()`.
- **deepagent's status for a child ended by a limit** (inspect_ai). Such a child reads `cancelled`, with no message; showing the limit's message, as `errored` shows its error, would tell the parent why.
- **Background work started by a controller.** No controller calls `background()`; an owner around the controller's `run`, drained at the stop, would extend the guarantee to controllers.
- **Unowned concurrency in members.** Raw `asyncio` tasks and detached sandbox processes; a harness-validity check could report sandbox processes still running when a member ends.
