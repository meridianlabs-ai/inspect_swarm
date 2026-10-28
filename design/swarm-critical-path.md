# Inspect Swarm: the dependency-linked critical path

## Overview

When a swarm ends, its metrics gain the swarm's critical path: the longest chain of model calls (steps) in which each step could have depended on the one before it. Steps of one agent loop follow each other. A step that reads another loop's work follows the step that wrote it: a shared-memory `view` links every write in the shown file version's history to the reader's next step, and a `swarm_wait` links the writes and member ends its result named to the waiter's next step. A member's subagents are loops of their own, forked from the step that started them and joined into the first step of their parent after they end, so subagents that run in parallel count once, by the slowest, as Kimi's CriticalSteps counts them.

Two numbers come out, in steps: `critical_steps`, the longest such chain in the swarm, and `critical_steps_to_answer`, the longest chain that ends at the answer's last step. They are what latency curves are interpreted by; M1's `steps_to_answer` and `longest_member_steps` stay as they are. The path is computed at run end from events the swarm's event log already sees, with no change to the bus or the channel's evidence. Because the swarm cannot see every way members share information (a shared sandbox above all), the metrics also list the unobserved ways it knows of, so analysis can tell an exact path from one that may miss edges.

## Background

This is the "dependency-linked critical path" of [swarm.md Metrics](swarm.md#observer-evidence-accounting-and-metrics) and of the plan's [Future work](swarm-implementation-plan.md#future-work). M1-11 shipped a simple definition: the selected member's step count and the largest member's step count (decision: Ransom, 2026-10-14). The shared-memory channel ([swarm-memory-channel.md](swarm-memory-channel.md#evidence)) and `swarm_wait` ([swarm-wait.md](swarm-wait.md#evidence)) were designed to write the evidence this metric needs, and swarm.md's caution stands until it exists: latency curves are not interpreted before it.

Code references are to inspect_swarm `main` at `1c8aea1`, and to inspect_ai as locked there. Claims marked *(spike)* were checked by running a swarm with mockllm members and printing its events, on asyncio and trio.

## Why

A swarm's latency question ([swarm.md](swarm.md), "What does a swarm buy in latency?") needs to know how many sequential model calls the answer took, given that members work in parallel but wait on and build on each other. M1's numbers cannot say:

- `steps_to_answer` counts every step of the answering member, its subagents' and drained background work's included (`src/inspect_swarm/_metrics.py:88-96`, `:221-224`). A deepagent member that runs five subagents in parallel is charged all five.
- Neither number sees members' dependencies. A member that waits for a peer's lemma and then proves the theorem in three steps has `steps_to_answer` 3, though the answer could not exist before the peer's ten steps. The field comment's claim that both numbers "bound the sequential depth from above" holds for each member alone, not for the swarm.

The evidence that closes the gap is already written: every board write's commit (`delivered`, with each file's new version), every view (`read`, with the version shown), every wait (`waited`, with the files and ends its result named), each in the tool span of the loop that acted.

## Goals and non-goals

Goals:

- A per-sample metric, in steps, of the longest dependency chain in the swarm and of the longest ending at the answer, computed when the swarm ends and written with the other metrics.
- Concurrency inside a member (parallel and background subagents) counted by its longest branch, not its sum.
- No change to the bus, the channel or the evidence format.
- An honest claim: the metrics name the ways members could share information that the swarm knows of and does not observe.

Non-goals:

