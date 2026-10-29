# Inspect Swarm: the result record in the transcript

## Overview

The swarm's result record (each member's state, status and time, and which member's output is the answer) moves out of the sample store and into the transcript. The runtime writes it once, when the swarm ends, as one evidence event in the swarm's span, from the synchronous `finally` that already writes the ledger and the metrics, so a limit, error or cancellation that ends the swarm still leaves it, marked `provisional`. While the swarm runs there is no record at all, so no member's tool can read its peers' results from it.

A public helper, `swarm_result()`, reads it. Called with no argument it reads the transcript of the sample being run or scored, as a scorer or a solver wrapper does, both during the eval and when a log is re-scored. Called with an `EvalSample` it reads a sample from a log, for analysis. Either way it returns the record of the last swarm to end in the sample, or `None`. The ledger and the metrics, which the swarm writes only after every member has ended and which hold no member text, stay in the store.

The scoring plan after M2 (the result contract, `member_scores`, the analysis helpers) builds on this home: members' outputs, answer keys and verdicts enter the record at the swarm's end, and every reader uses `swarm_result()`.

## Why

Tools that read and write the sample store are common in Inspect tasks, so anything the swarm puts in the store is readable by members' tools during the run (decision: Ransom, 2026-10-29). Today that view is each peer's state, status and time. Once the record carries members' outputs and verdict explanations ([swarm-scoring.md, What the record adds](swarm-scoring.md#what-the-record-adds), the first step of the scoring plan after M2), a store tool would show members their peers' answers: a channel between members outside the bus, with no fencing and no evidence. The user docs warn about it today ([Scoring](https://meridianlabs-ai.github.io/inspect_swarm/scoring.html#the-record-belongs-to-the-swarm)); a warning does not stop a generic store tool a task already has.

Tools that read the transcript are rare. A tool that does already sees every member's model calls and tool calls, so a record there shows such a tool nothing new. The record therefore lives in the transcript, as evidence: scorers read it in the sample, analysis reads it from the log.

The record is moved whole, not only the outputs and verdicts the scoring plan adds, so that it has one home and one reader.

## Goals and non-goals

Goals:

- The result record lives in the transcript, not the store, with the same fields and format version.
- One public reader for scorers, solver wrappers, re-scoring and analysis, correct under bounded transcripts and with condensed logs.
- No record exists while its swarm runs.
- The scoring plan after M2 (swarm-scoring.md steps 1 to 7) builds on the new home, and the other designs that name the store key say where the record is.

Non-goals:

- Moving the ledger or the metrics ([The ledger and the metrics](#the-ledger-and-the-metrics) says why they stay).
- The `SwarmEvent` type itself ([swarm-event-types.md](swarm-event-types.md)); this design adds one kind to its mapping.
- Stopping a tool from reading the transcript. It is Python code in the sample, as it is today.
- Records of several swarms in one sample beyond "the last to end", as the store gives today ([Not this design](#not-this-design)).
- A compatibility reader for logs written while the record was in the store.

## Current behaviour

inspect_swarm references are to `main` at `33bf17b`; inspect_ai references are to the 0.3.286 release this repository's lock file resolves, as installed in its virtualenv. Paths are relative to each repository's root.

### Where the record is written

- **The store key and the writer.** `RECORD_KEY = "inspect_swarm:result"` (`src/inspect_swarm/_record.py:9`); `write_record()` sets `record.model_dump(mode="json")` under it (`:79-80`). The models are `MemberResult`, `FinalRecord` and `SwarmResultRecord` (`:12-75`), with `version: Literal[1]`.
- **Every change rewrites it.** `settle()` (`src/inspect_swarm/_final.py:74-90`) builds the record from the members' results, applies the answer rule, sets the chosen member's output on the swarm's `AgentState`, and writes the record. `SwarmRun._settle()` (`src/inspect_swarm/_controller/_control.py:630-638`) calls it with `provisional=True` when the swarm starts (`:405`), when a member starts (`:346`), when one ends (`:625`) and when the controller chooses the answer (`:386`). `finalise()` (`_final.py:95-121`) calls it once more with `provisional=False`, then writes the `final` evidence event.
- **The final step can fail.** `finalise()` sets the returned conversation first, and an answer that does not fit the sample's message limit raises there, before `settle()`, leaving the record provisional (`_final.py:103-110`).
- **The `finally`.** `SwarmRun.run()`'s synchronous `finally` (`_control.py:468-530`) runs on every exit, cancellation included. It writes the ledger and the metrics with `write_ledger()` and `write_metrics()` (`src/inspect_swarm/_evidence.py:188-199`), each both an `InfoEvent` with source `inspect_swarm` (`{"type": "ledger", **data}`, `{"type": "metrics", **data}`) and a store entry. It computes the answer once more for the metrics (`_control.py:505-508`). `ended` says whether finalisation completed (`:421`, `:463`).
- **Per-member evidence.** Each member's end writes a `member_result` event (status, time, sandbox) in its span (`_control.py:616-624`), and finalisation a `final` event (`_final.py:113-121`). Neither carries member text.
- **Log copies.** Spans inside the swarm log no `StoreEvent`s, but the swarm's span and every enclosing span log the net change, and the sample's `store` is copied when it ends ([swarm.md Store events](swarm.md#observer-evidence-accounting-and-metrics)), so each record value is in the log several times. `tests/test_controller.py:976-982` asserts the swarm span's change includes the record.

### Who reads it

Searched in this repository and in every checkout beside it (`~/git/*`, excluding node_modules and virtualenvs) for `inspect_swarm:result`, `inspect_swarm:ledger`, `inspect_swarm:metrics`, `RECORD_KEY` and `swarm_ledger`. Nothing outside this repository uses them.

- **Runtime.** Nothing reads the record back. Controllers see `MemberHandle`s and `MemberEnded` events, not the record.
- **Scorers after the swarm.** The docs' per-member sandbox example (`docs/scoring.qmd:64-78`) and `examples/proof_swarm.py:117-139` read `state.store.get("inspect_swarm:result")["members"]` for each member's `sandbox`.
- **Docs that name the key.** `docs/scoring.qmd` (the record section and its rule, `:23-51`), `docs/restrictions.qmd:24`, `:28-30`, `docs/analysis.qmd:13`, `docs/index.qmd:104-108`, `docs/channels.qmd:47`, `README.md:105-109`.
- **Tests.** `tests/swarm_runtime.py` reads the live record in `after()` (`:134-143`), a test agent that polls it until named members have ended, and after a run in `run_swarm()` (`:212`). The record's write timing is `tests/test_final.py:423-484`; other reads are in `test_final.py`, `test_background_members.py`, `test_member_sandboxes.py` (a scorer, `:443-454`), `test_metrics.py`, `test_examples.py`, `test_swarm_limits.py` and `test_controller.py`.
- **Designs.** [swarm-scoring.md](swarm-scoring.md) Part 2: `member_scores` reads members (and from Part 2 their outputs) from the record during final scoring and re-scoring, and `attempt_rows` reads it from logs. [swarm-orbit.md P1](swarm-orbit.md#orbits-solver-wrapper) has ORBIT's solver wrapper read it in its own synchronous `finally`, after the swarm has exited, possibly by raising.

So every reader runs after the swarm has exited: a scorer, a solver wrapper around the swarm, re-scoring or analysis. Only the test helper `after()` reads the record while the swarm runs, as a scheduling device for tests.

### What a transcript reader gets from Inspect

- **One transcript per sample.** Every member's events, model and tool calls included, are in the sample's transcript, which any code in the sample reaches with `transcript()`.
- **Bounded transcripts.** A sample's transcript is bounded only when `INSPECT_TRANSCRIPT_BOUNDED` is set and the eval has a sample buffer database; it then gets a history provider over that database (`src/inspect_ai/_eval/task/run.py:615-624`, `:2494-2500`). Otherwise it keeps every event. A bounded transcript keeps the newest 100 events resident (`src/inspect_ai/log/_transcript.py:84`) and evicts older ones; every event is written synchronously to the buffer as it is logged (`src/inspect_ai/_eval/task/log.py:664-667`), and the provider resolves attachments when it reads events back (`src/inspect_ai/log/_recorders/buffer/transcript_history_provider.py:204`, `:220`).
- **`TranscriptHistory.find_last(predicate)`** (`_transcript.py:461`) returns the newest matching event, searching the resident tail first and then the provider in pages of 16 growing to 256, newest first. It raises `TranscriptHistoryUnavailableError` only when evicted events cannot be served: no provider, which the run's configuration rules out, or a buffer already torn down. It is synchronous.
- **Re-scoring.** `task_state_from_sample()` rebuilds the `TaskState` and seeds a fresh, unbounded transcript with the sample's events, attachments resolved (`src/inspect_ai/_eval/score.py:449-512`, `:504-507`), so a scorer's `transcript()` holds the run's events.
- **Logs.** When a log is written, strings longer than 100 characters in an `InfoEvent`'s `data` become `attachment://<hash>` references into `EvalSample.attachments` (`src/inspect_ai/log/_condense.py:455-459`, `:1099-1104`). `read_eval_log()` resolves them only with `resolve_attachments=True`; `samples_df()` reads full samples with them resolved (`src/inspect_ai/analysis/_dataframe/samples/table.py:336`). `resolve_events_attachments(events, attachments)` (`_condense.py:931`) resolves a list of events.

A spike ran a two-member swarm on mockllm, then a solver step that wrote one `InfoEvent` with a 300-character string and 150 more events, and a scorer that called `transcript().history.find_last()` for the swarm's ledger event:

- unbounded and with `INSPECT_TRANSCRIPT_BOUNDED=1`, where the ledger event had been evicted, the scorer found it;
- `inspect_ai.score()` on the written log found it, and the long string resolved;
- `read_eval_log()` without resolving gave the long string as `attachment://...`, and with `resolve_attachments=True` the text.

### What the ledger and the metrics hold

Both are written once, in the `finally`, after every member of the swarm has ended, and hold names, statuses, usage, cost and counts, no member text (`src/inspect_swarm/_ledger.py`, `src/inspect_swarm/_metrics.py`). `swarm_ledger()` and `SwarmColumns` read the ledger from `sample.store` (`src/inspect_swarm/analysis/_columns.py:14-34`), and [swarm-critical-path.md](swarm-critical-path.md#where-it-is-computed) puts its metric in the metrics store entry so scorers can read it.

## Design

### At a glance

| | Today | After |
|---|---|---|
| Where | sample store, `inspect_swarm:result` | an `InfoEvent` with source `inspect_swarm` and `type: "result_record"`, in the swarm's span |
| When written | at start, at each member start and end, at `choose_answer()`, at finalisation | once, in `SwarmRun.run()`'s `finally` |
| While the swarm runs | readable, provisional | does not exist |
| After an outer limit, error or cancellation | the last write, `provisional: true` | written in the `finally`, `provisional: true` |
| Reader | `state.store["inspect_swarm:result"]`, `sample.store[...]` | `swarm_result()`, `swarm_result(sample)` |
| Format | `SwarmResultRecord`, `version: 1` | unchanged |
| Ledger, metrics | store and event | unchanged |

### The event

```python
# src/inspect_swarm/_record.py
RECORD_TYPE = "result_record"


# Only the runtime writes the record: once, when the swarm ends, in the swarm's span.
def write_record(record: SwarmResultRecord) -> None:
    transcript().info(
        {"type": RECORD_TYPE, **record.model_dump(mode="json")}, source="inspect_swarm"
    )
```

The payload has the shape of the ledger's and the metrics' events: `type`, then the model's fields, its own `version` included. The models do not change, so `version` stays 1: the fields and their meanings are the same, and only where the record is kept and when it is written change. `RECORD_KEY` is removed.

Under [swarm-event-types.md](swarm-event-types.md#how-inspect_swarm-writes-them) the record becomes a `SwarmEvent` with `kind="result_record"`, `member=None` and `data` the model's dump, its `version` included, like the ledger and the metrics; that design's mapping gains the row.

### When it is written

Once, in `SwarmRun.run()`'s `finally`, first, before the ledger:

```python
# src/inspect_swarm/_controller/_control.py, SwarmRun.run(), finally
record = SwarmResultRecord(
    members=self._results(),
    final=answer(self._results(), self._final, self._pick, provisional=not ended),
)
write_record(record)
...
write_metrics(swarm_metrics(..., record.final.member))
```

- **`settle()` stops writing.** It keeps applying the answer rule and setting the chosen member's output on the swarm's `AgentState` at every change, which `as_solver()` copies to the task state however the swarm ends ([swarm-scoring.md The final answer](swarm-scoring.md#the-final-answer-first)), and returns the `FinalRecord`; it no longer builds or writes a record. `finalise()` keeps writing the `final` event. The metrics take the chosen member from the record instead of a second `answer()` call.
- **`provisional`** keeps its field and meaning: `True` when the swarm ended without finalising its answer (a limit, error or cancellation ended it first, or the answer did not fit the sample's message limit), `False` after finalisation. The record a finalised swarm wrote before is the record it writes now; the record an interrupted swarm leaves is the one its last provisional write left, since every change to the members or the choice wrote it.
- **States at the end.** A member the controller never started is `pending`. A started member's wrapper calls `end()` in its own `finally` (`src/inspect_swarm/_member.py:472-474`), so the member is `ended` with its status once its task has run; `running` stays a valid `MemberState` in the format.
- **Nothing needs the live record.** Every reader runs after the swarm has exited ([Who reads it](#who-reads-it)). Controllers have handles and events for live state, and the per-member `member_result` events mark each end in the transcript as it happens. The test helper `after()` waits on those events instead ([Testing](#testing)).
- **Under cancellation.** `transcript().info()` is synchronous, so the write runs in the `finally` under cancellation, as the ledger's does. A sample whose process dies leaves no record, as it leaves no logged store today.
- **Log size.** The record is in the log once, in its event: no longer in the swarm's span's `StoreEvent`, the enclosing spans' or `sample.store`. Long strings in it (from Part 2, members' outputs) are condensed into attachments, shared with identical text elsewhere in the sample.

### Reading it: `swarm_result()`

```python
# src/inspect_swarm/_record.py, exported from inspect_swarm with the three models
def swarm_result(sample: EvalSample | None = None) -> SwarmResultRecord | None:
    """Read the swarm's result record.

    The swarm writes its record to the transcript once, when it ends, however it ends. A sample in which several swarms ended has a record for each; this returns the last one written, which is the outermost of nested swarms.

    Args:
        sample: A sample read from a log, with its events. Omit it to read the transcript of the sample being run or scored: in a scorer, during the eval and when a log is re-scored, or in a solver after the swarm.

    Returns:
        The record of the last swarm to end, or `None` when no swarm has ended in the sample. A swarm that is still running has no record.

    Raises:
        ValueError: The sample was read without its events, or the record is one this inspect_swarm cannot read, such as a version other than 1.
    """
```

- **Without a sample**: `transcript().history.find_last(_is_record)`, which reads back evicted events in a bounded transcript ([Bounded transcripts](#bounded-transcripts)). The live and the re-scoring transcripts hold events with attachments resolved, so nothing more is needed.
- **With a sample**: the newest event in `sample.events` for which `_is_record()` holds, then `resolve_events_attachments([event], sample.attachments)` when the sample has attachments, so a log read with or without `resolve_attachments` gives the same record. Only that one event is resolved, not the whole sample.
- **`sample.events` empty** means the sample was read with `exclude_fields={"events"}` (a logged sample always has its `SampleInitEvent`): `ValueError` naming the sample and saying to read its events, rather than `None`, which would read as "no swarm".
- **Validation**: `SwarmResultRecord.model_validate()` of the payload without `type`. A `ValidationError` becomes `ValueError` naming the sample (or "the transcript" without one), as `swarm_ledger()` does.
- **`_is_record(event)`** is `isinstance(event, InfoEvent) and event.source == "inspect_swarm" and isinstance(event.data, dict) and event.data.get("type") == RECORD_TYPE`. When the `SwarmEvent` switch lands, it also accepts `isinstance(event, SwarmEvent) and event.kind == RECORD_TYPE`, reading `event.data`, and keeps the `InfoEvent` form, so a log reads the same whichever inspect_swarm wrote it. That is two lines, and it keeps every log with a record readable by analysis.
- **Synchronous.** ORBIT's wrapper calls it in a `finally` that must not await.
- **Exports.** `swarm_result`, `SwarmResultRecord`, `MemberResult` and `FinalRecord` are exported from `inspect_swarm` (scorers import from there; `MemberState` already is) and listed in a "Results" section of `docs/reference/inspect_swarm.qmd`. Analysis code imports the same function; `inspect_swarm.analysis` does not re-export it.

A scorer that read the store now calls the helper:

```python
async def score(state: TaskState, target: Target) -> Score:
    record = swarm_result()
    assert record is not None, "the task's solver is a swarm"
    passed = [
        m.name
        for m in record.members
        if (await sandbox(m.sandbox).exec(["sh", "/tests/run.sh"])).success
    ]
    ...
```

### Bounded transcripts

- **Live.** A bounded transcript always has a history provider ([What a transcript reader gets](#what-a-transcript-reader-gets-from-inspect)), so `find_last()` finds the record however many events came after it. The record is written before the ledger, the metrics and any scorer, so the search reads the resident tail, and pages back through the provider only when more than 100 events (a later solver step's, or earlier scorers' model calls) followed it.
- **A sample with no record** (a plain agent scored by Part 2's `member_scores`) makes `find_last()` read the whole history, newest first, in pages. That is one pass per call, paid only by tasks that score non-swarm arms with swarm readers.
- **Re-scoring and logs** hold every event: re-scoring seeds an unbounded transcript, and a log's `events` are the full history.
- **`TranscriptHistoryUnavailableError`** propagates. It arises only when the buffer has been torn down, which does not happen while the sample runs or is scored.

### Who may write it

Only the runtime writes the record. Task code must not write an `InfoEvent` with source `inspect_swarm`, since `swarm_result()` reads the last one; the docs say so where they said task code must not write the store key.

### The ledger and the metrics

They stay in the store, and as events, unchanged:

- **The stated risk does not apply.** They are written once, after every member of their swarm has ended, so no member of that swarm can read them, and they hold no member text.
- **Scorers and analysis read them there.** `state.store` gives scorers the metrics (swarm-critical-path.md's intent); `swarm_ledger()` and `SwarmColumns` read every log written so far from `sample.store`, independent of the `SwarmEvent` switch.
- **What remains readable.** A swarm nested in a member writes its ledger and metrics when it ends, and the outer swarm's other members can then read those through the store: the inner members' names, usage, cost and counts. A swarm that ran earlier in the sample leaves the same to a later swarm's members. Neither is a peer's answer.

[Open question 1](#open-questions) offers the alternative of moving them too.

### The docs

The user docs state the result:

- `docs/scoring.qmd`, "The result record": the record is an evidence event the swarm writes when it ends; read it with `swarm_result()`; the JSON shown is the record (the event adds `"type": "result_record"`); `provisional: true` when a limit, error or cancellation ended the swarm before it finalised. "The record belongs to the swarm" becomes: only the swarm writes it; there is no record while the swarm runs; a tool that reads the transcript sees every member's events anyway, a channel between members the swarm records no evidence of. The per-member sandbox example uses `swarm_result()`.
- `docs/restrictions.qmd`: "The store keys" lists `inspect_swarm:ledger` and `inspect_swarm:metrics`, written when the swarm ends, which task code must not write; the "What the log records" bullet drops "the result record read while the swarm runs" and names a tool that reads the transcript instead.
- `docs/channels.qmd:47`: the bullet becomes a tool that reads the transcript, which sees the other members' model and tool calls.
- `docs/index.qmd` and `README.md`: two store entries and the result record in the transcript, read with `swarm_result()`.
- `docs/analysis.qmd:13`: the record is read with `swarm_result(sample)`.
- `examples/proof_swarm.py`'s scorer uses `swarm_result()`.

### Changes to the other designs

Made in this design's pull request:

- **[swarm-scoring.md](swarm-scoring.md).** Part 1's [What the swarm records](swarm-scoring.md#what-the-swarm-records) describes the event, the single write and `swarm_result()`, and drops "Why the store" (its premise, that re-scoring rebuilds only the store, no longer holds) and the readable-by-member-code bullet. Its testing and plan say the same. Part 2 builds on the event: [What the record adds](swarm-scoring.md#what-the-record-adds) has the added fields enter the record at the swarm's end; `member_scores`, re-scoring, `attempt_rows`, the logs table, Compatibility and Security read and describe the event; and the plan after M2 depends on this design's pull request.
- **[swarm-orbit.md](swarm-orbit.md).** The ORBIT wrapper reads the record with `swarm_result()` in its synchronous `finally`.
- **[swarm-chosen-names.md](swarm-chosen-names.md)**, **[swarm-implementation-plan.md](swarm-implementation-plan.md)** and **[swarm-lean-open-problems.md](swarm-lean-open-problems.md)**: the store key becomes the record, with a link here, and the plan's documentation steps state this design's rule.
- **[swarm-event-types.md](swarm-event-types.md).** The `SwarmEvent` kinds and the mapping table gain `result_record`, and the reader keeps both forms ([Reading it](#reading-it-swarm_result)).

## Alternatives considered

**Keep the record in the store, written only at the end.** Removes the live view, but a swarm nested in a member, or one that ran earlier in the sample, would still leave records with outputs that the outer or later swarm's members read through ordinary store tools. The transcript holds the same facts at no new exposure. Rejected.

**Keep writing the record as an event at every change**, so a reader during the run sees the provisional state. Nothing reads it live ([Who reads it](#who-reads-it)), each write would copy every member's output into the log once Part 2 adds them, and a live record is exactly the view this design removes. Rejected.

**Rebuild the record from the `member_result` and `final` events** instead of writing it. One fewer event, but those events lack fields the record has (role, `choose_name`, gate counts, and from Part 2 outputs, keys and verdicts), and adding them would put member text in per-member events written during the run. Rejected.

**Keep the record in memory for in-sample readers** (a context variable the runtime sets) and write it to the transcript for logs. Two sources with one meaning, and a context-variable reader is as reachable from a member's tool as the store. Rejected.

**Two readers**, one for the running sample in `inspect_swarm` and one for logs in `inspect_swarm.analysis`. They would share all but the event source. One function with an optional sample is simpler to document and test.

**Move the ledger and the metrics too.** One rule, "the swarm keeps nothing in the store". [Open question 1](#open-questions).

**Read old logs' store records in `swarm_result(sample)`.** A fallback to `sample.store["inspect_swarm:result"]` for logs written before this change. No analysis code reads the record from logs yet, and the format is experimental ([swarm-implementation-plan.md Decisions](swarm-implementation-plan.md#decisions), item 6). Not included; such logs are read from `sample.store` by hand.

## Compatibility and migration

- **Eval logs.** New logs have no `inspect_swarm:result` store entry and one `InfoEvent` with `type: "result_record"` per swarm, whose payload is the record's format, `version: 1`, unchanged. The viewer shows it as an info event in the swarm's span. Old logs keep their store entry; `swarm_result()` returns `None` for them ([Alternatives](#alternatives-considered)).
- **Public API.** Code that reads `state.store["inspect_swarm:result"]` or `sample.store[...]` finds nothing and must call `swarm_result()`. The only such code is this repository's docs, example and tests. New exports: `swarm_result`, `SwarmResultRecord`, `MemberResult`, `FinalRecord`. The pull request is a breaking change (`feat!:`), so the release notes name it.
- **Timing.** A reader that polled the store during the run (only the tests) has no record until the swarm ends.
- **Store events.** The swarm's span's net `StoreEvent` no longer includes the record.
- **`SwarmEvent` switch.** When it lands, the record is written as a `SwarmEvent` and the reader accepts both forms, so logs from before and after it read the same.
- **The ledger and the metrics**, their store entries, `swarm_ledger()` and `SwarmColumns`: unchanged.
- **inspect_ai.** No change. The reader uses `TranscriptHistory.find_last()` (public), `InfoEvent` and `EvalSample` (public), and `resolve_events_attachments()` (private, `inspect_ai.log._condense`), as inspect_swarm may use internals.

## Security

- **What reaches the record.** Member names (the eval author's), statuses, times, sandbox names, the controller's `choose_answer()` reason (controller code), and from Part 2 members' outputs (model output), answer keys and verdict explanations (task code over model output). It is JSON data in an `InfoEvent`, never Markdown, and no prompt the harness composes contains it.
- **Who can read it, when.** Nobody during the swarm's run, since it does not exist. After the swarm ends, any code in the sample, through `transcript()`. A member of a later swarm in the sample, or of an outer swarm after a nested one ends, can read the earlier swarm's record with a tool that reads the transcript; such a tool sees that swarm's members' model calls anyway, so the record adds nothing to what it could read.
- **What the store still exposes.** The ledger and metrics, after their swarm ends: no member text ([The ledger and the metrics](#the-ledger-and-the-metrics)).
- **Forged records.** Task code that writes an `InfoEvent` with source `inspect_swarm` and `type: "result_record"` after the swarm would be read as the record, as task code writing the store key would be today. The docs forbid it; models cannot write events.
- **Reading logs.** `swarm_result(sample)` resolves only `attachment://` references found in the record's own event against the sample's own attachments, and validates the result with the pydantic model, so a malformed record raises `ValueError` rather than being used.

## Testing

Mockllm, no network or Docker unless marked, asyncio and trio for async tests.

- **`tests/test_final.py`** (replacing `test_the_record_is_written_at_start_and_on_each_start_and_end`):
  - while the swarm runs, `swarm_result()` called by the controller after a start and after an end, and by a member's tool, returns `None`, and the transcript has no `result_record` event;
  - after it ends, exactly one `result_record` event, in the swarm's span, after the `final` event and before the ledger, whose record has the started member `ended` with its status, time and fields and the never-started one `pending`, `provisional: false`;
  - the exact member key set the old test checked, now on the event's record;
  - a controller's `choose_answer()` reason is the record's `final.reason`.
- **The existing outer-limit cases** (`test_an_outer_limit_leaves_the_provisional_answer`, `test_an_answer_finalisation_cannot_fit_ends_the_sample_on_its_message_limit` and `test_an_outer_limit_leaves_the_latest_submission_under_last` in `tests/test_final.py`, `test_a_sample_limit_leaves_the_ledger_and_the_provisional_answer` in `tests/test_swarm_limits.py`) read the record with `swarm_result(sample)` and still find `provisional: true` with the answer so far.
- **`tests/test_record.py`** (new), the reader:
  - in a scorer, `swarm_result()` returns the record, unbounded and with `INSPECT_TRANSCRIPT_BOUNDED=1` and a solver step after the swarm that writes more than 100 events (the record evicted), as `test_metrics.py`'s bounded case does;
  - re-scoring a written log with `inspect_ai.score()` and a scorer that calls `swarm_result()` gives the same record;
  - `swarm_result(sample)` on `read_eval_log()` with and without `resolve_attachments`, for a record whose `final.reason` is longer than 100 characters (a controller's reason), gives the same record with the full reason;
  - a sample read with `exclude_fields={"events"}` raises `ValueError` naming it; a record with `version: 2` written by hand raises `ValueError`; a sample whose solver is a plain agent gives `None`;
  - two swarms in sequence in one sample, and a swarm nested in a member: the record returned is the last to end (the outer one);
  - in a solver wrapper's `finally` around `as_solver(swarm(...))`, after an outer sample limit ends the swarm, `swarm_result()` returns the provisional record (the shape ORBIT's wrapper relies on).
- **`tests/test_controller.py`**: the store-events test asserts that the swarm span's net change includes the members' writes and no `inspect_swarm:result`.
- **`tests/swarm_runtime.py`**: `run_swarm()` reads `swarm_result()` from the test sample's transcript; `after()` waits until the named members' `member_result` events are in `transcript()`.
- **Every test that read `RECORD_KEY`** (`test_background_members.py`, `test_member_sandboxes.py`, `test_metrics.py`, `test_examples.py`, `test_swarm_limits.py`, `test_final.py`) reads `swarm_result()` or `swarm_result(sample)`; the member-sandboxes scorer and `examples/proof_swarm.py` (whose slow Docker test, `tests/test_examples.py`, CI skips as today) use the helper.

## Implementation plan

One pull request, `feat!: the swarm's result record moves from the sample store to the transcript`:

- `src/inspect_swarm/_record.py`: `RECORD_TYPE`, `write_record()` writing the event, `swarm_result()` and `_is_record()`; `RECORD_KEY` removed; `FinalRecord.provisional`'s and `SwarmResultRecord`'s docstrings.
- `src/inspect_swarm/_final.py`: `settle()` no longer builds or writes a record and returns the `FinalRecord`; `finalise()` unchanged otherwise.
- `src/inspect_swarm/_controller/_control.py`: the record built and written first in `run()`'s `finally`, with `provisional=not ended`; the metrics take its chosen member; the comments at `:469-470` and `:505`.
- `src/inspect_swarm/__init__.py`: the four exports; `docs/reference/inspect_swarm.qmd`: a "Results" section.
- Docs: `docs/scoring.qmd`, `docs/restrictions.qmd`, `docs/channels.qmd`, `docs/index.qmd`, `docs/analysis.qmd`, `README.md`; `examples/proof_swarm.py`.
- Tests as in [Testing](#testing).

The scoring plan after M2 (swarm-scoring.md step 1 onward) starts from this pull request. The `SwarmEvent` switch ([swarm-event-types.md](swarm-event-types.md#implementation-plan) step 3), whenever it lands, writes the record as a `SwarmEvent` and widens `_is_record()`.

## Open questions

1. **Should the ledger and the metrics leave the store too?**
   - (a) Keep them there (this design). No member of their swarm can read them, they hold no member text, scorers read the metrics from `state.store`, and `swarm_ledger()` and `SwarmColumns` keep reading every log written so far, whatever the event types become. The docs keep two store keys.
   - (b) Move them, for one rule: the swarm keeps no record in the store. `swarm_ledger()` and `SwarmColumns` read events (with the same two-form matcher), a `swarm_metrics()` reader is added, the docs drop the store keys, and nested or earlier swarms' costs and counts leave the store.

   Recommendation: (a). The reason for the move, peers' answers, does not apply to them, and (a) changes nothing that works today.

## Not this design

- **Choosing among several swarms' records.** A sample with several swarms (in sequence, or nested) has a record for each, and `swarm_result()` returns the last. A reader for a given swarm (by span, or all of them) can come when a task needs it.
- **Result columns for `samples_df()`.** `SwarmColumns` could gain the answer's member and mode, or members started, from the record.
- **ORBIT's saved conversations.** The ORBIT port's member wrapper saves each member's final conversation to an ORBIT store key ([swarm-orbit.md P1](swarm-orbit.md#p1-members-input-and-output-no-new-api)), which peers' store tools could read while the swarm runs, the risk this design removes for the record.
- **The users-isolation record.** [swarm-sandbox-isolation.md](swarm-sandbox-isolation.md) keeps the group, users and uids it created under an `inspect_swarm` store key, which the runtime itself reads back in a later swarm. It is runtime state with no member text, so this design leaves it.
