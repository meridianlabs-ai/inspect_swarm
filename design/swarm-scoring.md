# Inspect Swarm: scoring

Status: proposed, 2026-10-08; restructured the same day after Ransom moved scoring work beyond M1's final answer to after M2. Issue: none. Author: agent (Claude), reviewed by Codex; see the PR.

## Overview

A swarm is scored the way any agent is. It is the task's solver, so the task's own scorers grade what it leaves behind: its answer, or the shared environment its members worked in. This design says what the swarm must leave behind for that to work in M1, and sketches what could be added after M2 for richer comparisons.

**In M1 the swarm adds no scorer.**

- The swarm's answer is the first submission: the member that submitted earliest wins, and a tie goes to the member listed first. Answer scorers such as `match()` or a model grader read it as they read any agent's output.
- Tasks scored on the environment, such as a repository the members edited together, are scored on the shared sandbox after every member has stopped.
- If a limit ends the sample early, a submission already made still counts. If no member submitted, the output is empty, rather than one member's unfinished work.
- The swarm records what each member did (its state, whether and when it submitted, its output) and which answer it chose and why, so analyses can look past the single score.
- Sample usage includes the scorers' model calls, as in every Inspect eval, so arms are compared with grading included.

**After M2, optionally:**

- A task can declare what a result is, so the swarm can check submissions with a verifier and choose its answer by verifying, then voting, then taking the first; or combine answers with a model call.
- Every member's submission can be scored, giving team-level numbers (did any member solve it, how did the average member do) to compare with best-of-k and epochs.
- Helpers run the same agent as a swarm of one for a baseline, select among attempts, and analyse arms side by side.
- Environment-scored tasks can get per-member artifacts, and solve cost can be separated from scoring cost.

A deeper dive on one topic of [swarm.md](swarm.md): how a swarm's results are scored. It details [Results and scoring](swarm.md#results-and-scoring-a-task-owned-contract) (the task-owned result contract) and the final-answer modes of [Controller](swarm.md#controller-topology-termination-final-answer). It covers answer-scored and shared-artifact tasks, per-member submissions, the comparisons with epochs (team@k, best@k, pass@k), the final-answer chain, verifiers, `synthesize`, per-member artifacts in a shared sandbox, what inspect_swarm ships for scoring, how all of it appears in logs and eval sets, and how a task opts in.

**How it is organised.** Ransom (2026-10-08): "let's limit M1 to first. Defer the cost boundary - already an issue with existing inspect evals. [...] M1 work at the top of -scoring, everything that is past M2 later in the doc."

