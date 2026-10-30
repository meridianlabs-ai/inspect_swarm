# Inspect Swarm: the dependency-linked critical path

## Overview

When a swarm ends, its metrics gain `critical_steps_to_answer`: the number of model calls (steps) on the longest chain of steps that the answer depended on. Each member's own steps, its loop's, its subagents' and its background work's, form one chain in the order they began, as the existing step counts treat them. Chains link between members only where the swarm recorded a dependency: a shared-memory read or a `swarm_wait` result follows the writes it showed, and a wait that names a member's end follows that member's whole chain. The chain ends at the answering member's end, after its drain, where its output, the one the controller selects, is settled.

It is the measure latency curves are interpreted by, beside the existing `steps_to_answer` and `longest_member_steps`, which stay as they are. It is computed only at run end, from the events the swarm's event log sees as they happen, with no change to the bus or the evidence. It reads board evidence only from the built-in shared-memory board the runtime opened for this swarm, which it knows from its own configuration, not from a channel's name. It has two plain limits, which pull in opposite directions. Inside a member it over-counts: concurrent subagents, background work and tool calls of one turn count one after another, the answering member's work drained after its agent returned counts whether or not it changed the answer, and that excess reaches peers through the member's later writes. Across members it misses links: it sees only shared memory and waits, so information that flows another way, such as a shared sandbox, adds no link, which can only lower it. So it is not a bound on true causal depth either way; it is the longest chain in the graph it defines.

## Background

This is the "dependency-linked critical path" of [swarm.md Metrics](swarm.md#observer-evidence-accounting-and-metrics) and of the plan's [Future work](swarm-implementation-plan.md#future-work), which latency curves wait for (decision: Ransom, 2026-10-14). The existing step counts are defined in the user docs ([Analysis, Metrics](https://meridianlabs-ai.github.io/inspect_swarm/analysis.html#metrics)). The shared-memory channel ([swarm-memory-channel.md](swarm-memory-channel.md#evidence)) and `swarm_wait` ([swarm-wait.md](swarm-wait.md#evidence)) write the evidence this metric reads.

## Why

A swarm's latency question ([swarm.md](swarm.md), "What does a swarm buy in latency?") needs to know how many sequential model calls the answer took, given that members work in parallel but wait on and build on each other. The existing counts cannot say, because neither sees members' dependencies (`src/inspect_swarm/_metrics.py:88-96`, `:221-224`). A member that waits for a peer's lemma and then proves the theorem in three steps has `steps_to_answer` 3, though the answer could not exist before the peer's ten steps. The field comment's claim that both counts "bound the sequential depth from above" holds for each member alone, not for the swarm.

The evidence that closes the gap is already written: every board write's commit (`delivered`, with each file's new version), every view (`read`, with the version shown), every wait (`waited`, with the files and ends its result named).

## Goals and non-goals

Goals:

- One per-sample metric, in steps, of the longest dependency chain between members ending at the answer, computed when the swarm ends and written with the other metrics.
- No change to the bus, the channel or the evidence format.
- A plain statement of what the number over-counts and what it cannot see.

Non-goals:

