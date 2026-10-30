# Inspect Swarm: operator intervention for every member

## Overview

An operator watching a running swarm can reach any member by name from `inspect ctl`, the control channel every eval already runs. `inspect ctl sample agent list` shows the swarm's members with what each is doing; `message` gives one member a message from the operator, either at its next turn or now, cancelling what its turn is doing, a long tool call included; `pause` and `resume` hold a member, or the whole swarm, between turns; `stop` ends one member, or stops the whole swarm the way its own stop does, and the sample goes on to its final answer and its scorers.

inspect_ai gains a small, generic registry: code running in a sample can make named agents, and named groups of them, addressable by the control channel, and the control channel gains routes and `inspect ctl` verbs that list them and send them `message`, `pause`, `resume` and `stop`. The swarm registers every member and itself as a group. In the swarm, a message is an ordinary operator `UserMessage` posted to the member's own loop's agent channel; to deliver it now, the swarm posts it and then interrupts the turn, so the loop's recovery finds the operator's redirect already queued and never waits. A pause is a turn-seam boundary callback with origin `operator` that waits until resumed, so a paused member is held: its time limit stops, its idle time is waiting time, and a stop still ends it at once. Stopping a member is the seam's `end(origin="operator")`, and stopping the swarm is `stop("operator")`. Members the swarm cannot reach that way are refused with the reason: bridged members take only `stop`, and members that run background work cannot be paused. Two inspect_ai fixes make an interrupted conversation valid on every provider and in the log.

## Why

