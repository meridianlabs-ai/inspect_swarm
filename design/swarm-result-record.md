# Inspect Swarm: the result record in the transcript

## Overview

The swarm's result record (each member's state, status and time, and which member's output is the answer) moves out of the sample store and into the transcript. The runtime writes it once, when the swarm ends, as one evidence event in the swarm's span, from the synchronous `finally` that already writes the ledger and the metrics, so a limit, error or cancellation that ends the swarm still leaves it, marked `provisional`. In the same `finally` it puts the record's id, and nothing else of it, in the store under `inspect_swarm:result_id`, so the record stays tied to the store the swarm ran with, as it is today when a fork or a subtask gives a branch its own store. While the swarm runs neither exists, so no member's tool can read its peers' results. When the swarm starts, before any member can run, it writes a small start event that holds nothing about any member, so a sample that ran a swarm can be told from a plain agent's however early the swarm was interrupted.

A public helper reads it. `swarm_result(state)` takes a scorer's or a solver's `TaskState` and returns the record its store names, from the transcript of the sample being run or scored, both during the eval and when a log is re-scored. `swarm_result(sample)` does the same for a sample read from a log, for analysis. Each returns `None` when no swarm ended with that store and the sample holds no swarm's events. A read that cannot give the record of the swarm being scored raises instead of returning `None` or another swarm's record: any read of a running sample while a swarm in it is still running, such as a task scorer that a member's `react(attempts=...)` or live control scoring calls during the solve, any read of a log written while the record was in the store, and any read of a sample that holds a swarm's events but whose store names no record, as a sample recovered from a crashed eval logs no store. A solver wrapper's read right after the swarm returns still works, and a scorer that the solve can call skips the record while `state.completed` is `False`. The ledger and the metrics, which the swarm writes only after every member has ended and which hold no member text, stay in the store.

The scoring plan after M2 (the result contract, `member_scores`, the analysis helpers) builds on this home: members' outputs, answer keys and verdicts enter the record at the swarm's end, and every reader uses `swarm_result()`.

## Why

