# Inspect Swarm: transcript event types for swarm evidence

## Overview

The swarm writes its evidence to the transcript as two inspect_ai event types. A `CommunicationEvent` (`event: "communication"`) records one act of communication between agents: a record sent, a record delivered, a read of shared content, or a wait for others. Its typed fields say who acted, through which channel and with which records (`record_ids`, the one join from a send to its delivery and to the reads of what it changed), and each kind's own details are one typed object: a send's record and refusal, a delivery's recipients and order, a wait's channels and outcome. What only one producer needs, such as the shared memory's file versions, is in a free-form `data` object that the producer owns, so the type fits agent communication in general, not only the swarm's channels. A `SwarmEvent` (`event: "swarm"`) records the rest of the swarm's evidence (its start, each member's result and answers, the final selection, the result record, the ledger, the metrics and the turn seam's boundaries) as a `kind`, the `member` it concerns and versioned `data` whose format inspect_swarm owns, so the swarm's schemas stay out of inspect_ai.

The viewer renders a communication event as one line (who, which channel, what) and a swarm event as a header over collapsed data, keeps both out of the outline as it keeps info events, and gives each its own toggle in the transcript filter, so the swarm's communication can be shown or hidden apart from everything else. inspect_swarm writes no `InfoEvent` for its evidence.

The types are `CommunicationEvent` and its models in inspect_ai's `inspect_ai/event/_communication.py` and `SwarmEvent` in `inspect_ai/event/_swarm.py`, both marked experimental. inspect_swarm writes them in [`_evidence.py`](../src/inspect_swarm/_evidence.py) and [`_record.py`](../src/inspect_swarm/_record.py), and reads them in [`_ledger.py`](../src/inspect_swarm/_ledger.py), [`_critical_path.py`](../src/inspect_swarm/_critical_path.py) and `_record.py`. The user docs are [The record of the board](../docs/shared-memory.qmd#the-record-of-the-board), [The record of messages](../docs/messages.qmd#the-record-of-messages), [Analysis](../docs/analysis.qmd) (the ledger, the metrics and the boundary events), [The result record](../docs/scoring.qmd#the-result-record) and [What the log records](../docs/restrictions.qmd#what-the-log-records).

## The event types

### `CommunicationEvent`

```python
# inspect_ai/event/_communication.py, exported from inspect_ai.event
CommunicationKind = Literal["sent", "delivered", "read", "waited"]


class CommunicationRecord(BaseModel):  # for `sent`: what one agent communicated
    id: str | None = None  # the producer's id for the record
    kind: str  # what it does in its channel, in the channel's terms ("message", "create")
    sender: str
    to: list[str] = []  # the addresses as written; empty for a write to shared state
    recipients: list[str] = []  # the agents the addresses resolved to
    content: str | None = None  # None when the producer cannot see it (encrypted)
    reply_to: str | None = None


class CommunicationRejection(BaseModel):  # for `sent`: why it was not delivered
    control: str  # the control that refused it, in the producer's terms ("rate", "admit")
    reason: str  # the reason the sender was given


class CommunicationSend(BaseModel):  # for `sent`
    record: CommunicationRecord
    rejected: CommunicationRejection | None = None


class CommunicationDelivery(BaseModel):  # for `delivered`
    recipients: list[str] = []
    seq: int | None = None  # its place in the producer's delivery order, across channels


class CommunicationWait(BaseModel):  # for `waited`
    id: str | None = None
    channels: list[str] = []
    outcome: str  # what ended it, in the producer's terms ("news", "timeout")
    duration: float  # seconds


class CommunicationEvent(BaseEvent):
    event: Literal["communication"] = "communication"
    kind: CommunicationKind
    source: str  # the producer, which owns `data` ("inspect_swarm")
    agent: str  # who acted: the sender, the reader or the waiter
    agent_span_id: str | None = None  # that agent's span
    loop_span_id: str | None = None  # the agent span of the loop that acted
    nested: bool = False  # the loop is not the agent's own, such as a subagent's
    channel: str | None = None  # None for a wait, whose channels are in `wait`
    record_ids: list[str] = []
    send: CommunicationSend | None = None
    delivery: CommunicationDelivery | None = None
    wait: CommunicationWait | None = None
    data: dict[str, JsonValue] = {}
```