An operator watching a swarm today ([#119](https://github.com/meridianlabs-ai/inspect_swarm/issues/119)) has three tools, and none reaches a member chosen by name:

- **ACP** (an editor or the Inspect TUI attached to the sample) can send messages and interrupt, but it binds whichever member opens its agent channel first, by chance, and no other member ([swarm-communication.md The binder and ACP](swarm-communication.md#the-binder-and-acp)).
- **`inspect ctl`** can cancel the whole sample, cancel one tool call, and read the sample's events and messages, but it knows nothing of members.
- **The swarm** has no pause, and nothing stops one member. A controller can start members and choose the answer; an operator's interrupt of the sample cancels everything and is recorded as `ended_by: operator` ([swarm-limits.md Exhaustion and errors](swarm-limits.md#exhaustion-and-errors)).

So an operator who sees one member going the wrong way, or burning budget on a dead end, can only let it run or end the whole sample. Steering, pausing to look, and stopping one member are the interventions an operator of a multi-agent system needs, and the swarm's own designs already have the pieces: the turn seam holds a member between turns and ends one on request ([swarm-turn-seam.md](swarm-turn-seam.md)), and the agent channel already carries operator messages and interrupts.

## Goals and non-goals

Goals:

- For every member, addressed by its roster name: a message from the operator, at the next turn or now (a long tool call included); pause and resume; stop.
- For the whole swarm: pause and resume, and a stop that does not end the sample.
- Say how paused time counts towards member limits, the swarm's time cap, the sample's time limit and its working time.
- An operator surface that every eval already has, scriptable for agents that watch runs, with no client to attach.
- A clear refusal, with the reason, for every member and state an action cannot reach.
- Evidence in the log of what the operator did.

Non-goals:

- **Peer messages and urgent peer steering.** Members steering each other through `Steer` items stays [Urgent messages and steering](swarm-communication.md#urgent-messages-and-steering)'s. This design shares two of its inspect_ai fixes (below) and nothing else.
- **Preemptive pause.** A pause takes effect at the member's next turn boundary, as every turn-seam hold does. Cutting a long tool call short is `message --interrupt` or the existing `inspect ctl sample cancel-tool-call`.
- **Bridged members' messages and pauses**, and holding a member's background work. Both are refused here.
- **ACP per member.** ACP keeps binding one member, as today ([Not this design](#not-this-design)).
- **A controller API.** Controllers already have the seam's `on_boundary()` and `end()`; nothing here adds to `SwarmControl`.
- **Broadcast.** A message to every member is a shell loop over `message`, as the control channel's no-fan-out rule asks.

## Current behaviour

Code references are to inspect_swarm `main` at `fb7e2b1` and inspect_ai `main` at `0e49f5f65`. Paths are relative to each repository's root. Claims marked *(spike)* were checked by running a swarm on both anyio backends.

### The agent channel

- **Producers** hold an `AgentRef` with `post(item)` and `interrupt(Cancel)` (`src/inspect_ai/agent/_channel/ref.py:13-41`). `interrupt` posts the item and cancels the loop's bound `turn_scope()`, if any; with none bound it is a plain post (`channel.py:164-179`). `turn_active` says whether a turn scope is bound (`channel.py:229-237`).
- **`react()`** extends its messages with `before_turn()` at the top of every turn, runs generation and tools inside `turn_scope()`, and on `AgentInterrupted` extends them with `after_cancel()` and continues (`src/inspect_ai/agent/_react.py:263-269`, `:369-373`; `react(submit=False)` at `:504-553`).
- **`before_turn()`** drains the queue and returns the drained `UserMessage`s, consecutive operator messages coalesced into one (`channel.py:432-453`, `items.py:74-112`).
- **`after_cancel()`** returns repairs for the interrupted tool calls, then the drained `UserMessage`s, and waits for one only if none was drained (`channel.py:455-479`). So a `UserMessage` posted before the interrupt is found at that drain and the loop resumes at once.
- *(spike)* A controller posted `UserMessage(ChatMessageUser(content=..., source="operator"))` to one `react()` member's channel 1 s into a 30 s tool call, then called `interrupt(Cancel(reason="user_cancel"))`. The member's next model input ended with the repair `Tool call cancelled by user.` and the operator's message, its sibling's tool ran to completion, and the sample ended `success`.
- **Repairs carry no function name.** `_repair()` builds each `ChatMessageTool` from the call id, text and error only (`channel.py:392-426`) *(spike: the repair's `function` was `None`)*. Bedrock's converter raises for such a message and Google's sends an empty function-response name ([swarm-communication.md Interrupting a member](swarm-communication.md#interrupting-a-member-through-its-agent-channel)), so an interrupted conversation cannot continue on Bedrock.
- **A tool call cancelled by an enclosing scope stays pending.** The per-call path finalises a `ToolEvent` cancelled through its own cancel function (`src/inspect_ai/model/_call_tools.py:568-620`), but one cancelled by an enclosing scope, as an interrupt's turn scope is, is left as it was *(spike: the interrupted `slow` call's `ToolEvent` had `pending=True` in the finished log)*. ACP finalises such events itself before it interrupts (`cancel_current_turn()`, `src/inspect_ai/agent/_acp/transport_live.py:1406`); nothing else does.
- **ACP** binds the first channel offered in a sample and rejects the rest until it is unbound (`transport_live.py:661-718`), and its drain observer treats any drained `UserMessage` as the operator's redirect (`:720-732`). The swarm records whether ACP drives a member's loop as `acp_bound` (`src/inspect_swarm/_bus/_tools.py:17-23`, `:55-61`). ACP's server refuses to start on trio (`src/inspect_ai/agent/_acp/server.py:133-150`).

### The control channel

- `inspect ctl` talks HTTP over an AF_UNIX socket to a server that runs as a task on the eval's own event loop, on by default ([control-channel.md](https://github.com/meridianlabs-ai/inspect_ai/blob/main/design/ctl/control-channel.md)). A handler therefore runs between the eval's own steps, and a handler that never awaits is atomic with respect to the sample.
- Sample mutations are `POST /evals/{eval_id}/sample/<verb>` with `sample_id` and a required `epoch` as query parameters, unknown parameters refused before the handler runs (`src/inspect_ai/_control/strict.py`), and a result with `ok`, `changed` and `dry_run` (`sample/cancel-tool-call` at `src/inspect_ai/_control/server.py:1274-1323`, its handler at `cancel.py:944-1010`). A handler finds its sample with `find_active_sample()` (`src/inspect_ai/_control/state.py:977-995`). The CLI wraps them in a shared scaffold, with the epoch required whenever the task runs more than one, and a target that is not the sample in an option, never after the optional `EPOCH` positional ([tool-call-cancel.md](https://github.com/meridianlabs-ai/inspect_ai/blob/main/design/ctl/tool-call-cancel.md#surface)).
- `ActiveSample` keeps per-sample state that the control channel and ACP read, such as `acp_transport` and `transcript` (`src/inspect_ai/log/_samples.py:212`, `:253`).
- Process and task pause (`inspect ctl process pause`, `task pause`, and `--now`, which holds in-flight samples at their next model call) keep time limits running while held: "wall clock is explicitly the operator's risk", and held time is waiting time for the working limit ([pause-resume.md](https://github.com/meridianlabs-ai/inspect_ai/blob/main/design/ctl/pause-resume.md#future-work)).

### The swarm

- **Stopping.** `stop(reason)` closes the bus, records the reason, cancels the controller's run and closes every running member's lever, synchronously; grace and the hard stop follow, then the final step (`src/inspect_swarm/_controller/_control.py:534-548`, `:653-664`). The first call wins. Stop reasons are a closed set ([swarm-limits.md Stopping](swarm-limits.md#stopping)); `ended_by` separately records what ended the swarm from outside (`_control.py:745-757`).
- **Members** are `MemberRun`s, which controllers see through `MemberHandle` (`_control.py:56-100`, `:192-243`). A member's context knows whether it is bridged and whether it has started background work (`src/inspect_swarm/_budget.py:303-322`).
- **Names.** Member names are unique within a sample, across every swarm that runs in it: each names a timeline, and a swarm whose member name is already a timeline in the sample is refused before any member runs (`src/inspect_swarm/_evidence.py:207-224`). A nested swarm runs inside an outer member's work (`tests/test_background_members.py:1088-1100`).
- **The turn seam** ([swarm-turn-seam.md](swarm-turn-seam.md), designed and not yet built) gives each member a gate that binds the member's own loop (`turn_loop`) at its first boundary, registers boundary callbacks with `on_boundary(callback, origin=...)`, holds the member while they run, and ends it on `end(origin=...)`. It refuses bridged members and `deepagent()` with background dispatch at registration, ends `errored` a member with callbacks whose background work is running at a boundary, and ends `errored` a member with callbacks that never reaches a boundary ([Supported members](swarm-turn-seam.md#supported-members)). It leaves interrupts to this design ([Interrupts](swarm-turn-seam.md#interrupts)).

### Who uses which member shapes

From `examples/*.py`, `docs/members.qmd` and the tests: `react()` and `deepagent()` members, wired with `swarm_tools()` or not; `deepagent(background=...)` members (`docs/members.qmd:138`); inspect_swe's bridged agents (`docs/members.qmd:169`); nested swarms run by a member (tests only); and test-only custom agents that generate without an agent channel or only sleep (`tests/swarm_runtime.py:106-135`). ACP agents are refused as members (`docs/restrictions.qmd:11`).

## Design

### At a glance

```python
# inspect_ai: src/inspect_ai/_control/operator.py (new, private)
OperatorAction = Literal["message", "pause", "resume", "stop"]


@dataclass(frozen=True)
class OperatorOutcome:
    changed: bool
    detail: str                  # one line for the operator, no agent text
    refused: str | None = None   # set when the action cannot apply; the route answers 409


@dataclass(frozen=True)
class OperatorAgentInfo:
    name: str
    state: str                   # the registrant's lifecycle: pending, running, ended
    status: str | None           # how it ended
    activity: str                # in_turn, between_turns, held, not_started, ended, unknown
    paused: bool                 # a pause is in effect for it, held yet or not
    actions: dict[OperatorAction, str | None]   # None: available now; else why not


class OperatorAgent(Protocol):
    def info(self) -> OperatorAgentInfo: ...
    def message(self, text: str, *, interrupt: bool, dry_run: bool) -> OperatorOutcome: ...
    def pause(self, *, dry_run: bool) -> OperatorOutcome: ...
    def resume(self, *, dry_run: bool) -> OperatorOutcome: ...
    def stop(self, *, dry_run: bool) -> OperatorOutcome: ...


@dataclass(frozen=True)
class OperatorGroupInfo:
    name: str
    state: str                   # running, stopping
    paused: bool
    agents: list[OperatorAgentInfo]


class OperatorGroup(Protocol):
    @property
    def agents(self) -> Mapping[str, OperatorAgent]: ...
    def info(self) -> OperatorGroupInfo: ...
    def pause(self, *, dry_run: bool) -> OperatorOutcome: ...
    def resume(self, *, dry_run: bool) -> OperatorOutcome: ...
    def stop(self, *, dry_run: bool) -> OperatorOutcome: ...


@contextmanager
def operator_group(name: str, group: OperatorGroup) -> Iterator[None]: ...
```

```
inspect ctl sample agent list    TASK SID [EPOCH]
inspect ctl sample agent message TASK SID [EPOCH] --agent NAME (--text TEXT | --file PATH) [--interrupt] [--dry-run]
inspect ctl sample agent pause   TASK SID [EPOCH] (--agent NAME | --group NAME) [--dry-run]
inspect ctl sample agent resume  TASK SID [EPOCH] (--agent NAME | --group NAME) [--dry-run]
inspect ctl sample agent stop    TASK SID [EPOCH] (--agent NAME | --group NAME) [--dry-run]
```

| Action | A member | The swarm (group) |
|---|---|---|
| `message` | an operator `UserMessage` at its next turn; with `--interrupt`, its turn is cancelled and it continues with the message now | none (no broadcast) |
| `pause` / `resume` | a turn-seam callback, origin `operator`, holds it at its next boundary until resumed | a latch: every member is held at its next boundary while it is set, members started meanwhile included |
| `stop` | `end(origin="operator")`: `stopped`, the rest of the swarm goes on | `stop("operator")`: the swarm's orderly stop, final answer, then the sample's scorers |

### The inspect_ai registry

A new private module, `src/inspect_ai/_control/operator.py`, holds the protocols above and `operator_group(name, group)`, a context manager that adds the group to the active sample's `ActiveSample.operator_groups` (a new `dict[str, OperatorGroup]`, empty by default) for its block and removes it on exit, synchronously in a `finally`. Outside a sample it does nothing. It raises `ValueError` when the sample already has a group of that name, or an agent of one of the group's names in another group, naming the clash. inspect_ai knows nothing of swarms: a group is any set of named agents that some code in the sample can act on, and the swarm is its only registrant.

The registry calls the registrant's methods synchronously from the control server's handlers, which run on the eval's loop, so an action is atomic with respect to the agents it acts on, and each method must not await or do work proportional to a conversation's size (the control channel's "cheap shoveling" rule). Outcomes and infos carry no agent-written text: names, lifecycle words, statuses and fixed sentences only.

### The control channel routes and verbs

- **`GET /evals/{eval_id}/sample/agents?sample_id=&epoch=`** returns `{sample_id, epoch, groups: [OperatorGroupInfo...]}`; a sample with no groups returns `groups: []`, and the CLI says that the sample has no addressable agents. `epoch` defaults to 1, as for the other reads.
- **`POST /evals/{eval_id}/sample/agent/{action}?sample_id=&epoch=&agent=|group=[&interrupt=][&dry_run=]`**, `action` one of `message`, `pause`, `resume`, `stop`. `epoch` is required, and exactly one of `agent` and `group` (a 400 otherwise); `message` takes only `agent`, `interrupt` only with `message`, and the message text as a JSON body, `{"text": "..."}`, not empty. An unknown sample, agent or group is a 404 that lists the names there are; a refused action (`OperatorOutcome.refused`) is a 409 with the reason; otherwise `{ok: true, changed, dry_run, target, detail}`. A repeat that changes nothing (a second pause, a resume of a running member, a stop of a stopping swarm) is `changed: false`, never an error. The text is a body because it is free, possibly long and multi-line, where the other mutations' parameters are scalars; the strict-parameter check is unchanged, since it checks query parameters only.
- **The CLI** adds `agent` under `inspect ctl sample`, through the shared sample-mutation scaffold, so target resolution, the epoch rule, `--json`, `--terse` and `--dry-run` work as for `cancel-tool-call`. The agent or group is an option, as `--tool-call-id` is, because `EPOCH` is an optional positional. `--file` reads the text from a file or `-` for stdin. `list` prints one row per agent: group, name, state, status, activity, paused, and the actions available.
- **Version skew.** New routes, so an older server answers with the router's stock 404, which the CLI reports as "this eval's inspect is too old for `sample agent`", as `cancel-tool-call` does. No control-API version change.

### The inspect_ai fixes for interrupted conversations

The same two fixes [Urgent messages and steering](swarm-communication.md#urgent-messages-and-steering) lists (its inspect_ai change, items 2 and 3), made here because an operator's interrupt needs them first:

- **`_repair()` names its repairs.** Each repair's `function` is the name of the unanswered call it answers, taken from the last assistant message's `tool_calls`, so the repaired conversation converts on Bedrock and gives Google a matching function-response name. ACP's interrupts get the same fix.
- **A `ToolEvent` cancelled by an enclosing scope is finalised** in `_call_tools.py`, with `pending` cleared and a `cancelled` error, as generate already finalises a cancelled `ModelEvent` (`src/inspect_ai/model/_model.py:1686-1700`). Events ACP has already finalised are left as they are.

Whichever of this design and urgent messages is built first carries them; the other then needs only its own changes.

### The swarm's registration

`SwarmRun.run()` registers one group around its member task group, after the members' timelines are claimed and before any member starts, and the registration ends in `run()`'s `finally`. The group (`SwarmOperator`, new module `src/inspect_swarm/_operator.py`) has one `MemberOperator` per roster member, under the member's roster name: never a name a member chose for itself (`choose_name=True`), which is model output.

The group's name is `swarm` for a swarm run directly in the sample, and `swarm:<member>` for a swarm run inside a member's work (read from `current_member` when the run starts), so a nested swarm's group is distinct from its outer swarm's and names the member that runs it. Member names cannot clash, since they are unique in a sample already. Two swarms that run concurrently in one sample outside any member, or inside the same member, would clash on the group name; no one runs that shape, and the registry's `ValueError` refuses it when the second swarm starts, naming the group.

### Messages

`MemberOperator.message(text, interrupt=...)` builds `ChatMessageUser(content=text, source="operator", metadata={"inspect_swarm": {"version": 1, "type": "operator", "id": "op-<n>"}})`, with the id counted per swarm run, and then, by the member's state:

| Member | `interrupt=False` | `interrupt=True` |
|---|---|---|
| pending or ended | refused: "has not started" / "has ended" | the same |
| bridged | refused: a bridged member has no agent channel; it takes `stop` only | the same |
| running, its turn loop not yet bound (setup before its first boundary) | kept by the swarm, posted when the loop binds; it reaches the first generation | the same: there is no turn to cancel yet |
| running, its turn loop bound and closed (its loop has returned) | refused: "its agent loop has ended" | the same |
| between turns, held, or paused | posted; the loop's next `before_turn()` returns it, after any hold | posted; nothing to interrupt, and the detail says so |
| in a turn (`turn_active`) | posted; it reaches the next turn, after the turn's tool calls and subagents finish | posted, then `interrupt(Cancel(reason="user_cancel"))`: the turn's model call, tool calls and subagents are cancelled, and `after_cancel()` returns the repairs and the message at once |

The posting is to the turn seam's `turn_loop`, the member's own top-level loop, through `channel._ref()`: the loop whose boundaries the seam holds, whose turn scope encloses its subagents' loops, and which no background task binds. The bus's `MemberContext.loop` is not used, because it may be a child loop ([The member's gate](swarm-turn-seam.md#the-members-gate)).

**Never an interrupt without a redirect.** The message is posted before the interrupt, in the same synchronous step, so the drain in `after_cancel()` always finds it and the loop never waits for an operator ([The binder and ACP](swarm-communication.md#the-binder-and-acp)). If the turn ends on its own between the post and the cancellation's delivery, the scope's exit raises nothing, the turn ends as usual, and the message is drained at the next `before_turn()`.

**Kept until the loop binds.** The seam gains one hook: when its gate binds a member's `turn_loop`, before it checks for a stop or runs callbacks, it calls the swarm's `loop_bound(member, channel)` synchronously. The swarm then posts any kept messages to the channel, subscribes its drain observer (below) and registers a pending pause ([Pause](#pause)). `before_turn()` drains after the gate returns, so kept messages reach the first generation. A member that never binds a loop (a custom agent with no agent channel) never receives them; they are recorded as undelivered when it ends.

**What an interrupted member loses** is what any interrupt loses: the cancelled tool calls' results (a sandbox command may keep running in the sandbox), a cancelled synchronous subagent's result, and the usage of a cancelled model call, which the ledger reports as unknown. That is the operator's choice; without `--interrupt` nothing is cancelled. A `deepagent()` member's background subagents are not in its turn, so an interrupt does not cancel them.

**The member is not told by whom.** The message is an operator turn like any other, in the user role with `source="operator"`. Operator messages queued together, from `inspect ctl` and from ACP, are coalesced into one by `before_turn()` as today, which builds a new message and so drops the metadata; the evidence (below) still records each.

**With ACP.** If ACP drives the member, a `message` releases an ACP operator's pending interrupt as that operator's own redirect would, and ACP's drain observer clears its pending flag: both are operators. Nothing else in ACP changes, and the swarm never calls ACP.

**A member in `swarm_wait`** is in a turn: a plain message waits until the wait returns (at most the wait's `max_wait`), and `--interrupt` cancels the wait, which loses nothing. The detail line says which the member is doing.

### Pause

A pause holds a member at its next turn boundary until it is resumed, through the turn seam. Each member has an operator flag, and the swarm has a latch; a member is paused while either is set.

- **The callback.** While a member is paused and its turn loop has bound, the swarm keeps one boundary callback registered on it with `origin="operator"`. At a boundary it returns `None` at once if the member is no longer paused; otherwise it waits, inside `suspend_working_limit()`, until a resume or a new pause changes the member's state, and checks again. On resume, the swarm wakes it and unregisters it, so a member that is not paused has no operator callback and the seam's checks for members with callbacks do not apply to it.
- **When the loop has not bound yet** (a pending member, or one in setup), the pause is recorded and the callback is registered from `loop_bound()`, before that boundary's callbacks run, so the member is held at boundary 0, before its first generation. Registering only once the loop has reached the gate keeps a pause from ever making a member `errored` under the seam's rule for members with callbacks that never reach a boundary: a custom member that never binds a loop is simply never held.
- **Refused**, with the reason: bridged members; `deepagent(background=True)` or an integer `background` (the seam's `on_boundary()` refuses them); members that have started background work (`MemberContext.background`), since the seam would end such a member `errored` at a boundary where that work runs; ended members; and any pause once the swarm is stopping. A member that starts background work after its pause was accepted is the seam's accepted case: it ends `errored` at its next boundary if that work is still running ([Supported members](swarm-turn-seam.md#supported-members)).
- **The whole swarm.** `SwarmOperator.pause()` sets the latch and registers the callback on every running member whose loop has bound and that a pause can reach; a member started while the latch is set gets it from `loop_bound()`. Members a pause cannot reach are listed in the outcome's detail and keep running. The latch is not a fan-out of member pauses: it also holds members the controller starts later, and resuming it leaves members the operator paused one by one paused, with the detail naming them, as inspect's `task resume` names a process pause that still holds a task.
- **Composition.** The operator's callback runs after callbacks registered before it ([Composition](swarm-turn-seam.md#the-callback-contract)): a pause registered after a scheduler's callback holds the member after the scheduler lets it go. Messages posted to a paused member are drained when the hold ends.
- **The controller** keeps running during a pause of the swarm. It can start members, which are held at their first boundary, and it sees no new events: a held member is still running.

**Time while paused**, as the seam defines it for every hold ([Time](swarm-turn-seam.md#time)), with the operator's callback marking its wait:

| | Paused time |
|---|---|
| The member's time limit | does not count |
| The member's token, cost, turn and message limits | unaffected: a paused member makes no model call |
| The swarm's time cap | counts |
| The sample's time limit | counts |
| The sample's working time | waiting time, unless other work in the sample runs meanwhile |
| `swarm_wait` | a paused member is not active, so when every other member is waiting or held, their waits return `all_waiting` |

The swarm's time cap and the sample's time limit are deadlines for the whole run, so they keep counting, as inspect's own hard pause keeps a sample's time limit running ("wall clock is the operator's risk"). An operator who pauses the whole swarm for long can stop it, or retune the sample's time limit with `inspect ctl config`; the swarm's cap, derived when it starts, does not move.

### Stop

- **A member.** `MemberOperator.stop()` calls the seam's `end(origin="operator")`: a held or paused member ends at once, a member in a turn at its next boundary or, past the grace period, by cancellation, and a pending one at boundary 0 once started ([Ending a member from outside the chain](swarm-turn-seam.md#the-callback-contract)). It ends `stopped`, with `ended by operator` in its lever's message, never as a submission, and the rest of the swarm goes on; the controller sees its `MemberEnded` as for any member. It works for every member shape, bridged ones included, since it closes the lever and cancels as a stop does. A second stop, or a stop of an ended member, changes nothing.
- **The swarm.** `SwarmOperator.stop()` calls `stop("operator")`. `operator` joins the closed set of stop reasons. It is the swarm's own orderly stop: the controller's run is cancelled, members are soft-stopped, held and paused members end at once, grace and the hard stop follow, and the final step chooses the answer. The swarm then returns, so the sample continues to its scorers, and the ledger's `stop_reason` is `operator` with `ended_by` unset. An operator who wants the sample itself ended still uses `inspect ctl sample cancel`, recorded as `ended_by: operator`.

### The list

`MemberOperator.info()` gives the member's roster name, its handle's state and status, whether it is paused, and its activity: `not_started`; `held` while any boundary callback holds it; `in_turn` or `between_turns` from its turn loop's `turn_active`; `unknown` for a running member whose loop has not bound (setup, a bridged member, a custom agent); `ended`. `actions` gives, for each action, `None` or the reason it would be refused now, so an operator or a watching agent can see what is possible before it asks. `SwarmOperator.info()` adds the swarm's state (`running` or `stopping`, with the stop reason) and whether the latch is set.

### Evidence

Two `SwarmEvent` kinds through `swarm_event()` ([swarm-event-types.md](swarm-event-types.md#swarmevent)), written in the member's own task and span, never from the control server's handler, which runs outside the sample's context:

- **`operator_message`**, once per message: `id`, `interrupt`, `in_turn` (whether a turn was active when it was posted), `outcome` and, when delivered, `message_id`, the id of the `ChatMessageUser` the loop received (the coalesced one when operator messages were coalesced). Delivery is seen by a drain observer the swarm subscribes on the turn loop (`subscribe_drained()`, which runs synchronously in the loop's task), matching its own items by identity: the outcome `delivered` is written then. A message still kept or queued when the member ends is written by the member's ending as `undelivered`. The text is not in the event: it is in the conversation and the next `ModelEvent`'s input.
- **Pauses** need no kind of their own: every boundary at which the operator's callback ran is the seam's `boundary` event, with `operator` in its `origins` and the time held in `held_for`, and the seam's `holds` and `held_time` metrics include it.
- **Stops** are the seam's `boundary` outcome `end` (or the member's status `stopped` with `ended by operator`), and the swarm's `stop_reason` `operator` in the ledger and the result record.

A message's interrupt is also visible in the conversation (the repair `Tool call cancelled by user.`) and, with the inspect_ai fix, as the finalised cancelled `ToolEvent`.

### Supported members

| Member | `message` | `--interrupt` | `pause` | `stop` |
|---|---|---|---|---|
| `react()`, `react(submit=False)`, `deepagent()` without background dispatch | yes | yes | yes | yes |
| `deepagent(background=...)` | yes, to the top-level loop | yes; background subagents go on | refused | yes |
| custom agent with an agent loop that passes its state | yes | yes | yes, while it has no background work | yes |
| custom agent with no agent channel (`generator()`, `sleeper()`) | kept, never delivered, recorded `undelivered` | the same | accepted, never held (activity `unknown`) | yes |
| bridged (declared or `inspect_swe/`) | refused | refused | refused | yes |
| nested loops (subagents) | not addressable: their member's loop is | | | |
| a nested swarm's members | addressable by their own names, in the group `swarm:<member>` | | | |

## Alternatives considered

**ACP sessions per member.** ACP's server would list each member as a target of its own, so an editor or the Inspect TUI attaches to `worker-2` and sends messages and interrupts as it does to a single-agent sample today. It is the most natural interactive experience, and it is what the binding problem in the issue points at. Against it: it reworks ACP's per-sample transport (one session, one binding, one transcript filter, one interrupt coordinator, `transport_live.py`) into per-agent sessions; ACP needs `--acp-server` and an attached client, and refuses trio; and ACP has no pause or stop, which would be `inspect/*` extensions only Inspect's clients send. The registry here is the seam such sessions would use, so they can be added on top later ([Not this design](#not-this-design)). Rejected as the first surface; see [Open questions](#open-questions).

**`Steer` items for operator messages.** `Steer` is designed for producer-composed content that must not count as the operator's redirect. An operator's message is the operator's redirect, and `UserMessage` is the item for it: it puts the text where an operator's text goes, and it satisfies `after_cancel()`'s wait, which is exactly what makes an interrupt safe. Rejected; `Steer` stays urgent peer messages'.

**A swarm-owned control socket and CLI.** The swarm would serve its own endpoint. It would duplicate the control channel's discovery, peer check, selectors and output contract. Rejected.

**Operator verbs on `SwarmControl`, for controllers to expose.** Each eval author would have to build a surface for the operator. Controllers already have `on_boundary()` and `end()`. Rejected.

**Registering the operator callback on every member at its start**, with the callback idle until a pause. Simpler state, but every member would then have a callback: the seam would end `errored` a `deepagent(background=...)` member at its first boundary with background work running, and any custom member with no agent loop at its end. Rejected for registering only while paused and only once the loop has bound.

**A preemptive pause** (interrupt, then hold). The interrupt's recovery waits in `after_cancel()`, which is not a boundary and which the seam does not hold; posting a message to release it would put text the operator did not write in the conversation. Rejected; an operator who needs the member to stop now sends `message --interrupt` or cancels the tool call, then pauses.

**Group actions as fan-out over members.** The CLI would loop over the members. A loop misses members the controller starts later, and stopping every member is not the swarm's stop: it skips the controller's cancellation and stop reason, and the controller would see every member end and return `all_done`. Rejected for a group target that the swarm implements.

**Reserving `swarm` as a member name** for a single namespace of agents and groups. `member(swarm(...))` defaults its name to `swarm` (its registry name is `inspect_swarm/swarm`), so reserving it would refuse an existing shape. Rejected for separate options, `--agent` and `--group`.

**Pausing the swarm's time cap while the whole swarm is paused.** It would protect the cap from an operator's long pause, but the sample's time limit keeps counting anyway, and inspect's own hard pause keeps time limits running. Rejected for consistency; see [Open questions](#open-questions).

## Compatibility and migration

- **inspect_ai.** New private module, a new `ActiveSample` field defaulting to empty, new control routes and CLI verbs: additions only. The repair and `ToolEvent` fixes change what interrupted conversations hold: repairs gain their function name, and a cancelled tool call's event is finalised rather than left pending, in ACP's interrupts too. No log schema change. inspect_swarm needs an inspect_ai release with both PRs.
- **inspect_swarm.** No public API change. One new stop reason, `operator`, in the ledger and the result record; analysis code that matches stop reasons exhaustively must add it, and `docs/analysis.qmd`'s list gains it. One new `SwarmEvent` kind, `operator_message`. A swarm in a sample now registers operator targets for its run; nothing changes for a sample nobody controls. Two swarms run concurrently in one sample outside any member, or inside the same member, are newly refused (no use of that shape exists).
- **Behaviour without an operator** is unchanged: no callback is registered, no message posted, and no interrupt made unless an operator asks.

## Security

- **Who can act.** The control channel binds an AF_UNIX socket with a peer UID check (`server.py:258-310`), so only the eval's own user can act, as for `sample cancel` and `cancel-tool-call`. The operator, human or agent, is trusted: their text goes into a member's conversation in the user role, which is what an operator message is.
- **No member reaches the registry.** No tool exposes it, and members cannot call the control channel through the swarm. A member that can reach the host's socket from its sandbox is outside this design and could already cancel the sample.
- **No agent text leaves through it.** Lists and outcomes carry roster names chosen by the eval author, lifecycle words, statuses and fixed sentences; never a name a member chose, a message, a tool result or an error message written by an agent. The evidence holds ids and flags, not text.
- **Untrusted input.** The message text comes from the operator, not from a model; it is passed through unchanged and never parsed. Agent and group names in requests are only looked up in dicts; unknown ones are answered with the registered names.

## Testing

No network, Docker or model provider: mockllm throughout.

- **inspect_ai, the fixes** (`tests/agent/test_channel/test_channel.py`, `tests/model/test_call_tools.py`): a repaired history with parallel calls to different functions has every repair named after its call, and converts through the Anthropic, Bedrock, Google, OpenAI Responses and chat-completions request builders (each provider's request-building function, no network); a tool call cancelled by an enclosing scope leaves a finalised `ToolEvent` with a `cancelled` error, and one cancelled through `ToolEvent._cancel()` is finalised as today.
- **inspect_ai, the registry and routes** (`tests/_control/test_operator.py`, with the control-channel test fixtures): `operator_group()` registers and unregisters, and refuses a duplicate group name and an agent name in two groups; `GET .../sample/agents` lists groups and agents and returns `[]` for a sample with none; each `POST .../sample/agent/{action}` reaches the right method with `interrupt` and `dry_run`, the text from the body; 400 for a missing epoch, for both or neither of `agent` and `group`, for `message` with `group`, for `interrupt` without `message`, and for empty text; 404 for an unknown sample, agent or group, listing the names; 409 with the refusal; `changed: false` passed through; the CLI's `list`, `message --text`, `--file -`, the `--json` envelope and the stock-404 message against a server without the routes.
- **inspect_swarm** (`tests/test_operator.py`, on asyncio and trio, calling the registered targets through inspect_ai's registry, not over HTTP):
  - message with `interrupt=True` to a `react()` member 1 s into a 30 s tool: its next model input ends with the repair, named after the tool, and the message with `source="operator"`; no wait; its sibling's tool completes; the `operator_message` event says `delivered`, `in_turn: true`; the same for a `react(submit=False)` member and a `deepagent()` member inside a `general()` subagent call;
  - message without interrupt mid-tool reaches the generation after the tool; to a paused member, the generation after the resume; to a member still in setup (a tool source that awaits before the loop's first boundary), the first generation;
  - refusals: pending, ended, bridged, a member whose loop has returned; a `generator()` member's message recorded `undelivered` at its end;
  - pause: a member paused mid-turn is held at its next boundary, its `time_limit(0.5)` does not run while held for 1 s, a sample through `eval_async()` counts the hold as waiting time, and it continues after resume; a pending member paused before start is held at boundary 0 with no model call; refused for bridged, `deepagent(background=True)`, a member that started background work, and while stopping; a `generator()` member paused is never held and ends as it would without the pause, not `errored`; a paused member's `swarm_wait` sibling returns `all_waiting`;
  - the swarm latch: running members held at their next boundary, a member the controller starts during the latch held at boundary 0, resume releases them but not a member paused on its own, whose name is in the detail;
  - composition: with a test scheduler callback registered first, the operator's pause holds the member after the scheduler releases it;
  - stop: a member stopped mid-turn, paused and pending ends `stopped` with `ended by operator` while a sibling submits; a bridged member's stop ends it; a second stop is `changed: false`; the swarm's stop through `eval_async()` with a scorer: stop reason `operator`, `ended_by` unset, the final answer chosen, the scorer run, the sample `success`; held members end at once;
  - the list: roster names, never a chosen name; activities `not_started`, `in_turn`, `between_turns`, `held`, `unknown`, `ended`; the refusal reasons in `actions`;
  - names: a nested swarm's group is `swarm:<member>`; two concurrent top-level swarms in one sample are refused with the registry's error.

## Implementation plan

1. **inspect_ai: interrupted conversations on every provider and in the log.** `src/inspect_ai/agent/_channel/channel.py` (`_repair()` names repairs), `src/inspect_ai/model/_call_tools.py` (finalise a `ToolEvent` cancelled by an enclosing scope); the fix tests above. Shared with urgent messages: the first of the two designs to be built carries it.
2. **inspect_ai: operator targets in the control channel.** `src/inspect_ai/_control/operator.py` (new), `src/inspect_ai/log/_samples.py` (`operator_groups`), `src/inspect_ai/_control/server.py` (the two routes), `src/inspect_ai/_cli/ctl/_sample.py` (the `agent` verbs), the ctl reference docs; `tests/_control/test_operator.py`.
3. **inspect_swarm: messages and stops.** Needs steps 1 and 2 in an inspect_ai release, the turn seam ([swarm-turn-seam.md Implementation plan](swarm-turn-seam.md#implementation-plan)), and the switch to `SwarmEvent`. `src/inspect_swarm/_operator.py` (new: `SwarmOperator`, `MemberOperator`, messages, the drain observer, the list), `src/inspect_swarm/_seam.py` (the `loop_bound()` hook), `src/inspect_swarm/_controller/_control.py` (registration in `run()`, `stop("operator")`, undelivered messages at a member's end), `src/inspect_swarm/_evidence.py` (`operator_message`), `docs/operators.qmd` (new: what an operator can do and how time counts), `docs/analysis.qmd` (`stop_reason` `operator`, the event); the message, stop, list and name tests.
4. **inspect_swarm: pause.** `src/inspect_swarm/_operator.py` (the flag, the latch, the callback, registering from `loop_bound()`), `docs/operators.qmd`; the pause, latch and composition tests.

## Open questions

1. **Which surface comes first: `inspect ctl` (this design) or ACP sessions per member?** ctl is on by default, scriptable by agents that watch runs, works on both backends, and needs only new routes over a small registry; ACP gives the interactive editor and TUI experience the issue's binding problem points at, but needs per-agent sessions in ACP's transport, an attached client and asyncio, and has no pause or stop. Recommendation: ctl now, with ACP sessions over the same registry as later work. Confidence: low, because this is a product choice about how operators will mostly watch swarms, which the code does not settle.
2. **Does the swarm's time cap keep counting while the whole swarm is paused?** Recommendation: yes, as the sample's time limit does and as inspect's own hard pause keeps time limits running; an operator retunes the sample's limits with `inspect ctl config` if needed. Confidence: high.

## Not this design

- **ACP sessions per member**, built on this registry: one ACP target per registered agent, with its own binding and a transcript filtered to its span, so that an editor or the TUI can drive any member.
- **Bridged members**: messages through inspect_swe's restart with `--resume` ([Push for bridged members](swarm-communication.md#push-for-bridged-members)), and pause through a hold at the model call.
- **Holding a member during an ACP operator's interrupt recovery.** An ACP interrupt without a redirect still leaves the ACP-bound member waiting in `after_cancel()`, outside the seam, as today; operators through `inspect ctl` never leave a member there.
- **Pausing the swarm's controller** with the swarm, and operator controls for controllers' own model calls.
- **A "message every member" verb**, if shell loops over `message` turn out not to be enough.
- **Operator targets for single-agent samples** (the sample's top-level `react()`), which ACP serves today.