Tools that read and write the sample store are common in Inspect tasks, so anything the swarm puts in the store is readable by members' tools during the run (decision: Ransom, 2026-10-29). Today that view is each peer's state, status and time. Once the record carries members' outputs and verdict explanations ([swarm-scoring.md, What the record adds](swarm-scoring.md#what-the-record-adds), the first step of the scoring plan after M2), a store tool would show members their peers' answers: a channel between members outside the bus, with no fencing and no evidence. The user docs warn about it today ([Scoring](https://meridianlabs-ai.github.io/inspect_swarm/scoring.html#the-record-belongs-to-the-swarm)); a warning does not stop a generic store tool a task already has.

Tools that read the transcript are rare. A tool that does already sees every member's model calls and tool calls, so a record there shows such a tool nothing new. The record therefore lives in the transcript, as evidence: scorers read it in the sample, analysis reads it from the log.

The record is moved whole, not only the outputs and verdicts the scoring plan adds, so that it has one home and one reader. What stays in the store is an id that says which record belongs to which store; it tells a reader of the store nothing about any member.

## Goals and non-goals

Goals:

- The result record lives in the transcript, not the store, with the same fields and format version.
- One public reader for scorers, solver wrappers, re-scoring and analysis, correct under bounded transcripts and with condensed logs, that returns the record of the state or sample it is given, as the store does today under forks and subtasks.
- No record exists while its swarm runs.
- A read that cannot give the record of the swarm being scored fails loudly, never returning `None` or another swarm's record: a read while a swarm in the sample is still running, a read of a log that holds the old store record (decision: Ransom, 2026-10-30), and a read of a sample whose store lost the id.
- The scoring plan after M2 (swarm-scoring.md steps 1 to 7) builds on the new home, and the other designs that name the store key say where the record is.

Non-goals:

- Moving the ledger or the metrics ([The ledger and the metrics](#the-ledger-and-the-metrics) says why they stay; decision: Ransom, 2026-10-29).
- The `SwarmEvent` type itself ([swarm-event-types.md](swarm-event-types.md)); this design adds one kind to its mapping.
- Stopping a tool from reading the transcript. It is Python code in the sample, as it is today.
- Records of several swarms that share one store beyond "the last to end", as the store gives today ([Not this design](#not-this-design)).
- A compatibility reader for logs written while the record was in the store; they are refused ([Logs from before this change](#logs-from-before-this-change)).
- A record a scorer can read while its swarm runs ([Reading during a run](#reading-during-a-run)).

## Current behaviour

inspect_swarm references are to `main` at `33bf17b`; inspect_ai references are to `main` at `2f78dcb29`; inspect_swe references are to `main` at `661d6bf`. Paths are relative to each repository's root.

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

Every reader that needs the record means to read it after its swarm has exited: a scorer in final scoring, a solver wrapper around the swarm (whose state still has `completed=False` then), re-scoring or analysis. Only tests read the record on purpose while the swarm runs: the helper `after()` and `tests/test_final.py`'s `after_choice()` (`:822-830`) poll it as scheduling devices, and `tests/test_final.py` asserts the live record at `:264`, `:429-434`, `:776`, `:845` and `:847`.

But a task's scorers also run during the solve, on the sample's state as it is then, with `completed=False`:

- **`score(state)`** copies the sample's `TaskState`, keeping its store object, and runs every task scorer (`src/inspect_ai/scorer/_score.py:47`, `:65`). It is called by `react(attempts=...)` after each submission (`src/inspect_ai/agent/_react.py:340`), `deepagent(attempts=...)` through its `react` (`src/inspect_ai/agent/_deepagent/deepagent.py:235`), `basic_agent` (`src/inspect_ai/solver/_basic_agent.py:234`), the human agent's `/score` (`src/inspect_ai/agent/_human/commands/score.py:58`, `:60`) and the attempt loops of six inspect_swe agents (`src/inspect_swe/_claude_code/claude_code.py:627`, `_codex_cli/codex_cli.py:793`, `_mini_swe_agent/mini_swe_agent.py:256`, `_opencode/opencode.py:368`, `_gemini_cli/gemini_cli.py:307`, `_kimi_code/kimi_code.py:402`). A swarm's members are such callers whenever a member is one of these agents with attempts, as the docs describe (`docs/scoring.qmd:93-97`) and `tests/test_scoring_loop.py` runs.
- **Live control scoring** runs the task's scorers over a held sample's live state, binding the sample's transcript and its live store (`src/inspect_ai/_control/scoring.py:1154`, `:1183-1190`).

Today such a call of a record-dependent scorer, such as the docs' sandbox example, which has no guard, reads the live provisional record of whichever swarm last wrote the store. With the record written only at the exit, the same call would read no record during the first swarm in a sample, and the previous swarm's record during a later or a nested one. Part 2's `member_scores` returns `None` while `state.completed` is `False`, before it reads anything ([swarm-scoring.md step 1](swarm-scoring.md#answer-scored-tasks-per-member-scores)).

A spike ran a swarm of two `react(attempts=2)` members inside a solver wrapper. Each member's in-loop scorer call and the final scorer saw the transcript object and the store object the swarm started with, `completed` `False` in the loop and `True` in final scoring; the wrapper's `finally`, after the swarm returned, saw the same transcript and store with `completed=False`.

### What a transcript reader gets from Inspect

- **One transcript per sample.** Every member's events, model and tool calls included, are in the sample's transcript, which any code in the sample reaches with `transcript()`.
- **Bounded transcripts.** A sample's transcript is bounded only when `INSPECT_TRANSCRIPT_BOUNDED` is set and the eval has a sample buffer database; it then gets a history provider over that database (`src/inspect_ai/_eval/task/run.py:616-626`, `:2497-2503`). Otherwise it keeps every event. A bounded transcript keeps the newest 100 events resident (`src/inspect_ai/log/_transcript.py:84`) and evicts older ones; every event is written synchronously to the buffer as it is logged (`src/inspect_ai/_eval/task/log.py:664-667`), and the provider resolves attachments when it reads events back (`src/inspect_ai/log/_recorders/buffer/transcript_history_provider.py:204`, `:220`).
- **`TranscriptHistory.find_last(predicate)`** (`_transcript.py:461`) returns the newest matching event, searching the resident tail first and then the provider in pages of 16 growing to 256, newest first. It raises `TranscriptHistoryUnavailableError` only when evicted events cannot be served: no provider, which the run's configuration rules out, or a buffer already torn down. It is synchronous.
- **Re-scoring.** `task_state_from_sample()` rebuilds the `TaskState` and seeds a fresh, unbounded transcript with the sample's events, attachments resolved (`src/inspect_ai/_eval/score.py:449-512`, `:504-507`), so a scorer's `transcript()` holds the run's events.
- **Logs.** When a log is written, strings longer than 100 characters in an `InfoEvent`'s `data` become `attachment://<hash>` references into `EvalSample.attachments` (`src/inspect_ai/log/_condense.py:455-459`, `:1099-1104`). `read_eval_log()` resolves them only with `resolve_attachments=True`; `samples_df()` reads full samples with them resolved (`src/inspect_ai/analysis/_dataframe/samples/table.py:336`). `resolve_events_attachments(events, attachments)` (`_condense.py:931`) resolves a list of events.

A spike ran a two-member swarm on mockllm, then a solver step that wrote one `InfoEvent` with a 300-character string and 150 more events, and a scorer that called `transcript().history.find_last()` for the swarm's ledger event:

- unbounded and with `INSPECT_TRANSCRIPT_BOUNDED=1`, where the ledger event had been evicted, the scorer found it;
- `inspect_ai.score()` on the written log found it, and the long string resolved;
- `read_eval_log()` without resolving gave the long string as `attachment://...`, and with `resolve_attachments=True` the text.

### Stores, forks and subtasks

- **A store per branch, one transcript.** `fork()` deep-copies the state and runs each branch in a subtask with that copy's store (`src/inspect_ai/solver/_fork.py:60`, `:73`); `subtask()` installs its own store, a fresh one unless given one, and logs into the sample's transcript (`src/inspect_ai/util/_subtask.py:122-132`). So two forked swarms write their records to two stores and their events to one transcript, and a swarm inside a subtask writes its record to a store its parent never sees.
- **The logged store is the final state's.** `EvalSample.store` is the store of the state the solver returned (`src/inspect_ai/_eval/task/run.py:3527`), so a sample whose solver chose fork A logs A's store, A's record included.
- **No caller composes swarms this way today**: the survey above found no swarm run under `fork()` or `subtask()`. The store's per-branch record is still the behaviour a reader of today's record gets, and a reader that took the latest record in the transcript would return branch B's record to a scorer of branch A's state, or a child's record to a parent that ran no swarm.

### What the ledger and the metrics hold

Both are written once, in the `finally`, after every member of the swarm has ended, and hold names, statuses, usage, cost and counts, no member text (`src/inspect_swarm/_ledger.py`, `src/inspect_swarm/_metrics.py`). `swarm_ledger()` and `SwarmColumns` read the ledger from `sample.store` (`src/inspect_swarm/analysis/_columns.py:14-34`), and [swarm-critical-path.md](swarm-critical-path.md#where-it-is-computed) puts its metric in the metrics store entry so scorers can read it.

## Design

### At a glance

| | Today | After |
|---|---|---|
| Where | sample store, `inspect_swarm:result` | an `InfoEvent` with source `inspect_swarm` and `type: "result_record"`, in the swarm's span; its id in the store, `inspect_swarm:result_id` |
| When written | at start, at each member start and end, at `choose_answer()`, at finalisation | once, in `SwarmRun.run()`'s `finally` |
| While the swarm runs | readable, provisional | does not exist |
| After an outer limit, error or cancellation | the last write, `provisional: true` | written in the `finally`, `provisional: true` |
| Reader | `state.store["inspect_swarm:result"]`, `sample.store[...]` | `swarm_result(state)`, `swarm_result(sample)` |
| Format | `SwarmResultRecord`, `version: 1` | unchanged |
| Ledger, metrics | store and event | unchanged |

### The event

```python
# src/inspect_swarm/_record.py
RECORD_TYPE = "result_record"
RESULT_ID_KEY = "inspect_swarm:result_id"


# Only the runtime writes the record: once, when the swarm ends, in the swarm's span,
# with its id in the store the swarm ran with, which ties the record to that store
def write_record(record: SwarmResultRecord) -> None:
    id = uuid4().hex
    transcript().info(
        {"type": RECORD_TYPE, "id": id, **record.model_dump(mode="json")},
        source="inspect_swarm",
    )
    store().set(RESULT_ID_KEY, id)
```

The payload has the shape of the ledger's and the metrics' events: `type`, then the model's fields, its own `version` included, with one more key, `id`, a fresh random id from `uuid4().hex`. The models do not change, so `version` stays 1: the fields and their meanings are the same, and only where the record is kept and when it is written change. `RECORD_KEY` is removed; its value stays only as the private `_LEGACY_RECORD_KEY` that the reader refuses ([Logs from before this change](#logs-from-before-this-change)).

**Why an id in the store.** The store is what ties a record to the state it belongs to: a forked branch and a subtask each have their own ([Stores, forks and subtasks](#stores-forks-and-subtasks)). Keeping the id there keeps that association exactly, with nothing a member could learn from it: the key exists only after the swarm has ended, as the ledger and the metrics do, and an id names an event but says nothing about any member. A swarm that ends after another with the same store replaces the id, as it replaces the record today.

Under [swarm-event-types.md](swarm-event-types.md#how-inspect_swarm-writes-them) the record becomes a `SwarmEvent` with `kind="result_record"`, `member=None` and `data` the model's dump, its `version` included, plus `id`, like the ledger and the metrics; that design's mapping gains the row.

### The start event

```python
# src/inspect_swarm/_record.py
START_TYPE = "swarm_start"


# Only the runtime writes it: when a swarm starts, in the swarm's span, before any
# member can start, so a sample that ran a swarm says so however early it was cut off
def write_start() -> None:
    transcript().info({"type": START_TYPE, "version": 1}, source="inspect_swarm")
```

- **Where.** `SwarmRun.run()` calls `write_start()` immediately before `_swarm_started()` ([Reading during a run](#reading-during-a-run)), both before its `try` (`src/inspect_swarm/_controller/_control.py:422`), where the current span is the swarm's (`self._span_id`, `:404`). Members start only inside the `try`, once the members' task group exists (`:433`) and the controller calls `start()` (`:344-350`), so the start event precedes every member's span. Today the runtime writes no event of its own before a member ends: the first is a member's `member_result` (`:603-624`).
- **Why.** A sample recovered from a crashed eval keeps its events but not its store ([Samples whose store lost the id](#samples-whose-store-lost-the-id)). Without this event, a swarm interrupted after its members started and before any of them ended leaves only span events, which a plain agent's sample has too, and the reader would return `None` for a multi-member run. With it, every swarm that could have started a member has left an event the reader recognises.
- **Durability.** It is logged as the span events around it are: synchronously, and with a bounded transcript written to the buffer database as it is logged ([What a transcript reader gets](#what-a-transcript-reader-gets-from-inspect)), from which recovery rebuilds the sample's events.
- **What it holds.** Its type and a format version, nothing else: no member names, no time, no id. A tool that reads the transcript learns from it only that a swarm started, which the swarm's span already shows.
- **Under the `SwarmEvent` switch** it becomes a `SwarmEvent` with `kind="swarm_start"`, `member=None` and `data` `{"version": 1}`; [swarm-event-types.md](swarm-event-types.md#how-inspect_swarm-writes-them) lists the kind and its mapping row.

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
- **Nothing needs the live record.** Every reader that needs it means to read it after the swarm has exited ([Who reads it](#who-reads-it)), and a scorer called during the solve is refused ([Reading during a run](#reading-during-a-run)). Controllers have handles and events for live state, and the per-member `member_result` events mark each end in the transcript as it happens. The test helper `after()` waits on those events instead ([Testing](#testing)).
- **Under cancellation.** `transcript().info()` and `store().set()` are synchronous, so both writes run in the `finally` under cancellation, as the ledger's do. A sample whose process dies leaves no record, as it leaves no logged store today.
- **Log size.** The record is in the log once, in its event: no longer in the swarm's span's `StoreEvent`, the enclosing spans' or `sample.store`, which hold only its id. Long strings in it (from Part 2, members' outputs) are condensed into attachments, shared with identical text elsewhere in the sample.

### Reading it: `swarm_result()`

```python
# src/inspect_swarm/_record.py, exported from inspect_swarm with the three models
def swarm_result(source: TaskState | EvalSample) -> SwarmResultRecord | None:
    """Read the swarm's result record.

    The swarm writes its record to the transcript once, when it ends, however it ends, and its id to the store it ran with. This returns the record the store of `source` names: the last swarm to end with that store, which is the outermost of nested swarms. A forked branch or a subtask has its own store, and so its own record.

    Args:
        source: The `TaskState` of the sample being run or scored, as a scorer has it in final scoring and when a log is re-scored, or as a solver has it after the swarm has returned; or a sample read from a log, with its events and attachments.

    Returns:
        The record, or `None` when no swarm has ended with this store and the sample holds no event of a swarm.

    Raises:
        RuntimeError: A swarm in the sample is still running, so the record of the swarm being scored does not exist yet; a scorer that the solve can call returns `None` while `state.completed` is `False`.
        ValueError: The store names a record the transcript or sample does not hold (as for a sample read without its events), the record refers to attachments the sample was read without, it is one this inspect_swarm cannot read, such as a version other than 1, the sample was written by an inspect_swarm that kept the record in the store, or the sample holds a swarm's events but its store names no record (as for a sample recovered from a crashed eval, whose store was not kept).
    """
```

- **Running swarms.** For a `TaskState`, the reader first raises `RuntimeError` when a swarm in the sample is still running ([Reading during a run](#reading-during-a-run)), before it looks at the store, so the first swarm in a sample gives the error, not `None`. A sample read from a log has finished, and is not checked.
- **The id.** `source.store.get(RESULT_ID_KEY)`. When it is absent, the reader checks for the old store record ([Logs from before this change](#logs-from-before-this-change)), then for any event of a swarm ([Samples whose store lost the id](#samples-whose-store-lost-the-id)), and otherwise returns `None`. A plain agent's read therefore costs two store lookups and one scan of the transcript.
- **From a `TaskState`**: the private `_transcript_result(state.store, where)`, which runs `transcript().history.find_last(lambda e: _is_record(e) and e.data.get("id") == id)`, which reads back evicted events in a bounded transcript ([Bounded transcripts](#bounded-transcripts)), and then applies the same unresolved-attachment check as the sample path (below) before validation. The live transcript and a bounded one's provider hold events with attachments resolved, but re-scoring resolves a sample's attachments only when its mapping is not empty (`src/inspect_ai/_eval/score.py:479-481`), and a hash missing from a partial mapping stays a reference, so a re-scored state's transcript can hold `attachment://` strings. A scorer's `state` is the state the solver returned, so a fork's chosen branch reads its own record.
- **From a sample**: the newest event in `sample.events` for which the same predicate holds, then `resolve_events_attachments([event], sample.attachments)`, so a log read with or without `resolve_attachments` gives the same record. Only that one event is resolved, not the whole sample. `sample.store` is the final state's store, so the record is the chosen branch's, as in scoring.
- **Matching the id.** Both scans compare `e.data.get("id")`, never `e.data["id"]`, so an event that has the record's source and type but no `id` (written by task code, or damaged) is skipped rather than raising `KeyError` out of `find_last()`, which lets a predicate's error propagate (`src/inspect_ai/log/_transcript.py:497`), and a newer such event cannot hide an older record with the id. The envelope is not otherwise validated before the match: only the matched event's payload is, below.
- **Naming the sample.** Every `ValueError` the public reader raises names the sample and epoch: `sample.id` and `sample.epoch` for a sample, `state.sample_id` and `state.epoch` for a state, which a `TaskState` always has, re-scoring's included. The public reader passes that name to `_transcript_result()` as `where`; the private reader's default, "the transcript", serves only its store-only callers, the tests' harness, which have no `TaskState` ([Testing](#testing)).
- **Not found.** An id with no matching event is `ValueError`, naming the sample: for a sample, that it was read without its events (`exclude_fields={"events"}`); otherwise a state from another sample, or a record event that lost its `id`. Never `None`, which would read as "no swarm".
- **Unresolved attachments.** Resolution leaves a reference whose hash is not in the mapping unchanged (`src/inspect_ai/log/_condense.py:516-531`), and the model would accept it as text. So on both paths, after the event is found (and, for a sample, resolved) and before validation, `_transcript_result()` and the sample path apply one check: any string in the payload of the form `attachment://<hash>` is `ValueError` naming the sample and saying to read it, or the log being re-scored, with its attachments (`read_eval_log()` without `exclude_fields={"attachments"}`). A member's output that is itself literally such a string, with no such attachment, is refused too; Inspect's own resolution cannot tell the two apart either.
- **Validation**: `SwarmResultRecord.model_validate()` of the payload without `type` and `id`. A `ValidationError` becomes `ValueError` naming the sample, as `swarm_ledger()` does. A version other than 1 fails the model's `Literal[1]` and so raises the same way as malformed data: the reader has one failure signal, and its callers, Part 2's `member_scores` included, let it propagate rather than score around it ([swarm-scoring.md Re-scoring](swarm-scoring.md#re-scoring)). A record written by a newer inspect_swarm is read with that inspect_swarm.
- **`_is_record(event)`** is `isinstance(event, InfoEvent) and event.source == "inspect_swarm" and isinstance(event.data, dict) and event.data.get("type") == RECORD_TYPE`. When the `SwarmEvent` switch lands, it also accepts `isinstance(event, SwarmEvent) and event.kind == RECORD_TYPE`, reading `event.data`, and keeps the `InfoEvent` form, so a log reads the same whichever inspect_swarm wrote it. That is two lines, and it keeps every log with a record readable by analysis.
- **Synchronous.** ORBIT's wrapper calls it in a `finally` that must not await.
- **Shapes.** `TaskState` and `EvalSample` are the shapes every reader has ([Who reads it](#who-reads-it)); the annotation refuses others. An `AgentState` has no store, and no caller reads a record from one.
- **Exports.** `swarm_result`, `SwarmResultRecord`, `MemberResult` and `FinalRecord` are exported from `inspect_swarm` (scorers import from there; `MemberState` already is) and listed in a "Results" section of `docs/reference/inspect_swarm.qmd`. Analysis code imports the same function; `inspect_swarm.analysis` does not re-export it.

A scorer that read the store now calls the helper, after the solve:

```python
async def score(state: TaskState, target: Target) -> Score | None:
    # the record exists once the swarm has ended; a member's react(attempts=...)
    # or live control scoring calls this scorer during the solve
    if not state.completed:
        return None
    record = swarm_result(state)
    assert record is not None, "the task's solver is a swarm"
    passed = [
        m.name
        for m in record.members
        if (await sandbox(m.sandbox).exec(["sh", "/tests/run.sh"])).success
    ]
    ...
```

### Reading during a run

A read of a running sample's state while any swarm in that sample is still running raises `RuntimeError` (decision: Ransom, 2026-10-30). The record of the swarm being scored does not exist yet, so the only answers the store could give are `None`, which reads as "no swarm", or an earlier or inner swarm's record, which reads as this one's.

```python
# src/inspect_swarm/_record.py
# swarms running in each sample, by the id of the sample's transcript, which the
# running swarm's context keeps alive, so the id is not reused while counted
_running: dict[int, int] = {}


def _swarm_started() -> int:
    key = id(transcript())
    _running[key] = _running.get(key, 0) + 1
    return key


def _swarm_ended(key: int) -> None:
    if _running[key] == 1:
        del _running[key]
    else:
        _running[key] -= 1
```

- **Where the count changes.** `SwarmRun.run()` calls `key = _swarm_started()` immediately before its `try` (`src/inspect_swarm/_controller/_control.py:422`), so a swarm counts as running from before its first member starts. Its `finally`'s body is wrapped in `try: ... finally: _swarm_ended(key)`, so the count drops after the record, its id, the ledger and the metrics are written and the context variables are reset, and also when one of those writes raises; both calls are synchronous, so it drops under cancellation too.
- **The check.** `swarm_result(state)` raises when `_running.get(id(transcript()), 0)` is not zero, with the message: "swarm_result() was called while a swarm in this sample is running; a swarm's result record is written when it ends. A scorer that the solve can call (react(attempts=...), basic_agent, the human agent's /score, live control scoring) returns None while state.completed is False."
- **What it refuses.** Every case in which the store could not name the record of the swarm being scored:
  - an in-loop scorer during the first swarm in a sample, which would otherwise get `None`;
  - an in-loop scorer during a second swarm that shares the store with an earlier one, which would otherwise get the earlier swarm's record;
  - a scorer called from a member of a swarm nested in a member, or from a member of the outer swarm after the inner swarm has ended, which would otherwise get no record or the inner swarm's: the outer swarm is still running;
  - live control scoring of a held sample while a swarm in it runs, since it binds the sample's own transcript (`init_transcript(active.transcript)`, `src/inspect_ai/_control/scoring.py:1189`).
- **What it allows.** A solver wrapper's read after `as_solver(swarm(...))` has returned, as ORBIT's is, although its state has `completed=False`: the swarm has left the count. Final scoring, which runs after the solver has returned. Re-scoring, whose transcript is a fresh one in which nothing runs. `swarm_result(sample)`, which reads a finished sample.
- **Why not `state.completed`.** The wrapper's state and an in-loop scorer's both have `completed=False` (the spike in [Who reads it](#who-reads-it)), so completion cannot tell the read that must work from the reads that must fail. A scorer can still use it to decide whether to read at all, as below.
- **Why the sample's transcript, not the state's store.** `score()` copies the sample's top-level `TaskState` (`sample_state()`, `src/inspect_ai/scorer/_score.py:42-47`), not the state of a branch the swarm may run in, so under `fork()` or `subtask()` a scorer called from a member holds a store in which no swarm runs and would read `None` or an earlier record. Every in-sample caller shares the sample's transcript object: the run creates one per sample and installs it (`src/inspect_ai/_eval/task/run.py:2497-2503`), hands the same object to live control scoring (`:2602`), and `subtask()` changes only the store (`src/inspect_ai/util/_subtask.py:122`). Keyed so, the check refuses those compositions too. Its cost: when two swarms run concurrently in one sample (forked branches), a read after one has returned while the other still runs is refused. No caller composes swarms so ([Stores, forks and subtasks](#stores-forks-and-subtasks)), and the refusal is loud.
- **Process.** The count is a module-level dictionary in the eval's process, where every in-sample caller runs; nothing about a running swarm enters the store, the transcript or the log.
- **A scorer the solve can call.** A record-dependent scorer of a task whose members may score in their loop returns `None` while `state.completed` is `False`, as `member_scores` does ([swarm-scoring.md step 1](swarm-scoring.md#answer-scored-tasks-per-member-scores)) and the example above does. `score()` drops `None` results (`src/inspect_ai/scorer/_score.py:66`), so in-loop callers see only the other scorers' feedback, whatever the order. Two rules follow:
  - **Another scorer must score during the solve.** `react(attempts=...)` and `basic_agent` index the first score `score()` returns (`src/inspect_ai/agent/_react.py:342`, `src/inspect_ai/solver/_basic_agent.py:235`), so a task whose only scorer is a guarded record scorer gives them an empty list, and the attempt loop fails with `IndexError`. Such a task also lists a scorer that returns a value during the solve.
  - **That scorer comes first.** The runtime counts a member's gate checks from the task's first configured scorer's score events (`first_scorer()`, `src/inspect_swarm/_metrics.py:152-155`), and a scorer that returns `None` writes no score event (`src/inspect_ai/scorer/_score.py:66-68`), so a guarded record scorer listed first leaves every gate count at zero. The record-dependent scorer follows the one that checks submissions, as `member_scores`' ordering rule already requires for other reasons ([swarm-scoring.md Ordering](swarm-scoring.md#answer-scored-tasks-per-member-scores)).
  - **Without the guard**, the scorer's in-loop call raises the error above, which the agent's attempt loop propagates and which ends the sample with that message.

### Logs from before this change

Logs written while the record was in the store hold it under `inspect_swarm:result` in `sample.store` and have no `inspect_swarm:result_id`. The reader refuses them rather than reading them as a sample with no swarm, which would have Part 2's `member_scores` score a multi-member run as a one-member plain agent (decision: Ransom, 2026-10-30):

- **The check.** When the store has no `RESULT_ID_KEY`, the reader looks for `_LEGACY_RECORD_KEY = "inspect_swarm:result"` (private, kept in `_record.py` only for this) and, when it is present, raises `ValueError` naming the sample and epoch, from the sample or from the state: "this sample was written by an inspect_swarm that kept the swarm's result record in the sample store under inspect_swarm:result; swarm_result() does not read it: read sample.store['inspect_swarm:result'] by hand, or score the log with that inspect_swarm."
- **Both shapes.** `swarm_result(sample)` checks `sample.store`; re-scoring such a log rebuilds the `TaskState` with that store (`src/inspect_ai/_eval/score.py:495`), so `swarm_result(state)` refuses it there too, and a record-dependent scorer's re-scoring of the sample fails with the error.
- **What it does not catch.** A genuine plain agent's sample has neither key and gives `None`. A new store carries no `inspect_swarm:result` unless task code writes that key, which the docs forbid.
- **No reader.** The old store record is not parsed or validated; the error tells the reader where it is.

### Samples whose store lost the id

A sample recovered from a crashed eval keeps its events but not its store: database recovery builds the `EvalSample` without one (`src/inspect_ai/log/_recover/_reconstruct.py:286-320`), and filestore recovery writes `{}` (`src/inspect_ai/log/_recover/_stream.py:338-339`). Such a sample can hold a complete `result_record` event, or, when the process died while the swarm ran, the swarm's other events and no record, and in either case nothing ties them to the logged state. Returning `None` would have Part 2's `member_scores` score a multi-member run as a plain agent, and taking the newest record in the transcript would give up the per-store ownership ([Stores, forks and subtasks](#stores-forks-and-subtasks)). The reader refuses it:

- **The check.** When the store has neither `RESULT_ID_KEY` nor the legacy key, the reader looks for any event the runtime writes, `_is_swarm_event(event)`: an `InfoEvent` with source `inspect_swarm` (the start event, the record, `final`, `member_result`, the ledger and the metrics among them), and from the `SwarmEvent` switch a `SwarmEvent` too. The [start event](#the-start-event) makes this hold from before a swarm's first member starts, so a swarm interrupted with its members running and none ended is refused like one that left its record. For a sample it scans `sample.events`; for a state, `transcript().history.find_last(_is_swarm_event)`. When one is found it raises `ValueError` naming the sample and epoch: "this sample holds events of an inspect_swarm swarm, but its store names no result record: the store was not kept (a sample recovered from a crashed eval logs an empty store), or the swarm ran with another store (a forked branch or a subtask); swarm_result() cannot tell which record, if any, belongs to it."
- **Both shapes.** Re-scoring a recovered sample rebuilds the state with its empty store and seeds the transcript with its events (`src/inspect_ai/_eval/score.py:495`, `:504-507`), so `swarm_result(state)` refuses it as `swarm_result(sample)` does, and a record-dependent scorer's re-scoring of the sample fails with the error.
- **What else it refuses.** A state whose own store ran no swarm while a forked sibling or a subtask in the sample did: a composition no caller uses ([Stores, forks and subtasks](#stores-forks-and-subtasks)), refused rather than read as a plain agent.
- **What it does not catch.** A plain agent's sample holds no event from the runtime, and gives `None`. A process that died after the swarm's span began and before its start event was logged had started no member, so no member's output can be lost; the sample reads as a plain agent's, which it is in effect. A recovered sample from an inspect_swarm that wrote no start event, interrupted before any member ended, has nothing to find either, and gives `None`; that log's store record was lost in recovery in any case. A recovered sample read without its events (`exclude_fields={"events"}`) has neither the id nor an event to find, and gives `None`: without its events, such a sample cannot be told from a plain agent's.
- **Not restored.** The reader does not rebuild the store from the sample's `StoreEvent`s; restoring a recovered sample's store is Inspect's recovery's concern, outside this design.

### Bounded transcripts

- **Live.** A bounded transcript always has a history provider ([What a transcript reader gets](#what-a-transcript-reader-gets-from-inspect)), so `find_last()` finds the record however many events came after it. The record is written before the ledger, the metrics and any scorer, so the search reads the resident tail, and pages back through the provider only when more than 100 events (a later solver step's, a later fork branch's, or earlier scorers' model calls) followed it.
- **A state with no record** (a plain agent scored by Part 2's `member_scores`) has no id in its store, so the reader searches the whole transcript once for any event of a swarm ([Samples whose store lost the id](#samples-whose-store-lost-the-id)) and finds none. Unbounded, that is a scan of the events in memory; bounded, `find_last()` pages the sample's evicted history back from the buffer database, once per call. A state with a record never makes this search.
- **Re-scoring and logs** hold every event: re-scoring seeds an unbounded transcript, and a log's `events` are the full history.
- **`TranscriptHistoryUnavailableError`** propagates. It arises only when the buffer has been torn down, which does not happen while the sample runs or is scored.

### Who may write it

Only the runtime writes the record and its id. Task code must not write `inspect_swarm:result_id` or an `InfoEvent` with source `inspect_swarm`; the docs say so where they said task code must not write the store key.

### The ledger and the metrics

They stay in the store, and as events, unchanged (decision: Ransom, 2026-10-29: "Since they are written after the members all exit they seem ok."):

- **The stated risk does not apply.** They are written once, after every member of their swarm has ended, so no member of that swarm can read them, and they hold no member text.
- **Scorers and analysis read them there.** `state.store` gives scorers the metrics (swarm-critical-path.md's intent); `swarm_ledger()` and `SwarmColumns` read every log written so far from `sample.store`, independent of the `SwarmEvent` switch.
- **What remains readable.** A swarm nested in a member writes its ledger, metrics and record id when it ends, and the outer swarm's other members can then read those through the store: the inner members' names, usage, cost and counts, and an id. A swarm that ran earlier in the sample leaves the same to a later swarm's members. Neither is a peer's answer.

### The docs

The user docs state the result:

- `docs/scoring.qmd`, "The result record": the record is an evidence event the swarm writes when it ends; read it with `swarm_result(state)` in a scorer and `swarm_result(sample)` from a log, which needs the sample's events and attachments; the JSON shown is the record (the event adds `"type"` and `"id"`); `provisional: true` when a limit, error or cancellation ended the swarm before it finalised. "The record belongs to the swarm" becomes: only the swarm writes it; there is no record while the swarm runs; a tool that reads the transcript sees every member's events anyway, a channel between members the swarm records no evidence of. The per-member sandbox example uses `swarm_result(state)` and returns `None` while `state.completed` is `False`, as [Reading it](#reading-it-swarm_result) shows. "Checks during the run" adds that a scorer the solve can call, under `react(attempts=...)` and the other attempt loops or live control scoring, cannot read the record then: `swarm_result()` raises while a swarm in the sample runs, so such a scorer returns `None` until `state.completed` is `True`. Since `score()` drops `None`, the task also needs a scorer that returns a value during the solve (an attempt loop indexes the first score returned and fails on an empty list), listed first so that the swarm's gate counts, which follow the first configured scorer, count its checks.
- `docs/restrictions.qmd`: "The store keys" lists `inspect_swarm:result_id`, `inspect_swarm:ledger` and `inspect_swarm:metrics`, written when the swarm ends, which task code must not write; the "What the log records" bullet drops "the result record read while the swarm runs" and names a tool that reads the transcript instead.
- `docs/channels.qmd:47`: the bullet becomes a tool that reads the transcript, which sees the other members' model and tool calls.
- `docs/index.qmd` and `README.md`: the result record in the transcript, read with `swarm_result()`, and the ledger and metrics in the store.
- `docs/analysis.qmd:13`: the record is read with `swarm_result(sample)`.
- `examples/proof_swarm.py`'s scorer uses `swarm_result(state)`.

### Changes to the other designs

Made in this design's pull request:

- **[swarm-scoring.md](swarm-scoring.md).** Part 1's [What the swarm records](swarm-scoring.md#what-the-swarm-records) describes the event, the single write and `swarm_result()`, and drops "Why the store" (its premise, that re-scoring rebuilds only the store, no longer holds) and the readable-by-member-code bullet. Its testing and plan say the same. Part 2 builds on the event: [What the record adds](swarm-scoring.md#what-the-record-adds) has the added fields enter the record at the swarm's end; `member_scores`, re-scoring, `attempt_rows`, the logs table, Compatibility and Security read and describe the event; and the plan after M2 depends on this design's pull request.
- **[swarm-orbit.md](swarm-orbit.md).** The ORBIT wrapper reads the record with `swarm_result(state)` in its synchronous `finally`.
- **[swarm-chosen-names.md](swarm-chosen-names.md)**, **[swarm-implementation-plan.md](swarm-implementation-plan.md)** and **[swarm-lean-open-problems.md](swarm-lean-open-problems.md)**: the store key becomes the record, with a link here, the plan's documentation steps state this design's rule, and swarm-chosen-names.md's record test names the test that replaces the one it extended.
- **[swarm-api.md](swarm-api.md)**, **[swarm-background-members.md](swarm-background-members.md)** and **[swarm.md](swarm.md)**: `member_scores` reads the record with `swarm_result(state)`, and in the loop never reads it; a member's end sets its run record's state, status and time, writes its `member_result` event and updates the provisional answer, and the result record is written only when the swarm ends; the store-events test expects the record's id in the swarm span's net change, not the record.
- **[swarm-event-types.md](swarm-event-types.md).** The `SwarmEvent` kinds and the mapping table gain `swarm_start` and `result_record`, the latter with its `id`, and the reader keeps both forms ([Reading it](#reading-it-swarm_result)).

## Alternatives considered

**Keep the record in the store, written only at the end.** Removes the live view, but a swarm nested in a member, or one that ran earlier in the sample, would still leave records with outputs that the outer or later swarm's members read through ordinary store tools. The transcript holds the same facts at no new exposure. Rejected.

**Keep writing the record as an event at every change**, so a reader during the run sees the provisional state. No reader needs it live ([Who reads it](#who-reads-it)); a scorer called during the solve is refused instead ([Reading during a run](#reading-during-a-run)). Each write would copy every member's output into the log once Part 2 adds them, and a live record is exactly the view this design removes. Rejected.

**Rebuild the record from the `member_result` and `final` events** instead of writing it. One fewer event, but those events lack fields the record has (role, `choose_name`, gate counts, and from Part 2 outputs, keys and verdicts), and adding them would put member text in per-member events written during the run. Rejected.

**Keep the record in memory for in-sample readers** (a context variable the runtime sets) and write it to the transcript for logs. Two sources with one meaning, and a context-variable reader is as reachable from a member's tool as the store. Rejected.

**Two readers**, one for the running sample in `inspect_swarm` and one for logs in `inspect_swarm.analysis`. They would share all but where the store and events come from. One function over a state or a sample is simpler to document and test.

**Read the latest record in the transcript**, with no id in the store. Simpler, and it needs no store key. But the transcript is shared by forked branches and subtasks while their stores are not ([Stores, forks and subtasks](#stores-forks-and-subtasks)): a scorer of the branch the solver chose would get the record of whichever branch ended last, and a parent that ran no swarm would get a child's. With no id, the reader could refuse a sample with several records, but not tell a parent that ran no swarm from one that did, and it would refuse a sample whose swarms ran in sequence or nested, which the id reads. Rejected.

**Use the event's own `uuid` as the id.** No extra payload key, but `transcript().info()` does not return the event, and building the `InfoEvent` to know its uuid means the private `transcript()._event()`. A payload id is explicit and survives the `SwarmEvent` switch unchanged. Rejected.

**Move the ledger and the metrics too.** One rule, "the swarm keeps nothing in the store", but they are written after every member has exited and hold no member text, and their readers use the store. Rejected (decision: Ransom, 2026-10-29).

**Read old logs' store records in `swarm_result(sample)`.** A fallback to `sample.store["inspect_swarm:result"]` for logs written before this change. No analysis code reads the record from logs yet, and the format is experimental ([swarm-implementation-plan.md Decisions](swarm-implementation-plan.md#decisions), item 6). Not included; such logs are refused and read from `sample.store` by hand (decision: Ransom, 2026-10-30).

**Return `None` for old logs.** No check, and no old key kept. But `None` means "no swarm", and Part 2's `member_scores` would then score the run's selected output as a one-member plain agent, reporting `best_member` and `mean_member` for a multi-member run whose members' outputs the log does not hold. Rejected.

**Return `None` during a run**, or the record the store names, as a store read would. `None` reads as "no swarm" and an earlier or inner swarm's record reads as this one's, so a record-dependent scorer would grade the wrong roster or none. Rejected (decision: Ransom, 2026-10-30).

**Refuse when `state.completed` is `False`.** One field, no count. But a solver wrapper's state after the swarm has returned is not completed either, and that read must work. Rejected.

**Count running swarms by the state's store.** Ties the check to the state a scorer holds, and lets one forked branch read its record while another branch's swarm still runs. But `score()` gives a member's scorer the sample's top-level state, so under `fork()` or `subtask()` it would read `None` or an earlier record with no swarm counted in its store. The sample's transcript is shared by every caller in the sample, and the concurrent-fork read it refuses has no caller. Rejected.

**Mark a running swarm in the store or the transcript**, so the check reads the sample's own data. A store marker is one more key members' tools can see, and the log keeps it if the process dies; a transcript check (a swarm span begun and not ended) scans the history on every read, through the provider when the begin has been evicted. The count is synchronous, process-local and leaves nothing in the log. Rejected.

**Recognise an interrupted swarm from its span events**, with no start event. Members' spans are ordinary agent spans (`span(name, type=AGENT_SPAN_TYPE)`, `src/inspect_swarm/_member.py:526`) named after the members, and the swarm's own span is the one its caller opens, named after the agent; a plain agent's sample has spans of the same kinds, so no rule over spans tells them apart without guessing from names. Rejected for one event the runtime owns.

**Write the record's id to the store at the start.** It would tie the record to its store before the swarm ends, but recovery loses the whole store, so it does nothing for the case the start event exists for. Rejected.

## Compatibility and migration

- **Eval logs.** New logs have no `inspect_swarm:result` store entry; they have one `InfoEvent` with `type: "result_record"` per swarm, whose payload is the record's format, `version: 1`, unchanged, plus `type` and `id`, and the last such id in `inspect_swarm:result_id`, and one `swarm_start` `InfoEvent` per swarm at its start. The viewer shows each as an info event in the swarm's span. Old logs keep their store entry; `swarm_result()` raises `ValueError` for them, from a log and when they are re-scored, so a record-dependent scorer fails on them rather than scoring them as plain agents ([Logs from before this change](#logs-from-before-this-change)).
- **Public API.** Code that reads `state.store["inspect_swarm:result"]` or `sample.store[...]` finds nothing and must call `swarm_result()`. The only such code is this repository's docs, example and tests. New exports: `swarm_result`, `SwarmResultRecord`, `MemberResult`, `FinalRecord`. A new store key, `inspect_swarm:result_id`. The pull request is a breaking change (`feat!:`), so the release notes name it.
- **Timing.** A reader that polled the store during the run (only the tests) has no record until the swarm ends. A task scorer that reads the record and is called during the solve (an attempt loop, the human agent's `/score`, live control scoring) read the provisional record before; it now gets `RuntimeError` unless it returns `None` while `state.completed` is `False` ([Reading during a run](#reading-during-a-run)). The only such scorer in this repository is the docs' example, which gains the guard.
- **Recovered logs.** Inspect's recovery of a crashed eval logs samples without their store, so a recovered swarm sample has lost today's store record as well as, after this change, the id. Today's readers read it as no record; `swarm_result()` raises `ValueError` for it when it holds a swarm's events, which a new log's swarm sample does from the start event on ([Samples whose store lost the id](#samples-whose-store-lost-the-id)).
- **Store events.** The swarm's span's net `StoreEvent` includes the id, not the record.
- **`SwarmEvent` switch.** When it lands, the record is written as a `SwarmEvent` and the reader accepts both forms, so logs from before and after it read the same.
- **The ledger and the metrics**, their store entries, `swarm_ledger()` and `SwarmColumns`: unchanged.
- **inspect_ai.** No change. The reader uses `TranscriptHistory.find_last()`, `InfoEvent`, `TaskState` and `EvalSample` (public), and `resolve_events_attachments()` (private, `inspect_ai.log._condense`), as inspect_swarm may use internals.

## Security

- **What reaches the record.** Member names (the eval author's), statuses, times, sandbox names, the controller's `choose_answer()` reason (controller code), and from Part 2 members' outputs (model output), answer keys and verdict explanations (task code over model output). It is JSON data in an `InfoEvent`, never Markdown, and no prompt the harness composes contains it. The start event holds only its type and version.
- **Who can read it, when.** Nobody during the swarm's run, since neither it nor its id exists, and `swarm_result(state)` refuses every read while a swarm in the sample runs, so not even an earlier swarm's record reaches a scorer called from a member's loop. The running count holds transcript ids and counts, nothing from a member. After the swarm ends, any code in the sample, through `transcript()`. A member of a later swarm in the sample, or of an outer swarm after a nested one ends, can read the earlier swarm's record with a tool that reads the transcript; such a tool sees that swarm's members' model calls anyway, so the record adds nothing to what it could read.
- **What the store still exposes.** The record's id, the ledger and the metrics, after their swarm ends: no member text ([The ledger and the metrics](#the-ledger-and-the-metrics)).
- **Forged records.** Task code that writes a `result_record` `InfoEvent` and points `inspect_swarm:result_id` at it after the swarm would be read as the record, as task code writing the store key would be today. The docs forbid it; models cannot write events or the store.
- **Reading logs.** `swarm_result(sample)` resolves only `attachment://` references found in the record's own event against the sample's own attachments; it and `swarm_result(state)` in re-scoring refuse any reference left unresolved, and validate the result with the pydantic model, so a malformed or partly loaded record raises `ValueError` rather than being used.

## Testing

Mockllm, no network or Docker unless marked, asyncio and trio for async tests.

- **`tests/test_final.py`, the record's write.** `test_the_record_is_written_once_when_the_swarm_ends` replaces `test_the_record_is_written_at_start_and_on_each_start_and_end`:
  - while the swarm runs, the controller, after a start and after an end, and a member's tool find no `result_record` event in the transcript and no `inspect_swarm:result_id` in the store;
  - after it ends, exactly one `result_record` event, in the swarm's span, after the `final` event and before the ledger, whose id the store holds, and whose record has the started member `ended` with its status, time and fields and the never-started one `pending`, `provisional: false`;
  - the exact member key set the old test checked, now on the event's record;
  - a controller's `choose_answer()` reason is the record's `final.reason`.
- **`tests/test_final.py`, the provisional answer during the run.** What these tests observe live is the swarm's `AgentState.output`, which still changes at each submission and choice; what they observe of the record moves after the exit:
  - `test_the_provisional_answer_is_set_at_the_first_submission` (`:254-280`) keeps its live `state.output.completion == "quick"`, checks the members' live states on the controller's handles (`quick` ended, `slow` running) instead of a live record, and checks the record's `final` after the exit;
  - `test_a_controller_chooses_the_answer_in_place_of_its_final_rule` (`:768-819`) holds its own `AgentState`, as the test above does, and asserts `state.output.completion == "c"` before the choice in place of the live record's `last` and `c`; the exit record's assertions stay;
  - `after_choice()` waits on an `anyio.Event` that `test_a_controllers_choice_is_the_provisional_answer_at_once` (`:834`) sets right after `choose_answer()`, instead of polling the record, so member `c` still runs only after the choice; the test's live checks at `:845` and `:847` keep `state.output.completion` and drop the record, whose `final` it checks after the exit.
- **The existing outer-limit cases** (`test_an_outer_limit_leaves_the_provisional_answer`, `test_an_answer_finalisation_cannot_fit_ends_the_sample_on_its_message_limit` and `test_an_outer_limit_leaves_the_latest_submission_under_last` in `tests/test_final.py`, `test_a_sample_limit_leaves_the_ledger_and_the_provisional_answer` in `tests/test_swarm_limits.py`) read the record with `swarm_result(sample)` and still find `provisional: true` with the answer so far.
- **`tests/test_record.py`** (new), the reader:
  - in a scorer, `swarm_result(state)` returns the record, unbounded and with `INSPECT_TRANSCRIPT_BOUNDED=1` and a solver step after the swarm that writes more than 100 events (the record evicted), as `test_metrics.py`'s bounded case does;
  - re-scoring a written log with `inspect_ai.score()` and a scorer that calls `swarm_result(state)` gives the same record;
  - `swarm_result(sample)` on `read_eval_log()` with and without `resolve_attachments`, for a record whose `final.reason` is longer than 100 characters (a controller's reason), gives the same record with the full reason;
  - the same sample read with `exclude_fields={"attachments"}` (an empty mapping), and with its `attachments` replaced by a mapping that lacks the reason's hash, raises `ValueError` naming the sample; re-scoring each of those two samples with `inspect_ai.score()` (re-scoring leaves the reference in the seeded transcript for both) and a scorer that calls `swarm_result(state)` raises the same error, never a record whose reason is the reference;
  - a sample read with `exclude_fields={"events"}` raises `ValueError` naming it; a record with `version: 2` and a malformed version-1 record, written by hand, raise `ValueError` naming the sample from `swarm_result(sample)`, and from `swarm_result(state)` in a scorer, in final scoring and on re-score, so the sample's scoring fails with that error; a sample and a state whose solver is a plain agent give `None`;
  - **malformed envelopes**: a `result_record` event without `id`, added by hand after the swarm's record, does not hide the record from `swarm_result(state)` or `swarm_result(sample)`; and when the store's id names only such an event (the record's own event written without its `id`), both raise the not-found `ValueError` naming the sample, never `KeyError`;
  - **a log from before this change**: a sample whose store holds a two-member version-1 record under `inspect_swarm:result` and no `inspect_swarm:result_id` (a solver that writes that key by hand stands in for the old runtime): `swarm_result(state)` in final scoring, `swarm_result(state)` when the written log is re-scored with `inspect_ai.score()`, and `swarm_result(sample)` each raise `ValueError` naming the sample and the old key; the plain-agent cases above still give `None`;
  - **a sample whose store lost the id**: the written log's two-member swarm sample with its `store` replaced by `{}`, as both of Inspect's recovery paths leave it, and the same sample with its `result_record` event also removed (a process that died while the swarm ran, after a member ended): `swarm_result(sample)`, and `swarm_result(state)` when a log holding that sample is re-scored with `inspect_ai.score()`, raise `ValueError` naming the sample, never `None`; a fork whose chosen branch is a plain agent while the other branch ran a swarm raises the same from the scorer's `swarm_result(state)`;
  - **a swarm interrupted before any member ended**: a two-member swarm with no channels whose members wait on an `anyio.Event` the test never sets, run with `inspect_ai.agent.run()` in the test sample's context; once both members have started, the test snapshots the transcript's events (span events and the start event, with no `member_result`, `final` or record) and builds an `EvalSample` from them with an empty store, as recovery leaves it, before cancelling the swarm. `swarm_result(sample)`, and `swarm_result(state)` when a log holding that sample is re-scored with `inspect_ai.score()`, raise the lost-id `ValueError`; with the start event removed from the snapshot, both give `None`, which is what the start event prevents;
  - **the start event**: exactly one per swarm, in the swarm's span, with payload `{"type": "swarm_start", "version": 1}`, before the `SpanBeginEvent` of every member, in a swarm whose members start at once and one whose controller starts them later;
  - every `ValueError` above names the sample's id and epoch, from `swarm_result(state)` (live and re-scored) as from `swarm_result(sample)`, never only "the transcript";
  - **reads during a run**, with a probe scorer that records what each call of `swarm_result(state)` gives (the record, `None`, or the exception) and returns a score, listed after a scorer that always passes, and members that are `react(attempts=2)` agents, each of which scores its first submission in its loop:
    - the first swarm in a sample: every in-loop call raises `RuntimeError`; final scoring reads the record;
    - two swarms in sequence sharing the store: the second swarm's in-loop calls raise, never returning the first swarm's record; final scoring reads the second's;
    - a swarm nested in a member of an outer swarm: calls from the inner swarm's members raise, and so do calls from an outer member after the inner swarm has ended; final scoring reads the outer swarm's record;
    - the docs' guarded scorer in the probe's place returns `None` in the loop, so the attempt loop sees only the submission scorer's result, and its score in final scoring; listed first instead, the attempt loop sees the same result and the members' gate counts are zero, and listed alone, the attempt loop fails with `IndexError`, which the docs' rule prevents;
    - live control scoring: a controller that waits until a member has started drives inspect_ai's live scoring of the held sample (the private `_score_live_sample()` over the sample's `ActiveSample`, as inspect_ai's `tests/_control/test_score.py` does, under the same backend restriction those tests use), and the probe's call raises `RuntimeError`;
    - the count drops however a swarm ends: after a swarm that ends normally, one ended by an outer sample limit and one cancelled by a solver that cancels its task group, a later `swarm_result(state)` in the same sample returns that swarm's record rather than raising;
  - **forks**: a solver that runs `fork(state, [swarm_a, swarm_b])` with B ending last and returns A's state: the scorer's `swarm_result(state)` and the log's `swarm_result(sample)` are A's record, and B's state reads B's;
  - **subtasks**: a swarm run inside `subtask()` makes the parent's `swarm_result(state)` raise the lost-id `ValueError` ([Samples whose store lost the id](#samples-whose-store-lost-the-id)), never `None` or the child's record, and `_transcript_result(store())` inside the subtask, after the child swarm, reads the child's record;
  - two swarms in sequence with one store, and a swarm nested in a member: the record returned is the last to end (the outer one);
  - in a solver wrapper's `finally` around `as_solver(swarm(...))`, after the swarm returns normally and after an outer sample limit ends it, `swarm_result(state)` returns the record (provisional after the limit), with `state.completed` still `False` (the shape ORBIT's wrapper relies on).
- **`tests/test_swarm_limits.py`**: `test_the_ledger_is_written_after_the_final_step` (`:1077-1087`) collects the swarm's `InfoEvent` kinds, the new `result_record` among them, so its assertion becomes `kinds[-4:] == ["final", "result_record", "ledger", "metrics"]`, keeping the ledger after the final step.
- **`tests/test_controller.py`**: the store-events test asserts that the swarm span's net change includes the members' writes and `inspect_swarm:result_id`, and no `inspect_swarm:result`.
- **`tests/swarm_runtime.py`**: `run_swarm()`, whose test sample has a store and a transcript but no `TaskState`, reads the record with `_transcript_result(store())`; `after()` waits until the named members' `member_result` events are in `transcript()`.
- **Direct-agent tests without a `TaskState`.** `tests/test_final.py`'s `_record()` helper (`:76-77`) and the post-exit reads in `tests/test_metrics.py` (`:463`, `:509`) run the swarm as an agent in the test `sample()` context, with an `AgentState`, a store and a transcript but no `TaskState` or `EvalSample`; they read with `_transcript_result(store())`, which `_record()` becomes, as `run_swarm()` does. The public reader keeps its two shapes.
- **Every other test that read `RECORD_KEY`** (`test_background_members.py`, `test_member_sandboxes.py`, the logged-sample reads in `test_metrics.py` and `test_final.py`, `test_examples.py`, `test_swarm_limits.py`) reads `swarm_result(state)`, `swarm_result(sample)` or `run_swarm()`'s record after the swarm has exited; the member-sandboxes scorer and `examples/proof_swarm.py` (whose slow Docker test, `tests/test_examples.py`, CI skips as today) use the helper.

## Implementation plan

One pull request, `feat!: the swarm's result record moves from the sample store to the transcript`:

- `src/inspect_swarm/_record.py`: `RECORD_TYPE`, `RESULT_ID_KEY`, `write_record()` writing the event and the id, `START_TYPE` and `write_start()`, `swarm_result()`, `_transcript_result()`, `_is_record()` and `_is_swarm_event()`; the running count, `_swarm_started()` and `_swarm_ended()`; `RECORD_KEY` removed and `_LEGACY_RECORD_KEY` refused; `FinalRecord.provisional`'s and `SwarmResultRecord`'s docstrings.
- `src/inspect_swarm/_final.py`: `settle()` no longer builds or writes a record and returns the `FinalRecord`; `finalise()` unchanged otherwise.
- `src/inspect_swarm/_controller/_control.py`: `write_start()` and then `_swarm_started()` before `run()`'s `try` and `_swarm_ended()` in a `finally` around its `finally`'s body; the record built and written first in `run()`'s `finally`, with `provisional=not ended`; the metrics take its chosen member; the comments at `:469-470` and `:505`.
- `src/inspect_swarm/__init__.py`: the four exports; `docs/reference/inspect_swarm.qmd`: a "Results" section.
- Docs: `docs/scoring.qmd`, `docs/restrictions.qmd`, `docs/channels.qmd`, `docs/index.qmd`, `docs/analysis.qmd`, `README.md`; `examples/proof_swarm.py`.
- Tests as in [Testing](#testing).

The scoring plan after M2 (swarm-scoring.md step 1 onward) starts from this pull request. The `SwarmEvent` switch ([swarm-event-types.md](swarm-event-types.md#implementation-plan) step 3), whenever it lands, writes the record as a `SwarmEvent` and widens `_is_record()`.

## Open questions

None.

## Not this design

- **Choosing among several swarms' records.** Swarms that share a store (in sequence, or nested) each leave a record, and `swarm_result()` returns the last to end. A reader for a given swarm (by span, or all of them) can come when a task needs it.
- **Result columns for `samples_df()`.** `SwarmColumns` could gain the answer's member and mode, or members started, from the record.
- **ORBIT's saved conversations.** The ORBIT port's member wrapper saves each member's final conversation to an ORBIT store key ([swarm-orbit.md P1](swarm-orbit.md#p1-members-input-and-output-no-new-api)), which peers' store tools could read while the swarm runs, the risk this design removes for the record.
- **The users-isolation record.** [swarm-sandbox-isolation.md](swarm-sandbox-isolation.md) keeps the group, users and uids it created under an `inspect_swarm` store key, which the runtime itself reads back in a later swarm. It is runtime state with no member text, so this design leaves it.
