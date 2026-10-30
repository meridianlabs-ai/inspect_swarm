# Inspect Swarm: operator intervention for every member

## Overview

An operator watching a running swarm can reach any member by name from `inspect ctl`, the control channel every eval already runs, on either async backend. `inspect ctl sample agent list` shows the swarm's members with what each is doing; `message` gives one member a message from the operator, either at its next turn or now, cancelling what its turn is doing, a long tool call included; `pause` and `resume` hold a member, or the whole swarm; `stop` ends one member, or stops the whole swarm the way its own stop does, and the sample goes on to its final answer and its scorers. Messages and pauses reach native members (`react()` and `deepagent()`, with or without background work); bridged CLI members can only be stopped, and the list says so.

inspect_ai gains a small, generic registry: code running in a sample can make named agents, and named groups of them, addressable by the control channel, which gains routes and `inspect ctl` verbs that list them and send them `message`, `pause`, `resume` and `stop`, and a serving path for trio. The swarm registers every member and itself as a group, and runs every action in the swarm's own context. A message is an ordinary operator `UserMessage` posted to the member's own loop's agent channel; to deliver it now, the swarm posts it and then interrupts the turn, so the loop's recovery finds the operator's redirect already queued and never waits. A pause holds a member's own loop between turns through the turn seam, and parks the member's background work at its next model call through a generate hold; a pause of a member or a swarm also pauses every swarm nested inside it. While held, a member's time limit stops and its idle time is waiting time. Stopping a member is the seam's `end(origin="operator")`, and stopping the swarm is `stop("operator")`. Three small inspect_ai fixes make an interrupted conversation valid on every provider, finish its cancelled tool calls in the log, and keep the ids of coalesced operator messages.

## Why

