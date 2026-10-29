# Inspect Swarm: the turn seam

## Overview

The turn seam hands a member's state to eval-author code at every turn boundary of the member's own agent loop: before its first generation and before each one after it, with no model call or tool call of the loop in flight. The code is a *boundary callback*, registered on the member's handle by a controller (ORBIT's scheduler, a coordinator that wakes a persistent member, an operator's pause). It may wait as long as it likes, then continue the member unchanged, continue it with a replaced conversation, or end it; and any code holding the handle can end the member while a callback still waits. While callbacks run, the member is *held*, and the swarm knows it: its time limit does not run, `swarm_wait` does not count it as active, and a stop ends it at once with status `stopped`. Work a callback does counts as working time; a callback marks its own idle waits with Inspect's `suspend_working_limit()`.

The boundary is a small hook in inspect_ai's agent channel: a private turn gate that `before_turn()` awaits, which the swarm installs for every member. So every `react()` and `deepagent()` member has the seam, its first turn and the turns that recover from a context overflow or an interrupt included, without its author wiring anything; nested loops pass through untouched. A member waiting for an operator's redirect after an interrupt is not at a boundary and is not held. When a callback rewrites the conversation, the runtime drops compaction summaries made from the history it changed and react resets its compaction state, so the model sees the rewrite; a rewrite must keep a user message. Every change a callback makes is recorded as one event, with the callback's origin and the ids and hashes of the messages it added (with their positions), removed and replaced, not their text. Members known to be unholdable (bridged CLIs, members with background work) are refused with an error, and a member with callbacks that ends without ever reaching a boundary, because it has no agent loop the seam can see, ends `errored`, so its unsupervised result is never an answer. What callers do at the boundary (scheduling, snapshots, attack input, defences, waking with new work) is their own code and their own designs.

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
- The runtime's knowledge of a held member: status, time limits, `swarm_wait`'s all-waiting release, and release at a stop, of the swarm or of that member alone.
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
- **`before_turn()`** drains the channel's queue and returns the operator's messages, and blocks only when neither the conversation nor the drained queue has a user message (`src/inspect_ai/agent/_channel/channel.py:432-453`): it then waits for the operator's input, which nothing in a swarm sends. react extends the list it passed in with what it returns. Nothing else runs there.
- **`after_cancel()`.** After an operator's interrupt, react calls `after_cancel()`, which returns repairs for the interrupted tool calls and waits for the operator's redirect when none is queued (`channel.py:455-479`), before the loop reaches `before_turn()` again. Only the loop ACP binds (the first channel offered, `__init__.py:97-143`) can be interrupted, so in a swarm at most one member, with its operator present.
- **`agent_channel()`** creates a channel, offers it to the sample's ACP transport (first binder wins), and sets it as the current channel for its block (`src/inspect_ai/agent/_channel/__init__.py:97-143`). A nested loop, such as a subagent called as a tool, opens a channel of its own (`:106-117`). Outside any block the current channel is an inert singleton (`:47-94`).
- **Who opens a channel.** In inspect_ai, only `react()` and `react(submit=False)` (a search for `agent_channel()` and `before_turn` in `src/inspect_ai`). `deepagent()`'s top-level loop is a `react()` (`src/inspect_ai/agent/_deepagent/deepagent.py:230-240`), and its subagents are `react()` loops running inside its tool calls. inspect_swe's bridged agents open none (the same search in inspect_swe `src` at `661d6bf` finds nothing).
- **Conversations are mutable lists.** A member's messages may be inspect_ai's limited list, whose slice assignment checks the message limit for the growth (`src/inspect_ai/util/_limited_conversation.py:41-52`).

### Compaction keeps its own view of the conversation

- react creates one compaction handler per loop before the loop starts, with the system messages and the sample input as its prefix (`_react.py:242`, `:486`, `_agent_compact` at `:690-713`). `deepagent()` turns compaction on by default.
- The handler keeps the input it last sent and the ids of the messages it has folded into it, and on each call adds only the messages whose ids it has not seen (`src/inspect_ai/model/_compaction/_compaction.py:99`, `:167-172`, `:276-296`). Below the threshold it sends that kept input.
- So a conversation rewritten between turns, by an `on_continue` that returns an `AgentState` or by anything else, does not reach the model when compaction is on: a message replaced under the same id, or deleted, stays in the next input as it was.
- **Summaries live in the conversation.** A summary strategy (`CompactionSummary`, and `CompactionAuto` when it falls back to one, as it does on providers without native compaction) returns its summary as a user message with `summary` in its metadata (`src/inspect_ai/model/_compaction/summary.py:144-162`), and react appends it to the member's messages (`_react.py:762-765`). The next summary compaction takes only the latest such message and what follows it (`summary.py:90-113`), so a summary stands in for everything before it, whatever the handler keeps. `CompactionTrim`, `CompactionEdit` and native compaction add no message to the conversation.

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
- With the park wrapped in `sample_wait()` (which `suspend_working_limit()` returns): one member held for 1 s while the other worked gave total 1.94 s, working time 1.47 s on asyncio, the held time counting as working while the other member's tool calls ran and as waiting after it ended; both held for 1 s gave total 1.82 s, working 0.73 s.
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
class TurnGate(Protocol):
    async def before_turn(
        self, channel: AgentChannel, messages: Sequence[ChatMessage], state: AgentState | None
    ) -> bool: ...   # True when it rewrote `messages` in place
    def closed(self, channel: AgentChannel) -> None: ...   # the channel's block has exited

@contextmanager
def turn_gate(gate: TurnGate | None) -> Iterator[None]: ...   # the gate for channels opened in the block


# inspect_swarm
@dataclass(frozen=True)
class Boundary:
    member: str
    index: int                                          # 0 before the loop's first generation
    after: Literal["start", "tools", "reply", "user"]   # what the conversation ends with
    messages: list[ChatMessage]                         # a detached copy, a plain list
    output: ModelOutput                                 # a detached copy of the loop's output


BoundaryCallback = Callable[[Boundary], Awaitable[list[ChatMessage] | Literal["end"] | None]]


class MemberHandle(Protocol):                           # the existing handle, plus:
    @property
    def held(self) -> bool: ...                         # its callbacks are running
    def on_boundary(self, callback: BoundaryCallback, *, origin: str) -> Callable[[], None]: ...
    def end(self, *, origin: str) -> None: ...         # end it now if held, else when its turn ends
