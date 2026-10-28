# Inspect Swarm: the turn seam

## Overview

The turn seam hands a member's state to eval-author code at every turn boundary of the member's own agent loop: before its first generation and before each one after it, with no model call or tool call of the loop in flight. The code is a *boundary callback*, registered on the member's handle by a controller (ORBIT's scheduler, a coordinator that wakes a persistent member, an operator's pause). It may wait as long as it likes, then continue the member unchanged, continue it with a replaced conversation, or end it. While callbacks run, the member is *held*, and the swarm knows it: its time limit does not run, the sample's working time counts the hold as waiting, `swarm_wait` does not count it as active, and a stop ends it at once with status `stopped`.

The boundary is a small hook in inspect_ai's agent channel: a private turn gate that `before_turn()` awaits, which the swarm installs for every member. So every `react()` and `deepagent()` member has the seam, its first turn and the turns that recover from a context overflow or an interrupt included, without its author wiring anything; nested loops pass through untouched. When a callback rewrites the conversation, react resets its compaction state so the model sees the rewrite. Every change a callback makes is recorded as one event, with the callback's origin and the ids and hashes of the messages appended, removed and replaced, not their text. Members the seam cannot hold (bridged CLIs, members with background work running, agents with no agent channel) are refused with an error. What callers do at the boundary (scheduling, snapshots, attack input, defences, waking with new work) is their own code and their own designs.

## Why

