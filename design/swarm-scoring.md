# Inspect Swarm: scoring

Status: proposed, 2026-10-08. Issue: none. Author: agent (Claude), reviewed by Codex; see the PR.

A deeper dive on one topic of [swarm.md](swarm.md): how a swarm's results are scored. It details [Results and scoring](swarm.md#results-and-scoring-a-task-owned-contract) (the task-owned result contract) and the final-answer modes of [Controller](swarm.md#controller-topology-termination-final-answer). It covers answer-scored and shared-artifact tasks, per-member submissions, the comparisons with epochs (team@k, best@k, pass@k), the final-answer chain, verifiers, `synthesize`, per-member artifacts in a shared sandbox, what inspect_swarm ships for scoring, how all of it appears in logs and eval sets, and how a task opts in.

It respects the decisions recorded in swarm.md (Ransom, 2026-10-07): Python 3.11+, inspect_ai internals may be used, limits stay soft, members share the sample sandbox by default, M1 then M2 then the rest in any order. Sibling deep dives cover limits and the ledger, ORBIT alignment and inter-agent communication. This document refers to them and does not design them.

Code references are to inspect_ai `main` at `215cf087` (2026-10-08) and inspect_evals `main` at `a73e6d99` (2026-10-08). Paths are relative to each repository's root.

## Why

Swarm.md fixes the principle: what counts as a member's result belongs to the task, because Inspect scorers take a `TaskState` and a target and may inspect the sandbox. It leaves open everything an implementer needs to build it:

- **What the task supplies, and where.** The swarm (a solver) selects a final answer at run time. Scorers run afterwards, and again when a log is re-scored. Both need the same facts about the task: whether answers are comparable, whether there is a verifier, whether members leave their own artifacts.
- **What "team@k" means.** The paper swarm.md cites defines team@k as "any of the k agents" solving the task, not the team's single final answer ([Test-Time Communication](https://arxiv.org/abs/2609.21032), ARC-AGI-3 and Terminal-Bench sections). An experiment that compares the swarm's *selected* answer against an *oracle* best-of-k from epochs, or the reverse, compares different things.
- **How the comparisons are computed.** Inspect ships `pass_at(k)` for binary scores and `max` for best-of-all-epochs, but nothing for best-of-k on graded scores, and nothing that applies a selection rule (verify, vote) to k independent attempts.
- **What scoring does to the cost comparison.** Per-member scoring runs a scorer k more times. Scorer model calls are added to the sample's usage, which swarm.md uses for arm totals (verified below).
- **What happens in the shared sandbox.** A shared-artifact scorer examines one environment. Some scorers that look answer-scored fall back to reading the environment, which would credit the team's work to a member.

Getting these wrong produces numbers that look comparable and are not. That is the failure the result contract exists to prevent.

## Goals and non-goals

### Goals

- One task-supplied contract, read by both the swarm and the scorers, that says what kind of result the task has and what it offers for selection (answer key, verifier) and for per-member scoring (member artifacts).
- Every member's result, and the run-time evidence selection needs, recorded in the sample, so scorers and the analysis read the same facts during the eval and after re-scoring.
- Baseline arms that produce the same record as the swarm arm, so comparisons use one clock, one eligibility rule and one usage boundary.
- A precise final-answer chain with the decided default (verify, else vote, else first) and an explicit `synthesize`.
- Named, defined comparison quantities (final@k, team@k, mean member, pass@k, best@k, select@k), each paired with its fair counterpart, and the code that computes each.
- Per-member scores in shared-artifact tasks when, and only when, the task provides per-member artifacts.
- Scores that appear in logs and eval sets as ordinary Inspect scores and metrics, needing no viewer or schema change.

### Non-goals

