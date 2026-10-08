# Inspect Swarm: scoring

Status: proposed, 2026-10-08. Issue: none. Author: agent (Claude), reviewed by Codex; see the PR.

A deeper dive on one topic of [swarm.md](swarm.md): how a swarm's results are scored. It details [Results and scoring](swarm.md#results-and-scoring-a-task-owned-contract) (the task-owned result contract) and the final-answer modes of [Controller](swarm.md#controller-topology-termination-final-answer). It covers answer-scored and shared-artifact tasks, per-member submissions, the comparisons with epochs (team@k, best@k, pass@k), the final-answer chain, verifiers, `synthesize`, per-member artifacts in a shared sandbox, what inspect_swarm ships for scoring, how all of it appears in logs and eval sets, and how a task opts in.

It respects the decisions recorded in swarm.md (Ransom, 2026-10-07): Python 3.11+, inspect_ai internals may be used, limits stay soft, members share the sample sandbox by default, M1 then M2 then the rest in any order. Sibling deep dives cover limits and the ledger, ORBIT alignment and inter-agent communication. This document refers to them and does not design them.

Code references are to inspect_ai `main` at `215cf087` (2026-10-08) and inspect_evals `main` at `a73e6d99` (2026-10-08). Paths are relative to each repository's root.

## Why

Swarm.md fixes the principle: what counts as a member's result belongs to the task, because Inspect scorers take a `TaskState` and a target and may inspect the sandbox. It leaves open everything an implementer needs to build it:

- **What the task supplies, and where.** The swarm (a solver) selects a final answer at run time. Scorers run afterwards. Both need the same facts about the task: whether answers are comparable, whether there is a verifier, whether members leave their own artifacts.
- **What "team@k" means.** The paper swarm.md cites defines team@k as "any of the k agents" solving the task, not the team's single final answer ([Test-Time Communication](https://arxiv.org/abs/2609.21032), ARC-AGI-3 and Terminal-Bench sections). An experiment that compares the swarm's *selected* answer against an *oracle* best-of-k from epochs, or the reverse, compares different things.
- **How the comparisons are computed.** Inspect ships `pass_at(k)` for binary scores and `max` for best-of-all-epochs, but nothing for best-of-k on graded scores, and nothing that applies a selection rule (verify, vote) to k independent attempts.
- **What scoring does to the cost comparison.** Per-member scoring runs the task's scorer k more times. A model-graded scorer's calls are added to the sample's usage, which swarm.md uses for arm totals (verified below).
- **What happens in the shared sandbox.** A shared-artifact scorer examines one environment. Scoring each member needs that member's artifact put in its place without disturbing the team score.

Getting these wrong produces numbers that look comparable and are not. That is the failure the result contract exists to prevent.

## Goals and non-goals

### Goals

- One task-supplied contract, read by both the swarm and the scorers, that says what kind of result the task has and what it offers for selection (answer key, verifier) and for per-member scoring (member artifacts).
- Every member's result recorded in the sample, in a form the scorers can read during the eval and when a log is re-scored.
- A precise final-answer chain with the decided default (verify, else vote, else first) and an explicit `synthesize`.
- Named, defined comparison quantities (final@k, team@k, mean member, pass@k, best@k, select@k), each paired with its fair counterpart, and the code that computes each.
- Per-member scores in shared-artifact tasks when, and only when, the task provides per-member artifacts.
- Scores that appear in logs and eval sets as ordinary Inspect scores and metrics, needing no viewer or schema change.

### Non-goals