- A wall-clock critical path ([Alternatives](#alternatives-considered)).
- An analysis helper that recomputes the path from existing logs ([Not this design](#not-this-design)).
- Edges for channels other than `shared_memory`; the later messages channel brings its own rule.
- Line-level attribution of a partial view, or of which parts of a file a reader used.
- Changing or removing `steps_to_answer` and `longest_member_steps`.

## Current behaviour

### What a step is and where it is logged

- **A step** is a completed model call in a member's spans, cache replays included and errored or cancelled calls excluded: `calls.calls + calls.cache_replays` of the member's ledger line (`_metrics.py:285-287`), counted by `count_call()` (`src/inspect_swarm/_ledger.py:532-550`).
- **The member's span is an agent span** opened by the member wrapper around its agent (`src/inspect_swarm/_member.py:516-519`). A `react()` or `deepagent()` member's own generations are `ModelEvent`s directly in that span *(spike)*; so is `checked_solver()` in `examples/stop_on_proof.py`, which calls `react()` inside its own body without a span.
- **Subagents are agent spans of their own.** A `deepagent()` `general()` child, synchronous or background, is an agent span whose parent is the tool span of the parent's `agent(...)` call; its generations are in it *(spike)*. A background child's span begins after the dispatching tool span has ended, and is still that span's child *(spike, both backends)*. deepagent tells the parent that a background child finished with a notice on its next turn and hands over the result through `agent_status` or `agent_wait` (`inspect_ai/agent/_deepagent/lifecycle_tools.py:510-529`, `:606`).
- **Tool calls run in tool spans**, children of the loop's agent span (`inspect_ai/model/_call_tools.py:855`). The `ToolEvent` itself carries the loop's agent span as its `span_id`, not the tool span *(spike)*, and tool call ids can repeat ([swarm-memory-channel.md Descendants](swarm-memory-channel.md#descendants)), so a tool span is tied to the generation that proposed it by order, not by id.
- **Order.** The transcript notifies its subscribers synchronously when it first reports an event and again on each update (`inspect_ai/log/_transcript.py:691-713`, `:762-807`); a `ModelEvent` is first reported when its call begins, before the tool spans it proposes *(spike)*. A sample's logged `events` keep the same first-report order.

### The evidence

Each is an `InfoEvent` with source `inspect_swarm` and `type: "comm"`, written in the tool span of the call that acted (its `span_id`), with `loop`, the acting loop's agent span (`src/inspect_swarm/_evidence.py:46-167`):

| Kind | Written | Fields the path reads |
|---|---|---|
| `delivered` | step 4 of `Bus.deliver()`, in the writer's tool span (`src/inspect_swarm/_bus/_bus.py:185-190`) | `loop`; `result.files`: `[{"file", "path", "base", "version", "deleted"?}]`, the version each file now has |
| `read` | a `view` returns (`src/inspect_swarm/_channel/shared_memory.py:841-931`) | `loop`; `files`: `[{"file", "version", "record", "lines", "deleted"}]`; `listing` |
| `waited` | a `swarm_wait` ends (`src/inspect_swarm/_bus/_wait.py:250-268`) | `loop`; `outcome`; `files`: `[{"file", "version", "deleted"}]` and `ended`: `[{"member", "status"}]`, what the result named; `woken_by` |

- Versions of a file are consecutive: a file is created at version 1 and every write that touches it, a delete included, adds one ([swarm-memory-channel.md The board's state](swarm-memory-channel.md#the-boards-state)). A file id is never reused, and renames keep it.
- A `view` marks the version it shows and writes `read` in the same synchronous call, and a write's `apply()` and its `delivered` event run with no `await` between them, so the order of `delivered` and `read` events in the transcript is the order in which the board changed and was read.
- `woken_by` lists the commits that released a wait for news. A wait that finds news already waiting returns at once with `woken_by: []` and the files it named in `files` *(spike)*; a release whose news has gone by the time the waiter resumes keeps its commit in `woken_by` though the result never named it ([swarm-wait.md What a wait returns](swarm-wait.md#what-a-wait-returns)).
- A cancelled wait records `files: []` and `ended: []`, since its result never reached the model (`_wait.py:246-249`).

### What the runtime keeps

`EventLog` (`_ledger.py:273-337`) is subscribed to the transcript for the swarm's interval and keeps each span's parent and the set of agent spans, each `ModelEvent` as it last stood (span, cache, error, pending, usage) by uuid, tool events, intermediate scores, and compact `comm` and `waited` records. It keeps no event order across kinds, no span ends, and none of the comm fields above beyond kind, member, channel, record id, rejection, payload size and `listing` (`CommRecord`, `:247-257`; `WaitRecord`, `:260-266`). The metrics are computed in `SwarmRun.run()`'s `finally`, once every member has ended, from the ledger, the event log, the member spans and the record's answer (`src/inspect_swarm/_controller/_control.py:452-509`).

### Who shares what outside the bus

What the swarm configuration declares, all of it in real use:

- **The sample's sandbox**, shared by default by every member; `channels=["filesystem"]` describes it, and with `channels=[]` it is still shared (`swarm()`'s docstring of `channels`, `src/inspect_swarm/_swarm.py:53`). `examples/shared_sandbox.py` uses it.
- **Per-member sandboxes**, with `filesystem(shared_volume=True)` for a volume they all mount, checked by `_check_member_sandboxes()` (`_swarm.py:313-350`); and **`visible_sandboxes`**, documented for "a shared service" every member may use (`_member.py:222`). `examples/proof_swarm.py`, the shape of Epoch's port, gives each member its own container and one shared service, a `comparator` that checks answers, and its members "share what they learn on a `shared_memory()` board, the only channel between them: they share no files".
- **Channels.** The built-ins are `shared_memory` (bus evidence) and `filesystem` (instructions only). Any other `@swarm_channel` is a custom channel whose evidence, if any, this metric does not know.
- **Controllers.** `leaderless()` and the example `until_proved()` start every member at once with the swarm's input (`src/inspect_swarm/_controller/leaderless.py:35-41`, `examples/stop_on_proof.py`). `SwarmControl.start()` also accepts an input of the controller's own, and a controller may start a member after another has ended (`_control.py:117-132`, `:311-340`); nothing in the repository or its examples does either.

Not declared and not detectable: a tool instance shared by name (`memory(instance="board")`), a store key that custom tools share, an outside service, or a service in `visible_sandboxes` that keeps what one member sends it for another.

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

 values:  a1..a6 = 1..6    b1 = 1   b2 = max(b1, a5) + 1 = 6   b3 = max(b2, a6) + 1 = 7
          c1 = 1   d1..d3 = 2..4   e1..e5 = 2..6   c2 = max(c1, d3, e5) + 1 = 7   c3 = 8
```

B answers. M1 gives `steps_to_answer` 3 and `longest_member_steps` 11 (C's 3 + 3 + 5). The path gives `critical_steps_to_answer` 7 (A's six steps, then B's view and submit) and `critical_steps` 8 (C's three steps plus its slowest child's five).

### The graph

**Nodes** are steps, as M1 counts them: completed model calls in a member's spans, cache replays included. Each has a weight of one.

**Loops.** Every agent span in a member's span tree is a loop; the member's span is its root loop. A step belongs to the nearest agent span enclosing its `ModelEvent`'s span (`_agent_span()`, `_metrics.py:290-293`), so a model call a tool makes inside the loop's tool span (a model-graded check in `react()`'s attempts) is a step of that loop, as M1 counts it. A loop's steps are ordered by when their calls began.

**Anchors.** Something that happens in a loop L (a tool call's evidence, a nested loop) is placed in L by its **anchor**: the span on its path to L that is a direct child of L, such as the tool span of the call. For a loop L and an event position x:

- `before(L, x)`: the value of L's last step that began before x; if none, `entry(L)`.
- `after(L, x)`: L's first step that began after x; if none, nothing.

**Values.** `value(s)` is the length, in steps, of the longest chain ending at step s:

```
value(s) = 1 + max(
    the previous step of s's loop, or entry(loop) for its first step,
    exit(C)       for each nested loop C joined into s,
    source(w)     for each write w that a read or wait consumed at s showed,
    exit(member)  for each member end named by a wait consumed at s,
)
source(w) = before(W, anchor(w).begin)                # W: the loop that made write w
entry(root of a member) = 0
entry(C) = before(P, anchor(C).begin)                  # fork: P is C's enclosing loop
C is joined into after(P, C.end)                       # join, when that step exists
exit(L) = max(value of L's last step, or entry(L) without one,
              exit(C) for each nested loop C joined into nothing)
exit(member) = exit(root of the member)
```

**Edges, by question.**

1. **Concurrent loops of a member: one chain per loop, with fork and join edges**. A nested loop forks from the step that started it, `before(P, anchor.begin)`, which for a `general()` child is the generation that proposed its `agent(...)` call, synchronous or background, since the child's span lies under that call's tool span whenever it begins. It joins into the parent's first step that begins after the nested loop's span ends. For a synchronous child that is exactly the next generation, which holds the child's result. For a background child it is the parent's first generation after the child finished, which carries deepagent's completion notice; the parent may fetch the result later with `agent_status`, so the join can be early, which only lengthens chains through the parent. A nested loop that ends after its parent's last step joins nothing and reaches the swarm's metrics through `exit()`. Two children forked in one turn and joined into the next both feed that step, so the step takes the slower: the parent's steps plus the slowest child's per stage, which is Kimi's CriticalSteps. A loop with no step of its own passes its entry to its own nested loops and their exits to its parent.
2. **Endpoints**. A write's source is the generation that proposed the call: `source(w)`, the last step of the writing loop W that began before the call's tool span, whose `delivered` event is w. A read's or wait's dependence starts at `after(L, anchor(event).end)` in the reading loop L: the first generation whose input holds the tool result. A read in a loop that makes no later step consumed nothing and adds no edge. Steps are matched by order, never by tool call id.
3. **Which reads make edges**:

   | What the loop was shown | Edge into the next step, from |
   |---|---|
   | `view` of a file, full or with `view_range` | every write in the shown version's history (versions 1 to the shown one of that file id) by a loop other than the reader; in practice the latest such write of each writing loop, since a loop's earlier writes precede its later ones on its own chain |
   | `view` of a deleted file's id | the deleting write: the reader is shown that the file was deleted and by whom, not its content |
   | `view` of a directory (listing) | none |
   | the status line, `changes` | none (no evidence, and the step that sees them was not blocked on them) |
   | `swarm_wait` result | the write that produced each version its `files` names; each member its `ended` names (`exit(member)`); nothing from a cancelled wait |

   A partial view links the whole version, as [swarm-memory-channel.md Evidence](swarm-memory-channel.md#evidence) says: the evidence records the range, but which lines came from which write would need the board replayed line by line, and over-approximating an edge errs towards a longer path. A listing shows ids, versions, writers and peer-chosen paths of up to the whole board; linking every listed file's writer would tie the reader to nearly every earlier step and make the path the run's length, so listings, which the metrics already count, make no edge. A loop that acts on a listing views what it needs, and that view makes the edge.

   **A wait is an edge, from what its result named, not from `woken_by`.** A wait blocks the loop until a write or an end it cares about, so the waiter's next step depends on it as much as on a view; that step usually views the file, which adds the content edge one step later, and the extra step is a real cost of the wait-then-view protocol. `files` and `ended` are what reached the model. `woken_by` is not: a wait that finds news at once has `woken_by: []`, and a release whose news vanished keeps a commit the result never named. `woken_by` stays what [swarm-wait.md](swarm-wait.md#evidence) uses it for, joining idle time to the commits that ended it.
4. **Every edge points forward in the transcript.** A write's source began before the write's tool span, which holds its `delivered` event; any read or wait result that shows its version comes after that event, inside the reader's tool span, and the reader's next step begins after that span ends; a fork's source began before the nested loop's anchor and a join's target after the loop's end; a member's end follows all its steps. So the graph is acyclic, and one pass over the steps in begin order computes every value from values already known.

### The metrics

```python
# src/inspect_swarm/_metrics.py, SwarmMetrics
    # critical path, the dependency-linked definition: the longest chain of steps
    # (completed model calls, cache replays included) in which each step follows the
    # previous step of its agent loop, a nested loop's last step joins into its
    # parent's next step, and a step follows the writes and member ends that a read or
    # wait of its loop showed it (_critical_path.py). None in metrics written before
    # these fields.
    critical_steps: int | None = None
    # the longest such chain ending at the answer: at the last step of the answering
    # member's own agent loop (its root loop's exit when that loop made no step); None
    # without an answer
    critical_steps_to_answer: int | None = None
    # how members could share information outside the swarm's evidence, as far as the
    # swarm knows; [] when it knows of none. A chain through any of these is missed.
    critical_path_unobserved: list[str] | None = None
```

- **Unit: steps**. A step is the unit of M1's counts and of Kimi's CriticalSteps, and it is independent of provider latency and of `max_connections`. A path's span in seconds, from its first step's start to the answer, is about the answer's own time, which the record already has (`MemberResult.time`); a sum of call durations along the path mixes connection waits and retries into it, which plan decision 12 ruled out for per-member timing until it can follow Inspect's working-time accounting ([swarm-implementation-plan.md Decisions](swarm-implementation-plan.md#decisions), item 12).
- **Which path**. Both, as M1 has both: `critical_steps_to_answer` is the measure latency curves are interpreted by, and `critical_steps` describes the swarm's depth, with or without an answer. The answer's step is the last step of the answering member's root loop: under `first` it is the generation that submitted, and background work drained after it is left out, as it did not contribute to the answer (M1's `steps_to_answer` counts it). A member whose root loop made no step (an agent that runs its whole loop as a nested agent) uses its root loop's `exit()`. With no answer, `critical_steps_to_answer` is `None`; with no step in the swarm, `critical_steps` is 0.
- **The old fields stay** with their meaning, and their comment is corrected: they bound each member's own sequential depth from above, not the swarm's. The new numbers are not bounded by them: a chain through two members can be longer than either member's step count, and a member's parallel children make its step count larger than its chain.

### What the swarm does not see

The list, in this order, holds:

| Entry | When |
|---|---|
| `"sandbox"` | the members share the sample's sandbox and the sample has a sandbox environment when the swarm starts (`sandbox_environments_context_var`, as `_check_sandbox_names()` reads it, `_swarm.py:353-354`) |
| `"shared_volume"` | the `filesystem` channel is open with `shared_volume=True` |
| `"channel:<name>"` | each open channel other than `inspect_swarm/shared_memory` and `inspect_swarm/filesystem`, by its registered name (`registry_info(channel).name`) |
| `"controller"` | the controller started a member with an input of its own, or after another member had ended |

A service in `visible_sandboxes` is not listed. The option exists for services every member uses, such as `proof_swarm.py`'s comparator, whose arm is meant to be board-only; flagging it would mark the swarm shape the board was built for as never exact. A service that carries information between members is, for this metric, like an outside service: not detected, and the docs say so.

The metric is always computed and always reported with this list; it is never refused or omitted. An empty list means the swarm knows of nothing outside its evidence, not that nothing exists: the metric's comment and the user docs say that a shared tool instance, a shared store key, an outside service and a shared service in `visible_sandboxes` are not detected. A missed channel can only remove edges, so with a non-empty list the path is a lower bound on the dependencies it would have shown, while its over-approximations (whole-version views, early background joins) lengthen it; analysis that needs the exact claim keeps the arms with an empty list. The default configuration, members sharing the sample's sandbox, always reports `["sandbox"]`, and an arm that wants an exact path gives members their own sandboxes or none.

Why report rather than refuse: refusing would leave the default configuration and every shared-sandbox arm with no number, though the edges the bus records hold there too, and a filesystem-only arm's path, its loops' own chains, is still the honest lower bound. The ledger makes the same kind of claim for cost (`total_complete`, `attribution_complete`), and analysis already filters on such flags.

### Where it is computed

**At run end, as a per-sample metric**, beside M1's, in the same `SwarmMetrics` event and store entry, so every new run carries it and scorers can read it. Everything it needs is seen by the runtime: the `EventLog` is subscribed for the whole interval, in first-report order, and the metrics are computed after every member has ended. The logs hold the same events in the same order, so the computation is a pure function over compact records that an `EventLog` builds from events: fed the runtime's stream or, later, a logged sample's `events`, it gives the same result, which the tests check.

### Code

**`src/inspect_swarm/_critical_path.py`** (new, private):

```python
# What the critical path reads from the transcript, in the order the transcript first
# reported each event. Filled by EventLog; positions are its own counter.
class PathLog:
    spans: dict[str, PathSpan]          # every span: parent, agent or not, begin and end positions
    steps: dict[str, tuple[str | None, int]]   # model event uuid -> (span id, begin position)
    writes: list[PathWrite]             # `delivered`: position, span, loop, [(file, version, deleted)]
    reads: list[PathRead]               # `read` without `listing`: position, span, loop, [(file, version, deleted)]
    waits: list[PathWait]               # `waited`: position, span, loop, [(file, version)], [ended members]

    def __call__(self, event: Event, position: int) -> None: ...

@dataclass(frozen=True)
class CriticalPath:
    steps: int                   # critical_steps
    to_answer: int | None        # critical_steps_to_answer

def critical_path(
    log: PathLog,
    completed: Callable[[str], bool],    # whether a model event uuid is a step (EventLog.calls, as count_call() counts it)
    places: Callable[[str | None], tuple[bool, str | None]],  # SpanPlaces: the member whose spans hold a span
    member_spans: Mapping[str, str],     # each started member's span, its root loop
    answer: str | None,
) -> CriticalPath: ...

# src/inspect_swarm/_controller/_control.py
def _unobserved(
    records: Sequence[tuple[Member, str]], channels: Sequence[Channel]
) -> list[str]: ...
```

- **Recording.** `EventLog.__call__` numbers each event the first time the transcript reports it (by uuid; an update keeps its number) and passes span begins and ends, model events and `comm` events to its `PathLog`. The `PathLog` keeps only ids, positions, loop ids, file ids and versions, and member names: nothing a member wrote. `CommRecord` and `WaitRecord` are unchanged.
- **Computing.** `critical_path()` builds the loops of each member from `places` and the span parents, orders each loop's steps (model events in that loop that `completed()` accepts) by begin position, and then walks every step, write, read, wait and span end in position order once. It keeps, per file id, the latest write of each loop to the file's current version, so a read costs one entry per writing loop; per loop, the reads and waits whose next step has not come yet; and per nested loop, its entry and, once it ends, its exit waiting for its join. A tombstone view or a wait takes the write that produced the named version from a map of `(file, version)` to write. Events of spans outside every member (the controller's, the final step's) are ignored. A read whose shown version differs from the file's current version at its position, or a version no write produced, would mean the ordering assumption broke; the function raises `RuntimeError` naming the file and versions, which the tests guard against, rather than computing a wrong path.
- **Unobserved.** `_unobserved()` runs once when the swarm starts, in `SwarmRun.run()`, from the member records, the resolved channels and the sample's sandbox environments. `SwarmRun.start()` sets a flag when it starts a member with its own `input` or after a member has ended, and at run end the list gains `"controller"` if the flag is set. `_critical_path.py` takes `places` as a callable so that it imports nothing from `_ledger.py`, which imports it.
- **Wiring.** `swarm_metrics()` takes `member_spans` (the `spans` mapping `_control.py` already builds for `places`) and the unobserved list, calls `critical_path()` with the event log's `PathLog` and `completed` built from `EventLog.calls`, and fills the three fields. Everything stays synchronous, as the rest of the `finally` is, so the metric is written under cancellation too.

**Configuration.** None: the metric is always on, as the other metrics are.

**Errors.** Nothing here touches the members or the run's outcome. The `RuntimeError` above is an internal invariant; the metrics are written in the `finally`, where an error would surface as any bug in the metrics would today.

## Alternatives considered

- **One chain per member**. Each member's steps in begin order, cross-member edges only. It equals M1's count for a member with no edges, so it charges a deepagent member every parallel child, the problem this metric exists to fix. Per-loop chains cost only the fork and join rule, which the span tree supports directly.
- **Joining a background child at the parent's `agent_status` or `agent_wait` result that returns it.** Exact, but it parses deepagent's tool results, which no evidence describes, and ties the metric to one agent's tools. The parent's first step after the child ends carries deepagent's notice anyway, and the error only lengthens chains through the parent.
- **`woken_by` as the wait's edge.** Misses waits that returned at once (`woken_by: []`) and adds commits whose news never reached the loop. `files` and `ended` are what the result showed.
- **No edge for waits, only for views.** Leaves a waiter's step after a wake independent of the write that woke it, so a wait-then-view hand-off counts one step fewer than it took.
- **Listings as edges.** Every listed file's writer would feed the reader, so a root listing ties a member to nearly all earlier work and the path tends to the run's length.
- **Refusing or omitting the metric when the swarm cannot see every channel**. The swarm can never know it sees every channel (shared tool instances are invisible), so a refusal would rest on what it can detect anyway, and it would drop the metric for the default configuration. A claim beside the number keeps both.
- **A wall-clock path, or both units**. See [The metrics](#the-metrics): the elapsed time along any chain ending at the answer is the answer's time, already recorded, and a duration-weighted path needs per-member working-time accounting the plan has deferred.
- **An analysis helper over logs instead of a run-end metric**. It works on logs written before this change, but every arm would then need the user to call it, scorers could not read the value, and it would be the only swarm metric not in `inspect_swarm:metrics`. The pure function keeps that helper a small addition when wanted ([Not this design](#not-this-design)).
- **Adding the step's index or the proposing tool call id to the evidence.** Would make endpoints explicit, but the order of events already determines them for every supported loop shape, and keeping the evidence as it is leaves every channel and log reader untouched.

## Compatibility and migration

- **`SwarmMetrics`** gains three optional fields with `None` defaults, so metrics written before load unchanged; `version` stays 1, as for the fields added since M1. The `metrics` `InfoEvent` and the `inspect_swarm:metrics` store entry carry them.
- **Evidence, the bus, channels, the ledger, the result record:** unchanged. `EventLog` and `CommRecord` are private.
- **Public API:** no new names. `SwarmColumns` is unchanged (it reads the ledger, not the metrics).
- **Docs:** [Analysis, Metrics](https://meridianlabs-ai.github.io/inspect_swarm/analysis.html#metrics) gains the three fields, what `critical_path_unobserved` can and cannot detect, and that latency is read from `critical_steps_to_answer`.
- **Old runs** have no critical path; recomputing it from their logs is the helper left out below.

## Security

No untrusted text reaches the computation. It reads span ids, positions, model event status, and from the swarm's own evidence loop ids, file ids, integer versions and roster names; paths, payloads and file contents are neither stored in the `PathLog` nor read. Model output reaches it only as the tool calls that made the evidence, which the channel validated, and a model cannot write an `InfoEvent`. Agent code can (any code may call `transcript().info()` with source `inspect_swarm`), but member and tool code is the eval author's and trusted, as for every other metric read from evidence. Work is linear in the events of the interval plus one entry per writing loop per read, so a member that views a file often costs the metric no more than its own evidence costs the log.

## Testing

All async tests run on asyncio and trio through the `anyio_backend` fixture.

- **`tests/test_critical_path.py`**: table-driven tests of `critical_path()` over hand-built `PathLog`s: a single loop (value equals its steps); two members with a view (the example above); a wait that returned at once (`woken_by: []`, edge from `files`); a wait released by a write whose news vanished (no edge); a wait naming an ended member (edge from its exit); a listing and a read in a loop's last turn (no edge); a tombstone view (deleting write only); a version history written by two loops (an edge from each); parallel synchronous children (max, not sum); a background child that ends before, and one that ends after, its parent's last step; a nested loop with no steps passing its children through; errored and cache-replayed model events; `critical_steps_to_answer` with no answer and with an answer whose root loop has no step.
- **`tests/test_metrics.py`**: mockllm swarms through `run_swarm()`, asserting the three fields: two `react()` members, one writing and one waiting and viewing (values as in the example); a deepagent member with two parallel `general()` children and one with a background child; a `filesystem`-only swarm in a shared sandbox (`critical_path_unobserved == ["sandbox"]`, the path equals the members' own chains); a controller that starts a member after another ended (`"controller"`); a custom channel (`"channel:<name>"`); per-member sandboxes with `shared_memory` and a service in `visible_sandboxes`, as `proof_swarm.py` runs (`[]`); `filesystem(shared_volume=True)` (`"shared_volume"`).
- **Runtime and log agree**: one swarm run through `eval_async()` whose logged sample's `events`, fed to a fresh `EventLog`, give the same `critical_path()` as the metrics the run wrote. This guards the ordering assumption and the later log helper.

No test needs Docker, a network or a model provider: the shared-sandbox case uses the `local` sandbox, as `tests/test_background_members.py` does, and the per-member cases the stand-in environments of `tests/test_member_sandboxes.py`.

## Implementation plan

1. **The path and its recording.** `src/inspect_swarm/_critical_path.py` (`PathLog`, `critical_path()`), `src/inspect_swarm/_ledger.py` (`EventLog` numbers events and feeds its `PathLog`), `tests/test_critical_path.py`.
2. **The metric.** `src/inspect_swarm/_metrics.py` (the three fields, the corrected comment on the old ones, `swarm_metrics()`'s new arguments), `src/inspect_swarm/_controller/_control.py` (`_unobserved()` at start, the controller flag in `start()`, the arguments at run end), `docs/analysis.qmd` (Metrics), `tests/test_metrics.py` with the runtime-and-log test.

## Open questions

1. **Report with a list, or refuse, for arms the swarm cannot fully observe?** The design always reports and lists what it knows is unobserved ([What the swarm does not see](#what-the-swarm-does-not-see)); the default shared-sandbox configuration then always reports `["sandbox"]`. The alternative omits the metric (`None`) whenever the list is non-empty. Recommendation: report with the list, so filesystem and shared-sandbox arms still get a lower bound and the filter is the analyst's.
2. **Is the answer path the latency measure?** The design makes `critical_steps_to_answer` the measure latency curves are interpreted by, ending at the answering member's last own-loop step and leaving out work drained after it, and keeps `critical_steps` for the swarm's depth. Recommendation: yes; `critical_steps` alone would charge an answer for work that never reached it.

## Not this design

- **An analysis helper over logs**, such as `swarm_critical_path(sample)` in `inspect_swarm.analysis`, feeding a sample's `events` to an `EventLog` and calling `critical_path()`, for logs written before the metric. Small once the metric exists; worth filing if older runs need the number.
- **The path itself**, as the members and step counts along it, for explaining a number; the metric records lengths only.
- **`SwarmColumns` for metrics.** The columns read the ledger; metrics, the critical path included, are read from the store entry today.
- **The messages channel's edges.** Its `read` evidence will name records rather than file versions; its design adds its rule here.
- **Edges for a member started by a controller** with another member's output: the list flags such starts, and a controller that hands members their peers' results would need its own evidence.
- **A loop running concurrent generations in one agent span** (a custom agent, or a bridged CLI agent's own subagents) is one chain, ordered by when calls began, so its path counts every call, as M1 does.
