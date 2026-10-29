# Inspect Swarm: persistent members

## Overview

A persistent member stays available after it answers. It is declared on its record, `member(worker, persistent=True)`, and runs inside one `react()` (or `deepagent()`) loop for its whole life: when it submits, or when a `react(submit=False)` member gives a tool-free reply, the swarm records that as one of its *answers* and holds the member *idle* at its next turn boundary instead of letting the loop return. The controller hears `MemberIdle`, and hands the member its next piece of work with `SwarmControl.wake(member, input)`, which appends the input to the member's conversation, so each new activation keeps everything the member has seen and done. While a member is idle, a write it would care about (one that would end a whole-board `swarm_wait` of its own loop) reaches the controller as `MemberNews`, and the controller decides whether to wake it. A swarm whose members are all idle or ended, with no event waiting, is *quiescent*: the controller's `events()` iterator ends, and the controller returns `quiescence`, wakes someone, or starts someone.

The mechanism is the turn seam ([swarm-turn-seam.md](swarm-turn-seam.md)) plus one private hook in inspect_ai: a `react()` loop that has an answer and would return it first asks the swarm's turn gate, through `after_answer()`, and the gate says "continue" for a persistent member, so the loop goes on to its next turn boundary, where the gate holds it idle. A persistent member's limits span its whole life; its member time limit does not run while it is idle. Every answer is a candidate for the swarm's answer: `first` and `last` choose across all answers, `choose_answer()` takes the member's latest, and the result record keeps the time of each answer and which one was chosen. An idle member that the swarm's stop or `end()` reaches ends `submitted`, its last answer its output. Members that cannot be held this way (bridged members, background dispatch, custom loops, which cannot ask at their answers), and members whose shared memory only a subagent reads, are refused with an error.

## Why

A member's run ends when its agent returns ([swarm-api.md](swarm-api.md#controllers)). A `react()` member that submits is finished, and `start()` on it raises, so a coordinator cannot hand a member a task, collect its result and send the same member the next task with its context intact ([#116](https://github.com/meridianlabs-ai/inspect_swarm/issues/116)). Coordinator topologies (a lead or a controller dispatching work to workers) and long-running swarms need members that persist across activations, and termination by quiescence needs members that can be idle rather than ended ([swarm.md](swarm.md#members), [swarm-wait.md Room for later work](swarm-wait.md#room-for-later-work)).

