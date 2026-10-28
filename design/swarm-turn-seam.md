# Inspect Swarm: the turn seam

## Overview

The turn seam lets the swarm hold a member between two turns of its own agent loop, with no model call or tool call in flight, and resume it later with its conversation intact. It is a small hook in inspect_ai's agent channel. When the swarm runs a member, it installs a *turn gate* that the channel's `before_turn()` awaits before every generation of the loop, the first one and the turns that recover from a context overflow or an interrupt included. With nothing requested the gate returns at once. When a controller has asked to hold the member, the gate parks there until the controller resumes it, optionally with input that the member sees before its next generation. Agent authors wire nothing: every `react()` and `deepagent()` member gets the seam, and nested loops such as subagents pass through it untouched.

A controller gets four operations on a member's handle: `hold()`, `wait_held()`, `resume(input=)`, and `held`, which says at which boundary a member is parked without showing any of its text. A stop releases every held member, which then ends at its next model call, refused by its closed lever, with the stop's status. Held time counts towards every time limit as wall-clock time, and is inspect_ai's sample waiting time for working time, so working time stops only when nothing in the sample is working. Members the seam cannot hold (bridged CLIs, `deepagent()` members with background dispatch, and agents with no agent channel) are refused with an error, never held silently or not at all. Persistent members, operator controls, ORBIT's scheduled mode and push delivery build on these operations in their own designs. The seam replaces the `swarm_on_continue()` hook that [swarm-orbit.md](swarm-orbit.md) P4 and P5 were built on.

## Why

