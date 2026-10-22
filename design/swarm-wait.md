# Inspect Swarm: waiting for other members

Status: proposed, 2026-10-21. Issue: none. Author: agent (Claude), reviewed by Codex; see the PR.

## Overview

A member that has done its part and needs a peer's result calls one tool, `swarm_wait`, instead of polling. The call blocks inside the member's tool call and returns as soon as there is something for it to look at: a shared-memory file where it asked to watch (a file, a directory, or the whole board) has a version another agent wrote that the member has not viewed, or another member has ended since the member's last wait said so. It returns at once if that is already the case. It also returns when nothing more can happen (every member is waiting, has ended or has not started), when the swarm begins to stop, or after a timeout the channel bounds. The result names changed files by id and ended members by name and status; it never contains file content, which the member reads with an ordinary, fenced `view`. The wait only wakes: viewing a file is what acknowledges it, so a wait keeps returning while a file under its path stays unviewed.

The wait belongs to the swarm's bus, not to one channel: the bus keeps the waiters, the release rules and the evidence, and a channel only says whether something is waiting for the loop to view. The shared-memory channel is the first channel that wakes waits, so `swarm_wait` comes with it; the later messages channel plugs in the same way instead of keeping a wait of its own in `read_messages`. Each wait writes one evidence event saying how long it waited and what ended it, from which the metrics report waits, wait time and idle time per member.

## Background