Several planned features need to stop a running member at a turn boundary and continue it later ([#123](https://github.com/meridianlabs-ai/inspect_swarm/issues/123)):

- **Persistent members** ([#116](https://github.com/meridianlabs-ai/inspect_swarm/issues/116)) hold an idle member after it answers, and wake it with new work.
- **Operator intervention** ([#119](https://github.com/meridianlabs-ai/inspect_swarm/issues/119)) pauses and resumes one member or the whole swarm, and stops one member.
- **ORBIT's scheduled mode** ([swarm-orbit.md P4](swarm-orbit.md#p4-scheduled-mode-the-turn-seam-and-activation) and [P5](swarm-orbit.md#p5-trusted-interventions)) runs a member for one quantum and parks it; trusted interventions change its conversation between activations.
- **Push delivery** for the later messages channel ([swarm-communication.md Push delivery after M2](swarm-communication.md#push-delivery-after-m2-optional)) adds reminders and injected content at a turn boundary, if [its open question 2](swarm-communication.md#open-questions) chooses push.
- **Turn-based controllers** (supersteps, Concordia-style sequential engines, AgentsNet-style synchronous rounds; [swarm.md](swarm.md)) and joint monitors that check every member between turns hold every member at a boundary, as a barrier.
- **Checkpoint and resume** ([#121](https://github.com/meridianlabs-ai/inspect_swarm/issues/121)) may want a point where no member has a call in flight.

Much of this can be built today in the caller's own code. ORBIT's member builder can give `react()` its own `on_continue`, which sees the member's state after each completed turn, can wait there until ORBIT's scheduler says go, and can return an `AgentState` that replaces the messages; attack input before the first turn can go through `start(member, input=[...])`, which takes messages in any role. What no caller can do on top is make the swarm know that a member is held. A member parked inside an author's own hook looks running to the runtime, so the swarm gets four things wrong:

- **Status.** A member let go by returning `False` after a tool-call turn returns normally with `stop_reason == "tool_calls"`, which the swarm reads as `submitted` ([swarm-orbit.md Spike](swarm-orbit.md#spike)).
- **Time.** Held time runs down the member's time limit.
- **Waits.** `swarm_wait`'s all-waiting release counts a held member as active, so the other members' waits run to their timeout.
- **Stop.** The swarm cannot release a held member: it waits out the grace period and cancels it.

And two things the callers cannot reach at all: the first turn and recovery turns, which `on_continue` never sees, and a member whose author wired nothing, such as one an operator wants to pause. So the seam is one contract that every caller uses, and the swarm owns only its knowledge of a held member.

## Goals and non-goals

Goals:

- One boundary callback contract: the member's state in; continue, replace the conversation, or end out; the member held while callbacks run. ORBIT's scheduler, an operator pause and waking a persistent member are all callers of it.
- Every boundary of the member's own loop, its first and its recovery turns included, with no wiring by agent authors.
- The runtime's knowledge of a held member: status, time limits, the sample's working time, `swarm_wait`'s all-waiting release, and release at a stop.
- A standard record of every change a callback makes at a boundary.
- A clear error for every member the seam cannot hold.

Non-goals:

- **The callers.** Scheduling (`activate()`, quanta, `round_robin`), snapshots, staged copies, `append` and `edit`, attack and defence logic: ORBIT's code. How a persistent member continues past an answer and how its answers count: #116. The operator's surface: #119. Whether to push: swarm-communication.md's open question 2. What a checkpoint holds: #121.
- **What text may move.** Whether code at the boundary may read a member's text or add input in the user or system role ([swarm-orbit.md open question 1](swarm-orbit.md#open-questions)) is the caller's decision. Code that wraps a member could always do either; the seam records what it did.
- **Holding work outside the member's own loop**: its background work, a bridged CLI's turns, nested loops on their own.
- **Preemption.** A hold takes effect when the current turn ends. Interrupting a turn in flight is urgent steering's mechanism ([Urgent messages and steering](swarm-communication.md#urgent-messages-and-steering)).

## Current behaviour

Code references are to inspect_swarm `main` at `1c8aea1` and inspect_ai at `c2fc3d9e3`.

### Where a loop meets the agent channel

- **`react()` calls `before_turn()` at the top of every iteration** of its loop (`src/inspect_ai/agent/_react.py:261-263`; `react(submit=False)`'s loop at `:501-503`), before the checkpoint tick and the turn scope. Generation and tool execution run inside `ch.turn_scope()` (`:268-367`).
  - A recovered context overflow `continue`s from inside the turn scope (`:275-286`), and so does a recovered interrupt (`:368-373`). Both reach `before_turn()` again, and neither runs `on_continue`.
  - `on_continue` runs only after a completed turn (`:375-401`).
  - So `before_turn()` runs before every generation of the loop, and `on_continue` misses the first and every recovery turn.
- **How a loop ends.** A submission ends a submitting `react()` (`:333-344`). Without an `on_continue`, a submitting `react()` appends its continue prompt after a tool-free reply (`:403-416`), and `react(submit=False)` ends at its first tool-free reply (`:571-572`). With an `on_continue` that returns its `AgentState`, either continues after a reply with nothing added (`:397-399`, `:566-568`).
- **`before_turn()`** drains the channel's queue and returns the operator's messages, and blocks only when the conversation has no user message yet (`src/inspect_ai/agent/_channel/channel.py:432-453`). react extends the list it passed in with what it returns. Nothing else runs there.
- **`agent_channel()`** creates a channel, offers it to the sample's ACP transport (first binder wins), and sets it as the current channel for its block (`src/inspect_ai/agent/_channel/__init__.py:97-143`). A nested loop, such as a subagent called as a tool, opens a channel of its own (`:106-117`). Outside any block the current channel is an inert singleton (`:47-94`).
- **Who opens a channel.** In inspect_ai, only `react()` and `react(submit=False)` (a search for `agent_channel()` and `before_turn` in `src/inspect_ai`). `deepagent()`'s top-level loop is a `react()` (`src/inspect_ai/agent/_deepagent/deepagent.py:230-240`), and its subagents are `react()` loops running inside its tool calls. inspect_swe's bridged agents open none (the same search in inspect_swe `src` at `661d6bf` finds nothing).
- **Conversations are mutable lists.** A member's messages may be inspect_ai's limited list, whose slice assignment checks the message limit for the growth (`src/inspect_ai/util/_limited_conversation.py:41-52`).

### Compaction keeps its own view of the conversation

- react creates one compaction handler per loop before the loop starts, with the system messages and the sample input as its prefix (`_react.py:242`, `:486`, `_agent_compact` at `:690-713`). `deepagent()` turns compaction on by default.
- The handler keeps the input it last sent and the ids of the messages it has folded into it, and on each call adds only the messages whose ids it has not seen (`src/inspect_ai/model/_compaction/_compaction.py:99`, `:167-172`, `:276-296`). Below the threshold it sends that kept input.
- So a conversation rewritten between turns, by an `on_continue` that returns an `AgentState` or by anything else, does not reach the model when compaction is on: a message replaced under the same id, or deleted, stays in the next input as it was.

### The member's own loop

- The member's own loop is `MemberContext.loop`, bound to the first agent channel in which `swarm_tools()` is resolved (`src/inspect_swarm/_bus/_tools.py:17-23`, `src/inspect_swarm/_budget.py:309-312`). The bus uses it to tell nested loops from the member's own (`_bus/_bus.py:197-200`), and the `tools_unwired` check reads whether it was ever bound (`_controller/_control.py:499-501`, `_metrics.py:216-220`).
- The swarm never posts to or binds a member's agent channel ([The binder and ACP](swarm-communication.md#the-binder-and-acp)).

### Running, stopping and time

- `start()` runs the member in its own task (`_controller/_control.py:311-340`). The member wrapper enters its tool-state scope, sandboxes, cancel scope, background owner and limit nodes, opens its span and calls the agent (`src/inspect_swarm/_member.py:468-551`, the call at `:586-600`).
- A normal return is `submitted` unless the output's `stop_reason` is `content_filter` or `model_length` (`_member.py:569-612`).
- `stop(reason)` closes the bus, cancels the controller and closes the lever of every running member, synchronously; then come the grace period and the hard stop (`_control.py:517-532`, `:636-647`). A closed lever fails the member's next model call's limit check (`_budget.py:528-532`), and the lever's error gives the status `stopped` (`_budget.py:386-396`). A cache hit is returned before those checks (`src/inspect_ai/model/_model.py:1525-1558`), so a closed lever does not stop a member whose next call is cached.
- A member time limit is Inspect's `time_limit()`, a wall-clock deadline on a cancel scope (`_member.py:403-404`). The limit derives its deadline from its start time and can re-derive it on a running scope (`src/inspect_ai/util/_limit.py:1406-1420`, `_refresh_deadline` at `:1466-1490`). The swarm's time cap sleeps, then stops the swarm (`_control.py:632-634`).
- **Sample working time.** Each instant is working if any known work (a model attempt, a sandbox exec, a tool call that is not itself waiting) is in progress anywhere in the sample, otherwise waiting if any known wait is open (`src/inspect_ai/_util/working.py:1-8`). `sample_wait()` opens such a wait for a block, and work inside it is not known work (`:560-577`).
- **`swarm_wait`'s all-waiting check** releases every wait only when no member is running without waiting (`_bus/_wait.py:133-146`): a running member that is not in a wait blocks it.
- **Background work.** `MemberBackground` owns work a member starts with `background()`, and the member's context counts the tasks running (`background_running`) (`src/inspect_swarm/_background.py:89-108`, `_budget.py:313-317`). Bridged members are detected from the declaration or the `inspect_swe/` registry namespace (`_budget.py:290-296`), or found in a model call through the bridge, where a member that cannot go on is cancelled and its refusal raised by the wrapper (`_budget.py:344-355`).
- **Controllers** see members through `MemberHandle`: name, role, state, status and whether it submitted (`_control.py:51-77`). `events()` yields `MemberEnded` only.

### Spike

A throwaway patch of `AgentChannel.before_turn` parked members of a real `swarm()` inside the call, keyed by `current_member` and bound to the first channel of each member that reached `before_turn()`. Two `react()` members on mockllm each called a 0.3-second tool twice and submitted; a test controller held them. On both asyncio and trio:

- A member held before it started parked at its first boundary, before its first generation, with its preamble as the last message.
- A message appended in place to the list `before_turn()` received was in the member's next `ModelEvent` input.
- One member held for 1 s while the other worked: total 1.94 s, working time 1.47 s on asyncio. The held time counted as working while the other member's tool calls ran and as waiting after it ended. Both held for 1 s: total 1.82 s, working 0.73 s.
- Every sample ended with status `success`.

### What the uses need

| Use | Wait at a boundary | Add messages | Rewrite messages | Read the state | End the member |
|---|---|---|---|---|---|
| Persistent members (#116) | idle, after a reply | the new work | | | when no more work comes |
| Operator controls (#119) | while paused | | | | stop one member |
| ORBIT scheduled mode (P4) | until activated | | | snapshots, eligibility | |
| ORBIT interventions (P5) | | scenario and attack input, any role | defences | | |
| Push delivery | | reminders, injections | | unread state is the bus's | |
| Turn-based controllers, joint monitors | every member at once, as a barrier | | | | |
| Checkpoint (#121) | every member at once | | | the conversation | |

Member shapes in use: `react()` and `deepagent()` members in the examples and docs (`examples/*.py`, `docs/members.qmd`), `deepagent(background=...)` members (`docs/members.qmd:115`), inspect_swe's bridged agents (`docs/members.qmd:146`), and test-only custom agents that call the model in their own loop (`tests/swarm_runtime.py`, `generator`).

## Design

### At a glance

```python
# inspect_ai: src/inspect_ai/agent/_channel/gate.py (new, private)
TurnGate = Callable[[AgentChannel, Sequence[ChatMessage]], Awaitable[list[ChatMessage] | None]]

@contextmanager
def turn_gate(gate: TurnGate | None) -> Iterator[None]: ...   # the gate for channels opened in the block


# inspect_swarm
@dataclass(frozen=True)
class Boundary:
    member: str
    index: int                                          # 0 before the loop's first generation
    after: Literal["start", "tools", "reply", "user"]   # what the conversation ends with
    state: AgentState                                   # a detached copy: messages and output


BoundaryCallback = Callable[[Boundary], Awaitable[list[ChatMessage] | Literal["end"] | None]]


class MemberHandle(Protocol):                           # the existing handle, plus:
    @property
    def held(self) -> bool: ...                         # its boundary callbacks are running
    def on_boundary(self, callback: BoundaryCallback, *, origin: str) -> Callable[[], None]: ...
```

### The callback contract

**Registering.** `MemberHandle.on_boundary(callback, origin=...)` registers a callback for the member and returns a function that unregisters it. Any code holding the handle registers: a controller's `run`, and through it whatever the controller hosts (ORBIT's scheduler, an operator surface). It may be called on a pending member, so the callback sees boundary 0, before the first generation, or on a running one, from its next boundary. Unregistering takes effect at the next boundary. `origin` names the caller in the evidence: a short identifier the caller chooses, such as `orbit` or `operator`, of 1 to 64 characters from letters, digits, `_`, `-`, `.` and `:`; `swarm` is reserved for the runtime's own callbacks. It raises:

- `ValueError` for an invalid origin, and for a member the seam does not support ([Supported members](#supported-members)), naming the member and why;
- `RuntimeError` for a member that has ended, once the swarm is stopping, as `start()` raises then, and for a member found unable to be held.

**At a boundary**, the callbacks run in the order they were registered, in the member's task and span, while the member is held. Each receives a `Boundary`:

- `index` counts the boundaries of the member's own loop from 0.
- `after` is what the conversation ends with: `start` at index 0; `tools` after a tool result; `reply` after an assistant message with no tool calls, which only an agent that continues after a reply reaches (an `on_continue` that returns the state); `user` after anything else (react's continue prompt, an incorrect-answer message, an operator's redirect after an interrupt).
- `state` is a detached deep copy of the member's messages and output. The first callback gets the member's state; each later one gets the state as the callbacks before it left it. Changing the copy in place changes nothing; a callback changes the member only by what it returns.

A callback returns:

- **`None`**: continue with the state as it received it.
- **A list of messages**: the conversation the member continues with. Messages keep their ids, and a new message gets one when it is built, so a message with no id, or two with the same id, is an error in the callback.
- **`"end"`**: end the member. The remaining callbacks do not run, and the member ends `stopped`, with the reason `ended by <origin>` in its lever's message.

A callback may wait as long as it likes (for a scheduler's go, an operator's resume, new work), and it may call models and tools of its own. Its time is held time ([Time](#time)); its model calls run under the member's limit nodes, so they count towards the member's token and cost limits and its ledger line, as an approval judge's do.

**When the chain finishes**, the runtime compares the final state with the member's live conversation, by message id and content hash:

- unchanged: the member continues as it was;
- the live messages unchanged and messages added after them: they are appended to the live list;
- anything else (a message removed, replaced, reordered or inserted before the end): the live list is replaced.

The runtime writes the evidence ([Evidence](#evidence)) and returns to the gate.

**Exceptions and cancellation.** An exception a callback raises propagates in the member's task, as an error from the member's agent does: the member ends `errored`, and the error leaves the swarm as swarm-api.md specifies for a foreign error. A cancellation from outside (the sample's limits or interrupt, the hard stop) cancels the callback where it waits, and the member ends as it would elsewhere.

**Composition.** Every caller's callbacks run at every boundary, so callers compose without knowing each other: an operator's pause callback registered after ORBIT's scheduler holds the member after the scheduler has activated it, and a scheduler activation waits for the operator's resume. A caller that wants to act only at some boundaries returns `None` at the others.

**What callers build on it.** Nothing here is a scheduling API; callers bring their own:

- **ORBIT's activation** is its own callback per member that signals "parked" and awaits the scheduler's go, so `activate()` sets that member's go and awaits its next park or its end (`MemberEnded` through `events()`). `model_step` is one boundary; `completion` keeps going until a boundary with `after == "reply"`. Snapshots read `Boundary.state`, interventions return the new conversation, and the scheduler stays ORBIT's code. inspect_swarm ships no `activate()` and no `round_robin`: each is a few lines over this contract in the caller's code, and as swarm API they would bring back the problem of an activation that must wait past another holder's pause, which composition by callbacks does not have.
- **An operator's pause** is a callback that waits while paused; its "stop this member" returns `"end"`.
- **Waking a persistent member** is a callback that, at a boundary with `after == "reply"`, waits for the next piece of work and returns the conversation with it appended.
- **Push delivery** would register a runtime callback with origin `swarm` that returns the conversation with a reminder or injection appended.

### The inspect_ai change: a turn gate and a compaction reset

**The gate.** A new private module, `src/inspect_ai/agent/_channel/gate.py`, holds a `ContextVar[TurnGate | None]` and the `turn_gate(gate)` context manager that sets it. It is a seam for a runtime that runs agents, as inspect_swarm is, in the way the private `background_target` seam serves its background owner.

- **`agent_channel()`** reads the current gate when it creates a channel and keeps it on the channel. Every loop opened in the block gets the same gate, nested loops included; the gate receives the channel, so the runtime decides which loop it holds, and inspect_ai stays unaware of swarms. The inert channel never has a gate.
- **`before_turn(messages)`**, when its channel has a gate, first awaits `gate(channel, messages)`:
  - `None`: the conversation is unchanged.
  - A list that starts with the same message objects as `messages`: an append. `before_turn()` extends `messages` with the rest.
  - Any other list: a rewrite. `before_turn()` assigns it in place (`messages[:] = result`), so the caller's list, which react extends and keeps, holds it, and marks the channel rewritten. If `messages` is not a `list`, it raises `TypeError` saying that a gate that rewrites the conversation needs the loop's own message list.

  Either way a limited list checks the message limit for the growth. An exception the gate raises propagates from `before_turn()`. Then it drains the queue and returns the operator's messages as today, so an operator's message posted during a hold reaches the turn it was meant for.
- **`after_cancel()`** is unchanged, since react goes on to `before_turn()` after it.

**The compaction reset.** The handler that `compaction()` returns gains `reset(prefix)`, also added to the `Compact` protocol: it clears the handler's kept input, the ids it has folded in, its token baseline and its memory-warning flag, in place so that a checkpointer tracking that state keeps tracking it, and replaces its prefix. After `before_turn()`, both react loops check whether the channel was marked rewritten (a private read that clears the mark), and if so call `compact.reset()` with the prefix derived from the new conversation, as `_agent_compact()` derives it. The next `compact_input()` then starts from the rewritten conversation: below the threshold it sends it as it is; above, it compacts it, which for a summary strategy is another model call. An append needs no reset, since the handler adds messages it has not seen.

Without a gate nothing changes: no `react()` behaviour, ACP, log-schema or public API change beyond `reset()`.

### The member's gate

Each member's run gets one `MemberGate` (new module `src/inspect_swarm/_seam.py`). The member wrapper installs it around the agent call with `turn_gate(gate)` (`_member.py`, around `_call_agent`), inside the member's scopes, so the gate sees every agent channel the member's work opens: its own loop, its subagents' loops and its background tasks' loops.

**Binding the member's own loop.** On each call the gate first decides whether the channel is the member's own loop:

- If `MemberContext.loop` is unbound, this channel becomes it, and `acp_bound` is recorded as `swarm_tools()` records it today. For `react()` and `deepagent()` members this is the top-level loop, which opens its channel and calls `before_turn()` before it can start a subagent.
- If `MemberContext.loop` is this channel, it is the own loop.
- Otherwise (a subagent, a handoff, a background task's loop, or a channel other than the one `swarm_tools()` bound first) the gate returns `None` at once.

`swarm_tools()` keeps binding `MemberContext.loop` when it resolves first, and now also sets a separate `MemberContext.tools` flag, which the `tools_unwired` check reads instead of the binding, so that check is unchanged. Binding happens once per member run and is never undone.

**At each boundary of the own loop:**

1. If the swarm is stopping, end the member ([Stopping](#stopping)).
2. If no callback is registered, return `None` with no `await`.
3. If any of the member's background work is running, refuse ([Supported members](#supported-members)).
4. Hold: mark the member held, pause its time limit, recheck the swarm's waits, and open inspect_ai's `sample_wait()`. Run the callbacks in a cancel scope of the gate's own, which a stop cancels.
5. Release: close the wait, resume the time limit, clear the mark, recheck the waits, write the evidence, and return the result: `None`, the live list with the added messages appended, the rewritten conversation, or for `"end"`, the member's end.

### Stopping

- **A stop ends a member at its boundary.** `stop(reason)`, in its synchronous first part, cancels every gate's running callbacks, and from then on every boundary of any member ends it. The gate ends a member by closing its lever and raising the lever's limit error, which the member wrapper already classifies as the lever's: the member ends `stopped`, as a member the stop reaches between calls does, with its last output as its output. No generation follows, so a cached reply or cached tool calls cannot run after the stop, and the member never returns normally to be read as `submitted`.
- Held members therefore end at once; they need no grace period or hard stop and leave no cancelled loop in the ledger. A change a cancelled callback had not returned is never applied, and the `boundary` event records the outcome `stop`.
- **`"end"`** takes the same path with the reason `ended by <origin>`, and the rest of the swarm goes on.
- **Outside cancellations** (the sample's limits, an operator's interrupt of the sample, the hard stop) cancel the callbacks like any other `await`.

### Time

| | Held time |
|---|---|
| The member's time limit | does not count: the limit pauses while the member is held |
| The swarm's time cap | counts |
| The sample's time limit | counts |
| The sample's working time | a sample wait: working only while other known work runs in the sample; a fully held swarm stops it |
| The metrics' `running_time` | counts; `held_time` (new) gives the held part |
| `swarm_wait`'s idle time | not idle: a held member has no wait |

- **The member's time limit pauses.** A member time limit becomes a `SwarmTimeNode`, a subclass of Inspect's time limit as the swarm's token, turn and message nodes subclass Inspect's. When the gate holds the member, the node sets its scope's deadline to infinity; when it releases, it moves its start time on by the held time and re-derives the deadline, so its deadline and its reported usage both count only time the member was not held. It raises Inspect's time limit error as before, so the member's status is still `member_limit:time`. The swarm's time cap and the sample's time limit are deadlines for the whole run and keep counting.
- **Working time.** The callbacks run inside `sample_wait()`, so the hold is waiting time unless other known work runs in the sample. Model calls a callback makes there are not known work either: a defence's model call during a hold counts as waiting, which slightly understates working time when callbacks call models.
- **`swarm_wait`.** A held member counts as not active: the all-waiting check becomes "no member is running, not waiting and not held", and the gate rechecks the waits when it holds and releases. So when every other member is waiting or held, the waits return (`all_waiting`) rather than wait for members that will not move until their callbacks return.

### Supported members

| Member | Seam |
|---|---|
| `react()`, `react(submit=False)`, `deepagent()` without background dispatch | Its top-level loop. A loop that ends at a submission or a tool-free reply has no boundary after it; to be held after a reply, the agent must continue after one (an `on_continue` that returns the state), which is #116's to decide. |
| Nested loops: subagents, handoffs, `as_tool()` agents, `deepagent()`'s synchronous `general()` subagents | Never held on their own; they run inside a turn of the member's own loop, and a hold takes effect when that turn ends. |
| `deepagent(background=True)` or an integer `background` | `on_boundary()` raises `ValueError`, detected from the agent's registry name, `inspect_ai/deepagent`, and its `background` argument, as M1 refused these members. Holding the top-level loop would leave its background subagents generating, which breaks every use's guarantee that a held member does nothing. |
| A custom agent that calls `background()` itself | Checked at every boundary with callbacks, before the member counts as held: if any of its background tasks is running, the gate does not hold it, and the member ends `errored` with a `RuntimeError` naming the member and saying that a member with background work running cannot be held. Tasks that have finished do not matter, and none can start while the loop is held. |
| Bridged members, declared or in the `inspect_swe/` namespace | `on_boundary()` raises `ValueError`: the CLI runs its own turns, and no agent channel is opened. |
| Members found unable to be held: a bridged member found by its first bridged call, or an agent with no agent channel of its own | Found at a model call the member makes, with a callback registered, before its own loop has ever reached the gate, in the same node checks that find bridged members. The member is cancelled and ends `errored` with a `RuntimeError` naming it and the reason, as a bridged member that starts background work ends today; a later `on_boundary()` raises. |
| A nested swarm inside a member | Its members have gates of their own, installed by its own runtime; the outer member's gate never binds their loops. |

### Evidence

Two `InfoEvent`s of type `turn`, in the member's span, with the swarm's evidence fields (`version`, `type`, `kind`, `member`, `span_id`), written only at boundaries where callbacks ran:

- **`boundary`**, once per such boundary: `boundary` (the index), `after`, `origins` (the callbacks' origins in order), `held_for` (seconds), `outcome` (`continue`, `end` or `stop`), `messages` before and after.
- **`changed`**, once for each callback whose result differs from the state it received: `boundary`, `origin`, and the change, found by diffing on message id:
  - `appended`: the ids and hashes of messages new at the end;
  - `removed`: the ids of messages no longer there;
  - `replaced`: for each message kept under its id with new content, its id and its hashes before and after;
  - `moved`: whether the kept messages changed order;
  - `applied`: whether the member continued with the change (false when a later callback ended it, or the stop came first).

A hash is the SHA-256 of the message as the log serializes it, its id excluded, so analysis can match a message in the conversation or in a `ModelEvent`'s input to the change that made it, where Inspect has not combined or compacted it. Returning the same content is no change and writes no `changed` event.

**Ids and hashes, not content.** The content of an appended or replaced message is in the member's conversation and in the next `ModelEvent`'s input, and a removed message's is in earlier ones. Recording it again would grow the log with every activation for a defence that rewrites much of the conversation each time. A caller that wants its own content record writes its own event; ORBIT already writes its own.

**One event per callback**, with the origin given at registration, rather than one per operation inside a callback. A caller that wants changes told apart (attack input, then a defence) registers a callback for each, in the order they must run. `changed` takes the place of swarm-orbit.md P5's `intervened` event: the same appended, removed and replaced ids and hashes, written by the runtime for every caller rather than by ORBIT's interventions; P5's `committed: false` for operations staged at a stop becomes `applied: false`, since staging is the caller's.

The metrics gain two fields per member, `holds` (boundaries at which callbacks ran) and `held_time`, which default to 0 so that metrics written before them load.

### What this means for swarm-orbit.md

swarm-orbit.md is revised separately once this design settles. In short, under this contract:

- **P4.** The seam replaces `swarm_on_continue()`: nothing to wire, every boundary covered. `activate()`, the quanta, `round_robin`, `ActivationResult`, the `schedule` events and the read cutoff become ORBIT's code over `on_boundary()`. A member is released only by continuing or by `"end"`, so the stop-cause change P4 proposed for a normal return is not needed, and a conversational member ended at a stop is `stopped`.
- **P5.** Staged copies, `snapshot()`, `append` and `edit` become ORBIT's code: a snapshot is `Boundary.state`, and an intervention returns the new conversation. `changed` replaces `intervened`. Whether controllers may read member text or add user- or system-role input (open question 1) stops being a swarm decision.
- **P2, option (c).** Stamping a read's result message with the read id becomes a callback that returns copies with metadata, covering the member's own loop only, as before.

## Alternatives considered

**An `on_continue` hook in inspect_swarm under the same contract** (a `swarm_on_continue(inner)` that the member's author wires in, running the callbacks after each completed turn). Its case: no new inspect_ai seam, and ORBIT's member builder already wires its own hooks. Against it:

- It misses the first turn and every recovery turn, so attack input before the first generation needs a second path (`start(input=)`), and a boundary after a recovered overflow cannot be held.
- It reaches only members whose author wired it. An operator pausing an arbitrary member, or push delivery to every member, would need every member author to wire it in, which is what the removal of notices ended (decision: Ransom, 2026-10-27).
- It must compose with the author's own `on_continue`, and their order matters.
- It still needs an inspect_ai change for any caller that rewrites the conversation: react's compaction ignores an `AgentState` returned from `on_continue` exactly as it would ignore the gate's rewrite ([Compaction](#compaction-keeps-its-own-view-of-the-conversation)). So "no inspect_ai change" holds only for callers that never rewrite, and ORBIT's defences rewrite.

Rejected: the gate covers every turn of every `react()`-based member, wired or not, and the compaction reset is needed either way.

**Swarm awareness only: a `held()` context manager for callers' own hooks.** The swarm would expose `async with member_held(): ...`, and ORBIT's own `on_continue` would enter it while it waits, so the runtime knows the member is held. The smallest swarm surface. But it keeps every gap of the hook above, ending a member needs its own path, and nothing records the changes callers make. Rejected.

**A swarm-owned scheduling API** (`hold()`, `resume(input=)`, `wait_held()`, and `activate()` on top). It puts one caller's policy in the swarm, and holders compose badly: an activation must ask for "the boundary after this one" while another holder's pause still holds this one, and a resume with input must say whose input comes first. Callbacks compose by running in order. Rejected.

**A hold at the model call**, in a generate hook as inspect_ai's `pause --now` holds one. It would also hold bridged members and every loop. But a model call is not a turn boundary: it comes mid-turn for a subagent, a parallel call, compaction and each retry, so a hold there leaves tool calls in flight, and it cannot change the conversation. Rejected; it stays the only route to pausing a bridged member, if #119 wants that.

**A public gate on `react()`** (`react(turn_gate=...)`). An explicit API, but it is wiring again. Rejected.

**`Steer` items posted between turns.** The planned agent-channel item can add messages at a boundary, but not hold one or rewrite, and every post meets ACP's binding. It stays urgent steering's mechanism.

**Content in the change record.** Self-contained evidence, but redundant with the conversation and `ModelEvent` inputs, and quadratic in log size for a defence that rewrites the conversation every activation. Rejected for ids and hashes.

**Leaving the stop to the closed lever.** A released member would end at its next model call. But a cached reply is returned before the lever's check, so a released `react(submit=False)` member can answer from the cache and end `submitted` after the stop. Rejected for ending the member at the boundary.

## Compatibility and migration

- **inspect_ai.** Without a gate nothing changes. The gate is a private module; `reset()` is added to the compaction handler and the `Compact` protocol, so a custom class that a type checker matched against `Compact` must add it. inspect_swarm needs an inspect_ai release with both.
- **inspect_swarm's API** gains `on_boundary()` and `held` on the handle, and `Boundary` and `BoundaryCallback`, additions only. `MemberHandle` is a protocol that only the runtime implements. `SwarmEvent` and `MemberState` are unchanged.
- **Behaviour without callbacks.** Two changes, both for every member:
  - A member that reaches a boundary after the swarm began to stop ends there, `stopped`, rather than at its next model call; the status is the same, and a cached call can no longer run after the stop.
  - The member's own loop is bound when its first agent channel calls the gate, before any tool resolves, rather than at the first `swarm_tools()` resolution. For `react()` and `deepagent()` members it is the same loop. A custom agent that resolves `swarm_tools()` before it opens a channel keeps that binding, and the gate follows it. `tools_unwired` reads the new flag and is unchanged.
- **Member time limits** are the swarm's subclass of Inspect's: logged, rebuilt and enforced as before, and identical while no callback holds the member.
- **Logs.** New `InfoEvent` kinds of a new type, and two metrics fields with defaults; logs written before load unchanged. The result record and the ledger are unchanged. The viewer has no generated types for these.

## Security

- **Callbacks are trusted code.** They are eval-author Python in the runner process, like controllers, and see the member's full state, peer text in tool results included. What they may read, and what they may put in a conversation and in which role, is the caller's policy: a callback that moves one member's text into another's input is eval code doing what eval code can always do. The seam's part is to make it visible: every change is a `changed` event with the caller's origin and the hashes of what was added or replaced.
- **No member reaches the seam.** No tool exposes `on_boundary()`, and the gate's `ContextVar` is set only by the member wrapper. Trusted code in the runner process could set another gate, as it could set `background_target`.
- **Model output in the evidence.** The gate and the events read message types, ids and hashes only, never text, so nothing a member writes appears in the swarm's evidence. `after` follows from the model's own output (a reply or tool calls), as a member's `submitted` does.
- **Holding does not stop the sandbox.** Processes a member left running keep running while it is held.
- **A callback that never returns** holds its member until the swarm's time cap or the sample's limits end the swarm, as with any controller that waits forever.

## Testing

All with mockllm, no network or Docker.

- **inspect_ai** (`tests/agent/test_channel/test_channel.py`, `tests/agent/test_agent_react.py`):
  - `before_turn()` awaits the gate before draining, with the channel and the messages; `None` leaves the list unchanged; a list that extends it by identity is appended; any other list replaces it in place and marks the channel; past the message limit both raise the limit error; a rewrite of a tuple raises `TypeError`; an exception from the gate propagates;
  - a nested `agent_channel()` gets the same gate; the inert channel never calls one; with no gate, `before_turn()` is unchanged;
  - a `react()` loop calls the gate before its first generation, after a turn with tool calls, after a recovered context overflow (a mock output with `stop_reason="model_length"`), and after an interrupt recovery;
  - with `react(compaction=CompactionTrim(...))` below its threshold, and with `deepagent()`'s default compaction: a rewrite that replaces a message under the same id, and one that deletes a message, reach the next model input as rewritten; an append does not reset the handler.
- **inspect_swarm** (`tests/test_turn_seam.py`, new, on asyncio and trio through `tests/conftest.py`):
  - a callback registered on a pending member gets boundary 0 with `after == "start"` before any model call, and the member is `held` while it waits; a returned conversation with an added user message is in the next model input;
  - `after` is `tools` after a tool turn, and `reply` for a `react(submit=False)` member whose `on_continue` returns its state;
  - two callbacks run in order, the second receives the first's result, and a pause callback registered second holds the member after a scheduler callback lets it go; a scheduler built on callbacks alternates two members turn by turn while a third caller pauses one of them and later resumes it;
  - unregistering takes effect at the next boundary; in-place changes to the copy change nothing; duplicate ids in a result raise and end the member `errored`;
  - `"end"` ends the member `stopped` with no further model call, and the rest of the swarm goes on;
  - a stop while held, and a stop before a member's next boundary, each end the member `stopped` with no further generation, with a cached plain reply and with cached tool calls waiting for it, and its ledger line is not `cancelled`;
  - a member time limit does not run while held: a member with `time_limit(0.5)` held for 1 s, then working 0.3 s, is not stopped; held for 1 s and working 0.6 s, it ends `member_limit:time`;
  - every member held: the sample's working time stops while the held wall clock passes (a real sample, through `eval_async()`, as `tests/test_binding.py` runs one);
  - a member in `swarm_wait` is released with `all_waiting` when the only other running member is held;
  - refusals: `ValueError` from `on_boundary()` for a declared bridged member, for `deepagent(background=True)` and `background=4`, and for an invalid origin; `RuntimeError` for an ended member and while stopping; a custom agent whose tool starts `background()` after a callback was registered ends `errored` at its next boundary while the task runs, and is held normally once it has finished; a custom agent that generates without opening an agent channel ends `errored` at its first model call;
  - a `deepagent()` member's synchronous subagent is never held, and the callback runs when the subagent's tool call returns;
  - the `boundary` and `changed` events: one `changed` per changing callback with its origin, an append, a deletion, a same-id replacement and a reorder each recorded as such, the hashes matching the messages in the next `ModelEvent`'s input, `applied: false` after a later `"end"`, no event for a callback that returns the same content, and no text in either event; the `holds` and `held_time` metrics; `tools_unwired` still names a `react()` member in a shared-memory swarm that was not given `swarm_tools()`.

## Implementation plan

1. **inspect_ai: the turn gate and the compaction reset.** `src/inspect_ai/agent/_channel/gate.py` (new), `channel.py` (`before_turn()`, the channel's gate and rewrite mark), `__init__.py` (`agent_channel()` reads the gate), `src/inspect_ai/model/_compaction/_compaction.py` and `types.py` (`reset()`), `src/inspect_ai/agent/_react.py` (reset after a rewrite, in both loops); the inspect_ai tests above.
2. **inspect_swarm: the seam.** Needs step 1 in an inspect_ai that inspect_swarm can depend on.
   - `src/inspect_swarm/_seam.py` (new): `MemberGate`, `Boundary`, `BoundaryCallback`, binding, the callback chain, the diff and its evidence.
   - `src/inspect_swarm/_member.py`: install the gate around the agent call; member time limits as `SwarmTimeNode`; the background-dispatch check for `on_boundary()`.
   - `src/inspect_swarm/_controller/_control.py`: `on_boundary()` and `held` on `MemberRun`, the stop's cancellation of callbacks, `MemberWiring` from the tools flag.
   - `src/inspect_swarm/_budget.py`: `SwarmTimeNode`; ending a member through its lever at a boundary; the `tools` flag on `MemberContext`; finding a member that cannot be held in the node checks.
   - `src/inspect_swarm/_bus/_tools.py`: set the tools flag; `src/inspect_swarm/_bus/_wait.py`: held members are not active.
   - `src/inspect_swarm/_evidence.py`, `src/inspect_swarm/_metrics.py`, `src/inspect_swarm/__init__.py`.
   - Docs: `docs/controllers.qmd` (boundary callbacks), `docs/members.qmd` (time limits while held), `docs/analysis.qmd` (the events and metrics).
   - Tests: `tests/test_turn_seam.py`.

## Open questions

1. **The status of a member a callback ends.** `"end"` always gives `stopped`, so a persistent or conversational member ended after its last reply is not a submission, and under swarm-scoring.md's rule only submissions are answers. The alternative is `"end"` with a status, letting a callback end a member `submitted` after a reply. Recommendation: `stopped` only here; #116, which decides how a persistent member's answers count, adds a submitted ending if it needs one, with the rule for which reply is the answer.

## Not this design

- **Holding a member's background work**, by gating its background tasks' loops, on top of the per-member background owner ([swarm-background-members.md](swarm-background-members.md)), or quiescing it before a hold.
- **Pausing bridged members**, through a hold at the model call or inspect_swe's restart with `--resume`.
- **The closed lever and cached calls inside a turn.** After a stop begins, model calls inside a turn that is already running (a subagent's, compaction's) can still be served from the cache, since a hit is returned before the limit checks; the seam ends only the member's own loop, at its next boundary. A fix belongs in Inspect's limit checks.
- **Idle and woken events**, for persistent members (#116).
- **Updating swarm-orbit.md** to this contract, in its own revision.