```

### The callback contract

**Registering.** `MemberHandle.on_boundary(callback, origin=...)` registers a callback for the member and returns a function that unregisters it. Any code holding the handle registers: a controller's `run`, and through it whatever the controller hosts (ORBIT's scheduler, an operator surface). It may be called on a pending member, so the callback sees boundary 0, before the first generation, or on a running one, from its next boundary. Unregistering takes effect at the next boundary. `origin` names the caller in the evidence: a short identifier the caller chooses, such as `orbit` or `operator`, of 1 to 64 characters from letters, digits, `_`, `-`, `.` and `:`; `swarm` is reserved for the runtime's own callbacks. It raises:

- `ValueError` for an invalid origin, and for a member the seam does not support ([Supported members](#supported-members)), naming the member and why;
- `RuntimeError` for a member that has ended, and once the swarm is stopping, as `start()` raises then.

**At a boundary**, the callbacks run in the order they were registered, in the member's task and span, while the member is held. Each receives a `Boundary`:

- `index` counts the boundaries of the member's own loop from 0.
- `after` is what the conversation ends with: `start` at index 0; `tools` after a tool result; `reply` after an assistant message with no tool calls, which only an agent that continues after a reply reaches (an `on_continue` that returns the state); `user` after anything else (react's continue prompt, an incorrect-answer message, an operator's redirect after an interrupt).
- `messages` and `output` are detached deep copies of the loop's own state, as the loop passes it to `before_turn()` (below); the runtime never rebuilds an output from messages. The first callback gets the member's; each later one gets the messages as the callbacks before it left them, and the same output. Changing the copy in place changes nothing; a callback changes the member only by what it returns.
- `messages` is a plain `list`, copied message by message, never an `AgentState` or Inspect's limited list: `AgentState`'s constructor, its `messages` setter and its `__deepcopy__` all build a list that checks the active message limits (`src/inspect_ai/agent/_agent.py:40`, `:50`, `:87-90`), so staging through it would refuse an intermediate conversation over the member's message cap before a later callback could trim it or end the member, and would refuse an in-place append to a copy that is discarded. The chain stages freely; the message cap is enforced once, when the gate applies the final result to the live list ([When the chain finishes](#the-callback-contract)). Model and tool calls a callback makes stay under the member's limits as before.

A callback returns:

- **`None`**: continue with the messages as it received them.
- **A list of messages**: the conversation the member continues with. Messages keep their ids, and a new message gets one when it is built, so a message with no id, or two with the same id, is an error in the callback. The list must hold at least one `ChatMessageUser`: one without (empty, system-only, or only assistant and tool messages) is an error in the callback too, a `ValueError` naming the member and the origin, whether or not an operator's message is queued in the channel. Without that guard, `before_turn()` would wait after the release for operator input that nothing in a swarm sends, with the member no longer held and its time limit running, until a grace period ran out and cancelled it. Every member's conversation starts with a user message (`start()`'s preamble, `_control.py:324-331`), and no use removes the last one; a rewrite that keeps any user message is unaffected.
- **`"end"`**: end the member. The remaining callbacks do not run, and the member ends `stopped`, with the reason `ended by <origin>` in its lever's message. It is always `stopped`, never a submission, even after a reply (decision: Ransom, 2026-10-28); persistent members (#116) add a submitted ending, with its rule for which reply is the answer, if they need one.

A callback may wait as long as it likes (for a scheduler's go, an operator's resume, new work), and it may call models and tools of its own. Its time is held time for the member's time limit ([Time](#time)), and its model calls run under the member's limit nodes, so they count towards the member's token and cost limits and its ledger line, as an approval judge's do.

**Working time is the callback's to mark.** The seam does not wrap callbacks in a wait: model calls, sandbox commands and tool calls a callback makes are working time like any other. A callback marks its own idle waits with Inspect's public `inspect_ai.util.suspend_working_limit()`, around the `await` that waits for a go, a resume or new work, and an author who wants some work left uncharged (a defence treated like a monitor, say) wraps it the same way. A callback that waits without it has its hold counted as working time, which can trip the sample's working limit while nothing works.

To make that rule hold whatever other callbacks do, the gate runs each callback inside inspect_ai's known-work span (`known_work()`, `src/inspect_ai/_util/working.py:609-621`). Inspect subtracts an open wait anywhere in the sample unless known work overlaps it (`:1-8`), and callbacks run outside any tool call's work span, so without that span one member's marked wait would hide another member's unmarked wait or computation. Inside it, a callback's time counts as working; its own `suspend_working_limit()` suspends its own span for the length of the wait (`SampleWait` holds the work of the context that opened it, `:536-557`), and nothing else's.

**When the chain finishes**, the runtime compares the final state with the member's live conversation, by message id and content hash:

- unchanged: the member continues as it was;
- the live messages unchanged and messages added after them, none of them a system message: they are appended to the live list (`messages.extend(...)`);
- anything else (a message removed, replaced, reordered or inserted before the end, or a system message added anywhere): the live list is replaced in place (`messages[:] = ...`), so the loop's own list, which react extends and keeps, holds it.

An added system message makes the change a rewrite, even at the end, because react's compaction handler keeps the system messages in a prefix it saved when the loop started (`_agent_compact` at `_react.py:690-713`) and after native compaction restores system messages from that prefix alone (`_compaction.py:220-230`), since the providers return none of their own: Anthropic returns the compacted message and a continue prompt (`src/inspect_ai/model/_providers/anthropic.py:1215-1220`), and OpenAI's `chat_messages_from_compact_response()` (`src/inspect_ai/model/_openai_responses.py:2452`) strips developer messages for the handler to restore. An appended system message would be missing from that prefix and dropped at the next native compaction (`CompactionNative`, or `CompactionAuto` on a provider with native compaction, as `deepagent()` defaults to). As a rewrite it marks the channel, and the compaction reset (below) rebuilds the prefix from the new conversation. Appending user, assistant or tool messages stays an append, which the handler adds incrementally.

The gate makes that change to the list the loop passed to `before_turn()`, so a member's limited list checks the message limit for the growth, as any append does, before it changes anything (`src/inspect_ai/util/_limited_conversation.py:26-29`, `:41-50`). If the loop passed a list that is not a `list` (a tuple from a custom loop), a change raises `TypeError` saying that a callback that changes the conversation needs the loop's own message list.

**Stale summaries are dropped from a rewrite.** A compaction summary is derived from the messages before it, so once a rewrite changes those messages it describes a conversation that no longer exists, and a later summary compaction would start from it and never see the rewrite. So before it replaces the live list, the runtime finds the longest run of messages at the start of the final list that match the live conversation one for one, by id and hash. Every message after that run that carries the `summary` metadata key and is unchanged from the live conversation (same id and hash) is removed from the final list. Summaries inside that run are kept: everything they were derived from is unchanged. So is a summary message the chain added or replaced: that text is a caller's. The removal is written as a `changed` event with origin `swarm` after the callbacks' own ([Evidence](#evidence)), and the user-message check above is made again on the result, failing the member the same way if a stale summary was its only user message. The compaction reset that follows (below) then rebuilds the model's input from the rewritten source.

The runtime writes the evidence ([Evidence](#evidence)) only after the change has reached the live list, or failed to: a change the message limit refuses leaves the conversation as it was, its evidence says `applied: false` with the outcome `rejected`, and the limit's error propagates, so the member ends `member_limit:message` (or as the sample's limit ends it), as today.

**Exceptions and cancellation.** An exception a callback raises propagates in the member's task, as an error from the member's agent does: the member ends `errored`, and the error leaves the swarm as swarm-api.md specifies for a foreign error. A cancellation from outside (the sample's limits or interrupt, the hard stop) cancels the callback where it waits, and the member ends as it would elsewhere. Either way no change from the chain is applied, and the evidence says so ([Evidence](#evidence)).

**Composition.** Every caller's callbacks run at every boundary, so callers compose without knowing each other: an operator's pause callback registered after ORBIT's scheduler holds the member after the scheduler has activated it, and a scheduler activation waits for the operator's resume. A caller that wants to act only at some boundaries returns `None` at the others.

**Ending a member from outside the chain.** Because the chain is serial, a callback can end the member only when its turn in the chain comes, and an earlier callback may wait indefinitely. So the handle has `end(origin=...)`, which any code holding the handle calls, synchronously and without the other callers' cooperation:

- If the member's callbacks are running, `end()` cancels the chain through the gate's own cancel scope: the callback that is waiting gets a cancellation, the ones after it do not run, and no change from the chain is applied. The member ends `stopped`, with the reason `ended by <origin>`.
- If the member is mid-turn, `end()` records the request and closes its lever, so its next model call is refused, and the gate ends it at its next boundary in the same way. A turn can also end the loop with no boundary after it: a final submit tool that was already running, or a `react(submit=False)` reply. So the member wrapper honours the request on a normal return too: a member whose end was requested before its agent returned ends `stopped`, with the reason `ended by <origin>`, whatever its output says. An end request wins over the normal-return statuses (`submitted`, `unsubmitted`); an error the agent raises is classified as today. Without an end request a normal return is classified as today, so a submission is still `submitted`.
- A turn may not reach its next boundary soon: a long tool call, or a member waiting in `after_cancel()` for an operator's redirect ([Interrupts](#interrupts)). So `end()` of a mid-turn member also gives it the grace a swarm stop gives (the effective grace, `_control.py:636-647`), on a timer in the runtime's task group, and cancels the member's scope if it is still running when the grace runs out, as the hard stop cancels a running member. A member ended that way ends `cancelled`, as one the hard stop reaches does, and its ledger line is `cancelled`.
- If it is pending, it ends at boundary 0 once started, before any generation.
- A second `end()`, or one for a member that has ended, changes nothing. Other members and the swarm go on.

A callback returning `"end"` is the same as `end()` with its own origin, without the cancellation. `end()` is the primitive an operator's "stop this member" (#119) needs; the operator's surface stays that design's.

**What callers build on it.** Nothing here is a scheduling API; callers bring their own:

- **ORBIT's activation** is its own callback per member that signals "parked" and awaits the scheduler's go inside `suspend_working_limit()`, so `activate()` sets that member's go and awaits its next park or its end (`MemberEnded` through `events()`). `model_step` is one boundary; `completion` keeps going until a boundary with `after == "reply"`. Snapshots read `Boundary.messages`, interventions return the new conversation, and the scheduler stays ORBIT's code. inspect_swarm ships no `activate()` and no `round_robin`: each is a few lines over this contract in the caller's code, and as swarm API they would bring back the problem of an activation that must wait past another holder's pause, which composition by callbacks does not have.
- **An operator's pause** is a callback that waits inside `suspend_working_limit()` while paused; its "stop this member" calls `end(origin="operator")`, which works whichever callback holds the member.
- **Waking a persistent member** is a callback that, at a boundary with `after == "reply"`, waits inside `suspend_working_limit()` for the next piece of work and returns the conversation with it appended.
- **Push delivery** would register a runtime callback with origin `swarm` that returns the conversation with a reminder or injection appended.

### The inspect_ai change: a turn gate and a compaction reset

**The gate.** A new private module, `src/inspect_ai/agent/_channel/gate.py`, holds a `ContextVar[TurnGate | None]` and the `turn_gate(gate)` context manager that sets it. A gate is an object with two entry points: `before_turn()`, and `closed()`, which `agent_channel()` calls when a channel that has a gate closes. It is a seam for a runtime that runs agents, as inspect_swarm is, in the way the private `background_target` seam serves its background owner.

- **`agent_channel()`** reads the current gate when it creates a channel and keeps it on the channel. Every loop opened in the block gets the same gate, nested loops included; the gate receives the channel, so the runtime decides which loop it holds, and inspect_ai stays unaware of swarms. The inert channel never has a gate. When the block exits, `agent_channel()` calls the channel's gate's `closed(channel)`, synchronously in its `finally`, so the runtime knows when a loop it holds has ended.
- **`before_turn(messages, state=None)`** gains a keyword argument, the loop's own `AgentState`. Both react loops pass the state they continue with (`ch.before_turn(state.messages, state=state)`), which is the state their last generation produced, even when a custom model agent returned a fresh one (`_react.py:271-273`, `:511-513`, `_agent_generate` at `:716-753`). A loop that follows the documented pattern without it passes nothing. When its channel has a gate, `before_turn()` first awaits `gate.before_turn(channel, messages, state)`. The gate changes `messages` in place itself, if it changes it at all, so that it records the change only once the change has been made ([When the chain finishes](#the-callback-contract)), and returns whether it rewrote the conversation rather than leaving it as it was or appending to it; on `True`, `before_turn()` marks the channel rewritten. An exception the gate raises, a message limit's included, propagates from `before_turn()`. Then it drains the queue and returns the operator's messages as today, so an operator's message posted during a hold reaches the turn it was meant for.
- **`after_cancel()`** is unchanged, and never calls the gate ([Interrupts](#interrupts)).

**The compaction reset.** The handler that `compaction()` returns gains `reset(prefix)`, also added to the `Compact` protocol: it clears the handler's kept input, the ids it has folded in, its token baseline and its memory-warning flag, in place so that a checkpointer tracking that state keeps tracking it, and replaces its prefix. After `before_turn()`, both react loops check whether the channel was marked rewritten (a private read that clears the mark), and if so call `compact.reset()` with the prefix derived from the new conversation, as `_agent_compact()` derives it, and set the loop's `overflow_compacted` flag back to `False` (`_react.py:277-285`, `:517-525`). That flag stops a second forced compaction after an overflow that a compaction has just recovered; a rewrite discards the reduced input it describes, so the next overflow gets a forced compaction of the rewritten conversation rather than the configured truncation, or an `unsubmitted` ending with truncation disabled. Without a rewrite the flag behaves as today. The next `compact_input()` then starts from the rewritten conversation: below the threshold it sends it as it is; above, it compacts it, which for a summary strategy is another model call. A summary strategy starts that compaction from the latest summary still in the conversation, which the gate has left only where nothing it was derived from changed ([Stale summaries](#the-callback-contract)). An append needs no reset, since the handler adds messages it has not seen.

Without a gate nothing changes: no `react()` behaviour, ACP, log-schema or public API change beyond `reset()` and the optional `state` of `before_turn()`.

### The member's gate

Each member's run gets one `MemberGate` (new module `src/inspect_swarm/_seam.py`). The member wrapper installs it around the agent call with `turn_gate(gate)` (`_member.py`, around `_call_agent`), inside the member's scopes, so the gate sees every agent channel the member's work opens: its own loop, its subagents' loops and its background tasks' loops.

**Binding the member's own loop: one loop per member.** The seam keeps its own binding, `MemberContext.turn_loop`, separate from `MemberContext.loop`, the bus's binding at the first `swarm_tools()` resolution, which stays as it is ([The member's own loop](#the-members-own-loop)). The member wrapper records the member's own task (`anyio.get_current_task().id` in the task that calls the agent). On each call the gate first decides whether the channel is the turn loop:

- If the call is not in the member's own task (a loop in a `background()` task, or in a task group a tool starts), the gate returns at once, changing nothing. The task decides, not the count of running background work: a background loop never binds, whatever else runs.
- If `turn_loop` is unbound, this channel becomes it: the first loop to call the gate in the member's own task. For `react()` and `deepagent()` members this is the top-level loop, which opens its channel and calls `before_turn()` before it can start a subagent.
- If `turn_loop` is this channel, it is the own loop.
- If `turn_loop` is another channel that is still open, this is a nested loop inside one of its turns (a subagent, a handoff, an `as_tool()` agent), and the gate returns at once.
- If `turn_loop` has closed (the gate's `closed()` was called for it), this is a second top-level loop in the member's own task, which the seam does not hold. With a callback registered, the gate raises a `RuntimeError` naming the member and saying that a member with boundary callbacks may run only one agent loop, before that loop's first generation, and the member ends `errored`. With none, it returns at once and the loop runs as today.

Binding happens once per member run and is never undone. So a member that runs a warmup loop in a `background()` task and then its main loop binds the main loop; one that awaits a warmup loop in its own task binds the warmup, and its main loop is a second loop, refused if a callback is registered by then. The two bindings usually name the same loop, but not when the parent loop has no `swarm_tools()` and a child loop has them, as with `research(extra_tools=[swarm_tools()])` under a `deepagent()` whose own tools omit them (`docs/shared-memory.qmd:113-115`): the bus binds the child, which keeps its loop-reach tools and its `swarm_wait` accounting as the member's own, and the seam holds the parent at its boundaries, the child running inside one of the parent's turns. Neither binding moves the other, so channel routing, the `tools_unwired` check and `acp_bound` are unchanged. In this design "the member's own loop" means the turn loop.

**The loop's state.** At every boundary of the own loop, before it can hold or end the member, the gate keeps a reference to the `AgentState` the loop passed, the loop's live object. When the member's agent raises rather than returns (an end at a boundary, a limit, an error), the member wrapper takes the member's output and conversation from that state rather than from the state it called the agent with, which a custom model agent's fresh state may have left behind; without a boundary it uses the state it called the agent with, as today.

**At each boundary of the own loop:**

1. If the swarm is stopping, or `end()` was called for the member, end the member ([Stopping](#stopping)).
2. If no callback is registered, return `None` with no `await`.
3. If the loop passed no state, or any of the member's background work is running, refuse ([Supported members](#supported-members)).
4. Hold: if the member's time limit is already overdue, let it end the member instead ([Time](#time)); otherwise mark the member held, pause its time limit and recheck the swarm's waits. Run the callbacks in a cancel scope of the gate's own, which a stop or `end()` cancels.
5. Release: resume the time limit, clear the mark and recheck the waits. Then apply the result to the live list (nothing, an append or a rewrite), write the evidence in a `finally` around that, and return whether it rewrote; for `"end"`, end the member instead.

Step 4 is the only place a member becomes held, and step 3 runs before it, so the runtime never reports a hold while the member's background work runs. None starts during the hold either: the member's background owner (`MemberBackground.start_soon()`, `src/inspect_swarm/_background.py:89-108`) refuses new work while the member is held, with a `RuntimeError` naming the member, as it refuses a bridged member's, so a callback that calls `background()` raises and the member ends `errored`.

### Interrupts

An operator's interrupt is outside the seam. While a member waits in `after_cancel()` for the operator's redirect, it is mid-turn: no callback runs, the gate does not hold it, and it keeps today's behaviour. The swarm counts it as running, so `swarm_wait`'s all-waiting check counts it as active; its member time limit runs; a swarm stop reaches it through the grace period and the hard stop, and `end()` through the same grace and cancellation ([Ending a member from outside the chain](#the-callback-contract)), not through the gate. When the redirect arrives, react goes on to its next boundary, an ordinary one with every check of the gate, the stop or end check and the background-work check included; a callback registered during the wait sees it. Interrupts addressed to swarm members are operator intervention's to build (#119).

### Stopping

- **A stop ends a member at its boundary.** `stop(reason)`, in its synchronous first part, cancels every gate's running callbacks, and from then on every boundary of any member ends it. The gate ends a member by closing its lever and raising the lever's limit error, which the member wrapper already classifies as the lever's: the member ends `stopped`, as a member the stop reaches between calls does, with its last output as its output. No generation follows, so a cached reply or cached tool calls cannot run after the stop, and the member never returns normally to be read as `submitted`.
- Held members therefore end at once, without waiting out the grace period. A change a cancelled callback had not returned is never applied, and the `boundary` event records the outcome `stop`.
- **`end()` and `"end"`** take the same path for one member, with the reason `ended by <origin>`, and the rest of the swarm goes on.
- **The hard stop treats a held member like any other.** The supervisor's hard stop (`_control.py:641-644`) and `end()`'s per-member grace timer cancel every member still running at their deadline, held or not, as today; agent and resource cleanup that follows the gate's ending (the wrapper's `finally`, react's MCP teardown) keeps that deadline. Cancelling a chain only schedules its unwinding, and the agent's cleanup follows it, so the hard stop can reach a member the gate is ending before its wrapper records the end (`_end`, `_control.py:586-609`): at any grace whose deadline that cleanup outlasts (an unshielded wrapper `finally` or react's MCP teardown that takes longer than the remaining grace), and always at an effective grace of zero (configured, or clipped by the sample's remaining time, `_grace_now()`), where even a parked callback's unwinding can lose the race. That member is then recorded `cancelled` in the ledger (`MemberRun.cancelled` reads `scope.cancel_called`, `:213-219`) rather than `stopped`. This is accepted: no model call is lost, since a parked member has none in flight, and only the ledger's label differs.
- **The ledger.** Cancelling a chain is not a hard stop, so the member's ledger line is not `cancelled`, unless the hard stop reached it before its ending was recorded (above) or a callback had a model call of its own in flight when the chain was cancelled (a model event begun in the member's span during the chain and not completed), whose usage is then unknown.
- **Members between boundaries.** A member mid-turn, a member waiting in `after_cancel()` for a redirect included, is reached by the closed lever, the grace period and the hard stop as today; if it reaches a boundary first, the gate ends it there.
- **Outside cancellations** (the sample's limits, an operator's interrupt of the sample, the hard stop) cancel the callbacks like any other `await`.

### Time

| | Held time |
|---|---|
| The member's time limit | does not count: the limit pauses while the member is held |
| The swarm's time cap | counts |
| The sample's time limit | counts |
| The sample's working time | the callback's to mark: its work counts, and its idle waits are waiting time when it wraps them in `suspend_working_limit()` |
| The metrics' `running_time` | counts; `held_time` (new) gives the held part |
| `swarm_wait`'s idle time | not idle: a held member has no wait |

- **The member's time limit pauses.** A member time limit becomes a `SwarmTimeNode`, a subclass of Inspect's time limit as the swarm's token, turn and message nodes subclass Inspect's. When the gate holds the member, the node first checks its budget: if its deadline is finite and at or before the current time, or its scope already has a cancellation requested, it moves nothing, and the gate does not hold the member but awaits a checkpoint inside that scope, so the overdue cancellation is delivered and the member ends `member_limit:time` through Inspect's own path (`_TimeLimit`'s `cancelled_caught` at exit, `src/inspect_ai/util/_limit.py:1406-1463`); a deadline already reached is never rescinded. This covers a turn's synchronous work, or the setup before boundary 0, that used the member's last time before the event loop delivered the timeout; the loop has reached the gate there, so the member ends with its own limit, not under the never-reached rule. Otherwise the node sets its scope's deadline to infinity; when it releases, it moves its start time on by the held time and re-derives the deadline, so its deadline and its reported usage both count only time the member was not held. It raises Inspect's time limit error as before, so the member's status is still `member_limit:time`. The swarm's time cap and the sample's time limit are deadlines for the whole run and keep counting.
- **Working time.** The seam opens no wait of its own (decision: Ransom, 2026-10-28). A callback's model calls, sandbox commands and tool calls count as working time, and so does the rest of its time, since each callback runs in its own known-work span ([The callback contract](#the-callback-contract)). An idle wait wrapped in `suspend_working_limit()` suspends that callback's own span and is waiting time, except while other known work runs in the sample, so a swarm whose members all wait that way stops its working time. A callback that waits unwrapped has its hold counted as working time and can trip the sample's working limit; work an author wants left uncharged is wrapped the same way.
- **`swarm_wait`.** A held member counts as not active: the all-waiting check becomes "no member is running, not waiting and not held", and the gate rechecks the waits when it holds and releases. So when every other member is waiting or held, the waits return (`all_waiting`) rather than wait for members that will not move until their callbacks return. The later quiescence check ([swarm-wait.md Room for later work](swarm-wait.md#room-for-later-work)) counts a held member the same way: not active.

### Supported members

| Member | Seam |
|---|---|
| `react()`, `react(submit=False)`, `deepagent()` without background dispatch | Its top-level loop. A loop that ends at a submission or a tool-free reply has no boundary after it; to be held after a reply, the agent must continue after one (an `on_continue` that returns the state), which is #116's to decide. |
| Nested loops: subagents, handoffs, `as_tool()` agents, `deepagent()`'s synchronous `general()` subagents | Never held on their own; they run inside a turn of the member's own loop, and a hold takes effect when that turn ends. |
| `deepagent(background=True)` or an integer `background` | `on_boundary()` raises `ValueError`, detected from the agent's registry name, `inspect_ai/deepagent`, and its `background` argument, as M1 refused these members. Holding the top-level loop would leave its background subagents generating, which breaks every use's guarantee that a held member does nothing. |
| A custom agent that calls `background()` itself | Checked at every boundary with callbacks, before the member counts as held: if any of its background tasks is running, the gate does not hold it, and the member ends `errored` with a `RuntimeError` naming the member and saying that a member with background work running cannot be held. Tasks that have finished do not matter, and the background owner refuses new ones while the member is held. A member whose background work survives an operator's interrupt is not held while it waits for the redirect ([Interrupts](#interrupts)), and its next boundary is checked like any other. |
| Bridged members, declared or in the `inspect_swe/` namespace | `on_boundary()` raises `ValueError`: the CLI runs its own turns, and no agent channel is opened. |
| Members with callbacks that never reach a boundary: an undeclared bridged wrapper, a custom agent that generates without an agent channel, or one that waits and returns or answers a constant (`generator()` and `sleeper()` in `tests/swarm_runtime.py:111-135`) | Found when the member ends, not before. The gate records whether the member's own loop ever reached it, and the handle records whether a callback was ever registered for this run, unregistered since or not. If one was and the loop never reached the gate, the member wrapper, after the agent's call ends and before it classifies the status (`_member.py:520-521`, `_returned()` at `:569-579`), ends the member `errored` with a `RuntimeError` naming it and saying that its callbacks never ran because it never reached a turn boundary, when the member produced its ending itself: a normal return, its own limit, or its own error, which becomes the new error's cause. An ending the runtime or the outside produced keeps its ordinary outcome: a swarm stop (`stopped` or, at the hard stop, `cancelled`), `end()`'s grace timer (`cancelled`), and a cancellation from outside the swarm, which propagates as today (decision: Ransom, 2026-10-29). Its output is therefore never a submission or the swarm's answer, so an unsupervised run cannot pass as a supervised one; it has no `boundary` events and its `holds` metric is 0. The trade: such a member runs once, unsupervised, to its end before it is marked; its result is discarded and its model calls are spent. Among the member's own endings the rule has no exception for a limit, so it also covers a supported loop that ends during its setup, before its first boundary: react's system prompt overflowing a tight `message_limit()`, or a member time limit expiring while MCP servers open. The runtime cannot tell such a loop from an unsupported one, and a limit hit during setup is a problem with the setup (decision: Ransom, 2026-10-29). The cost: the `RuntimeError` is a foreign error, so it fails the swarm and cancels the other members, where today a member limit too small for setup ends only that member, `member_limit:*`. It applies only to members with callbacks, whose sample then fails loudly rather than half-running. A supported loop that reached the gate and then finished with no further boundary ends as today, and so does any member with no callback registered. |
| A custom loop that calls `before_turn()` without its state | Refused at its first boundary with a callback registered: the member ends `errored` with a `RuntimeError` saying that its loop does not pass its state, so a callback could not be given its output. Without callbacks it runs as today. |
| A nested swarm inside a member | Its members have gates of their own, installed by its own runtime; the outer member's gate never binds their loops. |

### Evidence

Two `InfoEvent`s of type `turn`, in the member's span, with the swarm's evidence fields (`version`, `type`, `kind`, `member`, `span_id`), written only at boundaries where callbacks ran:

- **`boundary`**, once per such boundary: `boundary` (the index), `after`, `origins` (the callbacks' origins in order), `held_for` (seconds), `outcome`, `messages` before and after. The outcome is `continue` (the chain's change, if any, is in the live list), `end` (by `"end"` or `end()`, with its origin), `stop`, `error` (a callback raised, or its result failed a check; with the origin of the one that raised and the error's type), `rejected` (applying the change to the live list raised, such as a message limit; with the error's type) or `cancelled` (a cancellation from outside the swarm). The events are written in a synchronous `finally`, so they are written however the chain ends.
- **`changed`**, once for each callback whose result differs from the state it received: `boundary`, `origin`, and the change, found by diffing on message id:
  - `added`: every message whose id was not in the state the callback received, wherever it was put, with its id, its hash and its index in the result, in order; an append is the case where every index is at the end;
  - `removed`: the ids of messages no longer there;
  - `replaced`: for each message kept under its id with new content, its id and its hashes before and after;
  - `order`: present only when the kept messages changed their relative order, the result's full list of ids;
  - `applied`: whether the change is in the member's live conversation: true only when the boundary's outcome is `continue`, which the gate records only after it has changed the list, so a change returned before a later callback ended the member, raised, or was cancelled, before a stop, or that the message limit refused, is recorded with `applied: false`.

A hash is the SHA-256 of the message as the log serializes it, its id excluded, so analysis can match a message in the conversation or in a `ModelEvent`'s input to the change that made it, where Inspect has not combined or compacted it. Returning the same content is no change and writes no `changed` event. The runtime's removal of stale summaries is one more `changed` event, origin `swarm`, with only `removed` and `applied`, written after the callbacks' events at a boundary where it removed any.

**Ids and hashes, not content.** The content of an added or replaced message is in the member's conversation and in the next `ModelEvent`'s input, and a removed message's is in earlier ones. Recording it again would grow the log with every activation for a defence that rewrites much of the conversation each time. A caller that wants its own content record writes its own event; ORBIT already writes its own.

**One event per callback**, with the origin given at registration, rather than one per operation inside a callback. A caller that wants changes told apart (attack input, then a defence) registers a callback for each, in the order they must run. `changed` takes the place of swarm-orbit.md P5's `intervened` event: the same added, removed and replaced ids and hashes, written by the runtime for every caller rather than by ORBIT's interventions; P5's `committed: false` for operations staged at a stop becomes `applied: false`, since staging is the caller's.

The metrics gain two fields per member, `holds` (boundaries at which callbacks ran) and `held_time`, which default to 0 so that metrics written before them load.

### What this means for swarm-orbit.md

swarm-orbit.md is revised separately once this design settles. In short, under this contract:

- **P4.** The seam replaces `swarm_on_continue()`: nothing to wire, every boundary covered. `activate()`, the quanta, `round_robin`, `ActivationResult`, the `schedule` events and the read cutoff become ORBIT's code over `on_boundary()`. The member wrapper does read a pending end request on a normal return ([Ending a member](#the-callback-contract)), which covers the case P4's stop-cause change was for, and a conversational member ended at a stop is `stopped`.
- **P5.** Staged copies, `snapshot()`, `append` and `edit` become ORBIT's code: a snapshot is `Boundary.messages`, and an intervention returns the new conversation. `changed` replaces `intervened`. Whether controllers may read member text or add user- or system-role input (open question 1) stops being a swarm decision.
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

**Callbacks inside a wait the seam opens.** The seam would wrap the whole chain in `sample_wait()`, so no callback needs to mark its waits. But work inside such a wait is not charged at all, so a defence that spends its time in model calls at the boundary would never count towards the sample's working limit. Rejected: callback work counts, and callbacks mark their idle waits themselves (decision: Ransom, 2026-10-28).

**Refusing an unsupported member before its first model call.** The member would be cancelled at the first call it makes while it has callbacks and has not reached the gate, so it never runs unsupervised. Every hook for it fell short. The swarm's transcript subscription misses a failed request whose bridge model-event sink defers its events, so a wrapper that retries can ask the provider again and again unseen. The limit nodes' checks (beside `detect_bridge()`) miss a cache hit, which is returned before them, and also run outside model dispatch, when the member's starting conversation is built and react inserts its system prompt, so they would refuse a supported member with a message limit before its loop could reach the gate; narrowing them to Inspect's pre-dispatch token and cost checks still needs the transcript for cache hits. Rejected for the check when the member ends: one place, no dependency on how Inspect emits events, at the cost of one unsupervised run whose result is discarded (decision: Ransom, 2026-10-29).

**Refusing a rewrite that keeps a stale summary**, rather than dropping it. The caller would see its choice. But every defence that edits history would have to know Inspect's summary metadata and the member's compaction strategy, and a summary is a cache Inspect derived, not a caller's message. **Ignoring summaries in the compaction after a reset** would need a change to every summary strategy's cut and would still send the stale summary below the threshold. Rejected for dropping stale summaries at the boundary, in the evidence.

**Holding successor and warmup loops.** The gate would follow a member through every top-level loop it opens, binding each in turn. No member shape in use runs more than one, and telling a successor from a nested loop and a warmup from the main loop needs bookkeeping for every case. Rejected for one loop per member, in the member's own task, and an error for a second one when callbacks are registered (decision: Ransom, 2026-10-29).

**Exempting an orderly ending from the hard stop.** The gate would mark a member it is ending so that a zero-grace hard stop could not reach it before the end is recorded, keeping its ledger line `stopped`. The mark had to end before the member's background drain and before agent and resource cleanup, or it would shield them past the stop's deadline, and each boundary of it was a new race. Rejected: the hard stop treats a held member like any other, and a parked member at a zero-grace stop may be recorded `cancelled`, losing no model call (decision: Ransom, 2026-10-29).

**Building the boundary's output from the messages.** The gate would need only the messages. But a custom model agent's output carries its stop reason and usage, which no chat message holds. Rejected for passing the loop's state to `before_turn()`.

## Compatibility and migration

- **inspect_ai.** Without a gate nothing changes. The gate is a private module; `before_turn()` gains an optional keyword argument, and `after_cancel()` is unchanged; `reset()` is added to the compaction handler and the `Compact` protocol, so a custom class that a type checker matched against `Compact` must add it. inspect_swarm needs an inspect_ai release with both.
- **inspect_swarm's API** gains `on_boundary()`, `end()` and `held` on the handle, and `Boundary` and `BoundaryCallback`, additions only. `MemberHandle` is a protocol that only the runtime implements. `SwarmEvent` and `MemberState` are unchanged.
- **Behaviour without callbacks.** One change, for every member: a member that reaches a boundary after the swarm began to stop ends there, `stopped`, rather than at its next model call; the status is the same, and a cached call can no longer run after the stop. Binding changes nothing: the seam binds its own turn loop beside the bus's `MemberContext.loop`, which is still bound at the first `swarm_tools()` resolution, so tools, waits, `tools_unwired` and `acp_bound` are as today.
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
  - `before_turn()` awaits the gate before draining, with the channel, the messages and the state react passes (`None` from a loop that passes none); `True` marks the channel rewritten and `False` does not; an exception from the gate propagates;
  - a nested `agent_channel()` gets the same gate; the inert channel never calls one; with no gate, `before_turn()` is unchanged;
  - a `react()` loop calls the gate before its first generation, after a turn with tool calls, after a recovered context overflow (a mock output with `stop_reason="model_length"`), and after an interrupt recovery, once the redirect has arrived, and not during `after_cancel()`'s wait;
  - with `react(compaction=CompactionTrim(...))` below its threshold, and with `deepagent()`'s default compaction: a rewrite that replaces a message under the same id, and one that deletes a message, reach the next model input as rewritten; an append does not reset the handler;
  - in both react loops with `CompactionTrim` and `truncation="disabled"`: an overflow recovered by forced compaction, a rewrite at the next boundary below the threshold, then another overflow, gets a forced compaction again and reaches the answer; with no rewrite between the two overflows the second gets no forced compaction, as today;
  - with `CompactionNative` and `CompactionAuto`'s native branch (a stub `Model.compact()` shaped as Anthropic's, returning no system message), in both react loops: a callback that appends a system message at boundary 0 and one that appends it at a later boundary keep it in the model input after the next native compaction; a callback that appends a user message is applied as an append and does not reset the handler;
  - `reset()` followed by a forced compaction and by one over the threshold, for `CompactionSummary` and `CompactionAuto`'s summary fallback (mockllm has no native compaction): the summarizer's input is drawn only from the conversation `compact_input()` is given after the reset, never from the handler's input before it.
- **inspect_swarm** (`tests/test_turn_seam.py`, new, on asyncio and trio through `tests/conftest.py`):
  - with a custom model agent that returns a fresh `AgentState` (as Inspect documents custom generation), `Boundary.output` is that agent's output, its `max_tokens` stop reason and usage intact, and a member then ended at the boundary by `end()` or a stop keeps that output in the result record;
  - a callback registered on a pending member gets boundary 0 with `after == "start"` before any model call, and the member is `held` while it waits; a returned conversation with an added user message is in the next model input;
  - `after` is `tools` after a tool turn, and `reply` for a `react(submit=False)` member whose `on_continue` returns its state;
  - two callbacks run in order, the second receives the first's result, and a pause callback registered second holds the member after a scheduler callback lets it go; a scheduler built on callbacks alternates two members turn by turn while a third caller pauses one of them and later resumes it;
  - unregistering takes effect at the next boundary; in-place changes to the copy change nothing, an append past the member's `message_limit()` included; duplicate ids in a result raise and end the member `errored`;
  - with a member at its `message_limit()`: a callback that returns a list over the cap followed by one that trims it continues with the trimmed conversation; one over the cap followed by one that returns `"end"` ends the member `stopped`; a final result over the cap is rejected when applied (outcome `rejected`, `member_limit:message`);
  - a result with no user message (an empty list, a system-only list, and one with only assistant and tool messages), with and without an operator's message queued, raises `ValueError` naming the member and the origin, ends the member `errored` at once with the `boundary` outcome `error`, and leaves no wait behind; a rewrite that removes all but one user message, and one that replaces the user messages with a new one, continue to the next generation;
  - `"end"` ends the member `stopped` with no further model call, and the rest of the swarm goes on;
  - `end()` called while a final submit tool runs ends the member `stopped`, not `submitted`, and its submission is not eligible as the answer; so does `end()` during a `react(submit=False)` turn that ends with a reply; with no `end()`, the same members end `submitted`;
  - an operator interrupt with the redirect delayed, with a callback registered: the member is not `held` while it waits, no callback runs, `swarm_wait`'s all-waiting check counts it as active, and its member time limit runs; `end()` during the wait ends it `cancelled` after the grace period, and a swarm stop through the grace period and the hard stop; a redirect that arrives continues it to a boundary where callbacks run; for `react()` and `react(submit=False)` members;
  - `end()` during a long tool call that outlasts the grace period ends the member `cancelled`, and one that finishes within it ends the member `stopped` at its next boundary;
  - a custom member whose background task survives an operator's interrupt: it is not held during the redirect wait, with and without a callback registered, and at its next boundary with a callback it ends `errored` while the task runs; a callback that calls `background()` during a hold raises the owner's `RuntimeError` and ends the member `errored`;
  - `end(origin="operator")` while a scheduler callback waits, with the operator's callback registered before the scheduler's and after it, and called after the first callback is already waiting: the waiting callback is cancelled, later ones do not run, no change of the chain is applied, the `boundary` event's outcome is `end`, and the member ends `stopped` with `ended by operator` while another member runs on; `end()` mid-turn ends the member at its next boundary, and on a pending member at boundary 0;
  - a stop while held, and a stop before a member's next boundary, each end the member `stopped` with no further generation, with a cached plain reply and with cached tool calls waiting for it, and, with its cleanup finishing within the grace, its ledger line is not `cancelled`;
  - with an effective grace of zero: a member whose parked callback the stop cancels ends with no generation and no model call lost, and its ledger line may be `cancelled`, the accepted outcome; with a positive grace, a wrapper whose `finally` awaits longer than the grace after the gate's ending, and react's MCP teardown that does, are cancelled at the hard stop's deadline and give a `cancelled` ledger line with no model call lost, for a swarm stop and for `end()`'s grace timer, while the same member whose cleanup finishes within the grace ends `stopped` with `cancelled` false;
  - a member time limit does not run while held: a member with `time_limit(0.5)` held for 1 s, then working 0.3 s, is not stopped; held for 1 s and working 0.6 s, it ends `member_limit:time`;
  - an overdue member time limit is not paused: a member whose synchronous work (no `await`) outlasts its `time_limit()` before boundary 0, and one that does so in a tool before a later boundary, each with a callback registered, ends `member_limit:time` at that boundary without being held and without the callback running; an unexpired limit across a hold is the control;
  - working time (a real sample, through `eval_async()`, as `tests/test_binding.py` runs one): every member held by a callback that waits inside `suspend_working_limit()` stops the sample's working time; the same wait unwrapped counts as working time; a callback's own model call counts as working time; one member's callback waiting marked while another member's callback waits unmarked, and while another computes, counts the second's time as working; marked and unmarked waits in one callback count only the unmarked part;
  - a member in `swarm_wait` is released with `all_waiting` when the only other running member is held;
  - refusals: `ValueError` from `on_boundary()` for a declared bridged member, for `deepagent(background=True)` and `background=4`, and for an invalid origin; `RuntimeError` for an ended member and while stopping; a custom agent whose tool starts `background()` after a callback was registered ends `errored` at its next boundary while the task runs, and is held normally once it has finished; a channel-less `generator()` with a callback registered (and with one registered then unregistered before it returns) runs to its end and ends `errored` with the `RuntimeError`, not `submitted`, is not the swarm's answer, and has no `boundary` event; so do `sleeper(0)` and `generator(calls=0, answer=...)`, and a `generator()` stopped by a member token limit; the same members with no callback registered end as today; a `react()` member with a callback registered before it starts and a member `message_limit()` reaches its gate and submits as usual; a `react()` member with a callback and a `message_limit()` that its setup exceeds (react's system prompt added to `start()`'s two user messages under `message_limit(2)`) ends `errored` and its sibling is cancelled, while the same member with no callback ends `member_limit:message` and its sibling submits; a `react(submit=False)` member with a callback that reaches its first boundary and then ends at a tool-free reply ends `submitted`; a custom loop that calls `before_turn()` without its state ends `errored` at its first boundary with a callback; before a member's first boundary (a `react()` member still opening an MCP server), a swarm stop ends it `stopped` or `cancelled`, `end()`'s grace timer ends it `cancelled`, and a cancellation from outside the swarm propagates, each as without the seam and with no `RuntimeError`;
  - stale summaries, for `react()` and `react(submit=False)` members with `CompactionSummary` and with `CompactionAuto` on mockllm (its summary fallback, as `deepagent()`'s default compaction resolves), after a real summary compaction has put a summary in the conversation: a callback that edits a message before the summary under its id, and one that deletes such a message, keeping the summary; the summary is removed, a `changed` event with origin `swarm` lists it, and at the next compaction, forced by an overflow and over the threshold, the summarizer's input has the edited text and neither the original nor the deleted one; a rewrite that changes only messages after the summary keeps it, and so does a summary the callback itself replaced; a rewrite whose only user message is a stale summary ends the member `errored`;
  - a `deepagent()` member's synchronous subagent is never held, and the callback runs when the subagent's tool call returns;
  - the `boundary` and `changed` events: one `changed` per changing callback with its origin, an append, an insertion at the start and in the middle (each in `added` with its index), a deletion, a same-id replacement and a reorder (`order` present only then) each recorded as such, the hashes matching the messages in the next `ModelEvent`'s input, `applied: false` after a later `"end"`, after a later callback's exception (outcome `error`) and after an outside cancellation (outcome `cancelled`), and, for a member at its `message_limit()`, after an append and after a growing rewrite (outcome `rejected`, the member ending `member_limit:message` with its conversation as it was, and the runtime's stale-summary `changed` event also `applied: false`), with the same changes below the limit applied as the controls; a callback that changes the conversation of a custom loop that passed a tuple ends the member `errored` with `TypeError`; no event for a callback that returns the same content, and no text in either event; the `holds` and `held_time` metrics; `tools_unwired` still names a `react()` member in a shared-memory swarm that was not given `swarm_tools()`.
  - binding, with and without a callback: a `react()` parent without `swarm_tools()` whose child loop (an `as_tool()` agent) has them, and a `deepagent()` with `research(extra_tools=[swarm_tools()])`: the child keeps its loop-reach channel's tools (`loop_write` as well as `member_write`), the bus treats it as the member's own loop, and its `swarm_wait` in a shared-memory swarm returns `all_waiting` as today; with a callback, the parent's boundaries are held and the child's are not;
  - one loop per member, with a callback registered: a wrapper that awaits two `react()` loops in turn ends `errored` at the second loop's first boundary, before its first generation, with no answer; with no callback, both run as today; a member that runs a warmup `react()` in a `background()` task, waits for it, then registers a callback and runs its main loop binds the main loop and holds it; a warmup awaited in the member's own task binds instead, and the main loop is refused; a nested `as_tool()` loop inside the bound loop's turn is never held and never refused;

## Implementation plan

1. **inspect_ai: the turn gate and the compaction reset.** `src/inspect_ai/agent/_channel/gate.py` (new), `channel.py` (`before_turn()`, the channel's gate and rewrite mark), `__init__.py` (`agent_channel()` reads the gate and calls its `closed()`), `src/inspect_ai/model/_compaction/_compaction.py` and `types.py` (`reset()`), `src/inspect_ai/agent/_react.py` (pass the state to `before_turn()`, reset compaction and clear `overflow_compacted` after a rewrite, in both loops); the inspect_ai tests above.
2. **inspect_swarm: the seam.** Needs step 1 in an inspect_ai that inspect_swarm can depend on.
   - `src/inspect_swarm/_seam.py` (new): `MemberGate`, `Boundary`, `BoundaryCallback`, binding (the member's own task, one loop), the callback chain in known-work spans, the result checks (ids, a user message), an added system message as a rewrite, the removal of stale summaries, applying the change to the live list, the diff and its evidence.
   - `src/inspect_swarm/_member.py`: install the gate around the agent call; the output from the gate's last state when the agent raises; a pending end request read on a normal return; ending `errored` a member whose callbacks never ran because it never reached the gate; member time limits as `SwarmTimeNode`; the background-dispatch check for `on_boundary()`.
   - `src/inspect_swarm/_background.py`: refuse new background work while the member is held.
   - `src/inspect_swarm/_controller/_control.py`: `on_boundary()`, `end()` (with its grace timer) and `held` on `MemberRun`, the stop's cancellation of callbacks.
   - `src/inspect_swarm/_budget.py`: `SwarmTimeNode`; ending a member through its lever at a boundary; the `turn_loop` binding on `MemberContext`.
   - `src/inspect_swarm/_ledger.py`: model calls in flight in a cancelled chain.
   - `src/inspect_swarm/_bus/_wait.py`: held members are not active.
   - `src/inspect_swarm/_evidence.py`, `src/inspect_swarm/_metrics.py`, `src/inspect_swarm/__init__.py`.
   - Docs: `docs/controllers.qmd` (boundary callbacks), `docs/members.qmd` (time limits while held), `docs/analysis.qmd` (the events and metrics).
   - Tests: `tests/test_turn_seam.py`.

## Open questions

None.

## Not this design

- **Holding a member's background work**, by gating its background tasks' loops, on top of the per-member background owner ([swarm-background-members.md](swarm-background-members.md)), or quiescing it before a hold.
- **Pausing bridged members**, through a hold at the model call or inspect_swe's restart with `--resume`.
- **Holding a member during interrupt recovery.** A member waiting in `after_cancel()` for an operator's redirect stays outside the seam ([Interrupts](#interrupts)): it is not held, and `end()` or a stop reaches it by cancellation. Making that wait known to the swarm (held status, a paused time limit, an immediate end) belongs with interrupts addressed to swarm members, in operator intervention for every member: steer, pause, stop ([#119](https://github.com/meridianlabs-ai/inspect_swarm/issues/119)), which already names the `after_cancel()` hazard. Today only the one member ACP binds can be interrupted, with its operator present.
- **The closed lever and cached calls inside a turn.** After a stop begins, model calls inside a turn that is already running (a subagent's, compaction's) can still be served from the cache, since a hit is returned before the limit checks; the seam ends only the member's own loop, at its next boundary. A fix belongs in Inspect's limit checks.
- **Idle and woken events**, for persistent members (#116).
- **Updating swarm-orbit.md** to this contract, in its own revision.