This is D-3 of [swarm-implementation-plan.md](swarm-implementation-plan.md#designs). The first internal user's feedback (2026-10-21) was that members that finished their own part early polled the board with `sleep 60; cat /shared/notes.md`, spending a tool turn on every check. Ransom's decision (2026-10-21): "I think it will be common for members to wait on other members if they are working together." So waiting is a common, first-class case in M2, not something deferred to the messages channel or to persistent members.

It builds on the bus as merged (M2-6, #33; M2-7, #42) and on the shared-memory channel ([swarm-memory-channel.md](swarm-memory-channel.md), D-1; implemented in #49), and amends both designs and the Wake section of [swarm-communication.md](swarm-communication.md#wake-and-turn-boundaries) where it changes them.

Code references are to inspect_swarm `main` at `31e0481`, which includes the shared-memory channel (M2-8, #49) and background members (M2-5, #50), and inspect_ai `main` at `e3310b753`, which this worktree's lock installs. Paths are relative to each repository's root. Claims marked *(spike)* were checked by running code on both anyio backends.

## Why

Polling costs what M2 measures. Each `sleep 60; cat` is a model call over the member's whole context, so it inflates the member's tokens and cost, its step count, the swarm's `longest_member_steps` and, for the member whose answer is chosen, `steps_to_answer` (M1-11 counts completed model calls, `src/inspect_swarm/_metrics.py:160-170`). Repeated identical polls also look like the stuck-in-loops signal (`longest_identical_tool_run`, `_metrics.py:22-26`). A wait inside one tool call costs one step for the whole wait.

Nothing in M2 lets a member wait:

- The shared-memory channel never blocks: a write record has `wake=False` ([swarm-memory-channel.md The write record](swarm-memory-channel.md#the-write-record)), and every command runs to completion (its tool awaits only `Bus.deliver()`, which itself never awaits).
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
- Changing notices or the status line. The wait reads D-1's `seen` and marks nothing; notices and status lines keep D-1's behaviour.

## Current behaviour

### The bus and member states

- **Member states** are `pending`, `running` and `ended`. `SwarmRun.start()` (the implementation of `SwarmControl.start()`) sets `running` (`src/inspect_swarm/_controller/_control.py:311`); the runtime's `_end()` sets `ended` and the status, writes `member_result` evidence and queues `MemberEnded`, all synchronously (`_control.py:539-561`). The bus holds the same member objects (`src/inspect_swarm/_bus/_bus.py:48-49`), so it reads their states directly; nothing tells the bus when a member ends.
- **Stopping.** `stop(reason)` calls `self.bus.close()` first, synchronously (`_control.py:470-476`); `close()` only sets the stopping flag that refuses later records (`_bus.py:91-93`). Then levers close, grace runs, and the hard stop cancels each running member's scope (`_control.py:580-591`).
- **Delivery.** `Bus.deliver()` runs steps 0 and 2 to 4 with no `await`; step 4 applies the record and writes `delivered` (`_bus.py:94-151`). Every record is built with `wake=False` (`_bus.py:118`); `Draft` has no `wake` field (`src/inspect_swarm/_bus/_record.py:69-75`).
- **Loops.** `swarm_tools()` binds the member to the first agent channel it is resolved in (`context.loop`) and gives other loops only `"member"`-reach channels' tools (`src/inspect_swarm/_bus/_tools.py:11-29`). `Bus.nested(context)` is true in any loop but the member's own, and `Bus.running(name)` says whether a member has started and not ended (`_bus.py:153-160`).
- **Who can be a member beside a bus channel.** `swarm()` refuses a bridged member when any channel has records (`_check_bus_channels`, `src/inspect_swarm/_swarm.py:272-307`). A bridged agent the rule misses runs without the channel's tools. Background members are accepted since M2-5.

### The shared-memory channel (M2-8)

- **News.** A file is news to a loop when its current version was written by another loop and is above the version that loop has seen; a tombstone only when the loop saw it or was notified of it before (`_is_news`, `src/inspect_swarm/_channel/shared_memory.py:846-858`). The member's own loop also leaves out what a notice covered (`_news`, `:860-867`).
- **The status line** ends every `shared_memory` result and names, by id, up to ten files that are news to the calling loop (`_status_line`, `:884-888`, appended at `:482-483`). It reports and does not mark anything; a file stays in it until the loop views it ([swarm-memory-channel.md Notices](swarm-memory-channel.md#notices-and-the-status-line)).
- **The tool** is built with `parallel=True` and `max_output=0` (`:430-437`), as memory() is parallel.

### Background members (M2-5)

- Each member's background work runs in an owner, `MemberBackground`, which counts `running` tasks (`src/inspect_swarm/_background.py:41`, `:99`, `:123`) and holds the member's `MemberContext`.
- When the member's own agent returns, `_drain()` closes the lever with `member_ended`, waits up to the effective grace for the owner's tasks, then cancels what is left (`src/inspect_swarm/_member.py:560-566`). The member is `running` throughout the drain and ends after it.

### Tool calls in one turn

inspect_ai splits a turn's tool calls into stages: consecutive calls to tools with `parallel=True` run together, and each call to a tool with `parallel=False` is a stage of its own (`src/inspect_ai/model/_call_tools.py:461-487`). *(spike: a turn calling `slow`, `swarm_wait`, `slow`, where only `swarm_wait` had `parallel=False`, ran them one after another on asyncio and trio: 0.0–0.3 s, 0.3–0.4 s, 0.4–0.7 s.)* So while a call to a non-parallel tool runs, its loop runs nothing else.

### Time

[swarm-limits.md](swarm-limits.md#interactions) already decides, for the messages channel's wait, that waiting is wall-clock time inside a tool call: it counts towards the member's time limit, the swarm's time cap, the sample's time limit and the sample's working time, and a stop releases every wait. Plan decision 12 keeps idle time as "the time members spend in the bus's read waits" and drops per-member waiting time from M1 because a member's subagents and parallel calls make a plain sum of waits misleading ([swarm-implementation-plan.md Decisions](swarm-implementation-plan.md#decisions), item 12).

### Who waits today

- **The first internal user** ran a leaderless swarm over the shared sandbox with the `filesystem` channel, whose instructions tell members to read `/shared/notes.md` "before you start and again as you work" (`src/inspect_swarm/_channel/filesystem.py:19-32`). Their members polled it with `sleep 60`.
- **Epoch's LeanOpenProblems swarm** (the M2 port) shares a board through `memory()`, with background `general()` children doing most of the work ([swarm-lean-open-problems.md Their swarm](swarm-lean-open-problems.md#their-swarm)). Their members have no wait either.
- **Nothing else.** No tool in this repository, its tests or the merged designs waits on a peer outside the later messages channel.

So the shapes this design serves are: native `react()` and `deepagent()` members and their `general()` children (synchronous and background), waiting for board changes and for peers to end, in swarms with the shared-memory channel, over a shared sandbox or per-member sandboxes.

## Design

### At a glance

```
 member worker-1                         member worker-2
 swarm_wait(path="/shared_memory/results")    shared_memory(create, /shared_memory/results/lemma.md)
   │ register waiter x-3                               │
   │ (returns at once if something is unviewed)        ▼
   │                                     Bus.deliver(): step 4 apply ── waiters whose filter the
   │◀──── event set ──────────────────────────────────── write matches (x-3) ── waited.woken_by
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

> Wait until there is something for you to look at in the swarm: a shared memory file where you watch has a change you have not viewed (another agent's write, including your own subagents'), or another member has ended. Returns at once if that is already so, and keeps returning while a changed file there stays unviewed, so view what it names, or narrow `path`. It also returns when every member is waiting, has ended or has not started, when the swarm is stopping, or after `timeout` seconds. It returns file ids and member names, never file content. Use it instead of polling with sleep.

- `path`: "A shared memory file or directory to watch, such as `/shared_memory/results`, or a file id such as `f-3`. Without it, any change to shared memory wakes you." The shared-memory channel checks it with its path grammar (or the file id grammar) and refuses anything else with the grammar's `ToolError`. The path need not exist yet, so a member can wait for a file to be created. A file is under `path` when its current path (a tombstone's: the path it had) is `path` or starts with `path + "/"`, or when its id is `path`.
- `timeout`: "Seconds to wait at most (default and maximum: 600)." `None` means `max_wait`; any value is clamped to 1 to `max_wait` (default 600). The result states the effective value when the wait times out, so a clamp is visible to the model.

**Why a separate tool, not a `wait` command on `shared_memory`.** swarm-communication.md rejected a separate `wait_for_messages` tool as "one more tool the model must learn" when `read_messages(wait_seconds=...)` could carry the wait. Three things change that here:

- **Ends are not a board operation.** A member waits as often for a peer to finish as for a file, and a peer's end is the bus's knowledge, not the board's.
- **One wait for every channel.** With the shared memory and, later, messages both open, a member waiting for "a reply or a result on the board" needs one call that either wakes. A wait inside `read_messages` could not wake on a board write, and a wait inside `shared_memory` could not wake on a message. So the one extra tool is learned once and serves every channel, and the messages channel drops its own wait ([Room for later work](#room-for-later-work)).
- **The all-waiting rule needs to know the loop is idle.** `shared_memory` must stay `parallel=True`, as memory() is, so a `view` and a `bash` run together. A wait inside it could run beside a long `bash` call in the same turn, and the member would look idle while it builds. A separate tool with `parallel=False` runs alone in its loop's turn ([Tool calls in one turn](#tool-calls-in-one-turn)), so a loop in a wait is doing nothing else.

### What wakes a wait

**The wait only wakes; viewing a file is the only acknowledgment** (decision: Ransom, 2026-10-21, after review round 3). A wait is level-triggered on two things, for the waiting loop:

1. **an unviewed change under `path`**: a file under `path` whose current version was written by another loop and is above the version this loop has **seen** (or, when the current version is the loop's own blind append, whose latest version by another loop is), where `seen` is D-1's per-loop mark, set only by a `view` of that version or by the loop's own write (an append only when the loop had seen the version it extended; #66, decision: Ransom, 2026-10-22). A blind append to a file the loop had never seen sets `seen` 0, which marks the file as known without acknowledging any version of it. A tombstone counts if the loop has viewed or written the file, including a blind append that records `seen` 0. A file created and deleted entirely by peers before the loop knew it stays hidden. Nothing else gates it: the status line, notices and earlier wait results mark nothing for the wait;
2. **an unreported end**: a member other than the waiter's own that has ended and that no earlier wait result of this loop named. The per-loop set of reported ends is the only mark the wait keeps.

If either holds when the wait is called, it returns at once. Otherwise the bus registers the waiter and returns it when one becomes true. One predicate in the channel, `reportable(spec, file)`, decides both whether a commit wakes a waiter and what a wait reports ([Channels that wake waits](#channels-that-wake-waits)).

**The cost, stated plainly.** While an unviewed file is under `path`, every wait on that `path` returns at once, every time: the "polling again" case an earlier revision of this design avoided with a separate "told" mark. In the normal flow it does not arise: a member waits, is told `f-7` changed, views `f-7`, and its next wait sleeps. A member that does not want a file narrows `path` to leave it out. This is a deliberate choice of one always-correct behaviour over the extra return, in line with Ransom's preference for the simpler protocol: a mark that the wait itself set, shared with status lines and notices, was the source of every blocking review finding in rounds 1 to 3, and none of them came from waking.

Why level-triggered on `seen`, not on the alternatives:

- **Edge-triggered** (only changes after the wait starts) loses a write that lands between the member's last tool result and its wait call, and the member then sleeps to the timeout with the result already on the board.
- **A separate acknowledgment mark** (a version is "told" once a wait or status line names it) stops the extra returns, but the mark must then be committed only when the result reaches the model, kept per filter, and kept apart from status lines and notices; each of those was a correctness bug in review. `seen` is set only by what actually put the content in the loop's context.

**Board changes, not every commit.** With many members writing, waking on every commit would be polling again; `path` narrows it, and a write by the waiting loop itself never wakes it. A write by the member's own subagent does, because the waiting loop has not seen it, as the status line and notices also report it ([swarm-memory-channel.md Notices](swarm-memory-channel.md#notices-and-the-status-line)).

**Every end wakes every waiter.** A member's end is reported to every loop of every other member, once, whatever its `path`. There is no `members` filter. Ends are rare (at most one per member per run, so at most N−1 extra returns per loop), each one matters to collaborators (the peer's work is final, and there is one fewer peer to wait for), and waking only waiters that named the member would need a second filter and would leave a waiter whose peer ended without writing to sleep to its timeout.

**Status lines and notices keep D-1's behaviour.** The wait reads `seen` and nothing else of the channel's marks, and sets none of them; D-1's `covered`, its tombstone rule and its status line are unchanged.

### What a wait returns

The result is fixed-template lines with counts, swarm-assigned file ids, roster names and statuses. It never contains file content, a path a peer chose, or the waiter's own `path` (the waiter knows it).

| Ended by | Result |
|---|---|
| Unviewed change | `2 shared memory files changed where you waited (f-3, f-9 new; by worker-2, worker-4). View one with shared_memory(command="view", path="f-3").` |
| Unreported end | `worker-2 ended (submitted). worker-4 ended (member_limit:cost).` |
| No member active | `Nothing can change: every member is waiting, has ended or has not started.` |
| Stop | `The swarm is stopping.` |
| The member's drain | `worker-1 is ending: its own agent has finished.` |
| Timeout | `Nothing you waited for happened in 600 s.` |

- **Recomputed at resume; nothing captured.** A release only sets the waiter's event and reason. When the waiter's task resumes, the bus asks the board again what is reportable under `path` and which ends are unreported. A release for news whose news has gone by then (the file was renamed out of `path`, or created and deleted before the loop ever saw it) finds nothing, and the waiter goes back to sleep for the rest of its timeout, registered again with the same id. Other releases (no member active, stop, drain, timeout) return at once.
- **What it names.** Changes under `path` (with `new` and `deleted` markers, up to ten ids then "and N more", as notices list them) and every unreported end, each kind on its line, then the release line for a release other than news (a stop that arrives after a write gives both). Naming a file marks nothing: files beyond the first ten, and every named file the loop does not view, are named again by its next wait, at once. Naming an end adds it to the loop's reported ends when the tool body returns.
- **No status line.** Unlike a `shared_memory` result, a wait result does not end with the shared memory's status line: it names the news under `path` itself, and files elsewhere on the board reach the loop through the status line of its next `shared_memory` call and through notices, with D-1's rules, unchanged. The status line is the shared-memory tool's own text (its formatter is private to that tool, `shared_memory.py:884-888`), and the bus asks a channel only for `news()` under the wait's filter, which returns `None` exactly when no matching changes are currently reportable, regardless of the release reason; giving the wait a status line would need a second read interface on `WaitingState` for one line of convenience.
- **Bounded and never truncated.** The tool is built with `max_output=0`, as `shared_memory` is, so neither the generate config's `max_tool_output` nor Inspect's 16 KiB default can cut a result (`src/inspect_ai/model/_call_tools.py:1537-1587`). The result is bounded instead by its fixed templates: a changed-files line names at most ten swarm-assigned ids with markers, a count and roster names as writers; an ends line names roster members and statuses from the runtime's closed set; there is no content and no peer path. Tests keep that bound: the shared memory's tests check that its changed-files line stays within a fixed limit for 48-byte names, and `tests/test_wait.py` checks a whole result against a fixed limit. The bus computes no wait budget at run time, since nothing would read it (decision: Ransom, 2026-10-21, after review of #62; an earlier revision had the bus compute one, which needed each channel to report its longest line).
- **What a lost result costs.** Inspect can still discard a result after the tool body returns: an operator interrupt during result review or delivery replaces it (`src/inspect_ai/model/_call_tools.py:424-453`, `:606-625`). Because naming a file marks nothing, a lost result loses no file news: the next wait names the same unviewed files. The only mark is the reported ends, so such a cancellation costs at most the notice of the ends that result named; the loop learns of those members' ends from nothing else of the wait. That is accepted, since it needs an operator interrupt in a window of a few synchronous steps, and the members' states are unchanged by it.
- **What a scheduling gap costs.** A waiter's own task decides how its wait ended, when it resumes, in its synchronous `finally`. The bus does not look at the waiter's timeout, the deadlines around it or its cancel scopes. So for one scheduling step, between a waiter's timeout or cancellation firing and its task resuming, the bus may still count it as waiting in the all-waiting check, or release it for news: a peer may then return `all_waiting` early, and the waiter's `outcome` or `woken_by` may name that release rather than the timeout. That is accepted: a gap of one scheduling step costs at most an early return or a mislabelled outcome, never lost news (nothing is marked until a result is built, and the next wait names what is unviewed) or a deadlock (every wait still ends by its own timeout) (decision: Ransom, 2026-10-21, after review round 3 of #62). Two review rounds of making the bus track this exactly (snapshots of enclosing deadlines, cancel-scope checks, idle intervals capped at deadlines) each found another gap.
- **What this settles from review round 3.** (B1) A result lost after the tool body returns marked no file, so nothing it named is lost; only reported ends are marked, at the cost above. (B2) Nothing is captured at release, so no captured item has to keep the filter match that caused it: a file renamed out of `path` or deleted unseen before resume simply is not reported, and the waiter sleeps again. (B3) No "told" mark exists and status lines and notices set nothing the wait reads, so a filtered wait returns for an unviewed file under its path even when a status line or notice already named the id.
- Why no content: a view is fenced, evidenced as `read` and bounded by the read budget for one file; a wait may report many files, and the model chooses which to read. Returning content would need a budget across files and a second kind of read. One `view` after a wake is the cost, and it is the acknowledgment.

### When a wait ends without news

- **No member active.** A running member is **waiting** when its own loop (the bound loop, `not bus.nested(context)`) is in `swarm_wait`, its waiter is not released, and none of its background work is running (`context.background_running == 0`). A member is **active** when it is running and not waiting. Pending and ended members are not active. When no member is active and some wait is registered, every waiter not yet released is released at once with the all-waiting result, and a wait called in that state returns at once with it. The bus checks this synchronously when a waiter registers or goes back to sleep, when a member ends, and when a member's background work drops to none ([Background members](#background-members-and-descendants)). A waiter stops counting as waiting the moment the bus releases it, not when its task resumes, so a check that runs in between cannot count a member that is about to act. A waiter whose timeout or cancellation has fired but whose task has not resumed still counts as waiting for one scheduling step ([What a scheduling gap costs](#what-a-wait-returns)). A member the controller starts later can still change the board; a released member sees it at its next wait or status line.
- **Stop.** `bus.close()`, the first act of `stop(reason)`, releases every waiter with the stop result, and a wait called afterwards returns it at once. So a waiting member does not spend the stop's grace blocked in a tool, and its next model call meets the closed lever ([swarm-limits.md Stopping](swarm-limits.md#stopping), step 0).
- **The member's drain.** When the member's own agent returns, the drain releases that member's waiters with the drain result and refuses its loops' later waits the same way, at once: the drain lets calls in flight finish, and a wait for peers serves nothing once the member's attempt is over. The background child's next model call then meets the lever closed with `member_ended`.
- **Timeout.** After the effective timeout, counted from the call, across any returns to sleep.
- **Cancellation.** A hard stop, the member's time limit or a cancellation from outside cancels the wait's task. The waiter is removed and its evidence written in a synchronous `finally`; nothing is marked, and the next all-waiting check sees one waiter fewer. A cancellation that is pending when the wait would return, whether it came before the call or after a release, ends the wait as `cancelled`: the wait checks for cancellation on every return, before it names anything, so it marks no ends for a result Inspect would discard.
- **Refusals.** A wait from a member that is not running returns `ToolError("worker-1 has ended.")`, as `shared_memory` does. A second wait from the member's own loop while another is in progress (from its acceptance to its return, including a wait that found news and has not registered) returns `ToolError("worker-1 is already waiting: its own loop has a swarm_wait in progress.")`, so the idle clock counts one wait at a time ([Metrics](#metrics)). A model's calls cannot meet it, since `parallel=False` runs a loop's waits one after another; only an agent that calls the tool from several tasks of its own loop can. Waits of different nested loops, and a nested loop's wait beside the own loop's, are not refused. A wait makes no model call, so limits checked only at model dispatch (tokens, cost, turns, messages, the lever) do not fire inside it; the member's time limit can cancel it, as above.

**Why this cannot deadlock.** A wait ends by news, by no member being active, by the stop or the drain, or by its timeout, which is at most `max_wait` from the call. Two members each waiting for the other's file are both waiting, so neither is active; if every other member is also waiting, ended or pending, both are released at once, and otherwise someone active can still write. The time cap, the sample's time limit and the hard stop end everything else. The registration, the check for news and the all-waiting check run with no `await` between them, so a write or an end cannot fall between "nothing to report" and "registered"; the same holds when a waiter goes back to sleep.

### Background members and descendants

**A waiting subagent does not make its member waiting.** Only the member's own loop's wait counts, and only while none of its background work runs. The rule must never release waits while someone is working: a released waiter that waits again while everyone is still waiting is released again, so a false release would turn waits back into polling. Each case:

- **A synchronous subagent** runs inside a tool call of the member's own loop, so the member's own loop is not in a wait. Its wait means the member is idle only if every loop between it and the member's own loop is blocked in a call that runs alone, which the bus cannot check. deepagent happens to make its subagent tool serial when a subagent holds a tool source such as `swarm_tools()` (`src/inspect_ai/agent/_deepagent/agent_tool.py:329`, `:1015-1050`), but a nested agent run through another tool can share a stage with other work. Not counting it costs only that the other members' waits end by news, an end or the timeout rather than by the all-waiting release.
- **Background children** run in the member's owner beside its own loop. The member's own loop waiting while a child runs is not idle. A child waiting while its siblings or the member's own loop work is not idle either. Counting a child's wait would need the owner to say which of its tasks are blocked in a wait with no nested work, which it does not track.
- So with background work running, a member is active. In Epoch's shape (children working, the top loop in `agent_wait` on them) the all-waiting release does not apply, and waits end by news, ends, the stop or the drain, or their timeouts. That is the price of a rule that never releases wrongly; [Not this design](#not-this-design) lists the refinement.

Descendants still wait for themselves: a `general()` child, synchronous or background, gets `swarm_wait` through the shared memory's `"member"` reach, its waits watch the board against its own `seen` (per loop) and its own reported ends, and every release except the all-waiting one reaches it.

**Background counts and the drain.** `MemberContext` (`src/inspect_swarm/_budget.py:303-320`) gains `background_running: int`, kept by the owner beside its own `running` count (`_background.py:41`, raised at `:99` and lowered at `:123`); when it drops to 0 the owner calls `Bus.recheck()` on the bus of the member's run, which it takes from `current_bus` when the owner is created (work the member starts may run with another bus, as when a nested swarm's controller starts it). `recheck()` runs the all-waiting check, in which the member now counts as waiting if its own loop's waiter is registered and not released. When it rises from 0 the owner calls `Bus.recheck()` too, which stops the idle clock of a member whose own loop is waiting: a member's own agent can run other tasks beside its loop, and one of them can start background work while the loop waits. `_drain()` calls `Bus.member_ending(name)` right after it closes the lever. `MemberContext` today has only `background: bool`, set when the member starts background work, which cannot tell a running child from a finished one.

### The filesystem channel and per-member sandboxes

- **Filesystem only** (`channels=["filesystem"]`, M1's default): no `swarm_wait`. The filesystem channel is instructions-only and observed: no swarm code sees a file written in the sandbox, so a wait could not wake on the notes file, only on peers' ends and its timeout. And `swarm_tools()` gives no tools without a bus channel, which keeps a `channels=[]` arm free of anything that reports peers' states. A filesystem swarm that wants waits adds the shared-memory channel and keeps its notes on the board (decision: Ransom, 2026-10-21: "Q1 agree with recommendation").
- **Filesystem and shared memory together** over a shared sandbox: `swarm_wait` wakes on board writes and ends, never on sandbox files. The tool's description and the shared memory's instructions say what it watches.
- **Per-member sandboxes** take the filesystem channel only as `filesystem(shared_volume=True)`, describing one volume every member's sandbox mounts (#54; `_check_member_sandboxes`, `src/inspect_swarm/_swarm.py:312-332`). That channel is instructions-only too, so the rules above apply unchanged: with `"shared_memory"` their members get the full wait, which never watches the volume; with `filesystem(shared_volume=True)` alone or `channels=[]` (both exercised by `tests/test_member_sandboxes.py`) they get no wait.

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
| `started`, `duration` | swarm time of the call, and seconds until it returned (0 for an immediate return), returns to sleep included |
| `idle` | seconds of the wait during which its member counted as waiting ([The idle clock](#metrics)); 0 for a nested loop's wait |
| `outcome` | `news`, `all_waiting`, `stopping`, `member_ending`, `timeout` or `cancelled` |
| `woken_by` | the commit sequence numbers (`delivered.seq`) of the commits that released it for news, in order, including any whose news was gone at resume; `[]` when none did |
| `files` | `[{"file", "version", "deleted"}]` the result named |
| `ended` | `[{"member", "status"}]` the result named |

`delivered` is unchanged. A wait's `woken_by` joins it to the commits that ended it through `delivered.seq`; with `files` (ids and versions), the later dependency-linked critical path can tell that a member's idle time ended at a peer's commit, which the `read` that usually follows then links by content.

### Metrics

Not in M2-13: the wait metrics land in their own pull request after it (decision: Ransom, 2026-10-21, after review round 3 of #62). M2-13 writes the `waited` events they are counted from; its ledger event log skips them, since they have `channels` rather than one `channel`. That pull request also adds the idle clock and the `waited` event's `idle` field (seconds of the wait during which the member counted as waiting, only own-loop waits having any), keeps `waited` events apart in the event log for the metrics, and gives `None` rather than 0 in a swarm that offers no wait, following M2-10's rule that 0 means "available and unused" (accepted: Ransom, 2026-10-21). Per member:

- `waits`: the number of `waited` events in the member's spans, and `waits_by_outcome`.
- `wait_time`: the sum of their durations, across all the member's loops. Concurrent waits of different loops each count, so it can exceed the member's running time; it measures how much waiting the member's loops did.
- `idle_time`: the sum of `idle`, the time the member as a whole did nothing but wait for peers (its own loop in `swarm_wait`, its waiter not released, no background work). This is swarm.md's idle time, made exact for members with subagents, which plan decision 12 asked for: it is not a sum of waits that ignores work running beside them.

**The idle clock.** Each wait's `idle` comes from a clock that runs exactly while the wait makes its member waiting ([No member active](#when-a-wait-ends-without-news)). The bus restarts or stops it at every change that rule reads, and nowhere else:

1. Only a wait of the member's own loop has a clock; a nested loop's wait has `idle` 0. The own loop has at most one wait in progress at a time: the bus refuses a second ([Refusals](#when-a-wait-ends-without-news)).
2. The clock starts when the wait registers or goes back to sleep with none of the member's background work running, or when that work drops to none during the wait.
3. It stops at the first of: a release by the bus (news, an end, all waiting, the stop, the drain), at the release and not when the task resumes; the member's background work rising from none; the wait's own timeout or cancellation. The bus sees a timeout or cancellation only when the task resumes, so the clock can run one scheduling step past it ([What a scheduling gap costs](#what-a-wait-returns)).
4. It starts again when a released wait finds its news gone and sleeps again. The time between the release and the return to sleep is not idle.
5. `idle` is the sum of these intervals, so it never exceeds the wait's `duration`, and a member's `idle_time` never exceeds its running time.

Neither is Inspect's working time, and neither replaces per-member working time (still future work). `longest_identical_tool_run` is unchanged: a member that calls `swarm_wait` with the same arguments several times in a row has a run of identical calls, and the metric alone does not distinguish these runs from a suspected loop; analysis must inspect the logged tool calls to identify waits. Changing the metric stays deferred ([Not this design](#not-this-design)).

### Channels that wake waits

```python
# src/inspect_swarm/_channel/_channel.py
class Channel:
    ...
    max_wait: int | None = None
    """This channel's contribution to the longest `swarm_wait`, in seconds, or `None` for a channel whose records wake no waits. The bus's effective maximum is the largest `max_wait` of the run's open channels that wake waits."""

@dataclass(frozen=True)
class WaitSpec:
    id: str              # "x-4"
    member: str
    loop: str | None     # the waiting loop's agent span
    own: bool            # the member's own loop
    path: str | None     # the shared memory's filter; later channels add theirs

@dataclass(frozen=True)
class WaitNews:
    lines: list[str]                      # the channel's lines for the result
    files: list[dict[str, JsonValue]]     # what `lines` name, for `waited.files`

class WaitingState(ChannelState, Protocol):
    def wait_check(self, spec: WaitSpec) -> None: ...                # validate the filter; raise ToolError
    def wakes(self, spec: WaitSpec, record: Record) -> bool: ...      # did this applied record make something reportable
    def news(self, spec: WaitSpec) -> WaitNews | None: ...            # what is reportable now; marks nothing
```

- A channel whose state wakes waits sets `max_wait` from its factory (like `storm`) and its state implements `WaitingState`; the bus refuses, with a `TypeError` when the run opens, a channel that sets `max_wait` but whose `open()` returns no such state, `None` included. The bus offers `swarm_wait` when some open channel has `max_wait`, clamps every wait to the effective maximum (the largest such `max_wait`), and asks only those states. A channel's `max_wait` is therefore a contribution, not a bound it enforces alone: with the shared memory at 600 and the later messages channel at 30, a wait may last 600 s.
- **Records that can wake** set `wake=True` (`Draft` gains `wake: bool = False`, which the bus copies to the record). Shared-memory writes always set it: every write may end a wait whose filter it matches, and the writer has no choice, as no write has a parameter for it. This amends D-1's `wake=False`.
- `wakes()` and `news()` apply the same `reportable(spec, file)` predicate ([What wakes a wait](#what-wakes-a-wait)). `wakes()` is called at step 4, after `apply()`, for each registered, unreleased waiter, with no `await`, and looks only at the files the record changed, so its cost is per write, not per board; `news()` scans the board, at the call and at resume. Neither marks anything, so the bus needs no acknowledgment step: it writes `waited.files` from `news().files` without parsing text or reading the channel's state.

### The bus

```python
# src/inspect_swarm/_bus/_wait.py (new), used by Bus
class Bus:
    async def wait(self, path: str | None, timeout: int | None) -> str: ...   # the tool's body
    def member_ended(self, name: str, status: str) -> None: ...   # from SwarmRun._end()
    def member_ending(self, name: str) -> None: ...               # from the drain
    def recheck(self) -> None: ...                                # from the owner
    def close(self) -> None: ...                                  # now also releases every waiter
```

The bus keeps the registered waiters and, per loop, the set of ends it has reported (`reported_ends: dict[loop span, set[str]]`), and nothing else for waits.

`wait()`, in the calling member's context:

1. Refuse if the member is not running.
2. Build the `WaitSpec` (`own` from `not bus.nested(context)`), clamp the timeout to the effective maximum, fix the deadline, and call `wait_check()` on each waiting state. If the bus is closed or the member is draining, go to step 6 at once. The filter is checked first because that result also names the news under `path`, so a malformed `path` during a stop gets the grammar's `ToolError`.
3. If any state's `news()` is not `None` or an end is unreported to this loop, go to step 6.
4. Register the waiter (an `anyio.Event`, its spec, its release reason and the `woken_by` list). If `own` and the member has no background work, the member is waiting. Run the all-waiting check, which may release it at once.
5. Await the event until the deadline. Steps 1 to 4 have no `await`.
6. Pass one scheduling checkpoint, on every path, the immediate returns of steps 2 and 3 included. It delivers a pending cancellation, an AnyIO one or the task's own (asyncio's `Task.cancel()`), before the wait names anything. Then, if the wait was released for news (or returned at once for it) and no state has `news()` and no end is unreported, the news is gone: go back to step 4 with the same spec, unless the deadline has passed, in which case the wait ends by its timeout. News that keeps vanishing so cannot keep a wait past its timeout.
7. Build the result from `news()`, the unreported ends and the release line; add the named ends to the loop's reported ends; write `waited`; return. On cancellation instead: write `waited` with `cancelled` and nothing named, and re-raise. In both cases the waiter is unregistered in a synchronous `finally`.

Release sources run synchronously. Each sets the waiter's event and reason (by the precedence below); a released waiter no longer makes its member waiting:

- `deliver()` step 4: for every registered waiter not yet released, `wakes()`; if true, it is released with `news` and the record's `seq` appended to its `woken_by`.
- `member_ended()`, called by `SwarmRun._end()` right after it marks the member ended: releases every not-yet-released waiter of another member with `news`, then runs the all-waiting check.
- `recheck()` runs the all-waiting check ([Background members](#background-members-and-descendants)), which releases only waiters not yet released, with `all_waiting`.
- `member_ending()` (the member's waiters) and `close()` (every waiter), released or not.

**Precedence of the outcome.** Apart from cancellation, `stopping` and `member_ending` are terminal and nothing replaces them: `close()` sets `stopping` on every waiter, `member_ending()` sets `member_ending` on its member's waiters unless they already have `stopping`, and the other sources (`news`, `all_waiting`) touch only unreleased waiters. At step 7 the outcome is `stopping` if the bus is closed, else `member_ending` if the member is draining, else the waiter's own reason (or `timeout`). The result still names the current `news()` and the unreported ends beside the stop or drain line, so a write, then a stop, then a peer's end, all before the waiter resumes, gives the changed-file line, the ends line and the stop line, with outcome `stopping`.

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

- **The messages channel** contributes its `max_wait` (its designed 120) to the effective maximum and implements `WaitingState` with `"loop"` reach: the member's own loop's wait is reportable while it has unread `wake=True` messages, and `news()` gives `3 unread messages (from worker-2, worker-4). Call read_messages() to read them.`; reading is the acknowledgment, as viewing is for files, and `read_messages` loses its `wait_seconds` parameter. Nested loops' waits never consider messages, since they cannot read them. `send_message(wake=...)` keeps its meaning. The amended [Wake](swarm-communication.md#wake-and-turn-boundaries) section says this.
- **Bridged members' bus tools** (later) give them `swarm_wait` through `swarm_bridged_tools()`. A bridged member never counts as waiting, because the host cannot tell whether one blocked call is all its CLI is doing, as swarm-communication.md already says; its waits end by news, ends, the stop or timeout. Its CLI's MCP timeout must exceed the bus's effective maximum wait.
- **Persistent members and wake from idle.** An idle member (submitted, held between activations) is not in a tool call, so it is not a waiter. The all-waiting check generalises to quiescence: no member active, idle members counting as not active, and every channel's `quiescent()`. Waking an idle member stays the controller's decision on a `SwarmEvent` ([swarm-communication.md Wake later](swarm-communication.md#wake-and-turn-boundaries)).
- **Push delivery.** An urgent message to a member in `swarm_wait` ends the wait (outcome `waiting` in the urgent-message table), as it ended a `read_messages` wait.

## Alternatives considered

**A `wait` command on `shared_memory`.** Keeps one tool and memory()'s schema with one more command. Rejected: the tool must stay `parallel=True`, so a wait could run beside a long call in the same turn and the all-waiting rule could release waits while a member works; ends are not a board operation; and the later messages channel would need its own wait.

**`view(path, wait_seconds=...)`**, a read that waits, as `read_messages(wait_seconds=...)` was. Returns content in the same call. Rejected for the same reasons, and because a wake may concern many files while a view shows one file or a listing.

**Wake on every commit.** No filter to learn. Rejected: with 32 members writing, a waiter wakes on most writes, which is polling with extra steps.

**A `members` filter**, waking only on named members' ends. Rejected: ends are at most N−1 per run, each is relevant, and a filter-only-on-path waiter whose peer ended without writing would sleep to its timeout.

**Return the changed files' content.** Saves the `view` after a wake. Rejected: a wake can name several files, which need a budget across files and a second kind of fenced, evidenced read; the model should choose what to read.

**Count a subagent's wait as its member waiting.** Lets Epoch-shaped members join the all-waiting release. Rejected for M2: the bus cannot check that a synchronous subagent's ancestors are all blocked in calls that run alone, and background children would need per-task wait tracking in the owner. Waits there end by news, ends, the stop or the timeout.

**Waits on the filesystem channel**, through a bus-owned tool offered without a bus channel. Rejected (decision: Ransom, 2026-10-21): it could wake only on ends and timeouts, it would put a peer-state report into `channels=[]` arms meant to have no channel, and the shared memory is M2's board.

**Edge-triggered waits.** See [What wakes a wait](#what-wakes-a-wait): they miss writes made just before the call.

**A wait that acknowledges what it names** (this design's revisions of rounds 1 to 3: a per-loop "told" mark set by wait results and status lines, own-loop `covered` updates, a capture at release and a per-loop backlog of captured items). It spares the extra returns while an unviewed file is under `path`. Rejected (decision: Ransom, 2026-10-21): every blocking finding of three review rounds came from it, not from waking. A mark set when the tool body returns is lost or wrong when Inspect discards the result afterwards; a mark shared with id-only status lines and notices suppresses a filtered wait for a file the member cannot map to its path; and a capture must keep the filter match that caused it across renames and bursts larger than one result. Viewing is already the acknowledgment that puts content in the loop's context, so the wait uses it and marks nothing but reported ends.

**Waits outside working time** (`suspend_working_limit()` or Inspect waiting time). Rejected, as swarm-limits.md did for `read_messages`: sample waiting time collapses across members, and the swarm measures idle time itself.

## Compatibility and migration

- **inspect_ai:** none. `parallel=False` and `ToolDef` are public; the bus already uses `current_agent_channel()` and `current_agent_span_id()`.
- **Eval logs:** only existing types. `waited` is a new comm kind inside the versioned `InfoEvent` payload, `delivered` is unchanged, and shared-memory `sent` records now carry `wake: true`. No schema or generated-type change. inspect_swarm has not released, so evidence stays `version: 1`.
- **inspect_swarm API (unreleased):** the `swarm_wait` tool (name, parameters, texts), `shared_memory(max_wait=)`, `Channel.max_wait`, `WaitSpec`, `WaitNews` and `WaitingState`, and `Draft.wake`. `Channel`/`ChannelState` stay experimental ([swarm-implementation-plan.md Decisions](swarm-implementation-plan.md#decisions), item 6). The tool and its texts are evaluated surface: an arm with the shared memory now also has `swarm_wait`, so results from before and after this change are different conditions.
- **Members and tasks:** nothing changes for a swarm without the shared-memory channel. A member definition with `swarm_tools()` gains `swarm_wait` in arms with the shared memory, and no new tool otherwise.

## Security

Untrusted input reaching this code, and how it is handled:

- **`path`** (model output): checked by the shared memory's path grammar or file id grammar and refused otherwise; logged in `waited`, never echoed in a result, never in another member's text.
- **`timeout`** (model output): an integer by the tool schema, clamped to 1 to `max_wait`.
- **What a result contains:** counts, swarm-assigned file ids, roster names validated by the channel's `check()`, and statuses from the runtime's closed set. No content, no peer-chosen path. A wait adds no channel for peer text; it adds one fact members did not have before, each peer's end and status, which `list_members` was always going to show in the messages channel.
- **Identity** comes from the member ContextVar and the agent span, never from arguments, as for every swarm tool.
- **Stalling others.** A member cannot keep another's wait open past its timeout, and cannot make the all-waiting release fire falsely: it fires only when no member is running anything but a wait. Frequent writes under a path others watch wake them repeatedly; the shared memory's per-member `rate` bounds that, as it bounds status-line noise.
- **Resources.** `parallel=False` gives each loop at most one model-called wait at a time, and the bus refuses a second wait of the member's own loop, so waiters are bounded by loops; each holds an `anyio.Event` and a small spec.
- **Isolation between samples:** waiters, reported ends and wait ids live on the run's bus.

## Testing

All tests use mockllm with scripted tool calls, run on asyncio and trio, and need no network, Docker or model provider. Scripted members coordinate with test tools that set `anyio.Event`s, so tests do not depend on sleeps; timeout cases use `max_wait=1`.

- **Waking** (`tests/test_wait.py`): a peer's write under `path` ends the wait and the result names the file by id and the writer; a write outside `path`, and the waiter's own write, do not; a write by the member's own subagent does; a file id as `path` follows the file through a rename; a `path` that does not exist yet wakes on its creation; a wait called while an unviewed change is under `path` returns at once, again on every call until the loop views the file, and then sleeps; a file named in a status line or a notice, but not viewed, still ends the next wait (the status line and notices mark nothing for it); eleven unviewed files under `path` are named ten then one more, by the first wait and again by the next while unviewed; a deletion of a file the loop had viewed ends the wait, and a file created and deleted by a peer before the loop viewed it does not; release-then-gone: a peer creates a file under `path` and, before the waiter's task resumes (a test tool holds it), renames it out of `path` or deletes it, and the waiter goes back to sleep and later returns by timeout, with `woken_by` naming the creation's `seq`; status lines and notices for the same sequences match D-1's behaviour without the wait (with M2-9 for notices); a wait result has no status line, and a peer's write outside `path` that did not wake it is named by the status line of the loop's next `shared_memory` call.
- **Ends**: a member ending wakes every waiter of every other member with its name and status; a loop's first wait after an end no wait of that loop has named returns at once; ends are reported once per loop, and a wait cancelled before returning reports nothing and marks no end.
- **Releases**: two members waiting with nothing else running are both released with the all-waiting result, as is a lone member; a pending member (a custom controller that never starts it) and an ended one count as not active; a member in a long scripted tool call keeps the others waiting; a subagent's wait does not make its member waiting; a turn calling a slow tool and `swarm_wait` runs them one after the other; the stop (controller return, swarm cap, time cap) releases every wait at once and a wait called after it returns at once; write-then-stop: a peer's write under `path` releases a waiter and `bus.close()` runs before its task resumes (a test tool holds it), and the result has both the changed-file line and the stop line; close-then-peer-end: after that stop, a peer ends before the waiter resumes, and the result has the changed-file, ends and stop lines; drain-then-peer-end: a background child's wait is released by its member's drain and another member ends before it resumes, and the result has the ends line and the drain line; the timeout returns the timeout result with the effective, clamped value.
- **Cancellation**: a member time limit during a wait writes `waited` with `cancelled`, and the next all-waiting check no longer counts that member; a wait called with a cancellation already pending (on news, the stop or the drain) writes `cancelled`, names nothing and leaves the ends for the loop's next wait.
- **Background members** (in `tests/test_background_members.py`): a background child's wait is released when the member's own agent returns, and a child's later wait returns the drain result at once; a member whose own loop waits while a child runs is not waiting, and becomes waiting (the all-waiting release fires) when the child ends; a child started by a nested swarm's controller, which runs with the inner bus, rechecks the outer member's bus when it ends.
- **Tools offered**: `swarm_wait` with `channels=["shared_memory"]` in the member's own loop and in a `general()` child; neither tool with `channels=["filesystem"]` or `channels=[]`.
- **Configuration** (`tests/test_shared_memory_channel.py`): `max_wait` logged and rebuilt, its bounds raise, two arms differing in it get distinct identifiers.
- **Truncation** (`tests/test_wait.py`): under a generate config with `max_tool_output=100`, a wait that reports several ends and changed files returns its whole text; a long result for a roster of 48-byte names (eleven deleted files, an end and the stop line) stays within a fixed limit, and the shared memory's changed-files line for such a roster stays within a fixed limit.
- **Evidence** (`tests/test_comm_evidence.py`): `waited` with each outcome, `files` equal to what the result named (in the write-then-stop and close-then-peer-end cases of Releases, outcome `stopping` with the written file's id and version in `files`; in drain-then-peer-end, outcome `member_ending` with the ended member in `ended`), in the waiter's tool span with `loop` and `nested`; `woken_by` names the `seq` of each commit that released it, including one whose news was gone at resume; a wait released and then cancelled writes `cancelled` with no `files`; `sent` records of shared-memory writes have `wake: true`; no result contains a file's content or a peer's path, with a peer path chosen to look like an instruction.
- **Metrics** (in their own pull request after M2-13, `tests/test_metrics.py`): `waits`, `waits_by_outcome`, `wait_time` and `idle_time` from a scripted run with known waits, including a member whose subagent waits while its own loop works (wait time counted, idle time not). The idle clock (in `tests/test_comm_evidence.py`): one row per clause of [The idle clock](#metrics), each checking the idle and non-idle intervals; and the refusal of a second own-loop wait, including while the first has found news and not yet returned (in `tests/test_wait.py`).

## Implementation plan

One pull request, M2-13 in [swarm-implementation-plan.md](swarm-implementation-plan.md#m2-pull-requests), after M2-8 and before M2-11:

1. **M2-13. Waiting for other members.**
   - `src/inspect_swarm/_bus/_wait.py` (new): the waiter registry, the per-loop reported ends, the all-waiting check, the `swarm_wait` tool and its result templates.
   - `src/inspect_swarm/_bus/_bus.py`: the registry on `Bus`; `wait()`, `member_ended()`, `member_ending()`, `recheck()`; `close()` releasing waiters; step 4 calling `wakes()` and recording `woken_by`; `Draft.wake` copied to the record.
   - `src/inspect_swarm/_bus/_record.py`: `Draft.wake`.
   - `src/inspect_swarm/_bus/_tools.py`: `swarm_tools()` adds `swarm_wait` when a reachable open channel has `max_wait`.
   - `src/inspect_swarm/_channel/_channel.py`: `Channel.max_wait`, `WaitSpec`, `WaitNews`, `WaitingState`.
   - `src/inspect_swarm/_channel/shared_memory.py`: `max_wait` with validation; `reportable()`, `wait_check()`, `wakes()`, `news()`; `wake=True` on its drafts; the instructions sentence. Its `seen`, `covered`, tombstone rule and status line are unchanged.
   - `src/inspect_swarm/_controller/_control.py`: `_end()` calls `bus.member_ended()`.
   - `src/inspect_swarm/_evidence.py`: `comm_waited()`.
   - `src/inspect_swarm/_ledger.py`: the event log skips `waited` events, which name no one channel.
   - Tests: `tests/test_wait.py`, and the cases above in `tests/test_shared_memory_channel.py`, `tests/test_bus.py` and `tests/test_comm_evidence.py`.
   - `MemberContext.background_running` (`src/inspect_swarm/_budget.py`), kept by the owner with `Bus.recheck()` at zero (`src/inspect_swarm/_background.py`), and `Bus.member_ending()` in `_drain()` (`src/inspect_swarm/_member.py`), with the background-member cases.
   - Not the wait metrics: they land in their own pull request after M2-13, with the idle clock, the `idle` field and the event log's `WaitRecord`s ([Metrics](#metrics)).
   - Depends on: M2-8 (the shared-memory channel, #49) and M2-5 (background members, #50), both merged. M2-11 documents it.
2. **M2-11** documents `swarm_wait` with the shared memory: what wakes it, that it counts as time, the idle metric, and that filesystem-only swarms have no wait.

## Open questions

None. The one question of round 1, whether a swarm without a bus channel (filesystem-only, as the first internal user ran) gets a wait, is decided: no wait in M2; a filesystem swarm that wants waits adds `"shared_memory"` and keeps its notes on the board (decision: Ransom, 2026-10-21: "Q1 agree with recommendation"; [The filesystem channel and per-member sandboxes](#the-filesystem-channel-and-per-member-sandboxes)).

## Not this design

Noticed and left out, for Ransom to file if wanted:

- **The filesystem channel's prompt invites polling**: it tells members to read the notes file "before you start and again as you work" (`src/inspect_swarm/_channel/filesystem.py:20`).
- **Counting subagents' waits** towards the all-waiting release, through per-task wait tracking in M2-5's owner, if Epoch-shaped swarms show long timeouts.
- **A switch to turn waiting off** (`shared_memory(max_wait=None)` or similar) for arms that must match a board without a wait, such as a strict Epoch-parity arm; a one-parameter addition when an experiment needs it.
- **Watching sandbox files** (inotify in the sandbox) so filesystem swarms could wake on writes.
- **`longest_identical_tool_run` and repeated waits**: whether the stuck-in-loops signal should skip `swarm_wait` once there is data.
- **The plan does not mark merged M2 pull requests done** (M2-1 #48, M2-2 #44, M2-3 #45, M2-6 #33, M2-7 #42).
