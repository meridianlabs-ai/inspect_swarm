# Inspect Swarm: transcript event types for swarm evidence

## Overview

inspect_ai gains two event types, and inspect_swarm writes its evidence with them instead of `InfoEvent`s. A `CommunicationEvent` (`event: "communication"`) records one act of communication between agents: a record sent, a record delivered, a read of shared content, or a wait for others. Its typed fields say who acted, on which channel, with which records, and for a send the record itself (sender, addresses, recipients, content). Producer-specific details, such as the shared memory's file versions, ride in a free-form `data` object, so the type fits any agent communication: inspect_swarm's channels now, and bridged agents' traffic (Codex `agent_message`, Claude Code agent-team messages) later. A `SwarmEvent` (`event: "swarm"`) records the rest of the swarm's evidence: each member's result, the final selection, the ledger and the metrics. It has a `kind`, the `member` it concerns, and versioned `data` whose format inspect_swarm owns, so the ledger and metrics schemas stay out of inspect_ai.

The viewer (ts-mono) renders a communication event as one line (who, which channel, what) and a swarm event as a one-line header over collapsed data, keeps both out of the left-nav outline as it keeps info events, and lists in the transcript filter only the event types the sample contains. The work is one inspect_ai PR, one ts-mono PR, and an inspect_swarm PR that switches the writers and readers.

## Why

In the viewer the swarm's evidence crowds the transcript, and it cannot be shown or hidden apart from other info events. The real logs in `~/git/inspect_swarm/logs/viewer-filtering/` are two runs of `examples/shared_memory.py` with claude-haiku-4-5: one sample of 4 members, and 3 epochs of 6 members. Counted with `read_eval_log()`, a 6-member sample has 54 to 65 swarm `InfoEvent`s against 27 to 40 `ToolEvent`s, and about 90% of them are communication (epoch 3: `read` 20, `notified` 16, `sent` 7, `delivered` 7, `waited` 6, with `member_result` ×6, `final`, the ledger and the metrics making up the rest). The `notified` kind is gone from the current code (below), which leaves the same picture.

- **The Info filter is all-or-nothing.** Every swarm evidence event is `event: "info"`, so hiding communication hides the final selection, the ledger and every other library's info events with it.
- **Each event is a JSON panel.** `InfoEventView` renders a card titled `Info: inspect_swarm` with the payload as collapsed JSON (`packages/inspect-components/src/transcript/InfoEventView.tsx:18`). A shared-memory write is three such cards, and none says who did what without expanding it.
- **The data has no schema.** Readers (the ledger, the tests, users following the docs) match `source == "inspect_swarm"` and dictionary keys, with casts.

