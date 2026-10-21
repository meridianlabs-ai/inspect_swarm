# Inspect Swarm: waiting for other members

Status: proposed, 2026-10-21. Issue: none. Author: agent (Claude), reviewed by Codex; see the PR.

## Overview

A member that has done its part and needs a peer's result calls one tool, `swarm_wait`, instead of polling. The call blocks inside the member's tool call and returns as soon as something the member has not yet been told about happens: a shared-memory file changes where it asked to watch (a file, a directory, or the whole board), or another member ends. It returns at once if that has already happened. It also returns when nothing more can happen (every member is waiting, has ended or has not started), when the swarm begins to stop, or after a timeout the channel bounds. The result names changed files by id and ended members by name and status; it never contains file content, which the member reads with an ordinary, fenced `view`.

The wait belongs to the swarm's bus, not to one channel: the bus keeps the waiters, the release rules and the evidence, and a channel only says which of its committed records a waiter has not been told about. The shared-memory channel is the first channel that wakes waits, so `swarm_wait` comes with it; the later messages channel plugs in the same way instead of keeping a wait of its own in `read_messages`. Each wait writes one evidence event saying how long it waited and what ended it, from which the metrics report waits, wait time and idle time per member.

## Background

This is D-3 of [swarm-implementation-plan.md](swarm-implementation-plan.md#designs). The first internal user's feedback (2026-10-21) was that members that finished their own part early polled the board with `sleep 60; cat /shared/notes.md`, spending a tool turn on every check. Ransom's decision (2026-10-21): "I think it will be common for members to wait on other members if they are working together." So waiting is a common, first-class case in M2, not something deferred to the messages channel or to persistent members.

It builds on the bus as merged (M2-6, #33; M2-7, #42) and on the shared-memory channel ([swarm-memory-channel.md](swarm-memory-channel.md), D-1; implementation in draft PR #49), and amends both designs and the Wake section of [swarm-communication.md](swarm-communication.md#wake-and-turn-boundaries) where it changes them.

Code references are to inspect_swarm `main` at `8144c41`, PR #49 (`m2-8-shared-memory`) at `5636220`, PR #50 (M2-5, `m2-5-background-members`) at `f98645f`, and inspect_ai `main` at `e3310b753`, which this worktree's lock installs. Paths are relative to each repository's root. Claims marked *(spike)* were checked by running code on both anyio backends.

## Why

Polling costs what M2 measures. Each `sleep 60; cat` is a model call over the member's whole context, so it inflates the member's tokens and cost, its step count, the swarm's `longest_member_steps` and, for the member whose answer is chosen, `steps_to_answer` (M1-11 counts completed model calls, `src/inspect_swarm/_metrics.py:96-107`). Repeated identical polls also look like the stuck-in-loops signal (`longest_identical_tool_run`, `_metrics.py:16-20`). A wait inside one tool call costs one step for the whole wait.

Nothing in M2 lets a member wait:

- The shared-memory channel never blocks: a write record has `wake=False` ([swarm-memory-channel.md The write record](swarm-memory-channel.md#the-write-record)), and every command runs to completion (PR #49's tool awaits only `Bus.deliver()`, which itself never awaits).
- Notices (M2-9) arrive at the member's next turn boundary. They save the read, not the polling turn: a member with nothing to do must still take turns to reach a boundary.
- The only wait in the designs is the later messages channel's `read_messages(wait_seconds=...)`, with its rules (the all-waiting release, the stop releasing waits, the idle-time metric) written for that channel alone ([swarm-communication.md Wake](swarm-communication.md#wake-and-turn-boundaries)). M2 has no messages channel.

## Goals and non-goals

Goals:

- One wait, owned by the bus, that the shared-memory channel exposes in M2 and the later messages channel reuses, with its tool shape decided.
- What wakes a wait: a filter on board changes, and members' ends; and what the wait returns.
- No deadlock: the all-waiting release, the stop, a member ending mid-wait, timeouts, and what a member's descendants' waits mean.
- What members get on the `filesystem` channel alone, and with per-member sandboxes.
- How waiting counts towards time limits, the idle-time metric, and the evidence a wait writes.

Non-goals:

- The messages channel, persistent members, coordinator wake-from-idle and push delivery. They stay future work; [Room for later work](#room-for-later-work) says how they fit.
- Bridged members' waits: a swarm with a bus channel refuses bridged members in M2 (M2-6).
- Watching files in a sandbox. The filesystem channel stays observed ([swarm-communication.md The filesystem channel](swarm-communication.md#the-filesystem-channel)).
- Changing what notices or the status line say.

## Current behaviour

### The bus and member states

- **Member states** are `pending`, `running` and `ended`. `SwarmControl.start()` sets `running` (`src/inspect_swarm/_controller/_control.py:294`); the runtime's `_end()` sets `ended` and the status, writes `member_result` evidence and queues `MemberEnded`, all synchronously (`_control.py:513-529`). The bus holds the same member objects (`src/inspect_swarm/_bus/_bus.py:48-49`), so it reads their states directly; nothing tells the bus when a member ends.
- **Stopping.** `stop(reason)` calls `self.bus.close()` first, synchronously (`_control.py:449-455`); `close()` only sets the stopping flag that refuses later records (`_bus.py:90-92`). Then levers close, grace runs, and the hard stop cancels each running member's scope (`_control.py:549-559`).
- **Delivery.** `Bus.deliver()` runs steps 0 and 2 to 4 with no `await`; step 4 applies the record and writes `delivered` (`_bus.py:94-153`). Every record is built with `wake=False` (`_bus.py:120`); `Draft` has no `wake` field (`src/inspect_swarm/_bus/_record.py:66-76`).
- **Loops.** `swarm_tools()` binds the member to the first agent channel it is resolved in (`context.loop`) and gives other loops only `"member"`-reach channels' tools (`src/inspect_swarm/_bus/_tools.py:11-29`). PR #49 adds `Bus.nested(context)`, true in any loop but the member's own, and `Bus.running(name)`.
- **Who can be a member beside a bus channel.** `swarm()` refuses a bridged member when any channel has records (`src/inspect_swarm/_swarm.py:272-300`). A bridged agent the rule misses runs without the channel's tools. Background members are refused on `main` and accepted from M2-5.

### The shared-memory channel (PR #49)

- **News.** A file is news to a loop when its current version was written by another loop and is above the version that loop has seen; a tombstone only when the loop saw it or was notified of it before (`_is_news`, `src/inspect_swarm/_channel/shared_memory.py:848-860`). The member's own loop also leaves out what a notice covered (`covered`, `:862-869`).
- **The status line** ends every `shared_memory` result and names, by id, up to ten files that are news to the calling loop (`_status_line`, `:886-890`). It reports and does not mark anything; a file stays in it until the loop views it ([swarm-memory-channel.md Notices](swarm-memory-channel.md#notices-and-the-status-line)).
- **The tool** is built with `parallel=True` and `max_output=0` (`:428-435`), as memory() is parallel.

### Background members (PR #50, M2-5)

- Each member's background work runs in an owner, `MemberBackground`, which counts `running` tasks (`src/inspect_swarm/_background.py:41`, `:99`, `:123`) and holds the member's `MemberContext`.
- When the member's own agent returns, `_drain()` closes the lever with `member_ended`, waits up to the effective grace for the owner's tasks, then cancels what is left (`src/inspect_swarm/_member.py:558-564`). The member is `running` throughout the drain and ends after it.

### Tool calls in one turn

inspect_ai splits a turn's tool calls into stages: consecutive calls to tools with `parallel=True` run together, and each call to a tool with `parallel=False` is a stage of its own (`src/inspect_ai/model/_call_tools.py:461-487`). *(spike: a turn calling `slow`, `swarm_wait`, `slow`, where only `swarm_wait` had `parallel=False`, ran them one after another on asyncio and trio: 0.0–0.3 s, 0.3–0.4 s, 0.4–0.7 s.)* So while a call to a non-parallel tool runs, its loop runs nothing else.

### Time

[swarm-limits.md](swarm-limits.md#interactions) already decides, for the messages channel's wait, that waiting is wall-clock time inside a tool call: it counts towards the member's time limit, the swarm's time cap, the sample's time limit and the sample's working time, and a stop releases every wait. Plan decision 12 keeps idle time as "the time members spend in the bus's read waits" and drops per-member waiting time from M1 because a member's subagents and parallel calls make a plain sum of waits misleading ([swarm-implementation-plan.md Decisions](swarm-implementation-plan.md#decisions), item 12).

### Who waits today

- **The first internal user** ran a leaderless swarm over the shared sandbox with the `filesystem` channel, whose instructions tell members to read `/shared/notes.md` "before you start and again as you work" (`src/inspect_swarm/_channel/filesystem.py:15-20`). Their members polled it with `sleep 60`.
- **Epoch's LeanOpenProblems swarm** (the M2 port) shares a board through `memory()`, with background `general()` children doing most of the work ([swarm-lean-open-problems.md Their swarm](swarm-lean-open-problems.md#their-swarm)). Their members have no wait either.
- **Nothing else.** No tool in this repository, its tests or the merged designs waits on a peer outside the later messages channel.

So the shapes this design serves are: native `react()` and `deepagent()` members and their `general()` children (synchronous, and background from M2-5), waiting for board changes and for peers to end, in swarms with the shared-memory channel, over a shared sandbox or per-member sandboxes.

## Design

### At a glance

```
 member worker-1                         member worker-2
 swarm_wait(path="/shared_memory/results")    shared_memory(create, /shared_memory/results/lemma.md)
   │ register waiter x-3                               │
   │ (returns at once if something is unreported)      ▼
   │                                     Bus.deliver(): step 4 apply ── waiters whose filter the
   │◀──── event set ──────────────────────────────────── write matches (x-3) ── delivered.woke
   ▼
 "1 shared memory file changed where you waited (f-7 new; by worker-2). View one with ..."

 Other releases, all from the bus:  a member ends (every waiter)  ·  no member active (every waiter)
                                    stop: bus.close() (every waiter)  ·  the member's drain (its waiters)
                                    timeout  ·  cancellation (hard stop, member time limit)
```

### The tool

```python
swarm_wait(path: str | None = None, timeout: int | None = None) -> str
```

Built per run by the bus with `parallel=False` and offered by `swarm_tools()` when at least one open channel wakes waits ([Channels that wake waits](#channels-that-wake-waits)); in M2 that is the shared-memory channel, so a swarm with `channels=["shared_memory"]` gives every member `shared_memory` and `swarm_wait`, and a swarm with `channels=["filesystem"]` or `channels=[]` gives neither. Its reach is the reach of the channels it waits on: the shared memory's is `"member"`, so a member's subagents get `swarm_wait` too, and their waits watch the board for them as their own `view`s do.

Description (fixed template; the numbers come from the configuration):

> Wait until something you have not been told about happens in the swarm: a shared memory file changes where you watch (another agent's write, including your own subagents'), or another member ends. Returns at once if that has already happened. It also returns when every member is waiting, has ended or has not started, when the swarm is stopping, or after `timeout` seconds. It returns file ids and member names, never file content; view a file to read it. Use it instead of polling with sleep.

- `path`: "A shared memory file or directory to watch, such as `/shared_memory/results`, or a file id such as `f-3`. Without it, any change to shared memory wakes you." The shared-memory channel checks it with its path grammar (or the file id grammar) and refuses anything else with the grammar's `ToolError`. The path need not exist yet, so a member can wait for a file to be created. A file is under `path` when its current path (a tombstone's: the path it had) is `path` or starts with `path + "/"`, or when its id is `path`.
- `timeout`: "Seconds to wait at most (default and maximum: 600)." `None` means `max_wait`; any value is clamped to 1 to `max_wait` (default 600). The result states the effective value when the wait times out, so a clamp is visible to the model.

**Why a separate tool, not a `wait` command on `shared_memory`.** swarm-communication.md rejected a separate `wait_for_messages` tool as "one more tool the model must learn" when `read_messages(wait_seconds=...)` could carry the wait. Three things change that here:

- **Ends are not a board operation.** A member waits as often for a peer to finish as for a file, and a peer's end is the bus's knowledge, not the board's.
- **One wait for every channel.** With the shared memory and, later, messages both open, a member waiting for "a reply or a result on the board" needs one call that either wakes. A wait inside `read_messages` could not wake on a board write, and a wait inside `shared_memory` could not wake on a message. So the one extra tool is learned once and serves every channel, and the messages channel drops its own wait ([Room for later work](#room-for-later-work)).
- **The all-waiting rule needs to know the loop is idle.** `shared_memory` must stay `parallel=True`, as memory() is, so a `view` and a `bash` run together. A wait inside it could run beside a long `bash` call in the same turn, and the member would look idle while it builds. A separate tool with `parallel=False` runs alone in its loop's turn ([Tool calls in one turn](#tool-calls-in-one-turn)), so a loop in a wait is doing nothing else.

### What wakes a wait

A wait is **level-triggered on what the loop has not been told**. It returns as soon as, for the waiting loop, there is:

1. **an unreported board change under `path`**: a file under `path` that is news to the loop (the channel's existing rule: written by another loop, newer than what the loop has seen, a tombstone only if heard of before) at a version above the highest version the loop has been **told** of; or
2. **an unreported end**: a member other than the waiter's own that has ended and that no earlier wait result of this loop named.

If either holds when the wait is called, it returns at once. Otherwise the bus registers the waiter and returns it when one becomes true.

**Told.** The shared-memory state keeps, per loop, the highest version of each file the loop has been told about (`told: dict[loop span, dict[file id, version]]`). A loop is told about a version when it views or writes it (the existing `seen`), when a status line names it, when a wait result names it, and, for the member's own loop, when a notice covers it. A wait result in the member's own loop also sets `covered`, as a notice does, so neither the next notice nor that loop's status line repeats what the wait said. Otherwise the status line is unchanged: it still lists a file until the loop views it, and naming it there marks it told only for waits.

Why level-triggered on "told", not on the alternatives:

- **Edge-triggered** (only changes after the wait starts) loses a write that lands between the member's last tool result and its wait call, and the member then sleeps to the timeout with the result already on the board.
- **Level-triggered on news alone** (unviewed files) returns at once, forever, for a file under `path` the member saw in a status line and chose not to view: polling again.
- "Told" is the line between them: the wait returns for exactly what the loop has not heard of, once.

**Board changes, not every commit.** With many members writing, waking on every commit would be polling again; `path` narrows it, and a write by the waiting loop itself never wakes it. A write by the member's own subagent does, because it is news to the waiting loop, as it is in the status line and notices ([swarm-memory-channel.md Notices](swarm-memory-channel.md#notices-and-the-status-line)).

**Every end wakes every waiter.** A member's end is reported to every loop of every other member, once, whatever its `path`. There is no `members` filter. Ends are rare (at most one per member per run, so at most N−1 extra returns per loop), each one matters to collaborators (the peer's work is final, and there is one fewer peer to wait for), and waking only waiters that named the member would need a second filter and would leave a waiter whose peer ended without writing to sleep to its timeout.

### What a wait returns

The result is fixed-template lines with counts, swarm-assigned file ids, roster names and statuses. It never contains file content, a path a peer chose, or the waiter's own `path` (the waiter knows it).

| Ended by | Result |
|---|---|
| Unreported board change | `2 shared memory files changed where you waited (f-3, f-9 new; by worker-2, worker-4). View one with shared_memory(command="view", path="f-3").` |
| Unreported end | `worker-2 ended (submitted). worker-4 ended (member_limit:cost).` |
| No member active | `Nothing can change: every member is waiting, has ended or has not started.` |
| Stop | `The swarm is stopping.` |
| The member's drain | `worker-1 is ending: its own agent has finished.` |
| Timeout | `Nothing you waited for happened in 600 s.` |

- A wait woken by news reports everything unreported at that moment: changes under `path` (with `new` and `deleted` markers, up to ten ids then "and N more", as notices list them) and ends, each on its line, and marks what it named as told. Files counted in "and N more" are not marked, so a second wait returns them.
- The result ends with the shared memory's status line, as every swarm tool result does, leaving out files already named above.
- Why no content: a view is fenced, evidenced as `read` and bounded by the read budget for one file; a wait may report many files, and the model chooses which to read. Returning content would need a budget across files and a second kind of read. One `view` after a wake is the cost.

### When a wait ends without news

- **No member active.** A running member is **waiting** when its own loop (the bound loop, `not bus.nested(context)`) is in `swarm_wait` and none of its background work is running (from M2-5, `context.background_running == 0`; before M2-5 a member has none). A member is **active** when it is running and not waiting. Pending and ended members are not active. When no member is active and some wait is registered, every waiter is released at once with the all-waiting result, and a wait called in that state returns at once with it. The bus checks this synchronously when a waiter registers, when a member ends, and when a member's background work drops to none ([Background members](#background-members-and-descendants)). A waiter stops counting as waiting the moment the bus releases it, not when its task resumes, so a check that runs in between cannot count a member that is about to act. A member the controller starts later can still change the board; a released member sees it at its next wait or status line.
- **Stop.** `bus.close()`, the first act of `stop(reason)`, releases every waiter with the stop result, and a wait called afterwards returns it at once. So a waiting member does not spend the stop's grace blocked in a tool, and its next model call meets the closed lever ([swarm-limits.md Stopping](swarm-limits.md#stopping), step 0).
- **The member's drain** (M2-5). When the member's own agent returns, the drain releases that member's waiters with the drain result and refuses its loops' later waits the same way, at once: the drain lets calls in flight finish, and a wait for peers serves nothing once the member's attempt is over. The background child's next model call then meets the lever closed with `member_ended`.
- **Timeout.** After the effective timeout.
- **Cancellation.** A hard stop, the member's time limit or a cancellation from outside cancels the wait's task. The waiter is removed and its evidence written in a synchronous `finally`, and the next all-waiting check sees one waiter fewer.
- **Refusals.** A wait from a member that is not running returns `ToolError("worker-1 has ended.")`, as `shared_memory` does; a member's own limits cannot fire during a wait, since a wait makes no model call.

**Why this cannot deadlock.** A wait ends by news, by no member being active, by the stop or the drain, or by its timeout, which is at most `max_wait`. Two members each waiting for the other's file are both waiting, so neither is active; if every other member is also waiting, ended or pending, both are released at once, and otherwise someone active can still write. The time cap, the sample's time limit and the hard stop end everything else. The registration, the check for news and the all-waiting check run with no `await` between them, so a write or an end cannot fall between "nothing to report" and "registered".

### Background members and descendants

**A waiting subagent does not make its member waiting.** Only the member's own loop's wait counts, and only while none of its background work runs. The rule must never release waits while someone is working: a released waiter that waits again while everyone is still waiting is released again, so a false release would turn waits back into polling. Each case:

- **A synchronous subagent** runs inside a tool call of the member's own loop, so the member's own loop is not in a wait. Its wait means the member is idle only if every loop between it and the member's own loop is blocked in a call that runs alone, which the bus cannot check. deepagent happens to make its subagent tool serial when a subagent holds a tool source such as `swarm_tools()` (`src/inspect_ai/agent/_deepagent/agent_tool.py:329`, `:1015-1050`), but a nested agent run through another tool can share a stage with other work. Not counting it costs only that the other members' waits end by news, an end or the timeout rather than by the all-waiting release.
- **Background children** (M2-5) run in the member's owner beside its own loop. The member's own loop waiting while a child runs is not idle. A child waiting while its siblings or the member's own loop work is not idle either. Counting a child's wait would need the owner to say which of its tasks are blocked in a wait with no nested work, which it does not track.
- So with background work running, a member is active. In Epoch's shape (children working, the top loop in `agent_wait` on them) the all-waiting release does not apply, and waits end by news, ends, the stop or the drain, or their timeouts. That is the price of a rule that never releases wrongly; [Not this design](#not-this-design) lists the refinement.

Descendants still wait for themselves: a `general()` child, synchronous or background, gets `swarm_wait` through the shared memory's `"member"` reach, its waits watch the board as its views do, its own "told" marks are its own (per loop), and every release except the all-waiting one reaches it.

**From M2-5:** `MemberContext` gains `background_running: int`, kept by the owner beside its own `running` count; when it drops to 0 the owner calls `Bus.recheck()` (through `current_bus`), since the member may now be waiting (it cannot rise from 0 while the member is waiting, since nothing of the member runs to start work); and `_drain()` calls `Bus.member_ending(name)` right after it closes the lever. Both land with whichever of M2-5 and this design's pull request merges second.

### The filesystem channel and per-member sandboxes

- **Filesystem only** (`channels=["filesystem"]`, M1's default): no `swarm_wait`. The filesystem channel is instructions-only and observed: no swarm code sees a file written in the sandbox, so a wait could not wake on the notes file, only on peers' ends and its timeout. And `swarm_tools()` gives no tools without a bus channel, which keeps a `channels=[]` arm free of anything that reports peers' states. A filesystem swarm that wants waits adds the shared-memory channel and keeps shared notes on the board ([Open questions](#open-questions)).
- **Filesystem and shared memory together** over a shared sandbox: `swarm_wait` wakes on board writes and ends, never on sandbox files. The tool's description and the shared memory's instructions say what it watches.
- **Per-member sandboxes** cannot be combined with the filesystem channel (`_swarm.py:319-321`); their members' only channel is the shared memory, so they get the full wait.

### Time

Intended, and documented for eval authors in `shared_memory()`'s docstring and the user docs (M2-11): a wait is wall-clock time inside a tool call, as swarm-limits.md decided for `read_messages`. It counts towards the member's time limit, the swarm's time cap, the sample's time limit and the sample's working time. A wait makes no model call, so it adds no tokens, cost or steps. Reporting waits to Inspect as waiting time would collapse across members, as connection waits do, which is why swarm-limits.md kept them as working time; the swarm measures them itself.

### Evidence

One `InfoEvent` per wait, written when it ends (in the synchronous `finally`, so also on cancellation), in the waiter's tool span, with comm's common fields (`version: 1`, `type: "comm"`, `member`, `span_id`, `loop`, `nested`):

| Field | Meaning |
|---|---|
| `kind` | `"waited"` |
| `wait` | the wait id, `x-4`, numbered per run |
| `channels` | the channels it waited on (`["shared_memory"]`) |
| `path` | the filter as given, or `null` (grammar-checked, so safe to log) |
| `timeout` | requested and effective seconds |
| `started`, `duration` | swarm time of the call, and seconds waited (0 for an immediate return) |
| `idle` | seconds of the wait during which the member counted as waiting (only own-loop waits can have any) |
| `outcome` | `news`, `all_waiting`, `stopping`, `member_ending`, `timeout` or `cancelled` |
| `files` | `[{"file", "version", "deleted"}]` the result named |
| `ended` | `[{"member", "status"}]` the result named |

And `delivered` gains `woke`: the ids of the waits the write ended. With the `waited` event's `files` (ids and versions), the later dependency-linked critical path can tell that a member's idle time ended at a peer's commit, which the `read` that usually follows then links by content.

### Metrics

Per member, written with the swarm's metrics from the `waited` events, by M2-10 (plan: "wait time"):

- `waits`: the number of `waited` events in the member's spans, and `waits_by_outcome`.
- `wait_time`: the sum of their durations, across all the member's loops. Concurrent waits of different loops each count, so it can exceed the member's running time; it measures how much waiting the member's loops did.
- `idle_time`: the sum of `idle`, the time the member as a whole did nothing but wait for peers (its own loop in `swarm_wait`, no background work). This is swarm.md's idle time, made exact for members with subagents, which plan decision 12 asked for: it is not a sum of waits that ignores work running beside them.

Neither is Inspect's working time, and neither replaces per-member working time (still future work). `longest_identical_tool_run` is unchanged: a member that calls `swarm_wait` with the same arguments several times in a row has a run of identical calls, which analysis that reads the signal can tell apart by function name.

### Channels that wake waits

```python
# src/inspect_swarm/_channel/_channel.py
class Channel:
    ...
    max_wait: int | None = None
    """The longest `swarm_wait` this channel lets a member make, in seconds, or `None` for a channel whose records wake no waits."""

@dataclass(frozen=True)
class WaitSpec:
    id: str              # "x-4"
    member: str
    loop: str | None     # the waiting loop's agent span
    own: bool            # the member's own loop
    path: str | None     # the shared memory's filter; later channels add theirs

class WaitingState(ChannelState, Protocol):
    def wait_check(self, spec: WaitSpec) -> None: ...         # validate the filter; raise ToolError
    def unreported(self, spec: WaitSpec) -> bool: ...         # anything unreported now
    def wakes(self, spec: WaitSpec, record: Record) -> bool: ...  # does this applied record make it so
    def report(self, spec: WaitSpec) -> list[str]: ...        # result lines; marks what they name told
```

- A channel whose state wakes waits sets `max_wait` from its factory (like `storm`) and its state implements `WaitingState`. The bus offers `swarm_wait` when some open channel has `max_wait`, clamps to the largest such `max_wait`, and asks only those states.
- **Records that can wake** set `wake=True` (`Draft` gains `wake: bool = False`, which the bus copies to the record). Shared-memory writes always set it: every write may end a wait whose filter it matches, and the writer has no choice, as no write has a parameter for it. This amends D-1's `wake=False`.
- `wakes()` is called at step 4, after `apply()`, for each registered waiter, with no `await`; it looks only at the files the record changed, so its cost is per write, not per board. `unreported()` scans the board once, at registration and at a recheck.

### The bus

```python
# src/inspect_swarm/_bus/_wait.py (new), used by Bus
class Bus:
    async def wait(self, path: str | None, timeout: int | None) -> str: ...   # the tool's body
    def member_ended(self, name: str, status: str) -> None: ...   # from SwarmRun._end()
    def member_ending(self, name: str) -> None: ...               # from M2-5's drain
    def recheck(self) -> None: ...                                # from M2-5's owner
    def close(self) -> None: ...                                  # now also releases every waiter
```

`wait()`, in the calling member's context:

1. Refuse if the member is not running. If the bus is closed or the member is draining, return that result at once.
2. Build the `WaitSpec` (`own` from `not bus.nested(context)`), clamp the timeout, and call `wait_check()` on each waiting state.
3. If any state's `unreported()` is true or an end is unreported to this loop, report and return at once.
4. Register the waiter (an `anyio.Event`, its spec and a released flag), mark the member waiting if `own` and it has no background work, and run the all-waiting check, which may release it at once.
5. Await the event under `anyio.move_on_after(timeout)`. Steps 1 to 4 have no `await`.
6. In `finally`: unregister, stop the member's idle clock, build the result (`report()` lines and ends, or the release reason), write `waited`.

Release sources set the waiter's event, its reason and its released flag synchronously (a released waiter no longer makes its member waiting): `deliver()` step 4 (each waiter for which a waiting state's `wakes()` is true; their ids go into `delivered.woke`), `member_ended()` (every waiter of another member; then the all-waiting check), `member_ending()` (the member's waiters), `recheck()` (the all-waiting check), and `close()` (every waiter). `SwarmRun._end()` calls `bus.member_ended(m.name, status)` right after it marks the member ended.

### Configuration

```python
@swarm_channel
def shared_memory(
    ...,                         # D-1's parameters, unchanged
    max_wait: int = 600,         # seconds; the longest swarm_wait
) -> Channel: ...
```

`max_wait` is logged with the channel's other parameters, so arms that differ in it get different eval-set identifiers, and `shared_memory()` raises `ValueError` outside 1 to 3,600. 600 s is long enough that a member with nothing to do costs about one model call every ten minutes, and short enough that a wait nothing will end (a peer working on something else for an hour) still lets the member reconsider. The shared memory's instructions gain one sentence: "To wait for another member's work, call swarm_wait rather than polling."

There is no switch to turn waiting off for an arm; [Not this design](#not-this-design) lists it.

### Room for later work

- **The messages channel** sets `max_wait` (its designed 120) and implements `WaitingState` with `"loop"` reach: a `wake=True` message delivered to the member wakes its own loop's wait, `report()` gives `3 unread messages (from worker-2, worker-4). Call read_messages() to read them.` and marks them told for the wait, and `read_messages` loses its `wait_seconds` parameter. Nested loops' waits never consider messages, since they cannot read them. `send_message(wake=...)` keeps its meaning. The amended [Wake](swarm-communication.md#wake-and-turn-boundaries) section says this.
- **Bridged members' bus tools** (later) give them `swarm_wait` through `swarm_bridged_tools()`. A bridged member never counts as waiting, because the host cannot tell whether one blocked call is all its CLI is doing, as swarm-communication.md already says; its waits end by news, ends, the stop or timeout. Its CLI's MCP timeout must exceed `max_wait`.
- **Persistent members and wake from idle.** An idle member (submitted, held between activations) is not in a tool call, so it is not a waiter. The all-waiting check generalises to quiescence: no member active, idle members counting as not active, and every channel's `quiescent()`. Waking an idle member stays the controller's decision on a `SwarmEvent` ([swarm-communication.md Wake later](swarm-communication.md#wake-and-turn-boundaries)).
- **Push delivery.** An urgent message to a member in `swarm_wait` ends the wait (outcome `waiting` in the urgent-message table), as it ended a `read_messages` wait.

## Alternatives considered

**A `wait` command on `shared_memory`.** Keeps one tool and memory()'s schema with one more command. Rejected: the tool must stay `parallel=True`, so a wait could run beside a long call in the same turn and the all-waiting rule could release waits while a member works; ends are not a board operation; and the later messages channel would need its own wait.

**`view(path, wait_seconds=...)`**, a read that waits, as `read_messages(wait_seconds=...)` was. Returns content in the same call. Rejected for the same reasons, and because a wake may concern many files while a view shows one file or a listing.

**Wake on every commit.** No filter to learn. Rejected: with 32 members writing, a waiter wakes on most writes, which is polling with extra steps.

**A `members` filter**, waking only on named members' ends. Rejected: ends are at most N−1 per run, each is relevant, and a filter-only-on-path waiter whose peer ended without writing would sleep to its timeout.

**Return the changed files' content.** Saves the `view` after a wake. Rejected: a wake can name several files, which need a budget across files and a second kind of fenced, evidenced read; the model should choose what to read.

**Count a subagent's wait as its member waiting.** Lets Epoch-shaped members join the all-waiting release. Rejected for M2: the bus cannot check that a synchronous subagent's ancestors are all blocked in calls that run alone, and background children would need per-task wait tracking in the owner. Waits there end by news, ends, the stop or the timeout.

**Waits on the filesystem channel**, through a bus-owned tool offered without a bus channel. Rejected as the default: it could wake only on ends and timeouts, it would put a peer-state report into `channels=[]` arms meant to have no channel, and the shared memory is M2's board ([Open questions](#open-questions)).

**Edge-triggered waits, or waits on news alone.** See [What wakes a wait](#what-wakes-a-wait): the first misses writes made just before the call, the second returns forever for files the member chose not to view.

**Waits outside working time** (`suspend_working_limit()` or Inspect waiting time). Rejected, as swarm-limits.md did for `read_messages`: sample waiting time collapses across members, and the swarm measures idle time itself.

## Compatibility and migration

- **inspect_ai:** none. `parallel=False` and `ToolDef` are public; the bus already uses `current_agent_channel()` and `current_agent_span_id()`.
- **Eval logs:** only existing types. `waited` is a new comm kind inside the versioned `InfoEvent` payload, `delivered` gains `woke`, and shared-memory `sent` records now carry `wake: true`. No schema or generated-type change. inspect_swarm has not released, so evidence stays `version: 1`.
- **inspect_swarm API (unreleased):** the `swarm_wait` tool (name, parameters, texts), `shared_memory(max_wait=)`, `Channel.max_wait`, `WaitSpec` and `WaitingState`, and `Draft.wake`. `Channel`/`ChannelState` stay experimental ([swarm-implementation-plan.md Decisions](swarm-implementation-plan.md#decisions), item 6). The tool and its texts are evaluated surface: an arm with the shared memory now also has `swarm_wait`, so results from before and after this change are different conditions.
- **Members and tasks:** nothing changes for a swarm without the shared-memory channel. A member definition with `swarm_tools()` gains `swarm_wait` in arms with the shared memory, and no new tool otherwise.

## Security

Untrusted input reaching this code, and how it is handled:

- **`path`** (model output): checked by the shared memory's path grammar or file id grammar and refused otherwise; logged in `waited`, never echoed in a result, never in another member's text.
- **`timeout`** (model output): an integer by the tool schema, clamped to 1 to `max_wait`.
- **What a result contains:** counts, swarm-assigned file ids, roster names validated by the channel's `check()`, and statuses from the runtime's closed set. No content, no peer-chosen path. A wait adds no channel for peer text; it adds one fact members did not have before, each peer's end and status, which `list_members` was always going to show in the messages channel.
- **Identity** comes from the member ContextVar and the agent span, never from arguments, as for every swarm tool.
- **Stalling others.** A member cannot keep another's wait open past its timeout, and cannot make the all-waiting release fire falsely: it fires only when no member is running anything but a wait. Frequent writes under a path others watch wake them repeatedly; the shared memory's per-member `rate` bounds that, as it bounds status-line noise.
- **Resources.** `parallel=False` gives each loop at most one wait at a time, so waiters are bounded by loops; each holds an `anyio.Event` and a small spec.
- **Isolation between samples:** waiters, told marks and wait ids live on the run's bus and channel state.

## Testing

All tests use mockllm with scripted tool calls, run on asyncio and trio, and need no network, Docker or model provider. Scripted members coordinate with test tools that set `anyio.Event`s, so tests do not depend on sleeps; timeout cases use `max_wait=1`.

- **Waking** (`tests/test_wait.py`): a peer's write under `path` ends the wait and the result names the file by id and the writer; a write outside `path`, and the waiter's own write, do not; a write by the member's own subagent does; a file id as `path` follows the file through a rename; a `path` that does not exist yet wakes on its creation; a wait called after an unreported write returns at once, and a second wait does not return for it again; a file named in a status line does not end the next wait; a notice-covered file does not end a wait in the member's own loop, and a wait's report is not repeated by the next notice (with M2-9).
- **Ends**: a member ending wakes every waiter of every other member with its name and status; a loop's first wait after an end it was not told about returns at once; ends are reported once per loop.
- **Releases**: two members waiting with nothing else running are both released with the all-waiting result, as is a lone member; a pending member (a custom controller that never starts it) and an ended one count as not active; a member in a long scripted tool call keeps the others waiting; a subagent's wait does not make its member waiting; a turn calling a slow tool and `swarm_wait` runs them one after the other; the stop (controller return, swarm cap, time cap) releases every wait at once and a wait called after it returns at once; the timeout returns the timeout result with the effective, clamped value.
- **Cancellation**: a member time limit during a wait writes `waited` with `cancelled`, and the next all-waiting check no longer counts that member.
- **Background members** (with whichever of M2-5 and this pull request merges second, in `tests/test_background_members.py`): a background child's wait is released when the member's own agent returns, and a child's later wait returns the drain result at once; a member whose own loop waits while a child runs is not waiting, and becomes waiting (the all-waiting release fires) when the child ends.
- **Tools offered**: `swarm_wait` with `channels=["shared_memory"]` in the member's own loop and in a `general()` child; neither tool with `channels=["filesystem"]` or `channels=[]`.
- **Configuration** (`tests/test_shared_memory_channel.py`): `max_wait` logged and rebuilt, its bounds raise, two arms differing in it get distinct identifiers.
- **Evidence** (`tests/test_comm_evidence.py`): `waited` with each outcome, in the waiter's tool span with `loop` and `nested`, `idle` 0 for nested waits; `delivered.woke` names the waits a write ended; `sent` records of shared-memory writes have `wake: true`; no result contains a file's content or a peer's path, with a peer path chosen to look like an instruction.
- **Metrics** (with M2-10, `tests/test_metrics.py`): `waits`, `waits_by_outcome`, `wait_time` and `idle_time` from a scripted run with known waits, including a member whose subagent waits while its own loop works (wait time counted, idle time not).

## Implementation plan

One pull request, M2-13 in [swarm-implementation-plan.md](swarm-implementation-plan.md#m2-pull-requests), after M2-8 and before M2-11:

1. **M2-13. Waiting for other members.**
   - `src/inspect_swarm/_bus/_wait.py` (new): `WaitSpec`, the waiter registry, the all-waiting check and idle clock, the `swarm_wait` tool and its result templates.
   - `src/inspect_swarm/_bus/_bus.py`: the registry on `Bus`; `wait()`, `member_ended()`, `member_ending()`, `recheck()`; `close()` releasing waiters; step 4 calling `wakes()` and passing `woke` to `delivered`; `Draft.wake` copied to the record.
   - `src/inspect_swarm/_bus/_record.py`: `Draft.wake`.
   - `src/inspect_swarm/_bus/_tools.py`: `swarm_tools()` adds `swarm_wait` when a reachable open channel has `max_wait`.
   - `src/inspect_swarm/_channel/_channel.py`: `Channel.max_wait`, `WaitingState`.
   - `src/inspect_swarm/_channel/shared_memory.py`: `max_wait` with validation; `told`; the status line marking told; `wait_check()`, `unreported()`, `wakes()`, `report()`; `wake=True` on its drafts; the instructions sentence.
   - `src/inspect_swarm/_controller/_control.py`: `_end()` calls `bus.member_ended()`.
   - `src/inspect_swarm/_evidence.py`: `comm_waited()`; `comm_delivered()` gains `woke`.
   - Tests: `tests/test_wait.py`, and the cases above in `tests/test_shared_memory_channel.py`, `tests/test_bus.py` and `tests/test_comm_evidence.py`.
   - With whichever of M2-5 and M2-13 merges second: `MemberContext.background_running` (`src/inspect_swarm/_budget.py`), kept by the owner with `Bus.recheck()` at zero (`src/inspect_swarm/_background.py`), and `Bus.member_ending()` in `_drain()` (`src/inspect_swarm/_member.py`), with the background-member cases.
   - With whichever of M2-10 and M2-13 merges second: the wait metrics in `src/inspect_swarm/_metrics.py` and their tests.
   - Depends on: M2-8 (the shared-memory channel, PR #49). M2-11 documents it.
2. **M2-11** documents `swarm_wait` with the shared memory: what wakes it, that it counts as time, the idle metric, and that filesystem-only swarms have no wait.

## Open questions

1. **Should a swarm without a bus channel have a wait?** The first internal user's swarm was filesystem-only. Recommendation: no, in M2. Such a wait could wake only on peers' ends and its timeout, since sandbox files are unobserved, and it would report peers' states into arms meant to have no channel; a filesystem swarm that wants waits adds `"shared_memory"` and keeps its shared notes on the board, where writes wake waits. The alternative is an opt-in that gives `swarm_wait` (ends and timeout only) to filesystem swarms, for example a `filesystem(wait=True)` parameter backed by a record-less bus state.

## Not this design

Noticed and left out, for Ransom to file if wanted:

- **The filesystem channel's prompt invites polling**: it tells members to read the notes file "before you start and again as you work" (`src/inspect_swarm/_channel/filesystem.py:18`).
- **Counting subagents' waits** towards the all-waiting release, through per-task wait tracking in M2-5's owner, if Epoch-shaped swarms show long timeouts.
- **A switch to turn waiting off** (`shared_memory(max_wait=None)` or similar) for arms that must match a board without a wait, such as a strict Epoch-parity arm; a one-parameter addition when an experiment needs it.
- **Watching sandbox files** (inotify in the sandbox) so filesystem swarms could wake on writes.
- **`longest_identical_tool_run` and repeated waits**: whether the stuck-in-loops signal should skip `swarm_wait` once there is data.
- **The plan does not mark merged M2 pull requests done** (M2-1 #48, M2-2 #44, M2-3 #45, M2-6 #33, M2-7 #42).