- **[Part 1: M1](#part-1-m1)** is what M1 builds. The final answer is `first`. There is a provisional answer, an empty output when nothing is submitted, and a small per-member record. Existing task scorers keep working, unchanged, with the swarm as the agent; M1 adds no scorer.
- **[Part 2: after M2](#part-2-after-m2-optional)** is optional follow-up work, in any order with the rest of swarm.md's menu:
  - the task's result contract;
  - the verify, vote, first chain, with verifiers and `synthesize`;
  - per-member scores (team@k, mean member) and `best_at(k)`;
  - baselines as a swarm of one, select@k and the analysis helper;
  - per-member artifacts;
  - the deferred solve/scoring cost boundary.
- Alternatives, Compatibility, Security, Open questions and Not this design cover both parts.

It respects the decisions recorded in swarm.md (Ransom, 2026-10-07): Python 3.11+, inspect_ai internals may be used, limits stay soft, members share the sample sandbox by default, M1 then M2 then the rest in any order. Sibling deep dives cover limits and the ledger, ORBIT alignment and inter-agent communication. This document refers to them and does not design them.

It builds on two designs merged on 2026-10-08 and uses their shapes:

- [swarm-api.md](swarm-api.md), the `swarm()` API. The swarm's fixed **runtime**, not the controller, keeps the member record, the provisional answer and finalisation; after M2 it also verifies submissions. After M2 the final-answer chain is the controller's `final=` parameter.
- [swarm-communication.md](swarm-communication.md). Its evidence payloads are the shape this design's events follow, and after M2 `synthesize` follows its fencing rules.

Where this document says "the runtime", it means swarm-api.md's runtime.

Code references are to inspect_ai `main` at `215cf087` (2026-10-08) and inspect_evals `main` at `a73e6d99` (2026-10-08). Paths are relative to each repository's root. Spikes ran against the inspect_ai this repository's lockfile installs (`0.3.278.dev1+gfccfb298e`), whose registry and scoring code is unchanged from `215cf087`.

## Part 1: M1

### Why M1 needs little from scoring

Ransom asked (2026-10-08): "Is there anything in the design that is needed before then? Won't existing scorers continue to work with the swarm as the agent?" They do. A swarm, like any solver, must return an answer in `TaskState.output` and leave the shared sandbox in the state its members made. M1 adds one thing beyond that: a record of what each member submitted, which swarm.md's M1 requires.

- **Existing scorers work unchanged.** `swarm()` is an `@agent` that a task uses as its solver, through `as_solver()`. Inspect runs the task's scorers after the solver, on the same `TaskState`, inside the sample's sandbox context ([Where scoring runs](#where-scoring-runs)).
  - An answer scorer (`match()`, `expression_equivalence()`, `model_graded_qa()`) reads `TaskState.output`, which holds the swarm's final answer.
  - A shared-artifact scorer (SWE-bench's) reads the environment the drained swarm left.
  - Neither needs to know that the solver was a swarm. Nothing in Part 2 is needed for this: not per-member scores, team@k, `best_at`, `baseline()`, select@k or the analysis helper.
- **Sample usage includes scoring, in every Inspect eval.** `EvalSample.model_usage` is `sample_model_usage()` read when the sample is logged, after scoring (`src/inspect_ai/_eval/task/run.py:3476`), so a model-graded scorer's calls are in it. A spike confirmed it. A solver that made no model call, scored by `model_graded_qa` on mockllm, gave the sample 223 tokens of `model_usage`, all of them the grader's.
  - Every arm grades its own output in the same way: a swarm arm and a single-agent arm grade once per sample, and an epochs arm once per epoch, as epochs always do.
  - M1 compares arms on sample usage with grading included, as existing Inspect evals do. It does not separate solve usage from scoring usage (decision: Ransom, 2026-10-08).
  - The asymmetry that would need that boundary comes from per-member scoring, which grades every member, so the boundary is deferred with it ([The cost boundary](#the-cost-boundary-deferred)).

### Current behaviour M1 relies on

In inspect_ai unless noted.

#### Where scoring runs

- Scorers run once per sample after the solver, sequentially, in task order, inside a `scorers` span (`src/inspect_ai/_eval/task/run.py:3059-3115`). `state.completed` is set to `True` first (`run.py:3025`).
- They run inside the sample's sandbox context, so `sandbox()` still reaches the environment the agents left (`run.py:2497-2512`).
- Each scorer gets the same `TaskState` object, and its result is added to `state.scores` after it returns. A scorer that writes its own entry there is an error (`run.py:3082-3087`).
- Scoring gets half the sample's time limit (`run.py:3034`).
- Scores a solver writes into `state.scores` before scoring are also recorded as sample scores (`run.py:3039`, `:3118-3135`).

#### Agent output and member state

- `react()` updates the `AgentState` it was given in place: each generation sets `state.output` and appends to `state.messages` (`src/inspect_ai/agent/_react.py:770-771`). A submission sets `output.completion` to the answer (`_react.py:303-309`).
- `run()` copies its input into a new `AgentState` and returns it, or returns it with the `LimitExceededError` when one of its own limits fired (`src/inspect_ai/agent/_run.py:75-111`). A cancellation propagates and the state is not returned.
- `as_solver()` keeps a reference to the agent's state so that it can copy the output to the task state even when an exception ends the agent (`src/inspect_ai/agent/_as_solver.py:65-80`). A plain single agent that hits its limit is therefore scored on its last output.
- A `LimitExceededError` raised inside the solver is recorded as `EvalSample.limit` only when it reaches the runner (`run.py:2992-3001`).

#### What the merged designs fix for M1

- **Runtime and controller.** The runtime owns the following:
  - member tasks;
  - the provisional answer;
  - the stop procedure (close scheduling, soft stop, grace, hard stop);
  - finalisation and the record.

  The controller decides who starts, when, and when the swarm is done. It may start members conditionally, so some members can stay `pending` and never run ([Controller and runtime](swarm-api.md#controller-and-runtime), [Controllers](swarm-api.md#controllers)).
- **Member state and status.** `MemberState` is `pending`, `running` or `ended`. Once ended, a member's status is the limits deep dive's closed set as swarm-api.md cites it: `submitted`, `stopped`, `share`, `member_limit:<type>`, `swarm_cap`, `cancelled`, `errored`. `submitted` is this design's submission ([Controllers](swarm-api.md#controllers), "Statuses").
- **Evidence.** Evidence is `InfoEvent`s with `source="inspect_swarm"` and payloads carrying `version`, `type` and `kind`, with common fields `member` and `span_id` ([Evidence and provenance](swarm-communication.md#evidence-and-provenance)). Communication changes nothing about how a member submits: a member still submits through its own agent.

### Terms

- **Member output.** The member's `AgentState.output` when it stops, however it stops: what Inspect would have scored had that member been the only solver. The runtime's member wrapper keeps a reference to the member's `AgentState`, as `as_solver()` does, so the output survives the member's own limit and the stop procedure. For agents that do not update their state in place, a cancelled member's output is empty.
- **Submission.** A member output from a member that ended with status `submitted`: it finished normally.
  - For `react()` that is a submit; for a bridged agent it is the agent returning.
  - A member that ended with any other status (stopped, its own or the swarm's limit, cancelled, errored) has an output but no submission.
  - Each member submits at most once in M1, because a member runs once and `react()`'s internal attempts are not visible outside it.
- **Not started.** A member the controller never started stays `pending`. It has no output and no submission.
- **Final answer.** What the swarm returns as its `AgentState.output`.
- **Swarm time.** Seconds on the runtime's clock (`anyio.current_time()`) from the swarm's start. Every ordering by time, in the swarm and in the analysis, uses it.

### The final answer: `first`

M1's runtime always selects the final answer with `first` (decision: Ransom, 2026-10-08).

- **`first`** is the submission with the earliest swarm time, then roster order. Only submissions qualify. A member that ended with any other status is never selected, and neither is a member that never started.
- **No configuration in M1.** In M1, `leaderless()` takes no parameters and `Controller` has no `final`: with one mode there is nothing to choose.
  - swarm-api.md's `final=` and `stop_on_verified=` come with Part 2's chain and verifier.
  - With them, `final=None` means the default chain for the task's result contract. For a swarm without a contract, which every M1 swarm is, that chain is `["first"]`, so the same configuration selects the same answer before and after ([What M1 leaves out of the merged API](#what-m1-leaves-out-of-the-merged-api)).
- **Provisional answer.** When a submission is recorded, the runtime applies `first` to the submissions so far and sets the result on the `AgentState` it was passed. Swarm.md's [exhaustion policy](swarm.md#observer-evidence-accounting-and-metrics) requires this. Because `as_solver()` copies that object's output to the task state even when an exception ends the agent ([Agent output](#agent-output-and-member-state)), an outer limit that ends the sample early leaves the first submission as the sample's output, and the record says `provisional: true`. When nothing has been submitted, the output stays empty and the record's `reason` says why. Nothing is invented.
- **No submissions.** When no member submitted, the final answer is empty with `reason="no_submissions"`, even though members have outputs. An output from a member stopped mid-work is usually a working message, not an answer.
- **Finalisation.** After the stop procedure (swarm-api.md's [Stopping](swarm-api.md#controllers): scheduling closed, soft stop, grace, hard stop; the limits deep dive owns its ordering), the runtime does the following:
  1. It applies `first` to all submissions, including those from members that ended during grace.
  2. It writes the final record with `provisional: false`.
  3. It writes the `final` evidence event and returns.

  M1's finalisation makes no model call.
- **The returned `AgentState`** has the sample's input messages followed by one assistant message: the final answer's `output.message`, with `metadata={"inspect_swarm": {"final": {"mode": "first", "member": ...}}}`. Its `output` is the chosen member's `ModelOutput`, unchanged, so the task's scorer sees exactly the object it would have seen had that member run alone. Members' own conversations stay in their spans and timelines.
- **Shared-artifact tasks.** The task's scorer reads the drained environment. The output still carries the first submission, because some scorers read the output text and fall back to the environment (Frontier-CS, [below](#shared-artifact-scorers-in-inspect_evals)). M1 claims nothing about which member produced the environment, so it needs no declaration of what kind of result the task has.

### What the swarm records

The runtime keeps one record in the sample store under the key `inspect_swarm.result`, as `model_dump(mode="json")` of:

```python
# src/inspect_swarm/_record.py
class MemberResult(BaseModel):
    name: str                         # roster name after count expansion, chosen by the eval author
    role: str | None
    model: str | None
    state: MemberState                # swarm-api.md: "pending", "running" or "ended"
    status: str | None                # the limits deep dive's status once ended, else None
    submitted: bool                   # status == "submitted"
    output: ModelOutput | None        # member output (see Terms); None while pending
    time: float | None                # swarm time when the member stopped

class FinalRecord(BaseModel):
    mode: str | None                  # the mode that produced the answer: "first" in M1
    member: str | None                # the member whose output was chosen
    reason: str | None                # why there is no answer: "no_submissions" in M1
    provisional: bool                 # True until finalisation completes

class SwarmResultRecord(BaseModel):
    version: Literal[1] = 1
    members: list[MemberResult]
    final: FinalRecord
```

- **When it is written.** The runtime writes the record when the swarm starts, with every roster member `pending` and a provisional final record with `reason="no_submissions"`. It updates the record when a member starts, when one ends (adding its status and output) and when the provisional answer changes. It finalises the record after the stop procedure.
  - An outer limit that ends the sample early therefore leaves an accurate record with `provisional: true`.
  - Members the controller never started stay `pending` in the final record, which is how the evidence records them as not started.
- **Why the store.** It is in the store, not only in events, because re-scoring rebuilds `TaskState.store` and nothing else from the run (`src/inspect_ai/_eval/score.py:461-475`). Part 2's scorers read it from there.
- **Evidence events.** Each member's end and the final selection also produce an evidence `InfoEvent` with `source="inspect_swarm"` and the payload shape swarm-communication.md uses for its own evidence:
  - `version: 1`, `type: "result"`, and `kind` one of `member_result` or `final`;
  - the common fields `member` and `span_id` (the swarm's span for `final`).

  Each event is written inside the member's span or the swarm's span. The events put the result in the transcript at the time it happened; the store record is what scorers read. The payload format is the Observer's ([Observer](swarm.md#observer-evidence-accounting-and-metrics)). This design adds the `result` type, as the communication design adds `comm`.
- **Outputs are stored once.** Outputs can be long (code), so they are stored only in the record. Evidence events carry the member name and status, not the text.
- **Additive later.** Part 2 adds optional fields to these models ([What the record adds](#what-the-record-adds)). `version` stays 1, because no existing field changes meaning, and an M1 record reads as the record of a swarm without a result contract.

### Comparisons in M1

- **final@k is M1's only swarm-side quantity.** It is the task's scorer on the swarm's final answer, or on the drained environment for a shared-artifact task.
- **What it is compared with.**
  - A single agent at the full budget.
  - Epochs, through Inspect's own reducers on the task's scorer: `mean`, `pass_at(k)`, `max`.
  - `pass_at(k)` and `max` select with knowledge of the target and final@k does not, so they are upper references, not like-for-like comparisons. Part 2's [Comparison quantities](#comparison-quantities) defines the fair pairs.
- **A single-agent arm with the swarm's rules is a swarm of one.** M1's API already expresses it as `swarm(members=member(agent), channels=[])`.
  - It records the same evidence and returns an empty output when its member does not submit, where a plain agent is scored on its last output.
  - Part 2's `baseline()` only names this configuration; M1 needs no helper.
- **Cost** is the ledger's realized cost (the limits deep dive), taken from sample usage, which includes grading ([above](#why-m1-needs-little-from-scoring)).
- **What M1 does not provide.** team@k, mean member, best@k on graded scores and select@k all need Part 2.

### What M1 leaves out of the merged API

swarm-api.md's surface includes parts that exist only for Part 2. M1 does not build them:

- `swarm(result=)` and the `ResultSpec` it takes;
- `leaderless(final=, stop_on_verified=)` and `Controller(final=)`;
- `VerdictSummary`, `MemberHandle.verdict` and `MemberEnded.verdict`;
- the `member_scores` case of swarm-api.md's `tests/test_scoring_loop.py`.

Each comes back later in an additive form: a keyword parameter whose default keeps M1's behaviour, or a field that defaults to `None`. A user controller written for M1 keeps working. This branch marks the affected parts of swarm-api.md as after M2: the signatures, validation rules, examples, tests and plan step ([Compatibility](#compatibility-and-migration)).

### Testing in M1

All tests use mockllm with scripted outputs, need no network or Docker, and run on asyncio and trio.

- **`first`** (table-driven, over lists of member results):
  - the earliest swarm time wins, with roster order on equal times;
  - members that ended `stopped`, `member_limit:<type>`, `swarm_cap`, `cancelled` or `errored` are never selected, and neither are never-started members;
  - no submissions give an empty output with `reason="no_submissions"` while members have outputs.
- **Provisional answer.** It is set at the first submission. An outer sample limit after a submission leaves that submission as the sample's output with `provisional: true`, and the sample records its limit. An outer limit before any submission leaves an empty output with the reason recorded.
- **Finalisation.** A member that submits during grace is selected only when nothing earlier was submitted. The final record has `provisional: false`, and the `final` event is written.
- **The record.**
  - It is written at start with every member `pending` and updated on each start and end.
  - A member's output is kept after the member's own limit, and after a hard stop for an agent that updates its state in place.
  - Statuses are the limits deep dive's, and `submitted` is true only for `submitted`.
  - A controller that starts only some members leaves the rest `pending`.
  - The `member_result` and `final` events carry `version`, `type`, `kind`, `member` and `span_id`, and no member text.
- **The returned state.** Its `output` is the chosen member's `ModelOutput`, unchanged. Its messages are the sample's input plus the final message with its metadata.
- **Existing scorers unchanged** (`tests/test_task_scorers.py`):
  - a task with `match()` scores the swarm's first submission;
  - a `model_graded_qa()` scorer on mockllm sees only the final answer;
  - with the local sandbox, a scorer that reads files scores the files the drained swarm left, even with an empty final output.

### Implementation plan for M1

One PR, after swarm-api.md's steps 2 to 4 (registry plumbing, `Member` and the `swarm()` signature, the controller surface in the runtime) and the limits deep dive's member wrapper and stop procedure:

1. **The record and `first`.**
   - `src/inspect_swarm/_record.py`: `MemberResult`, `FinalRecord`, `SwarmResultRecord`.
   - `src/inspect_swarm/_final.py`: `first`, the provisional answer, finalisation and the returned `AgentState`.
   - The runtime writes and updates the record and the `member_result` and `final` events (`_swarm.py`, `_controller/_control.py`, `_evidence.py`), and the member wrapper keeps its `AgentState` reference.
   - Tests in `tests/test_final.py` and `tests/test_task_scorers.py`.

## Part 2: after M2 (optional)

Everything in this part is optional follow-up work after M2, in any order with the rest of swarm.md's menu (decision: Ransom, 2026-10-08). It keeps the detail it was designed with, so whoever builds a piece needs no further decision except [Open question 1](#open-questions), which only the chain needs. Its parts depend on each other as follows:

- The result contract comes first. The chain's `verify` and `vote` need it, `synthesize` needs the chain, and `member_scores` needs the contract.
- `best_at(k)` is a reducer and depends on nothing else.
- select@k and `compare_arms` need the record's additions, `baseline()` and `member_scores`.
- Per-member artifacts extend `member_scores`.
- The cost boundary matters once `member_scores` runs a model-graded scorer in arms compared on cost.

### Why

Swarm.md fixes the principle: what counts as a member's result belongs to the task, because Inspect scorers take a `TaskState` and a target and may inspect the sandbox. Claims beyond the swarm's final answer need more than M1 provides:

- **What the task supplies, and where.** The swarm (a solver) selects a final answer at run time. Scorers run afterwards, and again when a log is re-scored. Both need the same facts about the task: whether answers are comparable, whether there is a verifier, whether members leave their own artifacts.
- **What "team@k" means.** The paper swarm.md cites defines team@k as "any of the k agents" solving the task, not the team's single final answer ([Test-Time Communication](https://arxiv.org/abs/2609.21032), ARC-AGI-3 and Terminal-Bench sections). An experiment that compares the swarm's *selected* answer against an *oracle* best-of-k from epochs, or the reverse, compares different things.
- **How the comparisons are computed.** Inspect ships `pass_at(k)` for binary scores and `max` for best-of-all-epochs, but nothing for best-of-k on graded scores, and nothing that applies a selection rule (verify, vote) to k independent attempts.
- **What per-member scoring does to the cost comparison.** Per-member scoring runs a scorer k more times in the swarm arm, and that usage lands in the sample's usage ([Why M1 needs little](#why-m1-needs-little-from-scoring)).
- **What happens in the shared sandbox.** A shared-artifact scorer examines one environment. Some scorers that look answer-scored fall back to reading the environment, which would credit the team's work to a member.

Getting these wrong produces numbers that look comparable and are not. That is the failure the result contract exists to prevent.

### Goals and non-goals

Goals:

- One task-supplied contract, read by both the swarm and the scorers, that says three things:
  - what kind of result the task has;
  - what it offers for selection (an answer key, a verifier);
  - what it offers for per-member scoring (member artifacts).
- Every member's result, and the run-time evidence selection needs, recorded in the sample, so scorers and the analysis read the same facts during the eval and after re-scoring.
- Baseline arms that produce the same record as the swarm arm, so comparisons use one clock and one eligibility rule.
- A precise final-answer chain with the decided default (verify, else vote, else first) and an explicit `synthesize`.
- Named, defined comparison quantities (final@k, team@k, mean member, pass@k, best@k, select@k), each paired with its fair counterpart, and the code that computes each.
- Per-member scores in shared-artifact tasks when, and only when, the task provides per-member artifacts.
- Scores that appear in logs and eval sets as ordinary Inspect scores and metrics, needing no viewer or schema change.

Non-goals:

- Changing any task's own scorer. The task's scorer stays the headline score in every arm.
- Scoring trajectories per member: scorers that read tool calls or intermediate messages, rather than the answer, are not scored per member.
- Defining the realized-cost ledger, the reserve or the swarm cap (the limits deep dive).
- Harness-validity checks and swarm metrics as scores (swarm.md's [Observer](swarm.md#observer-evidence-accounting-and-metrics)).
- `reporter` and coordinator topologies, beyond reserving the name.
- Per-member sandboxes ([Sandbox topology](swarm.md#sandbox-topology) later work).

### Current behaviour Part 2 relies on

In inspect_ai unless noted, in addition to [what M1 relies on](#current-behaviour-m1-relies-on).

#### Scores, dict values and metrics

- `Scorer` returns `Score | None` (`src/inspect_ai/scorer/_scorer.py:34-40`). A `Score` has `value`, `answer`, `explanation`, `reason` and `metadata` (`src/inspect_ai/scorer/_metric.py:118-142`).
- `Score.unscored()` sets the value to NaN, which metrics and reducers skip (`_metric.py:164-180`). Model graders and `expression_equivalence()` return it when they cannot grade (`src/inspect_ai/scorer/_model.py:348`, `src/inspect_ai/scorer/_math.py:1322`).
- A scorer may return a dict value and declare metrics per key, `@scorer(metrics={"key": [mean()]})` (`_scorer.py:133-136`). Each key becomes its own `EvalScore` named after the key, and a NaN under a key counts that sample as unscored for that key only (`src/inspect_ai/_eval/task/results.py:488-600`, `:523-534`).

#### Scorer arguments in the log and re-scoring

- A registered scorer's arguments are logged as JSON. A registry object with parameters becomes a `{type, name, params}` dict that is rebuilt on replay. A plain object, such as a dataclass holding callables, becomes its type name, and a callable becomes its `__name__` (`src/inspect_ai/_util/registry.py:218-247`, `:660-706`).
- Default `inspect score` rebuilds each scorer from its logged name and options, loading the task file when the scorer was defined there (`src/inspect_ai/_eval/score.py:600-640`). So a scorer survives re-scoring when its factory is registered and its logged arguments rebuild it.
  - A spike checked this. A no-argument `@scorer` factory, defined in the task file, built its scorer from a dataclass of callables. In a fresh process, `resolve_scorers()` and `score()` rebuilt it, and it gave the same score and metadata.
- Re-scoring rebuilds a `TaskState` from the logged sample, including `store`, `metadata` and `output`, with `completed=True` (`score.py:461-475`). There is no sandbox: `sandbox()` raises `ProcessLookupError` (`src/inspect_ai/util/_sandbox/context.py:55-63`).

#### Epochs and reducers

- `Epochs(n, reducer=[...])` applies every reducer to every scorer's per-epoch scores (`src/inspect_ai/_eval/task/epochs.py:4-29`). A reducer sees only `list[Score]` for one sample and one scorer (`src/inspect_ai/scorer/_reducer/types.py:7-14`). There is no per-scorer reducer.
- `pass_at(k)` is the unbiased without-replacement estimator 1 − C(n−c, k)/C(n, k) for n ≥ k epochs, NaN when fewer than k are scored (`src/inspect_ai/scorer/_reducer/reducer.py:163-205`).
- `max` is best-of-all-n epochs (`reducer.py:247-301`). There is no best-of-k estimator for graded scores.
- `mode` and `majority` take the most common score *value* (`reducer.py:12-82`). They vote on correctness, not on answers, so neither is majority voting over the agents' answers.

#### Usage and timing during scoring

- Sample usage includes scoring ([Why M1 needs little](#why-m1-needs-little-from-scoring)). The `ScoreEvent` records a snapshot of sample usage after each scorer (`run.py:3102-3104`).
- Native compaction adds usage with no model event (`src/inspect_ai/model/_model.py:1340-1361`), so model events alone cannot separate solve usage from scoring usage.
- `EvalSample.working_time` is computed when the sample is logged, after scoring and cleanup, and excludes waiting (`run.py:3164`, `:3456`, `:3490`). It is not when an attempt finished solving.

#### In-loop scoring

- `score(state)` runs every task scorer, with the target, during the solve and records `ScoreEvent(intermediate=True)` (`src/inspect_ai/scorer/_score.py:14-80`). The state it scores has `completed=False`.
- Its callers include:
  - `react(attempts=...)` (`src/inspect_ai/agent/_react.py:330`), and synchronous `deepagent()` attempts through `react()`;
  - `basic_agent` (`src/inspect_ai/solver/_basic_agent.py:234`);
  - the human agent's `/score` (`src/inspect_ai/agent/_human/commands/score.py:58`);
  - inspect_swe's Claude Code and Codex attempt loops (`inspect_swe: src/inspect_swe/_claude_code/claude_code.py:621`, `src/inspect_swe/_codex_cli/codex_cli.py:749`).
- `react()` reads only the first score (`_react.py:330-333`). This is target-informed feedback, not a verifier.

#### Shared-artifact scorers in inspect_evals

- SWE-bench diffs the repository against the start commit and runs the test script in the sandbox; the value depends only on the environment (`inspect_evals: src/inspect_evals/swe_bench/scorers.py:28-88`).
- Frontier-CS extracts code from `output.completion` and compiles and runs it in the sandbox. When the *extracted code* is empty it recovers code from the sandbox's files (`inspect_evals: src/inspect_evals/frontier_cs/scorer.py:672-676`, `:631-660`).
  - Extraction returns the last fenced block's contents (`scorer.py:38-69`), so a non-empty completion holding an empty fenced block also triggers the recovery.
  - The unchanged scorer therefore reads the answer from the environment in some cases: it is not answer-scored in this design's sense.

#### What the merged designs fix for Part 2

- **Where the chain is passed.** `final=` is a controller parameter: `leaderless(final=..., stop_on_verified=...)`. `Controller(run, final=...)` holds it, and `final=None` means this design's default chain for the task's `result` ([Controllers](swarm-api.md#controllers)). `swarm(result=)` stays on `swarm()`.
  - swarm-api.md types the chain as `FinalSpec = str | FinalMode | Sequence[str | FinalMode]`; this design narrows `str` to the mode names.
  - Every `FinalMode` must be a pydantic model with a discriminator, so a mixed chain is logged faithfully and validates back from JSON ([Logging and replay](swarm-api.md#logging-and-replay)).
- **What controllers see of a verdict.** `VerdictSummary(passed, value)`, on `MemberHandle.verdict` and `MemberEnded.verdict`. A member counts as running until the runtime has taken its submission's verdict and queued its `MemberEnded`. The explanation stays in the result record.
- **Logging.** `swarm(result=)` is logged as the type name `"ResultSpec"`. A raw-solver replay (`--solver inspect_swarm/swarm -S ...`) passes that string back, and `swarm()` raises `TypeError` for it. The supported routes are the task, or a registered swarm builder ([Logging and replay](swarm-api.md#logging-and-replay)).
- **Task identity.** Scorers are not hashed into `task_identifier()`, and a member's in-loop scoring makes the task's scorer part of the trajectory. A scorer ablation is therefore an arm of its own, chosen by a task argument ([Task identity](swarm-api.md#task-identity-what-makes-two-arms-distinct)).
- **Fencing (M2).** Peer text is fenced in tagged envelopes whose tag markers are removed from payloads, repeating until none remain ([Fencing](swarm-communication.md#fencing)).

### The result contract

The task describes its result once:

```python
# src/inspect_swarm/_result.py
ResultKind = Literal["answer", "artifact"]

@dataclass(frozen=True)
class Verdict:
    passed: bool
    value: float | None = None        # optional graded verdict; higher is better
    explanation: str | None = None

class Verifier(Protocol):
    async def __call__(self, answer: str, metadata: dict[str, Any]) -> Verdict: ...

class MemberArtifacts(Protocol):
    def materialize(self, member: str) -> AbstractAsyncContextManager[None]: ...

@dataclass(frozen=True)
class ResultSpec:
    kind: ResultKind
    answer_key: Callable[[str], str | None] | None = None
    verifier: Verifier | None = None
    member_artifacts: MemberArtifacts | None = None   # kind == "artifact" only

def answer_result(*, answer_key=None, verifier=None) -> ResultSpec: ...
def artifact_result(*, answer_key=None, verifier=None, member_artifacts=None) -> ResultSpec: ...
```

**`kind="answer"`** asserts that the scorer given to `member_scores()` is a function of the answer.

- The answer means `output`, the last assistant message, the sample's input, metadata and target.
- The scorer may use the sandbox as a fixed evaluator (compile and run the answer), but never as a source of the answer.
- A scorer that reads the trajectory, or that can read the answer out of the environment, is not answer-scored. The unchanged Frontier-CS scorer is one, because its recovery path reads the environment.
- Such a task either declares `kind="artifact"`, or passes `member_scores()` an answer-only adapter of its scorer that scores empty extracted code as zero instead of recovering it ([Answer-scored tasks](#answer-scored-tasks-per-member-scores)).

**`kind="artifact"`** asserts that the scorer examines the environment the agents leave behind (SWE-bench). The team has one result.

**The answer key, `answer_key(text) -> str | None`**, maps an output's completion to a comparable key: extract and normalise, for example a boxed number. It returns `None` when there is no comparable answer. It never sees the target. It is task code, so an exception from it propagates and fails the sample; a key function that cannot parse should return `None`.

**`verifier(answer, metadata) -> Verdict`** is the task's in-loop checker: visible tests, a proof checker, a format check, a model judge.

- **Inputs.** It gets the answer text and the sample's metadata, and may use `sandbox()`. It is never given the target.
- **A documented contract.** The swarm cannot stop a verifier from reading the target through private state. A verifier must not call `score()`, which runs the task's scorers with the target.
- **Errors.** A verifier exception propagates and fails the sample, because silently degrading `verify` to `vote` would change what is measured.
- **When it runs.** Verifiers run only during the solve ([When verdicts are taken](#the-final-answer-chain)). Scoring and re-scoring read the recorded verdicts and never call a verifier.

For audit, the record stores a label for the verifier: its `name` attribute if it has one, else its `__qualname__`, else its class's `__qualname__`. It is a label, not a reconstruction; the verifier's code is the task's.

**`member_artifacts.materialize(member)`** is an async context manager that puts that member's artifact where the scorer looks for the team artifact, and restores the team artifact on exit, including on error. See [Shared-artifact tasks](#shared-artifact-tasks-and-per-member-artifacts).

A swarm without `result=`, which includes every M1 swarm, still runs:

- its default chain is `first`;
- its record has no `kind`;
- `member_scores()` reports per-member values as unavailable.

Omitting the contract never produces a per-member claim.

`ResultSpec` stays a frozen dataclass. It holds the task's callables, so no serialisable form would rebuild it, and swarm-api.md already routes it through the task or a registered swarm builder ([What the merged designs fix](#what-the-merged-designs-fix-for-part-2)).

### Opting in

The spec holds callables, which Inspect cannot log or rebuild as a scorer argument ([Scorer arguments](#scorer-arguments-in-the-log-and-re-scoring)) or as a solver argument. So the task does three things:

- it keeps the spec in one function;
- it passes the spec to `swarm()` directly;
- it wraps `member_scores()` in a **registered, no-argument scorer factory of its own**, which re-scoring rebuilds by name.

```python
def frontier_math_result() -> ResultSpec:
    return answer_result(answer_key=boxed_number)

@scorer(metrics=MEMBER_METRICS)
def frontier_math_members() -> Scorer:
    return member_scores(expression_equivalence(), frontier_math_result())

@task
def frontier_math(arm: str = "swarm", k: int = 4, budget: float = 40.0) -> Task:
    agent = react(...)
    solver = (
        swarm(
            members=member(agent, name="solver", count=k),
            budget=Budget(cost=budget),
            result=frontier_math_result(),
        )
        if arm == "swarm"
        else baseline(agent, result=frontier_math_result())
    )
    return Task(
        dataset=...,
        solver=solver,
        scorer=[expression_equivalence(), frontier_math_members()],
        epochs=Epochs(k, ["mean", pass_at(k), best_at(k)]) if arm == "epochs" else 1,
        cost_limit=budget / k if arm == "epochs" else budget,
    )
```

- **The builder.** `member_scores(scorer, result, *, value_to_float=value_to_float(), empty_value=0.0) -> Scorer` is a plain builder, not itself registered. `MEMBER_METRICS` is `{"best_member": [mean(), stderr()], "mean_member": [mean(), stderr()]}`. Both are exported from `inspect_swarm.scorer`.
- **Factory parameters.** If the factory needs parameters, they must be values Inspect logs and rebuilds (strings, numbers, registered objects), and the factory builds the spec from them.
- **The headline.** The task's own scorer stays first and unchanged, so the headline score is the same scorer in every arm.
- **Defaults and arm identity.** The swarm arm uses swarm-api.md's defaults: `controller="leaderless"`, whose `final=None` gives this design's default chain, and `channels=("filesystem",)`. The arms differ in the task argument `arm`, so they are distinct in an eval set even though scorers and epochs are not hashed ([Task identity](swarm-api.md#task-identity-what-makes-two-arms-distinct)).
- **Replay from a solver spec.** A task that wants the swarm replayable from a solver spec (`--solver`, inspect_flow solver specs) wraps it in a registered swarm builder, swarm-api.md's `math_swarm`, which builds the same `result=` inside.
- **Baselines run as a swarm of one.** `baseline(agent, result)` is `swarm(members=member(agent), controller=leaderless(final="first"), channels=[], result=result)`, as swarm-api.md's [parameter table](swarm-api.md#where-the-sibling-designs-parameters-go) has it. It is swarm.md's natural baseline, and the result-contract form of M1's swarm of one ([Comparisons in M1](#comparisons-in-m1)).
  - Every arm then writes the same record, with the same clock, eligibility rule and run-time verdicts, which select@k needs.
  - A plain agent without `baseline()` still gets `member_scores` values (its output is scored as one member), but no run-time evidence. The analysis leaves it out of select@k and says so.
- **The reserve.** In practice the swarm's cap sits below the sample limit by the final-answer reserve (the limits deep dive); the example leaves that out.

A swarm of one returns an empty output when its member never submits, where a plain agent would be scored on its last output. That is the same conservative rule the swarm arm follows ([No submissions](#the-final-answer-first)), so the arms stay comparable with each other, if not with a plain agent.

### What the record adds

Part 2 adds optional fields to M1's record ([What the swarm records](#what-the-swarm-records)). Each defaults to `None`, or to `False` for `tie`:

```python
class MemberResult(BaseModel):        # adds
    answer_key: str | None            # result.answer_key(output.completion), when defined
    verdict: Verdict | None           # verifier result for a submission, when a verifier exists
    verdict_time: float | None        # swarm time when the verdict was taken

class FinalRecord(BaseModel):         # adds; reason gains e.g. "none_verified"
    chain: list[str] | None           # mode names, e.g. ["verify", "vote", "first"]; parameters are in the plan
    votes: dict[str, int] | None      # answer key -> members, when vote ran
    tie: bool                         # vote's plurality was tied and broken by time
    synthesize_error: str | None      # a recovered synthesize failure, when one occurred

class SwarmResultRecord(BaseModel):   # adds
    kind: ResultKind | None
    verifier: str | None              # verifier label, for audit
    final_verdict: Verdict | None     # artifact kind: verifier on the drained environment
```

- **When they are written.** The runtime adds a member's key and verdict when its submission is recorded and verified, and the chain fields when the provisional answer or the final selection changes.
- **The `verdict` event.** Each verdict also produces an evidence event of kind `verdict`, alongside M1's `member_result` and `final`.
- **The cost boundary**, when it is built, adds `solve_usage`, `solve_role_usage` and `solve_exit` ([The cost boundary](#the-cost-boundary-deferred)).

### The final-answer chain

The controller's `final=` parameter (`leaderless(final=...)`) takes one mode or a sequence of modes. A sequence is a chain: each mode is tried in order on the submissions, and the first that produces an answer wins. The runtime applies the controller's chain; the controller never sees member text ([What the merged designs fix](#what-the-merged-designs-fix-for-part-2)).

```python
# src/inspect_swarm/_final.py
FinalName = Literal["verify", "vote", "first", "synthesize"]   # "synthesize" is synthesize() with its defaults

class Synthesize(BaseModel):          # frozen; arbitrary types allowed for Model
    mode: Literal["synthesize"] = "synthesize"
    model: str | Model | None = None  # a Model is written as Inspect's model dict and read back from it
    prompt: str | None = None

class Reporter(BaseModel):            # frozen
    mode: Literal["reporter"] = "reporter"
    member: str                       # a roster name

FinalMode = Annotated[Synthesize | Reporter, Field(discriminator="mode")]
FinalSpec = FinalName | FinalMode | Sequence[FinalName | FinalMode]

def synthesize(model: str | Model | None = None, prompt: str | None = None) -> Synthesize: ...
def reporter(member: str) -> Reporter: ...
def final_chain(final: FinalSpec | None, result: ResultSpec | None) -> list[FinalName | FinalMode]: ...
```

- **Logging and replay.** A controller factory validates its `final` argument with a `TypeAdapter(FinalSpec)`, which also accepts the dict forms replay passes back, as swarm-api.md requires of every pydantic argument.
  - `Synthesize.model` needs its own serialiser and validator, because `registry_value()` reaches a `Model` only at top level or inside lists and dicts, not inside a pydantic model (`src/inspect_ai/_util/registry.py:660-686`). The serialiser writes a `Model` with `registry_value()` (Inspect's model dict: name, config, base URL, model args), and the validator accepts that dict.
  - A spike confirmed the round trip, with an `@agent` factory standing in for `leaderless`. `final=["verify", Synthesize(model=get_model("mockllm/model"), prompt="x"), "first"]` was logged as `["verify", {"mode": "synthesize", "model": {"model": "mockllm/model", "config": {}, "base_url": null, "model_args": {}}, "prompt": "x"}, "first"]`. `create_registry_object()` on those params rebuilt the chain with a `Model` whose name matched.
  - The plain string `"synthesize"` is logged as itself.
- **`final_chain()`** turns the controller's `final` and the task's `result` into the chain the runtime applies: the default below when `final` is `None`, otherwise `final` as a list.

| Mode | Needs | Produces | Produces nothing when |
|---|---|---|---|
| `verify` | `result.verifier` | The passed submission with the highest `Verdict.value` (`None` ranks below any number), earliest swarm time on ties, then roster order | no submission passed |
| `vote` | `result.answer_key` | The earliest submission in the plurality group of answer keys; one vote per member; `None` keys do not vote. A tied plurality goes to the group whose first submission is earliest, and `tie` is recorded | no submission has a key |
| `first` | nothing | As in M1 ([The final answer](#the-final-answer-first)) | there are no submissions |
| `synthesize` | `kind="answer"` | One extra model call over the submissions ([below](#synthesize)) | there are no submissions, or the call fails recoverably |
| `reporter` | a coordinator topology | Reserved; no built-in controller accepts it yet | |

**The default.** With the controller's `final=None` (the default of `leaderless()`), the chain is built from what the task supplies:

- `verify` if there is a verifier;
- then `vote` if there is an answer key;
- then `first`.

So a task with both gets `["verify", "vote", "first"]`, and one with neither gets `["first"]`, M1's behaviour (decision: Ransom, 2026-10-07, for the order). This design reads the decision as a chain that falls through: when nothing verifies, the swarm votes, and when nothing can vote, it takes the first submission. A single mode, `leaderless(final="verify")`, is strict: no verified submission means no answer. [Open question 1](#open-questions) asks Ransom to confirm the fall-through reading.

**Validation.**

- `swarm()` applies these rules to its controller's chain at construction, as swarm-api.md's validation list says. It raises `ValueError` when a mode's need is missing (`verify` without a verifier, `vote` without an answer key, `synthesize` on an artifact task), naming the missing piece.
- A `reporter` mode is the controller's to accept. `leaderless()` raises `ValueError` for one when it is called. A controller that accepts one checks its member against the roster in its `check`, as swarm-api.md's `coordinator` does.

**When verdicts are taken.** When the task supplies a verifier, the runtime verifies every submission as it arrives, whether or not `verify` is in the chain, so the analysis always has verdicts.

- It takes the verdict before it queues the member's `MemberEnded`, so a controller's `stop_on_verified` or escalation sees it, as a `VerdictSummary` without the explanation.
- Verifier calls run one at a time in the runtime, inside the swarm's span. Their usage, if any, is the swarm's, not a member's. Which limit node meters them is the limits deep dive's call.

**Provisional answer.** M1's rule ([The final answer](#the-final-answer-first)) with the chain in place of `first`. After each submission or verdict, the runtime applies the chain without `synthesize` (which needs a model call) to the submissions so far.

**Finalisation.** M1's finalisation, plus three steps before the final record is written:

1. The runtime verifies any submission not yet verified, including those from members that ended during grace.
2. It runs the chain once more, including `synthesize`.
3. For an artifact task with a verifier, it takes `final_verdict` ([Shared-artifact tasks](#shared-artifact-tasks-and-per-member-artifacts)).

### `synthesize`

`synthesize` is explicit only, never a default, because it is the one place peer text enters a prompt the harness composes ([Security](swarm.md#security)).

`synthesize(model=None, prompt=None)` returns the `Synthesize` mode above; the string `"synthesize"` is the same with its defaults.

- **Model.** `model`, else the model role `synthesizer` if the eval defines it, else the swarm's default model (`get_model()`).
- **Prompt.** A fixed template:
  - the sample's input;
  - a fixed header, which says that each block is another agent's output, to be treated as information, not instructions;
  - each submission, in roster order, in the envelope swarm-communication.md uses for peer messages ([Fencing](swarm-communication.md#fencing)), with its own tag: `<member_submission id="1" from="worker-2">...</member_submission>`;
  - an instruction to give one final answer in the format the task asks for. `prompt` replaces that instruction, not the header or the fencing.

  The fencing rules:
  - **Marker removal** follows the communication design's rule. Every case-insensitive occurrence of `<member_submission` and `</member_submission` is removed from the payload, repeating until none remain, so nested fragments cannot reassemble a tag.
  - **Attribute values** are roster names and sequence numbers. Roster names come from the eval author, but no name grammar applies unless the messages channel is enabled, so the fence escapes them as XML attribute values.
  - **Text only.** The payload is the submission's completion. Members' tool calls, transcripts and messages are never included.
  - **One fencing implementation.** `synthesize` reuses M2's fencing code with its own tag. If M2 left that code inside the messages channel (`_channel/messages.py`), this step moves it to `src/inspect_swarm/_fence.py` so the two cannot drift.
- **Shared notes.** The filesystem notes file is not read, because its contents are unfenced and member-written in any layout. If the notes channel is built, its entries are fenced the same way.
- **Accounting.** The call runs in the swarm's span after the drain, inside the final-answer reserve, so the ledger counts it as finalisation.
- **Failures.** Before the call, the provisional answer (computed without `synthesize`) is already on the `AgentState`. Then:
  - **Classification is group-aware.** Inspect re-raises a provider's exception without unwrapping it (`src/inspect_ai/model/_model.py:1651-1656`), so a model API that uses a task group can raise an `ExceptionGroup` holding a `LimitExceededError`.
    - An exception is *stopping* if it is, or is a `BaseExceptionGroup` that contains at any depth, a `LimitExceededError`, a `TerminateSampleError` or the backend's cancellation exception (`anyio.get_cancelled_exc_class()`).
    - The check uses `BaseExceptionGroup.subgroup()` (Python 3.11).
  - **Recoverable** failures move the chain to its next mode, and `synthesize_error` records why. A chain of `["synthesize"]` alone keeps the provisional answer. They are:
    - an `Exception` from the call that is not stopping: a provider error after Inspect's retries, a `ModelRefusalError`, a group of such errors;
    - a returned output with an empty completion or `stop_reason="content_filter"`.
  - **Not recoverable:** a stopping exception propagates unchanged, as raised.
    - A group is never split. A group that mixes a limit with an ordinary error propagates whole, which is what swarm.md's exhaustion policy expects of mixed groups.
    - A limit error reaches the runtime's [exhaustion policy](swarm.md#observer-evidence-accounting-and-metrics), which recovers only the swarm's own cap. Every other limit reaches the runner, so Inspect records `EvalSample.limit`.
    - Either way the provisional answer is the sample's output.
- **Scoring.** The synthesized answer is no member's output. The task's scorer scores it; per-member scores are unaffected.

### Answer-scored tasks: per-member scores

The scorer `member_scores(scorer, result, ...)` builds works in six steps.

1. **Skips in-loop scoring.** If `state.completed` is `False`, it returns `None` at once. Final scoring and re-scoring both have `completed=True`.
   - `score()` drops `None` results, so in-loop callers see exactly the task scorer's feedback and call count they saw before. The callers are `react(attempts=...)`, `basic_agent`, the human agent's `/score` and the inspect_swe attempt loops.
   - During the solve the wrapper never runs the inner scorer, touches the sandbox or reads a half-written record.
2. **Reads the members.** It reads them from the `inspect_swarm.result` record when it exists. Otherwise (a plain agent, not `baseline()`) it treats the sample's `output` as one started member named `solver` with status unknown, and computes its answer key, which is a pure function. It never runs the verifier.
3. **Checks the contract.** If `result.kind` is not `"answer"`, or the record's kind differs from `result.kind`, it returns `best_member` and `mean_member` as NaN with `reason` saying why: `"artifact_without_member_artifacts"`, `"kind_mismatch"` or `"kind_not_declared"`. An M1 record, which has no kind, gives `"kind_not_declared"`. Shared-artifact tasks with member artifacts are handled [below](#shared-artifact-tasks-and-per-member-artifacts).
4. **Grades each member, in roster order.** A member that never started (state `pending`) gets the outcome **not started**: no value, reason `"not_started"`, and the scorer is not called. It ran nothing, so it is neither a failure nor unavailable. Every other member gets one of three outcomes:
   - **Empty.** No output, or a completion that is empty after stripping whitespace: value `empty_value` (default 0.0), reason `"empty_output"`, and the scorer is not called.
     - This keeps a scorer's environment fallback from crediting the team's work to a member that produced nothing.
     - It is a backstop, not the contract. A non-empty completion can still extract to nothing (an empty fenced block), which is why `kind="answer"` requires a scorer that never reads the answer from the environment.
   - **Graded.** Otherwise it builds a deep copy of the `TaskState` whose `output` is the member's `ModelOutput` and whose `messages` are the sample's input messages plus the member's `output.message`. It awaits the scorer with the sample's target, and converts the value with `value_to_float`.
   - **Unavailable.** The scorer returned `None`, a NaN value (`Score.unscored()`), or a dict or list value (there is no general best of a dict). The member's value is NaN, with the scorer's own `reason` kept, or `"scorer_returned_none"` or `"non_scalar_value"`.
5. **Aggregates over the members with a value** (empty and graded). It ignores unavailable and not-started members explicitly, rather than through `max`/`mean` on NaN, so the result does not depend on roster order.
   - `best_member` is their maximum and `mean_member` their mean.
   - Metadata records `members_total` (the roster), `members_started`, `members_valued` and `complete` (no started member unavailable). Analysis can then restrict to complete samples and see how many members a conditional controller ran.
   - With no member valued, both keys are NaN with reason `"no_member_valued"`.
6. **Returns** `Score(value={"best_member": ..., "mean_member": ...}, metadata=...)`. The analysis reads the metadata, which holds:
   - per member: outcome, value, answer, explanation, reason, state, status, `submitted`, answer key, verdict and time;
   - the record's `final` block and `kind`.

Scores of the copies are not added to `state.scores`, so they do not appear as extra sample scores. Each inner scoring runs inside a span named after the member, within `member_scores`'s scorer span, so a model-graded scorer's calls are attributable.

**Which scorer.** The scorer given to `member_scores` must meet the answer-scored contract. It is usually the task's own scorer.

- When the task's scorer can read the answer from the environment, the task passes an answer-only adapter instead. For Frontier-CS that is a variant that scores empty extracted code as 0 rather than recovering code from the sandbox. Frontier-CS has no switch for this today; the adapter is task code.
- The headline stays the unchanged task scorer. Oracle comparisons (team@k against best@k) use `member_scores` values in every arm, so both sides use the same adapter ([Comparison quantities](#comparison-quantities)).

**Ordering.** `member_scores` must follow the task's own scorer in the scorer list. The task's scorer then scores the final answer, and the environment, before any per-member scoring can touch the sandbox. Answer-scored scorers that use the sandbox as an evaluator (compiling and running the answer) run once per member in the same sandbox, one after another. This is safe when the scorer writes its own files before reading them, which is the task's responsibility.

**Time.** Per-member scoring runs within Inspect's scoring time limit, half the sample's (`run.py:3034`). Sandbox evaluators across many members can exceed it; the sample then errors as for any slow scorer. Tasks with costly evaluators should set a time limit with that in mind.

**Cost.** A model-graded scorer is called k more times per sample in the swarm arm, and once more in other arms. That usage is in the sample's totals. Until [the cost boundary](#the-cost-boundary-deferred) exists, arms compared on cost should use a scorer for `member_scores` that makes no model calls, or report the difference.

### Shared-artifact tasks and per-member artifacts

For `kind="artifact"`:

- **The team score** is the task's scorer on the drained environment, as in M1. It is the only result the task defines, and it is computed first.
- **The final-answer chain** still runs and sets `output`, because some scorers read the output text and fall back to the environment (Frontier-CS). `synthesize` is rejected.
- **The verifier**, when given, checks the environment, not an answer.
  - It is called with the submitting member's completion and the sample metadata whenever a member submits.
  - A controller can stop on it (`leaderless(stop_on_verified=True)`, [swarm-api.md](swarm-api.md#examples)).
  - Members still working when it fires keep editing until the stop procedure ends them, so the environment that is scored can differ from the one that was verified.
- **`final_verdict`** is therefore taken in finalisation, after the stop procedure, on the environment that will be scored. The runtime calls the verifier once more with the final answer's completion, or `""` when there is no final answer. Analysis compares it with the run-time verdicts and with the team score.
- **Per-member scores** are unavailable unless the task supplies `member_artifacts`.

With `member_artifacts`, `member_scores` grades each member's artifact instead of its output text. Every member is graded, whatever its completion. The empty-output outcome is for answer tasks only, because a member can leave a passing artifact and stop without submitting any text. Unavailable grades and aggregation follow answer tasks:

```python
for m in members:                                   # roster order
    async with result.member_artifacts.materialize(m.name):
        score = await scorer(copy_of_state_with_member_output(m), target)
```

- **`materialize` is task code**, because only the task knows its layout. Take a SWE-bench-shaped task where each member works in its own git worktree on branch `swarm/<member>` ([Sandbox topology](swarm.md#sandbox-topology)). Its `materialize` would:
  - commit or stash the team's working tree;
  - check out the member's branch in the scored location;
  - on exit, restore the team's state, untracked and scorer-created files included.
- **It is given only roster names**, never names a member chose, so paths and branch names come from the eval author's layout.
- **It runs after the team score** and restores the team state on exit, so the team score never depends on per-member scoring. A `materialize` that raises propagates and fails the sample, as any scorer error does.
- **Attribution in a shared sandbox is by convention.** Any member can write into another member's worktree or branch. Per-member artifact scores say "what was on that member's branch", not "what that member did alone". Tool-call evidence can show cross-writes, but only as far as the observation limit in [Security](swarm.md#security) allows.

`member_artifacts` is its own step in [the plan below](#implementation-plan-after-m2), after `member_scores`.

### Comparison quantities

Each quantity is defined once and paired with the one it is fairly compared with. k is the number of members, or the number of independent attempts.

| Quantity | Arm | Definition | Uses the target to select? | Computed by |
|---|---|---|---|---|
| **final@k** | swarm of k | Answer tasks: the task's scorer on the swarm's final answer, `empty_value` when there is none. Artifact tasks: the task's scorer on the drained environment, whatever the final text | No | the task's scorer; the helper applies the empty rule to answer tasks only |
| **team@k** | swarm of k | Answer tasks: `best_member`. Artifact tasks: the shared environment's score, equal to final@k | Answer: yes. Artifact: no | `member_scores`; the task's scorer |
| **mean member** | swarm of k | `mean_member` | No | `member_scores` |
| **pass@k** | n ≥ k epochs of `baseline()` | P(at least one of k attempts correct), binary values, without replacement | Yes | Inspect `pass_at(k)` on the oracle value source (below) |
| **best@k** | n ≥ k epochs of `baseline()` | E[max over k attempts], graded values, without replacement | Yes | inspect_swarm `best_at(k)` on the oracle value source (below) |
| **select@k** | n ≥ k epochs of `baseline()` | The swarm's chain applied to k independent attempts | No | `inspect_swarm.analysis` |
| **single** | one `baseline()` attempt at the full budget | As final@k | No | as final@k |

**The oracle value source** is the scorer that both sides of team@k against pass@k or best@k are read from. The result kind fixes it:

- **Answer tasks:** the member scorer's `best_member`. In a `baseline()` arm each epoch has one member, so its `best_member` is that attempt's value under the same scorer that `member_scores` uses in the swarm arm (possibly an answer-only adapter).
- **Artifact tasks:** the task's own scorer, the environment score.
  - Without `member_artifacts`, `best_member` is NaN in every arm, and Inspect's reducers drop NaN epochs (`reducer.py:195-205`). An artifact pass@k read from `best_member` would always be NaN.
  - team@k for artifact tasks is the swarm's environment score, so the baseline side reads the same scorer. pass@k and best@k are the reducers' values on the task scorer, which the example's `Epochs(...)` already computes for every scorer.
  - With `member_artifacts`, `best_member` adds per-member artifact grades, but the oracle comparison still uses the environment score on both sides.

`compare_arms` applies this rule from the rows' `result_kind`, so a user never picks the source by hand.

team@k follows Test-Time Communication's usage: "a game counts as solved if any of the k agents clears all levels", while on Terminal-Bench "team@2 contributes one shared final state". The fair pairs are:

- **team@k against pass@k or best@k.** Both pick the best of k outputs with knowledge of the target, read from the same oracle value source. They measure whether communication raises the capability present in k agents.
- **final@k against select@k, and against single.** Both apply a rule that does not see the target, scored by the task's scorer, with the same empty rule.
  - They measure what a deployed swarm returns against what k independent attempts with the same selection rule, or one bigger attempt, return.
  - The gap team@k − final@k is what the final step loses.
- **Artifact tasks.** team@k is already non-oracle (one environment). Comparing it with pass@k, as Test-Time Communication does, favours the epochs arm. The non-oracle comparison is select@k with the verifier on each attempt's environment.

All of them are compared at realized cost, with each arm's cap and realized spend reported (swarm.md, [Eval questions](swarm.md#eval-questions-and-the-experimental-design-they-imply)).

Two things the merged designs add to what these quantities mean:

- **Conditional controllers.** A controller may leave members `pending`. swarm-api.md's `escalate`, for example, starts its workers only when the first answer does not verify.
  - k stays the roster size, the arm's configuration.
  - team@k and mean member are over the members that started, and `members_started` is reported beside them.
  - The cost comparison, at realized cost, reflects how many members ran.
  - With `leaderless`, every member starts, so these coincide.
- **Communication (M2).** With the messages channel, a member's submission may repeat what a peer sent it.
  - Per-member scores measure what each member submitted, which is team@k's meaning ("any of the k agents"), not what it derived alone. `vote` counts a copied answer as that member's vote.
  - The communication evidence (`read` and `exposed`, [Evidence and provenance](swarm-communication.md#evidence-and-provenance)) lets analysis tell the cases apart. The record and the scorers need no change for it.

### `best_at(k)`: best of k from n epochs

```python
@score_reducer
def best_at(k: int, value_to_float: ValueToFloat = value_to_float()) -> ScoreReducer: ...
```

- **The estimator.** It is the unbiased without-replacement estimator of the expected maximum of k of the n scored epochs. With the n values sorted ascending as v₁ ≤ … ≤ vₙ, best@k = Σᵢ₌ₖⁿ vᵢ · C(i−1, k−1) / C(n, k).
- **Checked.** For binary values it equals `pass_at(k)`. A script checked both properties: against brute-force enumeration of all k-subsets for 200 random cases (n ≤ 8, graded values), and against Inspect's `pass_at(k)` on binary values.
- **NaN.** NaN epochs are dropped first. If fewer than k remain, the result is NaN, as for `pass_at` (`reducer.py:181-186`).
- **Dict and list values.** Dict values are reduced per key and list values per index, following Inspect's reducer helpers.

### select@k: the selection rule on independent attempts

select@k needs each attempt's eligibility, answer key, verdict and time, which a reducer cannot see. The `baseline()` record has them, so select@k is computed in the analysis helper, not as a reducer.

**An attempt** is one epoch of a `baseline()` arm, read from its record and scores:

```python
@dataclass(frozen=True)
class Attempt:
    epoch: int
    submitted: bool                   # the member's status was "submitted"
    time: float | None                # swarm time when it stopped
    answer_key: str | None
    verdict: Verdict | None
    final_value: float | None         # task scorer's value for this epoch's sample (NaN -> None)
    final_verdict: Verdict | None     # artifact tasks: verifier on the drained environment
```

**The pool** for a sample is its epochs with a final record and a non-NaN `final_value`; others are excluded and counted. With fewer than k attempts in the pool, the sample's select@k is NaN (unscored), as for `pass_at`.

**One subset (answer tasks).**

- Apply the chain to the *submitted* attempts of the subset, with the same rules and tie-breaks as the swarm: verdict value, then time, then epoch in place of roster order.
- Unsubmitted attempts are never selected, as the swarm never selects an unsubmitted member.
- The subset's value is the chosen attempt's `final_value`. Because a `baseline()` attempt's final answer is its own submission, that is the task's scorer on the chosen output.
- When the chain produces nothing (no submissions; strict modes with nothing passing or no keys), the value is `empty_value`, the rule final@k uses.

**Artifact tasks** differ, because every attempt leaves an environment and the swarm arm's team environment is scored whether or not anyone submitted text:

- every attempt in the pool is eligible, submitted or not, and its value is its environment score (`final_value`, the task's scorer on its drained environment);
- the modes read different fields:
  - `verify` uses each attempt's `final_verdict`, the verifier on its drained environment, which is what the swarm arm's own environment check corresponds to;
  - `vote` uses the answer keys of submitted attempts, when the task defines a key;
  - `first` takes the attempt with the earliest stop time;
- the helper appends `first` to the chain if it is not already last, so every subset selects an environment, as the swarm arm always has one. A strict chain therefore has no artifact analogue, and the helper says so in its output when the swarm arm used one;
- `empty_value` is never used for artifact tasks.

**The sample's select@k** is the mean over all C(n′, k) subsets of its pool when that is at most 10,000, otherwise over 10,000 subsets sampled with a fixed seed recorded in the output. The helper also reports, per sample, the fraction of subsets that produced no answer.

**Time** is the record's swarm time, never `EvalSample.working_time`, which includes scoring and excludes waiting ([Usage and timing](#usage-and-timing-during-scoring)). Every `baseline()` attempt starts its swarm clock at zero, so "first" means the shortest time to submit.

`synthesize` has no epoch analogue, because it is a new model call. A swarm using it is compared on final@k against `single`, and against select@k without it.

### The cost boundary (deferred)

Deferred (decision: Ransom, 2026-10-08: "Defer the cost boundary - already an issue with existing inspect evals."). Until it is built, an arm's total is its sample usage with scoring included, as in M1 and in every Inspect eval ([Why M1 needs little](#why-m1-needs-little-from-scoring)). This design proposes no inspect_ai change for it. The analysis and a sketch are kept here for whoever picks it up.

**Why it would matter.** Per-member scoring makes the grading part of sample usage differ between arms. With a model-graded scorer, `member_scores` makes k + 1 times as many scorer calls in a swarm arm as in a baseline arm. Model events cannot separate the solve phase from scoring, because native compaction records usage without one ([Usage and timing](#usage-and-timing-during-scoring)).

**A sketch, if it is built.** The record gains three fields:

```python
class SwarmResultRecord(BaseModel):   # adds, with the cost boundary
    solve_usage: dict[str, ModelUsage] | None       # sample usage when the swarm exited, however it exited
    solve_role_usage: dict[str, ModelUsage] | None  # sample role usage at the same moment
    solve_exit: Literal["returned", "limit", "terminated", "error", "cancelled"] | None
```

- **The snapshot.** `solve_usage` is the sample's cumulative usage (`sample_model_usage()`, and `sample_role_usage()` for roles). It is taken in the swarm's outermost `finally`, however the swarm exits:
  - when it returns, after finalisation (`synthesize` included);
  - when a sample limit, `TerminateSampleError`, an error or a cancellation propagates out of it, after its task group has cancelled and awaited the members.
- **Why it is reliable.** Both reads and the store write are synchronous, so they run under cancellation too, and `solve_exit` records the path. Inspect scores only after the solver has returned or raised (`run.py:2960-3059`), so the snapshot always falls before grading.
- **Coverage.** Sample usage already counts native compaction and excludes response-cache replays, so it is complete where model events are not. The ledger's unknown-cost rules still apply to it: calls cancelled in flight, unpriced models.
- **Placement.** The snapshot covers everything up to the swarm's exit, so the swarm must be the task's last solver step. `swarm()` would document this, because a later solver step's usage would land after the boundary.
- **Scoring usage** is the sample total minus `solve_usage`, reported separately per arm, so the cost of per-member scoring is visible.
- **Samples without a boundary.** A plain agent without `baseline()`, or a sample that failed before the swarm started, has no `solve_usage`. The helper keeps its total under a separate name, `sample_total_usage`, never as solve usage. It excludes the sample from every solve-cost comparison and reports the number excluded per arm. Its scores are still reported.
- **The ledger.** The limits deep dive's ledger would take arm totals from `solve_usage`, follow the same exclusion, and join on (log, sample id, epoch).

### Re-scoring

- **Answer tasks with sandbox-free scorers.** `member_scores` reads only the store record, the sample's input and the target. So `inspect score` reproduces it from the log, and can apply a new scorer to every member's output after the fact, provided the task's registered factory is loadable ([Opting in](#opting-in)).
  - That substitutes for a scorer arm only when no member consulted the task's scorer in the loop; the analysis flags such samples `target_assisted`.
  - In-loop scoring makes the scorer part of the trajectory, so otherwise a scorer change is an arm of its own ([Task identity](swarm-api.md#task-identity-what-makes-two-arms-distinct)).
- **Scorers that need the sandbox** (an answer evaluator that compiles code, any artifact scorer) cannot be re-scored. `sandbox()` raises `ProcessLookupError` and the error propagates, as it does for the task's own scorer today. `member_scores` neither catches it nor reports a partial result.
- **Selection evidence is never recomputed.** Verdicts, keys and times come from the record written during the run; re-scoring never calls a verifier.
- **Old records.** `member_scores` checks `version`. A record with an unknown version is reported as unscored with reason `"unknown_record_version"`, never misread.

### How it appears in logs

| Where | What |
|---|---|
| Sample `output` and last assistant message | The final answer, the chosen member's own `ModelOutput`; message metadata names the mode and member (M1) |
| Sample `store["inspect_swarm.result"]` | The record: every member's output, state, status and time, and the final selection (M1); keys, verdicts, the chain's details and `final_verdict` (Part 2) |
| Transcript | `InfoEvent`s of kinds `member_result` and `final` (M1) and `verdict` (Part 2), with source `inspect_swarm`, at the time they happened; `member_scores`' per-member spans under the scorer span |
| Sample scores | The task's scorer (final@k, or team@k for artifact tasks); the task's member scorer with `best_member` and `mean_member`, per-member detail in its metadata |
| Eval results | One `EvalScore` per task scorer and reducer; the member scorer contributes `best_member` and `mean_member` as separate `EvalScore`s with `scored_samples` and `unscored_samples` counts |
| Eval spec (plan) | `swarm()`'s `result` argument is logged as the type name `"ResultSpec"`, because it holds callables; the record carries the facts. The chain is logged faithfully inside the controller's params, for example `{"type": "controller", "name": "inspect_swarm/leaderless", "params": {"final": ["verify", {"mode": "synthesize", ...}], "stop_on_verified": false}}` |

Nothing here needs a new event type, schema change or viewer change. The viewer shows the store, `InfoEvent`s and dict scores generically.

Inspect applies every epoch reducer to every scorer and key. An epochs arm with `["mean", pass_at(k), best_at(k)]` therefore also shows, for example, `pass_at` of `mean_member`. Those cells are well defined but not meaningful; the analysis helper selects the cells that are.

### How it appears in eval sets

- **Arms as task arguments.** Each arm is the same task with different arguments (`arm`, `k`, `budget`), as in [Opting in](#opting-in). `eval_set` and inspect_flow sweep them, and each arm's log carries its arguments in `eval.task_args`.
- **Identifying arms.** A log whose records have one member is a baseline arm (n is its epochs); more than one member, a swarm arm (k is the member count). Arguments the user names (`arm`, `budget`) label the rows.
- **Same scorers everywhere.** The task's member scorer runs in every arm, so all arms have the same scorer names, and `evals_df` and `samples_df` (`inspect_ai.analysis`) line them up without renaming.
- **What distinguishes arms.** `task_identifier()` hashes the task's arguments and the solver's parameters, including the controller's `final` and `stop_on_verified`. It does not hash scorers, epochs or `result=` (logged as a type name).
  - Arms that differ only in a scorer, in epochs or in the result contract therefore take the difference from a task argument, as `arm` does above.
  - Otherwise `eval_set` refuses them as not distinct ([Task identity](swarm-api.md#task-identity-what-makes-two-arms-distinct)).

### The analysis helper

`inspect_swarm.analysis` adds the scoring part of swarm.md's "thin analysis helpers", in two layers:

```python
def attempt_rows(logs: Sequence[str | EvalLog], *, scorer: str, member_scorer: str) -> list[AttemptRow]: ...
def compare_arms(rows: Sequence[AttemptRow], *, k: int, chain: Sequence[str] | None = None, empty_value: float = 0.0, max_subsets: int = 10_000, seed: int = 0) -> list[ArmScores]: ...
def best_at_k(values: Sequence[float], k: int) -> float: ...
def select_at_k(attempts: Sequence[Attempt], k: int, chain: Sequence[str], *, kind: ResultKind, empty_value: float = 0.0, max_subsets: int = 10_000, seed: int = 0) -> float: ...
```

- **`AttemptRow`**: one per (log, sample id, epoch). It holds:
  - the log path, the arm labels (`eval.task_args`), the member count and `result_kind` (the record's `kind`);
  - the task scorer's value;
  - `final_verdict` (the record's verifier result on the drained environment) and `final` (mode, member, reason);
  - `best_member`, `mean_member` and `complete`;
  - the per-member states, statuses, keys, verdicts, times and submission flags, and `members_started`;
  - `sample_total_usage`;
  - the flag `target_assisted`.

  With the cost boundary, a row also has `solve_usage`, `solve_exit` and scoring usage. A row without them is flagged `no_solve_usage` and left out of solve-cost comparisons. The ledger and the limits deep dive's curves join on (log, sample id, epoch).
- **`ArmScores`**: one per arm. It holds:
  - the arm's labels, `arm_kind` (swarm or baseline), `result_kind`, and k or n;
  - each quantity of [Comparison quantities](#comparison-quantities) that applies to the arm, as a mean over samples with its count of scored samples and of excluded samples.

  `chain` defaults to the chain recorded in the swarm arm's records.
- **From rows to attempts.** For a baseline arm, `compare_arms` builds one `Attempt` per row:
  - `epoch`;
  - `submitted`, `time`, `answer_key` and `verdict` from the row's single member: its status `submitted`, its end time, its key, its run-time verdict;
  - `final_value` from the task scorer's value;
  - `final_verdict` from the row's `final_verdict`.

  It passes the arm's `result_kind` to `select_at_k` as `kind`.
  - The run-time verdict and `final_verdict` are kept apart, because the environment can change between a member's submission check and the drain. Artifact `verify` reads only `final_verdict`. Nothing derives a verdict from `final_value`, which would select with the target.
  - An arm whose rows disagree on `result_kind`, or whose `result_kind` differs from the swarm arm it is compared with, is an error naming the logs.
- **Target-assisted samples.** `target_assisted` is set when the solve phase contains `ScoreEvent(intermediate=True)`: an agent consulted the task's scorer with the target in the loop (for example `react(attempts=...)`). In a swarm that feedback can be shared with peers, so the helper reports those samples separately.

### Testing after M2

All runtime tests use mockllm with scripted outputs, need no network or Docker, and run on asyncio and trio unless marked. Each piece's tests come with that piece.

- **Chain selection** (table-driven, over lists of member results):
  - `verify`: best verdict value, `None` values, ties by time then roster, none passed;
  - `vote`: plurality, `None` keys abstain, ties by earliest, one vote per member;
  - fall-through in the default chain, and the strict single mode leaving an empty answer with its reason;
  - never-started members are never selected;
  - construction errors for each missing need, raised by `swarm()` for the controller's chain and by `leaderless()` for a `Reporter`.
- **Chain types** (`tests/test_final.py`, beside swarm-api.md's logging test of the same chain):
  - `final_chain()` builds the default from the result;
  - a chain mixing strings, `synthesize(model=get_model(...), prompt=...)` and `reporter(...)`, logged through a controller's params, validates back from the logged JSON and from the dicts `create_registry_object()` passes, with the `Model` rebuilt;
  - two arms differing only in a `synthesize` prompt get different `eval_set` identifiers.
- **Provisional answer** follows the chain without `synthesize` after each submission and verdict.
- **Verifier.**
  - It is called once per submission, one at a time, never with the target.
  - Its exception fails the sample. Neither scoring nor re-scoring calls it.
  - Its verdict is on the member's `MemberEnded` and handle as a `VerdictSummary` before the controller sees the event, and no event or handle field contains the explanation.
- **`synthesize`.**
  - The prompt holds each submission in a `<member_submission>` envelope, with nested marker fragments removed, a roster name containing `"` and `<` escaped, and no tool calls or messages.
  - The shared fence code gives the same results on the communication design's `<peer_message>` cases.
  - A provider error, a refusal and an empty output fall through the chain with `synthesize_error` set.
  - A sample-level `LimitExceededError`, `TerminateSampleError` and cancellation propagate, the sample records its limit, and the provisional answer is the output.
  - An `ExceptionGroup` holding a sample limit (from a model API that uses a task group), and a mixed group of a limit and an ordinary error, propagate whole, on both backends.
- **Record additions.** Keys, verdicts and chain fields are written as specified. An M1 record reads with them absent.
- **Artifact `final_verdict`** is taken after the drain on the final environment, with the final completion or `""` when there is no final answer. It differs from a run-time verdict when a member edits after a passing check.
- **`member_scores`** with `match()`:
  - per-member values, `best_member` and `mean_member`; empty outputs get `empty_value` and never reach the scorer in answer tasks;
  - an inner scorer returning `None`, `Score.unscored()` and a dict value gives unavailable members with their reasons; mixed and all-unavailable cases; every roster permutation gives the same aggregates;
  - artifact kind without artifacts, kind mismatch and a missing contract (including an M1 record) give NaN with the right reason;
  - a plain agent is scored as one member with its key and no verdict;
  - never-started members get the outcome `not_started`, no value and no scorer call, and leave `complete` true; `members_started` counts the others;
  - the copies do not appear in `state.scores`.
- **In-loop guard** (the `member_scores` case of swarm-api.md's `tests/test_scoring_loop.py`, which runs it inside a swarm). A `react(attempts=3)` agent with `member_scores` listed after the task scorer gets the same feedback and the same number of task-scorer calls as without it, and `member_scores` returns `None` while `completed=False`.
- **Answer attribution.** A Frontier-CS-shaped scorer with a sandbox fallback, given a member completion holding an empty fenced block, is not credited with the team's files when `member_scores` uses the answer-only adapter.
- **Re-scoring.**
  - In a fresh process, default scorer resolution (`resolve_scorers()` then `score()`, as `inspect score` does) rebuilds the task's member-scorer factory and reproduces its scores for a sandbox-free answer scorer.
  - Programmatic re-scoring with freshly built scorers does the same.
  - A sandbox-dependent scorer raises `ProcessLookupError`.
- **`best_at(k)`** against brute-force enumeration (a table of small cases), equality with `pass_at(k)` on binary values, NaN with fewer than k scored epochs, and dict values per key.
- **`select_at_k`** for answer tasks, against a reference built from the runtime selector:
  - exact enumeration on small pools, and the seeded sample above 10,000 subsets;
  - unsubmitted attempts never selected, and subsets with no answer valued at `empty_value`;
  - pools smaller than k giving NaN;
  - `first` by record time, unchanged when the grader's duration is varied.
- **Artifact oracle source.** Take an artifact task with no `member_artifacts`, run as `baseline()` epochs with environment scores `[0, 1]`.
  - The task scorer gives pass@2 = 1 and a valued best@2, while `best_member` is NaN.
  - `compare_arms` reports those values for the baseline, and the environment score as team@k for the swarm arm.
- **Rows to attempts, end to end.** From written logs through `attempt_rows` and `compare_arms`:
  - an artifact baseline whose run-time verdict passed but whose `final_verdict` failed (and the reverse) is selected by `final_verdict` only;
  - `result_kind` survives into `select_at_k`;
  - mismatched kinds across arms raise.
- **Artifact results ignore answer text.** Use an artifact-shaped scorer, which scores the environment and ignores the completion.
  - A passing environment and an empty final output give final@k and team@k 1, not `empty_value`.
  - A member with an empty completion and a passing materialized artifact is graded 1.
  - Artifact select@k selects unsubmitted attempts, uses `final_verdict`, and always selects an environment.
- **`member_artifacts`.** With the local sandbox, and files standing in for branches:
  - the team score runs before any `materialize`;
  - each member is graded with its artifact in place;
  - the team state is restored on exit and on a scorer error.

  A git-worktree layout test needs Docker and is marked slow, following inspect_ai's conventions.
- **The cost boundary**, if built:
  - `solve_usage` excludes a model-graded scorer's usage and a scorer's native compaction (mockllm compaction usage);
  - it includes the swarm's own compaction and `synthesize`, and counts cache replays as Inspect does;
  - a sample ended by an outer limit, with a grader that makes model calls and compacts, still has `solve_usage` (with `solve_exit="limit"`) equal to the solve's usage alone; the same holds for termination and cancellation;
  - a plain-agent sample has no `solve_usage`, and is excluded from solve-cost comparisons and counted.

### Implementation plan after M2

Optional, after M2, in any order with swarm.md's menu, subject to the dependencies at the top of this part. One PR per step:

1. **Result contract and record additions.**
   - `src/inspect_swarm/_result.py`: `ResultSpec`, `answer_result`, `artifact_result`, `Verdict`, `Verifier`, `MemberArtifacts`.
   - `swarm(result=)`, with swarm-api.md's `TypeError` for the string a replay passes.
   - The record's added fields.
   - The runtime verifies submissions, writes `verdict` events, and puts each verdict's `VerdictSummary` on the handle and `MemberEnded` before queuing the event (`_swarm.py`, `_controller/_control.py`, `_evidence.py`).
   - Exports in `src/inspect_swarm/__init__.py`. Tests in `tests/test_result.py`.
2. **Final-answer chain.**
   - `src/inspect_swarm/_final.py` gains `FinalName`, `Synthesize`, `Reporter`, `FinalMode`, `FinalSpec` and `final_chain()`.
   - Validation against the result, called from `swarm()`'s construction checks.
   - `leaderless(final=, stop_on_verified=)` and `Controller(final=)` (`_controller/_controller.py`, `_controller/leaderless.py`); `leaderless()` validates its `final` with the `TypeAdapter`.
   - The chain in the provisional answer and finalisation, including the artifact `final_verdict`.
   - Tests extend `tests/test_final.py` and `tests/test_controller.py`.
3. **`synthesize`.**
   - In `_final.py`, with its prompt template in `src/inspect_swarm/_prompts.py` and its failure classes.
   - The fence code shared with M2's read tool, in `src/inspect_swarm/_fence.py`.
   - Tests extend `tests/test_final.py`; `tests/test_fence.py`.
4. **Scorers and baselines.**
   - `src/inspect_swarm/scorer/__init__.py`, `scorer/_member_scores.py` (`member_scores` and `MEMBER_METRICS`; answer kind; artifact kind returns unavailable) and `scorer/_best_at.py`.
   - `baseline()` in `_swarm.py`.
   - Tests in `tests/scorer/`, including the re-scoring test with a task-file factory, and the `member_scores` case of `tests/test_scoring_loop.py`.
5. **Analysis.** `src/inspect_swarm/analysis/__init__.py` and `analysis/_scores.py`: `attempt_rows`, `compare_arms`, `best_at_k`, `select_at_k` and the target-assisted flag. Tests on synthetic logs in `tests/analysis/`.
6. **Per-member artifacts for shared-artifact tasks.** `member_artifacts` in `member_scores`, and a worked SWE-bench-shaped example layout in the docs.
7. **The cost boundary**, if wanted. The snapshot fields, and the solve/scoring split in the analysis.

## Alternatives considered

**Keep the chain, the result contract and per-member scoring in M1.** This document's drafts before 2026-10-08 did this. Existing scorers already work with the swarm as the agent, and nothing M1 keeps needs a verifier, an answer key or per-member scores. Moved after M2 (decision: Ransom, 2026-10-08).

**Keep `final=` in M1, accepting only `"first"`.** This would match swarm-api.md's signature from the start. But with one valid value, the parameter configures nothing. Adding it later with a default that reproduces `first` changes no M1 swarm's answer. Not chosen.

**Pass `ResultSpec` to a registered `member_scores` scorer.** The obvious shape, and the first draft's. But Inspect logs a plain dataclass argument as its type name, so default `inspect score` rebuilds the scorer with a string in place of the spec. Making the spec a registered object would need a new registry type in inspect_ai. A task-owned no-argument factory uses existing machinery and keeps the spec's callables in the task's code. Chosen.

**Compute per-member scores inside the swarm.** The swarm would call the task's scorer at the end of the solve. But scorers belong to the task, and they need the target, which the solver should not use. Their usage would land in the solve, and re-scoring would miss them. Rejected.

**Replace the task's scorer with a swarm scorer** that returns `{"final", "best_member", "mean_member"}`. One scorer for everything. But the headline score would then differ in name and shape between arms, and a task's metrics would change. Rejected.

**A separate scorer per member** (`member_1`, `member_2`, …). The number of scorers would vary with k, so arms would not line up, and "member 3" means nothing in a leaderless swarm. Per-member detail lives in metadata instead.

**Plain agents as baselines, with evidence computed at scoring time.** The first draft ran the verifier in `member_scores` for arms without a swarm, and timed attempts by `working_time`. This had three problems:

- re-scoring would rerun a possibly sandbox-dependent verifier;
- `working_time` includes scoring, so a slow grader can reverse which attempt counts as first;
- a limit-truncated output had no eligibility flag.

Running baselines as a swarm of one records the same evidence at run time under the same rules. Chosen. Plain agents remain scoreable but are left out of select@k.

**Selection comparisons as epoch reducers** (`select_at(k)`). This does not fit:

- a reducer sees one scorer's scores, so the keys and verdicts would have to ride in score metadata;
- Inspect would apply the reducer to every other scorer too;
- the subset estimator with sampling does not fit a reducer.

`best_at(k)` is a closed form on values alone, so it is a reducer; select@k is in the helper.

**Voting with Inspect's `mode`/`majority` reducers.** They vote on correctness values, not on answers. Rejected.

**Solve cost by subtracting scoring model events** (for the deferred cost boundary). Native compaction during scoring records usage with no event, so it would be counted as solve cost. A usage snapshot at the swarm's exit is complete. Preferred, if the boundary is built.

**Snapshot only on a normal return, falling back to the sample total** (for the deferred cost boundary). Budget exhaustion is a normal way for an arm to end, and Inspect still grades those samples. The fallback total would then include grading, which differs between arms. A snapshot in the swarm's `finally` gives limit-ended samples a boundary too.

**Strict modes only** (verify yields nothing when nothing verifies). Simpler, and it measures the verifier alone. But it makes the default swarm return nothing where an attempt would otherwise be scored on its answer. Strict behaviour stays available as `leaderless(final="verify")`. See [Open question 1](#open-questions).

**`final=` on `swarm()`** (this design's drafts before swarm-api.md merged). swarm-api.md moved it to the controller, because the valid modes and the default depend on the topology; the chain's semantics are unchanged. Adopted.

**Count never-started members as empty** (value `empty_value`). Every roster member would then have a value. But a conditional controller's choice not to run a member would read as that member failing, lowering `mean_member` because of a scheduling decision, not any output. Not started is its own outcome, with `members_started` reported.

**Score unsubmitted outputs as the last fallback.** This matches how Inspect scores a plain agent that hits its limit. But it selects working messages as answers, and swarm.md decided that nothing is invented. Per-member scores still score those outputs.

**Treat any unavailable member as making the sample unscored.** Simple and conservative, but one failed model grade among k would discard the sample's other grades. Aggregating over valued members with a `complete` flag keeps the data and lets analysis choose.

**Per-member sandboxes for per-member artifacts.** Clean attribution, and the isolation epochs have. But it changes the default topology Ransom decided (a shared sandbox), and the shared filesystem is M1's channel. It stays [later work](swarm.md#sandbox-topology); `materialize` covers the shared default.

## Compatibility and migration

- **inspect_swarm** has no released API; everything here is new. Python 3.11+ as decided.
- **Eval logs.** Only existing structures are used: a store key, `InfoEvent`s, a message metadata key, and from Part 2 scores with dict values and metadata. Old readers and the viewer show them generically. The store record and the `InfoEvent` payloads carry `version: 1`. Part 2's record fields are optional additions under the same version.
- **Logged parameters across versions.** When Part 2 adds `result=` to `swarm()` and `final`/`stop_on_verified` to `leaderless()`, logged plan params gain those keys. A run's `task_identifier()` can then differ between inspect_swarm versions for the same task, as it can for any solver that gains a parameter. Arms in one eval set run one installed inspect_swarm, so they are unaffected.
- **Names** (`member_scores`, `MEMBER_METRICS`, `best_member`, `mean_member`, `best_at`, `baseline`) become an interface the analysis and users' notebooks depend on once released; changing them later is a breaking change. The member scorer's own name is the task's.
- **inspect_ai.** No change beyond swarm-api.md's two registry types.
  - M1 uses the store, `transcript().info` and the member wrapper's reference to its `AgentState`.
  - Part 2 adds public scorer and reducer APIs and `registry_value()` for the `Synthesize` model field. If the cost boundary is built, it also uses `sample_model_usage()` and `sample_role_usage()`, which are private and which inspect_swarm may use.
- **Tasks.** In M1, tasks need no change: their scorers score the swarm's output. From Part 2, tasks opt in. A task that does not pass `result=` behaves as in M1, with `first` and no per-member claims. A task whose scorer can read the answer from the environment must declare `kind="artifact"` or supply an answer-only adapter.
- **swarm.md.** This PR makes small edits there where this document refines it:
  - M1's final answer is `first`; the chain, the result contract and the scorers are listed after M2;
  - team@k's definition and its fair pairs;
  - the chain's fall-through and the strict single mode;
  - the provisional answer, kept by the runtime, and its test bullet;
  - arm totals include scoring in M1, with the boundary deferred;
  - links to this document.
- **swarm-overview.md.** The same main decision, in the overview: M1 builds `first` only, and the result contract, the chain and per-member scores come after M2.
- **swarm-api.md.** This PR marks the parts that exist only for Part 2 as after M2, and changes nothing else:
  - `result=`, `leaderless()`'s `final` and `stop_on_verified`, `Controller(final=)` and the verdict fields;
  - their validation rules;
  - the verifier-based examples and tests;
  - the `member_scores` case of `tests/test_scoring_loop.py`, which moves from its step 4 to its later step.

  The shapes themselves are unchanged. As before, this design defines what swarm-api.md left to it: the pydantic `FinalMode` types with their discriminator and model serialiser, and how scoring treats members a controller never started.

## Security

Untrusted input reaching the new code:

- **Member outputs are model output.**
  - In M1 they reach only the record, the evidence events (as member names and statuses, without text) and the returned output, exactly as a single agent's output reaches the task state. No harness-composed prompt contains member text in M1.
  - After M2 they also reach:
    - `answer_key`, task code that must accept any text and return `None` rather than raise on unparseable input;
    - the verifier, which may execute the answer (code) and must do so only inside the sandbox;
    - the member scorer, as they would from a single agent, and only after the solve (`completed=True`);
    - `synthesize`'s prompt, in the communication design's envelope with harness-written attributes and markers stripped, in a separate call outside every member's context. That is weaker than tool output, so `synthesize` is never a default.
- **Answer attribution.** A member's output must not be credited with the team's environment. The answer-scored contract forbids scorers that read the answer from the environment, and the empty-output backstop keeps the commonest case from reaching the scorer at all.
- **The target.** Neither the verifier, nor `answer_key`, nor any member tool is given the target. Agents that consult the task's scorer in the loop are flagged by the analysis.
- **Controllers.** A controller sees no member text (swarm-api.md). After M2 it sees a verdict only as `VerdictSummary(passed, value)`. The verifier's explanation may quote the answer or a judge model, so it stays in the record, and a controller cannot relay member text through a verdict.
- **Shared-sandbox attribution.** In a shared sandbox, any member can alter another's worktree or branch, so per-member artifact scores are attributions by convention.
  - `materialize` receives only roster names, never member-chosen names, so a member cannot steer which path is materialized.
  - Detecting cross-writes relies on tool-call evidence and its stated limit.
- **Logs.** Outputs and answer keys are stored as JSON data in the store and in score metadata, not as Markdown.
- **Reward hacking.** Members can read whatever the sandbox holds, scorer test files included, exactly as a single agent can. Nothing here adds or removes that exposure.

## Open questions

1. **Does the default chain fall through?** This is a Part 2 question, needed only when the chain is built; M1 has no chain to decide.
   - With a verifier and an answer key, the default is `["verify", "vote", "first"]`.
   - This design falls through when a mode yields nothing: nothing verified, so vote; no keys, so first.
   - The alternative reads the decision as choosing one mode by what the task supplies, so nothing verified means no answer.

   The options:
   - (a) Fall through (this design). The swarm returns an answer whenever anyone submitted. The strict reading stays available as `leaderless(final="verify")`.
   - (b) Strict. It measures the verifier alone, and returns no answer more often.

   Recommendation: (a). Swarm.md's text on the post-M2 provisional answer and its test bullet match (a), applying the strict reading to `leaderless(final="verify")` only.

## Not this design

- **The final-answer reserve in M1** (the limits deep dive). With `first`, M1's finalisation makes no model call. The reserve swarm.md sizes for the final-answer step therefore has no final-step call to cover until `synthesize` exists. Whether M1 keeps a reserve for other reasons is the limits deep dive's call.
- **`best_at(k)` in inspect_ai.** It generalises `pass_at(k)` to graded scores and belongs beside it; propose it upstream once it has been used here.
- **Per-scorer epoch reducers** (inspect_ai). Today every reducer applies to every scorer and key, which fills logs with meaningless cells such as `pass_at` of `mean_member`.
- **A no-recovery option for Frontier-CS's scorer** (inspect_evals), so swarm tasks need no adapter.
- **Re-scoring with a sandbox.** Inspect cannot re-score sandbox scorers; snapshots of the final environment would be needed.
- **Per-member trajectory scoring.** Scorers that read tool calls or intermediate messages would need each member's conversation in a scorer-readable form.
- **Harness-validity and cost as scores.** Showing a member that never acted, or realized cost, as `Score`s so they appear in eval-set summaries (Observer and limits work).
- **A verifier library.** Common verifiers (run visible tests, check a format, a model judge with no target) as reusable functions.