- Changing any task's own scorer. The task's scorer stays the headline score in every arm.
- Scoring trajectories per member: scorers that read tool calls or intermediate messages, rather than the answer, are not scored per member.
- Defining the realized-cost ledger, the reserve or the swarm cap (the limits deep dive). This document defines only the usage boundary between solving and scoring.
- Harness-validity checks and swarm metrics as scores (swarm.md's [Observer](swarm.md#observer-evidence-accounting-and-metrics)).
- `reporter` and coordinator topologies, beyond reserving the name.
- Per-member sandboxes ([Sandbox topology](swarm.md#sandbox-topology) later work).

## Current behaviour

What the design depends on, in inspect_ai unless noted.

### Where scoring runs

- Scorers run once per sample after the solver, sequentially, in task order, inside a `scorers` span (`src/inspect_ai/_eval/task/run.py:3059-3115`). `state.completed` is set to `True` first (`run.py:3025`).
- They run inside the sample's sandbox context, so `sandbox()` still reaches the environment the agents left (`run.py:2497-2512`).
- Each scorer gets the same `TaskState` object, and its result is added to `state.scores` after it returns. A scorer that writes its own entry there is an error (`run.py:3082-3087`).
- Scoring gets half the sample's time limit (`run.py:3034`).
- Scores a solver writes into `state.scores` before scoring are also recorded as sample scores (`run.py:3039`, `:3118-3135`).

### Scores, dict values and metrics

- `Scorer` returns `Score | None` (`src/inspect_ai/scorer/_scorer.py:34-40`). A `Score` has `value`, `answer`, `explanation`, `reason` and `metadata` (`src/inspect_ai/scorer/_metric.py:118-142`). `Score.unscored()` sets the value to NaN, which metrics and reducers skip (`_metric.py:164-180`). Model graders and `expression_equivalence()` return it when they cannot grade (`src/inspect_ai/scorer/_model.py:348`, `src/inspect_ai/scorer/_math.py:1322`).
- A scorer may return a dict value and declare metrics per key, `@scorer(metrics={"key": [mean()]})` (`_scorer.py:133-136`). Each key becomes its own `EvalScore` named after the key, and a NaN under a key counts that sample as unscored for that key only (`src/inspect_ai/_eval/task/results.py:488-600`, `:523-534`).

### Scorer arguments in the log and re-scoring

- A registered scorer's arguments are logged as JSON. A registry object with parameters becomes a `{type, name, params}` dict that is rebuilt on replay; a plain object, such as a dataclass holding callables, becomes its type name, and a callable becomes its `__name__` (`src/inspect_ai/_util/registry.py:218-247`, `:660-706`).
- Default `inspect score` rebuilds each scorer from its logged name and options, loading the task file when the scorer was defined there (`src/inspect_ai/_eval/score.py:600-640`). So a scorer survives re-scoring when its factory is registered and its logged arguments rebuild it. I checked this with a spike: a no-argument `@scorer` factory, defined in the task file, that built its scorer from a dataclass of callables, was rebuilt in a fresh process by `resolve_scorers()` and `score()` and gave the same score and metadata.
- Re-scoring rebuilds a `TaskState` from the logged sample, including `store`, `metadata` and `output`, with `completed=True` (`score.py:461-475`). There is no sandbox: `sandbox()` raises `ProcessLookupError` (`src/inspect_ai/util/_sandbox/context.py:55-63`).

### Epochs and reducers

- `Epochs(n, reducer=[...])` applies every reducer to every scorer's per-epoch scores (`src/inspect_ai/_eval/task/epochs.py:4-29`). A reducer sees only `list[Score]` for one sample and one scorer (`src/inspect_ai/scorer/_reducer/types.py:7-14`). There is no per-scorer reducer.
- `pass_at(k)` is the unbiased without-replacement estimator 1 − C(n−c, k)/C(n, k) for n ≥ k epochs, NaN when fewer than k are scored (`src/inspect_ai/scorer/_reducer/reducer.py:163-205`).
- `max` is best-of-all-n epochs (`reducer.py:247-301`). There is no best-of-k estimator for graded scores.
- `mode` and `majority` take the most common score *value* (`reducer.py:12-82`). They vote on correctness, not on answers, so neither is majority voting over the agents' answers.

### Usage and timing during scoring

- Model calls a scorer makes are added to the sample's usage. I checked this with a spike: a solver that made no model call and a `model_graded_qa` scorer on mockllm gave the sample `model_usage` of 223 tokens, all of them the grader's. The `ScoreEvent` records a snapshot of sample usage after each scorer (`run.py:3102-3104`).
- Native compaction adds usage with no model event (`src/inspect_ai/model/_model.py:1340-1361`), so model events alone cannot separate solve usage from scoring usage.
- So `EvalSample.model_usage`, which swarm.md uses for each arm's total ([Accounting](swarm.md#observer-evidence-accounting-and-metrics)), includes scoring.
- `EvalSample.working_time` is computed when the sample is logged, after scoring and cleanup, and excludes waiting (`run.py:3164`, `:3456`, `:3490`). It is not when an attempt finished solving.

### In-loop scoring

- `score(state)` runs every task scorer, with the target, during the solve and records `ScoreEvent(intermediate=True)` (`src/inspect_ai/scorer/_score.py:14-80`). The state it scores has `completed=False`.
- Callers include `react(attempts=...)` (`src/inspect_ai/agent/_react.py:330`), synchronous `deepagent()` attempts through `react()`, `basic_agent` (`src/inspect_ai/solver/_basic_agent.py:234`), the human agent's `/score` (`src/inspect_ai/agent/_human/commands/score.py:58`), and inspect_swe's Claude Code and Codex attempt loops (`inspect_swe: src/inspect_swe/_claude_code/claude_code.py:621`, `src/inspect_swe/_codex_cli/codex_cli.py:749`). `react()` reads only the first score (`_react.py:330-333`). This is target-informed feedback, not a verifier.

### Agent output and member state

- `react()` updates the `AgentState` it was given in place: each generation sets `state.output` and appends to `state.messages` (`_react.py:770-771`). A submission sets `output.completion` to the answer (`_react.py:303-309`).
- `run()` copies its input into a new `AgentState` and returns it, or returns it with the `LimitExceededError` when one of its own limits fired (`src/inspect_ai/agent/_run.py:75-111`). A cancellation propagates and the state is not returned.
- `as_solver()` keeps a reference to the agent's state so that it can copy the output to the task state even when an exception ends the agent (`src/inspect_ai/agent/_as_solver.py:65-80`). A plain single agent that hits its limit is therefore scored on its last output.
- A `LimitExceededError` raised inside the solver is recorded as `EvalSample.limit` only when it reaches the runner (`run.py:2992-3001`).

### Shared-artifact scorers in inspect_evals

- SWE-bench diffs the repository against the start commit and runs the test script in the sandbox; the value depends only on the environment (`inspect_evals: src/inspect_evals/swe_bench/scorers.py:28-88`).
- Frontier-CS extracts code from `output.completion` and compiles and runs it in the sandbox. When the *extracted code* is empty it recovers code from the sandbox's files (`inspect_evals: src/inspect_evals/frontier_cs/scorer.py:672-676`, `:631-660`). Extraction returns the last fenced block's contents (`scorer.py:38-69`), so a non-empty completion holding an empty fenced block also triggers the recovery. The unchanged scorer therefore reads the answer from the environment in some cases: it is not answer-scored in this design's sense.

## Design

### Terms

- **Member output.** The member's `AgentState.output` when it stops, however it stops: what Inspect would have scored had that member been the only solver. The member runner keeps a reference to the member's `AgentState`, as `as_solver()` does, so the output survives the member's own limit and the drain. For agents that do not update their state in place, a cancelled member's output is empty.
- **Submission.** A member output from a member that finished normally (status `done`). For `react()` that is a submit; for a bridged agent it is the agent returning. A member stopped by its own limit, an error or the drain has an output but no submission. Each member submits at most once, because `react()`'s internal attempts are not visible outside it.
- **Final answer.** What the swarm returns as its `AgentState.output`, chosen by the final-answer chain from the submissions.
- **Answer key.** The task's comparable form of an answer, used for voting.
- **Swarm time.** Seconds on the controller's clock (`anyio.current_time()`) from the swarm's start. Every ordering by time, in the swarm and in the analysis, uses it.

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

**`kind="answer"`** asserts that the scorer given to `member_scores()` is a function of the answer: `output`, the last assistant message, the sample's input, metadata and target. It may use the sandbox as a fixed evaluator (compile and run the answer), but never as a source of the answer. A scorer that reads the trajectory, or that can read the answer out of the environment, is not answer-scored. The unchanged Frontier-CS scorer is one: its recovery path reads the environment. Such a task either declares `kind="artifact"`, or passes `member_scores()` an answer-only adapter of its scorer that scores empty extracted code as zero instead of recovering it ([Answer-scored tasks](#answer-scored-tasks-per-member-scores)).

**`kind="artifact"`** asserts that the scorer examines the environment the agents leave behind (SWE-bench). The team has one result.

**`answer_key(text) -> str | None`** maps an output's completion to a comparable key: extract and normalise, for example a boxed number. It returns `None` when there is no comparable answer. It never sees the target. It is task code, so an exception from it propagates and fails the sample; a key function that cannot parse should return `None`.

**`verifier(answer, metadata) -> Verdict`** is the task's in-loop checker: visible tests, a proof checker, a format check, a model judge. It gets the answer text and the sample's metadata, and may use `sandbox()`. It is never given the target. The swarm cannot stop a verifier from reading the target through private state, so this is a documented contract. A verifier must not call `score()`, which runs the task's scorers with the target. A verifier exception propagates and fails the sample, because silently degrading `verify` to `vote` would change what is measured. Verifiers run only during the solve ([When verdicts are taken](#the-final-answer-chain)); scoring and re-scoring read the recorded verdicts and never call a verifier.

For audit, the record stores a label for the verifier: its `name` attribute if it has one, else its `__qualname__`, else its class's `__qualname__`. It is a label, not a reconstruction; the verifier's code is the task's.

**`member_artifacts.materialize(member)`** is an async context manager that puts that member's artifact where the scorer looks for the team artifact, and restores the team artifact on exit, including on error. See [Shared-artifact tasks](#shared-artifact-tasks-and-per-member-artifacts).

A swarm without `result=` still runs. Its chain is `first`, its record says `kind: null`, and `member_scores()` reports per-member values as unavailable. Omitting the contract never produces a per-member claim.

### Opting in

The spec holds callables, which Inspect cannot log or rebuild as a scorer argument ([Current behaviour](#scorer-arguments-in-the-log-and-re-scoring)). So the task keeps the spec in one function, passes it to `swarm()` directly, and wraps `member_scores()` in a **registered, no-argument scorer factory of its own**, which re-scoring rebuilds by name:

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
        swarm(members=member(agent, count=k), budget=cost_limit(budget), result=frontier_math_result())
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

- `member_scores(scorer, result, *, value_to_float=value_to_float(), empty_value=0.0) -> Scorer` is a plain builder, not itself registered. `MEMBER_METRICS` is `{"best_member": [mean(), stderr()], "mean_member": [mean(), stderr()]}`. Both are exported from `inspect_swarm.scorer`.
- If the factory needs parameters, they must be values Inspect logs and rebuilds (strings, numbers, registered objects), and the factory builds the spec from them.
- The task's own scorer stays first and unchanged, so the headline score is the same scorer in every arm.
- **Baselines run as a swarm of one.** `baseline(agent, result)` is `swarm(members=member(agent), channels=[], final="first", result=result)`: swarm.md's natural baseline. Every arm then writes the same record, with the same clock, eligibility rule, run-time verdicts and usage boundary, which select@k and the cost comparison need. A plain agent without `baseline()` still gets `member_scores` values (its output is scored as one member), but no run-time evidence, so the analysis leaves it out of select@k and cost comparisons and says so.
- In practice the swarm's cap sits below the sample limit by the final-answer reserve (the limits deep dive); the example leaves that out.

A swarm of one returns an empty output when its member never submits, where a plain agent would be scored on its last output. That is the same conservative rule the swarm arm follows ([No submissions](#the-final-answer-chain)), so the arms stay comparable with each other, if not with a plain agent.

### What the swarm records

The controller keeps one record in the sample store under the key `inspect_swarm.result`, as `model_dump(mode="json")` of:

```python
class MemberResult(BaseModel):
    name: str                         # roster name, assigned by the eval author or controller
    role: str | None
    model: str | None
    status: Literal["running", "done", "limit", "errored", "cancelled"]
    output: ModelOutput | None        # member output (see Terms)
    time: float | None                # swarm time when the member stopped
    answer_key: str | None            # result.answer_key(output.completion), when defined
    verdict: Verdict | None           # verifier result for a submission, when a verifier exists
    verdict_time: float | None        # swarm time when the verdict was taken

class FinalRecord(BaseModel):
    chain: list[str]                  # e.g. ["verify", "vote", "first"]
    mode: str | None                  # the mode that produced the answer
    member: str | None                # the member whose output was chosen; None for synthesize
    reason: str | None                # why there is no answer, e.g. "no_submissions", "none_verified"
    votes: dict[str, int] | None      # answer key -> members, when vote ran
    tie: bool = False                 # vote's plurality was tied and broken by time
    synthesize_error: str | None      # a recovered synthesize failure, when one occurred
    provisional: bool                 # True until finalisation completes

class SwarmResultRecord(BaseModel):
    version: Literal[1] = 1
    kind: ResultKind | None
    verifier: str | None              # verifier label, for audit
    members: list[MemberResult]
    final: FinalRecord | None
    final_verdict: Verdict | None     # artifact kind: verifier on the drained environment
    solve_usage: dict[str, ModelUsage] | None       # sample usage when the swarm exited, however it exited
    solve_role_usage: dict[str, ModelUsage] | None  # sample role usage at the same moment
    solve_exit: Literal["returned", "limit", "terminated", "error", "cancelled"] | None
```

- The controller writes the record when the swarm starts, updates it when a member stops (adding its output, key and verdict) and when the provisional answer changes, and finalises it after the drain. An outer limit that ends the sample early therefore leaves an accurate record with `provisional: true`.
- `solve_usage` is the sample's cumulative usage (`sample_model_usage()`, and `sample_role_usage()` for roles), taken in the swarm's outermost `finally`, however the swarm exits: after finalisation (`synthesize` included) when it returns, and after its task group has cancelled and awaited the members when a sample limit, `TerminateSampleError`, an error or a cancellation propagates out of it. Both reads and the store write are synchronous, so they run under cancellation too, and `solve_exit` records the path. Inspect scores only after the solver has returned or raised (`run.py:2960-3059`), so the snapshot always falls before grading. Sample usage already counts native compaction and excludes response-cache replays, so it is complete where model events are not ([Cost comparisons](#cost-comparisons-exclude-scoring)). It covers everything up to the swarm's exit, so the swarm must be the task's last solver step; `swarm()` documents this, and a later solver step's usage would land after the boundary.
- It is in the store, not only in events, because re-scoring rebuilds `TaskState.store` and nothing else from the run.
- Each stop, verdict and the final selection also produce an evidence `InfoEvent` (`source="inspect_swarm"`, versioned payload, kinds `member_result`, `verdict`, `final`), inside the member's span or the swarm's span. They put the result in the transcript at the time it happened; the store record is what scorers read. The InfoEvent payload format is the Observer's ([Observer](swarm.md#observer-evidence-accounting-and-metrics)); this design adds these three kinds.
- Outputs can be long (code). They are stored once, in the record; evidence events carry the member name and the answer key, not the text.

The swarm's returned `AgentState` has the sample's input messages followed by one assistant message: the final answer's `output.message`, with `metadata={"inspect_swarm": {"final": {"mode": ..., "member": ...}}}`. Its `output` is the chosen member's `ModelOutput`, unchanged, so the task's scorer sees exactly the object it would have seen had that member run alone. Members' own conversations stay in their spans and timelines.

### The final-answer chain

`swarm(final=...)` takes one mode or a sequence of modes. A sequence is a chain: each mode is tried in order on the submissions, and the first that produces an answer wins.

| Mode | Needs | Produces | Produces nothing when |
|---|---|---|---|
| `verify` | `result.verifier` | The passed submission with the highest `Verdict.value` (`None` ranks below any number), earliest swarm time on ties, then roster order | no submission passed |
| `vote` | `result.answer_key` | The earliest submission in the plurality group of answer keys; one vote per member; `None` keys do not vote. A tied plurality goes to the group whose first submission is earliest, and `tie` is recorded | no submission has a key |
| `first` | nothing | The submission with the earliest swarm time, then roster order | there are no submissions |
| `synthesize` | `kind="answer"` | One extra model call over the submissions ([below](#synthesize)) | there are no submissions, or the call fails recoverably |
| `reporter` | a coordinator topology | Reserved; rejected in M1 | |

**The default.** With `final=None`, the chain is built from what the task supplies: `verify` if there is a verifier, then `vote` if there is an answer key, then `first`. So a task with both gets `["verify", "vote", "first"]`, one with neither gets `["first"]` (decision: Ransom, 2026-10-07, for the order). This design reads the decision as a chain that falls through: when nothing verifies, the swarm votes, and when nothing can vote, it takes the first submission. A single mode, `final="verify"`, is strict: no verified submission means no answer. [Open question 1](#open-questions) asks Ransom to confirm the fall-through reading.

**Validation.** `swarm()` raises `ValueError` at construction when a mode's need is missing (`verify` without a verifier, `vote` without an answer key, `synthesize` on an artifact task, `reporter` on a leaderless swarm), naming the missing piece.

**When verdicts are taken.** When the task supplies a verifier, the controller verifies every submission as it arrives, whether or not `verify` is in the chain, so the analysis always has verdicts. Verifier calls run one at a time in the controller, inside the swarm's span, so their usage, if any, is in `solve_usage` and the ledger, charged to the swarm rather than to a member. Which limit node meters them is the limits deep dive's call.

**Provisional answer.** After each submission or verdict, the controller applies the chain without `synthesize` (which needs a model call) to the submissions so far and sets the result on the `AgentState` it was passed, as swarm.md's [exhaustion policy](swarm.md#observer-evidence-accounting-and-metrics) requires. When nothing qualifies, the output stays empty and `reason` says why. Nothing is invented.

**No submissions.** When no member submitted, the final answer is empty with `reason="no_submissions"`, even though members have outputs. An output from a member stopped mid-work is usually a working message, not an answer. The per-member scores still score those outputs, so `best_member` reflects them.

**Finalisation.** After the drain (the limits deep dive owns its ordering against the reserve), the controller verifies any submission not yet verified, runs the chain once more including `synthesize`, and, for an artifact task with a verifier, takes `final_verdict` ([Shared-artifact tasks](#shared-artifact-tasks-and-per-member-artifacts)). It then writes the final record with `provisional: false`, writes the `final` evidence event and returns; `solve_usage` is taken on the way out.

### `synthesize`

`synthesize` is explicit only, never a default, because it is the one place peer text enters a prompt the harness composes ([Security](swarm.md#security)).

```python
def synthesize(model: str | Model | None = None, prompt: str | None = None) -> FinalMode: ...
```

- **Model.** `model`, else the model role `synthesizer` if the eval defines it, else the swarm's default model (`get_model()`).
- **Prompt.** A fixed template: the sample's input, then each submission fenced as data under a sender line the harness writes (the member's roster name), with the fence markers removed from the payload, as in [Delivery](swarm.md#delivery-peer-messages-are-model-output). It ends with an instruction to give one final answer in the format the task asks for. Only submissions are included, never members' tool calls or transcripts. `prompt` replaces the instruction, not the fencing.
- **Shared notes.** In M1 there is no notes channel; the filesystem notes file is not read, because its contents are unfenced and member-written in any layout. If the notes channel is built later, its entries are fenced the same way.
- **Accounting.** The call runs in the swarm's span after the drain, inside the final-answer reserve, so `solve_usage` and the ledger count it as finalisation.
- **Failures.** Before the call, the provisional answer (computed without `synthesize`) is already on the `AgentState`. Then:
  - **Classification is group-aware.** Inspect re-raises a provider's exception without unwrapping it (`src/inspect_ai/model/_model.py:1651-1656`), so a model API that uses a task group can raise an `ExceptionGroup` holding a `LimitExceededError`. An exception is *stopping* if it is, or is a `BaseExceptionGroup` that contains at any depth, a `LimitExceededError`, a `TerminateSampleError` or the backend's cancellation exception (`anyio.get_cancelled_exc_class()`). The check uses `BaseExceptionGroup.subgroup()` (Python 3.11).
  - **Recoverable:** an `Exception` from the call that is not stopping (a provider error after Inspect's retries, a `ModelRefusalError`, a group of such errors), and a returned output with an empty completion or `stop_reason="content_filter"`. The chain moves to its next mode and `synthesize_error` records why. A chain of `["synthesize"]` alone keeps the provisional answer.
  - **Not recoverable:** a stopping exception propagates unchanged, as raised. A group is never split: a group that mixes a limit with an ordinary error propagates whole, which is what swarm.md's exhaustion policy expects of mixed groups. A limit error reaches the controller's [exhaustion policy](swarm.md#observer-evidence-accounting-and-metrics), which recovers only the swarm's own cap and lets every other limit reach the runner, so Inspect records `EvalSample.limit`. Either way the provisional answer is the sample's output.
- **Scoring.** The synthesized answer is no member's output. The task's scorer scores it; per-member scores are unaffected.

### Answer-scored tasks: per-member scores

The scorer `member_scores(scorer, result, ...)` builds:

1. **Skips in-loop scoring.** If `state.completed` is `False`, it returns `None` at once. `score()` drops `None` results, so `react(attempts=...)`, `basic_agent`, the human agent's `/score` and the inspect_swe attempt loops see exactly the task scorer's feedback and call count they saw before, and the wrapper never runs the inner scorer, touches the sandbox or reads a half-written record during the solve. Final scoring and re-scoring both have `completed=True`.
2. **Reads the members.** From the `inspect_swarm.result` record when it exists. Otherwise (a plain agent, not `baseline()`) it treats the sample's `output` as one member named `solver` with status unknown, and computes its answer key, which is a pure function. It never runs the verifier.
3. **Checks the contract.** If `result.kind` is not `"answer"`, or the record's kind differs from `result.kind`, it returns `best_member` and `mean_member` as NaN with `reason` saying why (`"artifact_without_member_artifacts"`, `"kind_mismatch"`, `"kind_not_declared"`). Shared-artifact tasks with member artifacts are handled [below](#shared-artifact-tasks-and-per-member-artifacts).
4. **Grades each member, in roster order.** Each member gets one of three outcomes:
   - **Empty.** No output, or a completion that is empty after stripping whitespace: value `empty_value` (default 0.0), reason `"empty_output"`, and the scorer is not called. This keeps a scorer's environment fallback from crediting the team's work to a member that produced nothing. It is a backstop, not the contract: a non-empty completion can still extract to nothing (an empty fenced block), which is why `kind="answer"` requires a scorer that never reads the answer from the environment.
   - **Graded.** Otherwise it builds a deep copy of the `TaskState` whose `output` is the member's `ModelOutput` and whose `messages` are the sample's input messages plus the member's `output.message`, awaits the scorer with the sample's target, and converts the value with `value_to_float`.
   - **Unavailable.** The scorer returned `None`, a NaN value (`Score.unscored()`), or a dict or list value (no general best of a dict). The member's value is NaN, with the scorer's own `reason` kept, or `"scorer_returned_none"` or `"non_scalar_value"`.
5. **Aggregates over the members with a value** (empty and graded), ignoring unavailable ones explicitly rather than through `max`/`mean` on NaN, so the result does not depend on roster order: `best_member` is their maximum and `mean_member` their mean. Metadata records `members_total`, `members_valued` and `complete` (no member unavailable), so analysis can restrict to complete samples. With no member valued, both keys are NaN with reason `"no_member_valued"`.
6. **Returns** `Score(value={"best_member": ..., "mean_member": ...}, metadata=...)`. The metadata holds, per member: outcome, value, answer, explanation, reason, status, `submitted`, answer key, verdict and time; plus the record's `final` block and `kind`. The analysis reads these.

Scores of the copies are not added to `state.scores`, so they do not appear as extra sample scores. Each inner scoring runs inside a span named after the member, within `member_scores`'s scorer span, so a model-graded scorer's calls are attributable.

**Which scorer.** The scorer given to `member_scores` must meet the answer-scored contract. It is usually the task's own scorer. When the task's scorer can read the answer from the environment, the task passes an answer-only adapter instead, for Frontier-CS a variant that scores empty extracted code as 0 rather than recovering code from the sandbox. Frontier-CS has no switch for this today; the adapter is task code. The headline stays the unchanged task scorer, and oracle comparisons (team@k against best@k) use `member_scores` values in every arm, so both sides use the same adapter ([Comparison quantities](#comparison-quantities)).

**Ordering.** `member_scores` must follow the task's own scorer in the scorer list. The task's scorer then scores the final answer, and the environment, before any per-member scoring can touch the sandbox. Answer-scored scorers that use the sandbox as an evaluator (compiling and running the answer) run once per member in the same sandbox, one after another; this is safe when the scorer writes its own files before reading them, which is the task's responsibility.

**Time.** Per-member scoring runs within Inspect's scoring time limit, half the sample's (`run.py:3034`). Sandbox evaluators across many members can exceed it; the sample then errors as for any slow scorer. Tasks with costly evaluators should set a time limit with that in mind.

**Cost.** A model-graded scorer is called k more times per sample in the swarm arm, and once more in other arms. That usage is in the sample's totals but after `solve_usage`, so it never enters an arm's solve cost ([Cost comparisons](#cost-comparisons-exclude-scoring)).

### Shared-artifact tasks and per-member artifacts

For `kind="artifact"`:

- **The team score** is the task's scorer on the drained environment. It is the only result the task defines, and it is computed first.
- **The final-answer chain** still runs and sets `output`, because some scorers read the output text and fall back to the environment (Frontier-CS). `synthesize` is rejected.
- **The verifier**, when given, checks the environment, not an answer. It is called with the submitting member's completion and the sample metadata whenever a member submits. The controller can use the verdict as a termination signal ([Controller](swarm.md#controller-topology-termination-final-answer)); members still working when it fires keep editing until the drain cancels them, so the environment that is scored can differ from the one that was verified.
- **`final_verdict`** is therefore taken in M1's finalisation, after the drain, on the environment that will be scored: the controller calls the verifier once more with the final answer's completion, or `""` when there is no final answer. Analysis compares it with the run-time verdicts and with the team score.
- **Per-member scores** are unavailable unless the task supplies `member_artifacts`.

With `member_artifacts`, `member_scores` grades each member's artifact instead of its output text. Every member is graded, whatever its completion: the empty-output outcome is for answer tasks only, because a member can leave a passing artifact and stop without submitting any text. Unavailable grades and aggregation follow answer tasks:

```python
for m in members:                                   # roster order
    async with result.member_artifacts.materialize(m.name):
        score = await scorer(copy_of_state_with_member_output(m), target)
```

- `materialize` is task code because only the task knows its layout. For a SWE-bench-shaped task where each member works in its own git worktree on branch `swarm/<member>` ([Sandbox topology](swarm.md#sandbox-topology)), it would commit or stash the team's working tree, check out the member's branch in the scored location, and on exit restore the team's state, untracked and scorer-created files included.
- It is given only roster names, never names a member chose, so paths and branch names come from the eval author's layout.
- It runs after the team score and restores the team state on exit, so the team score never depends on per-member scoring. A `materialize` that raises propagates and fails the sample, as any scorer error does.
- **Attribution in a shared sandbox is by convention.** Any member can write into another member's worktree or branch. Per-member artifact scores say "what was on that member's branch", not "what that member did alone". Tool-call evidence can show cross-writes, but only as the observation limit in [Security](swarm.md#security) allows.

Swarm.md places per-member scoring of shared-artifact tasks outside M1. This design keeps that: M1 reports the team score and `final_verdict`, and `member_artifacts` is a later item ([Implementation plan](#implementation-plan)).

### Comparison quantities

Each quantity is defined once and paired with the one it is fairly compared with. k is the number of members, or the number of independent attempts.

| Quantity | Arm | Definition | Uses the target to select? | Computed by |
|---|---|---|---|---|
| **final@k** | swarm of k | Answer tasks: the task's scorer on the swarm's final answer, `empty_value` when there is none. Artifact tasks: the task's scorer on the drained environment, whatever the final text | No | the task's scorer; the helper applies the empty rule to answer tasks only |
| **team@k** | swarm of k | Answer tasks: `best_member`. Artifact tasks: the shared environment's score, equal to final@k | Answer: yes. Artifact: no | `member_scores`; the task's scorer |
| **mean member** | swarm of k | `mean_member` | No | `member_scores` |
| **pass@k** | n ≥ k epochs of `baseline()` | P(at least one of k attempts correct), binary values, without replacement | Yes | Inspect `pass_at(k)` on `best_member` |
| **best@k** | n ≥ k epochs of `baseline()` | E[max over k attempts], graded values, without replacement | Yes | inspect_swarm `best_at(k)` on `best_member` |
| **select@k** | n ≥ k epochs of `baseline()` | The swarm's chain applied to k independent attempts | No | `inspect_swarm.analysis` |
| **single** | one `baseline()` attempt at the full budget | As final@k | No | as final@k |

In a `baseline()` arm each epoch has one member, so its `best_member` is that attempt's value under the same scorer `member_scores` uses in the swarm arm.

team@k follows Test-Time Communication's usage: "a game counts as solved if any of the k agents clears all levels", while on Terminal-Bench "team@2 contributes one shared final state". The fair pairs are:

- **team@k against pass@k or best@k.** Both pick the best of k outputs with knowledge of the target, under the same scorer. They measure whether communication raises the capability present in k agents.
- **final@k against select@k, and against single.** Both apply a rule that does not see the target, scored by the task's scorer, with the same empty rule. They measure what a deployed swarm returns against what k independent attempts with the same selection rule, or one bigger attempt, return. The gap team@k − final@k is what the final step loses.
- **Artifact tasks.** team@k is already non-oracle (one environment). Comparing it with pass@k, as Test-Time Communication does, favours the epochs arm; select@k with the verifier on each attempt's environment is the non-oracle comparison.

All of them are compared at realized cost, with each arm's cap and realized spend reported (swarm.md, [Eval questions](swarm.md#eval-questions-and-the-experimental-design-they-imply)).

### `best_at(k)`: best of k from n epochs

```python
@score_reducer
def best_at(k: int, value_to_float: ValueToFloat = value_to_float()) -> ScoreReducer: ...
```

- The unbiased without-replacement estimator of the expected maximum of k of the n scored epochs. With the n values sorted ascending as v₁ ≤ … ≤ vₙ, best@k = Σᵢ₌ₖⁿ vᵢ · C(i−1, k−1) / C(n, k).
- For binary values it equals `pass_at(k)`. I checked both properties with a script: against brute-force enumeration of all k-subsets for 200 random cases (n ≤ 8, graded values), and against Inspect's `pass_at(k)` on binary values.
- NaN epochs are dropped first. If fewer than k remain, the result is NaN, as `pass_at` does (`reducer.py:181-186`).
- Dict values are reduced per key, list values per index, following Inspect's reducer helpers.

### select@k: the selection rule on independent attempts

select@k needs each attempt's eligibility, answer key, verdict and time, which a reducer cannot see. The `baseline()` record has them, so select@k is computed in the analysis helper, not as a reducer.

**An attempt** is one epoch of a `baseline()` arm, read from its record and scores:

```python
@dataclass(frozen=True)
class Attempt:
    epoch: int
    submitted: bool                   # the member's status was "done"
    time: float | None                # swarm time when it stopped
    answer_key: str | None
    verdict: Verdict | None
    final_value: float | None         # task scorer's value for this epoch's sample (NaN -> None)
    final_verdict: Verdict | None     # artifact tasks: verifier on the drained environment
```

**The pool** for a sample is its epochs with a final record and a non-NaN `final_value`; others are excluded and counted. With fewer than k attempts in the pool, the sample's select@k is NaN (unscored), as `pass_at` does.

**One subset (answer tasks).** Apply the chain to the *submitted* attempts of the subset, with the same rules and tie-breaks as the swarm (verdict value, then time, then epoch in place of roster order). Unsubmitted attempts are never selected, as the swarm never selects an unsubmitted member. The subset's value is the chosen attempt's `final_value`. Because a `baseline()` attempt's final answer is its own submission, that is the task's scorer on the chosen output. When the chain produces nothing (no submissions; strict modes with nothing passing or no keys), the value is `empty_value`, the rule final@k uses.

**Artifact tasks** differ, because every attempt leaves an environment and the swarm arm's team environment is scored whether or not anyone submitted text:

- every attempt in the pool is eligible, submitted or not, and its value is its environment score (`final_value`, the task's scorer on its drained environment);
- `verify` uses each attempt's `final_verdict`, the verifier on its drained environment, which is what the swarm arm's own environment check corresponds to; `vote` uses the answer keys of submitted attempts, when the task defines a key; `first` takes the attempt with the earliest stop time;
- the helper appends `first` to the chain if it is not already last, so every subset selects an environment, as the swarm arm always has one. A strict chain therefore has no artifact analogue, and the helper says so in its output when the swarm arm used one;
- `empty_value` is never used for artifact tasks.

**The sample's select@k** is the mean over all C(n′, k) subsets of its pool when that is at most 10,000, otherwise over 10,000 subsets sampled with a fixed seed recorded in the output. The helper also reports, per sample, the fraction of subsets that produced no answer.

**Time** is the record's swarm time, never `EvalSample.working_time`, which includes scoring and excludes waiting ([Current behaviour](#usage-and-timing-during-scoring)). Every `baseline()` attempt starts its swarm clock at zero, so "first" means the shortest time to submit.

`synthesize` has no epoch analogue, because it is a new model call; a swarm using it is compared on final@k against `single` and select@k without it.

### Cost comparisons exclude scoring

Swarm.md takes each arm's total from Inspect's sample usage ([Accounting](swarm.md#observer-evidence-accounting-and-metrics)). Sample usage includes scorer model calls (verified above), and `member_scores` makes k + 1 times as many scorer calls in the swarm arm as in a baseline arm. Model events cannot separate the two phases, because native compaction records usage without one.

- An arm's realized solve usage is the record's `solve_usage`: the sample's cumulative usage when the swarm exited ([What the swarm records](#what-the-swarm-records)), after finalisation when it returned, after the drain when a limit, termination, error or cancellation ended it. It has the coverage of Inspect's sample usage, native compaction included and cache replays excluded, and the ledger's unknown-cost rules still apply to it (calls cancelled in flight, unpriced models).
- Scoring usage is the sample total minus `solve_usage`, reported separately per arm, so the cost of per-member scoring is visible.
- A sample without `solve_usage` has no solve/scoring boundary: a plain agent without `baseline()`, or a sample that failed before the swarm started. The helper keeps its total under a separate name, `sample_total_usage`, never as solve usage, excludes the sample from every solve-cost comparison, and reports the number excluded per arm. Its scores are still reported.
- The limits deep dive, which owns the ledger, takes arm totals from `solve_usage` and follows the same exclusion when it joins on (log, sample id, epoch).

### Re-scoring

- **Answer tasks with sandbox-free scorers.** `member_scores` reads only the store record, the sample's input and the target, so `inspect score` reproduces it from the log, and can apply a new scorer to every member's output after the fact, provided the task's registered factory is loadable ([Opting in](#opting-in)).
- **Scorers that need the sandbox** (an answer evaluator that compiles code, any artifact scorer) cannot be re-scored: `sandbox()` raises `ProcessLookupError` and the error propagates, as it does for the task's own scorer today. `member_scores` neither catches it nor reports a partial result.
- **Selection evidence is never recomputed.** Verdicts, keys, times and `solve_usage` come from the record written during the run; re-scoring never calls a verifier.
- **Old records.** `member_scores` checks `version`. A record with an unknown version is reported as unscored with reason `"unknown_record_version"`, never misread.

### How it appears in logs

| Where | What |
|---|---|
| Sample `output` and last assistant message | The final answer, the chosen member's own `ModelOutput`; message metadata names the mode and member |
| Sample `store["inspect_swarm.result"]` | The full record: every member's output, status, key, verdict, time; the final selection; `final_verdict`; `solve_usage` |
| Transcript | `InfoEvent`s of kinds `member_result`, `verdict` and `final` (source `inspect_swarm`) at the time they happened; `member_scores`' per-member spans under the scorer span |
| Sample scores | The task's scorer (final@k, or team@k for artifact tasks); the task's member scorer with `best_member` and `mean_member`, per-member detail in its metadata |
| Eval results | One `EvalScore` per task scorer and reducer; the member scorer contributes `best_member` and `mean_member` as separate `EvalScore`s with `scored_samples` and `unscored_samples` counts |
| Eval spec | `swarm()`'s `result` argument is logged as the type name `ResultSpec`, because it holds callables; the record carries the facts |

Nothing here needs a new event type, schema change or viewer change. The viewer shows the store, `InfoEvent`s and dict scores generically.

Because Inspect applies every epoch reducer to every scorer and key, an epochs arm with `["mean", pass_at(k), best_at(k)]` also shows, for example, `pass_at` of `mean_member`. Those cells are well defined but not meaningful; the analysis helper selects the cells that are.

### How it appears in eval sets

- **Arms as task arguments.** Each arm is the same task with different arguments (`arm`, `k`, `budget`), as in [Opting in](#opting-in). `eval_set` and inspect_flow sweep them, and each arm's log carries its arguments in `eval.task_args`.
- **Identifying arms.** A log whose records have one member is a baseline arm (n is its epochs); more than one member, a swarm arm (k is the member count). Arguments the user names (`arm`, `budget`) label the rows.
- **Same scorers everywhere.** The task's member scorer runs in every arm, so all arms have the same scorer names, and `evals_df` and `samples_df` (`inspect_ai.analysis`) line them up without renaming.

### The analysis helper

`inspect_swarm.analysis` adds the scoring part of swarm.md's "thin analysis helpers", in two layers:

```python
def attempt_rows(logs: Sequence[str | EvalLog], *, scorer: str, member_scorer: str) -> list[AttemptRow]: ...
def compare_arms(rows: Sequence[AttemptRow], *, k: int, chain: Sequence[str] | None = None, empty_value: float = 0.0, max_subsets: int = 10_000, seed: int = 0) -> list[ArmScores]: ...
def best_at_k(values: Sequence[float], k: int) -> float: ...
def select_at_k(attempts: Sequence[Attempt], k: int, chain: Sequence[str], *, kind: ResultKind, empty_value: float = 0.0, max_subsets: int = 10_000, seed: int = 0) -> float: ...
```

- **`AttemptRow`**, one per (log, sample id, epoch): the log path, the arm labels (`eval.task_args`), member count, the task scorer's value, `final` (mode, member, reason), `best_member`, `mean_member`, `complete`, the per-member keys, verdicts, times and submission flags, `solve_usage` and `solve_exit`, scoring usage, `sample_total_usage`, and flags (`no_solve_usage`, `target_assisted`). Rows flagged `no_solve_usage` have no solve or scoring usage and are left out of cost comparisons. The ledger and the limits deep dive's curves join on (log, sample id, epoch).
- **`ArmScores`**, one per arm: the arm's labels, kind (swarm or baseline), k or n, and each quantity of [Comparison quantities](#comparison-quantities) that applies to it, as a mean over samples with its count of scored samples and of excluded samples. `chain` defaults to the chain recorded in the swarm arm's records.
- **Target-assisted samples.** `target_assisted` is set when the solve phase contains `ScoreEvent(intermediate=True)`: an agent consulted the task's scorer with the target in the loop (for example `react(attempts=...)`). In a swarm that feedback can be shared with peers, so the helper reports those samples separately.

## Alternatives considered

**Pass `ResultSpec` to a registered `member_scores` scorer.** The obvious shape, and the first draft's. Inspect logs a plain dataclass argument as its type name, so default `inspect score` rebuilds the scorer with a string in place of the spec. Making the spec a registered object would need a new registry type in inspect_ai. A task-owned no-argument factory uses existing machinery and keeps the spec's callables in the task's code. Chosen.

**Compute per-member scores inside the swarm.** The swarm calls the task's scorer at the end of the solve. Scorers belong to the task, need the target (which the solver should not use), and their usage would land in the solve. Re-scoring would also miss them. Rejected.

**Replace the task's scorer with a swarm scorer** that returns `{"final", "best_member", "mean_member"}`. One scorer for everything, but the headline score would then differ in name and shape between arms, and a task's metrics would change. Rejected.

**Separate scorer per member** (`member_1`, `member_2`, …). The number of scorers would vary with k, so arms would not line up, and "member 3" means nothing in a leaderless swarm. Per-member detail lives in metadata instead.

**Plain agents as baselines, with evidence computed at scoring time.** The first draft ran the verifier in `member_scores` for arms without a swarm, and timed attempts by `working_time`. Re-scoring would then rerun a possibly sandbox-dependent verifier; `working_time` includes scoring, so a slow grader can reverse which attempt counts as first; and a limit-truncated output had no eligibility flag. Running baselines as a swarm of one records the same evidence at run time under the same rules. Chosen; plain agents remain scoreable but are left out of select@k.

**Selection comparisons as epoch reducers** (`select_at(k)`). A reducer sees one scorer's scores, so the keys and verdicts would have to ride in score metadata, Inspect would apply the reducer to every other scorer too, and the subset estimator with sampling does not fit a reducer. `best_at(k)` is a closed form on values alone, so it is a reducer; select@k is in the helper.

**Voting with Inspect's `mode`/`majority` reducers.** They vote on correctness values, not on answers. Rejected.

**Solve cost by subtracting scoring model events.** Native compaction during scoring records usage with no event, so it would be counted as solve cost. A usage snapshot at the swarm's exit is complete. Chosen.

**Snapshot only on a normal return, falling back to the sample total.** The second draft's rule. Budget exhaustion is a normal way for an arm to end, and Inspect still grades those samples, so the fallback total would include grading, which differs between arms. Taking the snapshot in the swarm's `finally` gives limit-ended samples a boundary too, and samples with none are excluded from cost comparisons rather than estimated.

**Strict modes only** (verify yields nothing when nothing verifies). Simpler, and measures the verifier alone. But it makes the default swarm return nothing where an attempt would otherwise be scored on its answer. Strict behaviour stays available as `final="verify"`. [Open question 1](#open-questions).

**Score unsubmitted outputs as the last fallback.** Matches how Inspect scores a plain agent that hits its limit. But it selects working messages as answers, and swarm.md decided that nothing is invented. Per-member scores still score those outputs.

**Treat any unavailable member as making the sample unscored.** Simple and conservative, but one failed model grade among k would discard the sample's other grades. Aggregating over valued members with a `complete` flag keeps the data and lets analysis choose.

**Per-member sandboxes for per-member artifacts.** Clean attribution, and the isolation epochs have. But it changes the default topology Ransom decided (shared sandbox), and the shared filesystem is M1's channel. It stays [later work](swarm.md#sandbox-topology); `materialize` covers the shared default.

## Compatibility and migration

- **inspect_swarm** has no released API; everything here is new. Python 3.11+ as decided.
- **Eval logs.** Only existing structures: a store key, `InfoEvent`s, scores with dict values and metadata, a message metadata key. Old readers and the viewer show them generically. The store record and the `InfoEvent` payloads carry `version: 1`.
- **Names** (`member_scores`, `MEMBER_METRICS`, `best_member`, `mean_member`, `best_at`, `baseline`) become an interface the analysis and users' notebooks depend on once released; changing them later is a breaking change. The member scorer's own name is the task's.
- **inspect_ai.** No change needed. The design uses public scorer and reducer APIs, the store, `transcript().info`, `sample_model_usage()` and `sample_role_usage()` (private, which inspect_swarm may use), and the member runner's reference to its `AgentState`.
- **Tasks.** Tasks opt in. A task that does not pass `result=` behaves as swarm.md describes, with `first` and no per-member claims. A task whose scorer can read the answer from the environment must declare `kind="artifact"` or supply an answer-only adapter.
- **Swarm.md.** This PR makes small edits there where this document refines it: team@k's definition and its fair pairs; the chain's fall-through and the strict single mode; the provisional answer's definition and its test bullet; arm totals taken from a usage snapshot at the swarm's return, so they exclude scoring; links to this document; and per-member artifacts as a later menu item. The overview's decisions are unchanged.

## Security

Untrusted input reaching the new code:

- **Member outputs are model output.** They reach:
  - `answer_key`, task code that must accept any text and return `None` rather than raise on unparseable input;
  - the verifier, which may execute the answer (code) and must do so only inside the sandbox;
  - the member scorer, as they would from a single agent, and only after the solve (`completed=True`);
  - `synthesize`'s prompt, fenced as data with harness-written sender lines and markers stripped, in a separate call outside every member's context. That is weaker than tool output, so `synthesize` is never a default.
- **Answer attribution.** A member's output must not be credited with the team's environment. The answer-scored contract forbids scorers that read the answer from the environment, and the empty-output backstop keeps the commonest case from reaching the scorer at all.
- **The target.** Neither the verifier, nor `answer_key`, nor any member tool is given the target. Agents that consult the task's scorer in the loop are flagged by the analysis.
- **Shared-sandbox attribution.** In a shared sandbox, any member can alter another's worktree or branch, so per-member artifact scores are attributions by convention. `materialize` receives only roster names, never member-chosen names, so a member cannot steer which path is materialized. Detecting cross-writes relies on tool-call evidence and its stated limit.
- **Logs.** Outputs and answer keys are stored as JSON data in the store and in score metadata, not as Markdown.
- **Reward hacking.** Members can read whatever the sandbox holds, scorer test files included, exactly as a single agent can. Nothing here adds or removes that exposure.

## Testing

All runtime tests use mockllm with scripted outputs, need no network or Docker, and run on asyncio and trio unless marked:

- **Chain selection** (table-driven, over lists of member results): `verify` (best verdict value, `None` values, ties by time then roster, none passed); `vote` (plurality, `None` keys abstain, ties by earliest, one vote per member); `first`; fall-through in the default chain; the strict single mode leaving an empty answer with its reason; construction errors for each missing need.
- **Provisional answer** follows the chain without `synthesize` after each submission and verdict, and an outer limit leaves it as the sample's output with `provisional: true`.
- **Verifier** is called once per submission, one at a time, never with the target; its exception fails the sample; neither scoring nor re-scoring calls it.
- **`synthesize`**: the prompt fences each submission under the harness's sender line with markers stripped and no tool calls; its usage falls inside `solve_usage`; a provider error, a refusal and an empty output fall through the chain with `synthesize_error` set; a sample-level `LimitExceededError`, `TerminateSampleError` and cancellation propagate, the sample records its limit, and the provisional answer is the output; an `ExceptionGroup` holding a sample limit (from a model API that uses a task group) and a mixed group of a limit and an ordinary error propagate whole, on both backends.
- **The record**: written at start, updated on each stop, finalised after the drain; member output kept after the member's own limit and after the drain for an agent that updates its state in place.
- **Artifact `final_verdict`**: taken after the drain on the final environment, with the final completion or `""` when there is no final answer, and differing from a run-time verdict when a member edits after a passing check.
- **`member_scores`** with `match()`:
  - per-member values, `best_member` and `mean_member`; empty outputs get `empty_value` and never reach the scorer in answer tasks;
  - an inner scorer returning `None`, `Score.unscored()` and a dict value gives unavailable members with their reasons; mixed and all-unavailable cases; every roster permutation gives the same aggregates;
  - artifact kind without artifacts, kind mismatch and missing contract give NaN with the right reason;
  - a plain agent is scored as one member with its key and no verdict;
  - the copies do not appear in `state.scores`.
- **In-loop guard**: a `react(attempts=3)` agent with `member_scores` listed after the task scorer gets the same feedback and the same number of task-scorer calls as without it, and `member_scores` returns `None` while `completed=False`.
- **Answer attribution**: a Frontier-CS-shaped scorer with a sandbox fallback, given a member completion holding an empty fenced block, is not credited with the team's files when `member_scores` uses the answer-only adapter.
- **Re-scoring**: in a fresh process, default scorer resolution (`resolve_scorers()` then `score()`, as `inspect score` does) rebuilds the task's member-scorer factory and reproduces its scores for a sandbox-free answer scorer; programmatic re-scoring with freshly built scorers does the same; a sandbox-dependent scorer raises `ProcessLookupError`.
- **`best_at(k)`** against brute-force enumeration (table of small cases), equality with `pass_at(k)` on binary values, NaN with fewer than k scored epochs, dict values per key.
- **`select_at_k`** for answer tasks against a reference built from the runtime selector: exact enumeration on small pools; the seeded sample above 10,000 subsets; unsubmitted attempts never selected; subsets with no answer valued at `empty_value`; pools smaller than k giving NaN; `first` by record time, unchanged when the grader's duration is varied.
- **Solve usage**: `solve_usage` excludes a model-graded scorer's usage and a scorer's native compaction (mockllm compaction usage), includes the swarm's own compaction and `synthesize`, and counts cache replays as Inspect does. A sample ended by an outer limit, with a grader that makes model calls and compacts, still has `solve_usage` (with `solve_exit="limit"`) equal to the solve's usage alone; the same holds for termination and cancellation. A plain-agent sample has no `solve_usage`, is excluded from cost comparisons and counted.
- **Artifact results ignore answer text**: with an artifact-shaped scorer (scores the environment, ignores the completion), a passing environment and an empty final output give final@k and team@k 1, not `empty_value`; a member with an empty completion and a passing materialized artifact is graded 1; artifact select@k selects unsubmitted attempts, uses `final_verdict`, and always selects an environment.
- **`member_artifacts`** (later item): with the local sandbox and files standing in for branches, the team score runs before any `materialize`, each member is graded with its artifact in place, and the team state is restored on exit and on a scorer error. A git-worktree layout test needs Docker and is marked slow, following inspect_ai's conventions.

## Implementation plan

This work is part of M1, after the swarm core (members, controller, drain) exists. One PR per step:

1. **Result contract, record and baseline.** `src/inspect_swarm/_result.py` (`ResultSpec`, `answer_result`, `artifact_result`, `Verdict`, `Verifier`, `MemberArtifacts`, the record models); the controller writes and updates the record, `solve_usage` and the `member_result`/`verdict` events (`_swarm.py`, `_member.py`, `_evidence.py`); the member runner keeps its `AgentState` reference; `baseline()` in `_swarm.py`. Exports in `src/inspect_swarm/__init__.py`. Tests in `tests/test_result.py`.
2. **Final-answer chain.** `src/inspect_swarm/_final.py`: modes, chain construction and validation, provisional answer, finalisation including the artifact `final_verdict`, the `final` event and the returned `AgentState`. Tests in `tests/test_final.py`.
3. **`synthesize`.** In `_final.py`, with its prompt template in `src/inspect_swarm/_prompts.py`, and its failure classes. Tests extend `tests/test_final.py`.
4. **Scorers.** `src/inspect_swarm/scorer/__init__.py`, `scorer/_member_scores.py` (`member_scores`, `MEMBER_METRICS`; answer kind; artifact kind returns unavailable), `scorer/_best_at.py`. Tests in `tests/scorer/`, including the re-scoring test with a task-file factory.
5. **Analysis.** `src/inspect_swarm/analysis/__init__.py`, `analysis/_scores.py` (`attempt_rows`, `compare_arms`, `best_at_k`, `select_at_k`, the target-assisted flag, the solve/scoring usage split). Tests on synthetic logs in `tests/analysis/`.

Later, in any order with the rest of swarm.md's menu:

6. **Per-member artifacts for shared-artifact tasks.** `member_artifacts` in `member_scores`, and a worked SWE-bench-shaped example layout in the docs.

## Open questions

1. **Does the default chain fall through?** With a verifier and an answer key the default is `["verify", "vote", "first"]`. This design falls through when a mode yields nothing (nothing verified, so vote; no keys, so first). The alternative reads the decision as choosing one mode by what the task supplies, so nothing verified means no answer.
   - (a) Fall through (this design). The swarm returns an answer whenever anyone submitted. The strict reading stays available as `final="verify"`.
   - (b) Strict. Measures the verifier alone, and returns no answer more often.

   Recommendation: (a). Swarm.md's provisional-answer text and its test bullet are edited in this PR to match (a), applying the strict reading to `final="verify"` only.

## Not this design

- **`best_at(k)` in inspect_ai.** It generalises `pass_at(k)` to graded scores and belongs beside it; propose it upstream once it has been used here.
- **Per-scorer epoch reducers** (inspect_ai). Today every reducer applies to every scorer and key, which fills logs with meaningless cells such as `pass_at` of `mean_member`.
- **Solve-phase usage in the eval log** (inspect_ai). A sample-level solve/scoring split would replace the record's snapshot for every task, swarm or not.
- **A no-recovery option for Frontier-CS's scorer** (inspect_evals), so swarm tasks need no adapter.
- **Re-scoring with a sandbox.** Inspect cannot re-score sandbox scorers; snapshots of the final environment would be needed.
- **Per-member trajectory scoring.** Scorers that read tool calls or intermediate messages would need each member's conversation in a scorer-readable form.
- **Harness-validity and cost as scores.** Showing a member that never acted, or realized cost, as `Score`s so they appear in eval-set summaries (Observer and limits work).
- **A verifier library.** Common verifiers (run visible tests, check a format, a model judge with no target) as reusable functions.
