# Inspect Swarm: the dependency-linked critical path

## Overview

When a swarm ends, its metrics gain `critical_steps_to_answer`: the length, in model calls (steps), of the longest chain of steps that the answer depended on. Steps of one agent loop follow each other. A step that reads another loop's work follows the step that wrote it: a shared-memory `view` links every write in the shown file version's history to the reader's next step, and a `swarm_wait` links the writes and member ends its result named to the waiter's next step. A member's subagents are loops of their own: each starts from what its parent had when it started it, and its result reaches the parent's later steps once it has returned, so subagents that run in parallel count once, by the slowest, as Kimi's CriticalSteps counts them. The chain ends where the answering member's agent returned, so background work drained after the answer is left out.

It is the measure latency curves are interpreted by, beside the existing `steps_to_answer` and `longest_member_steps`, which stay as they are. It is computed at run end from events the swarm's event log already sees, with no change to the bus or the evidence. It counts only dependencies the swarm records (shared memory, waits, and subagents starting and returning), so when members, or a member's own subagents, can also share information another way, such as a sandbox, it is a lower bound.

## Background

This is the "dependency-linked critical path" of [swarm.md Metrics](swarm.md#observer-evidence-accounting-and-metrics) and of the plan's [Future work](swarm-implementation-plan.md#future-work), which latency curves wait for (decision: Ransom, 2026-10-14). The existing step counts are defined in the user docs ([Analysis, Metrics](https://meridianlabs-ai.github.io/inspect_swarm/analysis.html#metrics)). The shared-memory channel ([swarm-memory-channel.md](swarm-memory-channel.md#evidence)) and `swarm_wait` ([swarm-wait.md](swarm-wait.md#evidence)) write the evidence this metric reads.

## Why

A swarm's latency question ([swarm.md](swarm.md), "What does a swarm buy in latency?") needs to know how many sequential model calls the answer took, given that members work in parallel but wait on and build on each other. The existing counts cannot say:

- `steps_to_answer` counts every step of the answering member, its subagents' and drained background work's included (`src/inspect_swarm/_metrics.py:88-96`, `:221-224`). A deepagent member that runs five subagents in parallel is charged all five.
- Neither count sees members' dependencies. A member that waits for a peer's lemma and then proves the theorem in three steps has `steps_to_answer` 3, though the answer could not exist before the peer's ten steps. The field comment's claim that both counts "bound the sequential depth from above" holds for each member alone, not for the swarm.

The evidence that closes the gap is already written: every board write's commit (`delivered`, with each file's new version), every view (`read`, with the version shown), every wait (`waited`, with the files and ends its result named), each in the tool span of the call that made it.

## Goals and non-goals

Goals:

- One per-sample metric, in steps, of the longest dependency chain ending at the answer, computed when the swarm ends and written with the other metrics.
- Concurrency inside a member (parallel and background subagents) counted by its longest branch, not its sum; sequential delegation counted in full.
- No change to the bus, the channel or the evidence format.
- A plain statement of what the number cannot see.

Non-goals:

