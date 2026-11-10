# Inspect Swarm: the turn seam

## Overview

The turn seam lets code outside a member stop it between the turns of its own agent loop and continue it later. A *turn boundary* comes before the loop's first generation and before each one after it, with no model or tool call of the loop in flight. Any code holding a member's handle registers a *boundary callback* with `on_boundary(callback, origin=...)`; at every boundary the member's callbacks run in the order they were registered, each receiving a `Boundary` (the member, the boundary's index and activation, what the conversation ends with, and copies of the conversation and the last output). A callback may wait as long as it likes, then continue the member unchanged, continue it with a replaced conversation, or end it. `end(origin=...)` ends a member from outside the chain, whichever callback is waiting.

While its callbacks run the member is *held*, and the swarm knows it: its member time limit does not run, `swarm_wait` does not count it as active, background work cannot start for it, and a swarm stop or `end()` ends it at once, `stopped`. A callback's own work counts towards the member's limits and the sample's working time; a callback marks its idle waits with `suspend_working_limit()`.

The boundary is a private hook in inspect_ai: a *turn gate* that every agent channel opened under it keeps and that `before_turn()` awaits. The member wrapper installs one gate per member run, so every `react()` and `deepagent()` member has the seam with nothing wired by its author, its first turn and the turns that recover from an overflow or an interrupt included. The gate binds the member's own top-level loop and lets nested loops pass. When a callback rewrites the conversation, the gate drops compaction summaries the rewrite made stale and react rebuilds its compaction state, so the model sees the rewrite. Every boundary where callbacks ran is a `boundary` event, and every change a callback made is a `changed` event with its origin and the ids and hashes of the messages, never their text.

Three features are built on the seam: persistent members are held idle at the boundary after each answer, an operator's pause is a callback and an operator's stop is `end()`, and ORBIT's scheduler and interventions are callbacks in ORBIT's own code. Members the seam cannot hold (bridged CLIs, members with background work) are refused with an error, and a member with callbacks that never reaches a boundary ends `errored`, so an unsupervised run never passes as a supervised one.

## Where it lives

The gate, the hold and the evidence's diff are `MemberGate` in [`_seam.py`](../src/inspect_swarm/_seam.py), with `Boundary` and `BoundaryCallback`; `on_boundary()`, `end()`, `held`, the stop's cancellation of chains and `end()`'s grace timer are on `MemberRun` and `SwarmRun` in [`_control.py`](../src/inspect_swarm/_controller/_control.py); installing the gate, `seam_refusal()`, the output taken from the loop's last state, end requests on a normal return and the never-reached rule are in [`_member.py`](../src/inspect_swarm/_member.py); `SwarmTimeNode` and the `turn_loop`, `held` and `operator_hold` fields of `MemberContext` in [`_budget.py`](../src/inspect_swarm/_budget.py); the held-member rule of the all-waiting check in [`_bus/_wait.py`](../src/inspect_swarm/_bus/_wait.py); the background owner's refusal in [`_background.py`](../src/inspect_swarm/_background.py); the events in [`_evidence.py`](../src/inspect_swarm/_evidence.py); the metrics in [`_metrics.py`](../src/inspect_swarm/_metrics.py); and the ledger's cancelled chains in [`_ledger.py`](../src/inspect_swarm/_ledger.py). In inspect_ai: the private module `inspect_ai.agent._channel.gate` (`TurnGate`, `turn_gate()`), `agent_channel()` and `AgentChannel.before_turn()` in `inspect_ai.agent._channel`, the compaction handler's `reset()` in `inspect_ai.model._compaction`, and the calls in both `react()` loops.