The fields every kind has (`source`, `agent`, the span ids, `nested`, `channel`, `record_ids`, `data`) are at the top level. What only one kind has is one typed object named with a noun: `send` for `sent`, `delivery` for `delivered`, `wait` for `waited`; a `read` has `record_ids` and producer `data` only. Every docstring of a field or model that one kind uses opens with that kind ("For `sent`, ...").

`record_ids` is the one join across kinds: a send's record id, the same id on its delivery, and on each read of the state it made. For a `read` it names the producer's records for what was read (for the shared memory, each file's last write at the version read). The ids identify state, not text: a listing or a partial view names a record whose content it did not show.

A `model_validator(mode="after")` refuses the shapes no producer writes, so a bug fails at the writer rather than in a reader: `send` is set exactly when `kind` is `sent`, `delivery` exactly when it is `delivered` and `wait` exactly when it is `waited`; every kind but `waited` has a `channel` and `waited` has none; and a `sent` whose record has an id has `record_ids == [send.record.id]`. Log reads run the same validator, so these rules are part of the stored format: a future writer that needs a shape refused here needs a reader that accepts it, even with the same type and kind.

**Why these fields.** The typed fields are those a reader needs without knowing the producer:

- *Who and where*: `agent`, `agent_span_id`, `loop_span_id`, `nested`, `channel`. A member is the swarm's word, so the event says agent; `span_id` is already `BaseEvent`'s (the span the event is in), so the actor's span has its own name.
- *What*: the record of a send, with the fields every message system has (an author, recipients and content, as Codex's `agent_message` and Claude Code's teammate messages have); the ids that join a send to its delivery and to reads; a delivery's order (`delivery.seq`), which `swarm_wait`'s `woken_by` and the critical path use.
- *Outcome*: a send's refusal, by control, which the metrics count; a wait's channels, outcome and duration, which they total.

`kind` is a closed literal because the four kinds are the vocabulary readers render and count: a new kind is an inspect_ai change, and older readers refuse it, as they refuse a new event type. Values that are producer vocabulary (`send.record.kind`, `send.rejected.control`, `wait.outcome`) are strings.

A bridged producer fits the same shape: a Codex `agent_message` would be a `sent` with the author as `sender`, the recipient in `to` and `recipients`, and `content` the text or `None` when it is encrypted, then a `delivered` when it enters the recipient's input; Codex's wait on spawned agents a `waited`; a Claude Code teammate's mailbox read a `read`. Each would have its own `source`. No such producer exists yet ([Not this design](#not-this-design)).

### `SwarmEvent`

```python
# inspect_ai/event/_swarm.py, exported from inspect_ai.event
class SwarmEvent(BaseEvent):
    event: Literal["swarm"] = "swarm"
    kind: str  # what the event records; the swarm defines the kinds
    member: str | None = None  # the member the event concerns, if any
    data: dict[str, JsonValue]  # the kind's format, its version in data["version"]
```

The swarm defines `kind` and the format of each kind's `data`, as sentinel defines its own event's vocabulary. The ledger and the metrics are pydantic models in inspect_swarm (`SwarmLedger`, `SwarmMetrics`) that change with the swarm's analysis; typing them in inspect_ai would keep two copies of each schema in two repositories and make every change an upstream pull request and a viewer type regeneration. `kind` is a plain string, so a new kind needs no inspect_ai change, as `answer` and the turn seam's `boundary` and `changed` needed none ([the kinds the swarm writes](#how-inspect_swarm-writes-them)). The viewer needs only `kind` and `member` for its header. There is no `source`: the type is the swarm's.

**Versions.** The version of a kind's format is `data["version"]`; for the ledger, the metrics and the result record it is the model's own `version` field, which their `data` already holds. A reader that interprets `data` checks the version first and refuses a format it does not know; the viewer, which shows `data` without interpreting it, needs none. The version tracks the shape of `data`, not the wording of the tools' texts. Every kind's format is at version 1.

**The name.** inspect_swarm's alias for what `SwarmControl.events()` yields a controller is `ControlEvent` (`MemberEnded | MemberIdle | MemberNews`), so the one public `SwarmEvent` an analysis imports is the transcript event ([swarm-api.md Controllers](swarm-api.md#controllers)).

## How inspect_swarm writes them

Every write is `transcript()._event(event)`, the private, synchronous call inspect_ai's own producers use, so the ledger, the metrics, `member_result` and `waited`, which are written in synchronous `finally` blocks, are written under cancellation too. Events are constructed without `span_id`, so each lands in the span it is written in. inspect_swarm writes no `InfoEvent` for its evidence.

**Communication.** `_comm()` builds every `CommunicationEvent`, with `source="inspect_swarm"` and `data["version"] = 1`, called by `comm_sent()` and `comm_delivered()` from `Bus.deliver()`, by `comm_read()` from the bus for a channel's read, and by `comm_waited()` from `swarm_wait`'s `finally`. Each is written in the tool span of the call that acted. `agent` and `agent_span_id` are the acting member and its member span, even when one of its descendants acted; `loop_span_id` is the agent span of the loop that acted, and `nested` whether that loop is not the member's own.

| Kind | Typed fields | `data` |
|---|---|---|
| `sent`, every attempt that reached the bus | `channel`; `record_ids=[record.id]`; `send.record` (`id`, `kind`, `sender`, `to`, `recipients`, `content` the payload, `reply_to`); `send.rejected` (`control`, `reason`) when a control refused it | `record`: `routed_out`, `op`, `wake`, `sender_span_id`, `tool_span_id`, `via` |
| `delivered`, each record the bus delivered | `channel`; `record_ids=[record.id]`; `delivery` (`recipients`, `seq`, the run's commit sequence number) | `notify`; `result`, what the channel's `apply()` returned (for the shared memory, the applied `files`) |
| `read`, a result that showed peer content | `channel`; `record_ids`, the records the result concerns | `read` (the read's id), `tool_span_id`, `depends_on`, `files` (the shared memory's file versions shown), `listing`, `via` |
| `waited`, each `swarm_wait` | `channel=None`; `wait` (`id`, `channels`, `outcome`, `duration`) | `path`, `file_id`, `timeout`, `started`, `idle`, `woken_by` (the `delivery.seq` of the commits that released it), `files`, `depends_on`, `ended`, `via` |

`via` is `"bridge"` for a bridged member's call through `swarm_bridged_tools()` and `"tool"` otherwise. What each channel reports in `record_ids` and `depends_on` is that channel's rule ([swarm-critical-path.md What channels report](swarm-critical-path.md#what-channels-report)); the user docs list the fields by channel ([shared memory](../docs/shared-memory.qmd#the-record-of-the-board), [messages](../docs/messages.qmd#the-record-of-messages)).

**The swarm's other evidence.** `swarm_event(kind, *, member, version=1, **data)` writes a `SwarmEvent` with `version` in `data`; the result record, the start event, the ledger, the metrics and the seam's events have writers of their own.

| `kind` | `member` | Written | `data` besides `version` | Defined in |
|---|---|---|---|---|
| `swarm_start` | `None` | when the swarm starts, in its span | none | [swarm-result-record.md](swarm-result-record.md#the-start-event) |
| `member_result` | the member | when it ends, in its span unless it ended before entering it | `span_id`, `status`, `time`, `sandbox` | [swarm-scoring.md](swarm-scoring.md) |
| `answer` | the member | for each answer of a persistent member, in its span | `answer`, the answer's index | [swarm-persistent-members.md](swarm-persistent-members.md#evidence) |
| `boundary`, `changed` | the member | at a turn boundary where callbacks ran, and at a persistent member's hold, in its span | the boundary and each callback's change, as ids and hashes | [swarm-turn-seam.md](swarm-turn-seam.md), [Boundary callbacks](../docs/analysis.qmd#boundary-callbacks) |
| `final` | the chosen member, or `None` | after the stop procedure, in the swarm's span | `span_id`, `mode`, `reason`, `stop_reason`, `answer` | [swarm-scoring.md](swarm-scoring.md) |
| `result_record` | `None` | once, when the swarm ends, in its span | the `SwarmResultRecord` dump and its `id` | [swarm-result-record.md](swarm-result-record.md#the-event), [The result record](../docs/scoring.qmd#the-result-record) |
| `ledger`, `metrics` | `None` | once, when the swarm ends, in its span | the model's dump, exactly the store entry `inspect_swarm:ledger` or `inspect_swarm:metrics` | [Analysis](../docs/analysis.qmd) |

So `SwarmLedger.model_validate(event.data)` reads a ledger event, as it reads the store entry.

## How inspect_swarm reads them

- **The ledger, live.** `EventLog` (`_ledger.py`) subscribes to the transcript and keeps, from each `CommunicationEvent` with `source == "inspect_swarm"`, a `WaitRecord` for a wait (`agent`, `wait.outcome`, `wait.duration`, `data["idle"]`) or a `CommRecord` for the other kinds (`kind`, `agent`, `channel`, the record id for `sent` and `delivered`, the refusing `send.rejected.control`, the UTF-8 bytes of an accepted send's `content`, and `data["listing"]`). A refused payload is not measured, since it can hold text no UTF-8 encoder takes. The metrics count these records. Matching on `source` keeps other producers' communication out of the swarm's metrics.
- **The critical path.** `PathLog` (`_critical_path.py`) takes `delivered` events as commits (`agent`, `record_ids`), `read` and `waited` events as results (`data["depends_on"]`, and a wait's `data["ended"]`), and `member_result` and `answer` `SwarmEvent`s as ends ([swarm-critical-path.md](swarm-critical-path.md)).
- **The result record.** `swarm_result()` matches the `SwarmEvent` of kind `result_record`, and also the `InfoEvent` with source `inspect_swarm` and `type: "result_record"` that inspect_swarm wrote before the swarm's evidence had its own types, so an older log still gives its record. Its check for a swarm's events in a sample whose store lost the record's id accepts any `SwarmEvent` or such an `InfoEvent` ([swarm-result-record.md](swarm-result-record.md#samples-whose-store-lost-the-id)).
- **Analysis after a run** reads the store entries, not the events: `swarm_ledger()` and the samples-dataframe columns.
- **Member timelines.** `MemberTimelines` builds each member's timeline from the events in its spans, the evidence included; inspect_ai's timeline builder treats both types as ordinary events.

## In inspect_ai besides the types

- **The union.** Both join `Event`, and `inspect_ai.event` exports them with `CommunicationKind` and the five communication models. They are listed in the `inspect_ai.event` reference.
- **Condensing.** `walk_communication_event` passes `send.record.content` and `send.rejected.reason` through the content function and walks `data`; `walk_swarm_event` walks `data`. Large strings become attachments, repeated ones share one, and resolving uses the same walkers.
- **`inspect ctl`.** `HIGH_SIGNAL_EVENT_TYPES` includes `communication` and `swarm`. The compact projection's metadata tier holds names, ids, numbers and producer vocabulary: for `communication`, `source`, `kind`, `agent`, `nested`, `channel`, `record_ids`, the delivery's `recipients` and `seq`, the refusing control as `rejected`, and the wait's `channels`, `outcome` and `duration`; for `swarm`, `kind` and `member`. The content tier adds, truncated, what can hold model text: `content`, `reason` and `data` for `communication`, `data` for `swarm`. Lone surrogates in every projected string of either type are escaped, since the JSON response cannot encode them. The terminal summary prints `agent → channel: kind ids` (`agent waited 4.2s on channels: outcome` for a wait) or `kind · member`, then the projected content, each field through `_sanitize_control`.
- **Textual display.** One line per event with identifiers only (agent, channel, kind, record ids, recipients, refusing control; a wait's duration, channels and outcome; a swarm event's kind and member). Record content, refusal reasons and `data` are left to the viewer and `inspect ctl --content`. Each string is sanitized (surrogates escaped, terminal controls removed, newlines flattened) and appended as plain Rich `Text`, never markup; the swarm event's kind in the rule title is escaped.

## In the viewer

- **Types and normalization.** ts-mono's generated types include both events and their models. The log normalizer fills `nested`, `record_ids`, `data` and the communication models' lists when a log omits them.
- **Filter.** The transcript filter lists `Communication` and `Swarm`, neither hidden by default, so the swarm's communication can be hidden alone. The filter applies before stored timelines are converted and orphaned events attached, so hiding a type removes it from every member's timeline and from the Overall timeline alike.
- **`CommunicationEventView`**, one `EventRow` per event: the agent's name, with ` (subagent)` when `nested`, then one line:
  - `sent`: `→ shared_memory: create (w-1) to b, c` and the first line of the content; a refused send adds `refused (rate)` styled as a refusal and the first line of the reason, with the refusal icon.
  - `delivered`: `→ shared_memory: delivered w-1 (#1) to b, c`, the number being `delivery.seq`.
  - `read`: `← shared_memory: read w-4`, with `(lines 3-8)` for a partial view or `(deleted)` for a tombstone, `listed 3 files` for a listing, or `read (nothing)`. These read the shared memory's `data` keys `listing` and `files` (`lines`, `deleted`); a producer without them gets `read` and its record ids.
  - `waited`: `waited 4.2s on shared_memory: news`.

  Clicking the line expands the event's own fields as JSON. Previews are truncated.
- **`SwarmEventView`**, an `EventPanel` with `Swarm:`, the kind and the member in its header and the event's fields as collapsed JSON. It interprets no kind, so inspect_swarm's kinds and formats change without a viewer change.
- **Outline.** Both types are removed from the outline, as info events are, and pass through a turn where a path keeps them.
- **Search and titles.** Search indexes each event's fields as one JSON block, as it indexed `InfoEvent.data`; the titles are `Communication: <kind>` and `Swarm: <kind>`.

## Decisions

- 2026-10-27: the swarm's evidence gets two inspect_ai event types, added together, rather than keeping the non-communication evidence as `InfoEvent`s; satellite packages already have event types in inspect_ai (sentinel) (decision: Ransom, 2026-10-27; [swarm.md open question 1](swarm.md#open-questions)).
- 2026-10-27: the communication type is shaped for agent communication in general, so that bridged agents' traffic can be mapped to it later, before any such mapping exists (decision: Ransom, 2026-10-27).
- 2026-10-27: the transcript event takes the name `SwarmEvent`, matching `SentinelEvent`, and inspect_swarm's experimental controller alias of that name became `ControlEvent` (decision: Ransom, 2026-10-27).
- 2026-10-27: a transcript filter that lists only the types a sample has is a separate viewer change, not part of this one (decision: Ransom, 2026-10-27).
- 2026-10-28: what only one communication kind has is one typed object per kind (`send`, `delivery`, `wait`), and `record_ids` is the one join across kinds (decision: Ransom, 2026-10-28).
- 2026-10-28: a `SwarmEvent`'s format version is `data["version"]`, not a typed field, as no inspect_ai event versions itself and the ledger's and metrics' models already carry theirs (decision: Ransom, 2026-10-28).
- 2026-10-23: the evidence `version` tracks the shape of the payload, not the wording of the tools' texts (decision: Ransom, 2026-10-23).

## Alternatives considered

- **Keep the non-communication evidence as `InfoEvent`s.** Leaves the Info toggle all-or-nothing for everything but communication; rejected (Decisions, 2026-10-27).
- **One swarm event for everything, communication included.** Simpler in inspect_ai, but communication is the evidence other producers share and the one the viewer renders per kind; a swarm-owned `data` has no typed fields to render or count.
- **Kind-specific fields at the top level** (`record`, `rejected`, `recipients`, `seq` beside `wait`). One level less to read, but a reader cannot tell which fields a kind has, and the validator needs a rule per field (Decisions, 2026-10-28).
- **Typing every communication field.** The shared memory's files, operations and wait filters would become inspect_ai schema no other producer writes, and each channel change an upstream change.
- **`kind: str` on `CommunicationEvent`.** New kinds without an inspect_ai change, but readers could not rely on the vocabulary, and the viewer would need a fallback line anyway.
- **A typed `version` on `SwarmEvent`.** Duplicates the models' own `version`, which the event would have to keep equal (Decisions, 2026-10-28).
- **Other names.** `AgentMessageEvent` collides with the Codex item and is too narrow (a write, a read and a wait are not messages); `CommEvent` abbreviates, which no inspect_ai event does. `SwarmEvidenceEvent` would have spared the controller alias, at the cost of a longer permanent name than its sibling `SentinelEvent` (Decisions, 2026-10-27).
- **A public `Transcript.event()` or typed helpers.** A new public inspect_ai API for one caller, which helpers would have to grow with the fields.
- **A producer-written summary line on `SwarmEvent`.** Puts presentation and harness-composed text in the log; `kind` and `member` suffice for a header.

## Compatibility and migration

- **New logs and older readers.** An inspect_ai that predates the types cannot read a log that has them: the discriminated union rejects the unknown `event` value, and with it the log, so a viewer served by an older Python reader fails too. An older browser viewer reading the log file directly opens it, renders nothing for the two types and shows them in the outline as rows that split turns, so the log looks as if it had no swarm evidence. inspect_ai accepts this cost for each new event type. It applies to every swarm log, a swarm of one included.
- **Older logs.** Logs from before the swarm had its own types hold the evidence as `InfoEvent`s with source `inspect_swarm` and still open in every viewer, without the compact lines or the new toggles. inspect_swarm reads back from such events only the result record, through `swarm_result()`; `swarm_ledger()` and the analysis columns read store entries, whose format is the same. Nothing else reads the older form, as the format is experimental ([swarm-implementation-plan.md Decisions](swarm-implementation-plan.md#decisions), item 6).
- **Public API.** inspect_ai's two events and six models (with `CommunicationKind`) are experimental. inspect_swarm's controller alias is `ControlEvent`; controllers that test `MemberEnded`, `MemberIdle` or `MemberNews` are unaffected.
- **Scout.** inspect_scout's event filters list types by literal, so a scanner cannot ask for the two types by name until Scout adds them; scanners that take all events see them.

## Security

- **Model text in the events.** A record's `content`, a refusal's `reason` and `data` (file paths, operations, a read's files) carry model output, possibly adversarial. They are JSON data in the log. The viewer renders every event string as plain text through its content-trust component (hidden characters revealed and bidi runs isolated when the content is untrusted), never as Markdown or HTML, and truncates previews. `inspect ctl` sanitizes terminal controls and escapes surrogates; the textual display shows no record content, reason or `data`, and appends identifiers as plain text, never markup.
- **Names.** `agent`, `member`, `sender`, `recipients` and `channel` come from the eval author's roster and channel names, or are bound by the bus, never from model output ([swarm-communication.md Security](swarm-communication.md#security)).
- **Unencodable text.** A refused payload can hold lone surrogates; it is recorded as given, the projection and the textual display escape it, and the ledger measures only accepted payloads.
- **Validation.** The validator refuses shapes, not values, and runs on read as well as on write: for an event of a valid shape, nothing in a field's value can make a log unreadable.
- **The result record** is checked before it is written so that no string in it reads back from the log as other text ([swarm-result-record.md](swarm-result-record.md)).

## Not this design

- **Mapping bridged traffic** to `CommunicationEvent`: Codex `agent_message` items and wait calls in inspect_swe's Codex event consumer, and Claude Code teammate messages once inspect_swe represents teams.
- **Scout support**: the two types in inspect_scout's event filter literals, so scanners can select swarm communication by type.
- **Dataframe columns** for communication events in inspect_ai's `events_df()`.
- **A swarm view in the viewer**, such as a message timeline or member graph over `CommunicationEvent`s, and the Activity view placing communication on its per-conversation rows.
- **A transcript filter that lists only the types the sample has**, a separate viewer change that does not depend on these types (Decisions, 2026-10-27). It would derive the toggles from the sample's unfiltered events or, for a chunked sample, from the per-type counts it loads on opening, keeping the stored hidden list apart from which toggles are shown.
- **A public API for writing events** other than `InfoEvent` (`Transcript.event()`), if another library needs one.
- **Tool rows in a member's outline**, an outline quirk the evidence makes visible but does not cause: Inspect records a call's `ToolEvent` inside its tool span with the caller's `span_id`, so the viewer keeps the tool span as its own row before its call when the span holds events the transcript shows, which ends the turn.