- The longest chain anywhere in the swarm: overall depth stays with the existing per-member `longest_member_steps` (decision: Ransom, 2026-10-28).
- A list of the unobserved ways members could share information (decision: Ransom, 2026-10-28; [Alternatives](#alternatives-considered)).
- A wall-clock critical path ([Alternatives](#alternatives-considered)).
- An analysis helper that recomputes the metric from existing logs ([Not this design](#not-this-design)).
- Edges for channels other than `shared_memory`; the later messages channel brings its own rule.
- Line-level attribution of a partial view.

## Current behaviour

### What a step is and where it is logged

- **A step** is a completed model call in a member's spans, cache replays included and errored or cancelled calls excluded: `calls.calls + calls.cache_replays` of the member's ledger line (`_metrics.py:285-287`), counted by `count_call()` (`src/inspect_swarm/_ledger.py:532-550`).
- **The member's span is an agent span** opened by the member wrapper around its agent (`src/inspect_swarm/_member.py:516-520`). It stays open through the member's drain: the wrapper holds it in an exit stack that closes after `_run_member()` returns, and that runs `_drain()` after the agent returns (`:443-445`, `:520-533`). Nothing is written to the transcript when the agent returns.
- **A member's own generations** are `ModelEvent`s in that span for `react()` and `deepagent()` members, and for `checked_solver()` in `examples/stop_on_proof.py`, which calls `react()` inside its own body. With Inspect's sample checkpointing on, `react()` groups turns in `checkpoint` spans (`inspect_ai/agent/_react.py:213`, `inspect_ai/util/_checkpoint/checkpointer_impl.py:293`), which are neither agent nor tool spans, so a turn's model call and tool spans can sit one level below the agent span.
- **Subagents are agent spans of their own.** A `deepagent()` `general()` child, synchronous or background, is an agent span whose parent is the tool span of the parent's `agent(...)` call. A background child's span begins after the dispatching tool span has ended and is still that span's child, on both backends. deepagent tells the parent that a background child finished with a notice on its next turn and hands over the result through `agent_status` or `agent_wait` (`inspect_ai/agent/_deepagent/lifecycle_tools.py:510-529`, `:606`). Delegating members call `run()` from their own body with no tool span between (`tests/test_metrics.py:290`, `:687`); `run()` opens the child's agent span and returns its state when it ends (`inspect_ai/agent/_run.py:83-101`). Background work a loop starts outlives that loop: it is owned by the member (`_member.py:492`, `src/inspect_swarm/_background.py:43`), not by the loop's span.
- **Tool calls run in tool spans** (`inspect_ai/model/_call_tools.py:855`). The `ToolEvent` itself carries the loop's agent span as its `span_id`, not the tool span, and tool call ids can repeat ([swarm-memory-channel.md Descendants](swarm-memory-channel.md#descendants)), so a tool span is tied to the generation that proposed it by order, not by id.
- **Order.** The transcript notifies its subscribers synchronously when it first reports an event, before counting it, and again on each update (`inspect_ai/log/_transcript.py:691-713`, `:762-807`); so during the first notification `transcript().history.event_count` is the event's index, the count `MemberTimelines` already reads (`src/inspect_swarm/_evidence.py:216`). A `ModelEvent` is first reported when its call begins, before the tool spans it proposes. A sample's logged `events` keep the same first-report order.

### The evidence

Each is an `InfoEvent` with source `inspect_swarm` and `type: "comm"`, written in the tool span of the call that acted (the event's `span_id`), with `member`, the acting member, and `loop`, the acting loop's agent span (`src/inspect_swarm/_evidence.py:46-167`):

| Kind | Written | Fields the metric reads |
|---|---|---|
| `delivered` | step 4 of `Bus.deliver()`, in the writer's tool span (`src/inspect_swarm/_bus/_bus.py:185-190`) | `member`, `loop`; `result.files`: `[{"file", "path", "base", "version", "deleted"?}]`, the version each file now has |
| `read` | a `view` returns (`src/inspect_swarm/_channel/shared_memory.py:841-931`) | `member`, `loop`; `files`: `[{"file", "version", "record", "lines", "deleted"}]`; `listing` |
| `waited` | a `swarm_wait` ends (`src/inspect_swarm/_bus/_wait.py:250-268`) | `member`, `loop`; `files`: `[{"file", "version", "deleted"}]` and `ended`: `[{"member", "status"}]`, what the result named; `woken_by` |

- Versions of a file are consecutive: a file is created at version 1 and every write that touches it, a delete included, adds one ([swarm-memory-channel.md The board's state](swarm-memory-channel.md#the-boards-state)). A file id is never reused within a board, and renames keep it. Each run's board numbers its files from `f-1` (`shared_memory.py:276`, `:712`), so a nested swarm's board reuses ids the outer board has.
- A `view` marks the version it shows and writes `read` in the same synchronous call, and a write's `apply()` and its `delivered` event run with no `await` between them, so within one board the order of `delivered` and `read` events in the transcript is the order in which the board changed and was read.
- `woken_by` lists the commits that released a wait for news. A wait that finds news already waiting returns at once with `woken_by: []` and the files it named in `files`; a release whose news has gone by the time the waiter resumes keeps its commit in `woken_by` though the result never named it ([swarm-wait.md What a wait returns](swarm-wait.md#what-a-wait-returns)). A cancelled wait records `files: []` and `ended: []` (`_wait.py:246-249`).
- A member's end is a `result` event of kind `member_result` naming it, written in its span when it ends, after its drain (`src/inspect_swarm/_controller/_control.py:586-608`).
- **Which run an event belongs to.** Member names are unique in a sample, since each names a timeline and a second swarm reusing one is refused (`_evidence.py:208-216`); the communication metrics already rely on this to keep another swarm's events out (`_metrics.py:229-231`). A nested swarm's members run inside an outer member's span (`tests/test_background_members.py:1090-1100`), so `SpanPlaces` attributes their spans to the outer member (`_ledger.py:490-515`), but their evidence names the inner members.

### What the runtime keeps

`EventLog` (`_ledger.py:273-337`) is subscribed to the transcript for the swarm's interval and keeps each span's parent and the set of agent spans, each `ModelEvent` as it last stood by uuid, tool events, intermediate scores, and compact `comm` and `waited` records. It keeps no event positions, no span ends, and none of the comm fields above beyond kind, member, channel, record id, rejection, payload size and `listing` (`CommRecord`, `:247-257`; `WaitRecord`, `:260-266`). The metrics are computed in `SwarmRun.run()`'s `finally`, once every member has ended, from the ledger, the event log, the member spans and the record's answer (`_control.py:452-509`).

### Ways members share information outside the evidence

All in real use: the sample's sandbox, shared by every member by default (`swarm()`'s docstring of `channels`, `src/inspect_swarm/_swarm.py:53`; `examples/shared_sandbox.py`); a per-member sandbox, which the member's own subagents and background work share (`_member.py:360`, `:485-489`; [Members](https://meridianlabs-ai.github.io/inspect_swarm/members.html)); `filesystem(shared_volume=True)`; a service in `visible_sandboxes`, such as `examples/proof_swarm.py`'s comparator; custom channels. A controller can also start a member with an input of its own, or after another has ended (`_control.py:117-132`); `leaderless()` and the examples start every member at once with the swarm's input. Shared tool instances, shared store keys and outside services are possible too.

## Design

### At a glance

```
 A (react)      a1 → a2 → a3 → a4 → a5 → a6        a5 creates f-1 (version 1); a6 appends (version 2)
                                    │     │
                     ┌──────────────┘     └──────────────┐
                     ▼                                   ▼
 B (react)      b1 → b2 ───────────────────────────────→ b3
                b1 calls swarm_wait; its result names f-1 version 1 (a5's write): a5 → b2
                b2 views f-1 and is shown version 2 (a5's and a6's writes): a6 → b3; b3 submits

 C (deepagent)  c1 ─┬→ d1 → d2 → d3 ──────────┬→ c2 → c3
                    └→ e1 → e2 → e3 → e4 → e5 ┘
                c1 starts two general() children, d and e, in one turn; c2 is its next turn

 D (wrapper)    p1 ─→ run(plan) q1 → q2 ─→ run(solve, plan's messages) r1 → r2 → r3 ─→ returns
                p1 is its own call; each run() is awaited in its body

 values:  a1..a6 = 1..6    b1 = 1   b2 = max(b1, a5) + 1 = 6   b3 = max(b2, a6) + 1 = 7
          c1 = 1   d1..d3 = 2..4   e1..e5 = 2..6   c2 = max(c1, d3, e5) + 1 = 7   c3 = 8
          p1 = 1   q1, q2 = 2, 3   r1..r3 = 4..6   D's answer = 6
```

If B answers, `critical_steps_to_answer` is 7 (A's six steps, then B's view and submit) where `steps_to_answer` is 3. If C answers, it is 8 (C's three steps plus its slower child's five) where `steps_to_answer` is 11. If D answers, it is 6, the whole sequence, where a rule that only followed D's own calls would give 1.

### The graph

**Nodes** are steps, as the existing counts define them: completed model calls in a member's spans, cache replays included. Each weighs one.

**Loops.** Every agent span in a member's span tree is a loop; the member's span is its root loop. A step belongs to the nearest agent span enclosing its `ModelEvent`'s span (`_agent_span()`, `_metrics.py:290-293`), through any span that is not an agent span (a `checkpoint` span, or a model-graded check inside a tool span). A loop's steps are ordered by when their calls began.

**Positions.** Every span begin and end, model call and evidence event has a position: its index in the transcript, as defined above.

**Anchors.** Something that happens inside a loop L (an evidence event, a nested loop's span) is placed in L by its **anchor**: the outermost tool span on its way up to L, which is the tool call one of L's generations proposed, however deep it sits under `checkpoint` or other grouping spans. When there is no tool span on the way, L's own code did it (a wrapper's `run()`), and the anchor is the thing itself: the nested loop's span, or the evidence event's position.

**What a loop has at a point.** For a loop L and a position x:

```
has(L, x) = max(
    entry(L),
    value(s)     for each step s of L that began before x,
    returned(N)  for each nested loop N of L whose span ended before x,
)
returned(L) = has(L, end of L's span)
```

`returned(N)` is what a nested loop hands back when it returns: its own work and that of the loops it ran that had returned by then. A loop that ends after its parent has ended, such as background work outliving the loop that started it, is in no `has()` of that parent at or before the parent's end, so later work never feeds an earlier return.

**Values.**

```
value(s) = 1 + max(
    has(L, begin of s),                        # L: s's loop
    source(w)          for each write w shown to L by a read or wait whose anchor ended before s began,
    member_end(M)      for each member M named as ended by a wait of L whose anchor ended before s began,
)
start(L, a) = value of L's last step that began before a, if a is a tool span   # the generation that proposed it
              has(L, begin of a), otherwise                                       # L's own code
            (has(L, begin of a) too when a tool span has no step of L before it)
entry(root of a member) = 0
entry(N) = start(P, anchor of N)               # P: the loop N is nested in
source(w) = start(W, anchor of w)              # W: the loop that made write w
member_end(M) = has(root of M, position of M's member_result event)
```

Joins need no rule of their own: a nested loop's return is in `has()` of every later point of its parent, so it feeds the parent's next step, and the next nested loop the parent's own code starts.

**The answer.** `critical_steps_to_answer = has(root of A, returned_at(A))`, where A is the answering member and `returned_at(A)` the position at which its agent returned ([Where the answer ends](#where-the-answer-ends)).

**Edges, by question.**

1. **Concurrent loops of a member: one chain per loop.** A nested loop starts from what its parent had when the loop started. For a `general()` child, synchronous or background, the anchor is the parent's `agent(...)` tool span, so it starts from the generation that proposed the call, even when the child's span begins after that tool span ended. Two children dispatched in one turn both start from that generation, even when deepagent runs them one after the other, because a turn's tool calls are proposed together and do not see each other's results; both feed the parent's next turn, which takes the slower: the parent's steps plus the slowest child's per stage, Kimi's CriticalSteps. A background child that has finished feeds the parent's first generation after its end, which carries deepagent's notice; the parent may fetch the result later with `agent_status`, so this can be early, which only lengthens chains through the parent. A loop the parent's own code runs (`run()` in a wrapper's body) has no tool span, so it starts from everything the parent had, including loops that had already returned: two `run()` calls in sequence chain, and two run concurrently (both begun before either ends) take the slower.
2. **Endpoints.** A write's source is the generation that proposed the call: `start(W, anchor)`, the last step of W that began before the call's tool span began. A read's or wait's dependence starts at the reading loop's first step that begins after the call's tool span ends: the first generation whose input holds the tool result. A read after a loop's last step consumed nothing and adds no edge. Steps are matched by position, never by tool call id. Under `checkpoint` spans the tool span is still the anchor, so a turn's write and read keep their own generations.
3. **Which reads make edges:**

   | What the loop was shown | Edge into its next step, from |
   |---|---|
   | `view` of a file, full or with `view_range` | every write in the shown version's history (versions 1 to the shown one of that file id) by a loop other than the reader; in practice the latest such write of each writing loop, since a loop's earlier writes precede its later ones on its own chain |
   | `view` of a deleted file's id | the deleting write: the reader is shown that the file was deleted and by whom, not its content |
   | `view` of a directory (listing) | none |
   | the status line, `changes` | none (no evidence, and the step that sees them was not blocked on them) |
   | `swarm_wait` result | the write that produced each version its `files` names; `member_end()` of each member its `ended` names; nothing from a cancelled wait |

   A partial view links the whole version, as [swarm-memory-channel.md Evidence](swarm-memory-channel.md#evidence) says: the evidence records the range, but which lines came from which write would need the board replayed line by line, and over-approximating an edge errs towards a longer chain. A listing shows ids, versions, writers and peer-chosen paths of up to the whole board; linking every listed file's writer would tie the reader to nearly every earlier step, so listings make no edge. A loop that acts on a listing views what it needs, and that view makes the edge.

   **A wait is an edge, from what its result named, not from `woken_by`.** A wait blocks the loop until a write or an end it cares about, so the waiter's next step depends on it as much as on a view; that step usually views the file, which adds the content edge one step later, and the extra step is a real cost of the wait-then-view protocol. `files` and `ended` are what reached the model. `woken_by` is not: a wait that finds news at once has `woken_by: []`, and a release whose news vanished keeps a commit the result never named. `woken_by` stays what [swarm-wait.md](swarm-wait.md#evidence) uses it for, joining idle time to the commits that ended it.
4. **This run's evidence only.** An evidence event counts when its `member` is in this swarm's roster; a wait's `ended` names only roster members too. A nested swarm's evidence names its own members and is left out, so its board's ids never mix with this board's. Its members' loops still count as nested loops of the outer member whose span holds them: their steps chain, start and return as any nested loop's do, and only the inner board's edges are missing, which the inner swarm's own metric counts.
5. **Every edge points forward.** A write's source began before the write's tool span, which holds its `delivered` event; any read or wait that shows its version comes after that event, inside the reader's tool span, and the reader's next step begins after that span ends. `start()` uses only what began before the anchor, `has()` only what began or ended before its point, and a member's `member_result` follows all its work. So every value is computed from positions before its own, and one pass in position order computes them all. A background grandchild that outlives its parent, views a file the grandparent wrote after the parent returned, and then makes a step feeds nothing back into the parent's return: that return is `has(parent, parent's end)`, and the grandchild had not ended by then.

### Where the answer ends

The answer's chain ends where the answering member's agent returned: its last generation, and the work that had returned to it by then, synchronous delegation included (D above). Background work still running is drained after the return and is left out, as it cannot have contributed to the answer; background work that finished before the return is included, which can over-count when the agent never fetched its result.

The transcript has no event at that point: the member's span stays open through the drain. So the member wrapper records it: right after `_call_agent()` returns, it stores `transcript().history.event_count` on the member's `MemberOutcome` as `returned_at`, the position of the next event. This is runtime state for the metric, not evidence. It is the smallest addition that tells drained work from returned work, since every existing event either precedes the return or (the member span's end, `member_result`) follows the drain. A member whose agent did not return (cancelled, or ended by a limit) has `returned_at` `None`; the answer always comes from a member that ended with a submission, so in practice it has one, and with `None` the metric is `None`.

### The metric

```python
# src/inspect_swarm/_metrics.py, SwarmMetrics
    # critical path, the dependency-linked definition: the longest chain of steps
    # (completed model calls, cache replays included) ending where the answering
    # member's agent returned, in which a step follows the previous steps of its agent
    # loop and what returned to that loop before it began, a nested loop starts from
    # what its parent had when it started it, and a step follows the shared-memory
    # writes and member ends that a read or wait of its loop showed it
    # (_critical_path.py). It counts only those dependencies, so it is a lower bound
    # when members, or one member's loops, can share information in other ways: a
    # shared sandbox (a member's subagents share its own sandbox), a shared volume, a
    # service, a custom channel, a shared tool or store key, or a controller that hands
    # one member's result to another. None without an answer, and in metrics written
    # before this field.
    critical_steps_to_answer: int | None = None
```

- **Always reported**, whatever the swarm's configuration, with no list of unobserved sharing (decision: Ransom, 2026-10-28). The ways members can share outside the evidence are the experimenter's own configuration, a list could never rule out shared tools, store keys or services, and it would flag nearly every arm, since a member's subagents share its sandbox. The field comment and the user docs say what the number counts and that it is a lower bound when any other sharing is possible.
- **Unit: steps.** A step is the unit of the existing counts and of Kimi's CriticalSteps, and it is independent of provider latency and of `max_connections`. A chain's span in seconds, from its first step to the answer, is about the answer's own time, which the record already has (`MemberResult.time`); a sum of call durations along it mixes connection waits and retries in, which plan decision 12 ruled out for per-member timing until it can follow Inspect's working-time accounting ([swarm-implementation-plan.md Decisions](swarm-implementation-plan.md#decisions), item 12).
- **The existing fields stay** with their meaning, and their comment is corrected: they bound each member's own sequential depth from above, not the swarm's. The new number is not bounded by them: a chain through two members can be longer than either member's step count, and a member's parallel children make its step count larger than its chain.
- **Over-approximations**, each of which can only lengthen the chain: a partial view links the whole version; a version's history links writes whose text a later edit removed; a background child joins the parent's first step after it finished; work finished before the answer's return counts though the agent may not have used it; concurrent generations inside one agent span (a custom agent, or a bridged CLI agent's own subagents) form one chain, as the existing counts treat them.

### Where it is computed

**At run end, as a per-sample metric**, beside the existing ones, in the same `SwarmMetrics` event and store entry, so every new run carries it and scorers can read it. Everything it needs is seen by the runtime: the `EventLog` is subscribed for the whole interval, and the metrics are computed after every member has ended. The computation is a pure function over compact records that an `EventLog` builds from events; the logs hold the same events in the same order, so fed a logged sample's `events` an `EventLog` builds the same records, which a test checks. Only `returned_at` is not in the log.

### Code

**`src/inspect_swarm/_critical_path.py`** (new, private):

```python
# What the critical path reads from the transcript, by position (the transcript's
# event index when the event was first reported). Filled by EventLog.
class PathLog:
    spans: dict[str, PathSpan]          # every span: parent, type, begin and end positions
    steps: dict[str, tuple[str | None, int]]   # model event uuid -> (span id, begin position)
    writes: list[PathWrite]             # `delivered`: position, span, member, loop, [(file, version, deleted)]
    reads: list[PathRead]               # `read` without `listing`: position, span, member, loop, [(file, version, deleted)]
    waits: list[PathWait]               # `waited`: position, span, member, loop, [(file, version)], [ended members]
    ends: dict[str, int]                # `member_result`: member -> position

    def __call__(self, event: Event, position: int) -> None: ...

def critical_steps_to_answer(
    log: PathLog,
    completed: Callable[[str], bool],    # whether a model event uuid is a step, as count_call() counts it
    places: Callable[[str | None], tuple[bool, str | None]],  # SpanPlaces: the member whose spans hold a span
    member_spans: Mapping[str, str],     # each started member's span, its root loop
    answer: str | None,
    returned_at: int | None,             # the answering member's return position
) -> int | None: ...
```

- **Recording.** `EventLog.__call__` passes each event the first time the transcript reports it (by uuid; an update is not passed again) to its `PathLog` with `transcript().history.event_count` as its position: span begins and ends, model events, `comm` events and `member_result` events. The `PathLog` keeps only ids, positions, span types, loop ids, file ids, versions and member names: nothing a member wrote. `CommRecord` and `WaitRecord` are unchanged. `places` is passed as a callable so that `_critical_path.py` imports nothing from `_ledger.py`, which imports it.
- **Returning.** `_run_member()` sets `outcome.returned_at = transcript().history.event_count` right after `_call_agent()` returns (`_member.py:520`); `MemberOutcome` gains the field, defaulting to `None`.
- **Computing.** `critical_steps_to_answer()` keeps the evidence of roster members only, builds each member's loops from `places` and the span parents, orders each loop's steps (model events in it that `completed()` accepts) by position, and walks steps, span ends, writes, reads, waits and member ends in position order once. Per loop it keeps its running `has()` (the latest step value, and the returns of nested loops as they end), the last step value before each open tool span (for `start()`), and the reads and waits whose next step has not come yet. Per file id it keeps the latest write of each loop to the file's current version, so a read costs one entry per writing loop, and a map of `(file, version)` to write for tombstone views and waits. Spans outside every member (the controller's, the final step's) are ignored. It returns `has(root of A, returned_at)`, or `None` without an answer or `returned_at`.
- **Inconsistent evidence.** A read whose shown version is not the file's current version at its position, or a named version no roster write produced, would mean the ordering assumption broke. The function then logs a warning naming the file and versions and returns `None`; it never raises, so the metric cannot fail a run that succeeds without it.
- **Wiring.** `swarm_metrics()` takes `member_spans` (the `spans` mapping `_control.py` already builds for `places`) and the answering member's `returned_at`, calls `critical_steps_to_answer()` with the event log's `PathLog` and `completed` built from `EventLog.calls`, and fills the field. Everything stays synchronous, as the rest of the `finally` is.

**Configuration.** None: the metric is always on, as the other metrics are.

## Alternatives considered

- **One chain per member.** Each member's steps in begin order, cross-member edges only. It equals the existing count for a member with no edges, so it charges a deepagent member every parallel child, the problem this metric exists to fix.
- **Joining a nested loop into its parent's next step only** (fork from the step before it, join into the step after it). Loses sequential delegation in a body with no model call between two `run()` calls, and the delegated solver after the root's last call; `has()` carries returned work forward to whatever the parent does next.
- **A loop's exit including everything it ever started.** Background work that outlives the loop would feed the loop's earlier return and can form a cycle; `returned(L)` takes only what had returned by L's end.
- **Ending the answer at the root loop's last model call, or at the member span's end.** The first misses a solver the root delegated to after its last call; the second includes the drain. Recording the return position is the only way to place the end exactly, as no event marks it.
- **An evidence event at the agent's return.** Would let a log helper place the end too, but it changes the evidence for a value only the runtime needs now; it can come with that helper.
- **`woken_by` as the wait's edge.** Misses waits that returned at once (`woken_by: []`) and adds commits whose news never reached the loop.
- **No edge for waits, only for views.** A wait-then-view hand-off would count one step fewer than it took.
- **Listings as edges.** A root listing would tie a member to nearly all earlier work.
- **`critical_steps`, the longest chain anywhere** (decision: Ransom, 2026-10-28, dropped). It charges no answer and describes overall depth, which `longest_member_steps` already gives per member.
- **`critical_path_unobserved`, a list of known unobserved sharing** (decision: Ransom, 2026-10-28, dropped). It reports configuration the experimenter set, cannot rule out what it does not detect, and would be non-empty for nearly every arm.
- **Omitting the metric when sharing outside the evidence is possible.** It would drop the metric for the default configuration, where the recorded edges still hold and give a lower bound (decision: Ransom, 2026-10-28: always report).
- **A wall-clock path.** See [The metric](#the-metric).
- **An analysis helper over logs instead of a run-end metric.** Works on older logs, but every arm would need the user to call it, scorers could not read the value, and it needs the return position, which the log lacks.
- **Adding the step's index or the proposing tool call id to the evidence.** The order of events already determines the endpoints, and keeping the evidence as it is leaves every channel and log reader untouched.

## Compatibility and migration

- **`SwarmMetrics`** gains one optional field with a `None` default, so metrics written before load unchanged; `version` stays 1, as for the fields added before. The `metrics` `InfoEvent` and the `inspect_swarm:metrics` store entry carry it.
- **Evidence, the bus, channels, the ledger, the result record:** unchanged. `EventLog`, `CommRecord` and `MemberOutcome` are private.
- **Public API:** no new names. `SwarmColumns` is unchanged (it reads the ledger, not the metrics).
- **Docs:** [Analysis, Metrics](https://meridianlabs-ai.github.io/inspect_swarm/analysis.html#metrics) gains `critical_steps_to_answer`: what it counts, that latency is read from it, and, plainly, that it is a lower bound whenever members or a member's own subagents can share information outside shared memory and waits (a shared sandbox, including a member's own sandbox shared with its subagents; a shared volume; a service; a custom channel; shared tools or store keys; a controller passing results between members).
- **Old runs** have no value; recomputing it from their logs is the helper left out below.

## Security

No untrusted text reaches the computation. It reads span ids and types, positions, model event status, and from the swarm's own evidence member names, loop ids, file ids and integer versions; paths, payloads and file contents are neither stored in the `PathLog` nor read. Model output reaches it only as the tool calls that made the evidence, which the channel validated, and a model cannot write an `InfoEvent`. Agent code can (any code may call `transcript().info()` with source `inspect_swarm`), but member and tool code is the eval author's and trusted, as for every other metric read from evidence; inconsistent evidence yields `None` and a warning, not an error. Work is linear in the events of the interval plus one entry per writing loop per read.

## Testing

All async tests run on asyncio and trio through the `anyio_backend` fixture.

- **`tests/test_critical_path.py`**: table-driven tests of `critical_steps_to_answer()` over hand-built `PathLog`s:
  - a single loop (its steps); two members with a wait and a view (B above, 7);
  - a wait that returned at once (`woken_by: []`, edge from `files`); a wait released by a write whose news vanished (no edge); a wait naming an ended member (edge from `member_end()`); a cancelled wait (no edge);
  - a listing, and a read after its loop's last step (no edge); a tombstone view (deleting write only); a version history written by two loops (an edge from each);
  - turns grouped in `checkpoint` spans: a write and a read keep their own generations;
  - parallel `general()` children run one after the other in one turn (C above, 8, not the sum); a background child that finishes before the parent's next step, and one still running at the return (left out);
  - a wrapper's planning call then a delegated three-step solver (4); two sequential `run()` stages (D above, 6) and two concurrent ones (the slower);
  - a background grandchild that outlives its parent and reads a later write of the grandparent (no cycle; the parent's return excludes it);
  - a nested swarm's evidence with its own `f-1` inside a member (ignored, no warning);
  - an inconsistent read version (`None` and a warning); errored and cache-replayed model events; no answer, and no `returned_at` (`None`).
- **`tests/test_metrics.py`**: mockllm swarms through `run_swarm()`, asserting the field: two `react()` members, one writing and one waiting and viewing (as B above); a deepagent member with two `general()` children dispatched in one turn, and one with a background child still running when it submits; a delegating wrapper member as in `tests/test_metrics.py:290`; a member running a nested swarm with shared memory (as `tests/test_background_members.py:1090`); a `react()` member with Inspect's sample checkpointing on, so its turns sit in `checkpoint` spans.
- **Runtime and log agree**: one swarm run through `eval_async()`, whose logged sample's `events`, fed to a fresh `EventLog`, build the same `PathLog` as the run's own `EventLog` did. This guards the ordering assumption and a later log helper.

No test needs Docker, a network or a model provider.

## Implementation plan

1. **The path and its recording.** `src/inspect_swarm/_critical_path.py` (`PathLog`, `critical_steps_to_answer()`), `src/inspect_swarm/_ledger.py` (`EventLog` passes first reports with their positions to its `PathLog`), `tests/test_critical_path.py`.
2. **The metric.** `src/inspect_swarm/_member.py` (`MemberOutcome.returned_at`, set after `_call_agent()`), `src/inspect_swarm/_metrics.py` (the field, the corrected comment on the existing counts, `swarm_metrics()`'s new arguments), `src/inspect_swarm/_controller/_control.py` (the arguments at run end), `docs/analysis.qmd` (Metrics), `tests/test_metrics.py` with the runtime-and-log test.

## Open questions

None.

## Not this design

- **An analysis helper over logs**, such as `swarm_critical_path(sample)` in `inspect_swarm.analysis`, for logs written before the metric. It needs each member's return position, which the log lacks, so it would come with an evidence field at the return.
- **The chain itself**, as the members and step counts along it, for explaining a number; the metric records its length only.
- **`SwarmColumns` for metrics.** The columns read the ledger; metrics are read from the store entry today.
- **The messages channel's edges.** Its `read` evidence will name records rather than file versions; its design adds its rule here.
- **Edges for a member started by a controller** with another member's output, which would need the controller to record what it passed.
- **Edges through a nested swarm's board** for the outer swarm's metric; the inner swarm's own metrics count them.