The user docs are [Controllers, Boundary callbacks](../docs/controllers.qmd#boundary-callbacks), the member time limit in [Members, Limits](../docs/members.qmd#limits), and the events and metrics in [Analysis, Boundary callbacks](../docs/analysis.qmd#boundary-callbacks). The features built on the seam have their own designs: [swarm-persistent-members.md](swarm-persistent-members.md), [swarm-operator-intervention.md](swarm-operator-intervention.md) and [swarm-orbit.md](swarm-orbit.md).

## Why

Persistent members, operator pause and stop, and ORBIT's scheduled mode all need to stop a running member between turns and continue it later, sometimes with a changed conversation. Much of that can be built in a caller's own code: a `react()` member's `on_continue` sees the state after each completed turn, can wait there, and can return a state that replaces the messages. What no caller can build on top is the swarm's knowledge that the member is held. A member parked inside its author's hook looks running to the runtime, so the swarm gets four things wrong:

- **Status.** A member let go by returning `False` after a tool-call turn returns normally with `stop_reason == "tool_calls"`, which the swarm reads as `submitted`.
- **Time.** Held time runs down the member's time limit.
- **Waits.** `swarm_wait`'s all-waiting release counts a held member as active, so the other members' waits run to their timeout.
- **Stop.** The swarm cannot release a held member: it waits out the grace period and cancels it.

And two things a caller cannot reach at all: the first turn and the recovery turns, which `on_continue` never sees, and a member whose author wired nothing, such as one an operator wants to pause. So the seam is one contract every caller uses, and the swarm owns only its knowledge of a held member.

## Goals and non-goals

The seam provides:

- One boundary callback contract: the member's state in; continue, replace the conversation, or end out; the member held while callbacks run.
- Every boundary of the member's own loop, its first and its recovery turns included, with no wiring by agent authors.
- The runtime's knowledge of a held member: status, time limits, `swarm_wait`'s all-waiting release, and release at a stop, of the swarm or of that member alone.
- A standard record of every change a callback makes.
- A clear error for every member the seam cannot hold.

It leaves out:

- **The callers' policy.** Scheduling, snapshots, attack input and defences are ORBIT's code; what a persistent member's answer is and how a wake continues it is [swarm-persistent-members.md](swarm-persistent-members.md)'s; the operator's surface is [swarm-operator-intervention.md](swarm-operator-intervention.md)'s.
- **What text may move.** Whether code at a boundary may read a member's text or add input in the user or system role is the caller's decision. Code that wraps a member could always do either; the seam records what it did.
- **Holding work outside the member's own loop**: its background work, a bridged CLI's turns, nested loops on their own.
- **Preemption.** A hold takes effect when the current turn ends. Cutting a turn short is an interrupt ([Interrupts](#interrupts)).

## The inspect_ai turn gate

The gate is a private seam for runtimes that run agents, as the private `background_target` seam serves the swarm's background owner; inspect_ai stays unaware of swarms.

- **`turn_gate(gate)`** sets a `ContextVar` for the block. **`agent_channel()`** reads it when it opens a channel and keeps it on the channel, so every loop opened in the block gets the same gate, nested loops included; the gate receives the channel and decides which loop it acts on. The inert channel outside any block never has a gate. When the channel's block exits, `agent_channel()` calls the gate's `closed(channel)` synchronously in its `finally`.
- **`AgentChannel.before_turn(messages, state=None)`** takes the loop's own `AgentState` as an optional keyword, which both react loops pass. When the channel has a gate it first awaits `gate.before_turn(channel, messages, state)`; then it drains the operator queue as it always does, so a message posted to a held member reaches the turn it was meant for. The gate changes `messages` in place itself, if at all, and returns `True` when it rewrote the conversation (removed, replaced, reordered or inserted messages, or added a system message anywhere) rather than leaving it or appending to it; `before_turn()` then marks the channel rewritten. An exception from the gate propagates from `before_turn()`.
- **`after_cancel()`** never calls the gate.
- **The compaction reset.** The handler `compaction()` returns, and the `Compact` protocol, have `reset(prefix)`: it clears the handler's kept input, the ids it has folded in, its token baseline and memory-warning flag, in place, and replaces its prefix. After `before_turn()`, both react loops read and clear the rewritten mark; when it was set they call `reset()` with the prefix derived from the new conversation and set their `overflow_compacted` flag back to `False`, so the next overflow may force a compaction of the rewritten conversation. Without a reset, a handler that adds only messages whose ids it has not seen would keep sending a replaced or removed message as it was. An append needs no reset.
- **`after_answer(channel, state)`** and the `TurnLoop` mark are the gate's hooks for persistent members ([swarm-persistent-members.md Continuing past an answer](swarm-persistent-members.md#continuing-past-an-answer)).

Without a gate nothing in inspect_ai behaves differently.

**Where a loop meets the gate.** `react()` and `react(submit=False)` call `before_turn()` at the top of every iteration, before the turn scope in which generation and tools run. A recovered context overflow and a recovered interrupt `continue` from inside the turn scope, so they reach `before_turn()` again; `on_continue` runs only after a completed turn. So the gate sees every generation's boundary of the loop, where `on_continue` misses the first and every recovery turn. `deepagent()`'s top-level loop is a `react()`, and its synchronous subagents are `react()` loops inside its tool calls. inspect_swe's bridged agents open no channel. react keeps its own `AgentState` object when a custom model agent returns another, so the state it passes is the loop's live one.

## The callback contract

**Registering.** `MemberHandle.on_boundary(callback, origin=...)` registers a callback and returns a function that unregisters it from the next boundary. Registered on a pending member, the callback sees boundary 0, before the first generation; on a running one, from its next boundary. `origin` names the caller in the evidence: 1 to 64 letters, digits, `_`, `-`, `.` or `:`, with `swarm` reserved for the runtime's own changes. It raises `ValueError` for an invalid origin and for a member the seam cannot hold ([Supported members](#supported-members)), and `RuntimeError` for an ended member and once the swarm is stopping.

**At a boundary** the callbacks run in registration order, in the member's task and span, while the member is held. Each receives a `Boundary`:

- `index` counts the boundaries of the member's own loop from 0; `activation` counts a persistent member's wakes, 0 for every other member.
- `after` is what the conversation ends with: `start` at index 0; `tools` after a tool result; `reply` after an assistant message with no tool calls (reached only by a loop that continues after a reply); `wake` after a persistent member's wake input; `user` after anything else (react's continue prompt, an incorrect-answer message, an operator's redirect after an interrupt).
- `messages` and `output` are deep copies: the first callback gets the member's conversation and the loop's last output, each later one the conversation as the callbacks before it left it, and the same output. Changing a copy changes nothing; a callback changes the member only by what it returns. `messages` is a plain `list`, never an `AgentState` or Inspect's limited list, so the chain stages freely: a callback may return a conversation over the member's message limit for a later callback to trim, and the limit is checked once, when the result is applied.

A callback returns:

- **`None`**: continue with the messages as it received them.
- **A list of messages**: the conversation to continue with. The gate deep-copies it, so changing it later changes nothing. Every message must have an id and the ids must be unique, since the diff and compaction both key on them; the list must hold at least one `ChatMessageUser`, because `before_turn()` would otherwise wait, after the release, for operator input that nothing in a swarm sends. A result that is not a list of chat messages raises `TypeError`, and one that breaks these rules `ValueError`, naming the member and the origin.
- **`"end"`**: end the member. Later callbacks do not run, and the member ends `stopped` with the reason `ended by <origin>`, never a submission, even after a reply (decision: Ransom, 2026-10-28). It is `end()` with the callback's origin, without a cancellation.

**Waiting and working time.** A callback may wait as long as it likes and may call models and tools of its own. Its model calls run under the member's limit nodes, so they count towards the member's token and cost limits and its ledger line. The seam opens no wait of its own (decision: Ransom, 2026-10-28): the gate runs each callback inside inspect_ai's known-work span (`known_work()`), so its time counts as working time unless it wraps an idle wait in `suspend_working_limit()`, which suspends that callback's span alone. Inspect subtracts an open wait anywhere in the sample unless known work overlaps it, so without the span one member's marked wait would hide another member's unmarked wait or computation.

**Composition.** Every caller's callbacks run at every boundary, so callers compose without knowing each other: a pause registered after a scheduler's callback holds the member after the scheduler lets it go, so an activation that waits for the member's next park waits for the resume too. A caller that wants to act at some boundaries returns `None` at the others.

**Applying the result.** When the chain finishes, the gate compares the staged conversation with the live one by message id and hash:

- unchanged: nothing to apply;
- the live messages unchanged and messages added after them, none a system message: an append, `messages.extend(...)`;
- anything else: a rewrite, `messages[:] = ...`, after which `before_turn()` marks the channel and react resets its compaction.

An appended system message is a rewrite because react's compaction handler keeps the system messages in a prefix saved when the loop started, and native compaction restores system messages from that prefix alone (the providers return none), so an appended one would be lost at the next native compaction; the reset rebuilds the prefix from the new conversation. The change is made to the list the loop passed, so a member's limited list checks the message limit for the growth before anything changes. A loop that passed something other than a `list` (a tuple from a custom loop) cannot take a change: the gate raises `TypeError` saying a callback that changes the conversation needs the loop's own message list.

**Stale summaries.** A compaction summary is a user message with `summary` in its metadata that stands in for everything before it, and the next summary compaction starts from the latest one. Once a rewrite changes what a summary was derived from, the summary describes a conversation that no longer exists. So before a rewrite is applied, the gate finds the longest run at the start of the staged conversation that matches the live one message for message, and removes every later message that carries `summary` and is unchanged from the live conversation. Summaries in that run are kept, and so is a summary the chain added or replaced, which is the caller's text. The removal is one more `changed` event with origin `swarm`, and the user-message check runs again on the result: a rewrite whose only user message was a stale summary raises `ValueError` and the member ends `errored`.

**Errors and cancellation.** An exception a callback raises propagates in the member's task, as an error from its agent does: the member ends `errored` and the error leaves the swarm as a foreign error ([swarm-api.md](swarm-api.md)). A change the message limit refuses propagates the limit's error, so the member ends `member_limit:message` with its conversation as it was. A cancellation from outside the swarm (the sample's limits or interrupt, the hard stop) cancels the callback where it waits. In every such case nothing from the chain is applied, and the evidence says so.

### Ending a member from outside the chain

The chain is serial, so a callback can end the member only when its turn comes, and an earlier callback may wait indefinitely. `MemberHandle.end(origin=...)` ends the member without the other callers' cooperation. The first request wins and later ones change nothing; a request for an ended member changes nothing. It closes the member's lever with `ended by <origin>`, so its next model call is refused, and:

- **Held**: it cancels the chain through the gate's own cancel scope. The waiting callback gets a cancellation, later ones do not run, and the member ends `stopped`. Cancelling is only a request, so the gate also checks for a stop or an end request after every callback returns; a callback that calls `end()` and returns, or catches the cancellation, gets the same result.
- **Mid-turn or in setup**: the gate ends the member at its next boundary. A turn can also end the loop with no boundary after it (a final submit tool already running, a `react(submit=False)` reply), so a member whose agent returns after an end request ends `stopped`, whatever its output; an error the agent raises is classified as usual.
- **Pending**: it ends `stopped` as soon as it is started, before its sandboxes, background owner, agent build or any model call.
- **Draining its background work** after its agent returned: the drain's own grace bounds it, and a return without a submission (`unsubmitted`) becomes `stopped` while a submission is kept (decision: Ransom, 2026-11-04).

A turn may not reach its next boundary soon (a long tool call, an interrupt's recovery), and agent and resource cleanup after the gate ends a member (a wrapper's `finally`, react's MCP teardown) may run long. So an end request for a running member, `"end"` included, starts a timer in the runtime's task group whose deadline is the swarm's effective grace from the request; if the member is still running then, its scope is cancelled and it ends `cancelled`, as one the hard stop reaches.

## The member's gate

Each member run has one `MemberGate`. The member wrapper installs it with `turn_gate(gate)` around the agent call, inside the member's scopes and limit nodes, and records the member's own task. The gate therefore sees every agent channel the member's work opens.

**Binding: one loop per member.** The seam keeps its own binding, `MemberContext.turn_loop`, separate from the bus's `MemberContext.loop`, which `swarm_tools()` binds where it is first resolved. On each call the gate decides whether the channel is the member's own loop:

- A call outside the member's own task (a loop in a `background()` task, or in a task group a tool starts) returns at once, changing nothing.
- With `turn_loop` unbound, this channel becomes it: the first loop to reach the gate in the member's own task. For `react()` and `deepagent()` that is the top-level loop, which reaches its first boundary before it can start a subagent.
- Another channel while `turn_loop` is open is a nested loop inside one of its turns (a subagent, a handoff, an `as_tool()` agent), and returns at once.
- Once `turn_loop` has closed, the next top-level loop in the member's task is a *successor*, and loops nested inside it return at once. The seam does not hold a successor. With callbacks still registered the gate raises `RuntimeError` at its first boundary, before its first generation, saying a member with boundary callbacks may run only one agent loop (decision: Ransom, 2026-10-29). With none it still ends the member at a successor's boundary once a stop or end request is pending, keeping that boundary's state for the member's output.

The two bindings usually name the same loop, but not when only a child loop has `swarm_tools()` (as with `research(extra_tools=[swarm_tools()])` under a `deepagent()` whose own tools omit them): the bus binds the child and the seam holds the parent, the child running inside one of the parent's turns. Neither binding moves the other. In this document "the member's own loop" means `turn_loop`.

**The loop's state.** At every boundary of the own or a successor loop the gate keeps the `AgentState` the loop passed, or none when it passed none, so an earlier loop's state is never kept for a later one. When the member's agent raises rather than returns (an end at a boundary, a limit, an error), the member wrapper takes the member's output and conversation from that state rather than from the state it called the agent with. A boundary's `output` is therefore the loop's own last output, with its stop reason and usage, never one rebuilt from the messages.

**At each boundary of the own loop**, in order:

1. Keep the state, mark the loop as having reached the gate, and take the boundary's index. A persistent member that has just answered takes the idle path instead ([swarm-persistent-members.md The idle hold](swarm-persistent-members.md#the-idle-hold)).
2. If the swarm is stopping or an end was requested, end the member ([Stopping](#stopping)).
3. At the first boundary, call the runtime's `loop_bound` hook, through which the operator layer subscribes to the loop and applies a pending pause ([swarm-operator-intervention.md The loop-bound hook](swarm-operator-intervention.md#the-loop-bound-hook)).
4. If the loop passed no state, or any of the member's background work is running, withdraw the withdrawable callbacks (below). With no callback left, return with no `await`.
5. With callbacks left, refuse such a boundary: the member ends `errored` with a `RuntimeError` naming it and the reason.
6. **Hold.** If a member time limit has already run out, deliver its cancellation instead and do not hold ([Time](#time)). Otherwise mark the member held, pause its time limits and recheck the swarm's waits; run the callbacks in the chain's cancel scope, rechecking for a stop or end request after each and, before each later callback, for background work that started during the hold.
7. **Release.** Resume the time limits, clear the mark and recheck the waits. A stop or end request pending now wins over whatever the chain returned: the staged changes are discarded and the member ends. A callback's `"end"` ends the member. Otherwise apply the result and return whether it rewrote. The evidence is written in a `finally`, however the hold ends.

The gate never returns control to the loop with a stop or end request pending, so no cached reply or cached tool call runs after one.

**Background work and holds.** Step 5 runs before step 6, so the runtime never reports a hold while the member's background work runs, and none starts during one: the member's background owner (`MemberBackground.start_soon()`) refuses new work while the member is held, with a `RuntimeError` naming the member, so a callback that calls `background()` ends the member `errored`.

**Withdrawable registrations.** The operator's pause registers its callback through a runtime-only, withdrawable registration, because a pause must never make a member error (decision: Ransom, 2026-11-02). Wherever the gate would raise into the member because callbacks are registered (no state, background work at a boundary, a successor loop, and the own loop's close), and when the member is first found bridged, it withdraws such callbacks first, telling the operator why; if no other caller's callback remains, the member goes on as one with none. The background owner may admit new work while a withdrawable callback is the one holding the member, which withdraws it; the gate's check before each later callback then applies the background rule to the next caller as at the start of a chain. The whole rule set is the operator design's ([swarm-operator-intervention.md Pause](swarm-operator-intervention.md#pause)).

## Interrupts

An interrupt is outside the seam. `after_cancel()` never calls the gate, so a member in an interrupt's recovery is mid-turn: no callback runs, the member is not held, `swarm_wait` counts it as active, its time limit runs, and a stop or `end()` reaches it through the closed lever, the grace period and cancellation. When the recovery ends, react goes on to an ordinary boundary with every check of the gate. Operators reach members through `inspect ctl`, which always posts the redirect before the interrupt, so the recovery never waits ([swarm-operator-intervention.md Messages](swarm-operator-intervention.md#messages)).

## Stopping

- **A stop ends a member at its boundary.** `stop(reason)`, in its synchronous first part, closes every running member's lever and cancels every gate's running chain, and from then on every boundary of any member, the own loop's or a successor's, with callbacks or without, ends it. The gate ends a member by raising its lever's limit error, which the member wrapper classifies as `stopped`, with the loop's last output as its output. No generation follows, so a cached reply or cached tool calls cannot run after the stop, and the member never returns normally to be read as `submitted`. A member that would have met a swarm cap at its next model call ends `stopped` at the boundary before it.
- **Held members** therefore end at once, without waiting out the grace period, and a change a cancelled callback had not returned is never applied.
- **`end()` and `"end"`** take the same path for one member, with the reason `ended by <origin>`, and the rest of the swarm goes on.
- **The hard stop treats a held member like any other.** The supervisor's hard stop and `end()`'s grace timer cancel every member still running at their deadline, held or not; cleanup after the gate's ending keeps that deadline. Cancelling a chain only schedules its unwinding, so the hard stop can reach a member the gate is ending before its wrapper records the end: whenever cleanup outlasts the remaining grace, and always at an effective grace of zero. That member's ledger line is then `cancelled` rather than `stopped`. This is accepted (decision: Ransom, 2026-10-29): a parked member has no model call in flight, so only the label differs.
- **The ledger.** Cancelling a chain is not a hard stop, so the member's ledger line is not `cancelled` unless the hard stop reached it (above) or a callback had a model call of its own in flight at the cancellation that did not then complete, whose usage is unknown. The gate records the calls in flight when it cancels a chain, and the ledger checks them.

## Time

| | Held time |
|---|---|
| The member's time limit | does not count: the limit pauses while the member is held |
| The swarm's time cap | counts |
| The sample's time limit | counts |
| The sample's working time | the callback's to mark: its work counts, and its idle waits are waiting time when wrapped in `suspend_working_limit()` |
| The metrics' `running_time` | counts; `held_time` gives the held part |
| `swarm_wait`'s idle time | not idle: a held member has no wait |

- **The member's time limit pauses.** A member time limit is a `SwarmTimeNode`, the swarm's subclass of Inspect's time limit. When the gate holds the member it first asks each node whether it is overdue (its deadline has passed, or its scope already has a cancellation requested), after preparing the boundary and with nothing that yields between that check and the pause. An overdue node is never paused: the gate cancels the limit's own scope and awaits a checkpoint inside it, so the member ends `member_limit:time` through Inspect's own path, whatever the backend (on asyncio the deadline's cancellation comes from a timer a bare checkpoint can precede). The member is not held and no `boundary` event is written. Otherwise the node sets its deadline to infinity; at the release it moves its start time on by the held time and re-derives the deadline, so its deadline and reported usage count only time the member was not held. It raises Inspect's time limit error as before.
- **The swarm's time cap and the sample's time limit** are deadlines for the whole run and keep counting.
- **`swarm_wait`.** A held member counts as not active: the all-waiting check releases the waits when every running member is waiting or held, and the gate rechecks the waits when it holds and releases. So members waiting on a scheduler's or a persistent member's hold are released rather than run to their timeout. The one exception is the operator's pause, judged by the callback holding the member now: while it is the pause's withdrawable registration, the member counts as active (decision: Ransom, 2026-11-02; [swarm-operator-intervention.md Pause](swarm-operator-intervention.md#pause)).

## Supported members

| Member | Seam |
|---|---|
| `react()`, `react(submit=False)`, `deepagent()` without background dispatch | Its top-level loop. A loop that ends at a submission or a tool-free reply has no boundary after it, unless it continues past the answer: an `on_continue` that returns the state, or a persistent member's `after_answer()`. |
| Nested loops: subagents, handoffs, `as_tool()` agents, `deepagent()`'s synchronous subagents | Never held on their own; they run inside a turn of the member's own loop, and a hold takes effect when that turn ends. |
| `deepagent(background=True)` or an integer `background` | `on_boundary()` raises `ValueError`, read from the registry name and arguments of the member's agent or factory. Its background subagents would go on generating while the loop was held. |
| A custom agent that calls `background()` | Checked at every boundary with callbacks: with background work running, the member ends `errored`; tasks that have finished do not matter. |
| Bridged members, declared or in the `inspect_swe/` namespace, or found bridged as they run | `on_boundary()` raises `ValueError`: the CLI runs its own turns and opens no channel. |
| A custom loop that calls `before_turn()` without its state | Refused at its first boundary with callbacks: the member ends `errored`, since a callback could not be given its output. Without callbacks it runs as without the seam. |
| A member that opens a second top-level loop after the first closed | Refused at that loop's first boundary with callbacks ([The member's gate](#the-members-gate)). |
| A nested swarm inside a member | Its members have gates of their own, installed by its runtime; the outer member's gate never binds their loops. |
| Members with callbacks that never reach a boundary: an undeclared bridged wrapper, a custom agent that generates without an agent channel, one that returns or answers a constant | Found when the member ends (below). |

**The never-reached rule.** The gate records whether the member's own loop ever reached it, and the handle whether a callback was ever registered for the run, unregistered since or not. If one was and the loop never reached the gate, the member wrapper ends the member `errored` with a `RuntimeError` saying its callbacks never ran because it never reached a turn boundary, when the member produced its ending itself: a normal return, its own limit, or its own error, which becomes the new error's cause. It checks after the agent's call ends and again after the member's background work has drained, for a callback registered during the drain. An ending the runtime or the outside produced keeps its ordinary outcome: a swarm stop, `end()`'s grace timer, a cancellation from outside the swarm. So an unsupervised result is never a submission or the swarm's answer, at the cost of one unsupervised run whose model calls are spent (decision: Ransom, 2026-10-29). The rule has no exception for a limit, so it also covers a supported loop that ends during its setup, before its first boundary (react's system prompt overflowing a tight member `message_limit()`, a time limit expiring while MCP servers open): the runtime cannot tell such a loop from an unsupported one, and a limit hit during setup is a problem with the setup (decision: Ransom, 2026-10-29). The `RuntimeError` is a foreign error, so it fails the swarm. A persistent member is held to the same rule without a callback ([swarm-persistent-members.md](swarm-persistent-members.md#which-agents-can-persist)).

## Evidence

Two inspect_ai `SwarmEvent` kinds ([swarm-event-types.md](swarm-event-types.md)), written synchronously in the member's span with `member` set and `data["version"] = 1`, only at boundaries where the member was held. Their fields are documented in [Analysis, Boundary callbacks](../docs/analysis.qmd#boundary-callbacks).

- **`boundary`**, once per hold: the index, `after`, the callbacks' origins in order, the seconds held, the message counts before and after, and the outcome: `continue`, `end` (with the origin), `stop`, `error` (with the origin and the error's type; origin `swarm` for the runtime's own checks), `rejected` (applying the change raised) or `cancelled` (a cancellation from outside the chain). A persistent member's idle hold writes one with or without callbacks.
- **`changed`**, once per callback whose result differs from what it received, found by diffing on message id: `added` (id, hash and index in the result), `removed` (ids), `replaced` (id and the hashes before and after), `order` (the result's ids, only when kept messages changed their relative order) and `applied`, true only when the boundary's outcome is `continue`. The runtime's own changes are `changed` events with origin `swarm`: a persistent member's wake input, before the callbacks' own, and the stale summaries a rewrite removed, after them.

A hash is the SHA-256 of the message as the log serializes it, its id excluded, so analysis can match a change to the messages in a later `ModelEvent`'s input where Inspect has not combined or compacted them. Returning the same content writes no `changed` event.

**Ids and hashes, not content.** The content is in the member's conversation and the `ModelEvent` inputs; recording it again would grow the log with every activation for a defence that rewrites much of the conversation. A caller that wants its own content record writes its own event, as ORBIT does.

**One event per callback**, with the origin given at registration, not one per operation inside a callback. A caller that wants changes told apart (attack input, then a defence) registers a callback for each, in the order they must run.

The metrics have `holds` (the boundaries at which the member was held) and `held_time` per member, defaulting to 0 so that older metrics load. The result record's and ledger's `end_origin` says whose end request decided how a member ended ([Analysis](../docs/analysis.qmd)).

## How the features use the seam

- **Persistent members.** inspect_ai's `after_answer()` hook lets the gate keep a persistent member's loop going past an answer to its next boundary, where the gate holds the member idle until `SwarmControl.wake()`: a hold in every sense above, the wake's input a runtime `changed` event and `after == "wake"`, with any callbacks running after it. [swarm-persistent-members.md](swarm-persistent-members.md).
- **Operator intervention.** A pause is a withdrawable callback with origin `operator` that waits inside `suspend_working_limit()`; a stop of one member is `end(origin="operator")`; messages go to the agent channel of `turn_loop` and drain after the gate returns. [swarm-operator-intervention.md](swarm-operator-intervention.md).
- **ORBIT.** ORBIT's scheduler is its own controller over `on_boundary()`, with its schedule, attack and defence callbacks in that order; its activation, snapshots and interventions are ORBIT's code. [swarm-orbit.md](swarm-orbit.md).

## Alternatives considered

**A swarm `on_continue` hook** (`swarm_on_continue(inner)`, wired in by the member's author and running the callbacks after each completed turn). No inspect_ai seam needed. But it misses the first turn and every recovery turn; it reaches only members whose author wired it, so an operator could not pause an arbitrary member, which is the wiring the removal of notices ended (decision: Ransom, 2026-10-27); it must compose with the author's own `on_continue`; and it still needs the inspect_ai compaction reset, since react's compaction ignores a state returned from `on_continue` exactly as it would a gate's rewrite. Rejected for the gate.

**Swarm awareness only**: a `member_held()` context manager for callers' own hooks. The smallest swarm surface, but it keeps every gap of the hook above, ending a member needs its own path, and nothing records the changes callers make.

**A swarm-owned scheduling API** (`hold()`, `resume(input=)`, `activate()`, `round_robin`). It puts one caller's policy in the swarm, and holders compose badly: an activation must ask for "the boundary after this one" while another holder's pause still holds this one, and a resume with input must say whose input comes first. Callbacks compose by running in order, and an activation is a few lines over them in the caller's code.

**A hold at the model call**, as inspect_ai's `pause --now` holds one. It would also reach bridged members and every loop, but a model call is not a turn boundary: it comes mid-turn for a subagent, a parallel call, compaction and each retry, so a hold there leaves tool calls in flight and cannot change the conversation. It stays the only way to pause a bridged member.

**A public gate on `react()`** (`react(turn_gate=...)`): explicit, but wiring again.

**`Steer` items posted between turns.** They can add messages at a boundary but not hold or rewrite, and every post meets ACP's binding.

**Content in the change record.** Self-contained, but redundant with the conversation and `ModelEvent` inputs, and quadratic in log size for a defence that rewrites every activation.

**Leaving the stop to the closed lever.** A released member would end at its next model call, but a cache hit is returned before the lever's check, so a released `react(submit=False)` member could answer from the cache and end `submitted` after the stop.

**Callbacks inside a wait the seam opens** (the chain wrapped in `sample_wait()`). No callback would need to mark its waits, but work inside such a wait is not charged at all, so a defence that spends its time in model calls would never count towards the working limit (decision: Ransom, 2026-10-28).

**Refusing an unsupported member before its first model call.** Every hook fell short: the transcript misses a failed bridged request whose events are deferred, the limit nodes miss a cache hit and also run while react builds the starting conversation, where they would refuse a supported member with a message limit. Rejected for the check when the member ends: one place, no dependency on how Inspect emits events (decision: Ransom, 2026-10-29).

**Refusing a rewrite that keeps a stale summary**, rather than dropping it. Every defence that edits history would have to know Inspect's summary metadata and the member's compaction strategy, and a summary is a cache Inspect derived, not a caller's message. Changing every summary strategy to ignore summaries after a reset would still send the stale summary below the threshold.

**Holding successor and warmup loops.** No member shape in use runs more than one top-level loop, and telling a successor from a nested loop and a warmup from the main loop needs bookkeeping for every case (decision: Ransom, 2026-10-29).

**Exempting an orderly ending from the hard stop**, so that a zero-grace stop could not reach a member the gate is ending. The mark had to end before the background drain and agent cleanup or it would shield them past the stop's deadline, and each of its edges was a new race (decision: Ransom, 2026-10-29).

**Building the boundary's output from the messages.** A custom model agent's output carries its stop reason and usage, which no chat message holds; the loop passes its state instead.

## Compatibility

- **inspect_ai.** The gate module, `turn_gate()` and `TurnGate` are private. Public changes are additive: `before_turn()`'s optional `state`, and `reset()` on the compaction handler and the `Compact` protocol, which a custom class matched structurally against `Compact` must add. Without a gate nothing changes.
- **inspect_swarm's API.** `on_boundary()`, `end()` and `held` on `MemberHandle`, and `Boundary` and `BoundaryCallback` exported from `inspect_swarm`. `MemberHandle` is a protocol that only the runtime implements; a structural implementation of it must provide them.
- **Members without callbacks.** One behaviour applies to every member: a member that reaches a boundary after the swarm began to stop, or after `end()`, ends there `stopped` rather than at its next model call. Binding changes nothing for the bus: tools, waits, `tools_unwired` and `acp_bound` read `MemberContext.loop`.
- **Member time limits** are the swarm's subclass of Inspect's, logged, rebuilt and enforced as Inspect's, and identical while the member is not held.
- **Logs.** The two event kinds are `SwarmEvent` kinds, which the viewer shows as any other; the metrics fields default to 0, so earlier logs load unchanged.

## Security

- **Callbacks are trusted code**: eval-author Python in the runner process, like controllers. They see the member's full state, peer text in tool results included. What they read, and what they put in a conversation and in which role, is the caller's policy; the seam's part is to make it visible, as `changed` events with the caller's origin.
- **No member reaches the seam.** No tool exposes `on_boundary()` or `end()`, and the gate's `ContextVar` is set only by the member wrapper. Trusted code in the runner could set another gate, as it could set `background_target`.
- **Model output in the evidence.** The gate and its events read message types, ids and hashes, never text. `after` follows from the model's own output (a reply or tool calls), as a member's `submitted` does. Origins are caller-chosen identifiers checked against a fixed pattern.
- **Holding does not stop the sandbox.** Processes a member left running keep running while it is held.
- **A callback that never returns** holds its member until the swarm's time cap or the sample's limits end the swarm, as with any controller that waits forever.

## Tests

All run on mockllm, with no network or Docker, on asyncio and trio through `tests/conftest.py`: the seam's contract, binding, stops, time, waits, refusals and evidence in `tests/test_turn_seam.py`; the idle hold in `tests/test_persistent.py`; the pause's callback and withdrawals in `tests/test_operator.py`; and an ORBIT-shaped scheduler over the seam in `tests/test_orbit_shape.py`. inspect_ai's gate, the react loops' calls and the compaction reset are tested in its `tests/agent/test_channel/test_channel.py` and `tests/agent/test_agent_react.py`.

## Not this design

- **Holding a member's background work**, by gating its background tasks' loops or quiescing them before a hold ([swarm-background-members.md](swarm-background-members.md)); persistent members and pauses with background work wait on it.
- **Pausing bridged members**, through a hold at the model call or inspect_swe's restart with `--resume`.
- **Holding a member during an interrupt's recovery.** An ACP interrupt without a redirect still leaves the ACP-bound member waiting in `after_cancel()`, outside the seam.
- **The closed lever and cached calls inside a turn.** After a stop begins, model calls inside a turn already running (a subagent's, compaction's) can still be served from the cache, since a hit is returned before the limit checks; the seam ends only the member's own loop, at its next boundary. A fix belongs in Inspect's limit checks.
- **Other callers**: push delivery for a messages channel, turn-based barrier controllers and joint monitors, and checkpoints at a point where no member has a call in flight could each be callbacks; none is built.