- Resolving concurrency inside a member: a member's steps are one chain, as the existing counts treat them (decision: Ransom, 2026-10-29; [Alternatives](#alternatives-considered), [Not this design](#not-this-design)).
- The longest chain anywhere in the swarm: overall depth stays with the existing per-member `longest_member_steps` (decision: Ransom, 2026-10-28).
- A list of the unobserved ways members could share information (decision: Ransom, 2026-10-28; [Alternatives](#alternatives-considered)).
- A wall-clock critical path ([The metric](#the-metric)).
- Recomputing the metric from a saved log, by replay or by an analysis helper ([Not this design](#not-this-design)).
- Edges for channels other than the built-in shared-memory board, a custom channel named `shared_memory` included; the later messages channel brings its own rule.
- Line-level attribution of a partial view.

## Current behaviour

### What a step is and whose it is

- **A step** is a completed model call in a member's spans, cache replays included and errored or cancelled calls excluded: `calls.calls + calls.cache_replays` of the member's ledger line (`_metrics.py:285-287`), counted by `count_call()` (`src/inspect_swarm/_ledger.py:532-550`). `SpanPlaces` attributes a span to the member whose span tree holds it (`_ledger.py:490-515`), so a member's steps include its subagents', its background work's (deepagent background children and raw `background()` model calls, `tests/test_background_members.py:104-131`) and those of any swarm nested inside it.
- **A member's span** is an agent span opened by the member wrapper around its agent (`src/inspect_swarm/_member.py:516-520`). It stays open through the member's drain: after the agent returns, `_drain()` closes the member's lever, so its background work dispatches no new provider calls, and waits the grace period for calls and tool calls in flight (`:533`, `:553-566`). A cache replay is not dispatched and returns before Inspect checks limits (`inspect_ai/model/_model.py:1525-1558`), so background work can still begin cache-replayed steps during the drain. Nothing is written to the transcript when the agent returns. The wrapper keeps the state the agent returned through the drain and passes it to the controller's end callback afterwards (`_member.py:465`, `:520-533`), and the controller selects the answer from an ended member's `agent_state.output` (`_controller/_control.py:565-568`, `:627-629`), so drained background work can still change the selected output, by replacing `state.output` with what it read, for example.
- **Concurrency inside a member** is common: deepagent runs `general()` children in parallel and in the background, and wrappers run `run()` children in a task group (`tests/test_metrics.py:855`). The transcript records each child's agent span and the event's span, not which task started a child or where its caller awaited it: a task-group join writes no event.

### Order

The transcript notifies its subscribers synchronously when it first reports an event, before counting it, and again on each update (`inspect_ai/log/_transcript.py:691-713`, `:762-807`); so during the first notification `transcript().history.event_count` is the event's index, the count `MemberTimelines` already reads (`src/inspect_swarm/_evidence.py:216`). A `ModelEvent` is first reported when its call begins.

There is one other way an event reaches subscribers: events restored from a checkpoint are appended and counted before they are notified (`_extend_restored_events()`, `_transcript.py:716-760`), so during their notification the count is one past their index, and the next ordinary event reads the same count. Their only caller is checkpoint hydration when a sample resumes (`inspect_ai/util/_checkpoint/hydrate.py:689-693`), which runs when the sample's first `react()` enters `checkpointer()` (`inspect_ai/agent/_react.py:213`, `inspect_ai/util/_checkpoint/checkpointer_impl.py:132-142`), inside a member and so inside the swarm's interval. The restored events are the earlier attempt's, its board evidence included, about a board this run does not have. The ledger already records whether the sample resumed (`sample_resumed()`, `_ledger.py:567-573`; `SwarmLedger.resumed`).

### The evidence

Each is an `InfoEvent` with source `inspect_swarm` and `type: "comm"`, written in the tool span of the call that acted, with `member`, the acting member, and `loop`, the acting loop's agent span (`src/inspect_swarm/_evidence.py:46-167`):

| Kind | Written | Fields the metric reads |
|---|---|---|
| `delivered` | step 4 of `Bus.deliver()` (`src/inspect_swarm/_bus/_bus.py:185-190`) | `channel`, `member`, `loop`; from each entry of `result.files`, `file`, `version` (the version the file now has) and `deleted` when present. Entries also carry `base` and `path`, or for a rename `old_path` and `new_path` and no `deleted` (`shared_memory.py:215-224`); the metric reads none of these. A rename of a directory has one entry per file it moved, and every entry is a write |
| `read` | a `view` returns (`src/inspect_swarm/_channel/shared_memory.py:841-931`) | `channel`, `member`; `files`: `[{"file", "version", "record", "lines", "deleted"}]`; `listing` |
| `waited` | a `swarm_wait` ends (`src/inspect_swarm/_bus/_wait.py:250-268`) | `member`, `loop`; `channels`, the waiting channels the loop reaches; `files`: the board's `[{"file", "version", "deleted"}]` and the messages channel's `[{"record", "from"}]`, and `ended`: `[{"member", "status"}]`, what the result named |

- Versions of a file are consecutive: a file is created at version 1 and every write that touches it, a delete included, adds one ([swarm-memory-channel.md The board's state](swarm-memory-channel.md#the-boards-state)). A file id is never reused within a board, and renames keep it. Each run's board numbers its files from `f-1` (`shared_memory.py:276`, `:712`), so a nested swarm's board reuses ids the outer board has.
- A `view` marks the version it shows and writes `read` in the same synchronous call, and a write's `apply()` and its `delivered` event run with no `await` between them, so within one board the order of `delivered` and `read` events in the transcript is the order in which the board changed and was read.
- A wait that finds news already waiting returns at once with `woken_by: []` and the files it named in `files`; a release whose news has gone by the time the waiter resumes keeps its commit in `woken_by` though the result never named it ([swarm-wait.md What a wait returns](swarm-wait.md#what-a-wait-returns)). A cancelled wait records `files: []` and `ended: []` (`_wait.py:246-249`).
- **A wait's `files` do not say which channel named them.** Every waiting channel the loop reaches adds its `WaitNews.files` to one list (`_wait.py:225-230`), and the public `WaitNews` contract lets a custom waiting channel name entries of its own shape (`src/inspect_swarm/_channel/_channel.py:162-170`); `channels` lists the channels reached, not which supplied each entry (`_evidence.py:135-156`). In this repo `shared_memory` and `messages` are the waiting channels; their entries differ in shape, since only a message entry names a `record`, and a member's own loop reaches both when the swarm has both. `delivered` and `read` carry their `channel`.
- **A channel's name does not say which implementation it is.** A channel's record name (`Channel.name`, what evidence carries as `channel`) is unique within a swarm: `_check_bus_channels()` refuses two channels with the same record name or prefix (`src/inspect_swarm/_swarm.py:273-296`); `_channels()` separately refuses a channel given twice by its registry name (`:261-270`). No record name is reserved: a custom channel may call itself `shared_memory` (`_channel/_channel.py:35-39`). The runtime knows each open channel's state by name (`Bus.states`, `src/inspect_swarm/_bus/_bus.py:83`, `:93-95`), and the built-in board's state is a `_SharedMemoryState`, which only `shared_memory()`'s channel opens (`shared_memory.py:84-104`, `:275`). The bus opens every channel when the run is built, before `SwarmRun.run()` starts the interval (`_control.py:262-267`, `:382`).
- A member's end is a `result` event of kind `member_result` naming it, written in its span when it ends, after its drain (`src/inspect_swarm/_controller/_control.py:586-608`).
- **Which run an event belongs to.** Member names are unique in a sample, since each names a timeline and a second swarm reusing one is refused (`_evidence.py:208-216`); the communication metrics already rely on this to keep another swarm's events out (`_metrics.py:229-231`). A nested swarm's members run inside an outer member's span (`tests/test_background_members.py:1090-1100`), so their steps are the outer member's, but their evidence names the inner members.

### What the runtime keeps

`EventLog` (`_ledger.py:273-337`) is subscribed to the transcript for the swarm's interval and keeps each span's parent and the set of agent spans, each `ModelEvent` as it last stood by uuid, tool events, intermediate scores, and compact `comm` and `waited` records. It keeps no event positions and none of the comm fields above beyond kind, member, channel, record id, rejection, payload size and `listing` (`CommRecord`, `:247-257`; `WaitRecord`, `:260-266`). The metrics are computed in `SwarmRun.run()`'s `finally`, once every member has ended, from the ledger, the event log, the span places and the record's answer (`_control.py:452-509`).

### Ways members share information outside the evidence

All in real use: the sample's sandbox, shared by every member by default (`swarm()`'s docstring of `channels`, `src/inspect_swarm/_swarm.py:53`; `examples/shared_sandbox.py`); `filesystem(shared_volume=True)`; a service in `visible_sandboxes`, such as `examples/proof_swarm.py`'s comparator; custom channels. A controller can also start a member with an input of its own, or after another has ended (`_control.py:117-132`); `leaderless()` and the examples start every member at once with the swarm's input. Shared tool instances, shared store keys and outside services are possible too.

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

 C (deepagent)  c1 → d1 → e1 → d2 → e2 → d3 → e3 → e4 → e5 → c2 → c3
                c1 starts two general() children, d and e, which run at once; their steps
                interleave in the transcript and count one after another

 values:  a1..a6 = 1..6    b1 = 1   b2 = max(b1, a5) + 1 = 6   b3 = max(b2, a6) + 1 = 7
          c1..c3 with d and e = 1..11
```

If B answers, `critical_steps_to_answer` is 7 (A's six steps, then B's view and submit) where `steps_to_answer` is 3. If C answers, it is 11, as `steps_to_answer` is: the parallel children count one after the other, where their slower branch alone would give 8.

### The graph

The metric is the longest path in a graph of the members' recorded events.

**Positions.** Every model call and evidence event has a position: its index in the transcript, as defined above.

**Nodes**, each belonging to one member:

| Node | Member | Weight |
|---|---|---|
| a step (a completed model call, cache replays included), at the position where its call began | the member whose span tree holds its span (`SpanPlaces`) | 1 |
| a write (`delivered`) or a read (`read`, not a listing) of the built-in board, a wait result (`waited`) | its `member`, when that is in the roster | 0 |
| the member's end (`member_result`) | the member it names | 0 |

**Edges.** Every edge goes from a node to a node with a later position:

1. **A member's chain.** Each node follows the member's previous node. A member's steps, from every loop, subagent and background task it runs, are one sequence in the order they began, as the existing counts treat them.
2. **Writes to reads.** A read or wait result follows the writes it showed:

   | What the loop was shown | Edges from |
   |---|---|
   | `view` of a live file, full or with `view_range` | every write in the shown version's history (versions 1 to the shown one of that file id) |
   | `view` of a deleted file's id | the deleting write: the reader is shown that the file was deleted and by whom, not its content |
   | `view` of a directory (listing) | none |
   | the status line, `changes` | none (no evidence, and the step that sees them was not blocked on them) |
   | `swarm_wait` result | for each version its `files` names, the latest write up to that version by a loop other than the waiter's: the peer write the result reports, since when the waiter's own blind append made the current version, the result names that version but reports the peer write before it (`news()` and `_by_others()`, `shared_memory.py:426`, `:973-982`); only when its `channels` include the built-in board and no channel other than the built-in messages channel, whose entries name a `record` and are skipped, so that every other entry is the board's; none from a wait that reached a custom waiting channel, or a cancelled wait. The evidence names at most the first ten files the result reports (`_LISTED_IDS`, `shared_memory.py:48`, `:426-439`), though the result's text counts them all and names every writer, so files past the tenth make no edge until a view names them |

   An edge from the reader's own member adds nothing, since its chain already passes through that write.

   **The built-in board, by instance.** The board is the open channel whose state is the built-in `_SharedMemoryState`, found from the bus's states when the run starts, not from the name in an event. Since names are unique within a swarm, the events that carry that channel's name are that board's. When the swarm has no built-in board, there is no board evidence at all: a custom channel's `delivered` and `read` events, whatever its name, make no node, and its wait entries no edge, so a custom channel named `shared_memory` is treated as any other custom channel. Member steps and member-end edges count either way.

   A wait that reached a custom waiting channel makes no file edges, alone or beside the board: its `files` mix entries from every channel it reached with nothing to say which named which, and a custom channel's entry can name an id the board also has, or one the board never had. Treating such entries as board versions would link an unrelated writer or turn a valid wait into inconsistent evidence; ignoring them, with no warning, misses that link, as any custom channel's is missed. Its `ended` entries come from the bus, not from a channel (`_wait.py:231-244`), so they still make member-end edges.
3. **Member ends to waits.** A wait result follows the end (`member_result`) of each member its `ended` names, and so that member's whole chain, drained work included.

**The metric.** `critical_steps_to_answer` is the largest number of steps on a path into the answering member's end, its `member_result`: its chain's value there.

**Acyclic.** Every edge points forward in position: a member's chain is in position order, a read follows the `delivered` event of each version it shows, which committed before it, and a wait result follows the writes and the `member_result` events it names. So one pass in position order computes the longest path into every node, keeping one running value per member.

**The view rules.** A partial view links the whole version, as [swarm-memory-channel.md Evidence](swarm-memory-channel.md#evidence) says: the evidence records the range, but which lines came from which write would need the board replayed line by line, and over-approximating an edge errs towards a longer chain. A live view links every write in the version's history, not only the latest, because different members write a file at different depths: a one-step member's append to a ten-step member's file leaves a version whose content depends on both. A listing shows ids, versions, writers and peer-chosen paths of up to the whole board; linking every listed file's writer would tie the reader to nearly every earlier step, so listings make no edge. A loop that acts on a listing views what it needs, and that view makes the edge.

**A wait is an edge, from what its result named, not from `woken_by`.** A wait blocks the loop until a write or an end it cares about, so what the waiter does next depends on it as much as on a view; the waiter usually views the file next, which adds the content edge one step later, and the extra step is a real cost of the wait-then-view protocol. `files` and `ended` are what reached the model. `woken_by` is not: a wait that finds news at once has `woken_by: []`, and a release whose news vanished keeps a commit the result never named. `woken_by` stays what [swarm-wait.md](swarm-wait.md#evidence) uses it for, joining idle time to the commits that ended it.

**This run's evidence only.** An evidence event is a node when its `member` is in this swarm's roster, checked before anything else in the event is read, and a wait's `ended` and a `member_result` count only for roster members. The board's record name is unique only within this swarm: a nested swarm may open a channel with the same name, the built-in board or a custom one, and its events reach the outer subscriber too, so the roster, not the name, keeps them out. A nested swarm's evidence names its own members and is left out, so its board's ids never mix with this board's. Its members' steps are still the outer member's, through `SpanPlaces`, as for the existing counts; only the inner board's edges are missing, which the inner swarm's own metric counts.

### The shapes

| Shape | How the rule handles it | Length |
|---|---|---|
| B in [At a glance](#at-a-glance): wait, view, submit | the wait result follows a5's write, the view follows a5's and a6's writes; B's next steps follow them | 7 |
| C: two `general()` children run at once | one chain in begin order | 11, the sum (over-count) |
| A wrapper's `run(plan)` then `run(solve)` | one chain | every step |
| A one-step member whose tool views a ten-step peer's file, calls a model with it and writes the result | the read raises the member's chain to 10; the tool's model call follows it | 11 at the write |
| A member that views a ten-step writer's file and returns it with no model call after | the read is before its end | 10, the writer's chain |
| Two members appending to one file at depths 10 and 2; a reader views it, then makes one step | the view follows every write in its history | 11 |
| One generation of a one-step member proposes a view of a ten-step peer's file and an independent write; the view runs first; a one-step consumer views the write | the view raises the member's chain to 10 before the write, whose content it did not use | 11 for the consumer (over-count, carried to a peer) |
| A concurrent branch of one member reads a ten-step peer's file; another branch makes the next step | the member's chain passes the read, so the other branch's next step follows it | 11 (over-count) |
| A waiter with one step before and one after a wait naming a member whose four steps end during its drain | the wait follows that member's end | 5 |
| A waiter naming a member that ended by a token limit, or was cancelled, before its agent returned | its `member_result` follows all its steps | that member's chain |
| A wait after the waiter's own blind append | the wait follows the peer write before the waiter's append | the peer's chain |
| The answering member's agent returns a placeholder with no step; its background work, drained, views a ten-step peer's file and replaces the output with it | the view is before the member's end | 10; the selected output holds the peer's file |
| The same drained read, the output left as the placeholder | the same | 10 (over-count: the selected output did not use it) |
| The answering member's background call in flight when its agent returns, completing in the drain | it is before the member's end | counted, as in `steps_to_answer` |
| A wait whose result reports eleven files, ten by a one-step P and the eleventh by a ten-step Q; the waiter, with one step before the wait and one after it, views only P's files | the evidence names P's ten files; Q's write makes no edge until a view of `f-11` | 2, though the text named Q (missed link) |
| A wait reaching shared memory and a custom waiting channel, whose only entry is the custom channel's, naming an id the board has (`f-1`) or not (`f-99`) | no file edges from that wait; its `ended` entries still link | the waiter's own chain, no warning |
| A nested swarm's members inside one outer member | their steps are the outer member's; their board's evidence is left out | the outer member's chain |
| An outer built-in board, and a nested swarm whose custom channel is also named `shared_memory`, delivering `result: {"size": 1}` and waiting on entries such as `{"foreign_record": "x-1"}` | its members are not in the roster, so its events are dropped before their payloads are read | the outer swarm's chains, no warning |

### Where the answer ends

The answer's chain ends at the answering member's end, its `member_result`, which `_end()` writes after the drain, when the wrapper hands the controller the member's final state ([What a step is and whose it is](#what-a-step-is-and-whose-it-is)). That state's output is the one the controller selects, and drained background work can still change it, so the end is the first point at which the selected answer is settled. Every node of the answering member counts: its own loop's and subagents' steps, the reads whose content it returned with no model call after, and everything its background work did before the end, drained work included. For an answering member that no link reaches, this is its `steps_to_answer`. Links from peers through the board and waits may raise it, when a peer's chain at the link is longer than the member's own chain there.

Drained work counts even when it did not change the selected output, its reads included: a background read of a ten-step peer's file during the drain makes the answer's chain at least 10 though the answer is the placeholder the agent returned. That is one more case of the over-count inside a member, as the shapes above show; telling the two apart would need to know which work the selected output came from, which the transcript does not record. Freezing the output when the agent returns would make the return the end, but would change which answer the swarm selects, so the metric follows the runtime as it is (decision: Ransom, 2026-10-29). No position needs recording at the agent's return: `member_result` is an event, and the answering member has one, since the controller selects only from ended members (`_control.py:627-629`); if it had none, the metric would be `None`.

### The metric

```python
# src/inspect_swarm/_metrics.py, SwarmMetrics
    # critical path, the dependency-linked definition: the longest chain of steps
    # (completed model calls, cache replays included) ending at the answering member's
    # end, linked across members where a read or a swarm_wait result showed a write of
    # the built-in board, or a wait named a member's end. Not a bound on causal depth
    # either way; see docs/analysis.qmd, "The critical path". None with no answer, when
    # the answering member has no recorded end, when the evidence is inconsistent (with
    # a warning), for a resumed sample, and in metrics written before this field.
    critical_steps_to_answer: int | None = None
```

- **Always computed**, whatever the swarm's configuration, with no list of unobserved sharing (decision: Ransom, 2026-10-28). The field is always present; its value is `None` in exactly the cases the field comment lists: no answer, an answering member with no recorded end ([Where the answer ends](#where-the-answer-ends)), inconsistent evidence, with a warning ([Code](#code)), and a sample resumed from a checkpoint (decision: Ransom, 2026-10-29; [Where it is computed](#where-it-is-computed)). The ways members can share outside the evidence are the experimenter's own configuration, a list could never rule out shared tools, store keys or services, and it would flag nearly every arm. The field comment says what the number counts and points to the user docs, which say that it over-counts concurrency inside a member and carries that excess to peers, and that it misses links when any other sharing is possible.
- **Unit: steps.** A step is the unit of the existing counts and of Kimi's CriticalSteps, and it is independent of provider latency and of `max_connections`. A chain's span in seconds, from its first step to the answer, is about the answer's own time, which the record already has (`MemberResult.time`); a sum of call durations along it mixes connection waits and retries in, which plan decision 12 ruled out for per-member timing until it can follow Inspect's working-time accounting ([swarm-implementation-plan.md Decisions](swarm-implementation-plan.md#decisions), item 12).
- **The existing fields stay** with their meaning, and their comment is corrected: they bound each member's own sequential depth from above, not the swarm's. For an answering member that no link reaches, the new number equals `steps_to_answer`. A link from a peer may raise it, and only when the peer's chain at the link is longer than the answering member's own chain there: after five steps, a read of a one-step or five-step peer's write takes `max(5, 1)` or `max(5, 5)`, and five more steps give 10, the member's `steps_to_answer`; a ten-step peer gives 15. So the two being equal does not mean no dependency was recorded.
- **Over-approximations**, each of which can only lengthen the chain, for the member and for every peer that later reads its writes: concurrent work inside a member counts as sequential, and a read by one branch, or by one tool call of a turn, feeds the member's later steps and writes in every branch, a sibling tool call's write whose content it never used included; the answering member's drained work counts, its reads included, whether or not it changed the selected output; a partial view links the whole version; a version's history links writes whose text a later edit removed; a read in a member's last turn reaches its end though no later generation saw it.

### Where it is computed

**At run end, as a per-sample metric**, beside the existing ones, in the same `SwarmMetrics` event and store entry, so every new run carries it and scorers can read it. Everything it needs is seen by the runtime: the `EventLog` is subscribed for the whole interval, the bus knows which channel is the built-in board, and the metrics are computed after every member has ended. It is computed only there, from the runtime's own event stream: nothing promises that a saved log's events give the same result, so the recording needs no position or board contract beyond the live run (decision: Ransom, 2026-10-29).

**A resumed sample has no value.** Its transcript receives the earlier attempt's events through checkpoint hydration, inside the interval ([Order](#order)): they are counted before they are notified, so a restored event and the next ordinary one would share a position, and their board evidence describes a board this run does not have. So when the interval finds the sample resumed, the same signal as `SwarmLedger.resumed`, the metric is `None` and no restored event is recorded for it: the interval knows at its start whether the sample resumed (`sample_resumed()`, read in `LedgerInterval.__init__`, `_ledger.py:385`), before any member enters `checkpointer()`, and then creates no `PathLog`, so the `EventLog` passes nothing on (decision: Ransom, 2026-10-29: resume is not supported for swarms, and the ledger and cost analysis already treat a resumed swarm's state as inconsistent); outside that case every first report reaches the subscriber before it is counted, so positions are distinct and in transcript order.

### Code

**`src/inspect_swarm/_critical_path.py`** (new, private):

```python
# What the critical path reads from the transcript, by position (the transcript's
# event count when the event was first reported). Filled by EventLog, live only.
class PathLog:
    def __init__(self, board: str | None, messages: str | None, roster: Collection[str]) -> None: ...  # the built-in board's and messages channel's record names, or None; this swarm's members

    steps: dict[str, tuple[str | None, int]]  # model event uuid -> (span id, position)
    writes: list[PathWrite]   # the board's `delivered`: position, member, loop, [(file, version, deleted)]
    reads: list[PathRead]     # the board's `read` without `listing`: position, member, [(file, version, deleted)]
    waits: list[PathWait]     # `waited`: position, member, loop,
                              # [(file, version)] when `channels` has board and at most messages besides, else [], [ended members]
    ends: dict[str, int]      # `member_result`: member -> position

    def __call__(self, event: Event, position: int) -> None: ...

def critical_steps_to_answer(
    log: PathLog,
    completed: Callable[[str], bool],    # whether a model event uuid is a step, as count_call() counts it
    places: Callable[[str | None], tuple[bool, str | None]],  # SpanPlaces: the member whose spans hold a span
    answer: str | None,                  # the answering member
) -> int | None: ...
```

- **The board.** `SwarmRun.run()` finds the built-in board and messages channel before it starts the interval: the names of the bus states whose types are `_SharedMemoryState` and `MessagesState`, or `None`. It passes those and the roster's names to `LedgerInterval(board=..., messages=..., roster=...)`, which creates its `EventLog` and the `EventLog`'s `PathLog` with them, unless the sample resumed, when the `EventLog` gets no `PathLog`.
- **Recording.** `EventLog.__call__` passes each event to its `PathLog` with `transcript().history.event_count` as its position; the `PathLog` takes an event the first time its uuid is passed (an update is not taken again): model events, whatever span they are in (`places` attributes a nested swarm's to the outer member), and `comm` and `member_result` events of roster members only: it reads an event's `member` first and drops the event when that is not in the roster, before reading its `channel`, `result`, `files` or `ended`, so a nested swarm's events are never decoded, whatever its channels are called; a wait's `ended` entries are kept for roster members only. It parses a roster member's `delivered` and `read` events only when their `channel` is the board, and a wait's `files` only when its `channels` include the board and no channel other than the built-in messages channel, skipping the entries that name a `record`; with no board it parses none, and never reads a custom channel's results. The `PathLog` keeps only ids, positions, loop ids, file ids, versions and member names: nothing a member wrote. `CommRecord` and `WaitRecord` are unchanged. `places` is passed as a callable so that `_critical_path.py` imports nothing from `_ledger.py`, which imports it.
- **Computing.** `critical_steps_to_answer()` takes the roster-filtered records and the steps `completed` accepts, each step's member from `places`, and walks the nodes in position order once with a running value per member: a step adds one; a write records the writer's value for its file version, with its loop; a read or wait result raises the reader's value to the largest of its in-edges; a `member_result` records the member's value as its end. For a file it keeps the largest value among all the writes in its history, which is what a live view of its current version follows, and each version's write with its loop and value, from which a tombstone view takes the deleting write. For a wait, each version also keeps its predecessor by another loop: the latest earlier write whose loop differs from this version's, which is version − 1 when that write's loop differs, and otherwise version − 1's own predecessor, so it is set in constant time when the write is recorded. A wait then takes the named version's write when another loop made it, and that version's predecessor when the waiter's loop did, with no scan back through the history. Steps in spans outside every member (the controller's, the final step's) are ignored. It returns the answering member's value at its `member_result`, or `None` without an answer or without that member's `member_result`.
- **Inconsistent evidence.** A read whose shown version is not the file's current version at its position, or a version a wait's board entry named that no roster write produced, would mean the ordering assumption broke. The function then logs a warning naming the file and versions and returns `None`; it never raises, so the metric cannot fail a run that succeeds without it.
- **Wiring.** `swarm_metrics()`, which already takes the answering member (`chosen` in `_control.py`), calls `critical_steps_to_answer()` with the event log's `PathLog`, `completed` built from `EventLog.calls`, `places` and that member, and fills the field; it leaves the field `None` when the sample resumed and the event log has no `PathLog`, without calling it. Everything stays synchronous, as the rest of the `finally` is.

**Configuration.** None: the metric is always on, as the other metrics are.

## Alternatives considered

- **A happens-before graph inside each member** (loops and tool calls as contexts, fork and join edges at each child's start and return, ordering by reporting task), so that parallel subagents count by their slower branch. The transcript does not record what that needs: which task started a child, where its caller awaited it (a task-group join writes no event), and which work a member's selected output came from. Span containment and reporting-task ids stand in for these and get ordinary shapes wrong in both directions, missing a planner a parent awaited in a task group and chaining a branch to an unrelated sibling that returned first. Doing it right needs causal evidence of spawning and awaiting ([Not this design](#not-this-design)); one chain per member is the existing counts' definition and always sound as an upper bound inside the member (decision: Ransom, 2026-10-29).
- **Placing a step at its completion rather than its begin.** Would let a call already in flight at a read take that read's dependency, across members; the begin position is the event's first report.
- **Ending the answer at the agent's return**, a position the wrapper records then. Drained background work can still replace the selected output, with content from a peer's file it read after the return, and the return would leave that source out; making the return final would mean freezing the output there, which changes which answer the swarm selects (decision: Ransom, 2026-10-29: end at the member's end).
- **Ending the answer at the member's last step.** Misses a read whose content the member returned with no model call after it.
- **`woken_by` as the wait's edge.** Misses waits that returned at once (`woken_by: []`) and adds commits whose news never reached the loop.
- **No edge for waits, only for views.** A wait-then-view hand-off would count one step fewer than it took.
- **Listings as edges.** A root listing would tie a member to nearly all earlier work.
- **Keeping only the latest write of a file for a live view.** A later write by a shallower member would hide a deeper member's earlier write whose text the version still holds.
- **`critical_steps`, the longest chain anywhere** (decision: Ransom, 2026-10-28, dropped). It charges no answer and describes overall depth, which `longest_member_steps` already gives per member.
- **`critical_path_unobserved`, a list of known unobserved sharing** (decision: Ransom, 2026-10-28, dropped). It reports configuration the experimenter set, cannot rule out what it does not detect, and would be non-empty for nearly every arm.
- **Omitting the metric when sharing outside the evidence is possible.** It would drop the metric for the default configuration, where the recorded edges still hold and only links through other channels are missing (decision: Ransom, 2026-10-28: always report).
- **A wall-clock path.** See [The metric](#the-metric).
- **An analysis helper over logs instead of a run-end metric.** Works on older logs, but every arm would need the user to call it, scorers could not read the value, and it needs the built-in board's identity, which the log lacks.
- **Also promising that a saved log's events rebuild the same result**, with each event's index as its position and a test that compares the two. It needs a position contract the live stream does not need (restored events are counted before they are notified, a replay has no live count) and board identity the log does not carry; the metric is computed at run end only (decision: Ransom, 2026-10-29).
- **The board by channel name** (`channel == "shared_memory"`). Names are not reserved, so a custom channel with that name would have its results read as board versions, and an unknown version would void the metric; the runtime knows the instance (decision: Ransom, 2026-10-29).
- **Counting a resumed sample** from the restored events. Their positions collide with the next event's and their board evidence is the earlier attempt's; telling them apart would need inspect_ai to mark restored events to subscribers.
- **Adding the step's index or the proposing tool call id to the evidence.** The order of events already places every node, and keeping the evidence as it is leaves every channel and log reader untouched.

## Compatibility and migration

- **`SwarmMetrics`** gains one optional field with a `None` default, so metrics written before load unchanged; `version` stays 1, as for the fields added before. The `metrics` `InfoEvent` and the `inspect_swarm:metrics` store entry carry it.
- **Evidence, the bus, channels, the ledger, the result record:** unchanged. `EventLog`, `CommRecord` and `MemberOutcome` are private.
- **Public API:** no new names. `SwarmColumns` is unchanged (it reads the ledger, not the metrics).
- **Docs:** [Analysis, Metrics](https://meridianlabs-ai.github.io/inspect_swarm/analysis.html#metrics) gains `critical_steps_to_answer`: what it counts, that latency is read from it, and, plainly: that it over-counts inside a member (parallel and background subagents, tool calls of one turn, and concurrent work of any kind, count one after another) and that this excess reaches peers through the member's later writes; that it misses links whenever members can share information outside the built-in board and waits (a shared sandbox, a shared volume, a service, a custom channel, its wait results included, shared tools or store keys, a controller passing results between members), and on the board itself when a wait reports more than ten files, since its evidence names only the first ten, which can only lower it; that with both it is not a bound on true causal depth either way; and the cases in which it is `None`.
- **Old runs** have no value; recomputing it from their logs is the helper left out below.

## Security

No untrusted text reaches the computation. It reads span ids, positions, model event status, and from the swarm's own evidence member names, loop ids, file ids and integer versions; paths, payloads and file contents are neither stored in the `PathLog` nor read. Model output reaches it only as the tool calls that made the evidence, which the channel validated, and a model cannot write an `InfoEvent`. Agent code can (any code may call `transcript().info()` with source `inspect_swarm`), but member and tool code is the eval author's and trusted, as for every other metric read from evidence; inconsistent evidence yields `None` and a warning, not an error. Work and memory are linear in the events of the interval: each node does constant work, a write records one entry per file it touches, and a read or wait one lookup per file it names.

## Testing

All async tests run on asyncio and trio through the `anyio_backend` fixture.

- **`tests/test_critical_path.py`**: table-driven tests of `critical_steps_to_answer()` over hand-built `PathLog`s, one row per shape in [The shapes](#the-shapes), with the length given there, and:
  - a single member (all its steps through its `member_result`, drained ones included, equal to its `steps_to_answer`);
  - a wait that returned at once (`woken_by: []`, edge from `files`); a wait released by a write whose news vanished (no edge); a cancelled wait (no edge); a wait after the waiter's own blind append, between two members and between two loops of one member; repeated waits after several blind appends by the waiter, each naming the same current version (every one links the peer's write); a wait whose evidence names ten files while its result reports eleven (no edge from the eleventh's writer until a view names it);
  - a listing (no edge); a tombstone view (the deleting write only); `delivered` and `read` of another channel (no node);
  - an outer built-in board with a nested swarm's events whose members are outside the roster, one nested channel a custom one named `shared_memory`: its `delivered` with `result: {"size": 1}`, its `read`, a wait with `files: [{"foreign_record": "x-1"}]` and `channels: ["shared_memory"]`, and its `member_result`, all dropped before their payloads are read (no node, no warning, outer value unchanged), while its model calls in an outer member's spans still count for that member;
  - one generation proposing a view of a ten-step peer's file and an independent write, the view first, and a consumer of the write (11, the propagated over-count);
  - a rename of a directory (entries with `old_path` and `new_path`, no `path` or `deleted`): every entry is a write;
  - a custom channel named `shared_memory` with no built-in board (`PathLog(board=None, ...)`): its deliveries, reads and a wait naming `f-99` version 7 make no node or edge, no warning and not `None`; member steps and its waits' `ended` entries still count;
  - waits that reached a custom waiting channel, alone or beside shared memory (and messages), with an entry naming a board id (`f-1`), an id the board never had (`f-99`), or none: no file edge, no warning, not `None`; their `ended` entries still link;
  - a wait that reached shared memory and messages, naming a ten-step peer's file and an unread message: the file links, the message entry is skipped (11);
  - evidence naming a member outside the roster, with its own `f-1` (ignored, no warning), and steps in spans outside every member (ignored);
  - an inconsistent read version (`None` and a warning); errored and cache-replayed model events; no answer, and an answering member with no `member_result` (`None`).
- **`tests/test_metrics.py`**: mockllm swarms through `run_swarm()`, asserting the field: two `react()` members, one writing and one waiting and viewing (as B above); a deepagent member with two `general()` children dispatched in one turn (the sum of their steps and its own); a member waiting for a peer's end while that peer's background work drains; a member whose own-loop wait reaches shared memory and messages and names a ten-step peer's file before that peer ends (11); the paired drained-read case, an answering member whose agent returns a placeholder with no step and whose background work views a ten-step peer's file during the drain and either replaces `state.output` with it or leaves the placeholder, asserting both the swarm's selected output (the peer's content, or the placeholder) and the metric (10 in both); a member running a nested swarm with shared memory (as `tests/test_background_members.py:1090`), whose inner board makes no edge; a swarm whose only channel is a registered custom channel named `shared_memory` (no file edges, member-end waits still link); a member with the built-in board running a nested swarm whose only channel is a custom channel named `shared_memory` that delivers and wakes waits (the outer value as without it, no warning); and a resumed interval recording no `PathLog`, so that `swarm_metrics()` leaves the field `None`.

No test needs Docker, a network or a model provider.

## Implementation plan

1. **The path and its recording.** `src/inspect_swarm/_critical_path.py` (`PathLog`, `critical_steps_to_answer()`), `src/inspect_swarm/_ledger.py` (`LedgerInterval(board=..., messages=..., roster=...)`, and `EventLog` passing first reports with the transcript's count to a `PathLog` created with the board, the messages channel and the roster, none for a resumed sample), `tests/test_critical_path.py`.
2. **The metric.** `src/inspect_swarm/_metrics.py` (the field, the corrected comment on the existing counts, the computation in `swarm_metrics()`), `src/inspect_swarm/_controller/_control.py` (the board and the messages channel from the bus's states before the interval starts), `docs/analysis.qmd` (Metrics), `tests/test_metrics.py`.

## Open questions

None.

## Not this design

- **Concurrency inside a member**, counted by its longest branch rather than its sum: a design of its own, which first records causal evidence of which task started a child and which task awaited it (and where), and which work a member's selected output came from. AnyIO exposes a task's identity and its parent, but not await points, and a task-group join writes no transcript event, so it may need an inspect_ai change.
- **An analysis helper over logs**, such as `swarm_critical_path(sample)` in `inspect_swarm.analysis`, for logs written before the metric: its own small design if it is ever wanted. It would need, recorded in the log, the built-in board's identity and a position for each event that holds for restored events too.
- **Refusing checkpointing in a swarm**, with an error at swarm start when the sample has an active checkpointer, is separate work; `None` for a resumed sample stays correct either way.
- **The chain itself**, as the members and step counts along it, for explaining a number; the metric records its length only.
- **`SwarmColumns` for metrics.** The columns read the ledger; metrics are read from the store entry today.
- **The messages channel's edges.** Its `read` evidence will name records rather than file versions; its design adds its rule here.
- **Edges for a member started by a controller** with another member's output, which would need the controller to record what it passed.
- **Edges through a nested swarm's board** for the outer swarm's metric; the inner swarm's own metrics count them.