Both types are added together, with the communication type shaped for bridged traffic before that traffic is mapped (decision: Ransom, 2026-10-27; [swarm.md open question 1](swarm.md#open-questions)). Keeping the non-communication evidence as `InfoEvent`s was considered and rejected: satellite packages already have event types in inspect_ai (sentinel), and the viewer can hide filter toggles for types a sample does not contain.

## Goals and non-goals

Goals:

- A `CommunicationEvent` in inspect_ai shaped for agent communication in general, with typed fields chosen from what inspect_swarm writes today and what its ledger and metrics read.
- A `SwarmEvent` in inspect_ai for the swarm's other evidence, with `kind` and versioned `data` owned by inspect_swarm.
- Viewer support: compact renderers, outline treatment like info events, and a filter toggle per type shown only when the sample has that type.
- inspect_swarm writes only these two types for its evidence and reads them in the ledger, the tests and the docs.

Non-goals:

- Mapping bridged traffic (Codex `agent_message`, Claude Code teams) to `CommunicationEvent`. The type leaves room for it; the mapping is the bridged-swarms work in inspect_swe ([swarm.md Bridged swarms](swarm.md#bridged-swarms)).
- A compatibility path for logs inspect_swarm wrote before the switch: its format is experimental ([Compatibility](#compatibility-and-migration) says what those logs lose).
- A swarm view in the viewer (member graph, message timeline), dataframe columns for the new types, or Scout scanner support ([Not this design](#not-this-design)).
- Changing what the swarm records. Every field it writes today is still written, in a typed field or in `data`.

## Current behaviour

Code references are to inspect_swarm `main` at `aa9ec88`, inspect_ai `main` at `f0d0971d8` and ts-mono `main` at `db44e810`. Paths are relative to each repository's root; `IC/` is ts-mono's `packages/inspect-components/src/transcript/`.

### What inspect_swarm writes

All evidence goes through `transcript().info(data, source="inspect_swarm")` in `src/inspect_swarm/_evidence.py`:

- **`evidence(type, kind, *, member, span_id, **fields)`** (`_evidence.py:25`) writes `{"version": 1, "type", "kind", "member", "span_id", **fields}`. `span_id` here is the acting member's span, not the span the event is in.
- **Communication, `type: "comm"`.** The common fields add `loop` (the agent span of the loop that acted) and `nested` (whether that loop is not the member's own, such as a subagent's).
  - `comm_sent` (`:54`), from `Bus.deliver()` (`src/inspect_swarm/_bus/_bus.py:176-183`): every attempt that reached the bus. `channel`; `record`, the whole `Record` (`id`, `channel`, `kind`, `sender`, `to`, `recipients`, `routed_out`, `payload`, `op`, `wake`, `reply_to`, `sender_span_id`, `tool_span_id`; `_record`, `:170`); `rejected`, `{"control", "reason"}` or `null`.
  - `comm_delivered` (`:77`), from `Bus.deliver()` (`_bus.py:189`): `channel`, `record` (the id), `recipients`, `seq` (the run's commit sequence number), `notify` and `result` (what the channel's `apply()` returned; for the shared memory, the applied `files`).
  - `comm_read` (`:102`), from the shared memory's views (`src/inspect_swarm/_channel/shared_memory.py:922`): `channel`, `read` (the read id), `tool_span_id`, `files` (each file shown, its version and the record that last wrote it) and `listing`.
  - `comm_waited` (`:130`), from `swarm_wait` (`src/inspect_swarm/_bus/_wait.py:256`): `wait`, `channels`, `path`, `file_id`, `timeout`, `started`, `duration`, `idle`, `outcome`, `woken_by`, `files` and `ended`.
- **Results, `type: "result"`.** `member_result` (`src/inspect_swarm/_controller/_control.py:599`), in the member's span: `status`, `time`, `sandbox`. `final` (`src/inspect_swarm/_final.py:113`), in the swarm's span: `member` (the chosen one, or `null`), `mode`, `reason`, `stop_reason`.
- **The ledger and the metrics.** `write_ledger` and `write_metrics` (`_evidence.py:188`, `:195`) write `{"type": "ledger", **SwarmLedger.model_dump()}` and `{"type": "metrics", **SwarmMetrics.model_dump()}` with no `kind`, and set the same dictionaries in the store as `inspect_swarm:ledger` and `inspect_swarm:metrics`. Each model carries its own `version` field.

These are the only kinds the code writes. The logs above also have `notified` (11 to 16 per sample), which the code no longer writes, since members receive no notices; nothing in this design handles it. The `stopped` kind that [swarm-implementation-plan.md M1-5](swarm-implementation-plan.md#m1-pull-requests) mentions is not written either: `final` takes its place. Other designs describe evidence not built yet: `verdict` ([swarm-scoring.md](swarm-scoring.md#what-the-record-adds)), the `schedule` and `orbit` types ([swarm-orbit.md](swarm-orbit.md)), the users-isolation census ([swarm-sandbox-isolation.md](swarm-sandbox-isolation.md#what-the-topology-makes-measurable)), and push delivery's `steered` and nested notices ([swarm-communication.md](swarm-communication.md#push-delivery-after-m2-optional)).

`transcript().info()` is synchronous, and the swarm relies on that: the ledger, the metrics and `waited` are written in synchronous `finally` blocks that also run under cancellation.

### Who reads it

- **The ledger, live.** `EventLog.__call__` (`src/inspect_swarm/_ledger.py:283`) subscribes to the transcript and keeps, from each `InfoEvent` with source `inspect_swarm` and `type: "comm"` (`:318-326`), a `CommRecord` (`kind`, `member`, `channel`, the record id for `sent` and `delivered`, the rejecting control, the accepted payload's UTF-8 bytes, `listing`; `:247`, `_comm_record` `:339`) or, for `waited`, a `WaitRecord` (`member`, `outcome`, `duration`, `idle`; `:260`, `_wait_record` `:330`). The metrics count these (`src/inspect_swarm/_metrics.py:231`, `:271`). Nothing reads the result, ledger or metrics events back.
- **Analysis after a run** reads the store, not the events: `swarm_ledger()` validates `sample.store["inspect_swarm:ledger"]` (`src/inspect_swarm/analysis/_columns.py:14`).
- **Tests** in twelve files match `isinstance(e, InfoEvent) and e.source == "inspect_swarm"` and a `type` or `kind` key: `tests/swarm_runtime.py:221` (the shared `evidence()` helper), `test_background_members.py`, `test_binding.py`, `test_budget.py`, `test_bus.py`, `test_comm_evidence.py`, `test_controller.py`, `test_final.py`, `test_metrics.py`, `test_shared_memory_channel.py`, `test_swarm_limits.py`, `test_wait.py`.
- **Docs.** `docs/analysis.qmd:8` (the ledger and metrics "also logged as an `InfoEvent`"), `docs/shared-memory.qmd:141-148` (the record of the board) and `:117`, `:189` (the loop and `nested`, and the sentinel join). Docstrings and comments: `_ledger.py:122`, `_metrics.py:69`.
- **Outside inspect_swarm.** Nothing in ts-mono, inspect_scout or inspect_ai references `inspect_swarm`. ORBIT's viewer reads `InfoEvent`s with source `orbit.communication`; the ORBIT port design has it accept inspect_swarm's evidence too ([swarm-orbit.md](swarm-orbit.md#compatibility-and-migration)).
- **Named timelines.** `MemberTimelines` (`_evidence.py:208`) builds one timeline per member with `timeline_build()` from the events in the member's spans and adds it to the sample. Evidence events are ordinary `TimelineEvent`s there: inspect_ai's timeline builder special-cases only model, tool and branch events (`src/inspect_ai/event/_timeline.py:374`).

### inspect_ai events

- **The union is closed and discriminated.** `Event` (`src/inspect_ai/event/_event.py:31-57`) has 25 members, each with a unique `event` literal, and logs are validated through `DiscriminatedEvent` (`:68`), so an inspect_ai that does not know an `event` value cannot read the log. `tests/log/test_event_discriminator.py:27` asserts the member count. Each event has its own module under `src/inspect_ai/event/`.
- **`BaseEvent`** (`src/inspect_ai/event/_base.py:16-52`) gives `uuid`, `span_id`, `timestamp`, `working_start`, `metadata` and `pending`. `span_id` is set from `current_span_id()` when the event is constructed, unless the caller passes one. Extra fields are ignored on reading (pydantic's default), so adding an optional field to an existing event does not break older readers.
- **Writing.** `Transcript.info()` (`src/inspect_ai/log/_transcript.py:619-626`) is `self._event(InfoEvent(...))`. There is no public API for other types: every producer calls the private, synchronous `transcript()._event(event)` (`:696`), sentinel at `src/inspect_ai/_sentinel/_dispatch.py:790` and `:883`, checkpoints at `src/inspect_ai/util/_checkpoint/checkpointer_impl.py:576`, reviews at `src/inspect_ai/review/_call.py:33`.
- **A satellite's event** is `SentinelEvent` (`src/inspect_ai/event/_sentinel.py:37-167`): marked experimental in its docstring, with `kind` and its own `Literal` aliases, because the event is always in the union and cannot import the satellite. `ReviewEvent` (`_review.py:9-28`) is the smallest recent event.
- **What else a new type touches.** The OpenAPI schema and ts-mono's generated types are regenerated with `python src/inspect_ai/_view/schema.py`, which picks up union members by itself (`src/inspect_ai/_view/schema.py:41-42`). Attachment condensing dispatches by type and passes unknown types through untouched (`src/inspect_ai/log/_condense.py:991-1009`), so an event's large strings are condensed only if it gets a branch; `InfoEvent.data` is condensed by `walk_info_event` (`:1099`). `inspect ctl`'s default event stream is `HIGH_SIGNAL_EVENT_TYPES` (`src/inspect_ai/_control/events.py:85-101`), which includes `info`. The textual display has a renderer list (`src/inspect_ai/_display/textual/widgets/transcript.py:517-534`). `ReviewEvent` is wired into exactly these, plus `event/__init__.py` and the docs reference.
- **No inter-agent event exists.** Codex Multi-Agent V2 `agent_message` items (`{"type", "author", "recipient", "role", "content"}`, `src/inspect_ai/model/_agent_message.py:10`) become author-attributed user messages (`src/inspect_ai/agent/_bridge/responses_impl.py:1256-1280`) and appear only inside `ModelEvent` inputs.

### The viewer

- **Filter.** The transcript filter lists every type in a static map, `eventTypes: Record<AllEventTypes, string>` (`apps/inspect/src/app/samples/transcript/hooks.ts:11`), which the generated `Event` type makes complete at compile time. `arrangedEventTypes` (`:91`) sorts them with the default-hidden types (`kDefaultExcludeEvents`, `IC/types.ts:129`: `sample_init`, `sandbox`, `state`, `store`, `branch`, `anchor`, `checkpoint`) last, and `TranscriptFilterPopover` renders a checkbox for each (`apps/inspect/src/app/samples/transcript/TranscriptFilter.tsx:72`). Nothing looks at which types the sample has. `SampleDisplay` renders the popover (`apps/inspect/src/app/samples/SampleDisplay.tsx:979`) and holds the sample's events (`sampleEvents`, `:230`); a chunked sample's events array is an empty shell.
- **Where the filter applies.** `useTimelinePipeline` (`IC/hooks/useTimelinePipeline.ts`) drops hidden types from the raw events (`:100`, anchors kept), builds the timelines from what is left, including the sample's stored timelines, whose nodes reference events by uuid (`convertServerTimeline`, `IC/timeline/core.ts:368-424`), then filters the selected lane's events again before building the event tree (`:176-182`). So a hidden type disappears from every member timeline as from the sample's own. When the stored timelines leave events unreferenced and there is more than one timeline, an "Overall" timeline of all events is added (`attachOrphanedEvents`, `IC/timeline/hooks/useTimelinesArray.ts:224`); a swarm sample always has such events (the swarm's span, the scorers), so it always gets one.
- **Renderers.** `RenderedEventNodeInner` (`IC/TranscriptVirtualList.tsx:57-326`) switches on `event.event`, `info` at `:84`, with `default: return null` (`:323`). `ReviewEventView` (`IC/ReviewEventView.tsx`) is a recent one-row renderer built on `EventRow`.
- **Outline.** `outlineFilterVisitors` (`IC/outline/tree-visitors.ts:47`) removes `logger`, `info`, `state`, `store`, `approval`, `review`, `sentinel`, `input` and `sandbox` nodes before `makeTurns` (`:93`) groups each model event with the tool events after it. `kTurnPassthroughEvents` (`:130`, `logger` and `info`) lets those types sit inside a turn, which matters only for types the filter visitors do not remove first. Any other node ends the turn, and tool events with no open model event are dropped (`makeTurn` clears them).

## Design

### At a glance

| | inspect_ai | ts-mono | inspect_swarm |
|---|---|---|---|
| `CommunicationEvent` | the event, its record, rejection and wait models; union, schema, condensing | one-line renderer; outline and filter | `sent`, `delivered`, `read`, `waited`; the ledger reads it |
| `SwarmEvent` | the event; union, schema, condensing | header over collapsed data; outline and filter | `member_result`, `final`, `ledger`, `metrics` |
| Filter | — | toggles only for the types the sample has | — |

### `CommunicationEvent`

New module `src/inspect_ai/event/_communication.py`, exported from `inspect_ai.event`:

```python
CommunicationKind = Literal["sent", "delivered", "read", "waited"]


class CommunicationRecord(BaseModel):
    """What one agent communicated: a message, or a change to state other agents read."""

    id: str | None = Field(default=None)
    """The producer's id for the record, if it has one."""

    kind: str
    """What the record does in its channel, in the channel's terms (`"message"`, `"create"`, `"append"`)."""

    sender: str
    """The name of the agent that sent it."""

    to: list[str] = Field(default_factory=list)
    """The addresses as written. Empty for an unaddressed record, such as a write to shared state."""

    recipients: list[str] = Field(default_factory=list)
    """The agents the addresses resolved to."""

    content: str | None = Field(default=None)
    """The text it carries, or `None` when the producer cannot see it (such as encrypted content)."""

    reply_to: str | None = Field(default=None)
    """The id of a record this one answers."""


class CommunicationRejection(BaseModel):
    """Why a record that was sent was not delivered."""

    control: str
    """The control that refused it, in the producer's terms (`"rate"`, `"admit"`)."""

    reason: str
    """The reason the sender was given."""


class CommunicationWait(BaseModel):
    """An agent waiting for other agents."""

    id: str | None = Field(default=None)
    """The producer's id for the wait."""

    channels: list[str] = Field(default_factory=list)
    """The channels it waited on."""

    outcome: str
    """What ended it, in the producer's terms (`"news"`, `"timeout"`, `"cancelled"`)."""

    duration: float
    """Seconds from the start of the wait until it ended."""


class CommunicationEvent(BaseEvent):
    """Communication between agents: a record sent or delivered, a read of what others communicated, or a wait for others.

    Experimental: not yet a stable API.
    """

    event: Literal["communication"] = Field(default="communication")

    kind: CommunicationKind
    """`sent`: an agent sent a record, which was delivered or refused (`rejected`). `delivered`: a record reached its channel and recipients. `read`: an agent was shown what others communicated. `waited`: an agent waited for others."""

    source: str
    """What produced the event (`"inspect_swarm"`). It names the owner of `data`."""

    agent: str
    """The name of the agent that acted: the sender (`sent`, `delivered`), the reader (`read`) or the waiter (`waited`)."""

    agent_span_id: str | None = Field(default=None)
    """The span of the agent that acted."""

    loop_span_id: str | None = Field(default=None)
    """The agent span of the loop that acted: the agent's own loop, or a subagent acting for it."""

    nested: bool = Field(default=False)
    """The loop that acted is not the agent's own, such as one of its subagents."""

    channel: str | None = Field(default=None)
    """The channel the communication went through. `None` for a wait, whose channels are in `wait`."""

    records: list[str] = Field(default_factory=list)
    """The ids of the records the event concerns: the record sent or delivered, or the records whose content a read showed."""

    record: CommunicationRecord | None = Field(default=None)
    """The record, for `sent`."""

    rejected: CommunicationRejection | None = Field(default=None)
    """For `sent`, why the record was refused; `None` when it was delivered."""

    recipients: list[str] = Field(default_factory=list)
    """For `delivered`, the agents it reached."""

    seq: int | None = Field(default=None)
    """For `delivered`, the delivery's position in the order the producer delivered records, across its channels."""

    wait: CommunicationWait | None = Field(default=None)
    """The wait, for `waited`."""

    data: dict[str, JsonValue] = Field(default_factory=dict)
    """Further details in the format `source` defines, such as a channel's operation or the versions of files read."""
```

A `model_validator(mode="after")` refuses the shapes no producer writes, so a bug fails at the writer rather than in a reader: `sent` without `record`, or with `records` other than `[record.id]` when the record has an id; `rejected` on any kind but `sent`; `recipients` or `seq` on any kind but `delivered`; `waited` without `wait`, or with a `channel`; `wait` on any other kind; and `sent`, `delivered` or `read` without a `channel`.

**Why these fields.** The typed fields are those a reader needs without knowing the producer, taken from what the swarm writes and what its ledger, metrics and the viewer read:

- *Who and where*: `agent`, `agent_span_id`, `loop_span_id`, `nested`, `channel`. The swarm's `member`, `span_id`, `loop` and `nested`, renamed for agents in general: a member is the swarm's word, and `span_id` is already `BaseEvent`'s, the span the event is in.
- *What*: the record of a send, with the fields every message system has (Codex's `agent_message` has an author, a recipient and content; Claude Code's teammate messages a sender, a recipient and text); the ids that join a send to its delivery and a delivery to the reads that show it (`records`); a delivery's order (`seq`), which the swarm's critical-path analysis and `swarm_wait`'s `woken_by` use.
- *Outcome*: a send's rejection, by control, which the metrics count; a wait's channels, outcome and duration, which they total.

Everything the swarm writes beyond these is shared-memory or bus mechanics and stays in `data`, owned by inspect_swarm: the operation and its files, `routed_out`, `wake`, `notify`, a read's files and listing flag, a wait's filters, timeout, idle time, wake-ups and ends. `kind` is a closed literal because the four kinds are the vocabulary readers render and count; a new kind is an inspect_ai change, and older readers would refuse it, like a new event type. Values that are producer vocabulary (`record.kind`, `rejected.control`, `wait.outcome`) are strings.

**What a bridged producer would write**, for the later mapping, to check the shape (not designed here): a Codex `agent_message` from `explorer` to the parent is a `sent` with `record.sender="explorer"`, `to` and `recipients` the recipient, and `content` the text or `None` when it is encrypted, then a `delivered` when it enters the recipient's input; Codex's `wait` on spawned agents is a `waited`; a Claude Code teammate's mailbox read is a `read`. Each would have its own `source`.

### `SwarmEvent`

New module `src/inspect_ai/event/_swarm.py`, exported from `inspect_ai.event`:

```python
class SwarmEvent(BaseEvent):
    """Evidence from an agent swarm (inspect_swarm) other than communication: a member's result, the final answer's selection, the swarm's ledger or its metrics.

    The swarm defines `kind` and the format of `data`. Experimental: not yet a stable API.
    """

    event: Literal["swarm"] = Field(default="swarm")

    kind: str
    """What the event records (`"member_result"`, `"final"`, `"ledger"`, `"metrics"`)."""

    member: str | None = Field(default=None)
    """The member the event concerns, if any."""

    version: int
    """The version of `data`'s format for this `kind`."""

    data: dict[str, JsonValue]
    """The evidence, in the format the swarm defines for `kind` and `version`."""
```

The starting point is confirmed: `kind` plus versioned `data` owned by inspect_swarm. The ledger and metrics are pydantic models in inspect_swarm (`SwarmLedger`, `SwarmMetrics`) that change with the swarm's analysis; typing them in inspect_ai would put two copies of each schema in two repositories and make every change an upstream PR and a ts-mono type regeneration. The viewer needs only `kind` and `member` to render a useful line. `kind` is a plain string, so the evidence other designs plan (`verdict`, the ORBIT port's `activation_started`, `activation_ended`, `intervened` and `model_exposure`, the users-isolation census) are new kinds with no inspect_ai change. `version` is typed so a reader can refuse a format it does not know before it reads `data`. There is no `source`: the type is the swarm's, as `SentinelEvent` is sentinel's.

### How inspect_swarm writes them

Both are written with `transcript()._event(event)`, the private, synchronous call inspect_ai's own producers use, so the writes in synchronous `finally` blocks still run under cancellation. inspect_swarm already uses private transcript names (`transcript()._subscribe`, `_ledger.py:369`). The events are constructed without `span_id`, so each lands in the span it is written in, as the `InfoEvent`s do today: communication in the tool span of the call that acted, `member_result` in the member's span (or the swarm's, for a member that never entered one), the rest in the swarm's span.

`src/inspect_swarm/_evidence.py` keeps its functions and signatures, so no caller changes, and maps today's payloads field by field:

| Today (`InfoEvent.data`) | `CommunicationEvent` |
|---|---|
| `type: "comm"`, `kind` | `kind` |
| — | `source="inspect_swarm"` |
| `member`, `span_id`, `loop`, `nested` | `agent`, `agent_span_id`, `loop_span_id`, `nested` |
| `channel` (sent, delivered, read) | `channel` |
| sent `record.id`, `kind`, `sender`, `to`, `recipients`, `payload`, `reply_to` | `record.id`, `kind`, `sender`, `to`, `recipients`, `content`, `reply_to`; `records=[record.id]` |
| sent `record.channel` | dropped: it is `channel` |
| sent `record.routed_out`, `op`, `wake`, `sender_span_id`, `tool_span_id` | `data["record"]` with these five keys |
| sent `rejected` | `rejected` |
| delivered `record`, `recipients`, `seq` | `records=[record]`, `recipients`, `seq` |
| delivered `notify`, `result` | `data` |
| read `files` | `data["files"]`; `records` the distinct non-null `record` values of `files`, in order |
| read `read`, `tool_span_id`, `listing` | `data` |
| waited `wait`, `channels`, `outcome`, `duration` | `wait` (`id`, `channels`, `outcome`, `duration`) |
| waited `path`, `file_id`, `timeout`, `started`, `idle`, `woken_by`, `files`, `ended` | `data` |
| `version: 1` | `data["version"] = 1` |

| Today (`InfoEvent.data`) | `SwarmEvent` |
|---|---|
| `type: "result"`, `kind: "member_result"` or `"final"` | `kind` |
| `member` | `member` |
| `version: 1` | `version=1` |
| the rest (`span_id`, `status`, `time`, `sandbox`; `span_id`, `mode`, `reason`, `stop_reason`) | `data` |
| `type: "ledger"` or `"metrics"`, then the model's fields | `kind="ledger"` or `"metrics"`, `member=None`, `data` the model's dump exactly as stored (`version` included), and `version` copied from it |

So `data` for the ledger and metrics is the store entry itself, and `SwarmLedger.model_validate(event.data)` reads it. The versions stay 1: the event type tells the new format apart from the `InfoEvent`s, and the rule that the evidence `version` tracks the payload's shape, not the tools' texts (decision: Ransom, 2026-10-23), applies to `data`. The swarm writes no `InfoEvent` after the switch.

The private helpers become two: `_comm(kind, *, agent, agent_span_id, loop, nested, channel, **typed, data)` builds and writes a `CommunicationEvent`, and `swarm_event(kind, *, member, version=1, **data)` replaces `evidence()` for `_control.py:599` and `_final.py:113`. A typed field the validator refuses raises at the writer, which a test of each kind catches.

### How inspect_swarm reads them

`EventLog.__call__` (`_ledger.py:318-326`) matches `isinstance(event, CommunicationEvent) and event.source == "inspect_swarm"` and builds the same records from typed fields:

- `CommRecord`: `kind`, `member=event.agent`, `channel`, `record=event.records[0]` for `sent` and `delivered` (else `None`), `rejected=event.rejected.control` if rejected, `payload_bytes` from `utf8_bytes(event.record.content)` for an accepted `sent` (a rejected payload is still not measured, since it can hold text no UTF-8 encoder takes), and `listing=event.data.get("listing") is True`.
- `WaitRecord`: `member=event.agent`, `outcome=event.wait.outcome`, `duration=event.wait.duration`, `idle=event.data["idle"]`.

`_comm_metrics` and `_waits` read these records and do not change. Matching on `source` keeps other producers' communication (a later bridged member's, or another library's) out of the swarm's metrics, as the source check does today. `swarm_ledger()` and the analysis columns read the store and do not change.

### Viewer: types, filter and renderers

All in ts-mono, in the files `ReviewEvent` is wired into, which are the ones a new type touches:

- **Types.** Regenerate `packages/inspect-common/src/types/generated.ts` from inspect_ai's schema and add the aliases (`CommunicationEvent`, `CommunicationRecord`, `CommunicationRejection`, `CommunicationWait`, `SwarmEvent`) in `packages/inspect-common/src/types/index.ts`; add both to the hand-kept unions (`IC/types.ts` `EventType` and `eventTypeValues`, `apps/inspect/src/app/types.ts`, `apps/inspect/src/client/api/types.ts`), the scout debug tree's exhaustive switch (`apps/scout/src/app/transcript/debugEventTree.ts:117`), the test factories (`packages/inspect-common/src/testing/index.ts`), `eventTitle` (`IC/event/utils.ts`) and `extractEventFields` (`IC/eventText.ts`), so search and copy include a record's content and the swarm event's kind and member.
- **Filter labels.** `eventTypes` (`hooks.ts:11`) gains `communication: "Communication"` and `swarm: "Swarm"`. Neither is hidden by default: the point is to see the swarm's communication, and to be able to hide it alone.
- **Only the types the sample has.** `SampleDisplay` computes the set of `event` values in `sampleEvents`, memoized on the array, and passes it to `TranscriptFilterPopover`, which passes it to `arrangedEventTypes`; that keeps exactly the keys in the set. Which toggles are listed depends on presence alone, never on the hidden list: `sampleEvents` is the sample's unfiltered events (`SampleDisplay.tsx:230`), so a type the user hid is still in the set and its toggle still lets them show it again, while a type the sample lacks is not listed even when it is hidden (by default, by the None preset, or by the user in another sample). The hidden list itself is unchanged and kept apart from the toggles: `useTranscriptFilter` still stores it across samples, the presets still write it as today (None hides every type in the map), and a hidden type the current sample lacks stays hidden for the next sample that has it. With no set (a chunked sample, whose events array is empty, or no events yet), every type is listed, as today. A running sample's set grows as events arrive.
- **`CommunicationEventView`** (`IC/CommunicationEventView.tsx`), an `EventRow` like `ReviewEventView`, one line per event:
  - `sent`: `counter-6 → shared_memory: create (w-1)`, then `to b, c` when addressed, then the first line of `record.content`, truncated; a rejected send shows `refused (rate)` styled as a refusal, with the reason inline.
  - `delivered`: `counter-6 → shared_memory: delivered w-1 (#1)`, and `to b, c` when `recipients` is not empty.
  - `read`: `counter-2 ← shared_memory: read w-1, w-4`, or `read (nothing)` with no records.
  - `waited`: `counter-5 waited 4.2s on shared_memory: news`.
  - `nested` adds `(subagent)` after the agent's name. Clicking the row expands the whole event (typed fields and `data`) as a `JSONPanel`. Content is rendered as plain text, never Markdown ([Security](#security)).
- **`SwarmEventView`** (`IC/SwarmEventView.tsx`), an `EventPanel` titled `Swarm: <kind>` with `member` beside it when set and `data` as a collapsed `JSONPanel`, as `InfoEventView` renders a payload. It knows no kind: inspect_swarm's formats change without a ts-mono PR.
- **Dispatch.** `RenderedEventNodeInner` gets a case for each type (`IC/TranscriptVirtualList.tsx`).
- **Outline.** `outlineFilterVisitors` gains `removeNodeVisitor("communication")` and `removeNodeVisitor("swarm")`, and `kTurnPassthroughEvents` gains both, as for info: removed from the outline, and absorbed into the turn where a path keeps them.
- **Icons.** Existing entries of `packages/react/src/icons/icons.ts` (the ones `approval` and `info` use), so no icon is added.
- **Member timelines.** Nothing changes: the filter applies to the events before stored timelines are converted, so hiding Communication hides it in every member's timeline and in the Overall one, and the swarm's span still gives the sample its Overall timeline.
- **Activity view** (`packages/inspect-components/src/sample-activity/activityData.ts`): its event switch ignores types it does not know, so nothing is required.

### inspect_ai changes besides the events

- **The union.** `CommunicationEvent` and `SwarmEvent` join `Event` (`_event.py`), and `tests/log/test_event_discriminator.py` counts 27 members. `event/__init__.py` exports the two events and `CommunicationKind`, `CommunicationRecord`, `CommunicationRejection` and `CommunicationWait`.
- **Condensing.** `walk_event` (`_condense.py:991`) gets `walk_communication_event`, which passes `record.content` through `content_fn` and `data` through `walk_json_value`, and `walk_swarm_event`, which passes `data` through `walk_json_value`, so large payloads become attachments as they do in `InfoEvent`s today.
- **`inspect ctl`.** `HIGH_SIGNAL_EVENT_TYPES` gains `communication` and `swarm`, since `info`, where the evidence was, is in it; `tests/_control/test_events.py` mirrors the set.
- **Textual display.** A one-line renderer for each type in `_renderers`, as `ReviewEvent` has.
- **Docs.** `docs/reference/inspect_ai.event.qmd` lists both.
- **Schema.** `python src/inspect_ai/_view/schema.py` regenerates `inspect-openapi.json` and ts-mono's generated types; the PR lands with the ts-mono PR by inspect_ai's two-phase gitlink procedure.

## Alternatives considered

- **Keep the non-communication evidence as `InfoEvent`s.** Rejected by Ransom (2026-10-27): satellite packages already have event types, and showing only the types a sample has keeps the filter from growing for everyone else.
- **One swarm event for everything, communication included.** Simpler in inspect_ai, but communication is the evidence other producers share, and the one the viewer renders per kind; a swarm-owned `data` gives it no typed fields to render or count.
- **Typing every communication field.** The shared memory's files, operations and wait filters would become inspect_ai schema that no other producer writes, and each channel's change an upstream PR. `data` keeps them with their owner.
- **`kind: str` on `CommunicationEvent`.** New kinds without an inspect_ai change, but readers could not rely on the vocabulary, and the viewer's per-kind lines would need a fallback anyway. The four kinds are general (ORBIT uses three of them); a fifth is rare enough to be an inspect_ai change.
- **A typed ledger and metrics in inspect_ai.** Discussed under [`SwarmEvent`](#swarmevent).
- **Event names.** `AgentMessageEvent` (`"agent_message"`) collides with the Codex item of that name and is too narrow: a shared-memory write, a read and a wait are not messages. `CommEvent` abbreviates, which no inspect_ai event does. `InterAgentEvent` is awkward and says nothing of what happened. `CommunicationEvent` follows the one-noun names (`ModelEvent`, `ApprovalEvent`, `CompactionEvent`). For the swarm, `SwarmEvent` matches `SentinelEvent`; `SwarmEvidenceEvent` would be longer for no gain.
- **A public `Transcript.event()` or typed helpers (`transcript().communication(...)`).** A new public inspect_ai API for one caller; inspect_swarm already uses private transcript names, and helpers would have to grow with the fields. Can be added if another library needs it.
- **A producer-written summary line on `SwarmEvent`**, so the viewer could show `counter-5 submitted after 13.2s` without knowing the kind. Puts presentation in the log and harness-composed text in front of the reader; `kind` and `member` suffice for one line. It can be added later as an optional field.

## Compatibility and migration

- **New logs and old readers.** A log with a `CommunicationEvent` or `SwarmEvent` cannot be read by an inspect_ai or viewer that predates them: the discriminated union rejects the unknown `event` value, and with it the log. inspect_ai has accepted this cost for each new type (sentinel, review, checkpoint, branch, anchor, compaction events). It applies to every swarm log written after the switch, a swarm of one included.
- **Old logs and the new inspect_swarm.** Logs written before the switch keep their `InfoEvent`s and still open in every viewer. What they lose: inspect_swarm's tests, docs and helpers no longer describe their evidence, a reader written against the new types finds none in them, and the viewer's compact lines and per-type toggles do not apply to them. inspect_swarm itself reads nothing back from old logs' events: `swarm_ledger()` and the analysis columns read the store entries, whose format does not change. No compatibility path is provided, as the format is experimental ([swarm-implementation-plan.md Decisions](swarm-implementation-plan.md#decisions), item 6).
- **inspect_swarm's dependency.** The switch needs an inspect_ai with the new events in `src/`, so the floor rises to the release that has them, or, until it is released, `pyproject.toml` points at inspect_ai `main` by git reference as `AGENTS.md` allows.
- **Public API.** inspect_ai adds the two events and four models to `inspect_ai.event`, marked experimental. inspect_swarm's public API does not change; its documented log format does.
- **Scout.** inspect_scout's event filters list types by literal (`src/inspect_scout/_transcript/types.py`, `_scanner/filter.py`, `_scanner/validate.py`), so a scanner cannot ask for the new types until Scout adds them; scanners that take all events see them. Its viewer copy of the generated types needs nothing for a new type.
- **ORBIT.** The port design's viewer change accepts the swarm's `CommunicationEvent`s instead of `InfoEvent`s with source `inspect_swarm` ([swarm-orbit.md](swarm-orbit.md#compatibility-and-migration)).

## Security

- **Model text in the new events.** A record's `content`, and `data` (file paths, operations, rejection reasons), carry model output, possibly adversarial, as the `InfoEvent` payloads do today. They are JSON data in the log. The viewer renders `content` and every string of `data` as plain text, never as Markdown or HTML, and truncates the one-line preview; `InfoEventView` renders a string payload as Markdown, which the new renderers do not copy. Search indexes the text as it indexes tool results.
- **Names.** `agent`, `member`, `sender`, `recipients` and `channel` come from the eval author's roster and channel names, or are bound by the bus, never from model output ([swarm-communication.md Security](swarm-communication.md#security)).
- **Unencodable text.** A rejected payload can hold lone surrogates; it is recorded as today (`InfoEvent.data` holds the same string) and the ledger still measures only accepted payloads.
- **Validation.** The validators refuse shapes, not values; nothing in a field's value can make a log unreadable.

## Testing

inspect_ai (mockllm, no network or Docker):

- `tests/log/test_event_discriminator.py`: the member count.
- `tests/log/test_swarm_events.py` (new): a round trip of each new event through `EvalLog` JSON and the `.eval` format; each kind's valid shape constructs and each refused shape the validator names raises `ValueError` (table-driven); a long `record.content` and a long string in either event's `data` become attachments and resolve back.
- `tests/_control/test_events.py`: the high-signal set.
- The schema check CI already runs (`check-schema-and-types`).

ts-mono (vitest):

- `CommunicationEventView` renders each kind's line, a rejected send and a nested loop, and renders Markdown-looking content as text.
- `SwarmEventView` renders an unknown kind with its data.
- `arrangedEventTypes` with a set of present types lists exactly those, table-driven over the hidden list: the default exclusions (none of them in the set, none listed), the None preset (only the present types listed), a present type the user hid (listed), and a type hidden in a previous sample that the current one lacks (not listed, and still hidden when a later sample has it); with no set it lists every type.
- The outline of a fixture with a model event, a communication event, a swarm event and a tool event is one turn, with neither new event in it.

inspect_swarm (mockllm, asyncio and trio):

- `tests/swarm_runtime.py` gets `comm_events(transcript, kind)` and `swarm_events(transcript, kind)`, returning the typed events, in place of `evidence()`, and the twelve test files match `CommunicationEvent` and `SwarmEvent` and assert typed fields and `data` keys instead of payload keys.
- `tests/test_comm_evidence.py`: one run writes each kind with every field of the mapping table, and the swarm writes no `InfoEvent` with source `inspect_swarm`.
- The ledger and metrics tests pass unchanged in what they assert: the metrics computed from the new events equal those from the same scripted run's expectations today.
- `tests/test_metrics.py`: a member timeline contains its members' `CommunicationEvent`s and `member_result`, and the ledger's `SwarmEvent.data` validates as `SwarmLedger` and equals the store entry.

## Implementation plan

1. **inspect_ai PR (UKGovernmentBEIS/inspect_ai): the event types.** `src/inspect_ai/event/_communication.py`, `_swarm.py` (new), `_event.py`, `__init__.py`; `src/inspect_ai/log/_condense.py`; `src/inspect_ai/_control/events.py`; `src/inspect_ai/_display/textual/widgets/transcript.py`; `src/inspect_ai/_view/inspect-openapi.json` (regenerated); `docs/reference/inspect_ai.event.qmd`; the tests above. It lands with step 2 by the two-phase gitlink procedure (the `_view/ts-mono` pointer, then `_view/dist`).
2. **ts-mono PR: viewer support.** The files under [Viewer](#viewer-types-filter-and-renderers): generated types and aliases, the unions, `hooks.ts`, `TranscriptFilter.tsx`, `SampleDisplay.tsx`, `CommunicationEventView.tsx` and `SwarmEventView.tsx` (new), `TranscriptVirtualList.tsx`, `event/utils.ts`, `eventText.ts`, `outline/tree-visitors.ts`, `debugEventTree.ts`, the test factories, and the tests. Its filter change (present types only) does not depend on the new types and can be reviewed on its own commit.
3. **inspect_swarm PR: switch the writers and readers.** `pyproject.toml` and `uv.lock` (the inspect_ai with step 1: a released floor, or a git reference to inspect_ai `main` until it is released, with `uv lock --upgrade-package inspect-ai`); `src/inspect_swarm/_evidence.py`; `_ledger.py` (the matcher, the two record builders, the docstring at `:122`); `_metrics.py` (the comment at `:69`); `_final.py` and `_controller/_control.py` (the calls to `swarm_event`); `docs/analysis.qmd`, `docs/shared-memory.qmd`; the twelve test files and `tests/swarm_runtime.py`. It needs step 1 only; the viewer renders its logs well once step 2 is released.

## Open questions

None.

## Not this design

- **The tool rows in a member's outline**, a known, pre-existing outline quirk that this design neither causes nor fixes. Inspect records a call's `ToolEvent` inside its tool span but with the caller's `span_id` (it is constructed before the span opens, `src/inspect_ai/model/_call_tools.py:388`), so the viewer treats the event as a sibling of the span and `unwrap_tools` keeps the span (`IC/transform/transform.ts:196-198`). A tool span that holds events the transcript shows, such as the swarm's evidence, stays in the outline as its own row before its call, which ends the turn: "1 turn / shared_memory / 1 turn". It happens today with the evidence as `InfoEvent`s, and readers already handle it.
- **Mapping bridged traffic** to `CommunicationEvent`: Codex `agent_message` items and `wait` calls in inspect_swe's Codex event consumer, Claude Code teammate messages once inspect_swe represents teams.
- **Scout support**: the two types in inspect_scout's event filter literals (`TIMELINE_DEFAULT_EVENTS`, `validate_events_filter`, `TYPE_TO_EVENT_FILTER`), so scanners can select swarm communication.
- **Dataframe columns** for communication events in inspect_ai's `events_df()`.
- **A swarm view in the viewer**, such as a message timeline or member graph over `CommunicationEvent`s.
- **The Activity view** placing communication on its per-conversation rows.
- **Present-types filtering for Scout's transcript filter**, which keeps its own list (`apps/scout/src/app/transcript/hooks/useTranscriptColumnFilter.ts`).
- **A public API for writing events** other than `InfoEvent` (`Transcript.event()`), if another library needs one.
