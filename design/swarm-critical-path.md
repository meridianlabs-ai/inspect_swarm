# Inspect Swarm: the dependency-linked critical path

## Overview

When a swarm ends, its metrics include `critical_steps_to_answer`: the number of model calls (steps) on the longest chain of steps the answer depended on. Each member's steps, its own loop's, its subagents' and its background work's, form one chain in the order they began. Chains link between members only where the swarm recorded a dependency: a channel's result follows the commits of the records it showed, as the channel reports them, and a `swarm_wait` result that names a member's end follows that member's whole chain. The chain ends at the answering member's end, after its drain, where the output the controller selects is settled.

The channels say what their results depend on, not the metric. Every result a channel returns to a loop, a `shared_memory` view, a `read_messages` read, a custom channel's read and each channel's part of a `swarm_wait` result, records `depends_on`: the ids of the committed records whose content or news it showed. Each commit's `delivered` event names its record id and its sender. The metric links each result to the commits it depends on, and each commit to its sender's chain at the commit, the same way for every channel: it reads no channel name, file, version or payload. The bus refuses an id the channel did not commit in this run, and writes each dependency into a loop's evidence once.

It is the measure latency curves are read by, beside `steps_to_answer` and `longest_member_steps`. It is computed once, at run end, from the events the swarm's event log saw as they happened. It has two limits, which pull in opposite directions. Inside a member it over-counts: concurrent subagents, background work and tool calls of one turn count one after another, the answering member's drained work counts whether or not it changed the answer, and that excess reaches peers through the member's later commits. Across members it misses links: it sees only what channels report and the ends waits name, so information that flows another way, such as a shared sandbox, adds no link, which can only lower it. So it is not a bound on true causal depth either way; it is the longest chain in the graph defined here.