Several planned features need to stop a running member at a turn boundary and continue it later ([#123](https://github.com/meridianlabs-ai/inspect_swarm/issues/123)):

- **Persistent members** ([#116](https://github.com/meridianlabs-ai/inspect_swarm/issues/116)) hold an idle member after it answers, and wake it with new work.
- **Operator intervention** ([#119](https://github.com/meridianlabs-ai/inspect_swarm/issues/119)) pauses and resumes one member or the whole swarm, and stops one member.
- **ORBIT's scheduled mode** ([swarm-orbit.md P4](swarm-orbit.md#p4-scheduled-mode-the-turn-seam-and-activation) and [P5](swarm-orbit.md#p5-trusted-interventions)) runs a member for one quantum with `activate()` and parks it; trusted interventions change its conversation between activations.
- **Push delivery** for the later messages channel ([swarm-communication.md Push delivery after M2](swarm-communication.md#push-delivery-after-m2-optional)) adds reminders and injected content at a turn boundary, if [its open question 2](swarm-communication.md#open-questions) chooses push.
- **Turn-based controllers** (supersteps, Concordia-style sequential engines, AgentsNet-style synchronous rounds; [swarm.md](swarm.md)) and joint monitors that check every member between turns hold every member at a boundary, as a barrier.
- **Checkpoint and resume** ([#121](https://github.com/meridianlabs-ai/inspect_swarm/issues/121)) may want a point where no member has a call in flight.

inspect_swarm has no such seam. The design that had one, swarm-orbit.md's `swarm_on_continue()`, assumed the turn hook already existed for notices. Notices and the hook were removed (decision: Ransom, 2026-10-27), which left the seam as [swarm-orbit.md open question 5](swarm-orbit.md#open-questions). Each feature above needs the same thing, so the seam is designed once, as shared infrastructure.

## Goals and non-goals

Goals:

- One seam at the turn boundary of a member's own loop, which can hold the member there and change its conversation before the next generation, serving every use above.
- No wiring by agent authors, and every turn covered: the first generation, and the turns after a recovered context overflow or an interrupt.
- A controller API to hold, wait for, and resume a member with input, which shows no member text.
- Defined behaviour for stops, time limits, the sample's working time and the swarm's waits; evidence for every hold.
- A clear error for every member the seam cannot hold.

Non-goals:

- **The features built on the seam.** How a persistent member continues past an answer, its idle and wake events and how its answers count (#116); the operator's surface and stopping one member (#119); ORBIT's `activate()`, snapshots, interventions and their evidence (P4, P5); whether and what to push (swarm-communication.md open question 2); what a checkpoint holds (#121).
- **Holding work outside the member's own loop**: its background work, a bridged CLI's turns, nested loops on their own.
- **Preemption.** A hold takes effect when the current turn ends. Interrupting a turn in flight is urgent steering's mechanism ([Urgent messages and steering](swarm-communication.md#urgent-messages-and-steering)).
- **Member text for controllers.** Reading or rewriting a member's conversation from a controller stays [swarm-orbit.md open question 1](swarm-orbit.md#open-questions).

## Current behaviour

Code references are to inspect_swarm `main` at `1c8aea1`, and to inspect_ai at its 0.3.283 release (`c2fc3d9e3`), the version inspect_swarm's lock file installs. The agent-channel files are unchanged on inspect_ai `main` at `c300b90fa`.

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

### The member's own loop

- The member's own loop is `MemberContext.loop`, bound to the first agent channel in which `swarm_tools()` is resolved (`src/inspect_swarm/_bus/_tools.py:17-23`, `src/inspect_swarm/_budget.py:309-312`). The bus uses it to tell nested loops from the member's own (`_bus/_bus.py:197-200`), and the `tools_unwired` check reads whether it was ever bound (`_controller/_control.py:499-501`, `_metrics.py:216-220`).
- The swarm never posts to or binds a member's agent channel ([The binder and ACP](swarm-communication.md#the-binder-and-acp)).

### Running, stopping and time

- `start()` runs the member in its own task (`_controller/_control.py:311-340`). The member wrapper enters its tool-state scope, sandboxes, cancel scope, background owner and limit nodes, opens its span and calls the agent (`src/inspect_swarm/_member.py:468-551`, the call at `:586-600`).
- `stop(reason)` closes the bus, cancels the controller and closes the lever of every running member, synchronously; then comes the grace period and the hard stop (`_control.py:517-532`, `:636-647`). A closed lever makes the member's next model call fail its limit check before the call is made (`_budget.py:528-532`), and the member ends with the lever's stop cause, such as `stopped`.
- A member time limit is Inspect's `time_limit()`, a wall-clock deadline on the member's cancel scope (`_member.py:403-404`). The swarm's time cap sleeps, then stops the swarm (`_control.py:632-634`).
- **Sample working time.** Each instant is working if any known work (a model attempt, a sandbox exec, a tool call that is not itself waiting) is in progress anywhere in the sample, otherwise waiting if any known wait is open (`src/inspect_ai/_util/working.py:1-8`). `sample_wait()` opens such a wait for a block (`:560-577`). inspect_ai's own `pause --now` parks a generate attempt inside `sample_wait()` and keeps `time_limit` deadlines running ("explicitly the operator's risk", `src/inspect_ai/_control/pause.py:620-661`).
- **`swarm_wait`'s all-waiting check** releases every wait only when no member is running without waiting (`_bus/_wait.py:133-146`): a running member that is not in a wait blocks it.
- **Background work.** `MemberBackground` owns work a member starts with `background()`; the member's context records that it has started some (`background`) and how many tasks are running (`background_running`) (`src/inspect_swarm/_background.py:89-108`, `_budget.py:313-317`). Bridged members are detected from the declaration or the `inspect_swe/` registry namespace (`_budget.py:290-296`), or found in a model call through the bridge (`_budget.py:344-355`).
- **Controllers** see members through `MemberHandle`: name, role, state, status and whether it submitted, never text (`_control.py:51-77`). `events()` yields `MemberEnded` only. The built-in `leaderless` reads `event.submitted` without testing the event's type (`_controller/leaderless.py:36-38`).

### Spike

A throwaway patch of `AgentChannel.before_turn` parked members of a real `swarm()` inside the call, keyed by `current_member` and bound to the first channel of each member that reached `before_turn()`. Two `react()` members on mockllm each called a 0.3-second tool twice and submitted; a test controller held them. On both asyncio and trio:

- A member held before it started parked at its first boundary, before its first generation, with its preamble as the last message.
- A message appended by slice assignment on the list `before_turn()` received was in the member's next `ModelEvent` input.
- One member held for 1 s while the other worked: total 1.94 s, working time 1.47 s on asyncio. The held time counted as working while the other member's tool calls ran and as waiting after it ended. Both held for 1 s: total 1.82 s, working 0.73 s.
- A held member released by the swarm's stop ended `stopped` at its next generation with no model call, its ledger line not `cancelled`.
- A held member with `time_limit(0.5)` ended `member_limit:time` at 0.50 s.
- Every sample ended with status `success`.

### What the uses need

| Use | Hold | Add messages | Rewrite messages | Read the conversation |
|---|---|---|---|---|
| Persistent members (#116) | at an idle boundary, after a reply | the new work, on wake | | |
| Operator controls (#119) | one member or all, composed with a controller's holds | | | |
| ORBIT scheduled mode (P4) | at every boundary (`model_step`), or at the next reply (`completion`) | | | snapshots (P4, open question 1) |
| ORBIT interventions (P5) | | scenario and attack input | defenses (`edit`) | |
| Push delivery | | reminders, injections | | |
| Turn-based controllers, joint monitors | every member at once, as a barrier | | | |
| Checkpoint (#121) | every member at once | | | the conversation, for the checkpoint |

So the seam holds, adds messages, and can replace the conversation outright, which covers ORBIT's `edit`; reading the conversation is the runtime's, not the controller's.

Member shapes in use: `react()` and `deepagent()` members in the examples and docs (`examples/*.py`, `docs/members.qmd`), `deepagent(background=...)` members (`docs/members.qmd:115`), inspect_swe's bridged agents (`docs/members.qmd:146`), and test-only custom agents that call the model in their own loop (`tests/swarm_runtime.py`, `generator`).

## Design

### At a glance

```python
# inspect_ai: src/inspect_ai/agent/_channel/gate.py (new, private)
TurnGate = Callable[[AgentChannel, Sequence[ChatMessage]], Awaitable[list[ChatMessage] | None]]

@contextmanager
def turn_gate(gate: TurnGate | None) -> Iterator[None]: ...   # the gate for channels opened in the block


# inspect_swarm: the controller's view
@dataclass(frozen=True)
class TurnBoundary:
    index: int                                        # 0 before the loop's first generation
    after: Literal["start", "tools", "reply", "user"]   # what the conversation ends with


class MemberHandle(Protocol):                         # the existing handle, plus:
    @property
    def held(self) -> TurnBoundary | None: ...        # where it is parked, or None
    def hold(self) -> None: ...                       # park at its next boundary, until resume()
    async def wait_held(self) -> TurnBoundary | None: ...   # once parked; None if it ends first
    def resume(self, input: str | list[ChatMessage] | None = None) -> None: ...
```

### The inspect_ai change: a turn gate

A new private module, `src/inspect_ai/agent/_channel/gate.py`, holds a `ContextVar[TurnGate | None]` and the `turn_gate(gate)` context manager that sets it. It is a seam for a runtime that runs agents, as inspect_swarm is, in the way the private `background_target` seam serves its background owner.

- **`agent_channel()`** reads the current gate when it creates a channel and keeps it on the channel. Every loop opened in the block gets the same gate, nested loops included; the gate receives the channel, so the runtime decides which loop it holds, and inspect_ai stays unaware of swarms. The inert channel never has a gate.
- **`before_turn(messages)`**, when its channel has a gate, first awaits `gate(channel, messages)`:
  - `None`: the conversation is unchanged.
  - A list: the conversation the turn continues with. `before_turn()` assigns it in place (`messages[:] = result`), so the caller's list, which react extends and keeps, holds it. A limited list checks the message limit for the growth, as for any append. If `messages` is not a `list`, `before_turn()` raises `TypeError` saying that a gate that changes the conversation needs the loop's own message list.

  It then drains the queue and returns the operator's messages as today, so an operator's message posted during a hold reaches the turn it was meant for.
- **Nothing else changes.** `after_cancel()` is unchanged, since react goes on to `before_turn()` after it. No `react()`, ACP, log-schema or public API change; without a gate, `before_turn()` behaves exactly as today.

A gate returns a whole conversation, not messages to append, so one primitive covers appending input and P5's `edit`, and ORBIT's interventions need no second inspect_ai change.

### The member's gate

Each member's run gets one `MemberGate` (new module `src/inspect_swarm/_seam.py`). The member wrapper installs it around the agent call with `turn_gate(gate)` (`_member.py`, around `_call_agent`), inside the member's scopes, so the gate sees every agent channel the member's work opens: its own loop, its subagents' loops and its background tasks' loops.

**Binding the member's own loop.** On each call the gate first decides whether the channel is the member's own loop:

- If `MemberContext.loop` is unbound, this channel becomes it, and `acp_bound` is recorded as `swarm_tools()` records it today. For `react()` and `deepagent()` members this is the top-level loop, which opens its channel and calls `before_turn()` before it can start a subagent.
- If `MemberContext.loop` is this channel, it is the own loop.
- Otherwise (a subagent, a handoff, a background task's loop, or a channel other than the one `swarm_tools()` bound first) the gate returns `None` at once.

`swarm_tools()` keeps binding `MemberContext.loop` when it resolves first, and now also sets a separate `MemberContext.tools` flag, which the `tools_unwired` check reads instead of the binding, so that check is unchanged. Binding happens once per member run and is never undone.

**At each boundary of the own loop**, with no `await` unless it parks:

1. Number it: `index` counts the loop's boundaries from 0. Classify `after` from the conversation's last message: `start` at index 0; `tools` after a tool result; `reply` after an assistant message with no tool calls, which only an agent that continues after a reply reaches; `user` after anything else (react's continue prompt, an incorrect-answer message, an operator's redirect after an interrupt).
2. If the swarm is stopping, return `None`, dropping any additions still queued. If no holder holds the member, return the queued additions appended to the conversation, or `None` when there are none. Only resume input queues additions today; push delivery's design adds its own producer to the same queue.
3. Otherwise park. Write the `held` event, set `held` to the boundary, wake `wait_held()` callers and recheck the swarm's waits; then wait for the release inside inspect_ai's `sample_wait()`. On release write the `resumed` event, clear `held`, recheck the waits, and continue as step 2.

**Holders.** A gate holds a set of holder names, and the member parks while the set is not empty. The controller's operations use the holder `controller`. A later design adds its own (#119's operator, say) without changing this one, so an operator's pause and a controller's schedule compose: a controller's `resume()` does not release a member the operator still holds.

### The controller's operations

`MemberRun`, which is the handle the runtime gives controllers, gains the operations; `TurnBoundary` is exported from `inspect_swarm`.

- **`hold()`** asks the member to park at its next boundary, and returns at once. A pending member parks at boundary 0, after its `start()`, before its first generation; a running one when its current turn ends, after its generation and tool calls, so a long tool call or a synchronous subagent delays the hold. The request stays until `resume()`, and a second `hold()` changes nothing. It raises:
  - `ValueError` for a member the seam does not support ([Supported members](#supported-members)), naming the member and why;
  - `RuntimeError` for a member that has ended, for one that has started background work or was found unable to park, and once the swarm is stopping, as `start()` raises then.
- **`wait_held()`** returns the member's `TurnBoundary` once it is parked (at once if it already is), or `None` if the member ends first. It raises `RuntimeError` when the member is found unable to park ([Supported members](#supported-members)); several callers may wait at once, and cancelling a wait withdraws no hold.
- **`resume(input=None)`** withdraws the controller's hold. When the member is parked and no other holder holds it, it continues, with `input` (a string is one user message; a list is used as given) appended before its next generation. Each park ends at its own release, so a `hold()` after `resume()` applies to the next boundary: `resume(); hold(); await wait_held()` runs exactly one turn, which is P4's `model_step` activation. `resume()` with no hold is a no-op; `input` for a member that is not parked raises `RuntimeError`, since there is no boundary for it.
- **`held`** is the boundary the member is parked at, else `None`.

A held member's state is `running`, its status unchanged, and the bus delivers to it as to any running member. `events()` and `SwarmEvent` are unchanged: no event kind is added, so controllers that read `event.submitted` untested, as `leaderless` does, are unaffected; persistent members add their idle and woken events. The handle still carries no member text: a `TurnBoundary` holds a number and the kind of the last message.

### Stopping

- **`stop(reason)` releases every hold** in its synchronous first part, after it closes the levers: each gate clears its holders and releases its park with `by: "stop"`, and from then on `hold()` raises and a boundary never parks. Additions queued at a parked boundary are dropped, and the `resumed` event counts them.
- **A released member continues to its next generation, which its closed lever refuses** before any call, so it ends with the stop's status (`stopped`, or a cap's), as a member between turns would. Held members therefore end at once, need no grace or hard stop, and leave no cancelled loop in the ledger.
- **Outside cancellations** (the sample's limits, an operator's interrupt of the sample, a member time limit, the hard stop) cancel the park like any other `await`, and the member ends as it would elsewhere. Token, cost, turn and message limits cannot fire while a member is parked, because it makes no call.

### Time

| | Held time |
|---|---|
| The member's time limit | counts: it is Inspect's wall-clock deadline ([open question 2](#open-questions)) |
| The swarm's time cap | counts |
| The sample's time limit | counts |
| The sample's working time | a sample wait: working only while other known work runs in the sample; a fully held swarm stops it |
| The metrics' `running_time` | counts; `held_time` (new) gives the held part |
| `swarm_wait`'s idle time | not idle: a held member has no wait |

This is how inspect_ai's own `pause --now` treats a held call. For `swarm_wait`, a held member counts as not active: the all-waiting check becomes "no member is running, not waiting and not held", and the gate rechecks the waits when a member parks and when it is released. So when every other member is waiting or held, the waits return (`all_waiting`) rather than wait for members that will not move until a controller resumes them.

A member time limit that fires while the member is parked ends it `member_limit:time`, and the ledger marks it `cancelled` as it marks any member time limit, though no call was in flight; the `held` event without a `resumed` event shows it.

### Supported members

| Member | Seam |
|---|---|
| `react()`, `react(submit=False)`, `deepagent()` without background dispatch | Its top-level loop. A loop that ends at a submission or a tool-free reply has no boundary after it; to be held after a reply, the agent must continue after one (an `on_continue` that returns the state), which is #116's to decide. |
| Nested loops: subagents, handoffs, `as_tool()` agents, `deepagent()`'s synchronous `general()` subagents | Never held on their own; they run inside a turn of the member's own loop, and a hold takes effect when that turn ends. |
| `deepagent(background=True)` or an integer `background` | `hold()` raises `ValueError`, detected from the agent's registry name, `inspect_ai/deepagent`, and its `background` argument, as M1 refused these members. Parking the top-level loop would leave its background subagents generating, which breaks every use's guarantee that a held member does nothing. |
| A custom agent that calls `background()` itself | `hold()` raises `RuntimeError` once the member has started background work. Work it starts after the hold was asked for keeps running while it is parked; the `held` event records how many tasks were running. |
| Bridged members, declared or in the `inspect_swe/` namespace | `hold()` raises `ValueError`: the CLI runs its own turns, and no agent channel is opened. |
| Members found unable to park: a bridged member found by its first bridged call, or an agent with no agent channel of its own | Found at a model call the member makes before its own loop has ever reached the gate, in the same node checks that find bridged members (`_budget.py:344-355`). From then on `wait_held()` raises `RuntimeError` naming the member and the reason, and so does `hold()`. |
| A nested swarm inside a member | Its members have gates of their own, installed by its own runtime; the outer member's gate never binds their loops. |

### Evidence

Two `InfoEvent`s of type `turn`, in the member's span, with the swarm's evidence fields (`version`, `type`, `kind`, `member`, `span_id`):

- **`held`**, when the member parks: `boundary`, `after`, `holders`, `messages` (the conversation's length) and `background_running`.
- **`resumed`**, when the park ends: `boundary`, `held_for` (seconds), `by` (`resume` or `stop`), `appended` (the ids of the messages added), `dropped` (additions dropped at a stop) and `messages`.

Neither carries text. The metrics gain two fields per member, `holds` and `held_time`, which default to 0 so that metrics written before them load.

### What this replaces in swarm-orbit.md

- **P4, The seam.** The gate replaces `swarm_on_continue()` and its steps: there is no hook to wire, no `no_tool_calls` parameter and no `scheduled_unwired` check, since a member that cannot be held is refused. A conversational ORBIT member is an agent that continues after a reply; its reply boundaries have `after == "reply"`.
- **P4, `activate()`.** It becomes a composition of the seam: `model_step` is `resume(input); hold(); await wait_held()`, and `completion` repeats that until `after == "reply"` or the member ends. The first activation of a pending member is `start()` with `hold()` asked for before, then that composition from boundary 0. `ActivationResult`, the `schedule` events and the read cutoff stay P4's; `parked` is `held`.
- **P4, Supported members** becomes this design's table: the same refusal of background members, and an error instead of an unscheduled run for members that cannot park.
- **P4, Stopping a scheduled swarm.** Releasing at the stop's first step is this design's. The member wrapper needs no stop cause on a normal return, because a released member never returns normally: its lever refuses its next generation. So a conversational member released at a reply ends `stopped`, not `submitted` as P4 had it; ORBIT's solver wrapper reads its last output either way ([ORBIT's solver wrapper](swarm-orbit.md#orbits-solver-wrapper)).
- **P5.** `append` is `resume(input=)`'s addition, and `edit` commits as the gate's replacement of the conversation. The staged copy, `snapshot()`, `intervened` events and the order of operations stay P5's, as does whether controllers get member text (open question 1).
- **P2, option (c).** Stamping a read's result message with the read id moves from `swarm_on_continue()` to the gate, which sees the turn's tool results at the next boundary and can return copies with metadata. It still covers only the member's own loop.
- **Open question 5** is answered for the seam. Whether ORBIT's channels keep notices stays the port's decision; notices would be additions at the boundary.
- **Alternatives considered**, the turn gate: its reason for rejection, one boundary split between a notice hook and a gate, went with the hook.

## Alternatives considered

**An `on_continue` wrapper in inspect_swarm** (swarm-orbit.md's `swarm_on_continue()`). Needs no inspect_ai change, and a spike showed parking works on unmodified `react()`. But every member's author must wire it in, which the removal of notices ended for good reason (decision: Ransom, 2026-10-27), and it never runs before the first generation or after a recovered overflow or interrupt, so those turns cannot be held. A hold asked for before a member's first turn would wait a whole turn. Rejected.

**A hold at the model call**, in a generate hook as inspect_ai's `pause --now` holds one. It would also hold bridged members and every loop. But a model call is not a turn boundary: it comes mid-turn for a subagent, a parallel call, compaction and each retry, so a hold there leaves tool calls in flight, and it cannot change the conversation. The process-wide hooks and the connection slot held during the call add their own problems ([swarm-orbit.md Alternatives considered](swarm-orbit.md#alternatives-considered)). Rejected; it stays the only route to pausing a bridged member, if #119 wants that.

**A public gate on `react()`** (`react(turn_gate=...)`). An explicit API, but it is wiring again, and it misses custom loops that follow the agent channel's documented pattern. Rejected.

**`Steer` items posted between turns.** The planned agent-channel item can add messages at a boundary, but not hold one, and every post meets ACP's binding. It stays urgent steering's mechanism. Rejected for the seam.

**One `run()` per activation on a stored state.** Rejected as in swarm-orbit.md: it needs clean `react()` re-entry and loses per-run state.

**A gate that returns messages to append.** A narrower contract, but P5's `edit` would then need a second inspect_ai change. Rejected in favour of returning the conversation.

**A `MemberHeld` event.** It would let a coordinator see holds in `events()`. But `leaderless` reads `event.submitted` without testing the event's type, and `wait_held()` serves every use here. Left to persistent members, which add idle events with their own meaning.

## Compatibility and migration

- **inspect_ai.** Without a gate nothing changes. The gate is a private module; inspect_swarm needs an inspect_ai release that has it.
- **inspect_swarm's API** gains the handle operations and `TurnBoundary`, additions only. `MemberHandle` is a protocol that only the runtime implements. `SwarmEvent` and `MemberState` are unchanged.
- **Behaviour without holds.** None, except where the member's own loop is bound: now when its first agent channel calls the gate, before any tool resolves, rather than at the first `swarm_tools()` resolution. For `react()` and `deepagent()` members it is the same loop. A custom agent that resolves `swarm_tools()` before it opens any channel keeps that binding, and the gate follows it. `tools_unwired` reads the new flag and is unchanged.
- **Logs.** New `InfoEvent` kinds of a new type, and two metrics fields with defaults; logs written before load unchanged. The result record and the ledger are unchanged. The viewer has no generated types for these.

## Security

- **Untrusted input reaching the new code.** Every member's conversation (model output, tool results, peer text inside tool results) passes through the gate. The gate reads only message types, the conversation's length and message ids, and neither the evidence nor `TurnBoundary` carries text. `after` follows from the model's own output (a reply or tool calls), as a member's `submitted` does.
- **Controller input.** `resume(input=)` is a second path, beside `start(input=)`, for controller-authored text into a member's conversation. Controllers see no member text, so they cannot relay one member's output into another's input through it; swarm-api.md's rule holds.
- **No member reaches the seam.** No tool exposes `hold()` or `resume()`, and the gate's `ContextVar` is set only by the member wrapper. Trusted code in the runner process could set another gate, as it could set `background_target`.
- **Holding does not stop the sandbox.** Processes a member left running keep running while it is held.
- **A controller that never resumes** holds members until the swarm's time cap or the sample's limits end the swarm, as with any controller that waits forever.

## Testing

All with mockllm, no network or Docker.

- **inspect_ai** (`tests/agent/test_channel/test_channel.py`, `tests/agent/test_agent_react.py`):
  - `before_turn()` awaits the gate before draining, with the channel and the messages; `None` leaves the list unchanged; a returned list replaces it in place, past the message limit raises the limit error, and on a tuple raises `TypeError`;
  - a nested `agent_channel()` gets the same gate; the inert channel never calls one; with no gate, `before_turn()` is unchanged;
  - a `react()` loop calls the gate before its first generation, after a turn with tool calls, after a recovered context overflow (a mock output with `stop_reason="model_length"`), and after an interrupt recovery.
- **inspect_swarm** (`tests/test_turn_seam.py`, new, on asyncio and trio through `tests/conftest.py`):
  - a pending member asked to hold parks at boundary 0 with `after == "start"` and no model call; `resume(input=...)` puts the input in its next model input;
  - a running member parks after its current turn (`after == "tools"`); `resume(); hold(); await wait_held()` runs one turn, and two members alternated this way call their tool in round-robin order;
  - a `react(submit=False)` member whose `on_continue` returns its state parks with `after == "reply"`; a member that submits before its next boundary returns `None` from `wait_held()`;
  - holders compose: a second holder keeps the member parked after the controller's `resume()`;
  - a stop while held: the member ends with the stop's status, makes no model call, and its ledger line is not `cancelled`; `hold()` then raises, and queued input is dropped and counted;
  - a member time limit while held ends `member_limit:time`;
  - every member held: the sample's working time stops while the held wall clock passes (a real sample, through `eval_async()`, as `tests/test_binding.py` runs one);
  - a member in `swarm_wait` is released with `all_waiting` when the only other running member parks;
  - refusals: `ValueError` for a declared bridged member and for `deepagent(background=True)` and `background=4`; `RuntimeError` for an ended member, for a member that has started background work, and for a custom agent that generates without opening an agent channel (its pending `wait_held()` raises at its first model call);
  - a `deepagent()` member's synchronous subagent never parks, and the hold takes effect when the subagent's tool call returns;
  - the `held` and `resumed` events' fields, with no text; the `holds` and `held_time` metrics; `tools_unwired` still names a `react()` member in a shared-memory swarm that was not given `swarm_tools()`.

## Implementation plan

1. **inspect_ai: the turn gate.** `src/inspect_ai/agent/_channel/gate.py` (new), `channel.py` (`before_turn()` and the channel's gate), `__init__.py` (`agent_channel()` reads the gate); the inspect_ai tests above.
2. **inspect_swarm: the seam.** Needs step 1 in an inspect_ai that inspect_swarm can depend on.
   - `src/inspect_swarm/_seam.py` (new): `MemberGate`, `TurnBoundary`, binding, classification, parking and its evidence.
   - `src/inspect_swarm/_member.py`: install the gate around the agent call; the background-dispatch check for `hold()`.
   - `src/inspect_swarm/_controller/_control.py`: the handle operations on `MemberRun`, release at `stop()`, `MemberWiring` from the tools flag.
   - `src/inspect_swarm/_budget.py`: the `tools` flag on `MemberContext`; finding a member that cannot park in the node checks.
   - `src/inspect_swarm/_bus/_tools.py`: set the tools flag; `src/inspect_swarm/_bus/_wait.py`: held members are not active.
   - `src/inspect_swarm/_evidence.py`, `src/inspect_swarm/_metrics.py`, `src/inspect_swarm/__init__.py`.
   - Docs: `docs/controllers.qmd` (holding members), `docs/analysis.qmd` (the events and metrics).
   - Tests: `tests/test_turn_seam.py`.

## Open questions

1. **The inspect_ai change.** The seam needs the gate in `before_turn()`, a behaviour change in inspect_ai (inert without a gate). Recommendation: make it. It is the only place that covers every turn of every `react()`-based member with no wiring, and it serves all the uses with one primitive.
2. **Member time limits while held.** Held time counts towards a member's time limit, as it does for inspect_ai's `pause --now`. The alternative is an active-time limit that pauses while held, which needs a swarm time node in place of Inspect's `time_limit()`. Recommendation: wall clock now; a use that needs active time, such as an idle persistent member under a time limit, brings that node in its own design.
3. **Background members.** They are refused. The alternative is to quiesce: park the member's loop, and count the member held once its background work finishes, which could take a long time. Recommendation: refuse until a use needs it; holding background subagents themselves is its own design on top of the background owner.

## Not this design

- **Holding a member's background work**, by gating its background tasks' loops, on top of the per-member background owner ([swarm-background-members.md](swarm-background-members.md)).
- **Pausing bridged members**, through a hold at the model call or inspect_swe's restart with `--resume`.
- **Active-time member limits** ([open question 2](#open-questions)).
- **Idle and woken events, and a stop for one member**, for persistent members (#116) and operator controls (#119).
- **The ledger's `cancelled` for a time limit that fires while a member is held**, which no call was in flight for.
- **Updating swarm-orbit.md's P4, P5 and open question 5** to the seam, in the ORBIT port's own design work.
