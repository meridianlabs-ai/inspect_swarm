# Inspect Swarm: channels report the records their results showed

## Overview

`critical_steps_to_answer` learns which writes a member depended on from the channels, not from decoding their evidence. Every result a channel returns to a loop, a `shared_memory` view, a `read_messages` read, a custom channel's read, and each channel's part of a `swarm_wait` result, records `records`: the ids of the committed records whose content or news it showed. A record's commit already has a `delivered` event naming its id and its sender. The metric links each result to the commits it lists, and each commit to its sender's chain at the commit, the same way for every channel: it no longer knows which channel is the built-in board or the messages channel, and reads no files, versions or entry shapes.

Each channel keeps deciding what its results showed. The board keeps its rules: a view lists every write in the shown file's history, a tombstone view the delete, a wait the peer write it reported, a listing nothing. The messages channel lists the messages a read returned and the unread messages a wait counted, so a reader follows each sender's chain at its send. A custom channel reports through a new `WaitNews.records` field and a new `Bus.read()`, beside `Bus.deliver()`. The bus refuses an id the channel did not commit in this run, and writes each record into a loop's evidence once, which bounds the evidence however often a long file is viewed.

## Background

The metric, its graph and its limits are designed in [swarm-critical-path.md](swarm-critical-path.md); this design replaces how that graph learns its cross-member edges and keeps everything else (steps, positions, member chains, ends, the roster filter, resumed samples). The channels' evidence is designed in [swarm-memory-channel.md Evidence](swarm-memory-channel.md#evidence), [swarm-wait.md Evidence](swarm-wait.md#evidence) and [swarm-communication.md Evidence and provenance](swarm-communication.md#evidence-and-provenance); the user docs describe it in [Shared Memory, The record of the board](https://meridianlabs-ai.github.io/inspect_swarm/shared-memory.html#the-record-of-the-board) and [Messages, The record of messages](https://meridianlabs-ai.github.io/inspect_swarm/messages.html#the-record-of-messages). The direction, that the channel reports the dependency and the metric links generically, is Ransom's (decision: Ransom, 2026-10-30). This design settles the messages channel's edge rule, which was left open.

## Why

The metric reads each channel's own evidence, so every channel is a special case and every new one is a gap:

- **It decodes the board.** `PathLog` keeps the built-in board's `delivered` file entries and `read` file versions, rebuilds each file's version history and, for waits, each version's latest write by another loop, and checks that a read's version was current (`src/inspect_swarm/_critical_path.py:111-126`, `:189-237`). It must know which channel is the built-in board by instance (`builtin_board()`, `src/inspect_swarm/_channel/shared_memory.py:947-951`, called at `src/inspect_swarm/_controller/_control.py:398`).
- **It sniffs wait entries.** A `waited` event's `files` is one list of every reached channel's `WaitNews.files`, with nothing saying which channel named each (`src/inspect_swarm/_bus/_wait.py:267-274`). So the metric keeps a wait's file entries only when the board and at most the built-in messages channel were reached, skips the entries that name a `record`, and drops every file link from a wait that reached any custom waiting channel (`_critical_path.py:91-110`).
- **The messages channel makes no edge.** A member that reads a ten-step peer's message and answers in one step has `critical_steps_to_answer` 1, though its answer could not exist before the peer's ten steps. Its evidence already names the messages a read showed (`records`, `src/inspect_swarm/_channel/messages.py:368-379`); the metric ignores it.
- **A custom channel makes no edge at all**, whatever it shows.
- **A wait that reports more than ten files** names only the first ten in its evidence (`_LISTED_IDS`, `shared_memory.py:45`, `:406-412`), so the eleventh file's writer makes no link though the result named it as a writer.

## Goals and non-goals

Goals:

- One generic edge rule in the metric: a result follows the commits it lists; a commit is its sender's chain at the commit. No channel names, no channel payloads.
- Each channel states what its results showed, in its evidence, by its own rule: the board's current rules unchanged in effect, a rule for the messages channel, and a public way for a custom channel.
- The cases the metric had to special-case removed: mixed-channel waits, the messages channel, custom channels, the ten-file wait cap.

Non-goals:

- Changing the graph otherwise: steps, positions, one chain per member, member ends, the answer's end, `None` cases and resumed samples are as [swarm-critical-path.md](swarm-critical-path.md) designs them.
- Edges from what a result shows that is not a record: a member's end in `list_members`, the unread-message count and the board's changed-file line on other results ([Not this design](#not-this-design)).
- Line-level attribution of a partial view.
- A fully public API for writing a channel with records: `Bus` and `Draft` stay unexported ([Not this design](#not-this-design)).
- Recomputing the metric from saved logs.

## Current behaviour

### What the metric records

`PathLog(board, messages, roster)` (`_critical_path.py:49-64`) is fed every event of the swarm's interval with its position, the transcript's event count when it was first reported (`src/inspect_swarm/_ledger.py:288-291`). It drops an evidence event whose `member` is not in the roster before reading anything else in it (`_critical_path.py:80-82`), so a nested swarm's events, which name its own members, never reach it. For a roster member it keeps:

| Event | Kept when | What |
|---|---|---|
| `delivered` | its `channel` is the built-in board | position, member, loop, `(file, version, deleted)` of each entry of `result.files` |
| `read` | the built-in board's, not a listing | position, member, `(file, version, deleted)` of each entry of `files` |
| `waited` | always | position, member, loop, `ended` in the roster; `(file, version)` of the entries without `record` only when `channels` include the board and nothing but the built-in messages channel besides |
| `member_result` | always | the member's end position |

`critical_steps_to_answer()` (`:156-239`) walks the nodes in position order with one running value per member: a step adds one; a board write records the writer's value per file version, the largest value along the file's history, and each version's latest earlier write by another loop; a read takes the history's largest value (a tombstone the delete's); a wait takes, per named version, that version's write, or the latest write by another loop when the waiter's loop wrote it; `ended` takes the ended member's value at its end. A read of a version that is not current, or a wait naming a version no roster write made, logs a warning and returns `None` (`:211-227`). `SwarmRun.run()` finds the board and messages channel by state type, `builtin_board()` and `builtin_messages()` (`messages.py:394-397`), and passes them to `LedgerInterval` (`_control.py:390-401`, `_ledger.py:378-386`); nothing else calls those two functions.

### What the channels write

Every comm event is written by `src/inspect_swarm/_evidence.py` with the acting `member`, its member span, the acting `loop` (an agent span) and `nested`:

- **`delivered`**, from `Bus.deliver()` step 4 for every channel: `channel`, `record` (the record id), `seq`, `notify`, and `result`, what the channel's `apply()` returned (`_evidence.py:77-99`, `src/inspect_swarm/_bus/_bus.py:196-202`).
- **`read`**, from `comm_read()` (`_evidence.py:102-132`), which takes either `files` or `records`. The board writes `files`, each with the file id, the version shown, `record` (the write that made that version), the line range and `deleted`, and `listing` (`shared_memory.py:885-904`, `:1067-1074`). The messages channel writes `records`, the ids of the messages the read returned and marked read, and writes no event for a read that showed none (`messages.py:349-383`). Nothing outside these two modules calls `comm_read()`.
- **`waited`**, from `Waits.wait()` when the wait returns or is cancelled (`_wait.py:299-318`): `channels`, the waiting channels the loop reaches, `woken_by`, the `seq` of each commit that released it, `files`, every reached channel's `WaitNews.files` in channel order, and `ended`, the members the result named as ended. A cancelled wait writes `files: []` and `ended: []` (`:295-297`). The board's entries are `{file, version, deleted}` for at most the first ten files it reports (`shared_memory.py:398-412`); the messages channel's are `{record, from}` for every unread message (`messages.py:247-257`).

What the board's results show:

- **A view of a live file** shows its current version, whose content is the sum of every write in its history. **A view of a deleted file's id** shows that it was deleted and by whom (`shared_memory.py:838-841`). **A listing** shows ids, versions, writers and paths (`:860-883`). The `changes` command and the line that ends every result name changed files by id, and write no evidence (`:501-506`, `:936-942`).
- **A wait** reports each file under its filter that has a version newer than the loop has seen, by a loop other than the waiter's (`_is_news()`, `:911-917`). It names the first ten by id and every file's writer: "11 shared memory files changed where you waited (f-1 new, ..., f-10 new, and 1 more; by p, q)" (`_writers()`, `_changed_where_you_waited()`, `:961-962`, `:1104-1106`). When the waiter's own blind append made the current version, the write the result reports is the latest one by another loop, which each file keeps as `peer_version` and `peer_writer` (`_by_others()`, `:954-958`, set in `apply()` at `:336-349`).

What the messages channel's results show: `read_messages` returns whole unread messages, oldest first, and marks exactly those read (`messages.py:349-383`); a wait in the member's own loop with any unread `wake=True` message reports every unread message, by count and senders, wake or not (`:247-257`). The unread count that ends other tools' results (`unread_line()`, `:385-388`) writes no evidence.

### Records and commits

- **Ids are unique within a run.** A record id is its channel's prefix, `-` and a number (`_bus.py:156-158`), and no two channels of a swarm share a prefix or a record name (`src/inspect_swarm/_swarm.py:304-313`); the number follows the last `-`, so ids of different channels never coincide. A record refused after the channel's `validate()` has consumed its number (`_bus.py:173-175`), so the ids alone do not say which were committed.
- **A commit is one synchronous step.** `deliver()` has no `await` from its checks to its `delivered` event (`_bus.py:183-202`): the `sent` and `delivered` events and `apply()` run back to back, so a message's send and its delivery are the same point in the sender's work.
- **A result is written after the commits it shows.** A board view marks the version and writes `read` in one synchronous call, and a write's `apply()` and `delivered` event run with no `await` between them; a wait's news is computed after its last `await` and written in its `finally` (`_wait.py:252-318`); a message is in an inbox only after `apply()`.

### Who writes channels

A custom channel with records reaches the bus through `ChannelRun.bus` (`src/inspect_swarm/_channel/_channel.py:84-92`), whose `deliver()` takes a `Draft`. Neither `Bus` nor `Draft` is exported (`src/inspect_swarm/__init__.py`); [Channels, Writing a channel](https://meridianlabs-ai.github.io/inspect_swarm/channels.html#writing-a-channel) documents only that such a channel's records go through the swarm's bus. The only custom channels with records are in the tests: `tests/test_bus.py:763-784` (wait news with no file entries) and `tests/test_metrics.py:1255-1311` (a custom waiting channel named `shared_memory` or `messages`, whose wait entries are `{"foreign_record": "x-1"}` and `{"file": "f-99", "version": 7}`, and whose agent writes a `read` with the private `comm_read()`). No example, doc page or checkout beside this repository under `~/git` defines one (searched for `swarm_channel`, `WaitNews` and `WaitingState`). So the custom-channel contract below is designed for the shapes the built-in channels use, and refuses others with an error.

## Design

### At a glance

```
 P (react)   p1 → … → p10 ─ send_message m-1 ─→ p11 … p15        m-1 committed at p's value 10
                              │
 R (react)   r1 ──────────────┴─→ read_messages [m-1] → r2       r2 = max(r1, 10) + 1 = 11

 A (react)   a1 → a2 ─ append f-1 (w-1, v1) → a3 ─ append f-1 (w-2, v2)
 B (react)   b1 → b2 ─ view f-1 v2: records [w-1, w-2] → b3      b3 = max(b2, 2) + 1 = 3
```

The view lists both writes of the version it showed; the metric takes the largest value among them. It does not know that `w-1` is a board write or `m-1` a message.

### The contract: a result names the commits it showed

Every channel result that shows peer state carries `records`, the ids of the commits whose content or news it showed, by the channel's own rule. Each id must be a record the same channel committed in this run, earlier. The metric links the result to every commit it lists. An empty list makes no edge. A channel may report a record again; the bus writes it into a loop's evidence once ([The bus](#the-bus)).

What counts as "showed" is the channel's decision, and it states the rule in its docs. The two rules a channel needs to get right: news counts (a result that tells the loop a commit happened, without its content, as a wait does, lists it), and a result whose content is built from several commits lists them all.

### The bus

`Bus` (`src/inspect_swarm/_bus/_bus.py`) gains:

```python
    # record id -> the channel that committed it, filled at step 4
    _committed: dict[str, str]
    # loop agent span -> the records already written into its results' evidence
    _reported: dict[str | None, set[str]]

    def read(
        self,
        channel: str,
        records: Sequence[str],
        *,
        files: list[JsonValue] | None = None,
        listing: bool = False,
    ) -> None:
        # a result of the channel's tool showed the calling loop these records:
        # writes its `read` evidence, for the member and loop calling, as deliver()
        # binds the sender

    def reported(self, channel: str, loop: str | None, records: Sequence[str]) -> list[str]:
        # the records a result of the channel showed the loop, checked and trimmed:
        # each must be one the channel committed in this run; returns those not yet
        # written for the loop, in order and once each, and marks them written
```

- **Commits.** `deliver()` adds `record.id -> record.channel` to `_committed` at step 4, beside the `delivered` event.
- **`read()`** is called by a channel's tool, inside the member's tool call. It finds the caller with `member()`, raising `RuntimeError` outside a member of this run, as `deliver()` does (`_bus.py:237-243`); checks the channel is open (`_state()`, `ValueError` otherwise); takes the loop from `current_agent_span_id()`, `nested` from `nested()`, the tool span from `current_span_id()`, the read id from `read_id()`; passes `records` through `reported()`; and writes `comm_read()` with them. `files` and `listing` are the board's details and are written only when given; a custom channel leaves them out. `comm_read()` is the only writer, and the board and messages channel call `read()` instead of it.
- **`reported()`** raises `ValueError` for an id the channel did not commit: "{channel}: a result names {id}, which is not a record this channel committed in this run." That is a bug in the channel's code, so it fails the tool call and the sample loudly, as any non-`ToolError` exception in a tool does. It then drops ids already in `_reported[loop]` and duplicates, adds the rest, and returns them.
- **Why trim per loop.** A loop belongs to one member, and a member's value in the metric never decreases along its chain, so a commit the loop's earlier result linked raises nothing when linked again: the metric is exactly the same. Without trimming, a board view writes one id per version of the file, every time it is viewed, and views are not rate-limited: a shared log of 300 appends viewed 50 times by each of 6 members writes 90,000 ids. With it, every commit is written at most once per loop, so the `records` a run writes are bounded by its commits times its loops, whatever the views. The trimming is the bus's, so no channel keeps seen state for it, and channels report what their results showed, not what is new.

### Evidence

- **`read`** gains `records` for every channel: the ids `reported()` returned, possibly empty. The board's event keeps `files` (still each file's version and its last write, `record`) and `listing`; the messages channel's keeps its `records`, now trimmed as every channel's are (a message is read once per member, so nothing changes in practice). `comm_read()` takes `records: list[str]` always, and `files: list[JsonValue] | None`, written only when not `None`.
- **`waited`** gains `records`: for each reached channel in order, the ids of its `WaitNews.records` passed through `reported()` for the waiting loop. `files` is unchanged: the merged entries the result's lines name, the board's capped at ten. A cancelled wait writes `records: []`, as it writes `files: []`.
- **`delivered`** is unchanged: its `record` is the commit's id and its `member` the sender.
- The payload `version` stays 1, as it did when `idle` and the messages channel's `records` were added: both fields are additive and every reader reads keys by name.

`WaitNews` (`src/inspect_swarm/_channel/_channel.py:162-170`) gains:

```python
    records: list[str] = field(default_factory=list[str])
    """The ids of the records whose content or news `lines` report, each one this channel committed in this run. A `swarm_wait` result that reports them makes the waiting member's later work depend on them in the swarm's critical path. Empty for news that reports no record."""
```

`Waits.wait()` collects each channel's `news.records` beside its `files` (`_wait.py:267-274`) through `self._bus.reported(channel, spec.loop, news.records)`. The collection runs after the wait's last `await`, so a cancellation cannot fall between marking and writing. A channel whose news names a record it did not commit raises from the wait, and the `finally` still writes the `waited` event with what was collected before it.

### The board's rule

The board reports exactly the writes the metric links today, so its edges do not change except for the ten-file cap ([Waits and the cap](#waits-and-the-cap)):

| Result | `records` |
|---|---|
| a view of a live file, full or with `view_range` | every write in its history: the record of each version from 1 to the shown one, renames and the writes that created and edited it included |
| a view of a deleted file's id | the delete: the tombstone's `record` |
| a listing | none (`[]`) |
| `changes` and the status line on other results | no evidence, as today |
| a `swarm_wait` result | for every file it reports, all of them, the write it reported: the file's `record` when another loop wrote the current version, and otherwise the latest version by another loop, `peer_record` |

`_SharedMemoryState` keeps `_history: dict[str, list[str]]`, file id -> the record of each version in order, appended in `apply()` for each change, so its length is the current version; a live view passes the whole list. `_BoardFile` gains `peer_record: str | None` beside `peer_version` and `peer_writer`, and `_by_others()` returns it with them. `news()` builds `records` from every reportable file, not only the ten it lists in `files`. `_read()` calls `self._bus.read(CHANNEL, records, files=..., listing=...)` instead of `comm_read()`.

### The messages channel's rule

A message's commit is the point in the sender's work its reader follows: the sender's chain at the `delivered` event of the send, which is also the `sent` event, since a commit is one synchronous step. Work the sender did after the send is not linked; the steps that led to it are, the generation that called `send_message` included, since its call began before the commit.

| Result | `records` |
|---|---|
| `read_messages` | every message the read returned (and marked read) |
| a `swarm_wait` result with the channel's news | every unread message it counted, `wake=False` ones included: the line counts them all and names all their senders |
| the unread count on other results, `list_members` | no evidence, as the board's status line |

A wait that tells a member that a ten-step peer sent it something makes its next step follow those ten steps, as a board wait does; a later `read_messages` of the same message links nothing more. `_read()` calls `self._bus.read(CHANNEL, [r.id for r in read])`; `news()` adds `records=[r.id for r in unread]`.

### Custom channels

A custom channel reports with the same two calls the built-in channels use:

- **A read**: its tool calls `run.bus.read(self.name, records)` with the ids of the records the result shows, from the `ChannelRun` its `open()` was given. A tool that shows nothing need not call it.
- **A wait's news**: `news()` returns `WaitNews(lines, files, records=[...])`.

A channel that reports nothing makes no edge, as today; its records still count in the communication metrics, and its `delivered` events are commits a later report can name. An id the channel did not commit in this run fails the tool call with `ValueError`; so do a call outside a member of this run and a channel that is not open. A channel's name does not matter: one named `shared_memory` or `messages` is treated as any other, since the metric reads no channel name. `ChannelRun.bus`'s docstring and [Channels, Writing a channel](https://meridianlabs-ai.github.io/inspect_swarm/channels.html#writing-a-channel) say how to report, with the two rules above (news counts; a result built from several commits lists them all).

### The metric

`src/inspect_swarm/_critical_path.py` becomes:

```python
# A commit (`delivered`): its record id and sender.
@dataclass(frozen=True)
class PathCommit:
    position: int
    member: str
    record: str


# A result that showed records (`read` or `waited`): the records it listed, and for a
# wait the roster members whose ends it named.
@dataclass(frozen=True)
class PathResult:
    position: int
    member: str
    records: list[str]
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
    completed: Callable[[str], bool],
    places: Callable[[str | None], tuple[bool, str | None]],
    answer: str | None,
) -> int | None: ...
```

- **Recording.** As today, an evidence event is dropped unless its `member` is in the roster, before anything else in it is read, and is taken once by uuid. From a roster member's events it reads: `member_result`; a `comm` `delivered`'s `record`; a `comm` `read`'s `records`; a `comm` `waited`'s `records` and `ended` (the bus's, not a channel's), the roster members only. It reads no `channel`, `channels`, `files`, `listing`, `result` or `loop`, and ignores `sent`.
- **Computing.** One pass in position order with a running value per member, as today: a step adds one; a commit records its record's value, the sender's value there; a result raises the member's value to the largest value among the records it lists and the ends it names; the answering member's `member_result` returns its value. A result listing a record with no earlier commit in the log logs a warning naming the member and the record and returns `None`: the bus refuses such an id, so it means evidence written outside the bus.
- **Wiring.** `LedgerInterval(roster=...)` creates `PathLog(roster)`; `SwarmRun.run()` no longer looks up the board or the messages channel, and `builtin_board()` and `builtin_messages()` are removed, having no other caller. `_metrics.py` is unchanged but for the field comment:

```python
    # critical path, the dependency-linked definition: the longest chain of steps
    # (completed model calls, cache replays included) ending at the answering member's
    # end, linked across members where a channel's result showed another member's
    # commit, as the channel reports it, or a wait named a member's end. Not a bound on
    # causal depth either way; see docs/analysis.qmd, "The critical path". None with no
    # answer, when the answering member has no recorded end, when the evidence names a
    # record no recorded commit made (with a warning), for a resumed sample, and in
    # metrics written before this field.
```

**Acyclic**, as before: a result lists commits written before it ([Records and commits](#records-and-commits)), and the bus refuses any other id.

### Waits that return at once, vanish, or are cancelled

`records` comes from what the result reported, read from each channel's `news()` when the wait returns, never from `woken_by`. A wait that finds news at once has `woken_by: []` and lists the records its news names. A release whose news has gone by the time the waiter resumes keeps its commit in `woken_by` and, unless the news is back, lists nothing for it. A cancelled wait lists nothing. A wait released by `all_waiting`, `stopping`, `member_ending` or its timeout still lists the news it reports, as it names its files today.

### Waits and the cap

The cap stays on `files`, which are the ids the result's lines name, ten at most. `records` has the write of every file the result reports, since the result names every one's writer and counts them all. So the board's eleventh file links its writer, where today it links nothing until a view names it. A wait's `records` are bounded by the files it reports and trimmed per loop, so a loop that keeps waiting without viewing writes each write once.

### Nested swarms and the roster

Each run has its own bus, so a nested swarm's channels check and trim against its own commits, and its record ids, which restart at 1, can equal the outer run's. Its evidence names its own members, so the outer `PathLog` drops it before reading `records`, as today: an inner `w-1` never reaches the outer metric's commits. The inner members' steps still count for the outer member through `SpanPlaces`. An outer member's own tools, called while a nested swarm runs inside it, write outer evidence about outer records.

### The shapes

| Shape | Records | Length |
|---|---|---|
| R reads a message from P, sent after P's ten steps, then makes one step | the read lists `m-1` | 11 |
| The same, P working five more steps after the send | the commit is at P's tenth step | 11 |
| R's wait counts P's unread message (`wake=True`, or `wake=False` beside a board write that woke it); one step after | the wait lists `m-1`; the read after it adds nothing | 11 |
| R's results show the unread count only, and R never reads | none | R's own chain |
| B in the board example above | the view lists `w-1`, `w-2` | 3 |
| A wait after the waiter's own blind append to a file a ten-step peer wrote; one step after | the wait lists the peer's write (`peer_record`) | 11 |
| The same wait repeated before any view | the second lists nothing (trimmed); the value is unchanged | 11 |
| A wait reporting eleven files, ten by a one-step P and the eleventh by a ten-step Q; one step before and after | `files` names ten, `records` all eleven writes | 11 (2 today) |
| A wait reaching the board, messages and a custom channel whose news has no records | the board's and messages' records link; the custom channel's entries are `files` only | as the board and messages give |
| A custom channel whose tool calls `run.bus.read(name, ["s-3"])` for a ten-step writer's `s-3`; one step after | the read lists `s-3` | 11 |
| A custom channel named `shared_memory`, alone, reporting nothing | no records; its waits' `ended` still link | its waits' ends only |
| A custom channel reporting `w-1` that the board committed, or `s-99` that it never committed | `ValueError` from the bus | the sample fails |
| A listing, then a view of one listed file | the listing lists none; the view lists the file's history | the file's history |
| A nested swarm committing its own `w-1` inside an outer member | dropped by member before its `records` are read | the outer chains |

The shapes of [swarm-critical-path.md The shapes](swarm-critical-path.md#the-shapes) keep their lengths, except the eleven-file wait above and the waits that also reached a custom waiting channel, which now link the board's and messages channel's records.

## Alternatives considered

- **The metric attributes `waited.files` by channel**, with each channel's entries tagged, and keeps decoding the board and adds a decoder for messages. It fixes mixed waits but keeps every channel a special case in the metric and gives custom channels nothing (decision: Ransom, 2026-10-30: the channel reports the dependency).
- **A commit names the commits it extended** (`delivered` with the previous version's record), so a view lists only the shown version's write and the metric follows lineage. Views would write one id instead of the history, but the metric would need a second generic edge, commit to commit, that every custom channel has to understand, and a wait reporting a peer write would inherit that write's whole file history, where today it links the write alone. Per-loop trimming bounds the history lists without either change.
- **Each channel trims its own reports**, as the board could with its per-loop seen state. Every channel would need the state, `news()` may not mark anything (`WaitingState`, `_channel.py:173-177`) and is called repeatedly while a wait sleeps, and a channel that forgot would grow the evidence. The bus sees every report already.
- **No trimming.** Exact and simplest, but a view's evidence grows with the file's history on every view, and views are not rate-limited, so a long shared log, the usage `append` exists for, multiplies its history by every view of it.
- **Each version's history value in the evidence instead of ids** (a view writes the largest commit "depth" it shows). The channel would compute the metric's values, which needs the step counts it does not have.
- **Linking a message at the sender's last step before its read**, rather than at the commit. It would charge the reader for the sender's work after the send, which the message could not contain.
- **Keeping the ten-file cap on links.** The result names every file's writer, so the eleventh writer's news reached the model; leaving it out was a limit of the evidence, not a rule.
- **Messages' wait news linking only `wake=True` messages.** The line counts every unread message and names every sender, so it reports the `wake=False` ones too.
- **Making `WaitNews.records` required.** No custom channel exists outside the tests, but news that reports no record (a timer, a test channel's note) is legitimate; an empty default keeps `WaitNews(lines, files)` valid and makes no edge.

## Compatibility and migration

- **Evidence.** `read` and `waited` gain `records`; nothing is removed or renamed, and `version` stays 1. A log written before has no `records` on the board's `read` or on `waited`; a reader treats the key's absence as "not reported". The metric reads only the live run ([swarm-critical-path.md Where it is computed](swarm-critical-path.md#where-it-is-computed)), so old logs are not read by it. The messages channel's `read.records` keeps its shape and meaning, trimmed per loop, which a member's single read of each message never triggers.
- **The planned typed event.** [swarm-event-types.md](swarm-event-types.md) maps a `read`'s `record_ids` from the board's per-file `record` values, a listing's included, and gives `waited` none. With this design, `records` is what a `read` and a `waited` showed by the channel's rule, which is the join a dependency reader needs; [Open questions](#open-questions) asks which `record_ids` carries.
- **Public API.** `WaitNews` gains `records` with an empty default, so existing constructions stay valid. `Bus.read()` is new beside `Bus.deliver()`, both reached through `ChannelRun.bus`; `Bus` stays unexported. No name is removed from `inspect_swarm`; `builtin_board()` and `builtin_messages()` are private.
- **The metric's values change** for swarms with the messages channel (reads and waits now link), for waits that reach a custom waiting channel beside the board (the board's records now link), for waits reporting more than ten files, and for custom channels that report records. `SwarmMetrics` is unchanged; its `version` stays 1, since the field's type and presence are the same.
- **Docs.** `docs/analysis.qmd`, The critical path: links come from any channel's results as the channel reports them (the board's views and waits, `read_messages` and the messages counted by a wait, a custom channel that reports), and member ends named by a wait. Removed: the paragraph that a wait reaching a custom waiting channel links no files, "messages" and "a custom channel and its wait results" from the list of what makes no link (replaced by "a custom channel that reports no records"), and the sentence on waits reporting more than ten files. "When the board's evidence is inconsistent" becomes "when the evidence names a record no recorded commit made". `docs/shared-memory.qmd` and `docs/messages.qmd`, the record sections: `read` and `waited` list the records their results showed, by the rules above. `docs/channels.qmd`, Writing a channel: how a channel reports. Reference: `WaitNews.records` and `ChannelRun.bus` docstrings.
- **Designs.** [swarm-critical-path.md](swarm-critical-path.md): its Goals ("no change to ... the evidence"), the edge table, the board-by-instance and custom-waiting-channel paragraphs, the `PathLog` code, the shapes for mixed and capped waits and its "Not this design" entry on messages edges describe this design's rule instead. The evidence sections of [swarm-memory-channel.md](swarm-memory-channel.md#evidence), [swarm-wait.md](swarm-wait.md#evidence) and [swarm-communication.md](swarm-communication.md#evidence-and-provenance) add `records`.

## Security

- **What reaches the new code.** Record ids come from channel state, assigned by the bus (`_bus.py:156-158`); no channel takes one from a model's arguments into `records`. The board's ids come from its history and files, the messages channel's from its inboxes. A model reaches them only by making writes and reads the channel validated, as today.
- **Custom channel code** is the eval author's and trusted, as the rest of a channel is; the bus still refuses an id the channel did not commit, so a buggy channel fails loudly rather than linking another channel's or another run's records.
- **Agent code** can write an `InfoEvent` with source `inspect_swarm` naming a roster member, as today; a `records` list naming no recorded commit yields `None` and a warning, never an exception, and the `PathLog` keeps only ids, positions and member names.
- **Size.** Model behaviour drives how many results there are, and views are not rate-limited, so the evidence must not grow with views times history: per-loop trimming bounds the `records` a run writes by its commits times its loops ([The bus](#the-bus)). Memory: `_committed` one entry per commit, `_reported` one per commit per loop that saw it, the board's `_history` one id per version, all within the evidence's own size. The metric's work stays linear in the ids it reads.

## Testing

All async tests run on asyncio and trio through the `anyio_backend` fixture. No test needs Docker, a network or a model provider.

- **`tests/test_bus.py`**: `Bus.read()` writes a `read` with the caller's member, loop, `nested`, tool span and the records; refuses an id another channel committed, one never committed and one a refused record consumed (`ValueError`), and a call outside a member (`RuntimeError`); trims ids already written for the loop, keeps them for another loop of the same member; a waiting channel whose `WaitNews.records` names an uncommitted id fails the wait with `ValueError` and still writes `waited`.
- **`tests/test_shared_memory.py`** (or `test_comm_evidence.py`, where the evidence tests live): a view's `records` are the file's history (create, appends, an edit, a rename of its directory); a partial view the same; a tombstone view the delete; a listing `[]`; a second view of the same version by the same loop `[]`, by another loop the history again. Waits: `records` names the peer write after the waiter's own blind append, between two members and between two loops of one member; a wait that returned at once; a release whose news vanished (its `woken_by` set, its record not listed); a cancelled wait (`[]`); eleven files (ten in `files`, eleven in `records`). The exact `waited` dictionary at `tests/test_comm_evidence.py:211-227` gains `records`.
- **`tests/test_wait.py`**: `test_eleven_files_are_named_ten_and_one_more_by_each_wait` (`:285-294`) also asserts eleven `records` for the first wait and none for the repeat.
- **`tests/test_messages.py`**: a read's `records` (unchanged assertion at `:159-163`); a wait's `records` are every unread message, `wake=False` included; a wait's records are trimmed on a repeat.
- **`tests/test_critical_path.py`**: the helpers become `write(member, record)` (a `delivered` with that id), `read(member, *records)` and `wait(member, *records, ended=...)`, and `_value()` builds `PathLog(ROSTER)`. Rows: every shape above in record terms (history, tombstone, peer record, eleven records), a record of any channel prefix, a result naming a record with no earlier commit (`None` and a warning), a result naming its own member's commit (no change), a nested swarm's events with the same ids under non-roster members (dropped, no warning), errored and cache-replayed model events, no answer and no `member_result` (`None`). The rows that tested the board-by-instance, the messages entries in `files` and custom channel shapes in `files` go: the metric no longer reads them.
- **`tests/test_metrics.py`**, mockllm swarms through `run_swarm()`: a messages swarm where a ten-step sender sends, works on, and a one-step reader reads and answers (11); the same with a wait before the read (11); the board cases already there keep their values; a registered custom channel whose tool calls `run.bus.read()` and whose news sets `records` (each links); the custom channels named `shared_memory` and `messages` (`:1255-1381`), which report no records, keep their values (2, 11); a new case puts a ten-step peer's board write before a wait that also reaches the custom waiting channel, which now links (11 where it was 1); a nested swarm with the board (its `w-1` ignored by the outer metric).

## Implementation plan

1. **Channels report records.** The metric is untouched and keeps reading `files`, which do not change, so `main` works between the steps.
   - `src/inspect_swarm/_bus/_bus.py`: `_committed`, `_reported`, `read()`, `reported()`.
   - `src/inspect_swarm/_bus/_wait.py`: collect `records` through `reported()`; write them in `waited`.
   - `src/inspect_swarm/_channel/_channel.py`: `WaitNews.records`; `ChannelRun.bus` docstring.
   - `src/inspect_swarm/_evidence.py`: `comm_read(records, files=None, ...)`, `comm_waited(records=...)`.
   - `src/inspect_swarm/_channel/shared_memory.py`: `_history`, `peer_record`, view, tombstone and listing records, wait records, `_read()` through `Bus.read()`.
   - `src/inspect_swarm/_channel/messages.py`: `_read()` through `Bus.read()`, `news()` records.
   - `docs/channels.qmd`, `docs/shared-memory.qmd`, `docs/messages.qmd`.
   - Tests: `tests/test_bus.py`, `tests/test_shared_memory.py` / `tests/test_comm_evidence.py`, `tests/test_wait.py`, `tests/test_messages.py`.
2. **The metric links records.**
   - `src/inspect_swarm/_critical_path.py`: `PathCommit`, `PathResult`, `PathLog(roster)`, the generic pass.
   - `src/inspect_swarm/_ledger.py`: `LedgerInterval(roster=...)`.
   - `src/inspect_swarm/_controller/_control.py`: no board or messages lookup.
   - `src/inspect_swarm/_channel/shared_memory.py`, `src/inspect_swarm/_channel/messages.py`: remove `builtin_board()`, `builtin_messages()`.
   - `src/inspect_swarm/_metrics.py`: the field comment.
   - `docs/analysis.qmd`: The critical path.
   - `design/swarm-critical-path.md`, `design/swarm-memory-channel.md`, `design/swarm-wait.md`, `design/swarm-communication.md`, and `design/swarm-event-types.md` as the open question is answered.
   - Tests: `tests/test_critical_path.py`, `tests/test_metrics.py`.

## Open questions

- **What the planned `CommunicationEvent.record_ids` carries for `read` and `waited`.** [swarm-event-types.md](swarm-event-types.md) defines a `read`'s `record_ids` as each file's last write at the version read, listings included, and gives a `waited` none; its decision that `record_ids` is the one join across kinds (Ransom, 2026-10-28) stands either way. Recommendation: `record_ids` carries this design's `records` for both kinds, so a reader of the typed event follows dependencies without knowing the producer, and the board's per-file last write stays in `data.files[].record`; step 2 updates that design's field docstring and mapping table. Confidence: high.

## Not this design

- **Ends shown outside a wait.** `list_members` shows which members have ended and how; it writes no evidence, so a member that learns of an end there gets no edge from it, where a wait naming the end does.
- **Status lines as edges.** The board's changed-file line and the unread-message count tell a loop that commits happened, with ids or counts; by both channels' rules they make no edge, as the changed-file line does today. Revisit only if members are seen acting on them without a read.
- **A public API for channels with records**: exporting `Draft` (and `Bus`, or a protocol of `deliver()` and `read()`) and documenting them, so a custom channel needs no private import.
- **Bridged members' reads**, which reach the bus only once bridged bus tools exist; they will report through the same `Bus.read()`.
- **Line-level attribution of a partial view**, which would need the board replayed line by line.