The code is [`_critical_path.py`](../src/inspect_swarm/_critical_path.py), `EventLog` and `LedgerInterval` in [`_ledger.py`](../src/inspect_swarm/_ledger.py), `swarm_metrics()` in [`_metrics.py`](../src/inspect_swarm/_metrics.py), and `Bus.read()` and `Bus.depends()` in [`_bus.py`](../src/inspect_swarm/_bus/_bus.py). The user docs are [Analysis, The critical path](../docs/analysis.qmd#the-critical-path) and, for a channel's report, [Channels, Writing a channel](../docs/channels.qmd#writing-a-channel). Each built-in channel's rule is in its own design: [the board's](swarm-memory-channel.md#what-a-result-depends-on), [the messages channel's](swarm-communication.md#evidence-and-provenance) ("What a messages result depends on") and [how a wait collects them](swarm-wait.md#evidence).

## Why

A swarm's latency question ([swarm.md](swarm.md), "What does a swarm buy in latency?") needs the number of sequential model calls the answer took, given that members work in parallel but wait on and build on each other. `steps_to_answer` and `longest_member_steps` cannot say, because neither sees members' dependencies: they bound each member's own sequential depth from above, not the swarm's. A member that waits for a peer's lemma and then proves the theorem in three steps has `steps_to_answer` 3, though the answer could not exist before the peer's ten steps.

## At a glance

```
 A (react)      a1 → a2 → a3 → a4 → a5 → a6        a5 creates f-1 (w-1, version 1); a6 appends (w-2, version 2)
                                    │     │
                     ┌──────────────┘     └──────────────┐
                     ▼                                   ▼
 B (react)      b1 → b2 ───────────────────────────────→ b3
                b1 calls swarm_wait; its result reports f-1 and depends on w-1: a5 → b2
                b2 views f-1 version 2, whose history is w-1 and w-2; the wait already
                linked w-1, so the view's depends_on is [w-2]: a6 → b3; b3 submits

 C (deepagent)  c1 → d1 → e1 → d2 → e2 → d3 → e3 → e4 → e5 → c2 → c3
                c1 starts two general() children, d and e, which run at once; their steps
                interleave in the transcript and count one after another

 P (react)      p1 → … → p10 ─ send_message m-1 ─→ p11 … p15
                               │
 R (react)      r1 ────────────┴─→ read_messages, depends_on [m-1] → r2

 values:  a1..a6 = 1..6    b1 = 1   b2 = max(b1, a5) + 1 = 6   b3 = max(b2, a6) + 1 = 7
          c1..c3 with d and e = 1..11
          r2 = max(r1, p10) + 1 = 11
```

If B answers, `critical_steps_to_answer` is 7 (A's six steps, then B's view and submit) where `steps_to_answer` is 3. If C answers, it is 11, as `steps_to_answer` is: the parallel children count one after the other, where their slower branch alone would give 8. If R answers, it is 11: P's ten steps before the send and R's one after the read, not P's five after the send. The metric does not know that `w-1` is a board write or `m-1` a message.

## The graph

The metric is the longest path in a graph of the members' recorded events.

### Steps and whose they are

- **A step** is a completed model call in a member's spans, cache replays included and errored or cancelled calls excluded, as `count_call()` counts it for the ledger. `SpanPlaces` attributes a span to the member whose span tree holds it, so a member's steps include its subagents', its background work's and those of any swarm nested inside it. Steps in spans outside every member (the controller's, the final step's) are ignored.
- **A member's span** is the agent span the member wrapper opens around its agent. It stays open through the member's drain: after the agent returns, `_drain()` closes the member's lever, so its background work dispatches no new provider calls, and waits the grace period for work in flight. A cache replay is not dispatched, so background work can still begin cache-replayed steps during the drain. The wrapper keeps the state the agent returned through the drain and hands it to the controller afterwards, and the controller selects the answer from an ended member's output, so drained background work can still change the selected output.
- **Concurrency inside a member** is common: deepagent runs `general()` children in parallel and in the background, and wrappers run `run()` children in a task group. The transcript records each child's agent span, not which task started a child or where its caller awaited it: a task-group join writes no event.

### Positions

Every model call and evidence event has a position: `transcript().history.event_count` when the transcript first notifies its subscribers of the event. The transcript notifies before it counts a new event, so the count is the event's index, and an update to an event is not a new report. A `ModelEvent` is first reported when its call begins, so a step's position is its begin.

### Nodes

| Node | Member | Weight |
|---|---|---|
| a step, at the position where its call began | the member whose span tree holds its span (`SpanPlaces`) | 1 |
| a commit (`delivered`) of any channel | its `agent`, the sender, when that is in the roster | 0 |
| a result (`read` or `waited`) | its `agent`, when that is in the roster | 0 |
| a member's end (`member_result`) | the member it names | 0 |

### Edges

Every edge goes from a node to a node with a later position:

1. **A member's chain.** Each node follows the member's previous node. A member's steps, from every loop, subagent and background task it runs, are one sequence in the order they began, as the existing step counts treat them.
2. **Commits to results.** A result follows the commit of each record its `depends_on` names, at the sender's value there: the sender's work after the commit is not linked. What a result depends on is its channel's rule ([What channels report](#what-channels-report)).
3. **Member ends to waits.** A wait result follows the end (`member_result`) of each roster member its `ended` names, and so that member's whole chain, drained work included.

An edge from the reader's own member adds nothing, since its chain already passes through that commit, and nor does a commit an earlier result of the same loop depended on, since a member's value never decreases along its chain: that is why the bus can write each id into a loop's `depends_on` once.

**The metric** is the largest number of steps on a path into the answering member's end, its `member_result`: the answering member's chain value there.

**Acyclic.** Every edge points forward in position: a member's chain is in position order; a result depends only on commits written before it, since a commit's `delivered` event and the channel's `apply()` run with no `await` between them and a result is computed from the state they made; and a wait result follows the `member_result` events it names, which `_end()` writes synchronously after releasing the waits. So one pass in position order computes the longest path into every node, keeping one running value per member.

**A wait is an edge, from what its result named, not from `woken_by`.** A wait blocks the loop until a commit or an end it cares about, so what the waiter does next depends on it as much as on a view; the waiter usually views the file next, which adds the content edge one step later, and that extra step is a real cost of the wait-then-view protocol. `depends_on` and `ended` name what reached the model. `woken_by` does not: a wait that finds news at once has `woken_by: []`, and a release whose news vanished keeps a commit the result never named. `woken_by` stays what [swarm-wait.md](swarm-wait.md#evidence) uses it for, joining idle time to the commits that ended it.

**This run's evidence only.** An evidence event is a node only when its member is in this swarm's roster, checked before anything else in the event is read, and a wait's `ended` counts only roster members. Member names are unique in a sample, since each names a timeline. A nested swarm's events reach the outer subscriber too, and its bus numbers its records from 1, so its ids can equal the outer run's; its evidence names its own members and is dropped before its `depends_on` is read, so its ids never mix with this run's. Its members' steps are still the outer member's, through `SpanPlaces`; only the inner channels' edges are missing for the outer metric, and the inner swarm's own metric counts them.

## What channels report

### The evidence the metric reads

Each is a `CommunicationEvent` with source `inspect_swarm`, written in the tool span of the call that acted, with `agent`, the acting member, and `loop_span_id`, the acting loop's agent span ([swarm-event-types.md](swarm-event-types.md#how-inspect_swarm-writes-them)):

| Kind | Written | What the metric reads |
|---|---|---|
| `delivered` | step 4 of `Bus.deliver()`, for every channel | `agent`, the sender; `record_ids`, the committed record |
| `read` | a channel's tool reports a result through `Bus.read()` | `agent`; `data.depends_on` |
| `waited` | a `swarm_wait` returns or is cancelled (`Waits.wait()`) | `agent`; `data.depends_on`; `data.ended`, `[{"member", "status"}]` |

and the `SwarmEvent` of kind `member_result` naming a member, which `_end()` writes in the member's span when it ends, after its drain. The metric reads no `channel`, `channels`, `record_ids` of a result, `files`, `listing`, `result` or loop, and ignores `sent`.

### Two lists: what a result concerns, and what it depends on

A result's evidence carries two lists of record ids, each checked by the bus to be records the result's own channel committed earlier in this run:

- **Provenance**, on `read` only, as its `record_ids`: the records the result concerns, complete, whatever the loop saw before. For the board, each shown file's last write at the version shown, listings and tombstones included; for a messages read, every message it returned.
- **The dependency**, `depends_on` in the event's `data`, on `read` and `waited`: the commits whose content or news the result showed, by the channel's own rule, less those the loop already depended on. It can be empty.

The two differ wherever the channel's rule does: a board view concerns the file's last write but depends on its whole history; a listing concerns every listed file's last write and depends on nothing; a `read_messages` after a wait that already counted the message concerns it and, in that loop, depends on nothing new. What counts as "showed" is the channel's decision, under two rules: news counts (a result that tells the loop a commit happened, without its content, as a wait does, depends on it), and a result built from several commits depends on them all.

### The bus checks and trims

`Bus` keeps `_committed`, record id to the channel that committed it, filled at step 4 of `deliver()`, and `_depended`, loop agent span to the records already written into its results' `depends_on`.

- **`Bus.read(channel, records, *, depends_on=None, listing=False)`** is how a channel's tool reports a result, inside the member's tool call. It finds the caller with `member()`, raising `RuntimeError` outside a member of this run, as `deliver()` does; checks that the channel is open (`ValueError` otherwise); takes the loop from `current_agent_span_id()`; checks `records` and writes them whole; passes `depends_on`, by default `records`, through `depends()`; and writes the `read` evidence with `comm_read()`, whose only caller it is, returning the read's id. A custom channel may pass `listing`. The run's `SwarmBus` also takes the board's `files=`, the versions a result showed, which the public `Bus` protocol leaves out.
- **`Bus.depends(channel, loop, depends_on)`** checks each id and returns those not yet written for the loop, in order and once each, marking them written. `Waits.wait()` calls it for each reached channel's `WaitNews.depends_on` ([swarm-wait.md Evidence](swarm-wait.md#evidence)).
- **The check** raises `ValueError` for an id the channel did not commit in this run, whether another channel committed it, a refused record consumed its number, or nothing did: "{channel}: a result names {id}, which is not a record this channel committed in this run." That is a bug in the channel's code, so it fails the tool call and the sample loudly.
- **Trimming per loop** leaves the metric exactly as without it, since a commit the loop's earlier result linked raises nothing when linked again. Without it, a board view writes the file's whole history every time it is viewed, and views are not rate-limited: a shared log of 300 appends viewed 50 times by each of 6 members would write 90,000 ids. With it, the dependency ids a run writes are bounded by its commits times its loops. Every subagent `run()` is a new agent span, so a fresh loop's first view writes the history again; that is one view per loop, each with a model step of its own, not the repeated views the trimming removes. `records` is not trimmed: it is one id per file or message shown. The trimming is the bus's, so no channel keeps seen state for it and channels report what their results showed, not what is new.

### Each channel's rule

- **The board** ([swarm-memory-channel.md What a result depends on](swarm-memory-channel.md#what-a-result-depends-on)): a live view depends on every write in the shown version's history, a tombstone view on the delete, a listing on nothing, and a wait on the write it reports for every file it reports, the peer write before the waiter's own blind append included.
- **The messages channel** ([swarm-communication.md Evidence and provenance](swarm-communication.md#evidence-and-provenance), "What a messages result depends on"): a read depends on the messages it returned, and a wait with the channel's news on every unread message it counts; a wait without message news, and the unread count on other results, depend on none.
- **A custom channel** reports with the same calls: its tool calls `run.bus.read(self.name, records)`, from the `ChannelRun` its `open()` was given, passing `depends_on=` when that differs from what the result concerns, and its waiting state's `news()` returns `WaitNews(lines, files, depends_on=[...])`. `WaitNews.depends_on` defaults to empty, so news that reports no record, a timer's or a test channel's, needs nothing. A channel that reports nothing makes no edge, though its commits still count in the communication metrics. A channel's name does not matter: a custom channel named `shared_memory` or `messages` is treated as any other. [Channels, Writing a channel](../docs/channels.qmd#writing-a-channel) and the `ChannelRun.bus` and `WaitNews.depends_on` docstrings document the contract for users.
- **Results that show no record make no edge**: a member's end in `list_members`, the unread-message count, and the board's changed-file line and `changes` command write no evidence ([Not this design](#not-this-design)).

## Where the answer ends

The answer's chain ends at the answering member's `member_result`, which `_end()` writes after the drain, when the wrapper hands the controller the member's final state. That state's output is the one the controller selects, and drained background work can still change it, so the end is the first point at which the selected answer is settled. Every node of the answering member counts: its own loop's and subagents' steps, the reads whose content it returned with no model call after, and everything its background work did before the end, drained work included. For an answering member that no link reaches, the metric is its `steps_to_answer`.

Drained work counts even when it did not change the selected output, its reads included: a background read of a ten-step peer's file during the drain makes the answer's chain at least 10 though the answer is the placeholder the agent returned. Telling the two apart would need to know which work the selected output came from, which the transcript does not record, and freezing the output when the agent returns would change which answer the swarm selects. The answering member always has a `member_result`, since the controller selects only from ended members; if it had none, the metric would be `None`.

## The metric

```python
# src/inspect_swarm/_metrics.py, SwarmMetrics
    # critical path, the dependency-linked definition: the longest chain of steps
    # (completed model calls, cache replays included) ending at the answering member's
    # end, linked across members where a channel's result showed another member's
    # commit, as the channel reports it, or a wait named a member's end. Not a bound on
    # causal depth either way; see docs/analysis.qmd, "The critical path". None with no
    # answer, when the answering member has no recorded end, when the evidence names a
    # record no recorded commit made (with a warning), for a resumed sample, and in
    # metrics written before this field.
    critical_steps_to_answer: int | None = None
```

- **Always computed**, whatever the swarm's configuration, with no list of the ways members could share outside the evidence. Those are the experimenter's own configuration, a list could never rule out shared tools, store keys or services, and it would flag nearly every arm. The user docs say plainly what the number over-counts and what it misses.
- **Unit: steps**, the unit of the existing counts and of Kimi's CriticalSteps, independent of provider latency and of `max_connections`. A chain's span in seconds is about the answer's own time, which the record already has (`MemberResult.time`), and a sum of call durations along it mixes connection waits and retries in.
- **The existing fields keep their meaning.** Their comment says that they bound each member's own sequential depth from above, not the swarm's. A link from a peer raises the new number above `steps_to_answer` only when the peer's chain at the link is longer than the answering member's own chain there: after five steps, a read of a one-step or five-step peer's write takes `max(5, 1)` or `max(5, 5)`, and five more steps give 10, the member's `steps_to_answer`; a ten-step peer gives 15. So the two being equal does not mean no dependency was recorded.
- **Over-approximations**, each of which can only lengthen the chain, for the member and for every peer that later reads its commits: concurrent work inside a member counts as sequential, and a read by one branch, or by one tool call of a turn, feeds the member's later steps and commits in every branch, a sibling tool call's write whose content it never used included; the answering member's drained work counts, its reads included, whether or not it changed the selected output; a partial view links the whole version; a version's history links writes whose text a later edit removed; a read in a member's last turn reaches its end though no later generation saw it.

## Where it is computed

**At run end, as a per-sample metric**, in the same `SwarmMetrics` event and `inspect_swarm:metrics` store entry as the other metrics, so every run carries it and scorers can read it. The `EventLog` is subscribed to the transcript for the whole interval, and the metrics are computed in `SwarmRun.run()`'s `finally`, once every member has ended. It is computed only there, from the runtime's own event stream: nothing promises that a saved log's events give the same result, so the recording needs no position contract beyond the live run.

**A resumed sample has no value.** A sample resumed from a checkpoint receives the earlier attempt's events through checkpoint hydration, counted before they are notified, so a restored event and the next ordinary one would share a position, and their evidence names commits this run did not make. Swarms refuse checkpointing: `SwarmRun.run()` raises `PrerequisiteError` before the interval starts when the sample has a checkpointer other than inspect_ai's no-op one. `LedgerInterval` still reads `sample_resumed()` at its start, before any member runs, and when it is true creates no `PathLog`, so the metric is `None`.

### Code

```python
# src/inspect_swarm/_critical_path.py (private)

# A commit (`delivered`): its record id and sender.
@dataclass(frozen=True)
class PathCommit:
    position: int
    member: str
    record: str

# A result that showed commits (`read` or `waited`): its `depends_on`, and for a wait
# the roster members whose ends it named.
@dataclass(frozen=True)
class PathResult:
    position: int
    member: str
    depends_on: list[str]
    ended: list[str]

class PathLog:
    def __init__(self, roster: Collection[str]) -> None: ...
    steps: dict[str, tuple[str | None, int]]  # model event uuid -> (span id, position)
    commits: list[PathCommit]
    results: list[PathResult]
    ends: dict[str, int]                      # `member_result`: member -> position

    def __call__(self, event: Event, position: int) -> None: ...

def critical_steps_to_answer(
    log: PathLog,
    completed: Callable[[str], bool],         # whether a model event uuid is a step
    places: Callable[[str | None], tuple[bool, str | None]],  # SpanPlaces
    answer: str | None,                       # the answering member
) -> int | None: ...
```

- **The log.** `SwarmRun.run()` passes the roster's names to `LedgerInterval(roster=...)`, which creates its `EventLog` with a `PathLog(roster)`, or with none for a resumed sample.
- **Recording.** `EventLog.__call__` passes each report to its `PathLog` with the transcript's event count as its position. The `PathLog` takes each event the first time its uuid is passed: every model event, whatever span it is in, and, from roster members only, `member_result` events and the swarm's `delivered`, `read` and `waited` events. It reads an event's member first and drops the event when that is not in the roster. From a roster member's events it reads a `delivered`'s record id, a `read`'s `depends_on`, and a `waited`'s `depends_on` and `ended`, keeping `ended` entries for roster members only. It keeps only ids, positions and member names, nothing a member wrote. `places` is a callable so that `_critical_path.py` imports nothing from `_ledger.py`, which imports it.
- **Computing.** `critical_steps_to_answer()` takes the steps `completed` accepts, each step's member from `places`, and walks the nodes in position order once with a running value per member: a step adds one; a commit records its record's value, the sender's value there; a result raises the member's value to the largest value among the records in its `depends_on` and the ends it names; a `member_result` records the member's value as its end. It returns the answering member's value at its `member_result`, or `None` without an answer or without that member's `member_result`.
- **A record with no commit.** A result naming a record with no earlier commit in the log means evidence written outside the bus, which refuses such an id. The function then logs a warning naming the member and the record and returns `None`; it never raises, so the metric cannot fail a run that succeeds without it.
- **Wiring.** `swarm_metrics()` calls `critical_steps_to_answer()` with the event log's `PathLog`, `completed` built from `EventLog.calls`, the span places and the answering member, and leaves the field `None` without calling it when the event log has no `PathLog`. Everything is synchronous, as the rest of the `finally` is.

**Configuration.** None: the metric is always on, as the other metrics are.

## The shapes

| Shape | How the rule handles it | Length |
|---|---|---|
| B in [At a glance](#at-a-glance): wait, view, submit | the wait depends on a5's write, the view on a6's (`w-1` already linked) | 7 |
| C: two `general()` children run at once | one chain in begin order | 11, the sum (over-count) |
| A wrapper's `run(plan)` then `run(solve)` | one chain | every step |
| A one-step member whose tool views a ten-step peer's file, calls a model with it and writes the result | the read raises the member's chain to 10; the tool's model call follows it | 11 at the write |
| A member that views a ten-step writer's file and returns it with no model call after | the read is before its end | 10 |
| Two members appending to one file at depths 10 and 2; a reader views it, then makes one step | the view depends on every write in its history | 11 |
| A listing, then a view of one listed file | the listing depends on nothing; the view on the file's history | the file's history |
| One generation of a one-step member proposes a view of a ten-step peer's file and an independent write; the view runs first; a one-step consumer views the write | the view raises the member's chain to 10 before the write, whose content it did not use | 11 for the consumer (over-count, carried to a peer) |
| A concurrent branch of one member reads a ten-step peer's file; another branch makes the next step | the member's chain passes the read | 11 (over-count) |
| A waiter with one step before and one after a wait naming a member whose four steps end during its drain | the wait follows that member's end | 5 |
| A waiter naming a member that ended by a token limit, or was cancelled, before its agent returned | its `member_result` follows all its steps | that member's chain |
| A wait after the waiter's own blind append to a file a ten-step peer wrote; one step after | the wait depends on the peer's write | 11 |
| The same wait repeated before any view | the second's `depends_on` is empty (trimmed) | 11, unchanged |
| A wait reporting eleven files, ten by a one-step P and the eleventh by a ten-step Q; one step before and after | `files` names ten, `depends_on` all eleven writes | 11 |
| A wait reaching the board and a custom waiting channel that reports nothing, after a ten-step peer's write; one step after | the board's dependency links | 11 |
| The answering member's agent returns a placeholder with no step; its drained background work views a ten-step peer's file and replaces the output with it | the view is before the member's end | 10 |
| The same drained read, the output left as the placeholder | the same | 10 (over-count) |
| The answering member's background call in flight when its agent returns, completing in the drain | it is before the member's end | counted, as in `steps_to_answer` |
| R reads a message P sent after its ten steps, while P works five more, then makes one step | the read depends on `m-1`, committed at P's tenth step | 11 |
| R's wait has the messages channel's news, counting P's `m-1` (`wake=True`, or `wake=False` beside a waking `m-2`); then `read_messages` and one step | the wait depends on every unread message; the read's `depends_on` is empty | 11 |
| R has only a `wake=False` message from P; a one-step Q's board write ends R's wait; R reads it and makes one step | the wait depends on Q's write only; the read links P | 11 |
| R's results show only the unread count, and R never reads | no evidence | R's own chain |
| A custom channel whose read or wait news reports a ten-step writer's `s-1`; one step after | the result depends on `s-1` | 11 |
| A custom channel named `shared_memory`, alone, reporting nothing | no dependencies; its waits' `ended` still link | its waits' ends only |
| A custom channel reporting `w-1`, which the board committed, or `s-99`, which nothing committed | `ValueError` from the bus | the sample fails |
| A nested swarm committing its own `w-1` inside an outer member, the built-in board or a custom channel named `shared_memory` | its members are not in the roster, so its events are dropped before their `depends_on` is read | the outer swarm's chains, no warning |

## Decisions

- 2026-10-14: the dependency-linked critical path is what latency curves are interpreted from (decision: Ransom, 2026-10-14).
- 2026-10-28: the metric is always computed and reported, with no list of unobserved sharing (`critical_path_unobserved` dropped) and no omission when sharing outside the evidence is possible; the longest chain anywhere (`critical_steps`) is dropped, overall depth staying with `longest_member_steps` (decision: Ransom, 2026-10-28).
- 2026-10-29: a member's steps are one chain, with no happens-before graph inside a member (decision: Ransom, 2026-10-29).
- 2026-10-29: the answer ends at the answering member's end, after the drain, not at its agent's return, and the output is not frozen at the return (decision: Ransom, 2026-10-29).
- 2026-10-29: the metric is computed at run end only, from the live event stream, with no promise that a saved log rebuilds it (decision: Ransom, 2026-10-29).
- 2026-10-29: a resumed sample has no value; resume is not supported for swarms (decision: Ransom, 2026-10-29).
- 2026-10-29: the metric does not find the board by channel name, since names are not reserved (decision: Ransom, 2026-10-29).
- 2026-10-30: each channel reports what its results depend on and the metric links them generically, reading no channel's evidence (decision: Ransom, 2026-10-30).
- 2026-10-30: provenance (`record_ids`) and the dependency (`depends_on`) are separate lists; `depends_on` goes in the event's `data`, for `read` and `waited` (decision: Ransom, 2026-10-30).

## Alternatives considered

- **A happens-before graph inside each member**, so that parallel subagents count by their slower branch. The transcript does not record which task started a child, where its caller awaited it (a task-group join writes no event), or which work the selected output came from; span containment and reporting-task ids stand in for these and get ordinary shapes wrong in both directions. One chain per member is the existing counts' definition and always an upper bound inside the member.
- **Placing a step at its completion rather than its begin.** Would let a call already in flight at a read take that read's dependency.
- **Ending the answer at the agent's return, or at the member's last step.** The first leaves out drained work that can replace the selected output; making it final would mean freezing the output, which changes which answer is selected. The second misses a read whose content the member returned with no model call after it.
- **`woken_by` as the wait's edge.** Misses waits that returned at once and adds commits whose news never reached the loop. **No edge for waits at all** would count a wait-then-view hand-off one step short.
- **Listings as edges.** A root listing would tie a member to nearly all earlier work.
- **Only a live file's latest write.** A later write by a shallower member would hide a deeper member's earlier write whose text the version still holds.
- **The metric decoding each channel's evidence** (the board found by instance or by name, its file versions and histories, wait entries split by shape, a decoder for messages). Every channel would be a special case in the metric, mixed-channel waits would need their entries attributed, and a custom channel would get no edge.
- **One list for provenance and dependency.** `record_ids` would lose the records a listing, a re-view or a read after a wait concerns.
- **A commit naming the commits it extended**, so a view depends only on the shown version's write. Views would write one id, but the metric would need a second generic edge, commit to commit, that every custom channel must understand, and a wait reporting a peer write would inherit that file's whole history.
- **Each channel trimming its own reports.** Every channel would need seen state, `news()` may not mark anything and is called repeatedly while a wait sleeps, and a channel that forgot would grow the evidence. **No trimming** is exact but multiplies a long file's history by every view of it.
- **Linking a message at the sender's last step before its read.** It would charge the reader for the sender's work after the send.
- **Keeping the ten-file cap on a wait's links.** The result names every file's writer, so the eleventh writer's news reached the model.
- **Linking quiet messages from a wait without message news.** It needs news that does not make a wait reportable, a second kind of news in the `WaitingState` contract for one count line.
- **An analysis helper over logs instead of a run-end metric.** Works on older logs, but every arm would need the user to call it, and scorers could not read the value.

## Compatibility

- **`SwarmMetrics.critical_steps_to_answer`** is optional with a `None` default, so metrics written before it load with `None`; the metrics `version` stays 1.
- **Evidence.** A read's provenance is the typed `CommunicationEvent.record_ids` field; read and wait dependencies are in `data.depends_on`. Evidence compatibility is defined in [swarm-event-types.md](swarm-event-types.md#compatibility-and-migration). The metric reads only the live run and does not recompute older logs.
- **Public API.** `WaitNews.depends_on` defaults to empty, so `WaitNews(lines, files)` stays valid. `Bus.read()` is reached through `ChannelRun.bus`, whose type is the public `Bus` protocol of [swarm-orbit.md S2](swarm-orbit.md#s2-a-public-bus-api-for-channels).

## Security

- **No untrusted text reaches the computation.** It reads span ids, positions, model event status, and from the swarm's own evidence member names and record ids; paths, payloads and file contents are neither stored in the `PathLog` nor read. Record ids come from channel state, assigned by the bus, never from a model's arguments; a model reaches them only by making writes and reads the channel validated.
- **Channel and agent code are the eval author's and trusted.** The bus still refuses an id the channel did not commit, so a buggy channel fails loudly rather than linking another channel's or another run's records. Agent code can write a `CommunicationEvent` with source `inspect_swarm` or a `SwarmEvent` through `transcript()._event()`; a `depends_on` naming no recorded commit yields `None` and a warning, never an exception.
- **Size.** Results are driven by model behaviour and views are not rate-limited, so the evidence must not grow with views times history within a loop: per-loop trimming bounds the `depends_on` ids a run writes by its commits times its loops ([The bus checks and trims](#the-bus-checks-and-trims)), and `record_ids` is one id per file or message shown. The bus keeps one `_committed` entry per commit and one `_depended` entry per commit per loop that depended on it, the board one history id per version. The `PathLog` keeps every result's `depends_on` list, and the metric visits every id in it, so its work and memory are linear in the events of the interval plus the dependency ids they carry. Those ids are bounded by commits times loops, not by events: N commits to one file followed by L fresh loops each viewing it once are O(N + L) events carrying N × L ids.

## Not this design

- **Concurrency inside a member**, counted by its longest branch rather than its sum: a design of its own, which first records causal evidence of which task started a child and which task awaited it (and where), and which work a member's selected output came from. AnyIO exposes a task's identity and its parent, but not await points, and a task-group join writes no transcript event, so it may need an inspect_ai change.
- **An analysis helper over logs**, such as `swarm_critical_path(sample)` in `inspect_swarm.analysis`, for logs written before the metric. It would need, recorded in the log, a position for each event that holds for restored events too.
- **The chain itself**, as the members and step counts along it, for explaining a number; the metric records its length only.
- **`SwarmColumns` for metrics.** The columns read the ledger; metrics are read from the store entry.
- **Edges for a member started by a controller** with another member's output, which would need the controller to record what it passed.
- **Edges through a nested swarm's channels** for the outer swarm's metric; the inner swarm's own metrics count them.
- **Ends shown outside a wait.** `list_members` shows which members have ended and how; it writes no evidence, so a member that learns of an end there gets no edge from it, where a wait naming the end does.
- **Status lines as edges.** The board's changed-file line and the unread-message count, on a wait or on other results, tell a loop that commits happened, with ids or counts; by both channels' rules they make no edge. Revisit only if members are seen acting on them without a read.
- **Bridged members' reads**, which reach the bus only once bridged bus tools exist; they will report through the same `Bus.read()`.
- **Line-level attribution of a partial view**, which would need the board replayed line by line.