- Changing any task's own scorer. The task's scorer stays the headline score in every arm.
- Scoring trajectories per member: scorers that read tool calls or intermediate messages, rather than the answer, are not scored per member.
- Defining the realized-cost ledger, the reserve or the swarm cap (the limits deep dive). This document states only the one constraint scoring puts on them.
- Harness-validity checks and swarm metrics as scores (swarm.md's [Observer](swarm.md#observer-evidence-accounting-and-metrics)).
- `reporter` and coordinator topologies, beyond reserving the name.
- Per-member sandboxes ([Sandbox topology](swarm.md#sandbox-topology) later work).

## Current behaviour

What the design depends on, in inspect_ai unless noted.

### Where scoring runs

- Scorers run once per sample after the solver, sequentially, in task order, inside a `scorers` span (`src/inspect_ai/_eval/task/run.py:3059-3115`).
- They run inside the sample's sandbox context, so `sandbox()` still reaches the environment the agents left (`run.py:2497-2512`).
- Each scorer gets the same `TaskState` object, and its result is added to `state.scores` after it returns. A scorer that writes its own entry there is an error (`run.py:3082-3087`).
- Scoring gets half the sample's time limit (`run.py:3034`).
- Scores a solver writes into `state.scores` before scoring are also recorded as sample scores (`run.py:3039`, `:3118-3135`). Their metrics come from a registered scorer of the same name, or the task's metrics, or `accuracy`/`stderr` (`src/inspect_ai/_eval/task/results.py:62-87`, `:130-143`).

### Scores, dict values and metrics

- A `Score` has `value`, `answer`, `explanation`, `reason` and `metadata` (`src/inspect_ai/scorer/_metric.py:118-142`). `Score.unscored()` sets the value to NaN, which metrics and reducers skip (`_metric.py:164-180`).
- A scorer may return a dict value and declare metrics per key, `@scorer(metrics={"key": [mean()]})` (`src/inspect_ai/scorer/_scorer.py:133-136`). Each key becomes its own `EvalScore` named after the key, and a NaN under a key counts that sample as unscored for that key only (`results.py:488-600`, `:523-534`).

### Epochs and reducers

- `Epochs(n, reducer=[...])` applies every reducer to every scorer's per-epoch scores (`src/inspect_ai/_eval/task/epochs.py:4-29`). A reducer sees only `list[Score]` for one sample and one scorer (`src/inspect_ai/scorer/_reducer/types.py:7-14`). There is no per-scorer reducer.
- `pass_at(k)` is the unbiased without-replacement estimator 1 − C(n−c, k)/C(n, k) for n ≥ k epochs, NaN when fewer than k are scored (`src/inspect_ai/scorer/_reducer/reducer.py:163-205`).
- `max` is best-of-all-n epochs (`reducer.py:247-301`). There is no best-of-k estimator for graded scores.
- `mode` and `majority` take the most common score *value* (`reducer.py:12-82`). They vote on correctness, not on answers, so neither is majority voting over the agents' answers.

### Usage during scoring

- Model calls a scorer makes are added to the sample's usage. I checked this with a spike: a solver that made no model call and a `model_graded_qa` scorer on mockllm gave the sample `model_usage` of 223 tokens, all of them the grader's. The `ScoreEvent` records a snapshot of sample usage after each scorer (`run.py:3102-3104`).
- So `EvalSample.model_usage`, which swarm.md uses for each arm's total ([Accounting](swarm.md#observer-evidence-accounting-and-metrics)), includes scoring.

### In-loop scoring

- `score(state)` runs the task's scorers, with the target, during the solve and records `ScoreEvent(intermediate=True)` (`src/inspect_ai/scorer/_score.py:14-80`).
- `react(attempts=...)` uses it to tell the model whether a submission was correct (`src/inspect_ai/agent/_react.py:327-357`). That is target-informed feedback, not a verifier.

### Agent output and member state

- `react()` updates the `AgentState` it was given in place: each generation sets `state.output` and appends to `state.messages` (`_react.py:770-771`). A submission sets `output.completion` to the answer (`_react.py:303-309`).
- `run()` copies its input into a new `AgentState` and returns it, or returns it with the `LimitExceededError` when one of its own limits fired (`src/inspect_ai/agent/_run.py:75-111`). A cancellation propagates and the state is not returned.
- `as_solver()` keeps a reference to the agent's state so that it can copy the output to the task state even when an exception ends the agent (`src/inspect_ai/agent/_as_solver.py:65-80`). A single-agent arm that hits its limit is therefore scored on its last output.

### Re-scoring

- `inspect score` rebuilds a `TaskState` from the logged sample, including `store`, `metadata` and `output` (`src/inspect_ai/_eval/score.py:461-475`). There is no sandbox. So anything a scorer needs at re-scoring time must be in the logged sample.

### Shared-artifact scorers in inspect_evals

- SWE-bench diffs the repository against the start commit and runs the test script in the sandbox; the value depends only on the environment (`inspect_evals: src/inspect_evals/swe_bench/scorers.py:28-88`).
- Frontier-CS extracts code from `output.completion` and compiles and runs it in the sandbox (`inspect_evals: src/inspect_evals/frontier_cs/scorer.py:661-676`). When the completion is empty it recovers the code from the sandbox (`scorer.py:631-676`). It is answer-scored, with the sandbox as an evaluator, except for that fallback, which reads the environment.

## Design

### Terms

- **Member output.** The member's `AgentState.output` when it stops, however it stops: what Inspect would have scored had that member been the only solver. The member runner keeps a reference to the member's `AgentState`, as `as_solver()` does, so the output survives the member's own limit and the drain. For agents that do not update their state in place, a cancelled member's output is empty.
- **Submission.** A member output from a member that finished normally (status `done`). For `react()` that is a submit; for a bridged agent it is the agent returning. A member stopped by its own limit, an error or the drain has an output but no submission. Each member submits at most once, because `react()`'s internal attempts are not visible outside it.
- **Final answer.** What the swarm returns as its `AgentState.output`, chosen by the final-answer chain from the submissions.
- **Answer key.** The task's comparable form of an answer, used for voting.

### The result contract

The task describes its result once and passes the same object to the swarm and to `member_scores()`:

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

**`kind="answer"`** asserts that the task's scorer is a function of the final answer: `output`, the last assistant message, the sample's input, metadata and target. It may use the sandbox as a fixed evaluator (compile and run the answer, as Frontier-CS does), but not as a source of the answer. A scorer that reads the trajectory, or reads the answer out of the environment, is not answer-scored for this purpose.

**`kind="artifact"`** asserts that the scorer examines the environment the agents leave behind (SWE-bench). The team has one result.

**`answer_key(text) -> str | None`** maps an output's completion to a comparable key: extract and normalise, for example a boxed number. It returns `None` when there is no comparable answer. It never sees the target. It is task code, so an exception from it propagates and fails the sample; a key function that cannot parse should return `None`.

**`verifier(answer, metadata) -> Verdict`** is the task's in-loop checker: visible tests, a proof checker, a format check, a model judge. It gets the answer text and the sample's metadata, and may use `sandbox()`. It is never given the target. The swarm cannot stop a verifier from reading the target through private state, so this is a documented contract; the verifier's registry name and parameters are recorded with every verdict so a reader can audit it. A verifier exception propagates and fails the sample, because silently degrading `verify` to `vote` would change what is measured. A verifier must not call `score()`, which runs the task's scorers with the target.

**`member_artifacts.materialize(member)`** is an async context manager that puts that member's artifact where the task's scorer looks for the team artifact, and restores the team artifact on exit, including on error. See [Shared-artifact tasks](#shared-artifact-tasks-and-per-member-artifacts).

A swarm without `result=` still runs. Its chain is `first`, its record says `kind: null`, and `member_scores()` reports per-member values as unavailable. Omitting the contract never produces a per-member claim.

### Opting in

A task opts in by building the spec and passing it to both sides. The task's own scorer stays first and unchanged, so the headline score is the same scorer in every arm:

```python
@task
def frontier_math(arm: str = "swarm", k: int = 4, budget: float = 40.0) -> Task:
    result = answer_result(answer_key=boxed_number, verifier=None)
    solver = (
        swarm(members=member(react(...), count=k), budget=cost_limit(budget), result=result)
        if arm == "swarm"
        else react(...)
    )
    return Task(
        dataset=...,
        solver=solver,
        scorer=[expression_equivalence(), member_scores(expression_equivalence(), result=result)],
        epochs=Epochs(k, ["mean", pass_at(k), best_at(k)]) if arm == "epochs" else 1,
        cost_limit=budget / k if arm == "epochs" else budget,
    )
```

`member_scores()` is listed in every arm. In an arm without a swarm it treats the solver as a swarm of one member, which is the baseline swarm.md describes, so all arms share scorer names and the analysis can line them up. In practice the swarm's cap sits below the sample limit by the final-answer reserve (the limits deep dive); the example leaves that out.

### What the swarm records

The controller keeps one record in the sample store under the key `inspect_swarm.result`, as `model_dump(mode="json")` of:

```python
class MemberResult(BaseModel):
    name: str                         # roster name, assigned by the eval author or controller
    role: str | None
    model: str | None
    status: Literal["running", "done", "limit", "errored", "cancelled"]
    output: ModelOutput | None        # member output (see Terms)
    time: float | None                # seconds from swarm start to the member stopping
    answer_key: str | None            # result.answer_key(output.completion), when defined
    verdict: Verdict | None           # verifier result for a submission, when a verifier exists

class FinalRecord(BaseModel):
    chain: list[str]                  # e.g. ["verify", "vote", "first"]
    mode: str | None                  # the mode that produced the answer
    member: str | None                # the member whose output was chosen; None for synthesize
    reason: str | None                # why there is no answer, e.g. "no_submissions", "none_verified"
    votes: dict[str, int] | None      # answer key -> members, when vote ran
    tie: bool = False                 # vote's plurality was tied and broken by time
    provisional: bool                 # True until finalisation completes

class SwarmResultRecord(BaseModel):
    version: Literal[1] = 1
    kind: ResultKind | None
    verifier: str | None              # registry name of the verifier, for audit
    members: list[MemberResult]
    final: FinalRecord | None
    final_verdict: Verdict | None     # artifact kind: verifier on the drained environment
```

- The controller writes the record when the swarm starts, updates it when a member stops (adding its output, key and verdict) and when the provisional answer changes, and finalises it after the drain. An outer limit that ends the sample early therefore leaves an accurate record with `provisional: true`.
- It is in the store, not only in events, because re-scoring rebuilds `TaskState.store` and nothing else from the run ([Re-scoring](#re-scoring)).
- Each stop, verdict and the final selection also produce an evidence `InfoEvent` (`source="inspect_swarm"`, versioned payload, kinds `member_result`, `verdict`, `final`), inside the member's span or the swarm's span. They put the result in the transcript at the time it happened; the store record is what scorers read. The InfoEvent payload format is the Observer's ([Observer](swarm.md#observer-evidence-accounting-and-metrics)); this design adds these three kinds.
- Outputs can be long (code). They are stored once, in the record; evidence events carry the member name and the answer key, not the text.

The swarm's returned `AgentState` has the sample's input messages followed by one assistant message: the final answer's `output.message`, with `metadata={"inspect_swarm": {"final": {"mode": ..., "member": ...}}}`. Its `output` is the chosen member's `ModelOutput`, unchanged, so the task's scorer sees exactly the object it would have seen had that member run alone. Members' own conversations stay in their spans and timelines.

### The final-answer chain

`swarm(final=...)` takes one mode or a sequence of modes. A sequence is a chain: each mode is tried in order on the submissions, and the first that produces an answer wins.

| Mode | Needs | Produces | Produces nothing when |
|---|---|---|---|
| `verify` | `result.verifier` | The passed submission with the highest `Verdict.value` (`None` ranks equal), earliest on ties | no submission passed |
| `vote` | `result.answer_key` | The earliest submission in the plurality group of answer keys; one vote per member; `None` keys do not vote. A tied plurality goes to the group whose first submission is earliest, and `tie` is recorded | no submission has a key |
| `first` | nothing | The earliest submission | there are no submissions |
| `synthesize` | `kind="answer"` | One extra model call over the submissions (below) | the call fails or there are no submissions |
| `reporter` | a coordinator topology | Reserved; rejected in M1 | |

**The default.** With `final=None`, the chain is built from what the task supplies: `verify` if there is a verifier, then `vote` if there is an answer key, then `first`. So a task with both gets `["verify", "vote", "first"]`, one with neither gets `["first"]` (decision: Ransom, 2026-10-07, for the order). This design reads the decision as a chain that falls through: when nothing verifies, the swarm votes, and when nothing can vote, it takes the first submission. A single mode, `final="verify"`, is strict: no verified submission means no answer. [Open question 1](#open-questions) asks Ransom to confirm the fall-through reading.

**Validation.** `swarm()` raises `ValueError` at construction when a mode's need is missing (`verify` without a verifier, `vote` without an answer key, `synthesize` on an artifact task, `reporter` on a leaderless swarm), naming the missing piece.

**When verdicts are taken.** When the task supplies a verifier, the controller verifies every submission as it arrives, whether or not `verify` is in the chain, so the analysis always has verdicts. Verifier calls run one at a time in the controller, inside the swarm's span, so their usage, if any, is in the ledger, charged to the swarm rather than to a member. Which limit node meters them is the limits deep dive's call.

**Provisional answer.** After each submission or verdict, the controller applies the chain without `synthesize` (which needs a model call) to the submissions so far and sets the result on the `AgentState` it was passed, as swarm.md's [exhaustion policy](swarm.md#observer-evidence-accounting-and-metrics) requires. When nothing qualifies, the output stays empty and `reason` says why. Nothing is invented.

**No submissions.** When no member submitted, the final answer is empty with `reason="no_submissions"`, even though members have outputs. An output from a member stopped mid-work is usually a working message, not an answer. This is deliberately conservative against the single-agent arm, which Inspect scores on its last output when it hits its limit. The per-member scores still score those outputs, so `best_member` reflects them.

**Finalisation.** After the drain (the limits deep dive owns its ordering against the reserve), the controller verifies any submission not yet verified, runs the chain once more including `synthesize`, writes the final record with `provisional: false`, writes the `final` evidence event and returns.

### `synthesize`

`synthesize` is explicit only, never a default, because it is the one place peer text enters a prompt the harness composes ([Security](swarm.md#security)).

```python
def synthesize(model: str | Model | None = None, prompt: str | None = None) -> FinalMode: ...
```

- **Model.** `model`, else the model role `synthesizer` if the eval defines it, else the swarm's default model (`get_model()`).
- **Prompt.** A fixed template: the sample's input, then each submission fenced as data under a sender line the harness writes (the member's roster name), with the fence markers removed from the payload, as in [Delivery](swarm.md#delivery-peer-messages-are-model-output). It ends with an instruction to give one final answer in the format the task asks for. Only submissions are included, never members' tool calls or transcripts. `prompt` replaces the instruction, not the fencing.
- **Shared notes.** In M1 there is no notes channel; the filesystem notes file is not read, because its contents are unfenced and member-written in any layout. If the notes channel is built later, its entries are fenced the same way.
- **Accounting.** The call runs in the swarm's span after the drain, inside the final-answer reserve, so the ledger counts it as finalisation.
- **Failure.** If the call raises (a limit, a model error), the chain moves to its next mode and the record says why. A chain of `["synthesize"]` alone then leaves the provisional answer, which is computed without `synthesize`.
- **Scoring.** The synthesized answer is no member's output. The task's scorer scores it; per-member scores are unaffected.

### Answer-scored tasks: per-member scores

`member_scores(scorer, result, *, value_to_float=value_to_float(), empty_value=0.0)` is a scorer (in `inspect_swarm.scorer`) that wraps the task's scorer:

```python
@scorer(metrics={"best_member": [mean(), stderr()], "mean_member": [mean(), stderr()]})
def member_scores(scorer: Scorer, result: ResultSpec, *, value_to_float=..., empty_value=0.0) -> Scorer: ...
```

For each sample it:

1. **Reads the members.** From the `inspect_swarm.result` record when it exists. Otherwise it treats the sample's `output` as a single member named `solver`, computes its answer key, and runs the verifier when one exists (inside a span named `verifier`, so its usage can be separated). That gives non-swarm arms the same per-attempt data as swarm members.
2. **Checks the contract.** If `result.kind` is not `"answer"`, or the record's kind differs from `result.kind`, it returns `best_member` and `mean_member` as NaN with `reason` saying why (`"artifact_without_member_artifacts"`, `"kind_mismatch"`, `"kind_not_declared"`). Shared-artifact tasks with member artifacts are handled [below](#shared-artifact-tasks-and-per-member-artifacts).
3. **Scores each member, in roster order.** For each member output it builds a deep copy of the `TaskState` whose `output` is the member's `ModelOutput` and whose `messages` are the sample's input messages plus the member's `output.message`, and awaits the task's scorer on it with the sample's target. A member with no output or an empty completion is not passed to the scorer, because an empty completion can make a scorer read the shared environment (Frontier-CS's fallback), which would credit the team's work to that member. It gets `empty_value` with reason `"empty_output"`, as Test-Time Communication counts unfinished tasks as zero.
4. **Converts values** with `value_to_float`. If the task's scorer returns a dict or list value, per-member values are unavailable (NaN, reason `"non_scalar_value"`): there is no general best of a dict.
5. **Returns** `Score(value={"best_member": max, "mean_member": mean}, metadata=...)`. The metadata holds, per member: value, answer, explanation, status, `submitted`, answer key, verdict and time; plus the record's `final` block and `kind`. The analysis reads these.

Scores of the copies are not added to `state.scores`, so they do not appear as extra sample scores. Each inner scoring runs inside a span named after the member, within `member_scores`'s scorer span, so a model-graded scorer's calls are attributable.

**Ordering.** `member_scores` must follow the task's own scorer in the scorer list. The task's scorer then scores the final answer, and the environment, before any per-member scoring can touch the sandbox. Answer-scored scorers that use the sandbox as an evaluator (Frontier-CS writes `solution.cpp` and outputs into its problem directory) run once per member in the same sandbox, one after another; this is safe when the scorer writes its own files before reading them, which is the task's responsibility.

**Cost.** A model-graded scorer is called k more times per sample in the swarm arm, and once more in other arms. That usage lands in the sample's totals (verified above), so arm totals must exclude the scoring phase ([Cost comparisons](#cost-comparisons-exclude-scoring)).

### Shared-artifact tasks and per-member artifacts

For `kind="artifact"`:

- **The team score** is the task's scorer on the drained environment. It is the only result the task defines, and it is computed first.
- **The final-answer chain** still runs and sets `output`, because some scorers read the output text and fall back to the environment (Frontier-CS). `synthesize` is rejected.
- **The verifier**, when given, checks the environment, not an answer. It is called with the submitting member's completion and the sample metadata whenever a member submits. The controller can use the verdict as a termination signal ([Controller](swarm.md#controller-topology-termination-final-answer)); members still working when it fires keep editing until the drain cancels them, so the environment that is scored can differ from the one that was verified. After the drain the controller runs the verifier once more on the final environment and records it as `final_verdict`, so that analysis can compare verified and scored outcomes.
- **Per-member scores** are unavailable unless the task supplies `member_artifacts`.

With `member_artifacts`, `member_scores` scores each member's artifact instead of its output text:

```python
for m in members:                                   # roster order
    async with result.member_artifacts.materialize(m.name):
        score = await scorer(copy_of_state_with_member_output(m), target)
```

- `materialize` is task code because only the task knows its layout. For a SWE-bench-shaped task where each member works in its own git worktree on branch `swarm/<member>` ([Sandbox topology](swarm.md#sandbox-topology)), it would commit or stash the team's working tree, check out the member's branch in the scored location, and on exit check the team's state back out.
- It is given only roster names, never names a member chose, so paths and branch names come from the eval author's layout.
- It runs after the team score and restores the team state on exit, so the team score never depends on per-member scoring. A `materialize` that raises propagates and fails the sample, as any scorer error does.
- **Attribution in a shared sandbox is by convention.** Any member can write into another member's worktree or branch. Per-member artifact scores say "what was on that member's branch", not "what that member did alone". Tool-call evidence can show cross-writes, but only as the observation limit in [Security](swarm.md#security) allows.
- Re-scoring has no sandbox, so artifact scores (team and per-member) cannot be recomputed by `inspect score`, as for any sandbox scorer today.

Swarm.md places per-member scoring of shared-artifact tasks outside M1. This design keeps that: M1 reports the team score, and `member_artifacts` is a later item ([Implementation plan](#implementation-plan)).

### Comparison quantities

Each quantity is defined once and paired with the one it is fairly compared with. k is the number of members, or the number of independent attempts.

| Quantity | Arm | Definition | Uses the target to select? | Computed by |
|---|---|---|---|---|
| **final@k** | swarm of k | The task's scorer on the swarm's final answer (answer tasks) | No | the task's scorer, `mean` over epochs |
| **team@k** | swarm of k | Answer tasks: the best member output, `best_member`. Artifact tasks: the shared environment's score, equal to final@k | Answer: yes. Artifact: no | `member_scores` (`best_member`); the task's scorer |
| **mean member** | swarm of k | Mean of member output scores, `mean_member` | No | `member_scores` |
| **pass@k** | n ≥ k epochs | P(at least one of k attempts correct), binary scores, without replacement | Yes | Inspect `pass_at(k)` |
| **best@k** | n ≥ k epochs | E[max over k attempts], graded scores, without replacement | Yes | inspect_swarm `best_at(k)` |
| **select@k** | n ≥ k epochs | The swarm's final-answer chain applied to k independent attempts | No | `inspect_swarm.analysis` |
| **single** | one agent at the full budget | The task's scorer | No | the task's scorer |

team@k follows Test-Time Communication's usage: "a game counts as solved if any of the k agents clears all levels", while on Terminal-Bench "team@2 contributes one shared final state". The fair pairs are:

- **team@k against pass@k or best@k.** Both pick the best of k outputs with knowledge of the target. They measure whether communication raises the capability present in k agents.
- **final@k against select@k, and against single.** Both apply a rule that does not see the target. They measure what a deployed swarm returns against what k independent attempts with the same selection rule, or one bigger attempt, return. The gap team@k − final@k is what the final step loses.
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

select@k needs each attempt's answer key, verdict and finishing time, which a reducer cannot use across scorers. `member_scores` records them in every arm (step 1 above), so select@k is computed in the analysis helper, not as a reducer:

- For each sample, take the n epochs as a pool of attempts. For each k-subset, apply the same chain as the swarm arm: `verify` picks the passed attempt with the highest verdict value, `vote` the plurality answer key, `first` the attempt with the shortest working time (`EvalSample.working_time`, the analogue of finishing first when the attempts run side by side), ties to the lower epoch. The subset's score is the task's score of the chosen attempt.
- select@k is the mean over all C(n, k) subsets when that is at most 10,000, otherwise over 10,000 subsets sampled with a fixed seed (recorded in the result).
- `synthesize` has no epoch analogue, because it is a new model call; a swarm using it is compared on final@k against `single` and select@k without it.
- If the epochs arm's verifier makes model calls at scoring time, the helper reports that usage as selection cost, separately from the arm's solve cost.

### Cost comparisons exclude scoring

Swarm.md takes each arm's total from Inspect's sample usage ([Accounting](swarm.md#observer-evidence-accounting-and-metrics)). Sample usage includes scorer model calls (verified above), and `member_scores` makes k + 1 times as many scorer calls in the swarm arm as in the single-agent arm. So:

- The realized solve cost of every arm is sample usage minus the usage of model events inside the `scorers` span, with the ledger's rules for cache replays. The limits deep dive, which owns the ledger, adopts this; this document only states the constraint.
- Scoring usage is reported separately, per scorer, so the cost of per-member scoring is visible.

### Re-scoring

- **Answer tasks.** `member_scores` reads only the store record, the sample's input and the target, so `inspect score` reproduces it, and can apply a new task scorer to every member's output after the fact.
- **Artifact tasks.** Not re-scorable, as for any sandbox scorer.
- **Old records.** `member_scores` checks `version`. A record with an unknown version is reported as unscored with reason `"unknown_record_version"`, never misread.

### How it appears in logs

| Where | What |
|---|---|
| Sample `output` and last assistant message | The final answer, the chosen member's own `ModelOutput`; message metadata names the mode and member |
| Sample `store["inspect_swarm.result"]` | The full record: every member's output, status, key, verdict, time; the final selection; `final_verdict` |
| Transcript | `InfoEvent`s of kinds `member_result`, `verdict` and `final` (source `inspect_swarm`) at the time they happened; `member_scores`' per-member spans under the scorer span |
| Sample scores | The task's scorer (final@k, or team@k for artifact tasks); `member_scores` with `best_member` and `mean_member`, per-member detail in its metadata |
| Eval results | One `EvalScore` per task scorer and reducer; `member_scores` contributes `best_member` and `mean_member` as separate `EvalScore`s with `scored_samples` and `unscored_samples` counts |

Nothing here needs a new event type, schema change or viewer change. The viewer shows the store, `InfoEvent`s and dict scores generically.

Because Inspect applies every epoch reducer to every scorer and key, an epochs arm with `["mean", pass_at(k), best_at(k)]` also shows, for example, `pass_at` of `mean_member`. Those cells are well defined but not meaningful; the analysis helper selects the cells that are.

### How it appears in eval sets

- **Arms as task arguments.** Each arm is the same task with different arguments (`arm`, `k`, `budget`), as in [Opting in](#opting-in). `eval_set` and inspect_flow sweep them, and each arm's log carries its arguments in `eval.task_args`.
- **Identifying arms.** The analysis helper classifies a log as a swarm arm when its samples carry the `inspect_swarm.result` record (k is the member count), and otherwise as an independent-attempts arm (n is the log's epochs). Arguments the user names (`arm`, `budget`) label the rows.
- **Same scorers everywhere.** Because `member_scores` runs in every arm, all arms have the same scorer names, so `evals_df` and `samples_df` (`inspect_ai.analysis`) line them up without renaming.

### The analysis helper

`inspect_swarm.analysis` adds the scoring part of swarm.md's "thin analysis helpers":

```python
def best_at_k(values: Sequence[float], k: int) -> float: ...
def select_at_k(attempts: Sequence[Attempt], k: int, chain: Sequence[str], *, max_subsets: int = 10_000, seed: int = 0) -> float: ...
def compare_arms(logs: Sequence[str | EvalLog], *, scorer: str, k: int) -> list[ArmScores]: ...
```

- `Attempt` is one epoch's (value, answer key, verdict, working time), read from `member_scores` metadata.
- `ArmScores` has, per arm: kind (swarm or attempts), k or n, the arm's labels, and the quantities of [Comparison quantities](#comparison-quantities) that apply to it, each with its count of scored samples.
- The realized-cost columns, λ fits and curves are the ledger's and the limits deep dive's; `compare_arms` returns rows they can join on (log, sample id, epoch).
- It also flags samples whose solve phase contains `ScoreEvent(intermediate=True)`: a member or agent consulted the task's scorer with the target in the loop (for example `react(attempts=...)`). In a swarm that feedback can be shared with peers, so those samples are marked as target-assisted.

## Alternatives considered

**Compute per-member scores inside the swarm.** The swarm calls the task's scorer at the end of the solve. It cannot, cleanly: scorers belong to the task, need the target (which the solver should not use), and their usage would land in the solve. Re-scoring would also miss them. Rejected.

**Solver-written scores.** The swarm writes `state.scores["best_member"]`, which Inspect records as a sample score (`run.py:3118-3135`). Same objection: the solver would have to run the task's scorer with the target during the solve. Rejected.

**Replace the task's scorer with a swarm scorer** that returns `{"final", "best_member", "mean_member"}`. One scorer for everything, but the headline score would then differ in name and shape between the swarm arm and the single-agent arm, and a task's metrics would change. Keeping the task's scorer first and unchanged is what makes final@k and single the same measurement. Rejected.

**Separate scorer per member** (`member_1`, `member_2`, …). Shows each member as a column, but the number of scorers varies with k, so arms do not line up and metrics over "member 3" mean nothing in a leaderless swarm. Per-member detail lives in metadata instead.

**Selection comparisons as epoch reducers** (`select_at(k)`). They would put select@k in the log. A reducer sees one scorer's scores, so the answer keys and verdicts would have to ride in `member_scores` metadata, Inspect would apply the reducer to every other scorer too, and the subset estimator with sampling does not fit a reducer well. `best_at(k)` is a closed form on values alone, so it is a reducer; select@k is in the helper.

**Voting with Inspect's `mode`/`majority` reducers.** They vote on correctness values, not on answers. Majority-of-correctness is not a selection rule a deployed system could apply. Rejected.

**Strict modes only** (verify yields nothing when nothing verifies). Simpler, and measures the verifier alone. But it makes the default swarm return nothing where a single agent would be scored on its answer, which biases the comparison against the swarm for a reason unrelated to coordination. Strict behaviour stays available as `final="verify"`. [Open question 1](#open-questions).

**Score unsubmitted outputs as the last fallback.** Matches how Inspect scores a single agent that hits its limit. But it selects working messages as answers, and swarm.md decided that nothing is invented. Per-member scores still score those outputs.

**Per-member sandboxes for per-member artifacts.** Clean attribution, and the isolation epochs have. But it changes the default topology Ransom decided (shared sandbox), and the shared filesystem is M1's channel. It stays [later work](swarm.md#sandbox-topology); `materialize` covers the shared default.

**Pass the contract only to the swarm**, and let `member_scores` read it from the store. Callables (key, verifier, `materialize`) cannot be stored, and arms without a swarm have no record. Passing the same `ResultSpec` to both is one more argument and nothing more.

## Compatibility and migration

- **inspect_swarm** has no released API; everything here is new. Python 3.11+ as decided.
- **Eval logs.** Only existing structures: a store key, `InfoEvent`s, scores with dict values and metadata, a message metadata key. Old readers and the viewer show them generically. The store record and the `InfoEvent` payloads carry `version: 1`.
- **Scorer and key names** (`member_scores`, `best_member`, `mean_member`) become an interface the analysis and users' notebooks depend on once released; changing them later is a breaking change.
- **inspect_ai.** No change needed. The design uses public scorer and reducer APIs, the store, `transcript().info`, and the member runner's reference to its `AgentState`.
- **Tasks.** Tasks opt in. A task that does not pass `result=` behaves as swarm.md describes, with `first` and no per-member claims.
- **Swarm.md.** This PR makes small edits there where this document refines it: team@k's definition and its fair pairs; the chain's fall-through and the strict single mode; the provisional answer's definition and its test bullet; the constraint that arm totals exclude scoring; links to this document; and per-member artifacts as a later menu item. The overview's decisions are unchanged.

## Security

Untrusted input reaching the new code:

- **Member outputs are model output.** They reach:
  - `answer_key`, task code that must accept any text and return `None` rather than raise on unparseable input;
  - the verifier, which may execute the answer (code) and must do so only inside the sandbox;
  - the task's scorer, as they would from a single agent;
  - `synthesize`'s prompt, fenced as data with harness-written sender lines and markers stripped, in a separate call outside every member's context. That is weaker than tool output, so `synthesize` is never a default.
- **The target.** Neither the verifier, nor `answer_key`, nor any member tool is given the target. `react(attempts=...)` members do consult the task's scorer in the loop; the analysis flags those samples ([The analysis helper](#the-analysis-helper)).
- **Shared-sandbox attribution.** In a shared sandbox, any member can alter another's worktree or branch, so per-member artifact scores are attributions by convention. `materialize` receives only roster names, never member-chosen names, so a member cannot steer which path is materialized. Detecting cross-writes relies on tool-call evidence and its stated limit.
- **Logs.** Outputs and answer keys are stored as JSON data in the store and in score metadata, not as Markdown.
- **Reward hacking.** Members can read whatever the sandbox holds, scorer test files included, exactly as a single agent can. Nothing here adds or removes that exposure.

## Testing

All runtime tests use mockllm with scripted outputs, need no network or Docker, and run on asyncio and trio:

- **Chain selection** (table-driven, over lists of member results): `verify` (best verdict value, ties by time, none passed); `vote` (plurality, `None` keys abstain, ties by earliest, one vote per member); `first`; fall-through in the default chain; the strict single mode leaving an empty answer with its reason; construction errors for each missing need.
- **Provisional answer** follows the chain without `synthesize` after each submission and verdict, and an outer limit leaves it as the sample's output with `provisional: true`.
- **Verifier** is called once per submission, one at a time, never with the target; its exception fails the sample.
- **`synthesize`**: the prompt fences each submission under the harness's sender line with markers stripped and no tool calls; its usage falls inside the swarm's span; a failure falls through the chain.
- **The record**: written at start, updated on each stop, finalised after the drain; member output kept after the member's own limit and after the drain for an agent that updates its state in place.
- **`member_scores`** with `match()`: per-member values, `best_member` and `mean_member`; empty outputs get `empty_value` and never reach the scorer; artifact kind without artifacts, kind mismatch and missing contract give NaN with the right reason; a dict-valued inner scorer gives `non_scalar_value`; a non-swarm arm is scored as one member with its key and verdict; the copies do not appear in `state.scores`.
- **Re-scoring**: `inspect_ai`'s `score()` on a written log reproduces `member_scores` for an answer task from the store alone.
- **`best_at(k)`** against brute-force enumeration (table of small cases), equality with `pass_at(k)` on binary values, NaN with fewer than k scored epochs, dict values per key.
- **`select_at_k`**: exact enumeration on small pools, the seeded sample above 10,000 subsets, `first` by working time.
- **Scoring-phase usage**: a model-graded inner scorer's usage is under the `scorers` span and is excluded by the helper's solve-cost rule (the spike above becomes a test).
- **`member_artifacts`** (later item): with the local sandbox and files standing in for branches, the team score runs before any `materialize`, each member is scored with its artifact in place, and the team state is restored on exit and on a scorer error. The local sandbox needs no Docker. A git-worktree layout test needs Docker and is marked slow, following inspect_ai's conventions.

## Implementation plan

This work is part of M1, after the swarm core (members, controller, drain) exists. One PR per step:

1. **Result contract and record.** `src/inspect_swarm/_result.py` (`ResultSpec`, `answer_result`, `artifact_result`, `Verdict`, `Verifier`, `MemberArtifacts`, the record models); the controller writes and updates the record and the `member_result`/`verdict` events (`_swarm.py`, `_member.py`, `_evidence.py`); the member runner keeps its `AgentState` reference. Exports in `src/inspect_swarm/__init__.py`. Tests in `tests/test_result.py`.
2. **Final-answer chain.** `src/inspect_swarm/_final.py`: modes, chain construction and validation, provisional answer, finalisation, the `final` event and the returned `AgentState`. Tests in `tests/test_final.py`.
3. **`synthesize`.** In `_final.py`, with its prompt template in `src/inspect_swarm/_prompts.py`. Tests extend `tests/test_final.py`.
4. **Scorers.** `src/inspect_swarm/scorer/__init__.py`, `scorer/_member_scores.py` (answer kind; artifact kind returns unavailable), `scorer/_best_at.py`. Tests in `tests/scorer/`.
5. **Analysis.** `src/inspect_swarm/analysis/__init__.py`, `analysis/_scores.py` (`best_at_k`, `select_at_k`, `compare_arms`, the target-assisted flag, solve-cost rule for scoring). Tests on synthetic logs in `tests/analysis/`.

Later, in any order with the rest of swarm.md's menu:

6. **Per-member artifacts for shared-artifact tasks.** `member_artifacts` in `member_scores`, the artifact-kind verifier's `final_verdict`, and a worked SWE-bench-shaped example layout in the docs.

## Open questions

1. **Does the default chain fall through?** With a verifier and an answer key the default is `["verify", "vote", "first"]`. This design falls through when a mode yields nothing (nothing verified, so vote; no keys, so first). The alternative reads the decision as choosing one mode by what the task supplies, so nothing verified means no answer.
   - (a) Fall through (this design). The swarm returns an answer whenever anyone submitted, as a single agent would. The strict reading stays available as `final="verify"`.
   - (b) Strict. Measures the verifier alone, and returns no answer more often than the single-agent arm.

   Recommendation: (a). Swarm.md's provisional-answer text ("or verified when the mode requires it") and its test bullet ("zero verified submissions leave an empty output") are edited in this PR to match (a), applying the strict reading to `final="verify"` only.

## Not this design

- **`best_at(k)` in inspect_ai.** It generalises `pass_at(k)` to graded scores and belongs beside it; propose it upstream once it has been used here.
- **Per-scorer epoch reducers** (inspect_ai). Today every reducer applies to every scorer and key, which fills logs with meaningless cells such as `pass_at` of `mean_member`.
- **Solve-phase usage in the eval log** (inspect_ai). Sample usage mixes solve and scoring; a separate solve-phase total, or usage on the `scorers` span, would remove the subtraction the helper does.
- **Per-member trajectory scoring.** Scorers that read tool calls or intermediate messages would need each member's conversation in a scorer-readable form.
- **Harness-validity and cost as scores.** Showing a member that never acted, or realized cost, as `Score`s so they appear in eval-set summaries (Observer and limits work).
- **A verifier library.** Common verifiers (run visible tests, check a format, a model judge with no target) as reusable functions.