The turn seam gives the runtime a held member and a boundary at which to change its conversation, but leaves this design four questions ([swarm-turn-seam.md Non-goals](swarm-turn-seam.md#goals-and-non-goals), [Supported members](swarm-turn-seam.md#supported-members)): how a member continues past an answer, since a loop that ends at a submission or a tool-free reply has no boundary after it; what an activation's input is; how limits, the ledger and the result record count several activations; and which of a member's answers the swarm's answer can be.

## Goals and non-goals

Goals:

- Members that answer, stay idle, and are woken with new work, keeping their conversation, tool state and limits, for `react()`, `react(submit=False)` and `deepagent()` members, with no wiring by the member's author beyond `persistent=True`.
- A controller surface for it: the idle state, `MemberIdle` and `MemberNews` events, and `wake()`.
- Quiescence: the controller can tell that nothing in the swarm will move unless it acts.
- A defined answer: every answer recorded, the final-answer rules and `choose_answer()` defined over answers, and the record and evidence saying which answer was chosen.
- A clear error for every member that cannot persist.

Non-goals:

- **A built-in coordinator controller** and its `lead_done` reason ([swarm-api.md](swarm-api.md#controllers) sketches it). Controllers that wake members are the eval author's code; this design gives them the surface and two examples.
- **Per-activation limits.** A persistent member's limits span its life.
- **Holding bridged members or a member's background work**, which the seam does not hold ([swarm-turn-seam.md Not this design](swarm-turn-seam.md#not-this-design)).
- **Delivery to pending members**, or starting a member on its first message.
- **Scores per answer** (per-member and per-answer scoring is swarm-scoring.md Part 2's).

## Current behaviour

Code references are to inspect_swarm `main` at `33bf17b` and inspect_ai `main` at `6f5f9c20a`. The turn seam is specified in [swarm-turn-seam.md](swarm-turn-seam.md), and this design assumes it: its private turn gate in inspect_ai's agent channel, the member's `MemberGate` in `src/inspect_swarm/_seam.py`, `Boundary`, `on_boundary()`, `end()` and `held` on the handle, the hold's effect on time limits and `swarm_wait`, and its `boundary` and `changed` evidence. Where this section says "the seam", it means that design.

### How a `react()` loop ends

- **A submitting `react()`** checks each turn's tool results for a submission (`src/inspect_ai/agent/_react.py:313-314`). With its attempts used up it breaks out of the loop (`:334-337`); with attempts left it scores the submission and breaks if it passed (`:340-345`), or appends the incorrect-answer message and continues (`:347-368`). The loop never calls `on_continue` after a submission that ends it. After the loop, the submit tool's calls and results are removed from the conversation unless `keep_in_messages` (`:419-425`).
- **`react(submit=False)`** ends at its first tool-free reply when it has no `on_continue` (`:573-574`), or when its `on_continue` returns `False` (`:571-572`). An `on_continue` that returns the state continues after a reply with nothing added (`:568-570`).
- Both loops also end, without an answer, after three consecutive content-filter refusals (`:292-295`, `:533-536`) and after a context overflow they cannot recover (`:283-284`, `:524-525`); the swarm reads those outputs as `unsubmitted` (`src/inspect_swarm/_member.py:612-621`).
- Every iteration starts with `before_turn()` (`_react.py:263`, `:504`), which is where the seam's gate runs.
- **Re-entry duplicates the system prompt.** Calling a `react()` agent again on the state it returned inserts its system message at index 0 a second time (`_react.py:215-216`, `:454-455`). A spike ran one `react(prompt=...)` on mockllm inside a sample, submitting `one`, appended a user message and called it again, submitting `two`: the roles after the first call were `system, user, assistant` (the submit tool's result removed), and after the second `system, system, user, assistant, user, assistant`, two system messages.
- **Compaction lives in the loop**: react builds its compaction handler once per loop, with the system messages and the starting input (`partition_messages()`'s `system` and `input`) as its always-kept prefix (`_react.py:242`, `_agent_compact` at `:692-713`). The handler sends only messages whose ids it has not processed (`src/inspect_ai/model/_compaction/_compaction.py:186-188`), so a message appended with an id it has already processed never reaches the model.

### The member's run

- `start()` refuses a member that is not `pending`: "a member runs once" (`src/inspect_swarm/_controller/_control.py:331-334`). `MemberState` is `pending`, `running` or `ended` (`src/inspect_swarm/_record.py:6`).
- A normal return is `submitted` unless the output's stop reason is `content_filter` or `model_length` (`_member.py:578-588`, `:612-621`). The runtime's `_end()` marks the member ended, releases the bus's waits, writes `member_result` and queues `MemberEnded(member, status, submitted)` (`_control.py:603-627`). `MemberEnded` is the only controller event (`_control.py:90-105`).
- `events()` ends when no member is `running` and no event is queued (`_control.py:354-371`, `_running()` at `:691-692`). The stop closes the levers of `running` members (`:544-546`), and the supervisor's grace and hard stop wait for and cancel `running` members (`:653-664`, `_until_quiet()` at `:694-696`).
- `quiescence` is already one of the topology stop reasons a controller may return as it is (`_control.py:173`).

### The answer

- `first` and `last` choose among members with `submitted` set, by the swarm time at which each ended (`MemberResult.time`), ties to roster order (`src/inspect_swarm/_final.py:30-40`). `choose_answer()` takes a member that has ended with a submission, once (`_control.py:373-386`). `settle()` writes the provisional answer after every change, from the ended members' outputs (`_control.py:630-647`, `_final.py:74-90`); the final step appends the chosen output's message to the sample's conversation (`_final.py:125-137`).
- The record: `MemberResult` has one `status`, `submitted` and `time` per member (`_record.py:12-46`); `FinalRecord` names the chosen member, not which of its outputs (`_record.py:49-62`).
- The metrics' `steps_to_answer` counts the answering member's steps through its end, and `critical_steps_to_answer` ends its chain at the answering member's `member_result` event (`src/inspect_swarm/_metrics.py:95-112`, `src/inspect_swarm/_critical_path.py:57`, `:83`).

### The bus and waits

- `Bus.running(name)` is `state == "running"` (`src/inspect_swarm/_bus/_bus.py:197-199`). The bus refuses records addressed to pending or ended members (`_resolve()`, `:234-251`) and from a sender that is not running (`_liveness()`, `:253-262`), so only a running member's tools deliver.
- Step 4 of a delivery applies the record and tells the waits (`_bus.py:189-195`); a `wake=True` record releases each wait for which the channel's `wakes(spec, record)` holds (`src/inspect_swarm/_bus/_wait.py:105-113`). The shared memory's `wakes()` is true for a file another loop wrote that is news to the waiting loop under its filter (`src/inspect_swarm/_channel/shared_memory.py:419-424`), and `news()` lists what is news now (`:426-440`).
- The all-waiting check releases every wait when no member is `running` without waiting (`_wait.py:133-146`).
- The member's own loop for the bus is the agent channel in which `swarm_tools()` is first resolved (`src/inspect_swarm/_bus/_tools.py:17-22`); its agent span is not recorded on `MemberContext` (`src/inspect_swarm/_budget.py:300-320`). The shared memory keys what a loop has seen by its agent span, from `current_agent_span_id()` when the loop calls the tool (`shared_memory.py:525-528`), and a view marks the file seen for that span only (`:884`), so each loop has its own view of the board ([Subagents](../docs/shared-memory.qmd#subagents)).
- The bus's binding is usually the member's top-level loop, but not when only a subagent is given `swarm_tools()`: with `research(extra_tools=[swarm_tools()])` under a `deepagent()` whose own tools omit them (`docs/shared-memory.qmd:115`), the first research child binds the bus, then returns and closes, and each later child runs in a new span (`src/inspect_ai/agent/_deepagent/agent_tool.py:355`). The seam keeps its own turn-loop binding beside this one and holds the parent ([swarm-turn-seam.md The member's gate](swarm-turn-seam.md#the-members-gate)). No example uses that shape; the docs describe it.

### Real use

Member shapes in use are `react()` and `deepagent()` members in the examples and docs (`examples/*.py`, `docs/members.qmd`), `deepagent(background=...)` members, inspect_swe's bridged agents, and test-only custom agents (`tests/swarm_runtime.py`). No controller in the repository, the docs or the examples wakes a member; the coordinator sketches in [swarm-api.md](swarm-api.md#controllers) assume persistent members. The uses this design serves are a controller that dispatches its own list of tasks to workers, and a lead member that posts work on the shared memory for workers that the controller wakes on the lead's writes.

## Design

### At a glance

```python
# inspect_ai: src/inspect_ai/agent/_channel/gate.py (the seam's private module), plus:
class TurnGate(Protocol):                  # the seam's, plus:
    async def after_answer(self, channel: AgentChannel, state: AgentState) -> bool: ...

async def after_answer(channel: AgentChannel, state: AgentState) -> bool: ...   # False without a gate


# inspect_swarm
def member(..., persistent: bool = False) -> Member: ...

MemberState = Literal["pending", "running", "idle", "ended"]

class MemberHandle(Protocol):              # the existing handle and the seam's, plus:
    @property
    def persistent(self) -> bool: ...
    @property
    def answers(self) -> int: ...          # answers recorded so far

class SwarmControl(Protocol):              # the existing surface, plus:
    def wake(self, member: str | MemberHandle, input: str | list[ChatMessageUser]) -> None: ...

@dataclass(frozen=True)
class MemberIdle:
    member: str
    answer: int        # the index of the answer it just gave, from 0
    news: bool         # whether a channel already has news for its own loop

@dataclass(frozen=True)
class MemberNews:
    member: str        # the idle member the record is news to
    channel: str
    record: str        # the record's id
    sender: str        # the roster name of the member that wrote it

ControlEvent = MemberEnded | MemberIdle | MemberNews   # the controller event alias

@dataclass(frozen=True)
class Boundary:        # the seam's, plus:
    activation: int    # 0 until the member is first woken
    after: Literal["start", "tools", "reply", "user", "wake"]
```

`ControlEvent` is the controller event alias, called `SwarmEvent` until the evidence switch renames it ([swarm-event-types.md](swarm-event-types.md#swarmevent)), as in the seam.

### Declaring a persistent member

`member(..., persistent=True)` sets `Member.persistent`. The field is logged only when it is `True`, so members that are not persistent log exactly as before. A member is persistent for its whole run; there is no switch at run time.

`swarm()` refuses, with `ValueError` naming the member, in a new `_check_persistent()` among its roster checks (`src/inspect_swarm/_swarm.py:66-90`), which runs whatever the channels, unlike the bridged check in `_check_bus_channels()` (`:291-325`):

- a persistent member that is bridged (declared, or in the `inspect_swe/` namespace): the CLI runs its own turns and the seam cannot hold it;
- a persistent `deepagent()` with `background=True` or an integer `background`, detected from its registry name `inspect_ai/deepagent` and its `background` argument as the seam detects it for `on_boundary()`.

**The bus's reader must be the turn loop, when there is news to read.** A persistent member's idle news is reported for the loop that the wake continues, so the loop that reads the board for the member (the bus's binding, `MemberContext.loop`) must be the loop the seam holds (`MemberContext.turn_loop`). When the board is given only to a subagent, the bus binds a child loop that closes when the child returns, and later children read under spans of their own, so no loop that survives the answer could clear the news: a controller waking on news would wake the member again and again for writes it already read. The gate refuses that member, but only in a swarm with a waiting channel, one that sets `max_wait` and so wakes waits and gives idle news (the bus's `waiting` states, `src/inspect_swarm/_bus/_bus.py:90-92`; today `shared_memory()`). With `channels=[]` or only `filesystem()` there is no news to report and nothing to acknowledge, so the binding does not matter and the member persists: `_SwarmTools.tools()` binds the first loop that resolves it whatever tools it returns (`_bus/_tools.py:20`), and one member definition serves swarms with different channels (`:42`). At every boundary of a persistent member's turn loop and at the start of every `after_answer()` from it, if the swarm has a waiting channel and `MemberContext.loop` is bound and is not the turn loop, the gate raises `RuntimeError` naming the member and saying that a persistent member that reads the swarm's channels must have `swarm_tools()` in its own tools, not only in its subagents'. Both checks run in the member's own top-level loop, so the error ends the member `errored` whichever subagent bound the bus; the child that bound it runs inside one of the parent's turns, so the parent's next boundary or answer comes before any answer is recorded or any news is asked. A member whose top-level loop has `swarm_tools()` and passes them to subagents too is unaffected: the top-level loop resolves its tools first and binds.

A persistent member counts as one with a callback registered for the seam's never-reached rule: a custom agent that never reaches a turn boundary (`generator()` and `sleeper()` in `tests/swarm_runtime.py`) ends `errored`, and its output is never an answer ([swarm-turn-seam.md Supported members](swarm-turn-seam.md#supported-members)).

### Continuing past an answer: `after_answer()`

**inspect_ai.** The seam's private module `_channel/gate.py` gains a function, `after_answer(channel, state)`, and its `TurnGate` protocol the entry point of the same name. A react loop that has an answer and would return it calls the function first with its channel, and if it returns `True`, continues to its next turn instead of returning. Without a gate on the channel (a loop outside a swarm, or the inert channel) it returns `False` at once, so nothing changes. With one it returns `await gate.after_answer(channel, state)`. It stays private, as `before_turn()`'s gate is: no public `AgentChannel` method is added, so inspect_ai's API is unchanged and only its own loops can persist (decision: Ransom, 2026-10-29). A custom loop that a persistent member runs ends `errored` ([Endings](#endings)); the hook can be made public later if a custom-loop member needs it.

Both react loops call it at every end that is an answer, and nowhere else:

- **`react()`**: a submission with its attempts used up (`_react.py:334-337`), and a submission whose check passed (`:340-345`). When it returns `True`, the loop sets `attempt_count` back to 0, so each activation has its configured attempts, and goes to the top of the loop without calling `on_continue`, as a submission that ends the loop today skips it. The submit tool's call and result stay in the conversation, so the model sees that it answered; `keep_in_messages=False` still removes them if the loop ever returns.
- **`react(submit=False)`**: a tool-free reply with no `on_continue` (`:573-574`), and an `on_continue` that returns `False` after a tool-free reply (`:571-572`). When it returns `True`, the loop goes to the top of the loop.

The ends that are not answers (content-filter refusals, an unrecoverable overflow, a submitting `react()`'s `on_continue` returning `False`) do not call it and end the loop as today. `deepagent()`'s top-level loop is a `react()`, so it gets the same calls; its subagents' loops are react loops that ask too, and the gate answers them `False` ([The gate](#the-gate)).

The loop's `state` is the one it would return: for a submitting `react()`, the output with the answer as its completion (`:315-332`).

### The gate

The member's `MemberGate` ([swarm-turn-seam.md The member's gate](swarm-turn-seam.md#the-members-gate)) gains `after_answer()` and an idle step at the boundary that follows an answer.

**`after_answer(channel, state)`**, synchronous apart from its signature (no `await`):

1. If the call is not from the member's turn loop (the seam's binding: the member's own task, the bound channel), or the member is not persistent, return `False`. A nested loop's answer, such as an `as_tool()` subagent's, returns to its caller as today.
2. If `end()` was called for the member, return `False`: the loop returns and the member ends `stopped`, its submission not an answer, as the seam rules for an end request that a final submission meets ([swarm-turn-seam.md Ending a member](swarm-turn-seam.md#the-callback-contract)).
3. Otherwise record the answer: its index (0 for the member's first), the activation it ended, the swarm time, and a detached deep copy of `state.output`. Mark the member `idle`, write the `answer` evidence ([Evidence](#evidence)), settle the record (the provisional answer may now be this one), queue `MemberIdle(member, answer, news)` and notify the controller's iterator. Return `True`.

A swarm stop does not change step 3: an answer given while the swarm stops is still recorded, and the boundary that follows ends the member at once, idle, so `submitted`.

**The idle boundary.** The loop goes from `after_answer()` to `before_turn()` with no generation or tool call between. At that boundary the gate's steps ([swarm-turn-seam.md](swarm-turn-seam.md#the-members-gate)) run with these changes:

- Step 1, the stop and end check, ends an idle member as `submitted` rather than `stopped` ([Endings](#endings)).
- Step 2 does not return early for an idle member: it is held even with no callback registered.
- Step 4, after the time-limit check, holds the member and then, before any callback, waits for its wake: in the gate's cancel scope, inside `suspend_working_limit()`, so idle time is waiting time for the sample's working limit. A stop or `end()` cancels the wait like any callback's, and step 5 ends the member. If `wake()` was called before the member reached the boundary, the wait returns at once.
- On a wake, the activation's input is appended to the staged conversation (the runtime's change, origin `swarm`), the boundary's `activation` is the new activation's index and its `after` is `"wake"`, and the callbacks run as at any boundary, seeing the input as the last messages. The chain's result is applied by the seam's rules: with the input alone, user messages only, it is an append. A callback that returns `"end"` there, or an `end()` after the wake, ends the member `stopped`: it was woken and has not answered in this activation.

Step 3's checks apply as at any boundary with callbacks: a member with background work running at the idle boundary ends `errored`.

The idle hold is a hold in every sense the seam defines ([swarm-turn-seam.md Time](swarm-turn-seam.md#time)): `held` is true, the member time limit is paused, `swarm_wait`'s all-waiting check counts the member as not active, and `holds` and `held_time` count it.

### Waking: `SwarmControl.wake()`

```python
def wake(self, member: str | MemberHandle, input: str | list[ChatMessageUser]) -> None:
    """Give an idle member its next piece of work, and return at once.

    The member continues its conversation with `input` appended: a string as one user message, a list of user messages as given. It keeps its tool state, its limits and what it has seen.

    Raises:
        KeyError: If no member has this name.
        ValueError: If `input` is an empty string, an empty list, or a list holding any message that is not a `ChatMessageUser` (a system message in particular).
        RuntimeError: If the member is not idle (pending members start with `start()`; a running member has not answered; an ended one has ended), or the swarm is stopping.
    """
```

It is synchronous, as `start()` is: it checks, builds the input's messages, sets the member's state to `running`, counts the activation, stores the input for the gate and releases its wait, settles the record and notifies. Every refusal comes before the member changes to `running`, so a refused wake leaves it idle. Its refusal once the swarm is stopping is `start()`'s `_Stopping`, so a controller that meets it ends with the stop, not with an error (`_control.py:176-186`).

**Every wake message gets a fresh id and no `source`.** A string becomes a new `ChatMessageUser`, and each message of a list is stored as a deep copy with a new id and its `source` cleared (`model_copy(update={"id": uuid(), "source": None}, deep=True)`, with shortuuid's `uuid()`, which inspect_ai's messages use for their own ids); the caller's messages are not changed. A controller may reuse one list for every wake, as `dispatch` below reuses its strings, or pass messages whose ids are already in the member's conversation. Appended with the caller's ids, the second wake's messages would repeat ids the conversation and the member's compaction already hold: the seam's rule that a conversation's ids are unique would break ([swarm-turn-seam.md The callback contract](swarm-turn-seam.md#the-callback-contract)), and compaction, which sends only messages whose ids it has not processed (`src/inspect_ai/model/_compaction/_compaction.py:186-188`), would silently leave that activation's input out of the next generation. Fresh ids rule the collision out for every input, with nothing for the caller to check and no refusal to define; the `changed` event records the new ids.

The input is eval-author text, as `start(input=...)` is: the controller sees no member text, so it can pass only its own ([swarm-api.md](swarm-api.md#controllers), "No member text"). Nothing else is added: the member's preamble was in its first input, and the swarm adds no message of its own ([swarm-communication.md](swarm-communication.md#how-members-learn-of-changes)). Wake input is user input only: a wake appends user messages after the member's answer, so the model does not continue from its own answer, and the change at the boundary is always an append. A system message is refused, not appended: the seam treats an added system message as a rewrite (`swarm-turn-seam.md` [The callback contract](swarm-turn-seam.md#the-callback-contract)), because react's compaction restores system messages only from the prefix it saved at loop start (`src/inspect_ai/model/_compaction/_compaction.py:244-247`), so an appended one would be lost at the next compaction. No controller passes anything but its own text; assistant or tool messages would put words in the member's mouth. A controller that needs another shape uses an `on_boundary()` callback, which the seam's rules cover. `source="input"` marks the starting input only (`src/inspect_swarm/_member.py:518-522` stamps it there), and the copy clears it from wake messages, since a caller may pass ordinary Inspect input that carries it (`sample_messages()` stamps it, `src/inspect_ai/_eval/task/util.py:24-26`): `partition_messages()` puts input-marked messages in the preserved input group (`src/inspect_ai/model/_trim.py:107`), which trimming and compaction emit before the conversation (`:88`), so a marked wake task would move ahead of the earlier activations' history and an older task would read as the latest instruction.

The swarm's waits are rechecked when the member is held and released, as for any hold.

### News for idle members: `MemberNews`

A member that is idle is not in a tool call, so nothing tells it of channel changes. The controller is told instead: the bus reports a delivered record to the runtime when it would end a whole-board `swarm_wait` of an idle member's own loop, and the runtime queues `MemberNews(member, channel, record, sender)`. The controller decides whether to wake ([swarm-communication.md Wake later](swarm-communication.md#wake-and-turn-boundaries)).

- **The member's own loop's span.** `MemberContext` gains `loop_span`, the agent span (`current_agent_span_id()`) of the loop that bound `MemberContext.loop`, recorded at the same binding in `_SwarmTools.tools()` (`_bus/_tools.py:20-22`). For a persistent member that loop is its turn loop, which lives across its activations ([Declaring a persistent member](#declaring-a-persistent-member) refuses the rest), so the span is one live reader whose views clear its news. The idle spec of a member is `WaitSpec(id=f"idle-{name}", member=name, loop=loop_span, own=True, path=None)`: what a whole-board wait of its own loop would watch.
- **At step 4 of every delivery**, after `Waits.delivered()`, with no `await`: if the record has `wake` set and its channel is a waiting channel (`WaitingState`), then for every idle member other than the sender whose `loop` is bound, if `state.wakes(idle_spec, record)`, the bus calls the runtime's news callback, which queues `MemberNews` and notifies. A `wake=False` record raises no event; the member sees it when it is next woken and looks.
- **At an answer**, `MemberIdle.news` is whether any waiting channel's `news(idle_spec)` is not `None`, so a controller learns of writes the member did not look at during its activation, which no later delivery would report.
- A member whose loop never bound `swarm_tools()` gets no news (`news` is `False`): it cannot read the channels.

Events can be stale when the controller reads them: two writes give two `MemberNews`, and the first wake makes the member `running`. A controller checks `swarm.member(name).state == "idle"` before it wakes on news; `wake()` on a running member raises.

The shared memory's `apply()` counts only running members in `notify`, the evidence's list of members a record is news to (`shared_memory.py:394-403`); it counts idle members too, since their own loop sees the record when woken. Nothing else in the bus changes: `Bus.running()` stays "state is `running`", so an idle member can neither send nor call a channel tool (it runs none), receives addressed records (only pending and ended recipients are refused), and is not active for the all-waiting check.

### Quiescence and the controller's iterator

`events()` keeps its rule, "ends when no member is `running` and no event is queued", and `running` excludes `idle`. So the iterator ends when every member is pending, idle or ended and nothing is queued, which is quiescence. It is stable: only a running member's tools deliver records (`_liveness()`, `_bus.py:253-262`), and an idle member has no background work running (the seam refuses a hold while it has any), so nothing can queue an event until the controller calls `start()` or `wake()`. A controller that has handled every event and wakes no one returns `quiescence`. There is no `Quiescent` event: the iterator's end says it.

The stop procedure covers idle members: `stop()` closes the levers of running and idle members, and the supervisor's grace and hard stop wait for and cancel members that are running or idle (`_control.py:544-546`, `:653-664`, `:694-696` move from `state == "running"` to "started and not ended"). An idle member ends at once at the stop, held as it is.

**`leaderless`** tests event kinds: it stops on a `MemberIdle` as on a submitted `MemberEnded` when `stop_on_submission` is set, since an answer is a submission, and when its iterator ends it returns `quiescence` if any member is idle, else `all_done`. It never wakes anyone, so under `leaderless` a persistent member answers once and stays idle until the stop.

### Endings

| How a persistent member ends | Status | Its answers |
|---|---|---|
| A stop or `end()` while idle | `submitted`, its last answer as its output | all eligible |
| A stop, `end()` or `"end"` while woken or working | `stopped`, as the seam rules | those already recorded, eligible |
| Its own limit, a swarm cap or the hard stop | as today | those already recorded, eligible |
| Its agent returns normally after an end request | `stopped` (the seam's rule) | those already recorded, eligible; the final one is not recorded |
| Its agent returns normally otherwise, with an `unsubmitted` output | `unsubmitted` | those already recorded, eligible |
| Its board read only by a subagent (the bus bound to a loop other than its turn loop) in a swarm with a waiting channel | `errored`, with the gate's `RuntimeError` ([Declaring a persistent member](#declaring-a-persistent-member)) | those already recorded, eligible (none, when the subagent ran before the first answer) |
| Its agent returns normally otherwise | `errored`, with a `RuntimeError` naming the member and saying that a persistent member must run a `react()` or `deepagent()` loop, which asks the gate at its answers, and its loop returned instead | none: the swarm fails, as for any foreign error |

The last row refuses loops that reach the gate but never ask at their answers (a custom loop that calls `before_turn()` only), and a `react(submit=False)` member whose `on_continue` returns `False` after a tool call. An idle member is ended through the gate as the seam ends a held member, by raising its lever's limit error; the member wrapper reads the idle mark before it classifies the status, and takes the member's output from its last answer.

An idle member reached by the hard stop before its ending is recorded can be `cancelled` in the ledger, as the seam accepts for any held member at a zero grace; its answers are unaffected.

### The swarm's answer

Every recorded answer is a candidate, whatever the member's later status (stopped, a limit, cancelled): an answer was complete when the member gave it, and a later activation is new work, so a worker stopped mid-task keeps its earlier results (decision: Ransom, 2026-10-29). A non-persistent member has one candidate, its output, when it ends `submitted`, at its end time, as today.

- **`first` and `last`** choose across all candidates by swarm time; ties go to roster order, then to the earlier answer. `first` is the earliest answer anyone gave, so a swarm whose workers answer subtasks before the answer that matters needs `last`, `choose_answer()`, or a controller that keeps those workers out of it.
- **`choose_answer(member, reason)`** takes the member's latest answer at the time of the call, and keeps it if the member answers again. For a persistent member it raises `RuntimeError` if the member has no answer yet, whatever its state; for other members it keeps today's rule.
- **The provisional answer** is settled after every answer, as after every end.
- **The final step** appends the chosen answer's output message to the sample's conversation, as today.

### Limits, ledger and record across activations

- **Limits.** A persistent member's limits (token, cost, turn, message, time) and its share of the swarm's caps are entered once, by the member wrapper, and span all its activations. The member time limit counts only time not held, so idle time is free; the conversation, and with it the message limit and the context window, grows across activations, with the member's compaction working on one loop as before.
- **The ledger.** One line per member, as today: its usage across all activations, its final status.
- **The result record** (`_record.py`), version 1 with fields that default:
  - `MemberState` gains `idle`, which appears only in provisional records;
  - `MemberResult.persistent: bool = False`;
  - `MemberResult.answer_times: list[float] = []`: the swarm times of a persistent member's answers, in order; empty for other members, whose answer is their submission at `time`;
  - `FinalRecord.answer: int | None = None`: for a persistent member, which of its answers is the swarm's answer, from 0; `None` otherwise.
- **Metrics** (`MemberMetrics`), with defaults: `activations: int = 0` (activations begun: 1 at start, one per wake) and `answers: int = 0`, both 0 for non-persistent members; and `awaiting_work_time: float = 0.0`, the part of the seam's `held_time` the member spent idle, from an answer to its wake or its end. (`idle_time` is taken: it is `swarm_wait`'s idle time.) `running_time` stays start to end.
- **The critical path and `steps_to_answer`** end at the chosen answer: for a persistent member at its `answer` event, counting the member's steps before it, rather than at `member_result`. For other members nothing changes.

### Evidence

New `SwarmEvent` kind ([swarm-event-types.md](swarm-event-types.md#swarmevent)), with `member` set, written in the member's span:

- **`answer`**, once per recorded answer of a persistent member: `answer` (index), `activation`, `time` (swarm time), `stop_reason` (the output's). No text: the answer's message is in the member's conversation and its `ModelEvent`s.

At the idle boundary, the seam's events, with additions:

- **`boundary`** is written at every idle boundary, callbacks or not, and gains `activation` (the activation the member continues into; absent when it ended there) and `idle_for` (seconds from the answer to the wake or the end). Its outcome is `continue` on a wake, and `stop` or `end` when the member ended idle.
- **`changed`** with origin `swarm` records the wake's input, as `added` ids, hashes and indices, before the callbacks' own events; `applied` follows the boundary's outcome as for any change.
- The `final` event gains `answer`, as the record does.

### Examples

A controller that hands one worker the eval author's tasks in turn, its last answer the swarm's:

```python
@swarm_controller
def dispatch(tasks: list[str], worker: str = "worker") -> Controller:
    """Give `worker` each task in turn, waking it after each answer."""

    async def run(swarm: SwarmControl) -> StopReason:
        pending = list(tasks)
        swarm.start(worker, input=pending.pop(0))
        async with aclosing(swarm.events()) as events:
            async for event in events:
                if isinstance(event, MemberIdle) and pending:
                    swarm.wake(event.member, input=pending.pop(0))
        return "quiescence" if swarm.member(worker).state == "idle" else "all_done"

    return Controller(run, final="last")


swarm(members=[member(worker_agent, name="worker", persistent=True)], controller=dispatch(tasks))
```

A lead that posts work on the shared memory for persistent workers, woken when the lead writes:

```python
WAKE = "There may be new work for you in the shared memory."


@swarm_controller
def lead_and_workers(lead: str = "lead") -> Controller:
    async def run(swarm: SwarmControl) -> StopReason:
        for m in swarm.members:
            swarm.start(m)
        async with aclosing(swarm.events()) as events:
            async for event in events:
                if isinstance(event, MemberEnded) and event.member == lead:
                    return "lead_done"   # recorded as controller:lead_done
                woken_by_lead = isinstance(event, MemberNews) and event.sender == lead
                idle_with_news = isinstance(event, MemberIdle) and event.news
                if (woken_by_lead or idle_with_news) and swarm.member(event.member).state == "idle":
                    swarm.wake(event.member, input=WAKE)
        return "quiescence"

    return Controller(run, final="last")
```

The lead ends by submitting; the workers stay idle until the stop and end `submitted`. With `final="last"` the lead's submission, the latest answer, is the swarm's answer when it ends last; a controller that must name it calls `choose_answer(lead)`.

### Relation to other designs

- [swarm.md Members](swarm.md#members) lists two candidates for persistence, a `submit=False` protocol and clean re-entry; this design takes neither ([Alternatives](#alternatives-considered)).
- [swarm-communication.md Wake later](swarm-communication.md#wake-and-turn-boundaries) sketches the news event for addressed messages; `MemberNews` is that event for every waiting channel, the later messages channel included once it implements `WaitingState` as [swarm-wait.md Room for later work](swarm-wait.md#room-for-later-work) plans.
- The seam's `"end"` is never a submission ([swarm-turn-seam.md The callback contract](swarm-turn-seam.md#the-callback-contract)); the submitted ending it leaves to this design is the idle ending above.

## Alternatives considered

**A `submit=False` member protocol** (swarm.md's first candidate). Members would run `react(submit=False)` with a swarm `report` tool that records an answer, and an `on_continue` that returns the state after a reply so the seam sees an `after == "reply"` boundary. It needs no inspect_ai change beyond the seam. But a standard `react()` worker, with its submit tool and prompt, could not persist: its author would rebuild it with another prompt and stop condition, and wire a swarm-specific hook. Rejected for `after_answer()`, which covers the agents members already are.

**Clean re-entry** (swarm.md's second candidate): the member wrapper calls the agent again on the state it returned, with an inspect_ai change so `react()` does not insert its system prompt twice. It would also cover custom agents that return. But each activation opens a new loop: the seam binds one loop per member and refuses a second when callbacks are registered (decision: Ransom, 2026-10-29), compaction would be initialized again for each activation (`_react.py:242`), MCP connections and the agent channel would be reopened, and the bus's loop binding would point at a closed channel. The spike showed the duplicated system message and the submit tool's results removed between activations. Rejected.

**Letting the gate wait inside `after_answer()`.** The gate would hold the member and wait for the wake in the answer call itself, and return with the input appended. It saves the step to `before_turn()`, but the hold would happen outside the seam's boundary: no `Boundary` for callbacks to see the woken member before its first generation, and a second place for the stop, time and evidence rules. Rejected for the idle step at the boundary.

**A public `AgentChannel.after_answer()`.** A public method beside `before_turn()` would let a custom loop persist too. No member loop needs it: the shapes in use are `react()` and `deepagent()`, and a custom loop is refused loudly. Rejected for the private hook, which adds no inspect_ai API and can be made public when a custom-loop member turns up (decision: Ransom, 2026-10-29).

**Keeping the caller's ids on wake input**, refusing a list whose ids repeat each other or the conversation's. The refusal would have to see the member's compaction state, which lives in its loop, and a controller reusing a message list would meet an error for an input that is harmless with fresh ids. Rejected for fresh ids.

**A controller-level switch** (`Controller(persistent=True)`, or a `hold()` a controller calls after `MemberEnded`). One switch for a whole roster, but a lead and its workers differ, and a hold after the end comes too late: the loop has returned. Rejected for the member record's field, which is logged with the arm.

**Only the latest answer is a member's candidate.** `first` and `last` would compare members by their latest answers. Simpler to state, but `first` would then depend on later work (a member's early answer stops being "first" when it answers again), and the earliest answer could never be the swarm's. Rejected for every answer as a candidate.

**Not recording an answer during a stop.** Simpler, but a member that answers in the grace period would end `stopped`, its answer lost, where a non-persistent member's submission in the grace counts. Rejected.

**A `Quiescent` event, or a `quiescent()` channel hook.** The iterator's end already says no member is active and nothing is queued. A channel hook is for channels whose state can keep a swarm going with no delivery (a task list with open tasks); none exists, so it is left out ([Not this design](#not-this-design)).

**Waking members on news in the runtime.** The bus would wake an idle member on any `wake=True` record. It takes the decision from the controller, and in a large swarm every write would wake every idle member. Rejected, as swarm-wait.md already decided ([Room for later work](swarm-wait.md#room-for-later-work)).

## Compatibility and migration

- **inspect_ai.** No public API change: the private gate module gains `after_answer()` and the private `TurnGate` an entry point, and the react loops' behaviour without a gate is unchanged. inspect_swarm needs an inspect_ai release with it.
- **Member records.** `persistent` defaults to `False` and is serialized only when `True`, so existing members log, rebuild and replay as before.
- **Controllers.** `ControlEvent` becomes a union, and `MemberState` gains `idle`, but only swarms with a persistent member produce either: a controller with no persistent member in its roster sees only `MemberEnded` and the three old states. The docs already say to test an event's kind before reading its fields. `leaderless` changes only for persistent rosters.
- **Logs.** The record, final event, metrics and `boundary` event gain fields with defaults, and there is one new `SwarmEvent` kind; logs written before load unchanged. The viewer shows the new kind as it shows any `SwarmEvent` kind, with no change. The record lives in the sample store and has no generated TypeScript type.
- **Behaviour without persistent members.** None, beyond the stop procedure's "started and not ended" reading, which is the same set when no member can be idle, and the shared memory's `notify`, which is the same set when none is idle.

## Security

- **Wake input is eval-author text** in the user role, as `start()`'s input is. No member text reaches a controller: `MemberIdle` and `MemberNews` carry roster names, indices, bus record ids, channel names and a flag. `MemberNews.sender` is the writer's roster name, which a controller can already see on `MemberEnded`; in a swarm where members choose names there are no record channels, so no news.
- **Model output in the evidence.** The `answer` event carries indices, a time and a stop reason; the wake's `changed` event carries ids and hashes. A stop reason comes from the provider, not the model's text.
- **A member cannot wake itself or flood the controller unchecked.** `wakes()` excludes the waiting loop's own writes; every write that can raise `MemberNews` passes the channel's storm controls and limits first; each event is a few small fields. A member can try to induce its peers' wakes by writing, which is the controller's policy to filter, by `sender` or otherwise.
- **The answers are model output**, kept as `ModelOutput` copies in the runtime and scored as submissions are today; nothing parses them.

## Testing

All with mockllm, no network or Docker, on asyncio and trio through `tests/conftest.py`.

- **inspect_ai** (`tests/agent/test_agent_react.py`, `tests/agent/test_channel/test_channel.py`): the gate module's `after_answer()` returns `False` for a channel without a gate and for the inert channel, and the loop returns as today; with a test gate that returns `True`, a submitting `react()` continues after a submission with attempts used up and after one that passed its check, without calling `on_continue`, with `attempt_count` reset (a second activation gets its full `attempts`, an incorrect answer in it getting the incorrect message), and with the submit tool's call and result kept in the conversation; `react(submit=False)` continues after a tool-free reply with no `on_continue` and after one whose `on_continue` returns `False`; refusal and overflow ends never call it; a gate that returns `False` ends each loop exactly as today.
- **inspect_swarm** (`tests/test_persistent.py`, new):
  - a persistent `react()` member answers, is `idle` and `held`, `MemberIdle` reaches the controller, `wake()` appends the input, and the next generation's input has the first activation's messages, the submit tool's call and result, and the wake input after them, one system message; the same for `react(submit=False)` and `deepagent()` without background;
  - a callback registered with `on_boundary()` sees `after == "wake"` with the input as the last messages and the new `activation`, and its own change applies after the input;
  - `wake()` refusals: pending, running and ended members, the stopping swarm (the controller ends with the stop), an empty string, an empty list, a list holding a `ChatMessageSystem` beside a `ChatMessageUser` (`ValueError`, the member still `idle` and its conversation unchanged); a wake called before the member reaches its idle boundary takes effect there;
  - wake ids: a member with `CompactionTrim` woken twice with the same `[ChatMessageUser(...)]` list gets both inputs in its next generations, each with an id new to the conversation and to the caller's list, which is unchanged; a fresh string input reaches the model the same way; the `changed` events carry the new ids; a wake with a `ChatMessageUser(source="input")` on a member with `CompactionTrim(preserve=1.0)` and a forced compaction keeps the wake task as the last user message, with only the starting input in `partition_messages()`'s input group, and the caller's message still marked;
  - endings, one test each, for every row of [Endings](#endings): a stop and `end()` while idle give `submitted` with the last answer as output; `end()` during the final submit gives `stopped` with that submission not recorded; an answer in the grace period is recorded and the member ends `submitted`; a stop while woken gives `stopped` and keeps earlier answers eligible; a custom loop that calls `before_turn()` and returns, and a `react(submit=False)` member whose `on_continue` returns `False` after a tool call, end `errored`; a `generator()` member ends `errored` by the never-reached rule;
  - refusals at `swarm()`: a persistent member declared bridged, one in the `inspect_swe/` namespace, and `deepagent(background=True)` and `background=4`;
  - the reader rule: in a `shared_memory()` swarm, a persistent `deepagent()` whose own tools omit `swarm_tools()` and whose `research(extra_tools=[swarm_tools()])` child views the board ends `errored` with the gate's message at the parent's next boundary, with no answer recorded and no `MemberIdle`, both when the child runs in the turn that submits and in an earlier turn; the same member that is not persistent runs as today; a persistent `deepagent()` with `swarm_tools()` in its own tools and in `research(extra_tools=...)` binds its top-level loop and is not refused; the child-only member persists in a swarm with `channels=[]` and in one with only `filesystem()`, answering, idling and being woken twice, with `MemberIdle.news` false and no `MemberNews`;
  - time: a persistent member with `time_limit(0.5)` idle for 1 s between two short activations is not stopped; working time stops while every member is idle (through `eval_async()`, as `tests/test_binding.py` runs a sample);
  - `swarm_wait`: a member waiting is released with `all_waiting` when every other member is idle;
  - news: a lead's shared-memory write while a worker is idle gives one `MemberNews` with the lead as sender; the worker's own writes, `wake=False` records and records that are not news to its loop give none; a worker that answers with an unviewed write from its activation gives `MemberIdle.news` true, and one that viewed it gives false; a worker that never resolved `swarm_tools()` gets no news; a worker whose top-level loop views the write after a wake and answers gives `MemberIdle.news` false and no further `MemberNews` for that write, across three activations, while a research child's view of the same write clears only the child's own view, as for any loop; `notify` in the `delivered` evidence includes the idle worker;
  - quiescence: with every member idle and nothing queued, `events()` ends; a controller then wakes one and a new `events()` call reports its next event; `leaderless` returns `quiescence` with a persistent roster and `stop_on_submission=False`, and stops at the first `MemberIdle` with it set;
  - answers: `first` and `last` across two persistent members' several answers, ties by roster and index; `choose_answer()` takes the latest answer and keeps it after a later one, and raises before any answer; the record's `answer_times`, `FinalRecord.answer`, the `final` event's `answer`, and the sample's final message being the chosen answer's;
  - evidence and metrics: one `answer` event per answer; a `boundary` event at each idle boundary with no callback, with `activation` and `idle_for`; the wake's `changed` event with origin `swarm` and the input's ids and hashes matching the next `ModelEvent`'s input; `activations`, `answers` and `awaiting_work_time`; `steps_to_answer` and `critical_steps_to_answer` ending at the chosen answer's `answer` event;
  - the two examples above as end-to-end tests: `dispatch` with three tasks gives three answers and the third as the swarm's; `lead_and_workers` with a lead that writes two tasks gives each worker its wakes and ends `lead_done`.

## Implementation plan

1. **inspect_ai: `after_answer()`.** `src/inspect_ai/agent/_channel/gate.py` (the `after_answer()` function and the `TurnGate` entry point), `src/inspect_ai/agent/_react.py` (the four answer ends, the attempt reset); the inspect_ai tests above. It needs the seam's step 1 (the gate) and can be part of it.
2. **inspect_swarm: persistent members.** Needs step 1 in an inspect_ai inspect_swarm can depend on, and the seam's step 2.
   - `src/inspect_swarm/_member.py`: `Member.persistent` and `member(persistent=...)`, serialized when true; the endings (the idle mark, the normal-return rule, the output from the last answer).
   - `src/inspect_swarm/_swarm.py`: the refusals in `_check_persistent()`.
   - `src/inspect_swarm/_seam.py`: `after_answer()`, the reader rule at boundaries and answers, the idle step, the wake input as the runtime's change, `Boundary.activation` and `after="wake"`.
   - `src/inspect_swarm/_controller/_control.py`: `MemberRun`'s answers and activations, the `idle` state, `wake()` with fresh message ids, `MemberIdle`, the union alias, `choose_answer()` over answers, the outputs of answers for `settle()`, the stop procedure over started members.
   - `src/inspect_swarm/_controller/leaderless.py`; `src/inspect_swarm/_final.py` (candidates over answers); `src/inspect_swarm/_record.py`.
   - `src/inspect_swarm/_evidence.py` (`answer`, the `boundary` additions, `final`'s `answer`), `src/inspect_swarm/_metrics.py`, `src/inspect_swarm/_critical_path.py` and `src/inspect_swarm/_ledger.py` (answer positions), `src/inspect_swarm/__init__.py` (`MemberIdle`).
   - Docs: `docs/members.qmd` (persistent members), `docs/controllers.qmd` (idle, `wake()`, quiescence, the `dispatch` example), `docs/analysis.qmd` (the event, record and metrics fields), `docs/reference/inspect_swarm.qmd`.
   - Tests: `tests/test_persistent.py` without the news cases.
3. **inspect_swarm: news for idle members.** `src/inspect_swarm/_budget.py` (`MemberContext.loop_span`), `src/inspect_swarm/_bus/_tools.py` (record it at binding), `src/inspect_swarm/_bus/_bus.py` (the idle check at step 4 and the runtime's callback), `src/inspect_swarm/_channel/shared_memory.py` (`notify` with idle members), `src/inspect_swarm/_controller/_control.py` (`MemberNews`, `MemberIdle.news`), `src/inspect_swarm/__init__.py`; `docs/controllers.qmd` (the lead-and-workers example); the news and example tests. Until it lands, `MemberIdle.news` is `False`.

## Open questions

None.

## Not this design

- **A built-in `coordinator` controller** and the `lead_done` topology reason.
- **Idle members in `swarm_wait` results**: telling a waiting lead that a worker went idle, as its result names ends. Today the lead hears of it only through the worker's writes or the all-waiting release.
- **A `quiescent()` channel hook**, for a channel whose state can keep the swarm going without a delivery, such as a task list with open tasks.
- **Per-activation limits**, and per-activation answers' scores (swarm-scoring.md Part 2).
- **A public `after_answer()` hook** for custom member loops, if one turns up.
- **Persistent members whose board is read only by subagents**, in a swarm with a waiting channel, which needs news defined for the member across its loops (a member-level seen state in the shared memory) rather than for one loop.
- **Persistent bridged members**, through inspect_swe's `--resume`; **persistent members with background work**, which needs holding background work ([swarm-turn-seam.md Not this design](swarm-turn-seam.md#not-this-design)).
- **Delivery to pending members**, and starting a member on its first message.
