# Inspect Swarm: channels report what their results depend on

## Overview

`critical_steps_to_answer` learns which writes a member depended on from the channels, not from decoding their evidence. Every result a channel returns to a loop, a `shared_memory` view, a `read_messages` read, a custom channel's read, and each channel's part of a `swarm_wait` result, records `depends_on`: the ids of the committed records whose content or news it showed. A record's commit already has a `delivered` event naming its id and its sender. The metric links each result to the commits in its `depends_on`, and each commit to its sender's chain at the commit, the same way for every channel: it no longer knows which channel is the built-in board or the messages channel, and reads no files, versions or entry shapes.

Each channel keeps deciding what its results showed. The board keeps its rules: a view depends on every write in the shown file's history, a tombstone view on the delete, a wait on the peer write it reported, a listing on nothing. The messages channel makes a read depend on the messages it returned, and a wait with message news on every unread message it counted, so a reader follows each sender's chain at its send. A custom channel reports through a new `WaitNews.depends_on` field and a new `Bus.read()`, beside `Bus.deliver()`. The bus refuses an id the channel did not commit in this run, and writes each dependency into a loop's evidence once, which bounds the evidence however often a long file is viewed. `depends_on` is a field of its own: a `read` keeps `records`, the complete list of the records it concerns, which the planned typed event carries as `record_ids`.

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
- Each channel states what its results depend on, in its evidence, by its own rule, beside the complete provenance a `read` already has: the board's current rules unchanged in effect, a rule for the messages channel, and a public way for a custom channel.
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

What the messages channel's results show: `read_messages` returns whole unread messages, oldest first, and marks exactly those read (`messages.py:349-383`). A wait in the member's own loop has the channel's news only while an unread `wake=True` message is waiting: `news()` returns `None` otherwise (`:247-250`), and `Waits._reportable()` reads `news()` to decide whether the wait returns (`_wait.py:376-385`), which is how a `wake=False` message leaves a wait asleep. With news, the line counts every unread message and names every sender, wake or not, and `files` names them all (`:251-257`). Without it, a wait that returns for another reason ends with the unread count only, the status line (`_wait.py:286-290`). That count, on the wait and on other tools' results (`unread_line()`, `:385-388`), writes no evidence.

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
 R (react)   r1 ──────────────┴─→ read_messages, depends_on [m-1] → r2     r2 = max(r1, 10) + 1 = 11

 A (react)   a1 → a2 ─ append f-1 (w-1, v1) → a3 ─ append f-1 (w-2, v2)   w-1 at 2, w-2 at 3
 B (react)   b1 → b2 ─ view f-1 v2, depends_on [w-1, w-2] → b3            b3 = max(b2, 3) + 1 = 4
```

The view depends on both writes of the version it showed; the metric takes the largest value among them. It does not know that `w-1` is a board write or `m-1` a message.

### Two lists: what a result concerns, and what it depends on

A result's evidence carries two lists of record ids, each checked by the bus to be records the result's own channel committed earlier in this run:

- **`records`, provenance**, on `read` only: the records the result concerns, complete, whatever the loop saw before. It keeps the meaning it has today: for a messages read, every message it returned; for the board, which gains it, each shown file's last write at the version shown, listings and tombstones included, the same ids as its `files[].record`. It is what the planned typed event carries as `record_ids` (decision: Ransom, 2026-10-30).
- **`depends_on`, the dependency**, on `read` and `waited`: the commits whose content or news the result showed, by the channel's own rule, which the metric links. The bus writes each id into a loop's `depends_on` once ([The bus](#the-bus)), so it can be shorter than what the channel reported, and empty.

The two differ wherever the channel's rule does: a board view's provenance is the file's last write but it depends on the whole history; a listing concerns every listed file's last write and depends on nothing; a `read_messages` after a wait that already counted the message concerns it and, in that loop, depends on nothing new.

What counts as "showed" is the channel's decision, and it states the rule in its docs. The two rules a channel needs to get right: news counts (a result that tells the loop a commit happened, without its content, as a wait does, depends on it), and a result whose content is built from several commits depends on them all.

### The bus

`Bus` (`src/inspect_swarm/_bus/_bus.py`) gains:

```python
    # record id -> the channel that committed it, filled at step 4
    _committed: dict[str, str]
    # loop agent span -> the records already written into its results' `depends_on`
    _depended: dict[str | None, set[str]]

    def read(
        self,
        channel: str,
        records: Sequence[str],
        *,
        depends_on: Sequence[str] | None = None,
        files: list[JsonValue] | None = None,
        listing: bool = False,
    ) -> None:
        # a result of the channel's tool concerned `records` and showed the calling
        # loop `depends_on` (by default `records`): writes its `read` evidence for the
        # member and loop calling, as deliver() binds the sender

    def depends(self, channel: str, loop: str | None, depends_on: Sequence[str]) -> list[str]:
        # a result's dependencies, checked and trimmed: each must be a record the
        # channel committed in this run; returns those not yet written for the loop, in
        # order and once each, and marks them written