An operator watching a swarm today ([#119](https://github.com/meridianlabs-ai/inspect_swarm/issues/119)) has three tools, and none reaches a member chosen by name:

- **ACP** (an editor or the Inspect TUI attached to the sample) can send messages and interrupt, but it binds whichever member opens its agent channel first, by chance, and no other member ([swarm-communication.md The binder and ACP](swarm-communication.md#the-binder-and-acp)).
- **`inspect ctl`** can cancel the whole sample, cancel one tool call, and read the sample's events and messages, but it knows nothing of members, and on trio it does not run at all.
- **The swarm** has no pause, and nothing stops one member. A controller can start members and choose the answer; an operator's interrupt of the sample cancels everything and is recorded as `ended_by: operator` ([swarm-limits.md Exhaustion and errors](swarm-limits.md#exhaustion-and-errors)).

So an operator who sees one member going the wrong way, or burning budget on a dead end, can only let it run or end the whole sample. Steering, pausing to look, and stopping one member are the interventions an operator of a multi-agent system needs, and the swarm's designs already have most of the pieces: the turn seam holds a member between turns and ends one on request ([swarm-turn-seam.md](swarm-turn-seam.md)), the agent channel carries operator messages and interrupts, and inspect's own hard pause parks model calls.

## Goals and non-goals

Goals:

- For every native member (`react()` and `deepagent()`, `deepagent(background=...)` included), addressed by its roster name: a message from the operator, at the next turn or now (a long tool call included); pause and resume; stop. For bridged members: stop. A member that cannot take an action says why.
- For the whole swarm: pause and resume, and a stop that does not end the sample.
- Say how paused time counts towards member limits, the swarm's time cap, the sample's time limit and its working time.
- An operator surface that every eval already has, on asyncio and trio, scriptable for agents that watch runs, with no client to attach.
- Evidence in the log of what the operator did, joinable to the conversation.

Non-goals:

- **Peer messages and urgent peer steering.** Members steering each other through `Steer` items stays [Urgent messages and steering](swarm-communication.md#urgent-messages-and-steering)'s. This design shares two of its inspect_ai fixes (below) and nothing else.
- **Preemptive pause of a member's own loop.** A pause of a native member's own loop takes effect at its next turn boundary, as every turn-seam hold does. Cutting a long tool call short is `message --interrupt` or the existing `inspect ctl sample cancel-tool-call`.
- **ACP per member.** ACP keeps binding one member, as today ([Not this design](#not-this-design)).
- **A controller API.** Controllers already have the seam's `on_boundary()` and `end()`; nothing here adds to `SwarmControl`.
- **Broadcast.** A message to every member is a shell loop over `message`, as the control channel's no-fan-out rule asks.
- **A retunable swarm time cap.** The cap keeps counting while paused ([Pause](#pause)).
- **Messages and pauses for bridged members.** Bridged CLI members are stop-only in this design (decision: Ransom, 2026-10-30): reaching a CLI needs its inspect_swe driver to read its agent channel ([Not this design](#not-this-design)).

## Current behaviour

Code references are to inspect_swarm `main` at `fb7e2b1` and inspect_ai `main` at `0e49f5f65`. Paths are relative to each repository's root. Claims marked *(spike)* were checked by running a swarm on both anyio backends.

### The agent channel

- **Producers** hold an `AgentRef` with `post(item)` and `interrupt(Cancel)` (`src/inspect_ai/agent/_channel/ref.py:13-41`). `interrupt` posts the item and cancels the loop's bound `turn_scope()`, if any; with none bound it is a plain post (`channel.py:164-179`). `turn_active` says whether a turn scope is bound (`channel.py:229-237`), and `subscribe_turn_state()` reports each scope's `started`, `ended` and `cancelled`, synchronously in the loop's task (`channel.py:293-333`).
- **`react()`** extends its messages with `before_turn()` at the top of every turn, runs generation and tools inside `turn_scope()`, and on `AgentInterrupted` extends them with `after_cancel()` and continues (`src/inspect_ai/agent/_react.py:263-269`, `:369-373`; `react(submit=False)` at `:504-553`).
- **`before_turn()`** drains the queue and returns the drained `UserMessage`s, consecutive operator messages coalesced into one new message (`channel.py:432-453`, `items.py:74-112`). `_drain()` tells drain observers of the original items before the coalescing (`channel.py:239-263`), so a merged message's id is one no observer saw, and the merge keeps no metadata.
- **`after_cancel()`** returns repairs for the interrupted tool calls, then the drained `UserMessage`s, and waits for one only if none was drained (`channel.py:455-479`). So a `UserMessage` posted before the interrupt is found at that drain and the loop resumes at once.
- *(spike)* A controller posted `UserMessage(ChatMessageUser(content=..., source="operator"))` to one `react()` member's channel 1 s into a 30 s tool call, then called `interrupt(Cancel(reason="user_cancel"))`. The member's next model input ended with the repair `Tool call cancelled by user.` and the operator's message, its sibling's tool ran to completion, and the sample ended `success`.
- **Repairs carry no function name.** `_repair()` builds each `ChatMessageTool` from the call id, text and error only (`channel.py:392-426`) *(spike: the repair's `function` was `None`)*. Bedrock's converter raises for such a message and Google's sends an empty function-response name ([swarm-communication.md Interrupting a member](swarm-communication.md#interrupting-a-member-through-its-agent-channel)), so an interrupted conversation cannot continue on Bedrock.
- **A tool call cancelled by an enclosing scope stays pending.** The per-call path finalises a `ToolEvent` cancelled through its own cancel function (`src/inspect_ai/model/_call_tools.py:568-620`), but one cancelled by an enclosing scope, as an interrupt's turn scope is, is left as it was *(spike: the interrupted `slow` call's `ToolEvent` had `pending=True` in the finished log)*. ACP finalises such events itself before it interrupts (`cancel_current_turn()`, `src/inspect_ai/agent/_acp/transport_live.py:1406`); nothing else does.
- **ACP** binds the first channel offered in a sample and rejects the rest until it is unbound (`transport_live.py:661-718`), and its drain observer treats any drained `UserMessage` as the operator's redirect (`:720-732`). ACP's server refuses to start on trio (`src/inspect_ai/agent/_acp/server.py:133-150`).

### The control channel

- `inspect ctl` talks HTTP over an AF_UNIX socket to a server that runs on the eval's own event loop, on by default ([control-channel.md](https://github.com/meridianlabs-ai/inspect_ai/blob/main/design/ctl/control-channel.md)). A handler therefore runs between the eval's own steps, so a handler that never awaits is atomic with respect to the sample; but it runs in the server's own task and context, not the sample's, so context-bound reads such as `sample_limits()` and `transcript()` do not see the sample there.
- **The server is asyncio only.** `ControlServer.start()` binds the socket itself, serves it with uvicorn through a server subclass that takes `asyncio.get_running_loop()` and an `asyncio.Event`, and runs it with `asyncio.create_task()` (`src/inspect_ai/_control/server.py:410-440`, `:1702-1762`). The peer-UID check is a uvicorn protocol subclass over the accepted asyncio transport (`:258-326`). `control_server()` logs a startup failure and runs the eval without the control surface (`:1839-1875`). So on trio the server never starts.
- **The app** is FastAPI with no lifespan handlers and no streaming responses (the only `lifespan` in the module is the mirrored uvicorn shutdown at `:485`). FastAPI and Starlette run on anyio.
- Sample mutations are `POST /evals/{eval_id}/sample/<verb>` with `sample_id` and a required `epoch` as query parameters, unknown parameters refused before the handler runs (`src/inspect_ai/_control/strict.py`), and a result with `ok`, `changed` and `dry_run` (`sample/cancel-tool-call` at `server.py:1274-1323`, its handler at `cancel.py:944-1010`). A handler finds its sample with `find_active_sample()` (`src/inspect_ai/_control/state.py:977-995`). The CLI wraps them in a shared scaffold, with the epoch required whenever the task runs more than one, and a target that is not the sample in an option, never after the optional `EPOCH` positional ([tool-call-cancel.md](https://github.com/meridianlabs-ai/inspect_ai/blob/main/design/ctl/tool-call-cancel.md#surface)).
- `ActiveSample` keeps per-sample state that the control channel and ACP read, such as `acp_transport` and `transcript` (`src/inspect_ai/log/_samples.py:212`, `:253`).
- **Hard pause.** `wait_generate_dispatch()`, awaited at the top of every generate and native-compaction attempt (`src/inspect_ai/model/_model.py:1369`, `:1519`), parks the attempt while a process, task or model hard pause is closed, as an open sample wait (waiting time unless other work runs), with its connection slot released while parked (`src/inspect_ai/_control/pause.py:620-660`). Time limits keep running: "wall clock is explicitly the operator's risk" ([pause-resume.md](https://github.com/meridianlabs-ai/inspect_ai/blob/main/design/ctl/pause-resume.md#future-work)).

### The swarm

- **Stopping.** `stop(reason)` closes the bus, records the reason, cancels the controller's run, closes every running member's lever, computes the effective grace with `_grace_now()` and sets `_stopped`, synchronously; grace and the hard stop follow, then the final step (`src/inspect_swarm/_controller/_control.py:534-548`, `:653-664`). `_grace_now()` reads `sample_limits()` (`:666-671`), which depends on the caller's context. The first call wins. Stop reasons are a closed set ([swarm-limits.md Stopping](swarm-limits.md#stopping)); `ended_by` separately records what ended the swarm from outside (`_control.py:745-757`). `start()` already runs member tasks from a context saved in `run()` (`_member_ctx`, `:349-352`, `:433-434`).
- **Members** are `MemberRun`s, which controllers see through `MemberHandle` (`_control.py:56-100`, `:192-243`). A member's context knows whether it is bridged and whether it has started background work (`src/inspect_swarm/_budget.py:303-322`). Background work runs in the member's owner, in tasks that copy the context of their `background()` call (`src/inspect_swarm/_background.py:83-108`), so a `ContextVar` set around the member's agent call reaches them. A member's status is `cancelled` when the hard stop or a cancellation reaches it (`src/inspect_swarm/_member.py:553-556`).
- **Names.** Member names are unique within a sample, across every swarm that runs in it: each names a timeline, and a swarm whose member name is already a timeline in the sample is refused before any member runs (`src/inspect_swarm/_evidence.py:207-224`). A nested swarm runs inside an outer member's work (`tests/test_background_members.py:1088-1100`).
- **The turn seam** ([swarm-turn-seam.md](swarm-turn-seam.md)) gives each member a gate that binds the member's own loop (`turn_loop`) at its first boundary, registers boundary callbacks with `on_boundary(callback, origin=...)` (which raises once the swarm is stopping), holds the member while they run, and ends it on `end(origin=...)`, `stopped` at a boundary or `cancelled` when its turn or cleanup outlasts the grace period ([Ending a member](swarm-turn-seam.md#the-callback-contract)). It refuses bridged members and `deepagent()` with background dispatch at registration, ends `errored` a member with callbacks whose background work runs at a boundary, and ends `errored` a member with callbacks that never reaches a boundary ([Supported members](swarm-turn-seam.md#supported-members)). It leaves interrupts to this design ([Interrupts](swarm-turn-seam.md#interrupts)).

### Bridged CLIs

Every bridge-backed agent in inspect_swe (Claude Code, Codex, Gemini CLI, OpenCode, Kimi Code, mini-SWE-agent, Antigravity) runs its CLI through `sandbox_agent_bridge`, so its turns and tool calls run inside the CLI, and none opens an agent channel. Nothing the swarm posts reaches such a member, and the turn seam refuses to hold one ([Supported members](swarm-turn-seam.md#supported-members)).

### Who uses which member shapes

From `examples/*.py`, `docs/members.qmd` and the tests: `react()` and `deepagent()` members, wired with `swarm_tools()` or not; `deepagent(background=...)` members and custom agents that call `background()` (`docs/members.qmd:138`); inspect_swe's bridged agents (`docs/members.qmd:169`); nested swarms run by a member (tests only); and test-only custom agents that generate without an agent channel or only sleep (`tests/swarm_runtime.py:106-135`). ACP agents are refused as members (`docs/restrictions.qmd:11`).

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


# inspect_ai: src/inspect_ai/_control/pause.py (added)
class GenerateHold(Protocol):
    def closed(self) -> bool: ...          # park the current attempt
    async def wait(self) -> None: ...      # returns when it may go on, or raises


@contextmanager
def generate_hold(hold: GenerateHold) -> Iterator[None]: ...   # added to the enclosing holds for model calls in the block
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
| `message` | an operator `UserMessage` at its next turn; with `--interrupt`, its turn is cancelled and it continues with the message now; refused for bridged members | none (no broadcast) |
| `pause` / `resume` | its own loop held at its next boundary by a turn-seam callback, origin `operator`; its background work parked at its next model call; refused for bridged members | a latch: every member paused while it is set, members started meanwhile and nested swarms included |
| `stop` | `end(origin="operator")`: `stopped`, or `cancelled` past the grace period; the rest of the swarm goes on | `stop("operator")`: the swarm's orderly stop, final answer, then the sample's scorers |

### The inspect_ai registry

A new private module, `src/inspect_ai/_control/operator.py`, holds the protocols above and `operator_group(name, group)`, a context manager that adds the group to the active sample's `ActiveSample.operator_groups` (a new `dict[str, OperatorGroup]`, empty by default) for its block and removes it on exit, synchronously in a `finally`. Outside a sample it does nothing. It raises `ValueError` when the sample already has a group of that name, or an agent of one of the group's names in another group, naming the clash. inspect_ai knows nothing of swarms: a group is any set of named agents that some code in the sample can act on, and the swarm is its only registrant.

The registry calls the registrant's methods synchronously from the control server's handlers. They run on the eval's loop, so an action is atomic with respect to the agents it acts on, but in the server's context, so a registrant that needs its own context enters it itself (the swarm does, [The swarm's registration](#the-swarms-registration)). Methods must not await or do work proportional to a conversation's size (the control channel's "cheap shoveling" rule). Outcomes and infos carry no agent-written text: names, lifecycle words, statuses and fixed sentences only.

### The control channel routes and verbs

- **`GET /evals/{eval_id}/sample/agents?sample_id=&epoch=`** returns `{sample_id, epoch, groups: [OperatorGroupInfo...]}`; a sample with no groups returns `groups: []`, and the CLI says that the sample has no addressable agents. `epoch` defaults to 1, as for the other reads.
- **`POST /evals/{eval_id}/sample/agent/{action}?sample_id=&epoch=&agent=|group=[&interrupt=][&dry_run=]`**, `action` one of `message`, `pause`, `resume`, `stop`. `epoch` is required, and exactly one of `agent` and `group` (a 400 otherwise); `message` takes only `agent`, `interrupt` only with `message`, and the message text as a JSON body, `{"text": "..."}`, not empty. An unknown sample, agent or group is a 404 that lists the names there are; a refused action (`OperatorOutcome.refused`) is a 409 with the reason; otherwise `{ok: true, changed, dry_run, target, detail}`. A repeat that changes nothing (a second pause, a resume of a running member, a stop of a stopping swarm) is `changed: false`, never an error. The text is a body because it is free, possibly long and multi-line, where the other mutations' parameters are scalars; the strict-parameter check is unchanged, since it checks query parameters only.
- **The CLI** adds `agent` under `inspect ctl sample`, through the shared sample-mutation scaffold, so target resolution, the epoch rule, `--json`, `--terse` and `--dry-run` work as for `cancel-tool-call`. The agent or group is an option, as `--tool-call-id` is, because `EPOCH` is an optional positional. `--file` reads the text from a file or `-` for stdin. `list` prints one row per agent: group, name, state, status, activity, paused, and the actions available.
- **Version skew.** New routes, so an older server answers with the router's stock 404, which the CLI reports as "this eval's inspect is too old for `sample agent`", as `cancel-tool-call` does. No control-API version change.

### Serving the control channel on trio

`ControlServer.start()` keeps uvicorn on asyncio, unchanged. On trio (`current_async_backend() == "trio"`) it serves the same FastAPI app on the same socket it has already bound, permissioned and written to the discovery file, with a small anyio-native HTTP/1.1 server in a new private module, `src/inspect_ai/_control/anyio_server.py`:

- **Listening.** `anyio.abc.SocketListener.from_socket(sock)` wraps the bound socket; the serve loop runs in a task group that `ControlServer` opens in `start()` and keeps until `stop()`, which `control_server()` holds across its `yield`, as it holds the asyncio task today. `stop()` stops accepting, gives open connections the same 5 s to finish that the asyncio path gives the serve task, then cancels the group.
- **The peer check.** Each accepted stream's raw socket (`stream.extra(SocketAttribute.raw_socket)`) goes through the existing `peer_uid()`; a connection from another UID is closed before a byte is read, with the same warning, and the check fails open where the platform has no credential API, as on asyncio.
- **Connections.** A `CapacityLimiter` of `_MAX_CONCURRENT_CONNECTIONS` (uvicorn's `limit_concurrency` on the asyncio path) bounds open connections; over it, a connection gets a 503 and is closed. Each connection parses requests with h11 (uvicorn's HTTP/1.1 parser, already installed with it; inspect_ai declares it directly), keeps the connection alive for 5 s between requests (`timeout_keep_alive=5`), and closes on any parse error with a 400.
- **ASGI.** For each request it builds an ASGI 3 `http` scope (method, path, raw path, query string, headers, `client` and `server` `None`, as for AF_UNIX on uvicorn), calls the app with `receive` and `send` bridged to h11 events, and writes the response. No lifespan protocol: the app has no lifespan handlers, and the module asserts that it has none, so one added later fails its test rather than being skipped silently. No streaming beyond chunked bodies; the push (SSE) phase of the control channel, when it comes, extends this path or replaces it.
- **Failures** keep `control_server()`'s rule: a startup failure is logged and the eval runs without the control surface.

The keep-alive park (`--ctl-server=keep`) builds a fresh server over the same registry in a sequential `anyio.run`; on trio it takes the same path.

### The inspect_ai fixes for interrupted conversations

The same two fixes [Urgent messages and steering](swarm-communication.md#urgent-messages-and-steering) lists (its inspect_ai change, items 2 and 3), and a third for evidence:

- **`_repair()` names its repairs.** Each repair's `function` is the name of the unanswered call it answers, taken from the last assistant message's `tool_calls`, so the repaired conversation converts on Bedrock and gives Google a matching function-response name. ACP's interrupts get the same fix.
- **A `ToolEvent` cancelled by an enclosing scope is finalised** in `_call_tools.py`, with `pending` cleared and a `cancelled` error, as generate already finalises a cancelled `ModelEvent` (`src/inspect_ai/model/_model.py:1686-1700`). Events ACP has already finalised are left as they are.
- **`coalesce()` keeps what it merges.** A merged message's `metadata` gains `coalesced_from`, the ids of the messages it replaces, in order, and keeps the first message's other metadata keys. A single message is returned as it is, as today. So a message's id names the delivered message either directly or through `coalesced_from`, in `before_turn()` and `after_cancel()` alike, whoever posted it.

Whichever of this design and urgent messages is built first carries the first two; the other then needs only its own changes.

### The inspect_ai generate hold

`src/inspect_ai/_control/pause.py` gains `GenerateHold` and `generate_hold(hold)`, a context manager that adds a hold to a private `ContextVar` for its block. The variable holds a tuple: `generate_hold()` appends to the tuple it finds rather than replacing it, so a block inside another keeps every enclosing hold, and leaving the block restores the tuple as it was. `wait_generate_dispatch()` checks the holds beside the process, task and model latches: while any hold in the current context's tuple is closed, the attempt parks on that hold's `wait()` inside the same parked-wait accounting as a hard pause (an open sample wait, the connection slot released and reacquired), then checks every hold again from the outermost, so an attempt goes on only when every enclosing hold and every inspect latch is open. An exception from `wait()` propagates from the attempt. Tasks that copy the context of the block, as `background()`'s tasks do, see the holds. inspect_ai sets none itself.

### The swarm's registration

`SwarmRun.run()` saves its context (`contextvars.copy_context()`, taken after the bus and budget variables are set, as `_member_ctx` is for members) and registers one group around its member task group, after the members' timelines are claimed and before any member starts; the registration ends in `run()`'s `finally`. The group (`SwarmOperator`, new module `src/inspect_swarm/_operator.py`) has one `MemberOperator` per roster member, under the member's roster name: never a name a member chose for itself (`choose_name=True`), which is model output.

**Every action runs in the swarm's context.** Each `SwarmOperator` and `MemberOperator` method runs its body with `self._context.copy().run(...)`, so `stop()`'s `_grace_now()`, `end()`'s grace timer and anything else context-bound read the sample's limits, transcript and budget, as they do when a member or the controller calls them, rather than the control server's. And `stop()` computes the effective grace before it records the reason, so an error in that computation leaves the swarm as it was, not with a reason recorded and `_stopped` never set.

The group's name is `swarm` for a swarm run directly in the sample, and `swarm:<member>` for a swarm run inside a member's work (read from `current_member` when the run starts), so a nested swarm's group is distinct from its outer swarm's and names the member that runs it. The same `current_member` gives the nested group its parent, the outer member's `MemberOperator`, which the member's `MemberContext` keeps for this ([Nested swarms](#nested-swarms)). Member names cannot clash, since they are unique in a sample already. Two swarms that run concurrently in one sample outside any member, or inside the same member, would clash on the group name; no one runs that shape, and the registry's `ValueError` refuses it when the second swarm starts, naming the group.

### The loop-bound hook

The seam gains one hook, `loop_bound(member, channel)`, which its gate calls synchronously when it binds a member's `turn_loop`, **after** its stop and end check (the seam's step 1) and before it looks for callbacks (step 2). So a member whose swarm has begun to stop, or whose end was requested, is ended by the gate before the hook runs, and the hook never registers a callback once `on_boundary()` would raise. The hook also checks the swarm's stop reason itself, as a guard, and does nothing when it is set. Otherwise it posts the member's kept messages ([Messages](#messages)), subscribes the swarm's drain and turn-state observers ([Evidence](#evidence)) and registers a pending pause ([Pause](#pause)). `before_turn()` drains after the gate returns, so kept messages reach the first generation, and a pause registered here holds the member at this same boundary.

### Messages

`MemberOperator.message(text, interrupt=...)` builds `ChatMessageUser(content=text, source="operator", metadata={"inspect_swarm": {"version": 1, "type": "operator", "id": "op-<n>"}})`, with the id counted per swarm run, and then, by the member's state:

| Member | `interrupt=False` | `interrupt=True` |
|---|---|---|
| pending or ended | refused: "has not started" / "has ended" | the same |
| bridged | refused: a bridged member's CLI reads no agent channel; it takes `stop` only | the same |
| running, its turn loop not yet bound (setup before its first boundary, or a custom agent with no agent loop) | kept by the swarm, posted by `loop_bound()`; a member that never binds a loop never receives it | the same: there is no turn to cancel yet |
| running, its turn loop bound and closed (its loop has returned) | refused: "its agent loop has ended" | the same |
| between turns, held, or paused | posted; the loop's next `before_turn()` returns it, after any hold | posted; nothing to interrupt, and the detail says so |
| in a turn (`turn_active`) | posted; it reaches the next turn, after the turn's tool calls and subagents finish | posted, then `interrupt(Cancel(reason="user_cancel"))`: the turn's model call, tool calls and subagents are cancelled, and `after_cancel()` returns the repairs and the message at once |

The posting is to the turn seam's `turn_loop`, the member's own top-level loop, through `channel._ref()`: the loop whose boundaries the seam holds, whose turn scope encloses its subagents' loops, and which no background task binds. The bus's `MemberContext.loop` is not used, because it may be a child loop ([The member's gate](swarm-turn-seam.md#the-members-gate)).

**Never an interrupt without a redirect.** The message is posted before the interrupt, in the same synchronous step, so the drain in `after_cancel()` always finds it and the loop never waits for an operator ([The binder and ACP](swarm-communication.md#the-binder-and-acp)). If the turn ends on its own between the post and the cancellation's delivery, the scope's exit raises nothing, the turn ends as usual, and the message is drained at the next `before_turn()`.

**What an interrupted member loses** is what any interrupt loses: the cancelled tool calls' results (a sandbox command may keep running in the sandbox), a cancelled synchronous subagent's result, and the usage of a cancelled model call, which the ledger reports as unknown. That is the operator's choice; without `--interrupt` nothing is cancelled. A member's background work is not in its turn, so an interrupt does not cancel it.

**The member is not told by whom.** The message is an operator turn like any other, in the user role with `source="operator"`. Operator messages queued together, from `inspect ctl` and from ACP, are coalesced into one by `before_turn()` as today; with the `coalesce()` fix the merged message names the ids it replaces.

**With ACP.** If ACP drives the member, a `message` releases an ACP operator's pending interrupt as that operator's own redirect would, and ACP's drain observer clears its pending flag: both are operators. Nothing else in ACP changes, and the swarm never calls ACP.

**A member in `swarm_wait`** is in a turn: a plain message waits until the wait returns (at most the wait's `max_wait`), and `--interrupt` cancels the wait, which loses nothing. The detail line says which the member is doing.

### Bridged members

Bridged members are stop-only (decision: Ransom, 2026-10-30). Their CLI runs its own turns and opens no agent channel, so `message`, with or without `--interrupt`, and `pause` are refused with that reason, and `list` shows them with only `stop` available. `stop` works for them as for any member ([Stop](#stop)).

### Pause

A pause holds a member until it is resumed. A member is paused while any of these is set: its own operator flag; its swarm's latch; and, for a swarm nested inside a member, the pause of that outer member, which reaches every swarm nested inside it, at any depth ([Nested swarms](#nested-swarms)). A pause has two parts:

- **The member's own loop, at its next boundary**, through the turn seam. While a member is paused and its turn loop has bound, the swarm keeps one boundary callback registered on it with `origin="operator"`. At a boundary it returns `None` at once if the member is no longer paused; otherwise it waits, inside `suspend_working_limit()`, until a resume or a new pause anywhere in its chain changes whether the member is paused, and checks again. When the member is no longer paused, the swarm wakes it and unregisters it, so a member that is not paused has no operator callback and the seam's rules for members with callbacks do not apply to it. When the loop has not bound yet (a pending member, or one in setup), the pause is recorded and the callback registered from `loop_bound()`, before that boundary's callbacks run, so the member is held at boundary 0. Registering only once the loop has reached the gate means a pause never makes a member `errored` under the seam's rule for members that never reach a boundary.
- **The member's background work, at its next model call**, through the generate hold. The member wrapper enters `generate_hold(MemberHold)` around the agent call, so the member's background tasks see it, and the background owner marks the tasks it starts with a `ContextVar` naming the member. A member's hold is closed while the member is paused and the attempt runs in one of that member's background tasks. Calls in the member's own loop and its turns are never parked by it: the seam holds those at the boundary, and parking a subagent mid-turn would leave the member neither working nor at a boundary.

**Background work.** For a member with background work (`deepagent(background=...)`, or a custom agent that calls `background()`), the operator's pause relaxes two seam rules that exist because a held member must do nothing: `on_boundary()`'s refusal of background dispatch, and the gate's ending `errored` a member whose background work runs at a boundary. The swarm registers its operator callback through the runtime's own registration, which skips them, because the generate hold parks that work: each background task stops at its next model call, a tool call already running in it finishes first, and the background owner still refuses new work while the member is held. Any other caller's callbacks keep the seam's rules unchanged ([swarm-turn-seam.md Supported members](swarm-turn-seam.md#supported-members)). A background task parked while its member's own loop runs does not make the member held.

**Stopping a parked call.** A stop of the swarm or an `end()` of the member wakes every parked call, and `wait()` then raises the member lever's limit error, as the closed lever would have refused the call at its pre-dispatch check; the error ends that background task alone, as a lever error from a background task does today.

**Refused**, with the reason: bridged members, ended members, and any pause once the swarm is stopping.

**The whole swarm.** `SwarmOperator.pause()` sets the latch, which pauses every member: running ones are held at their next boundary, their background work parked at its next model call, and members the controller starts while it is set are paused from their start. The latch is not a fan-out of member pauses: it also holds members the controller starts later, and resuming it leaves members the operator paused one by one paused, with the detail naming them, as inspect's `task resume` names a process pause that still holds a task.

**Composition.** The operator's callback runs after callbacks registered before it ([Composition](swarm-turn-seam.md#the-callback-contract)): a pause registered after a scheduler's callback holds the member after the scheduler lets it go. Messages posted to a paused member are drained when the hold ends. Inspect's own `pause --now` parks model calls independently; a call goes on only when both are open.

**The controller** keeps running during a pause of the swarm. It can start members, which are paused from their start, and it sees no new events: a held member is still running.

**Time while paused**, as the seam defines it for every hold ([Time](swarm-turn-seam.md#time)), with the operator's waits marked as waiting:

| | Paused time |
|---|---|
| The member's time limit | does not count while the member is held |
| The member's token, cost, turn and message limits | unaffected: a held member makes no model call |
| Its background tasks | parked at their next model call; a tool call already running in one keeps running and counts as working time |
| The swarm's time cap | counts |
| The sample's time limit | counts |
| The sample's working time | waiting time, unless other work in the sample runs meanwhile |
| `swarm_wait` | a held member is not active, its background tasks included (they are parked or finishing a tool call), so when every other member is waiting or held, their waits return `all_waiting` |

The swarm's time cap and the sample's time limit are deadlines for the whole run, so they keep counting, as inspect's own hard pause keeps a sample's time limit running: wall clock is the operator's risk (decision: Ransom, 2026-10-30). A whole-swarm pause can therefore use up the cap and start the swarm's stop with no member generating again. The operator can retune the sample's time limit with `inspect ctl config`; the swarm's cap, derived when it starts, is not retunable.

### Nested swarms

A swarm run inside a member's work (a member that calls `run(swarm(...))`, or `member(swarm(...))`) registers its group with a parent: the outer `MemberOperator`, read from `current_member` when the inner run starts. Pauses compose down that chain, so no inner control can undo an outer one:

- **Paused is computed, never copied.** An inner member is paused while its own flag, its swarm's latch, its parent member's pause or any pause further up the chain is set. A pause of an outer member, or of the outer swarm, therefore pauses every member of every swarm nested inside it, and those members are held at their next boundaries even while the outer member's own loop is mid-turn running the inner swarm.
- **Changes propagate.** A pause or resume anywhere in the chain re-evaluates every member below it: members that become paused get the operator callback (or have it registered when their loop binds), members that stop being paused are woken and the callback unregistered.
- **A resume clears only what it names.** Resuming an inner member clears its own flag, and resuming the inner swarm its latch; while an outer pause holds, the member stays paused and the outcome's detail names the outer pause, as for the swarm latch above.
- **Generate holds compose too.** An inner member's wrapper adds its hold inside the outer member's, so an inner member's calls made in the outer member's background task (a nested swarm started with `background()`) are parked by the outer hold whatever the inner member's own state, and the inner hold cannot reopen them.

The outer member itself counts as held only once its own loop reaches a boundary; until then its turn waits on its paused inner members, and its time limit runs, as for any member mid-turn.

### Stop

- **A member.** `MemberOperator.stop()` calls the seam's `end(origin="operator")`, in the swarm's context. A held or paused member ends at once; a member in a turn ends at its next boundary, or, if its turn or cleanup outlasts the effective grace, by cancellation; a pending one at boundary 0 once started ([Ending a member from outside the chain](swarm-turn-seam.md#the-callback-contract)). Its status is `stopped`, with `ended by operator` in its lever's message, when it ends at a boundary or on a normal return, and `cancelled` when the grace timer cancels it, as the hard stop's members are; never a submission. The rest of the swarm goes on, and the controller sees its `MemberEnded` as for any member. It works for every member shape, bridged ones included, since it closes the lever and cancels as a stop does. A second stop, or a stop of an ended member, changes nothing.
- **The swarm.** `SwarmOperator.stop()` calls `stop("operator")`, in the swarm's context. `operator` joins the closed set of stop reasons. It is the swarm's own orderly stop: the controller's run is cancelled, members are soft-stopped, held and paused members end at once, grace and the hard stop follow, and the final step chooses the answer. The swarm then returns, so the sample continues to its scorers, and the ledger's `stop_reason` is `operator` with `ended_by` unset. An operator who wants the sample itself ended still uses `inspect ctl sample cancel`, recorded as `ended_by: operator`.

### The list

`MemberOperator.info()` gives the member's roster name, its handle's state and status, whether it is paused, and its activity: `not_started`; `held` while it is held; `in_turn` or `between_turns` from its turn loop's `turn_active`; `unknown` for a running member whose loop has not bound (setup, a bridged member, a custom agent with no agent loop); `ended`. `actions` gives, for each action, `None` or the reason it would be refused now (for a message, also when it would only be kept), so an operator or a watching agent can see what is possible before it asks. `SwarmOperator.info()` adds the swarm's state (`running` or `stopping`, with the stop reason) and whether the latch is set.

### Evidence

One `SwarmEvent` kind through `swarm_event()` ([swarm-event-types.md](swarm-event-types.md#swarmevent)), written in the member's own task and span, never from the control server's handler:

- **`operator_message`**, once per message: `id` (the swarm's `op-<n>`), `message_id` (the id of the `ChatMessageUser` the swarm built), `interrupt`, `in_turn` (whether a turn was active when it was posted) and `outcome`. A drain observer the swarm subscribes on the turn loop marks a message drained when its item leaves the queue; a turn-state observer writes `delivered` at the loop's next `started`, which comes after react has added what `before_turn()` or `after_cancel()` returned to the conversation. Both observers run synchronously in the loop's task. A message not yet delivered when the member ends, kept, queued or drained without a further turn, is written by the member's ending as `undelivered`. In the conversation, `message_id` is either the user message itself or, when operator messages were coalesced, one of the ids in the merged message's `coalesced_from`. The text is not in the event: it is in the conversation and the next `ModelEvent`'s input.
- **Pauses** need no kind of their own: every boundary at which the operator's callback ran is the seam's `boundary` event, with `operator` in its `origins` and the time held in `held_for`, and the seam's `holds` and `held_time` metrics include it. Parked background tasks write nothing.
- **Stops** are the seam's `boundary` outcome `end` (or the member's status `stopped` with `ended by operator`, or `cancelled` past the grace period), and the swarm's `stop_reason` `operator` in the ledger and the result record.

A message's interrupt is also visible in the conversation (the repair `Tool call cancelled by user.`, with the tool's name) and, with the inspect_ai fix, as the finalised cancelled `ToolEvent`.

### Supported members

| Member | `message` | `--interrupt` | `pause` | `stop` |
|---|---|---|---|---|
| `react()`, `react(submit=False)`, `deepagent()` | yes | yes | its loop at its next boundary | yes |
| `deepagent(background=...)`, custom agents with `background()` | yes, to the top-level loop | yes; background work goes on | its loop at its next boundary, its background work at its next model call | yes |
| custom agent with an agent loop that passes its state | yes | yes | its loop at its next boundary | yes |
| a member running a nested swarm (`member(swarm(...))`, or a tool that runs one) | to its own loop, if it has one | the same | its loop, if any, at its next boundary, and every member of the nested swarm ([Nested swarms](#nested-swarms)) | yes |
| custom agent with no agent channel (test-only: `generator()`, `sleeper()`) | kept, recorded `undelivered` | the same | accepted, never held: it has no boundary and no background work | yes |
| bridged (Claude Code, Codex, Gemini CLI, OpenCode, Kimi Code, mini-SWE-agent, Antigravity) | refused | refused | refused | yes |
| nested loops (subagents) | not addressable: their member's loop is | | | |
| a nested swarm's members | addressable by their own names, in the group `swarm:<member>` | | | |

## Alternatives considered

**ACP sessions per member as the first surface.** ACP's server would list each member as a target of its own, so an editor or the Inspect TUI attaches to `worker-2` as it does to a single-agent sample today. Against it: it reworks ACP's per-sample transport into per-agent sessions, needs `--acp-server` and an attached client, refuses trio, and has no pause or stop. Rejected as the first surface; ACP sessions can later be built over the same registry (decision: Ransom, 2026-10-30).

**Hypercorn for trio.** It serves ASGI on trio, but it is a new dependency for one backend and accepts connections itself, where the anyio path reuses the socket `ControlServer` has already bound and permissioned and runs the existing peer check on each accepted socket. **One anyio server for both backends** would retire uvicorn, but the asyncio path is shipped and its shutdown and concurrency behaviour are pinned by tests; changing it is not needed for trio. Rejected for an anyio path on trio only.

**`Steer` items for operator messages.** `Steer` is designed for producer-composed content that must not count as the operator's redirect. An operator's message is the operator's redirect, and `UserMessage` is the item for it: it satisfies `after_cancel()`'s wait, which is exactly what makes an interrupt safe. Rejected; `Steer` stays urgent peer messages'.

**Delivery evidence from the drain observer alone.** Observers see items before `coalesce()` merges them, so the delivered message's id would be unknown whenever two operator messages meet, and a drained item may never reach the model if the loop ends first. Rejected for `coalesced_from` and confirmation at the next `started`.


**Pausing background work through the seam alone.** The seam cannot hold a background task's loop, and holding only the member's own loop leaves its background work generating. Holding every one of a member's model calls instead, the member's own loop included, would park a subagent mid-turn, leaving the member neither at a boundary nor working, with its time limit running. Rejected for the seam at the boundary plus the generate hold for background work.

**Copying a pause onto nested swarms**, setting inner members' flags when an outer member or swarm is paused. An inner resume would then clear the copied flag and bypass the outer pause, and a member started in the inner swarm afterwards would miss it. Rejected for a pause computed up the chain at every check. **One generate hold per context**, each member's wrapper replacing the enclosing hold, would likewise let an inner member's open hold release calls an outer pause should park. Rejected for a tuple of holds that compose.

**A preemptive pause** (interrupt, then hold). The interrupt's recovery waits in `after_cancel()`, which is not a boundary and which the seam does not hold; posting a message to release it would put text the operator did not write in the conversation. Rejected; an operator who needs the member to stop now sends `message --interrupt` or cancels the tool call, then pauses.

**Registering the operator callback on every member at its start**, idle until a pause. Every member would then have a callback, and the seam would end `errored` any custom member with no agent loop at its end. Rejected for registering only while paused and only once the loop has bound.

**Group actions as fan-out over members.** A loop misses members the controller starts later, and stopping every member is not the swarm's stop: it skips the controller's cancellation and stop reason, and the controller would see every member end and return `all_done`. Rejected for a group target that the swarm implements.

**Reserving `swarm` as a member name** for a single namespace of agents and groups. `member(swarm(...))` defaults its name to `swarm` (its registry name is `inspect_swarm/swarm`), so reserving it would refuse an existing shape. Rejected for separate options, `--agent` and `--group`.

## Compatibility and migration

- **inspect_ai.** New private modules, a new `ActiveSample` field defaulting to empty, new control routes and CLI verbs, a trio serving path, and a hold that is never set unless a registrant sets it: additions. The repair and `ToolEvent` fixes change what interrupted conversations hold: repairs gain their function name, and a cancelled tool call's event is finalised rather than left pending, in ACP's interrupts too. A coalesced operator message gains `coalesced_from` in its metadata. No log schema change. h11 becomes a direct dependency (it is already installed with uvicorn).
- **inspect_swarm.** No public API change. One new stop reason, `operator`, in the ledger and the result record; analysis code that matches stop reasons exhaustively must add it, and `docs/analysis.qmd`'s list gains it. One new `SwarmEvent` kind, `operator_message`. A swarm in a sample now registers operator targets for its run. Two swarms run concurrently in one sample outside any member, or inside the same member, are newly refused (no use of that shape exists).
- **Behaviour without an operator** is unchanged: no callback is registered, no hold closed, no message posted, and no interrupt made unless an operator asks.

## Security

- **Who can act.** The control channel binds an AF_UNIX socket with a peer UID check, on both serving paths, so only the eval's own user can act, as for `sample cancel` and `cancel-tool-call`. The operator, human or agent, is trusted: their text goes into a member's conversation in the user role, which is what an operator message is.
- **No member reaches the registry or the hold.** No tool exposes them, and the hold's `ContextVar` is set only by the member wrapper. A member that can reach the host's socket from its sandbox is outside this design and could already cancel the sample.
- **No agent text leaves through it.** Lists and outcomes carry roster names chosen by the eval author, lifecycle words, statuses and fixed sentences; never a name a member chose, a message, a tool result or an error message written by an agent. The evidence holds ids, flags and durations, not text.
- **Untrusted input.** The message text comes from the operator, not from a model; it is passed through unchanged and never parsed. On trio, request bytes from the socket are parsed by h11, as by uvicorn on asyncio, and a malformed request closes the connection. Agent and group names in requests are only looked up in dicts; unknown ones are answered with the registered names.

## Testing

No network or model provider: mockllm throughout.

- **inspect_ai, the fixes** (`tests/agent/test_channel/test_channel.py`, `tests/model/test_call_tools.py`): a repaired history with parallel calls to different functions has every repair named after its call, and converts through the Anthropic, Bedrock, Google, OpenAI Responses and chat-completions request builders (each provider's request-building function, no network); a tool call cancelled by an enclosing scope leaves a finalised `ToolEvent` with a `cancelled` error, and one cancelled through `ToolEvent._cancel()` is finalised as today; two operator messages, and an ACP message with a ctl one, coalesced by `before_turn()` and by `after_cancel()`, give one message whose `coalesced_from` lists both ids in order.
- **inspect_ai, the generate hold** (`tests/_control/test_pause.py`): a closed hold parks a generate and a native compaction attempt, as waiting time, with its connection slot released, until `wait()` returns; an exception from `wait()` propagates; a task that copies the context parks, one outside does not; a hold and a task hard pause both closed need both open; with two nested `generate_hold()` blocks, a call inside both parks while either is closed and goes on only when both are open, and leaving the inner block restores the outer hold alone.
- **inspect_ai, the registry, routes and serving** (`tests/_control/test_operator.py`, `tests/_control/test_server.py`, with the control-channel test fixtures, on asyncio and trio): the control server starts under each backend and serves `GET /tasks` and a mutation over its socket (httpx's async client with a UDS transport); on trio, a connection from another UID is rejected (with the UID check patched, as the asyncio test does), connections over the cap get 503, a malformed request gets 400, and `stop()` closes idle keep-alive connections; `operator_group()` registers and unregisters, and refuses a duplicate group name and an agent name in two groups; `GET .../sample/agents` lists groups and agents and returns `[]` for a sample with none; each `POST .../sample/agent/{action}` reaches the right method with `interrupt` and `dry_run`, the text from the body; 400 for a missing epoch, for both or neither of `agent` and `group`, for `message` with `group`, for `interrupt` without `message`, and for empty text; 404 for an unknown sample, agent or group, listing the names; 409 with the refusal; `changed: false` passed through; the CLI's `list`, `message --text`, `--file -`, the `--json` envelope and the stock-404 message against a server without the routes.
- **inspect_swarm, end to end over the socket** (`tests/test_operator.py`, on asyncio and trio, a real sample through `eval_async(..., ctl_server=True)` with a scorer, the socket found through the control channel's discovery file, requests sent with httpx from a task outside the sample's context): `message --interrupt` to a `react()` member 1 s into a 30 s tool, whose next model input ends with the named repair and the message; then `stop --group swarm`, after which the swarm's stop reason is `operator`, `ended_by` is unset, the final answer is chosen, the scorer runs and the sample is `success`.
- **inspect_swarm, through the registry** (`tests/test_operator.py`, on asyncio and trio, calling the registered targets from a task with an empty context, as a handler has):
  - message: without interrupt mid-tool, it reaches the generation after the tool; to a paused member, the generation after the resume; to a member still in setup (a tool source that awaits before the loop's first boundary), the first generation; the same for `react(submit=False)` and for a `deepagent()` member inside a `general()` subagent call; refusals for pending, ended and a member whose loop has returned; a `generator()` member's message recorded `undelivered` at its end; two messages sent together are coalesced and both `operator_message` events are `delivered`, their ids in the merged message's `coalesced_from`; a message drained just before a stop, with no further turn, is `undelivered`;
  - pause: a member paused mid-turn is held at its next boundary, its `time_limit(0.5)` does not run while held for 1 s, a sample through `eval_async()` counts the hold as waiting time, and it continues after resume; a pending member paused before start is held at boundary 0 with no model call; a member paused during setup and then reached by the swarm's time cap before boundary 0 ends `stopped` or `cancelled` as without the pause, with no `RuntimeError`; a pause while the swarm is stopping is refused; a `generator()` member paused is never held and ends as it would without the pause, not `errored`; `message` and `pause` to a bridged member are refused with the reason, and `list` shows only `stop` for it; a paused member's `swarm_wait` sibling returns `all_waiting`;
  - background: a `deepagent(background=True)` member and a custom member whose `background()` task generates in a loop: paused, the own loop is held at its boundary, the background task parks at its next model call (its running tool call finishes first), neither ends `errored`, and both go on after resume; a stop while parked ends the task with the lever's error and the member `stopped`;
  - the swarm latch: running members held, a member the controller starts during the latch held at boundary 0, resume releases them but not a member paused on its own, whose name is in the detail;
  - composition: with a test scheduler callback registered first, the operator's pause holds the member after the scheduler releases it;
  - stop: a member stopped while paused, while pending, and mid-turn with a short tool ends `stopped` with `ended by operator`; one in a tool that outlasts the grace period ends `cancelled`; a sibling submits meanwhile; a bridged member's stop ends it; a second stop is `changed: false`;
  - the list: roster names, never a chosen name; activities `not_started`, `in_turn`, `between_turns`, `held`, `unknown`, `ended`; the refusal reasons in `actions`;
  - nested swarms: a member whose tool runs a nested swarm, and a `member(swarm(...))` member, paused, and the outer swarm's latch set: every inner member is held at its next boundary; resuming an inner member, or the inner swarm, leaves it held with the outer pause named in the detail; resuming the outer member or latch releases them; an inner member started while the outer pause holds is held at boundary 0; a nested swarm started in an outer member's `background()` task: with the outer member paused, inner members' calls park on the outer hold, and resuming the inner members does not release them;
  - names: a nested swarm's group is `swarm:<member>`; two concurrent top-level swarms in one sample are refused with the registry's error.

## Implementation plan

1. **inspect_ai: interrupted conversations on every provider and in the log.** `src/inspect_ai/agent/_channel/channel.py` (`_repair()` names repairs), `src/inspect_ai/agent/_channel/items.py` (`coalesce()` keeps `coalesced_from`), `src/inspect_ai/model/_call_tools.py` (finalise a `ToolEvent` cancelled by an enclosing scope); the fix tests above. Shared with urgent messages: the first of the two designs to be built carries the first two.
2. **inspect_ai: the control channel on trio.** `src/inspect_ai/_control/anyio_server.py` (new), `src/inspect_ai/_control/server.py` (`start()` and `stop()` choose the path by backend), `pyproject.toml` (h11); the serving tests above on both backends.
3. **inspect_ai: operator targets and the generate hold.** `src/inspect_ai/_control/operator.py` (new), `src/inspect_ai/log/_samples.py` (`operator_groups`), `src/inspect_ai/_control/server.py` (the two routes), `src/inspect_ai/_cli/ctl/_sample.py` (the `agent` verbs), `src/inspect_ai/_control/pause.py` (`GenerateHold`, `generate_hold()`, checked in `wait_generate_dispatch()`), the ctl reference docs; `tests/_control/test_operator.py`, `tests/_control/test_pause.py`.
4. **inspect_swarm: messages and stops.** Needs the turn seam ([swarm-turn-seam.md Implementation plan](swarm-turn-seam.md#implementation-plan)), the switch to `SwarmEvent`, and steps 1 to 3 in inspect_ai. `src/inspect_swarm/_operator.py` (new: `SwarmOperator`, `MemberOperator`, running each action in the saved context, messages, the observers, the list), `src/inspect_swarm/_seam.py` (the `loop_bound()` hook after the stop and end check), `src/inspect_swarm/_controller/_control.py` (the saved context, registration in `run()`, `stop("operator")` with the grace computed before the reason is recorded, undelivered messages at a member's end), `src/inspect_swarm/_evidence.py` (`operator_message`), `docs/operators.qmd` (new: what an operator can do and how time counts), `docs/analysis.qmd` (`stop_reason` `operator`, the events); the message, stop, list, name and end-to-end tests.
5. **inspect_swarm: pause.** `src/inspect_swarm/_operator.py` (the flag, the latch, the parent chain of nested swarms, the callback, registering from `loop_bound()`, `MemberHold`), `src/inspect_swarm/_member.py` (enter `generate_hold()` around the agent call), `src/inspect_swarm/_background.py` (mark background tasks for the hold), `src/inspect_swarm/_seam.py` (the runtime's registration that skips the background rules for the operator's callback), `src/inspect_swarm/_bus/_wait.py` (a held member's background tasks do not make it active), `docs/operators.qmd`; the pause, background, latch, nested-swarm and composition tests.

## Open questions

None.

## Not this design

- **ACP sessions per member**, built on this registry: one ACP target per registered agent, with its own binding and a transcript filtered to its span, so that an editor or the TUI can drive any member.
- **Messages, interrupts and pauses for bridged members** (Claude Code, Codex, Gemini CLI, OpenCode, Kimi Code, mini-SWE-agent, Antigravity): each CLI's inspect_swe driver would read its agent channel at the CLI's safe points, relaunch the CLI on its own conversation with the operator's message, and be paused there or at its model requests. This design refuses them and leaves them stop-only (decision: Ransom, 2026-10-30).
- **Holding a member during an ACP operator's interrupt recovery.** An ACP interrupt without a redirect still leaves the ACP-bound member waiting in `after_cancel()`, outside the seam, as today; operators through `inspect ctl` never leave a member there.
- **Pausing the swarm's controller** with the swarm, and operator controls for controllers' own model calls.
- **A "message every member" verb**, if shell loops over `message` turn out not to be enough.
- **Operator targets for single-agent samples** (the sample's top-level `react()`), which ACP serves today.
- **A retunable swarm time cap**, if operators' long pauses turn out to need one.