```

- **Commits.** `deliver()` adds `record.id -> record.channel` to `_committed` at step 4, beside the `delivered` event.
- **`read()`** is called by a channel's tool, inside the member's tool call. It finds the caller with `member()`, raising `RuntimeError` outside a member of this run, as `deliver()` does (`_bus.py:237-243`); checks the channel is open (`_state()`, `ValueError` otherwise); takes the loop from `current_agent_span_id()`, `nested` from `nested()`, the tool span from `current_span_id()` and the read id from `read_id()`; checks `records` as `depends()` does but writes them whole; passes `depends_on` through `depends()`; and writes `comm_read()`. `files` and `listing` are the board's details and are written only when given; a custom channel leaves them out. `comm_read()` keeps being the only writer, and the board and messages channel call `read()` instead of it.
- **The check** raises `ValueError` for an id the channel did not commit: "{channel}: a result names {id}, which is not a record this channel committed in this run." That is a bug in the channel's code, so it fails the tool call and the sample loudly, as any non-`ToolError` exception in a tool does.
- **Why trim `depends_on` per loop.** A loop belongs to one member, and a member's value in the metric never decreases along its chain, so a commit the loop's earlier result linked raises nothing when linked again: the metric is exactly the same. Without trimming, a board view writes one id per version of the file, every time it is viewed, and views are not rate-limited: a shared log of 300 appends viewed 50 times by each of 6 members writes 90,000 ids. With it, every commit is in a loop's `depends_on` at most once, so the dependency ids a run writes are bounded by its commits times its loops, whatever the views. `records` is not trimmed: it is one id per file a view or listing shows and one per message read, as today. The trimming is the bus's, so no channel keeps seen state for it, and channels report what their results showed, not what is new.

### Evidence

- **`read`** gains `depends_on` for every channel, possibly empty, and `records` for the board; the messages channel's `records` keep their meaning and stay complete. The board's event keeps `files` and `listing`. `comm_read()` takes `records: list[str]` and `depends_on: list[str]` always, and `files: list[JsonValue] | None`, written only when not `None`.
- **`waited`** gains `depends_on`: for each reached channel in order, its `WaitNews.depends_on` passed through `depends()` for the waiting loop. `files` is unchanged: the merged entries the result's lines name, the board's capped at ten. A cancelled wait writes `depends_on: []`, as it writes `files: []`. `waited` gets no `records`, as the typed event gives it no `record_ids`.
- **`delivered`** is unchanged: its `record` is the commit's id and its `member` the sender.
- The payload `version` stays 1, as it did when `idle` and the messages channel's `records` were added: every field is additive and every reader reads keys by name.

`WaitNews` (`src/inspect_swarm/_channel/_channel.py:162-170`) gains:

```python
    depends_on: list[str] = field(default_factory=list[str])
    """The ids of the records whose content or news `lines` report, each one this channel committed in this run. A `swarm_wait` result that reports them makes the waiting member's later work depend on them in the swarm's critical path. Empty for news that reports no record."""
```

`Waits.wait()` collects each channel's `news.depends_on` beside its `files` (`_wait.py:267-274`) through `self._bus.depends(channel, spec.loop, news.depends_on)`. The collection runs after the wait's last `await`, so a cancellation cannot fall between marking and writing. A channel whose news names a record it did not commit raises from the wait, and the `finally` still writes the `waited` event with what was collected before it.

### The board's rule

The board's dependencies are exactly the writes the metric links today, so its edges do not change except for the ten-file cap ([Waits and the cap](#waits-and-the-cap)):

| Result | `records` | `depends_on` |
|---|---|---|
| a view of a live file, full or with `view_range` | its last write at the shown version | every write in its history: the record of each version from 1 to the shown one, renames and the writes that created and edited it included |
| a view of a deleted file's id | the delete | the delete |
| a listing | each listed file's last write | none |
| `changes` and the status line on other results | no evidence, as today | |
| a `swarm_wait` result | (none: `waited`) | for every file it reports, all of them, the write it reported: the file's `record` when another loop wrote the current version, and otherwise the latest version by another loop, `peer_record` |

`records` is the distinct `record` values of `files`, in order. `_SharedMemoryState` keeps `_history: dict[str, list[str]]`, file id -> the record of each version in order, appended in `apply()` for each change, so its length is the current version; a live view passes the whole list. `_BoardFile` gains `peer_record: str | None` beside `peer_version` and `peer_writer`, and `_by_others()` returns it with them. `news()` builds `depends_on` from every reportable file, not only the ten it lists in `files`. `_read()` calls `self._bus.read(CHANNEL, records, depends_on=..., files=..., listing=...)` instead of `comm_read()`.

### The messages channel's rule

A message's commit is the point in the sender's work its reader follows: the sender's chain at the `delivered` event of the send, which is also the `sent` event, since a commit is one synchronous step. Work the sender did after the send is not linked; the steps that led to it are, the generation that called `send_message` included, since its call began before the commit.

| Result | `records` | `depends_on` |
|---|---|---|
| `read_messages` | every message the read returned (and marked read), as today | the same |
| a `swarm_wait` result with the channel's news (an unread `wake=True` message is waiting) | (none: `waited`) | every unread message the news counts, `wake=False` ones included: its line counts them all and names all their senders |
| a `swarm_wait` result without the channel's news (only `wake=False` messages unread), whatever ended it | | none: its result has only the unread count |
| the unread count on other results, `list_members` | no evidence | |

A wait that tells a member that a ten-step peer sent it something makes its next step follow those ten steps, as a board wait does; a later `read_messages` of the same message keeps it in `records` and adds nothing to `depends_on`. A message sent with `wake=False` is linked when the member reads it, or when a wait with the channel's news counts it beside a waking one; a wait that only ends with the unread count, as a board write, a peer's end, the timeout or the stop can make it, shows a number, which makes no edge, as the count on every other result. Reporting quiet messages from that wait would need news that does not make the wait reportable, while `_reportable()` treats any news as a reason to return, so a `wake=False` message would end waits. `_read()` calls `self._bus.read(CHANNEL, ids)`, its `depends_on` defaulting to the same ids; `news()` adds `depends_on=[r.id for r in unread]`.

### Custom channels

A custom channel reports with the same two calls the built-in channels use:

- **A read**: its tool calls `run.bus.read(self.name, records)` with the ids of the records the result shows, from the `ChannelRun` its `open()` was given; when what the result depends on differs from what it concerns, it also passes `depends_on=`. A tool that shows nothing need not call it.
- **A wait's news**: `news()` returns `WaitNews(lines, files, depends_on=[...])`.

A channel that reports nothing makes no edge, as today; its records still count in the communication metrics, and its `delivered` events are commits a later report can name. An id the channel did not commit in this run fails the tool call with `ValueError`; so do a call outside a member of this run and a channel that is not open. A channel's name does not matter: one named `shared_memory` or `messages` is treated as any other, since the metric reads no channel name. `ChannelRun.bus`'s docstring and [Channels, Writing a channel](https://meridianlabs-ai.github.io/inspect_swarm/channels.html#writing-a-channel) say how to report, with the two rules above (news counts; a result built from several commits depends on them all).

### The metric

`src/inspect_swarm/_critical_path.py` becomes:

```python
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
    completed: Callable[[str], bool],
    places: Callable[[str | None], tuple[bool, str | None]],
    answer: str | None,
) -> int | None: ...
```

- **Recording.** As today, an evidence event is dropped unless its `member` is in the roster, before anything else in it is read, and is taken once by uuid. From a roster member's events it reads: `member_result`; a `comm` `delivered`'s `record`; a `comm` `read`'s `depends_on`; a `comm` `waited`'s `depends_on` and `ended` (the bus's, not a channel's), the roster members only. It reads no `channel`, `channels`, `records`, `files`, `listing`, `result` or `loop`, and ignores `sent`.
- **Computing.** One pass in position order with a running value per member, as today: a step adds one; a commit records its record's value, the sender's value there; a result raises the member's value to the largest value among the records in its `depends_on` and the ends it names; the answering member's `member_result` returns its value. A result naming a record with no earlier commit in the log logs a warning naming the member and the record and returns `None`: the bus refuses such an id, so it means evidence written outside the bus.
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

**Acyclic**, as before: a result depends on commits written before it ([Records and commits](#records-and-commits)), and the bus refuses any other id.

### Waits that return at once, vanish, or are cancelled

`depends_on` comes from what the result reported, read from each channel's `news()` when the wait returns, never from `woken_by`. A wait that finds news at once has `woken_by: []` and depends on the records its news names. A release whose news has gone by the time the waiter resumes keeps its commit in `woken_by` and, unless the news is back, depends on nothing for it. A cancelled wait depends on nothing. A wait released by `all_waiting`, `stopping`, `member_ending` or its timeout still depends on the news it reports, as it names its files today.

### Waits and the cap

The cap stays on `files`, which are the ids the result's lines name, ten at most. `depends_on` has the write of every file the result reports, since the result names every one's writer and counts them all. So the board's eleventh file links its writer, where today it links nothing until a view names it. A wait's `depends_on` is bounded by the files it reports and trimmed per loop, so a loop that keeps waiting without viewing writes each write once.

### Nested swarms and the roster

Each run has its own bus, so a nested swarm's channels check and trim against its own commits, and its record ids, which restart at 1, can equal the outer run's. Its evidence names its own members, so the outer `PathLog` drops it before reading `depends_on`, as today: an inner `w-1` never reaches the outer metric's commits. The inner members' steps still count for the outer member through `SpanPlaces`. An outer member's own tools, called while a nested swarm runs inside it, write outer evidence about outer records.

### The shapes

| Shape | Evidence | Length |
|---|---|---|
| R reads a message from P, sent after P's ten steps, then makes one step | the read's `records` and `depends_on` are `[m-1]` | 11 |
| The same, P working five more steps after the send | the commit is at P's tenth step | 11 |
| R's wait has the channel's news, counting P's `wake=True` `m-1` (or a `wake=False` `m-1` beside a waking `m-2`); one step after | the wait depends on every unread message | 11 |
| The wait, then `read_messages` in the same loop | the read's `records` are `[m-1]`, its `depends_on` `[]` | 11, unchanged by the read |
| R has only a `wake=False` `m-1` from P; a board write, a peer's end, the timeout or the stop ends R's wait, or it finds the board's news at once | the wait has no message news and ends with the unread count; no message in `depends_on` | R's chain and whatever else the wait reports |
| R's results show the unread count only, and R never reads | no evidence | R's own chain |
| B in the board example above | the view's `records` are `[w-2]`, its `depends_on` `[w-1, w-2]` | 4 |
| A wait after the waiter's own blind append to a file a ten-step peer wrote; one step after | the wait depends on the peer's write (`peer_record`) | 11 |
| The same wait repeated before any view | the second's `depends_on` is empty (trimmed); the value is unchanged | 11 |
| A wait reporting eleven files, ten by a one-step P and the eleventh by a ten-step Q; one step before and after | `files` names ten, `depends_on` all eleven writes | 11 (2 today) |
| A wait reaching the board, messages and a custom channel whose news has no `depends_on` | the board's and messages' dependencies link; the custom channel's entries are `files` only | as the board and messages give |
| A custom channel whose tool calls `run.bus.read(name, ["s-3"])` for a ten-step writer's `s-3`; one step after | the read's `records` and `depends_on` are `[s-3]` | 11 |
| A custom channel named `shared_memory`, alone, reporting nothing | no dependencies; its waits' `ended` still link | its waits' ends only |
| A custom channel reporting `w-1` that the board committed, or `s-99` that it never committed | `ValueError` from the bus | the sample fails |
| A listing, then a view of one listed file | the listing's `records` are the listed files' last writes and its `depends_on` empty; the view depends on the file's history | the file's history |
| A nested swarm committing its own `w-1` inside an outer member | dropped by member before its `depends_on` is read | the outer chains |

The shapes of [swarm-critical-path.md The shapes](swarm-critical-path.md#the-shapes) keep their lengths, except the eleven-file wait above and the waits that also reached a custom waiting channel, which now link the board's and messages channel's dependencies.

## Alternatives considered

- **The metric attributes `waited.files` by channel**, with each channel's entries tagged, and keeps decoding the board and adds a decoder for messages. It fixes mixed waits but keeps every channel a special case in the metric and gives custom channels nothing (decision: Ransom, 2026-10-30: the channel reports the dependency).
- **One list for provenance and dependency.** `record_ids` would then lose the records a listing, a re-view or a read after a wait concerns, and a read that returned a message would seem to concern none; provenance keeps its own complete list and the dependency gets a field of its own (decision: Ransom, 2026-10-30).
- **A commit names the commits it extended** (`delivered` with the previous version's record), so a view depends only on the shown version's write and the metric follows lineage. Views would write one id instead of the history, but the metric would need a second generic edge, commit to commit, that every custom channel has to understand, and a wait reporting a peer write would inherit that write's whole file history, where today it links the write alone. Per-loop trimming bounds the history lists without either change.
- **Each channel trims its own reports**, as the board could with its per-loop seen state. Every channel would need the state, `news()` may not mark anything (`WaitingState`, `_channel.py:173-177`) and is called repeatedly while a wait sleeps, and a channel that forgot would grow the evidence. The bus sees every report already.
- **No trimming.** Exact and simplest, but a view's evidence grows with the file's history on every view, and views are not rate-limited, so a long shared log, the usage `append` exists for, multiplies its history by every view of it.
- **Each version's history value in the evidence instead of ids** (a view writes the largest commit "depth" it shows). The channel would compute the metric's values, which needs the step counts it does not have.
- **Linking a message at the sender's last step before its read**, rather than at the commit. It would charge the reader for the sender's work after the send, which the message could not contain.
- **Keeping the ten-file cap on links.** The result names every file's writer, so the eleventh writer's news reached the model; leaving it out was a limit of the evidence, not a rule.
- **Linking quiet messages from a wait that has no message news**, through news that reports without making the wait reportable. It needs a second kind of news in the `WaitingState` contract for one count line, which no other result's count gets; a quiet message links when it is read.
- **Messages' wait news depending only on `wake=True` messages.** The line counts every unread message and names every sender, so it reports the `wake=False` ones too.
- **Making `WaitNews.depends_on` required.** No custom channel exists outside the tests, but news that reports no record (a timer, a test channel's note) is legitimate; an empty default keeps `WaitNews(lines, files)` valid and makes no edge.

## Compatibility and migration

- **Evidence.** `read` gains `depends_on`, and the board's `read` gains `records`; `waited` gains `depends_on`. Nothing is removed or renamed, the messages channel's `read.records` keep their meaning and stay complete, and `version` stays 1. A log written before has no `depends_on`, and no `records` on the board's `read`; a reader treats an absent key as "not reported". The metric reads only the live run ([swarm-critical-path.md Where it is computed](swarm-critical-path.md#where-it-is-computed)), so old logs are not read by it.
- **The planned typed event** ([swarm-event-types.md](swarm-event-types.md)). `record_ids` stays provenance, as that design defines it: for a `read`, the evidence's `records`, which for the board are the distinct `record` values of `files` that its mapping already takes, listings and tombstones included, and for a messages read every message returned; a `waited` has none. `depends_on` goes in the event's `data`, beside the board's `files` and `listing` and a wait's other details, for `read` and `waited` (decision: Ransom, 2026-10-30). That design's mapping table takes the read's `record_ids` from `records` and lists `depends_on` under `data`; its field docstrings are unchanged.
- **Public API.** `WaitNews` gains `depends_on` with an empty default, so existing constructions stay valid. `Bus.read()` is new beside `Bus.deliver()`, both reached through `ChannelRun.bus`; `Bus` stays unexported. No name is removed from `inspect_swarm`; `builtin_board()` and `builtin_messages()` are private.
- **The metric's values change** for swarms with the messages channel (reads and waits with message news now link), for waits that reach a custom waiting channel beside the board (the board's dependencies now link), for waits reporting more than ten files, and for custom channels that report dependencies. `SwarmMetrics` is unchanged; its `version` stays 1, since the field's type and presence are the same.
- **Docs.** `docs/analysis.qmd`, The critical path: links come from any channel's results as the channel reports them (the board's views and waits, `read_messages` and the messages a wait's message news counts, a custom channel that reports), and member ends named by a wait; a quiet message makes a link when it is read. Removed: the paragraph that a wait reaching a custom waiting channel links no files, "messages" and "a custom channel and its wait results" from the list of what makes no link (replaced by "a custom channel that reports no dependencies"), and the sentence on waits reporting more than ten files. "When the board's evidence is inconsistent" becomes "when the evidence names a record no recorded commit made". `docs/shared-memory.qmd` and `docs/messages.qmd`, the record sections: `read` lists the records it concerns and what it depends on, `waited` what it depends on, by the rules above. `docs/channels.qmd`, Writing a channel: how a channel reports. Reference: `WaitNews.depends_on` and `ChannelRun.bus` docstrings.
- **Designs.** [swarm-critical-path.md](swarm-critical-path.md): its Goals ("no change to ... the evidence"), the edge table, the board-by-instance and custom-waiting-channel paragraphs, the `PathLog` code, the shapes for mixed and capped waits and its "Not this design" entry on messages edges describe this design's rule instead. The evidence sections of [swarm-memory-channel.md](swarm-memory-channel.md#evidence), [swarm-wait.md](swarm-wait.md#evidence) and [swarm-communication.md](swarm-communication.md#evidence-and-provenance) add `records` and `depends_on`, and [swarm-event-types.md](swarm-event-types.md)'s mapping table as above.

## Security

- **What reaches the new code.** Record ids come from channel state, assigned by the bus (`_bus.py:156-158`); no channel takes one from a model's arguments into `records` or `depends_on`. The board's ids come from its history and files, the messages channel's from its inboxes. A model reaches them only by making writes and reads the channel validated, as today.
- **Custom channel code** is the eval author's and trusted, as the rest of a channel is; the bus still refuses an id the channel did not commit, so a buggy channel fails loudly rather than linking another channel's or another run's records.
- **Agent code** can write an `InfoEvent` with source `inspect_swarm` naming a roster member, as today; a `depends_on` naming no recorded commit yields `None` and a warning, never an exception, and the `PathLog` keeps only ids, positions and member names.
- **Size.** Model behaviour drives how many results there are, and views are not rate-limited, so the evidence must not grow with views times history: per-loop trimming bounds the `depends_on` ids a run writes by its commits times its loops ([The bus](#the-bus)), and `records` is one id per file or message shown, the size of today's `files` and message `records`. Memory: `_committed` one entry per commit, `_depended` one per commit per loop that depended on it, the board's `_history` one id per version, all within the evidence's own size. The metric's work stays linear in the ids it reads.

## Testing

All async tests run on asyncio and trio through the `anyio_backend` fixture. No test needs Docker, a network or a model provider.

- **`tests/test_bus.py`**: `Bus.read()` writes a `read` with the caller's member, loop, `nested`, tool span, `records` and `depends_on` (defaulting to `records`); refuses an id another channel committed, one never committed and one a refused record consumed, in either list (`ValueError`), and a call outside a member (`RuntimeError`); trims `depends_on` ids already written for the loop and keeps them for another loop of the same member, while `records` stays whole; a waiting channel whose `WaitNews.depends_on` names an uncommitted id fails the wait with `ValueError` and still writes `waited`.
- **`tests/test_shared_memory.py`** (or `test_comm_evidence.py`, where the evidence tests live): a view's `records` are the file's last write and its `depends_on` the file's history (create, appends, an edit, a rename of its directory); a partial view the same; a tombstone view the delete in both; a listing's `records` every listed file's last write and its `depends_on` `[]`; a second view of the same version by the same loop has the same `records` and `depends_on` `[]`, by another loop the history again. Waits: `depends_on` names the peer write after the waiter's own blind append, between two members and between two loops of one member; a wait that returned at once; a release whose news vanished (its `woken_by` set, its record not in `depends_on`); a cancelled wait (`[]`); eleven files (ten in `files`, eleven in `depends_on`). The exact `waited` dictionary at `tests/test_comm_evidence.py:211-227` gains `depends_on`.
- **`tests/test_wait.py`**: `test_eleven_files_are_named_ten_and_one_more_by_each_wait` (`:285-294`) also asserts eleven `depends_on` ids for the first wait and none for the repeat.
- **`tests/test_messages.py`**, **`tests/test_messages_channel.py`**: a read's `records` (unchanged assertion at `tests/test_messages.py:159-163`) and equal `depends_on`; wait then read in one loop: the wait's `depends_on` is `[m-1]`, the read's `records` still `[m-1]` (the assertion at `tests/test_messages_channel.py:184-212` unchanged) and its `depends_on` `[]`; a mixed inbox (a `wake=True` and a `wake=False` message): the wait depends on both; a quiet-only inbox: a wait that a board write releases, one that a peer's end releases, one that times out, one the stop releases and one that finds board news at once each end with the unread count and no message id in `depends_on`, and the quiet message still ends no wait.
- **`tests/test_critical_path.py`**: the helpers become `write(member, record)` (a `delivered` with that id), `read(member, *depends_on, records=...)` and `wait(member, *depends_on, ended=...)`, and `_value()` builds `PathLog(ROSTER)`. Rows: every shape above in dependency terms (history, tombstone, peer record, eleven ids, the board example at 4), a record of any channel prefix, `records` naming a commit that `depends_on` leaves out (no edge from it), a result naming a record with no earlier commit (`None` and a warning), a result naming its own member's commit (no change), a nested swarm's events with the same ids under non-roster members (dropped, no warning), errored and cache-replayed model events, no answer and no `member_result` (`None`). The rows that tested the board-by-instance, the messages entries in `files` and custom channel shapes in `files` go: the metric no longer reads them.
- **`tests/test_metrics.py`**, mockllm swarms through `run_swarm()`: a messages swarm where a ten-step sender sends, works on, and a one-step reader reads and answers (11); the same with a wait before the read (11); a quiet message released by a board write of a one-step peer, then read (the read links the sender); the board cases already there keep their values; a registered custom channel whose tool calls `run.bus.read()` and whose news sets `depends_on` (each links); the custom channels named `shared_memory` and `messages` (`:1255-1381`), which report no dependencies, keep their values (2, 11); a new case puts a ten-step peer's board write before a wait that also reaches the custom waiting channel, which now links (11 where it was 1); a nested swarm with the board (its `w-1` ignored by the outer metric).

## Implementation plan

1. **Channels report dependencies.** The metric is untouched and keeps reading `files`, which do not change, so `main` works between the steps.
   - `src/inspect_swarm/_bus/_bus.py`: `_committed`, `_depended`, `read()`, `depends()`.
   - `src/inspect_swarm/_bus/_wait.py`: collect `depends_on` through `depends()`; write it in `waited`.
   - `src/inspect_swarm/_channel/_channel.py`: `WaitNews.depends_on`; `ChannelRun.bus` docstring.
   - `src/inspect_swarm/_evidence.py`: `comm_read(records, depends_on, files=None, ...)`, `comm_waited(depends_on=...)`.
   - `src/inspect_swarm/_channel/shared_memory.py`: `_history`, `peer_record`, view, tombstone and listing `records` and `depends_on`, wait `depends_on`, `_read()` through `Bus.read()`.
   - `src/inspect_swarm/_channel/messages.py`: `_read()` through `Bus.read()`, `news()` `depends_on`.
   - `docs/channels.qmd`, `docs/shared-memory.qmd`, `docs/messages.qmd`.
   - Tests: `tests/test_bus.py`, `tests/test_shared_memory.py` / `tests/test_comm_evidence.py`, `tests/test_wait.py`, `tests/test_messages.py`, `tests/test_messages_channel.py`.
2. **The metric links dependencies.**
   - `src/inspect_swarm/_critical_path.py`: `PathCommit`, `PathResult`, `PathLog(roster)`, the generic pass.
   - `src/inspect_swarm/_ledger.py`: `LedgerInterval(roster=...)`.
   - `src/inspect_swarm/_controller/_control.py`: no board or messages lookup.
   - `src/inspect_swarm/_channel/shared_memory.py`, `src/inspect_swarm/_channel/messages.py`: remove `builtin_board()`, `builtin_messages()`.
   - `src/inspect_swarm/_metrics.py`: the field comment.
   - `docs/analysis.qmd`: The critical path.
   - `design/swarm-critical-path.md`, `design/swarm-memory-channel.md`, `design/swarm-wait.md`, `design/swarm-communication.md`, `design/swarm-event-types.md`.
   - Tests: `tests/test_critical_path.py`, `tests/test_metrics.py`.

## Open questions

None.

## Not this design

- **Ends shown outside a wait.** `list_members` shows which members have ended and how; it writes no evidence, so a member that learns of an end there gets no edge from it, where a wait naming the end does.
- **Status lines as edges.** The board's changed-file line and the unread-message count, on a wait or on other results, tell a loop that commits happened, with ids or counts; by both channels' rules they make no edge, as the changed-file line does today. Revisit only if members are seen acting on them without a read.
- **A public API for channels with records**: exporting `Draft` (and `Bus`, or a protocol of `deliver()` and `read()`) and documenting them, so a custom channel needs no private import.
- **Bridged members' reads**, which reach the bus only once bridged bus tools exist; they will report through the same `Bus.read()`.
- **Line-level attribution of a partial view**, which would need the board replayed line by line.
