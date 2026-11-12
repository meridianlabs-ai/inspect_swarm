# Inspect Swarm: scoring

## Overview

A swarm is scored the way any agent is. It is the task's solver, so the task's own scorers run after it and grade what it leaves behind: its answer in the sample's output, or the shared environment its members worked in. The swarm adds no scorer of its own to that headline score. It chooses one answer from its members' submissions, keeps that answer current on the sample's output while it runs so a limit that ends the sample early still leaves it, and writes a result record to the transcript when it ends, saying what each member did and which answer it chose and why. With no member submission the output is empty: an output from a member that a limit, the stop or an error ended is never passed off as an answer. A non-persistent member's normal return counts as a submission unless its output stopped as `react()` does without an answer, so a loop that returns early on its own, such as through an `on_continue` turn limit, submits its last reply.

A task can say more about its result with a *result contract*, `swarm(result=answer_result(...))` or `artifact_result(...)`: whether it is scored on an answer or on an artifact, an *answer key* that makes answers comparable, a *verifier* that checks each submission during the run, and, for artifact tasks, a way to put each member's artifact where the scorer looks. The controller's `final=` chain then chooses the answer: `verify` (the best passing submission), else `vote` (the most common answer key), else `first` (the earliest submission) by default, with `last` and `synthesize` (one model call over the submissions) available explicitly, and a controller can choose the answer itself. Each answer of a persistent member is a submission of its own, keyed when it is given and verified while the swarm runs, and the member goes idle only once that answer's verdict is taken. The record keeps every member's output, key and verdict, and each persistent answer's key and verdict, so `member_scores()` can grade each member after the run, giving team-level numbers, the best member (team@k) and the mean member, beside the task's own score. Re-scoring reads the record from the log, so it grades the members' outputs as the log holds them.

For comparisons, `baseline()` runs a single agent as a swarm of one under the same rules and records, and the analysis helpers read one row per sample and apply the swarm's own selection rule to k independent attempts (select@k). Separating the cost of solving from the cost of grading is designed but deferred; until then an arm's cost includes grading, as in every Inspect eval.

## Where it lives

The code is the result contract in [`_result.py`](../src/inspect_swarm/_result.py); the chain, its modes, the provisional answer, finalisation and `synthesize` in [`_final.py`](../src/inspect_swarm/_final.py), with `synthesize`'s prompt in [`_prompts.py`](../src/inspect_swarm/_prompts.py) and the shared fence in [`_fence.py`](../src/inspect_swarm/_fence.py); the record, its writer and `swarm_result()` in [`_record.py`](../src/inspect_swarm/_record.py); verification, keying, the controller's choice and the final step in `SwarmRun` in [`_controller/_control.py`](../src/inspect_swarm/_controller/_control.py); `final=` and `stop_on_verified=` in [`_controller/_controller.py`](../src/inspect_swarm/_controller/_controller.py) and [`_controller/leaderless.py`](../src/inspect_swarm/_controller/leaderless.py); `swarm(result=)`, its checks and `baseline()` in [`_swarm.py`](../src/inspect_swarm/_swarm.py); `member_scores()` and `MEMBER_METRICS` in [`scorer/_member_scores.py`](../src/inspect_swarm/scorer/_member_scores.py); `attempt_rows()` and `select_at_k()` in [`analysis/_scores.py`](../src/inspect_swarm/analysis/_scores.py).

The user docs are [Scoring](../docs/scoring.qmd) (the answer, the record, the result contract, member scores, per-member artifacts and comparisons), [Controllers, Choosing the answer](../docs/controllers.qmd#choosing-the-answer), [Members, A swarm of one](../docs/members.qmd#a-swarm-of-one) and [Analysis, Attempts](../docs/analysis.qmd#attempts). Where the record lives and how it is read is [swarm-result-record.md](swarm-result-record.md); the evidence events are [swarm-event-types.md](swarm-event-types.md); the statuses, the stop procedure and the meters are [swarm-limits.md](swarm-limits.md); persistent members' answers are [swarm-persistent-members.md](swarm-persistent-members.md#the-swarms-answer).

## Why

What counts as a member's result belongs to the task, because Inspect scorers take a `TaskState` and a target and may inspect the sandbox ([swarm.md](swarm.md#results-and-scoring-a-task-owned-contract)). Existing scorers must keep working with a swarm as the agent, unchanged. Claims beyond the swarm's one answer need facts only the task has and evidence only the swarm sees:

- **What the task supplies, and where.** The swarm chooses an answer at run time; scorers run afterwards, and again when a log is re-scored. Both need the same facts: whether answers are comparable, whether there is a verifier, whether members leave artifacts of their own.
- **What "team@k" means.** The paper swarm.md cites defines team@k as "any of the k agents" solving the task, not the team's single answer ([Test-Time Communication](https://arxiv.org/abs/2609.21032)). Comparing the swarm's *chosen* answer with an *oracle* best-of-k from epochs, or the reverse, compares different things.
- **How the comparisons are computed.** Inspect has `pass_at(k)` for binary scores and `max` over all epochs, but nothing for best-of-k on graded scores, and nothing that applies a selection rule (verify, vote) to k independent attempts.
- **The shared sandbox.** A shared-artifact scorer examines one environment, and some scorers that look answer-scored fall back to reading the environment, which would credit the team's work to one member.

Getting these wrong gives numbers that look comparable and are not. The result contract, the record and the named comparison quantities exist to prevent that.

## Terms

- **Member output.** The member's `AgentState.output` when it ends, however it ends: what Inspect would have scored had that member been the only solver. The member wrapper builds the member's `AgentState` and keeps it, as `as_solver()` does, rather than calling the agent through `run()`, which discards the state when the agent raises, so the output survives the member's own limit and the stop procedure. At the member's end the wrapper takes the output from the state the agent returned, if it returned one, else from the state its own loop passed at its last turn boundary ([swarm-turn-seam.md](swarm-turn-seam.md#the-members-gate)), else from the state it called the agent with; so a member whose agent raised or was cancelled has an empty output only when neither of the last two holds one, as for a custom agent that never reaches a turn boundary and leaves the state it was called with unchanged. A member with background work keeps the output its agent ended with through its drain ([swarm-background-members.md](swarm-background-members.md#entering-the-owner)).
- **Submission.** The output of a member that ended with status `submitted`. A non-persistent member's normal return, with no stop, end request or self-stop overriding it, is classified by `output_status()` in [`_member.py`](../src/inspect_swarm/_member.py): an empty output, or one whose stop reason is `stop`, `tool_calls`, `max_tokens` or `unknown`, is `submitted`, and `content_filter` or `model_length` (how `react()` returns after three refusals or an overflow it could not recover from) is `unsubmitted`. So for `react()` a submission is usually a submit call, and for a bridged agent its return, but a `react()` whose `on_continue` returns `False` (a turn limiter such as inspect_evals' `TurnLimit`) returns its last reply, which is `submitted` though it never called submit and can be chosen, keyed and verified. A persistent member's normal return is always `unsubmitted`, since its answers are recorded where its loop gives them ([swarm-persistent-members.md Endings](swarm-persistent-members.md#endings)). A member that ended any other way (`unsubmitted`, `stopped`, a member limit, `swarm_cap`, `cancelled`, `errored`; the closed set is [swarm-limits.md](swarm-limits.md#stopping)'s) has an output but no submission. A foreign error from a member's background child during its drain makes it `errored` even when its agent had submitted. Each answer of a persistent member is a submission ([swarm-persistent-members.md](swarm-persistent-members.md#the-swarms-answer)).
- **Not started.** A member the controller never started stays `pending`: no output, no submission.
- **Final answer.** What the swarm returns as its `AgentState.output`.
- **Swarm time.** Seconds on the runtime's clock (`anyio.current_time()`) from the swarm's start. Every ordering by time, in the swarm and in the analysis, uses it.
- **Gate checks.** A member's own retries are visible as checks: each submission `react(attempts=...)` checks with the task's first scorer, in the member's own agent span, counts in `gate_checks`, with `gate_passes` (a value of 1.0 under the default `value_to_float()`) and `first_pass_time` ([swarm-lean-open-problems.md Recommendation 5](swarm-lean-open-problems.md#5-m2-gate-checks-as-metrics)). The submission is still the member's output when it ends.

## The task's own scorers

Existing scorers work unchanged with the swarm as the agent. `swarm()` is an `@agent` the task uses as its solver, and Inspect runs the task's scorers after the solver, on the same `TaskState`, inside the sample's sandbox context:

- an answer scorer (`match()`, `expression_equivalence()`, `model_graded_qa()`) reads `TaskState.output`, which holds the swarm's final answer, the chosen member's own `ModelOutput`;
- a shared-artifact scorer (SWE-bench's) reads the environment the drained swarm left. The output still carries the chosen submission, since some scorers read the output and fall back to the environment. Members still working change what is scored, so such tasks pass `leaderless(stop_on_submission=False)` to let every member run to its end ([Scoring what members leave](../docs/scoring.qmd#scoring-what-members-leave)).

Neither needs to know the solver was a swarm, nor needs the result contract.

**Sample usage includes scoring, in every Inspect eval.** `EvalSample.model_usage` is read when the sample is logged, after scoring, so a model-graded scorer's calls are in it. Every arm grades its own output the same way (once per sample, or once per epoch), so arms are compared on sample usage with grading included, as existing Inspect evals are (decision: Ransom, 2026-10-08). Per-member scoring breaks that symmetry, which is what [the cost boundary](#the-cost-boundary-deferred) would address.

### What Inspect provides

The shapes below follow from these facts about inspect_ai and inspect_evals:

- **Where scoring runs.** Scorers run once per sample after the solver, in task order, with `state.completed` set to `True`, inside the sample's sandbox context, within half the sample's time limit (`src/inspect_ai/_eval/task/run.py`). A scorer's result is added to `state.scores` after it returns.
- **Scores.** A `Score` has `value`, `answer`, `explanation`, `reason` and `metadata`; `Score.unscored()` sets a NaN value, which marks the result unscored; metrics skip it, and each reducer defines how it handles it. A scorer may return a dict value with metrics per key (`@scorer(metrics={"key": [...]})`), and each key becomes its own `EvalScore`, a NaN under one key counting that sample unscored for that key only.
- **In-loop scoring.** `score(state)` runs every task scorer with the target during the solve, with `completed=False`, and drops `None` results. Its callers include `react(attempts=...)`, `basic_agent`, the human agent's `/score`, inspect_swe's attempt loops and live control scoring. `react()` reads only the first score. This is target-informed feedback, not a verifier.
- **Logged arguments.** A registered scorer's arguments are logged as JSON; a registry object becomes a dict that is rebuilt on replay, but a plain object such as a dataclass of callables becomes its type name and a callable its `__name__` (`src/inspect_ai/_util/registry.py`). Default `inspect score` rebuilds each scorer from its logged name and arguments, loading the task file when the scorer was defined there, so a registered no-argument factory survives re-scoring.
- **Re-scoring** rebuilds a `TaskState` from the logged sample (`store`, `metadata`, `output`, `completed=True`) and seeds the scorer's transcript with the sample's events. There is no sandbox: `sandbox()` raises `ProcessLookupError`.
- **Epochs and reducers.** Every reducer applies to every scorer and key, and sees only one scorer's `list[Score]` for one sample. `pass_at(k)` is the unbiased without-replacement estimator, NaN with fewer than k scored epochs; `max` is best of all epochs. `mode` takes the most common scored value; `majority` requires a value held by more than half of all the epochs' scores, unscored ones included in that total, and is NaN otherwise. Both reduce score values: they vote on correctness, not on answers.
- **Usage and timing.** Native compaction adds usage with no model event, so model events alone cannot separate solving from scoring. `EvalSample.working_time` is computed after scoring and excludes waiting, so it is not when an attempt finished solving.
- **A scorer that reads the environment.** inspect_evals' Frontier-CS scorer extracts code from the last fenced block of `output.completion` and, when the extracted code is empty, recovers code from the sandbox's files (`src/inspect_evals/frontier_cs/scorer.py`). A non-empty completion holding an empty fenced block also triggers the recovery, so that scorer is not answer-scored in this design's sense.

## The result contract

The task describes its result once:

```python
# src/inspect_swarm/_result.py, exported from inspect_swarm
ResultKind = Literal["answer", "artifact"]

@dataclass(frozen=True)
class Verdict:
    passed: bool
    value: float | None = None        # optional graded verdict; higher is better
    explanation: str | None = None    # for the record; a controller never sees it

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

`ResultSpec` refuses (`ValueError`) a `kind` outside `ResultKind` and `member_artifacts` with `kind="answer"`.

- **`kind="answer"`** asserts that the scorer given to `member_scores()` is a function of the answer: the output, the last assistant message, and the sample's input, metadata and target. It may use the sandbox as a fixed evaluator (compile and run the answer), but never as a source of the answer. A scorer that reads the trajectory, or can read the answer out of the environment (the unchanged Frontier-CS scorer), is not answer-scored: such a task declares `kind="artifact"`, or passes `member_scores()` an answer-only adapter of its scorer that scores empty extracted code as zero.
- **`kind="artifact"`** asserts that the scorer examines the environment the members leave behind (SWE-bench). The team has one result.
- **`answer_key(text) -> str | None`** maps a completion to a comparable key (extract and normalise, such as a boxed number), or `None` when there is no comparable answer. It never sees the target. It is task code: an exception from it propagates and fails the sample, and so does a result that is not a `str` or `None` (`TypeError`, checked by its actual class before the key is stored). A key function that cannot parse returns `None`.
- **`verifier(answer, metadata) -> Verdict`** is the task's in-loop checker: visible tests, a proof checker, a format check, a model judge. It gets the submission's completion and the sample's metadata, may use `sandbox()`, and is never given the target. The swarm cannot stop it from reading the target through private state; the documented contract is that it must not, nor call `score()`. An exception from it fails the sample, since silently degrading `verify` to `vote` would change what is measured. So does a return that is not a `Verdict`, or a `Verdict` whose `passed` is not a `bool`, `value` not a float, an `int` a float holds exactly, or `None`, or `explanation` not a `str` or `None` (`TypeError`). Verifiers run only during the solve and its final step; scoring and re-scoring read recorded verdicts. The record copies a verdict's declared fields into a plain `Verdict`, whatever subclass the verifier returned, and stores a label for the verifier for audit: its `name`, else its `__qualname__`, else its class's.
- **`member_artifacts.materialize(member)`** is an async context manager that puts that member's artifact where the scorer looks for the team's, and restores the team's on exit, including on error ([Shared-artifact tasks](#shared-artifact-tasks-and-per-member-artifacts)).

The answer key and the verifier are task code and fall under the same trust rule as the rest of it: they are trusted to honour cancellation, return their real types and build a `Verdict` the ordinary way, so the type checks catch mistakes, not a callback written to defeat them (decision: Ransom, 2026-11-04). Either error leaves the record with the member's end and output, and any key or verdict recorded before it; the ledger and metrics are written.

A swarm without `result=` runs as before: its default chain is `["first"]`, its record has no `kind`, and `member_scores()` reports its per-member values as unavailable. Omitting the contract never produces a per-member claim.

`ResultSpec` is a frozen dataclass of the task's callables, which no serialisable form could rebuild. `swarm()` logs it as the type name `"ResultSpec"`, and refuses that string with `TypeError` when a raw-solver replay (`--solver inspect_swarm/swarm -S ...`) passes it back. The supported routes are the task itself, or a registered `@agent` builder that passes `result=` ([swarm-api.md Logging and replay](swarm-api.md#logging-and-replay)).

### Opting in

Because the spec holds callables, which Inspect logs as a type name ([What Inspect provides](#what-inspect-provides)), a task keeps it in one function, passes it to `swarm()` directly, and wraps `member_scores()` in a **registered, no-argument scorer factory of its own**, which re-scoring rebuilds by name:

```python
def frontier_math_result() -> ResultSpec:
    return answer_result(answer_key=boxed_number)

@scorer(metrics=MEMBER_METRICS)
def frontier_math_members() -> Scorer:
    return member_scores(expression_equivalence(), frontier_math_result())

@task
def frontier_math(arm: str = "swarm", k: int = 4, budget: float = 40.0) -> Task:
    agent_args = {...}   # react()'s arguments
    solver = (
        swarm(
            members=member(react, args=agent_args, name="solver", count=k),
            controller=leaderless(stop_on_submission=False),   # every member runs to its end, for team@k
            budget=Budget(cost=budget),
            result=frontier_math_result(),
        )
        if arm == "swarm"
        else baseline(member(react, args=agent_args, name="solver"), result=frontier_math_result())
    )
    return Task(
        dataset=...,
        solver=solver,
        scorer=[expression_equivalence(), frontier_math_members()],
        epochs=Epochs(k, ["mean", pass_at(k)]) if arm == "epochs" else 1,
        cost_limit=budget / k if arm == "epochs" else budget,
    )
```

- `member_scores(scorer, result, *, value_to_float=value_to_float(), empty_value=0.0) -> Scorer` is a plain builder, not itself registered. `MEMBER_METRICS` is `{"best_member": [mean(), stderr()], "mean_member": [mean(), stderr()]}`. Both are exported from `inspect_swarm.scorer`.
- A factory that needs parameters takes values Inspect logs and rebuilds (strings, numbers, registered objects) and builds the spec from them.
- The task's own scorer stays first and unchanged, so the headline is the same scorer in every arm.
- The arms differ in a task argument (`arm`), so they are distinct in an eval set even though scorers and epochs are not hashed into `task_identifier()` and a contract's contents are not either: `result=` is hashed only as its logged value, `"ResultSpec"` for every contract ([swarm-api.md Task identity](swarm-api.md#task-identity-what-makes-two-arms-distinct)).

## The final answer

### The final-answer chain

The controller's `final=` takes one mode or a sequence of modes. A sequence is a chain: each mode is tried in order on the submissions, and the first that produces an answer wins. The runtime applies the chain, from member text the controller's handles and events never carry. A controller's own choice goes before it ([The controller's choice](#the-controllers-choice)).

```python
# src/inspect_swarm/_final.py; FinalName, Synthesize, synthesize and FinalSpec are exported
FinalName = Literal["verify", "vote", "first", "last", "synthesize"]   # "synthesize" is synthesize() with its defaults

class Synthesize(BaseModel):          # frozen, extra="forbid", arbitrary types allowed for Model
    mode: Literal["synthesize"] = "synthesize"
    model: str | Model | None = None  # a Model is logged as Inspect's model dict and read back from it
    prompt: str | None = None

class Reporter(BaseModel):            # reserved; frozen, extra="forbid"
    mode: Literal["reporter"] = "reporter"
    member: str

FinalMode = Annotated[Synthesize | Reporter, Field(discriminator="mode")]
FinalSpec = FinalName | FinalMode | Sequence[FinalName | FinalMode]

def synthesize(model: str | Model | None = None, prompt: str | None = None) -> Synthesize: ...
```

The candidates are every submission: in roster order, each non-persistent member that ended `submitted`, at its end time, and each answer of a persistent member, at its answer time, in answer order.

| Mode | Needs | Produces | Produces nothing when | Reason with no answer |
|---|---|---|---|---|
| `verify` | `result.verifier` | The passed submission with the highest `Verdict.value` (`None` and NaN rank below any number, `-inf` included), then the earliest swarm time, then roster order, then answer order; each answer of a persistent member is ranked by its own verdict | no submission passed | `none_verified` |
| `vote` | `result.answer_key` | The earliest submission in the plurality group of answer keys; one vote per member, and a `None` key does not vote. A persistent member votes with its latest answer, which is its candidate in the group. A tied plurality goes to the group whose earliest submission is earliest, and `tie` is recorded | no member votes | `no_answer_keys` |
| `first` | nothing | The submission with the earliest swarm time, then roster order, then answer order | there are no submissions | `no_submissions` |
| `last` | nothing | The submission with the latest swarm time, then roster order, then answer order | there are no submissions | `no_submissions` |
| `synthesize` | `kind="answer"` | One extra model call over the submissions, in the final step ([`synthesize`](#synthesize)) | there are no submissions, or the call fails recoverably | `synthesize_failed`; `not_synthesized` in a provisional answer |
| `reporter` | a coordinator topology | Reserved: no controller accepts it, and `swarm()` refuses it | | |

With no answer, the final record's `reason` is `no_submissions` when nothing was submitted, and otherwise the reason of the chain's last mode.

**The default.** With `final=None` (the default of `leaderless()` and `Controller`), the chain is built from the contract: `verify` if there is a verifier, then `vote` if there is an answer key, then `first` (decision: Ransom, 2026-10-07, for the order). A task with both gets `["verify", "vote", "first"]`; one with neither, or with no contract, gets `["first"]`. The chain falls through: when nothing verifies the swarm votes, and when nothing can vote it takes the first submission, so it returns an answer whenever anyone submitted (decision: Ransom, 2026-11-09). A single mode is strict: `leaderless(stop_on_submission=False, final="verify")` gives no answer when no submission passed.

**Validation.**

- `Controller(final=)` validates its argument with a `TypeAdapter(FinalSpec)` (`check_final()`), which also accepts the dict forms a log gives back, and refuses (`ValueError`) anything else and an empty chain.
- `leaderless(stop_on_submission=True, *, final=None, stop_on_verified=False)` refuses (`ValueError`) a `reporter`, an explicit chain other than `first` while `stop_on_submission` is on, and `stop_on_verified=True` with it. With `stop_on_submission=True` the swarm stops at the first submission (decision: Ransom, 2026-10-21), so `last`, `verify` or `vote` would choose from that one submission and any made during grace, and the first submission would stop the swarm whether or not it passed. `final=None` is accepted with it, since `leaderless()` does not know the contract; the default chain then chooses from the submissions made before the members stopped.
- `swarm()` builds the chain for its contract (`final_chain()`) and checks it when it is built (`check_chain()`), raising `ValueError` naming the missing piece for `verify` without a verifier, `vote` without an answer key, `synthesize` without a contract of kind `answer`, `synthesize` more than once (the record has one `synthesize_error`), and any `reporter`, which no runtime path runs.

**`stop_on_verified=True`** makes `leaderless()` return the stop reason `verified` at the first `MemberEnded`, or a persistent member's `MemberIdle`, whose verdict passed. Without a verifier no submission has a verdict, so it never fires.

**Logging and replay.** A controller factory's `final` argument is logged inside the controller's params, and validates back from them. `Synthesize.model` has its own serialiser and validator, because `registry_value()` reaches a `Model` only at top level or inside lists and dicts: the serialiser writes a `Model` as Inspect's model dict (name, config, base URL, model args) and the validator rebuilds it with `model_create_from_dict()`. So `final=["verify", synthesize(model=get_model("mockllm/model"), prompt="x"), "first"]` is logged as `["verify", {"mode": "synthesize", "model": {"model": "mockllm/model", "config": {}, "base_url": null, "model_args": {}}, "prompt": "x"}, "first"]` and rebuilt from it, and two arms that differ only in a `synthesize` prompt get different `eval_set` identifiers. The plain string `"synthesize"` is logged as itself.

**`reporter`** is reserved for coordinator topologies, where the answer is one named member's output. `Reporter` validates as a mode so a chain naming it logs and rebuilds, but `leaderless()` refuses it and `swarm()` refuses any chain that holds it, until a controller topology uses it. A controller that accepts it would check its member against the roster in its `check`.

### The controller's choice

`SwarmControl.choose_answer(member, reason=None)` makes that member's submission the answer, in place of the chain (decision: Ransom, 2026-10-22). Handles and control events carry no member text, so a controller chooses from what they show: names, roles, statuses, the order of events, and verdicts as `VerdictSummary`. A controller that registers boundary callbacks also sees copies of the member's conversation and last output at each turn boundary ([Controllers, Boundary callbacks](../docs/controllers.qmd#boundary-callbacks)); callbacks are the eval author's trusted code, and what they read is the controller's policy ([swarm-turn-seam.md Security](swarm-turn-seam.md#security)).

- The member must have ended with a submission, or, if persistent, have answered: its latest answer at the time of the call, kept if it answers again. Otherwise `RuntimeError`; `KeyError` for an unknown name. A controller chooses once; a second call raises `RuntimeError`.
- The choice is the provisional answer from the call on, and the final answer after the stop; until the call, the chain gives the provisional answer.
- A strict chain does not limit the choice: a controller that wants a verified answer reads the handle's `verdict` before it chooses.
- The record and the `final` event give the mode `controller`, the member, the persistent answer index and the controller's `reason`. The reason is the eval author's text, refused when a log would change it ([swarm-result-record.md](swarm-result-record.md#reading-it-swarm_result)).

### Verdicts

When the contract has a verifier, the runtime verifies every submission, each answer of a persistent member included, whether or not `verify` is in the chain, so the analysis always has verdicts.

- **Order.** A member that ends with a submission, and a persistent member's answer when it is given, is queued for verification; one runtime task takes the verdicts one at a time, in the order the submissions ended and the answers were given, in the swarm's span, under the runtime lever it shares with the controller's task. Its usage is the swarm's, not a member's ([swarm-limits.md](swarm-limits.md#swarm-nodes)). Each verifier call runs inside Inspect's `known_work()`, so its time counts as working time even while a held member marks the sample as waiting, unless the verifier marks its own waits.
- **Before the controller hears.** The member's state, status, time and output are recorded at its end, and its handle shows them with `verdict` `None`. Its `MemberEnded` is queued only after the verdict is taken, carrying it as `VerdictSummary(passed, value)`, never the explanation. Until then the member counts as live for `events()` and for the stop procedure's wait for quiet members, so neither misses it. A persistent member whose answer is being verified (*checking*) stays `running` and held after its answer; once the verdict is taken it is set `idle` and its `MemberIdle` is queued with that verdict in one step, so `wake()` cannot overtake the verdict and a persistent member has at most one answer on the queue ([swarm-persistent-members.md Keys and verdicts](swarm-persistent-members.md#keys-and-verdicts)).
- **Through the stop.** Verification continues through grace. A verifier call that the stop's closed runtime lever or a reached cap refuses leaves that submission unverified. The hard stop cancels verification: submissions not yet verified are left unverified and their `MemberEnded` events are queued with no verdict. A verdict returned after cancellation (the hard stop's or the sample's) is discarded even when the verifier caught the cancellation, and no further verifier is called. A persistent member that a stop or `end()` reaches while it is checking ends `submitted`, and its `MemberEnded` waits for that answer's verdict as a submission's does.
- **In the final step**, the runtime verifies each non-persistent submission still without a verdict, in the order they ended ([The provisional answer and finalisation](#the-provisional-answer-and-finalisation)). Only the record and the answer see those verdicts. It never verifies a persistent member's answer: one the queue settled without a verdict stays unverified, since the controller has already heard of it without one, and an `artifact` verifier would by then check the environment as later activations and the stop left it, not as it was when the answer was given.
- **Evidence.** Each verdict writes a `verdict` swarm event in the swarm's span with the member, its span id, `passed`, `value`, the verdict's swarm time and `answer`, the index of a persistent member's answer (`null` for a submission), never the explanation. A submission or answer left unverified has none.
- **What a controller sees.** `MemberHandle.verdict` is the verdict on a member's submission or, for a persistent member, on its latest answer, the one `choose_answer()` takes; `MemberIdle.verdict` is the verdict on the answer it names; `MemberEnded.verdict` is the handle's when the event is queued, however the member ended. All are `VerdictSummary`, and `None` without a verifier.
- **Artifact tasks** also get `final_verdict` in the final step ([Shared-artifact tasks](#shared-artifact-tasks-and-per-member-artifacts)).

**Keys.** The answer key is computed when a submitting member ends, after its `member_result` event and its output are settled, so an error from it leaves the answer and the record agreeing. A persistent member's answer is keyed when it is given, after it is recorded and settled; the provisional answer is settled again after each key. Nothing is keyed or verified again when a persistent member ends. A key that raises on an answer, or returns something other than a `str` or `None`, raises a private `AnswerKeyError` (a `RuntimeError` naming the member and the answer's index) from the key's error: the key runs inside the member's loop, where a sourceless custom `LimitExceededError` would otherwise read as the member stopping itself, so the member ends `errored` and the sample fails, with the answer recorded and no key.

### The provisional answer and finalisation

**The provisional answer.** After each change that can move it (a member's end, its key, a verdict, the controller's choice, a persistent member's answer), the runtime applies the chain without `synthesize`, which needs a model call, to the submissions so far, and sets the chosen output on the `AgentState` it was passed. `as_solver()` copies that object's output to the task state even when an exception ends the agent, so an outer limit that ends the sample early leaves the answer so far as the sample's output, and the record says `provisional: true`. With nothing submitted the output stays empty, and the record's `reason` says why. A provisional answer whose chain ends with `synthesize` has the reason `not_synthesized`.

**No submissions.** When no member submitted, the final answer is empty, even though members have outputs: an output from a member stopped mid-work is usually a working message, not an answer, and nothing is invented.

**Finalisation.** After the stop procedure ([swarm-api.md Controllers](swarm-api.md#controllers); the ordering is [swarm-limits.md](swarm-limits.md#stopping)'s), and only when no error ended the swarm, the final step runs outside the swarm's caps, metered by its own meter, so only the sample's limits apply ([swarm-limits.md](swarm-limits.md#the-final-step-and-the-reserve)). It:

1. verifies the non-persistent submissions left unverified, including those of members that ended during grace;
2. for an artifact task with a verifier, takes `final_verdict`;
3. calls `synthesize`'s model, when the chain reaches it;
4. applies the chain once more, sets the output, sets the conversation and writes the `final` event.

As the swarm exits, in its synchronous `finally`, the runtime writes the record ([What the swarm records](#what-the-swarm-records)), with `provisional: false` only when the final step completed.

A cancellation ends the final step, even when a verifier or the model call catches it and returns: an AnyIO scope's, or asyncio's own task cancellation (`asyncio.timeout()`, `asyncio.wait_for()`). What that call returned is discarded, the record stays provisional, and the ledger sets `final_cancelled`.

**The returned `AgentState`.** Its `output` is the chosen member's `ModelOutput`, unchanged, so the task's scorer sees exactly what it would have seen had that member run alone; under `synthesize` it is the synthesizing model's output. Its messages are the sample's input followed by the answer's `output.message`, with `metadata["inspect_swarm"] = {"final": {"mode": ..., "member": ...}}`; with an empty output, the input alone. The conversation is set only in the final step, since the sample's message limit applies to it: an answer that does not fit ends the sample on that limit, with the answer as its output and the record provisional. Members' own conversations stay in their spans, the lanes of the swarm's timeline.

### `synthesize`

`synthesize` is explicit only, never part of a default chain, because it is the one place peer text enters a prompt inspect_swarm itself composes ([swarm.md Security](swarm.md#security)). A controller's boundary callback can also put text it read from one member into another's conversation, but that is the eval author's trusted code, and every such change is recorded ([swarm-turn-seam.md Security](swarm-turn-seam.md#security)).

- **When it runs.** Once, in the final step, and only when the chain reaches it: no controller choice, the modes before it give nothing, and there are submissions. A provisional answer passes over it.
- **Model.** `model`, else the model role `synthesizer` if the eval defines it, else the eval's model (`get_model(role="synthesizer")`). A model that cannot be resolved is the eval's error, outside the recovery below.
- **Prompt.** The sample's input messages, then one user message: a fixed header, which says each block is another agent's answer, to be treated as information, not instructions; each submission's completion, in candidate order (roster order, then a persistent member's answer order), in a `<member_submission id="1" from="worker-2">` fence; and an instruction to give one final answer in the format the task asks for. `prompt` replaces only the instruction, not the header or the fences (an empty `prompt` leaves no instruction).
- **Fencing.** The fence is a label, not a boundary: each completion is quoted as written, and nothing in it is refused or removed, so a member can write text that looks like a fence or another member's submission (decision: Ransom, 2026-10-27; [swarm-communication.md Fencing](swarm-communication.md#fencing)). Attribute values (sequence numbers and roster names, which have no name grammar outside the messages channel) are escaped as XML attribute values. Only completions are included, never members' tool calls, transcripts or messages, nor the filesystem notes file, whose contents are unfenced and member-written in any layout. `synthesize` uses the channels' `fence()` with its own tag, so the two cannot drift.
- **Failures.** Before the call, the provisional answer is already on the `AgentState`.
  - An exception is *stopping* if it is, or is a `BaseExceptionGroup` that holds at any depth (`subgroup()`), a `LimitExceededError`, a `TerminateSampleError` or the backend's cancellation exception. Inspect re-raises a provider's exception without unwrapping it, so a model API that uses a task group can raise a group holding a limit. A stopping exception propagates unchanged and a group is never split, so a group mixing a limit with an ordinary error propagates whole. A bare limit reaches the runtime's exhaustion policy, which recovers only the swarm's own cap; any other limit reaches the runner, which records `EvalSample.limit`. Inspect's runner unwraps a group to its first leaf before it classifies it, so a mixed group is recorded as that leaf: a limit when the limit comes first, and an ordinary sample error, with no `EvalSample.limit`, otherwise. The provisional answer is the sample's output.
  - **Recoverable** failures move the chain to its next mode and set `synthesize_error`: any other `Exception` from the call (a provider error after Inspect's retries, a `ModelRefusalError`, a group of such errors), recorded by its type alone (`"the model call raised RateLimitError"`), since an error's message can hold model text and the final record's text is the eval author's; or an output that is empty, has an empty completion, or stopped with `content_filter`. A chain of `["synthesize"]` alone then has no answer, with `reason="synthesize_failed"`.
- **Scoring.** The synthesized answer is no member's output: the final record has `mode="synthesize"` and `member=None`, and the answer's message metadata names that mode and member. The task's scorer scores it; per-member scores are unaffected.

## What the swarm records

The runtime writes one record when the swarm ends: a `SwarmEvent` of kind `result_record` in the swarm's span, whose data is the record below and an `id`, with the same id in the store under `inspect_swarm:result_id` ([swarm-result-record.md](swarm-result-record.md) designs the home and the reader, `swarm_result()`):

```python
# src/inspect_swarm/_record.py, exported from inspect_swarm
class MemberResult(BaseModel):
    name: str                          # roster name
    role: str | None
    choose_name: bool = False          # the member chose its own name (swarm-chosen-names.md)
    state: MemberState                 # "pending", "running", "idle", "ending" or "ended"
    status: str | None                 # how it ended, once it has
    submitted: bool                    # status == "submitted"
    time: float | None                 # swarm time when it ended, after any background drain
    sandbox: str | None = None         # its own sandbox, by name; None when it shares the sample's
    gate_checks: int = 0               # its own submissions checked in its loop by the task's first scorer
    gate_passes: int = 0               # those that passed under the default value_to_float()
    first_pass_time: float | None = None
    persistent: bool = False
    answer_times: list[float] = []     # a persistent member's answers, in order
    answer_results: list[AnswerResult] = []  # a persistent member's, one per answer; [] for others
    output: ModelOutput | None = None  # the member output once it ended; None before
    answer_key: str | None = None      # result.answer_key(output.completion), for a submission
    verdict: Verdict | None = None     # the verifier's verdict on its submission
    verdict_time: float | None = None
    end_origin: str | None = None      # who asked it to end (swarm-operator-intervention.md)
    user: str | None = None            # users isolation: the member's user, uid, home, reaping and
    uid: int | None = None             #   commands run as another user (swarm-sandbox-isolation.md);
    home: str | None = None            #   None without isolation
    reap: MemberReap | None = None
    foreign_execs: int | None = None

class FinalRecord(BaseModel):
    mode: str | None                   # a chain mode, or "controller"; None with no answer
    member: str | None                 # None with no answer, and under synthesize
    reason: str | None                 # why there is no answer; under "controller", the controller's reason
    provisional: bool                  # True unless the final step completed
    answer: int | None = None          # which of a persistent member's answers
    chain: list[str] | None = None     # the chain's mode names
    tie: bool = False                  # vote chose from a tied plurality
    synthesize_error: str | None = None

class SwarmResultRecord(BaseModel):    # ser_json_inf_nan="constants", as EvalSample
    version: Literal[1] = 1
    members: list[MemberResult]        # roster order
    final: FinalRecord
    isolation_incomplete: bool | None = None  # users isolation: a member may have left processes running
    kind: ResultKind | None = None     # the contract's kind
    verifier: str | None = None        # the verifier's label, for audit
    final_verdict: Verdict | None = None  # artifact kind: the verifier on the drained environment

class AnswerResult(BaseModel):         # one answer of a persistent member
    answer_key: str | None = None      # result.answer_key(answer.completion)
    verdict: Verdict | None = None     # the verifier's verdict on the answer
    verdict_time: float | None = None
```

- **When it is written.** Once, in the runtime's synchronous `finally`, however the swarm exits, with each member's state, status, time, output, key and verdict at that point, and the answer: `provisional: false` after finalisation, `provisional: true` when the swarm ended before it ([swarm-result-record.md When it is written](swarm-result-record.md#when-it-is-written)). The runtime keeps these values on its run as they arise; no record exists while the swarm runs, so no member can read a peer's output, key or verdict from it, and members the controller never started are `pending`. The gate fields are filled in when a member ends.
- **Why the transcript.** Tools that read and write the sample store are common in Inspect tasks, and the record holds members' outputs and verdict explanations, so a record in the store would be readable by members' tools during the run (decision: Ransom, 2026-10-29). Scorers read it with `swarm_result(state)`, during the eval and when a log is re-scored, and analysis with `swarm_result(sample)`. A read while a swarm in the sample is still running raises, and so does a read of a log that kept the record in the store or of a sample whose store lost the id ([swarm-result-record.md Reading it](swarm-result-record.md#reading-it-swarm_result)).
- **Owned by the runtime.** Only the runtime writes the record; task code (tools, member builders, controllers, scorers, Sentinel protocols) must not write a `SwarmEvent`, an event with source `inspect_swarm`, or the `inspect_swarm:result_id` key. A tool that reads the transcript sees every member's events anyway, a channel between members outside the bus; the [user docs](../docs/scoring.qmd#the-record-belongs-to-the-swarm) say so.
- **The output** is a copy of each member's output when it ended, with or without a contract, with its `completion` as the member returned it: validating a `ModelOutput` fills an empty `completion` from its message, so the record keeps an empty one both when it is made and when it is read. `member_scores()` reads the outputs from here.
- **No vote counts.** The final record keeps no count of votes by answer key: keys are model text, and a log's processing rewrites values but not dict keys, so with `log_images=False` an image data URI would survive in a key while it is removed from the member. Grouping the submitted non-persistent members by their `answer_key`, and each persistent member, however it ended, by the key of its last entry in `answer_results`, counts the keys the record keeps; a log can turn different keys into the same text (two image URIs both become the removal placeholder), so these counts may differ from the original vote. `mode`, `member` and `tie` describe the original choice (decision: Ransom, 2026-11-10).

### Model text in the record

Outputs, answer keys and verdict explanations (the members' and the final verdict's) come from model output, and a log can give some of it back changed. The record holds each as Inspect's own sample-writing code gives it back, and accepts those changes rather than refusing the value, so `member_scores()` grades what a log holds (decision: Ransom, 2026-11-03). The record's other text is the eval author's and is refused when a log would change it ([swarm-result-record.md](swarm-result-record.md#reading-it-swarm_result)).

- **The round trip.** The writer gets the logged form from Inspect's own sample-writing code rather than rebuilding it: it puts the record in a sample's event, writes the sample as the log writer does (`condense_sample()`, then `to_json_safe()` with `EvalSample`'s serialization settings) and reads it back as a log is read, with the deserializing context and the event's attachments resolved. So a NaN or infinite number is kept, a tuple becomes a list, a date becomes text, and text that is not valid UTF-8 is escaped. Each member's output is first round-tripped alone, in a copy of the record that holds only that member and that output, so that an output a log cannot write or read back is named by its member. The event the swarm writes holds the whole record as the round trip gives it back.
- **Literal text that reads as a reference.** A log reads a string that begins with `attachment://` as a reference to an attachment, and rewrites `tc://` as `attachment://`, so a literal string with either prefix would read back as other text or as an unresolved reference. The writer keeps it by marking it: in the model text alone, it puts the fixed marker `inspect_swarm:literal:` before every string that begins with `attachment://`, `tc://` or the marker itself, so the marking is reversible. The marker does not itself begin with a reference prefix, so a log leaves a marked string alone, apart from condensing a long one into an attachment like any other. The model text is each member's output (every string in it, not dict keys, which a log never reads as references), answer key and verdict explanation, each persistent answer's key and explanation, and the final verdict's explanation. `swarm_result()` removes the marker on both paths, after the event's attachments are resolved and after its check for unresolved references ([swarm-result-record.md](swarm-result-record.md#reading-it-swarm_result)), so a bare prefix in the record always means an attachment that was not loaded. ORBIT's experiments put such strings in member outputs on purpose ([swarm-orbit.md The answer](swarm-orbit.md#the-answer-and-a-record-of-the-answer-scored)) (decision: Ransom, 2026-10-30). The record's version stays 1. The cost: an older inspect_swarm reading a newer record returns marked text with the marker on. Only a value that the older writer left out, or text that begins with the marker itself, carries the marker; a newer inspect_swarm reading an older record removes the marker from such text, which an older writer kept unmarked, so a record written by an earlier inspect_swarm whose model text literally began with `inspect_swarm:literal:` reads back without it. This cost is accepted rather than marking version-1 records with an encoding flag: the marker string is new with this change, so no real earlier record is expected to hold model text that begins with it (decision: Ransom, 2026-11-12).
- **Images, when the log is written.** The round trip runs as an eval that logs images. When the eval does not (`log_images=False`), Inspect's log writer (`condense_sample()`) removes base64 `data:` URIs from the record's event later, when it writes the sample, as it does from the sample's messages and the other events; it leaves the sample's own `output` as it is. So in that case only, an output, answer key or verdict explanation holding a data URI differs between the live record, which final scoring reads, and the logged record, which analysis and re-scoring read: the URI is replaced by Inspect's removal placeholder. Otherwise the live record and the logged one are the same. A single agent's re-score grades the sample's `output`, which keeps the URI, so for such an answer re-scoring `member_scores()` on a swarm's log grades the placeholder where re-scoring a single agent's log grades the URI, and the swarm sample's own logged `output` keeps the URI while the record's copy of the chosen member's output does not. The record's author text, which a log must give back unchanged under both settings, is checked with both round trips ([swarm-result-record.md](swarm-result-record.md#reading-it-swarm_result)).
- **Each answer's text.** A persistent member's answers' keys and verdict explanations are marked in the same way, and the writer's check of the author's text leaves `answer_results` out, as it leaves out `verdict`.
- **An output the record cannot hold fails the sample.** The writer refuses an output when the record could not read it back or writing it could hang: an output nested more than `MAX_TOOL_CALL_ARGUMENTS_DEPTH + 20` (120) levels, or with a cycle; an output whose containers reached by more than one path would add more than 10,000 containers when serialized (serializing writes a shared container once per path); or an output the round trip cannot write or read back, such as one holding a dict key with more digits than Python prints, which YAML gives for a long hex integer. Both bounds are measured in one walk before anything is serialized, each container once by identity; Inspect's `exceeds_max_depth()` bounds depth but not expansion. The swarm's end then raises `ValueError` naming the member and where the problem is: the path in the output to the cycle or to the nesting past the limit, or the log's error for a round trip that fails. For a persistent member that ended with an answer, whose output is that answer, it also names which answer. No record is written, so every read of the sample gives the lost-id `ValueError` ([swarm-result-record.md](swarm-result-record.md#reading-it-swarm_result)); the ledger and metrics are written. Inspect caps tool-call arguments at `MAX_TOOL_CALL_ARGUMENTS_DEPTH` (100) levels, but YAML aliases can produce shared containers that exceed the expansion limit. Such model-generated output fails the sample too. The sample fails loudly rather than the record dropping the output (decision: Ransom, 2026-11-12). This reverses the decision that one member's model output never costs the sample its record or its scores, with such a value left out of the record and named in a `left_out` field (decision: Ransom, 2026-10-30, narrowed to model-made values on 2026-11-03). An answer key or verdict explanation that a log could not write or read back would also fail the sample; none is expected, since a log gives back every string, marked where it would read as a reference.
- **Objects only task code makes.** Before that, the runtime checks each output for values a model cannot make, and refuses them as the record's other author text is refused. Inspect's own models are trusted only where Inspect puts them (the output, its choices, messages, content, citations and tool calls), and a model counts as Inspect's only when its exact class is one a `ModelOutput` is built from, found from the type hints, so a task's subclass is refused. Their free-form fields (typed `Any` or `JsonValue`: metadata, tool arguments, content `data`, providers' `internal`, and Pydantic extra fields) must hold what JSON and YAML parsers give: dicts, lists, tuples, strings, numbers, booleans and `None`, and the dates, times, bytes and sets YAML tool arguments give, allowed in metadata too (decision: Ransom, 2026-11-03). Every value is approved by its exact class, compared by identity, without `isinstance()` or reading its attributes, so a subclass of a container, a string or a number is refused without the check calling its methods; a `datetime` is approved only with no time zone or an exact `timezone` whose offset and name are an exact `timedelta` and `str`. The walk visits each container once per position, without recursion, so a refusal never depends on what else the output holds or in what order; it serializes nothing and advances no iterator. A dict key too large to print is named by its type in the error.
- **What a refusal does.** `_answer()` checks a persistent member's answer before keeping it, and `_end()` checks every ending output before the member's end is published, so a refused output ends its member `errored`, whatever its classification gave, with a `TypeError` naming the member and the path. The answer and the record never read that output again: the record's `MemberResult` has `output=None`, and the record, ledger and metrics are written as for any error that fails the sample. Task code is trusted: a refusal promises the error and the partial record, not that none of the object's own methods (`__deepcopy__`, `model_copy`, `utcoffset`, ...) run before it, since Inspect itself runs such methods when it logs the member's model events (decision: Ransom, 2026-11-04).

### Persistent members

A persistent member gives a submission at each answer. The record keeps their times in `answer_times`, each answer's key, verdict and verdict time in `answer_results`, in the same order, and `final.answer` says which answer is the swarm's ([swarm-persistent-members.md Record, metrics and evidence](swarm-persistent-members.md#record-metrics-and-evidence)). A contract's kinds are treated alike: an `artifact` task's verifier checks the environment at each answer, as it does at each submission.

- **The member's own fields.** `answer_key`, `verdict` and `verdict_time` describe a submission at `time` and stay `None` for a persistent member, whose answers have theirs in `answer_results`. When it ends `submitted`, its last answer is its output. A record written before `answer_results` existed reads with `[]`.
- **The chain.** `first`, `last` and `synthesize` use every answer. The controller's choice takes the member's latest answer at the time of the call and keeps it if the member answers again; it cannot choose an earlier answer once a newer one exists ([The controller's choice](#the-controllers-choice)). `verify` ranks each answer by its own verdict, beside the submissions'. `vote` gives a persistent member one vote, by its latest answer, the one `choose_answer()` takes, which is then its candidate in the group; when that answer has no key the member abstains, whatever its earlier answers' keys (decision: Ransom, 2026-11-10).
- **Stopping on a verdict.** `leaderless(stop_on_submission=False, stop_on_verified=True)` returns `verified` at a passing verdict carried by `MemberEnded` or, for an answer, by `MemberIdle`.
- **Baselines.** `baseline()` refuses a persistent member and `select_at_k()` refuses a persistent member's rows ([Baselines](#baselines-a-swarm-of-one)).

### Evidence events

Each member's end and the final selection also write swarm events: `member_result` (in the member's span: its status, end time, sandbox and end origin), `verdict` ([Verdicts](#verdicts)) and `final` (in the swarm's span: the chosen member, mode, reason, stop reason and persistent answer index). [swarm-event-types.md](swarm-event-types.md#how-inspect_swarm-writes-them) defines their fields and versioning. The events put the result in the transcript when it happened and carry no member text; the record, written at the end, is what scorers read.

## Member scores

### Answer-scored tasks: per-member scores

The scorer `member_scores(scorer, result, ...)` builds works in six steps.

1. **Skips in-loop scoring.** While `state.completed` is `False` it returns `None` at once. `score()` drops `None`, so in-loop callers (`react(attempts=...)`, `basic_agent`, the human agent's `/score`, inspect_swe's attempt loops, live control scoring) see the task scorer's feedback and call count exactly as without it. During the solve it never runs the inner scorer or touches the sandbox, and does not read the record, which does not exist yet.
2. **Reads the members** from the record with `swarm_result(state)`, which raises for a record it cannot read, a log that kept the record in the store, and a sample whose store lost the record's id, so neither of the last two is scored as a plain agent ([Re-scoring](#re-scoring)). When the reader returns `None` (no swarm ran: a plain agent, not `baseline()`), it treats the sample's `output` as one ended member named `solver`, with status unknown and `submitted` false, and computes its answer key, a pure function. It never runs the verifier.
3. **Checks the contract.** It refuses to grade, in this order: `kind_not_declared` (the record has no kind, as without a contract), `kind_mismatch` (the record's kind differs from `result.kind`), `artifact_without_member_artifacts`. A plain agent has no record, so only the last applies to it. Each started member is then unavailable with that reason, the scorer is not called, and both values are NaN with the score's `reason` set to it.
4. **Grades each member, in roster order.** A member that never started (`pending`) is **not started**: no value, reason `not_started`, not graded. It ran nothing, so it is neither a failure nor unavailable. Every other member is one of:
   - **Unavailable**: the scorer returned `None` (`scorer_returned_none`), a value that is not one `str`, number or `bool` (`non_scalar_value`; there is no general best of a dict), or NaN, checked before `value_to_float`, which may map it to a number, and after it (the grade's own `reason`, else `unscored`). Its value is NaN.
   - **Empty**: no output, or a completion that is empty after stripping whitespace: value `empty_value`, reason `empty_output`, not graded. This keeps a scorer's environment fallback from crediting the team's work to a member that produced nothing. It is a backstop, not the contract: a non-empty completion can still extract to nothing, which is why `kind="answer"` requires a scorer that never reads the answer from the environment.
   - **Graded**: otherwise, on a deep copy of the `TaskState` whose `output` is a copy of the member's `ModelOutput` and whose `messages` are the sample's input plus the member's `output.message`, or the input alone when the output has only a completion and no choices, as the swarm gives such an answer. The scorer is awaited with the sample's target, its value converted with `value_to_float`, and the grade's own `reason` (such as `invalid_response_format`) kept.
5. **Aggregates over the members with a value** (empty and graded), ignoring unavailable and not-started members explicitly rather than through `max`/`mean` on NaN, so the result does not depend on roster order: `best_member` is their maximum and `mean_member` their mean. With none valued, both are NaN with reason `no_member_valued` (or the contract's reason).
6. **Returns** `Score(value={"best_member": ..., "mean_member": ...}, reason=..., metadata=...)`. The metadata holds the record's `kind` and `final`; `members_total`, `members_started`, `members_valued` and `complete` (no started member unavailable), so analysis can restrict to complete samples and see how many members a conditional controller ran; and per member, in roster order: name, outcome (`not_started`, `empty`, `graded` or `unavailable`), value, the grade's answer and explanation, reason, state, status, `submitted`, answer key, verdict and time. A persistent member's answer key and verdict are those of its last entry in `answer_results` when it ended `submitted`, since its graded output is that answer, and `None` when it ended any other way, whose output is not a recorded answer, or when `answer_results` is empty, as in a record written before it existed, which is still graded with no key or verifier run.

Each inner grade runs inside a span named after the member, within the member scorer's span, so a model-graded scorer's calls are attributable. The copies' scores are not added to `state.scores`.

**Which scorer.** The scorer given to `member_scores()` must meet the answer-scored contract; it is usually the task's own. When the task's scorer can read the answer from the environment, the task passes an answer-only adapter (for Frontier-CS, a variant that scores empty extracted code as 0 rather than recovering it; that adapter is task code). The headline stays the unchanged task scorer. Oracle comparisons use `member_scores()` values in every arm, so both sides use the same adapter ([Comparison quantities](#comparison-quantities)).

**Ordering.** `member_scores()` follows the task's own scorer in the list, so the task's scorer scores the final answer and the environment before any per-member scoring touches the sandbox. Sandbox evaluators then run once per member in the same sandbox, one after another; writing their own files before reading them is the task's responsibility.

**Time and cost.** Per-member scoring runs within Inspect's scoring time limit, half the sample's; sandbox evaluators across many members can exceed it, and the sample then errors as for any slow scorer. A model-graded scorer is called once more per member, and that usage is in the sample's totals, so arms compared on cost use a `member_scores()` scorer that makes no model calls, or report the difference, until [the cost boundary](#the-cost-boundary-deferred) exists.

### Shared-artifact tasks and per-member artifacts

For `kind="artifact"`:

- **The team score** is the task's scorer on the drained environment, the only result the task defines, computed first.
- **The final-answer chain** still runs and sets `output`, because some scorers read the output text and fall back to the environment. `synthesize` is refused.
- **The verifier**, when given, checks the environment, not an answer. It is called with the submitting member's completion and the sample's metadata whenever a member submits, and a controller can stop on it (`leaderless(stop_on_submission=False, stop_on_verified=True)`). Members still working when it fires keep editing until the stop procedure ends them, so the scored environment can differ from the verified one.
- **`final_verdict`** is therefore taken in the final step, after the stop procedure, on the environment that will be scored: the verifier is called once more with the final answer's completion, or `""` with no answer. An error from it fails the sample. Analysis compares it with the run-time verdicts and the team score, and select@k's artifact `verify` reads it.
- **Per-member scores** are unavailable unless the task supplies `member_artifacts`.

With `member_artifacts`, `member_scores()` grades each member that started on its artifact instead of its text:

```python
for m in members:                                   # roster order, started members
    async with result.member_artifacts.materialize(m.name):
        score = await scorer(copy_of_state_with_member_output(m), target)
```

- Every started member is graded, whatever its completion: there is no empty outcome, because a member can leave a passing artifact and submit no text. A member with no output is graded with an empty one, since its artifact is what is graded. Unavailable grades and aggregation follow answer tasks.
- **`materialize` is task code**, because only the task knows its layout, and runs inside the member's span with its grade. It is given only roster names, never names a member chose, so paths and branch names come from the eval author's layout. It runs after the team score and restores the team's state on exit, so the team score never depends on per-member scoring. An error from it fails the sample.
- **A plain agent** has no roster name and its artifact is the environment it left, so it is graded on the environment as it is, without `materialize`.
- **Attribution in a shared sandbox is by convention** wherever members can write each other's work: in any shared location without users isolation, and in a writable share (`Share(path, "rw")`, a shared repository's branches included) with it. There a member's artifact score says "what was in its place when the scorer ran", not "what that member did alone", and tool-call evidence can show cross-writes only as far as the observation limit in [swarm.md Security](swarm.md#security) allows. Under users isolation (`swarm(isolation=users())`), a member's private directory can be written only by that member and root, so a `materialize` that reads a member's artifact from there reads entries only that member could create ([swarm-sandbox-isolation.md Scoring](swarm-sandbox-isolation.md#scoring), [Peers' access to private directories](swarm-sandbox-isolation.md#peers-access-to-private-directories)).

The docs' worked example ([Per-member artifacts](../docs/scoring.qmd#per-member-artifacts)) is a SWE-bench-shaped layout with a branch per member: it copies the team's checkout aside (`cp -a`), puts the member's branch in the scored checkout with `git add -A` and `git read-tree -u --reset`, leaving `HEAD` on the team's branch, and on exit moves the copy back. The copy keeps the team's ignored files and index, which the member's branch could otherwise overwrite or its `.gitignore` expose to cleanup. The move back is never retried, since a second run would delete the restored checkout. If scoring times out, Inspect turns it into an ordinary sample error and the checkout may be left unrestored; scores from earlier scorers, such as the team's, were taken before any member's branch was in place, and a retry starts in a fresh sandbox.

## Baselines: a swarm of one

`baseline(member, result=None)` is `swarm(members=member, controller=leaderless(final="first"), channels=[], result=result)`, where `member` is a built agent or a `member()` record. It refuses (`ValueError`) a `member` that stands for more than one member, and a persistent member, read from the `member()` record before any builder runs: a baseline's attempt is its member's one submission, read from its own end, key and verdict, and a persistent member gives one at each answer. It is swarm.md's natural baseline: every arm then writes the same record, with the same clock, eligibility rule and run-time verdicts, which select@k needs.

A swarm of one returns an empty output when its member never submits, where a plain agent is scored on its last output: the same conservative rule the swarm arm follows, so the arms stay comparable with each other, if not with a plain agent. A plain agent without `baseline()` still gets `member_scores()` values (its output scored as one member), but no run-time evidence, so the analysis leaves it out of select@k.

## Comparison quantities

Each quantity is defined once and paired with the one it is fairly compared with. k is the number of members, or of independent attempts.

| Quantity | Arm | Definition | Uses the target to select? | Computed by |
|---|---|---|---|---|
| **final@k** | swarm of k | Answer tasks: the task's scorer on the swarm's final answer, `empty_value` when there is none. Artifact tasks: the task's scorer on the drained environment, whatever the final text | No | the task's scorer; the user's analysis applies the empty rule to answer tasks |
| **team@k** | swarm of k | Answer tasks: `best_member`. Artifact tasks: the shared environment's score, equal to final@k | Answer: yes. Artifact: no | `member_scores()`; the task's scorer |
| **mean member** | swarm of k | `mean_member` | No | `member_scores()` |
| **pass@k** | n ≥ k epochs of `baseline()` | P(at least one of k attempts correct), binary values, without replacement | Yes | Inspect's `pass_at(k)` on the oracle value source |
| **best@k** | n ≥ k epochs of `baseline()` | E[max over k attempts], graded values, without replacement | Yes | the user's analysis, from the oracle value source's per-epoch values |
| **select@k** | n ≥ k epochs of `baseline()` | The swarm's chain applied to k independent attempts | No | `select_at_k()` |
| **single** | one `baseline()` attempt at the full budget | As final@k | No | as final@k |

**The oracle value source** is the scorer both sides of team@k against pass@k or best@k are read from, fixed by the result kind (each `AttemptRow` carries `result_kind`):

- **Answer tasks:** the member scorer's `best_member`. In a `baseline()` arm each epoch has one member, so its `best_member` is that attempt's value under the same scorer, possibly an answer-only adapter, that `member_scores()` uses in the swarm arm.
- **Artifact tasks:** the task's own scorer. Without `member_artifacts`, `best_member` is NaN in every arm and `pass_at(k)` counts only scored epochs, so an artifact pass@k read from it would always be NaN. team@k is the swarm's environment score, so the baseline side reads the same scorer: pass@k is the reducer's value on it, and best@k is computed from its per-epoch values. With `member_artifacts`, `best_member` adds per-member grades, but the oracle comparison still uses the environment score on both sides.

team@k follows Test-Time Communication's usage: "a game counts as solved if any of the k agents clears all levels", while on Terminal-Bench "team@2 contributes one shared final state". The fair pairs are:

- **team@k against pass@k or best@k.** Both pick the best of k outputs with knowledge of the target, from the same oracle value source. They measure whether communication raises the capability present in k agents.
- **final@k against select@k, and against single.** Each applies a rule that does not see the target, scored by the task's scorer, with the same empty rule. They measure what a deployed swarm returns against what k independent attempts with the same selection rule, or one bigger attempt, return. When both are read with the same grading function and empty-output rule, and the final answer is one of the outputs `member_scores()` grades, the gap team@k − final@k is what the final step loses. Otherwise the gap also reflects the difference: an answer-only adapter in `member_scores()` while the headline scorer recovers from the environment; a persistent member, graded on its ending output while the chain can choose one of its earlier answers, so final@k can exceed team@k; or `synthesize`, whose answer is a new output no member gave.
- **Artifact tasks.** team@k is already non-oracle (one environment); comparing it with pass@k, as Test-Time Communication does, favours the epochs arm. The non-oracle comparison is select@k with the verifier on each attempt's environment.

All of them are compared at realized cost, with each arm's cap and realized spend reported ([swarm.md Eval questions](swarm.md#eval-questions-and-the-experimental-design-they-imply)). `pass_at(k)` and `max` select with knowledge of the target and the swarm's rule does not, so against final@k they are upper references, not like-for-like comparisons.

Two things change what these quantities mean:

- **Conditional controllers.** A controller may leave members `pending` (swarm-api.md's `escalate` starts its workers only when the first answer does not verify). k stays the roster size, the arm's configuration; team@k and mean member are over the members that started, with `members_started` beside them; the cost comparison reflects how many ran. With `leaderless` every member starts. A team@k arm passes `leaderless(stop_on_submission=False)` so every member runs to its end and its result measures what it would submit.
- **Communication.** With a bus channel, a member's submission may repeat a peer's. Per-member scores measure what each member submitted, team@k's meaning ("any of the k agents"), not what it derived alone, and `vote` counts a copied answer as that member's vote. The communication evidence ([swarm-communication.md Evidence and provenance](swarm-communication.md#evidence-and-provenance)) lets analysis tell the cases apart; the record and the scorers need nothing for it.

### best@k on graded scores

inspect_swarm ships no best-of-k reducer: it is generic statistics, not swarm data (decision: Ransom, 2026-10-20). best@k is computed in the user's analysis from per-epoch values in `samples_df()`, with the unbiased without-replacement estimator of the expected maximum of k of n scored epochs: with the n values sorted ascending as v₁ ≤ … ≤ vₙ, best@k = Σᵢ₌ₖⁿ vᵢ · C(i−1, k−1) / C(n, k). For binary values it equals `pass_at(k)`.

### select@k and the analysis helpers

select@k needs each attempt's eligibility, answer key, verdict and time, which a reducer cannot see, so it is computed in `inspect_swarm.analysis` from the `baseline()` records. The helpers read or replay only what the swarm records; means over samples, estimators and comparisons across arms are the user's own dataframe work (decision: Ransom, 2026-10-20).

```python
# src/inspect_swarm/analysis/_scores.py, exported from inspect_swarm.analysis
def attempt_rows(logs: Sequence[str | EvalLog], *, scorer: str, member_scorer: str) -> list[AttemptRow]: ...
def select_at_k(rows: Sequence[AttemptRow], k: int, chain: str | Sequence[str], *, empty_value: float = 0.0, max_subsets: int = 10_000, seed: int = 0) -> float: ...
```

**`attempt_rows()`** reads one frozen `AttemptRow` per (log, sample, epoch), by log and then in each log's sample order, from the sample's record (`swarm_result(sample)`) and scores. A log path is read whole, with its samples' events. Each row holds `log` (the log's local path) and `id` (the sample id as text), the keys `samples_df()` gives, and `epoch`; `task_args`; `result_kind`; `final_value` (the task scorer's value under the default `value_to_float()`); `final` and `final_verdict` from the record; `best_member`, `mean_member` and `complete` from the member scorer; `members`, `members_total` and `members_started`; `sample_total_usage` (the sample's `model_usage`, scorers included); and the flags `target_assisted` (the sample holds an intermediate `ScoreEvent`: an agent consulted the task's scorers with the target during the solve, feedback that in a swarm can reach peers) and `no_solve_usage` (always set until the cost boundary exists). A missing or NaN score is `None`; a sample with no record has `final=None` and no members. It raises `ValueError` for a log read without its samples, a scorer name that no scored sample in a log has, a task scorer whose value is not a single value, a member scorer whose value is not `member_scores()`'s, and a record `swarm_result()` refuses, so a misspelled name never gives a column of `None`. `samples_df()` with `SwarmColumns`, which has the ledger's realized cost, joins on the same keys.

**`select_at_k()`** takes one sample's rows from a `baseline()` arm, one per epoch, and returns that sample's select@k. It refuses rows that cannot be `baseline()` attempts (below), but cannot confirm that a one-member, non-persistent record came from `baseline()`: a one-member `swarm()` with a strict chain or `synthesize` gives the same rows, while its `final_value` grades an answer that may be empty or no member's, not its member's submission. The caller passes only rows of a `baseline()` arm, identified by its task arguments.

- **The pool** is the rows with a record and a task value, in epoch order. With fewer than k, the result is NaN (unscored), as for `pass_at`.
- **One subset** is chosen by the runtime's own selector (`_final.answer()`), each attempt a `MemberResult` named after its epoch, so modes, tie-breaks and fall-through are the swarm's by construction, with epoch order in place of roster order. The chosen attempt's `final_value` is the subset's value; since a `baseline()` attempt's answer is its own submission, that is the task's scorer on the chosen output. The target is read only for the chosen attempt.
- **Answer tasks** (and records without a contract, read as the swarm reads them): only submitted attempts are chosen, and a subset where the chain chooses nothing is worth `empty_value`, the rule final@k uses.
- **Artifact tasks**: every attempt left an environment, so every attempt in the pool can be chosen, submitted or not, valued by its environment score. `verify` reads each attempt's `final_verdict`, never its run-time verdict, because the environment can change between a submission check and the drain, and nothing derives a verdict from `final_value`, which would select with the target. `vote` reads the answer keys of submitted attempts; `first` and `last` read end times, and an attempt cut off before it ended orders after every attempt that ended. `first` is appended to the chain when it is not already last, so every subset chooses an environment; a strict chain therefore has no artifact analogue, and `empty_value` is never used.
- **The sample's value** is the mean over all C(n, k) subsets when that is at most `max_subsets`, otherwise over `max_subsets` subsets sampled with `seed`.
- **Time** is the record's swarm time, never `EvalSample.working_time`, which includes scoring and excludes waiting. Every `baseline()` attempt starts its clock at zero, so `first` means the shortest time to submit.
- **Refusals** (`ValueError`): `k` or `max_subsets` below 1; an empty chain, or a mode other than `verify`, `vote`, `first` and `last` (`synthesize` makes a new model call, so it has no epoch analogue: a swarm using it is compared on final@k against single, and against select@k without it); rows of more than one sample, or a repeated epoch; a row whose record has more than one member or a persistent one, so it is not a `baseline()` attempt; rows that disagree on `result_kind`, naming the logs.

## Re-scoring

- **Answer tasks with sandbox-free scorers.** `member_scores()` reads only the record (from the transcript re-scoring seeds), the sample's input and the target, so `inspect score` reproduces it from the log, except where an eval that does not log images removed a data URI from a member's output or key ([Model text in the record](#model-text-in-the-record)), and can apply a new scorer to every member's output after the fact, provided the task's registered factory is loadable. That substitutes for a scorer arm only when no member consulted the task's scorer in the loop (`target_assisted`); otherwise the scorer is part of the trajectory and a scorer change is an arm of its own ([swarm-api.md Task identity](swarm-api.md#task-identity-what-makes-two-arms-distinct)).
- **Scorers that need the sandbox** (an answer evaluator that compiles code, any artifact scorer) cannot be re-scored: `sandbox()` raises `ProcessLookupError` and the error propagates, as for the task's own scorer. `member_scores()` neither catches it nor reports a partial result.
- **Selection evidence is never recomputed.** Verdicts, keys and times come from the record written during the run; re-scoring never calls a verifier.
- **Records this inspect_swarm cannot read.** `swarm_result(state)` raises `ValueError` for a version other than 1, a malformed record, a missing event or missing attachments, and `member_scores()` lets it propagate, so scoring a record written by a newer inspect_swarm fails that sample's scoring rather than misreading it. A new record version comes with a reader for it.
- **Logs that kept the record in the store** (under `inspect_swarm:result`, with no `inspect_swarm:result_id`) and **recovered samples** (events kept, store lost, so no record id) make `swarm_result(state)` raise, and `member_scores()` lets it propagate, rather than reporting a multi-member run as a one-member plain agent (decision: Ransom, 2026-10-30; [swarm-result-record.md](swarm-result-record.md#logs-from-before-this-change)).

## How it appears in logs and eval sets

| Where | What |
|---|---|
| Sample `output` and last assistant message | The final answer, the chosen member's own `ModelOutput` or the synthesized output; message metadata names the mode and member |
| Transcript, the `result_record` event in the swarm's span | The record: every member's state, status, time, output, key and verdict, each persistent answer's key and verdict, the final selection and chain, `kind`, the verifier's label and `final_verdict` |
| Transcript, other swarm events | `member_result`, `verdict` and `final` at the time they happened; `member_scores()`' per-member spans under its scorer span |
| Sample scores | The task's scorer (final@k, or team@k for artifact tasks); the task's member scorer with `best_member` and `mean_member`, per-member detail in its metadata |
| Eval results | One `EvalScore` per task scorer and reducer; the member scorer contributes `best_member` and `mean_member` as separate `EvalScore`s with their own scored and unscored counts |
| Eval spec (plan) | `swarm()`'s `result` as the type name `"ResultSpec"`; the chain faithfully inside the controller's params, such as `{"type": "swarm_controller", "name": "inspect_swarm/leaderless", "params": {"final": ["verify", {"mode": "synthesize", ...}], "stop_on_verified": false}}` |

Nothing here needs an event type, schema change or viewer change beyond [swarm-event-types.md](swarm-event-types.md)'s; the viewer shows the swarm events and dict scores generically. Inspect applies every epoch reducer to every scorer and key, so an epochs arm with `["mean", pass_at(k)]` also shows, for example, `pass_at` of `mean_member`: well defined but not meaningful, and the user's analysis selects the cells that are.

In an eval set, each arm is the same task with different arguments (`arm`, `k`, `budget`), which `eval_set` and inspect_flow sweep and each log carries in `eval.task_args`. The member scorer runs in every arm, so all arms have the same scorer names and `evals_df()` and `samples_df()` line them up. Which arm a log is comes from those declared arguments and the solver's logged configuration, not from its member count: a swarm arm can have k = 1, and a one-member `swarm()` whose chain is strict or ends with `synthesize` is not a baseline, since its answer can be empty or no member's. In a baseline arm n is the epochs; in a swarm arm k is the member count. `task_identifier()` hashes the task's arguments and the solver's parameters, including the controller's `final` and `stop_on_verified` and `swarm()`'s logged `result`, but not scorers or epochs. `result` is logged as `"ResultSpec"` for any contract, so passing one, or passing `None`, can change the identifier, while two contracts that differ only in their contents (kind, answer key, verifier, member artifacts) hash the same. Arms that differ only in scorers, epochs or a contract's contents take the difference from a task argument; otherwise `eval_set` refuses them as not distinct.

## The cost boundary (deferred)

Designed but not built (decision: Ransom, 2026-10-08: "Defer the cost boundary - already an issue with existing inspect evals"; held again on 2026-11-10). Until it is built, an arm's total is its sample usage with scoring included, every `AttemptRow` has `no_solve_usage` set, and arms compared on cost use a `member_scores()` scorer that makes no model calls or report the difference. It needs no inspect_ai change.

**Why it would matter.** With k gradable members and the same model grader for the headline and the member scores, a swarm arm makes k + 1 grading calls per sample and a baseline arm 2, and that usage lands in the sample's. Model events cannot separate solving from scoring, because native compaction records usage without one.

**The design.** The record would gain:

```python
class SwarmResultRecord(BaseModel):   # with the cost boundary
    solve_usage: dict[str, ModelUsage] | None = None       # sample usage when the swarm exited, however it exited
    solve_role_usage: dict[str, ModelUsage] | None = None  # sample role usage at the same moment
    solve_exit: Literal["returned", "limit", "terminated", "error", "cancelled"] | None = None
```

- **The snapshot** is the sample's cumulative usage (`sample_model_usage()`, `sample_role_usage()`), taken in the swarm's outermost `finally` however it exits: after finalisation (`synthesize` included) when it returns, and after its task group has cancelled and awaited the members when a limit, `TerminateSampleError`, an error or a cancellation propagates. The reads and the record's write are synchronous, so they run under cancellation too, and `solve_exit` records the path. Inspect scores only after the solver has returned or raised, so the snapshot always falls before grading.
- **Coverage.** Sample usage counts native compaction and excludes response-cache replays, so it is complete where model events are not; the ledger's unknown-cost rules still apply.
- **Placement.** The snapshot covers everything up to the swarm's exit, so the swarm must be the task's last solver step, which `swarm()` would document.
- **Scoring usage** is the sample total minus `solve_usage`, reported per arm.
- **Samples without a boundary** (a plain agent without `baseline()`, a sample that failed before the swarm started) keep their total as `sample_total_usage`, never as solve usage, flagged `no_solve_usage`; the user's analysis leaves them out of solve-cost comparisons and reports how many per arm. `attempt_rows()` would add `solve_usage`, `solve_exit` and scoring usage to each row, and the ledger would take arm totals from `solve_usage` ([swarm-limits.md](swarm-limits.md)).

Two alternatives lost: subtracting scoring model events from the total (native compaction during scoring has no event, so it would count as solve cost), and snapshotting only on a normal return with the sample total as a fallback (budget exhaustion is a normal way for an arm to end, and the fallback would include grading, which differs between arms).

## Decisions

- 2026-10-07: the default chain's order is verify, then vote, then first (decision: Ransom, 2026-10-07).
- 2026-10-08: the swarm adds no scorer to the headline; arms are compared on sample usage with grading included; the cost boundary is deferred (decision: Ransom, 2026-10-08). It stays deferred (decision: Ransom, 2026-11-10).
- 2026-10-20: a gate pass assumes `react()`'s default `value_to_float()`, so a member with its own converter can record passes that differ from its own gate (decision: Ransom, 2026-10-20).
- 2026-10-20: inspect_swarm ships no best-of-k reducer, and its analysis helpers supply only what the swarm records (decision: Ransom, 2026-10-20).
- 2026-10-21: `leaderless()` stops the swarm at the first submission by default, with the stop reason `submitted` (decision: Ransom, 2026-10-21).
- 2026-10-22: `last` and the controller's own choice of the answer, which goes before the chain (decision: Ransom, 2026-10-22).
- 2026-10-27: `synthesize`'s fence is a label, not a security boundary: peer text is quoted as written (decision: Ransom, 2026-10-27).
- 2026-10-29: the record lives in the transcript, not the store (decision: Ransom, 2026-10-29).
- 2026-10-30: a log that kept the record in the store is refused by `member_scores()`, not scored as a plain agent (decision: Ransom, 2026-10-30).
- 2026-11-03: the record holds model text as Inspect's sample-writing code gives it back; free-form fields hold what JSON and YAML parsers give; any other object fails the sample with a `TypeError` (decision: Ransom, 2026-11-03).
- 2026-11-04: task code (answer keys, verifiers, objects in outputs) is trusted: its checks catch mistakes, and a refusal promises the error and the partial record, not that none of the object's methods ran (decision: Ransom, 2026-11-04).
- 2026-11-09: the default chain falls through: nothing verified, so vote; no keys, so first (decision: Ransom, 2026-11-09).
- 2026-11-10: the final record keeps no vote counts by answer key (decision: Ransom, 2026-11-10).
- 2026-11-10: a persistent member votes once, by its latest answer, and abstains when that answer has no key; `baseline()` refuses a persistent member, and persistent baselines are left to their own design (decision: Ransom, 2026-11-10).
- 2026-11-12: an output the record cannot hold fails the sample with a `ValueError` naming the member, rather than being left out of the record; this reverses the decision of 2026-10-30 that one member's model output never costs the sample its record or its scores, narrowed to model-made values on 2026-11-03 (decision: Ransom, 2026-11-12).

## Alternatives considered

**Pass `ResultSpec` to a registered `member_scores` scorer.** The obvious shape, but Inspect logs a plain dataclass argument as its type name, so default `inspect score` would rebuild the scorer with a string in place of the spec. A registered spec would need a new registry type in inspect_ai. A task-owned no-argument factory uses existing machinery and keeps the callables in the task's code.

**Compute per-member scores inside the swarm**, calling the task's scorer at the end of the solve. Scorers belong to the task and need the target, which the solver should not use; their usage would land in the solve, and re-scoring would miss them.

**Replace the task's scorer with a swarm scorer** returning `{"final", "best_member", "mean_member"}`. The headline would differ in name and shape between arms, and the task's metrics would change.

**A separate scorer per member** (`member_1`, `member_2`, …). The number of scorers would vary with k, so arms would not line up, and "member 3" means nothing in a leaderless swarm. Per-member detail lives in metadata.

**Plain agents as baselines, with evidence computed at scoring time** (the verifier in `member_scores()`, attempts timed by `working_time`). Re-scoring would rerun a possibly sandbox-dependent verifier, `working_time` includes scoring so a slow grader could reverse which attempt counts as first, and a limit-truncated output has no eligibility flag. A swarm of one records the same evidence at run time under the same rules.

**Selection comparisons as epoch reducers** (`select_at(k)`). A reducer sees one scorer's scores, so keys and verdicts would ride in score metadata; Inspect would apply it to every other scorer too; and subset sampling does not fit a reducer. best@k, a closed form on values alone, would fit as a reducer beside `pass_at(k)` if wanted.

**Voting with Inspect's `mode`/`majority` reducers.** They vote on correctness values, not on answers.

**Strict modes only**, where nothing verified means no answer. Simpler, and it measures the verifier alone, but the default swarm would return nothing where an attempt would otherwise be scored on its answer. Strict behaviour stays available as a single mode.

**`final=` on `swarm()`.** One fewer object, but the valid modes and the default depend on the topology, so the chain is the controller's parameter ([swarm-api.md](swarm-api.md#controllers)).

**Count never-started members as empty** (`empty_value`). A conditional controller's choice not to run a member would read as that member failing, lowering `mean_member` because of a scheduling decision. Not started is its own outcome, with `members_started` reported.

**Score unsubmitted outputs as the chain's last fallback.** It matches how Inspect scores a plain agent that hits its limit, but it selects working messages as answers. Per-member scores still grade those outputs.

**Treat any unavailable member as making the sample unscored.** One failed model grade among k would discard the others. Aggregating over valued members with `complete` keeps the data and lets analysis choose.

**Other votes for a persistent member.** Letting every answer vote would use every key, but a member that answers often would outweigh its peers. Its earliest keyed answer would match `first`, and its latest keyed answer would keep a member voting after an answer with no key, but both vote with an answer the member has since replaced. A vote is a member's view, and its latest answer is its current one; when that answer has no key, the member abstains, as any member with no key does (decision: Ransom, 2026-11-10). A swarm whose persistent workers answer different subtasks gives keys that do not compare, which no rule fixes; it uses `last`, `choose_answer()` or a controller of its own.

**Vote counts in the final record.** They would make a vote auditable from the record alone, but the keys are model text in dict keys, which a log's processing does not rewrite, so a removed image URI would survive there. Counting from the members' recorded keys gives the same information with the record's own handling of model text.

**Per-member sandboxes for per-member artifacts.** Clean attribution, but as the default it would change the decided topology (a shared sandbox), whose filesystem is a channel. Per-member sandboxes are an opt-in ([swarm.md Sandbox topology](swarm.md#sandbox-topology)); `materialize` covers the shared default.

## Compatibility

- **Records.** `version` is 1 throughout. The contract's member and record fields, the chain fields, `persistent`, `answer_times`, `answer_results`, `choose_name` and `end_origin` all have defaults, so records written before them load, and a record without a contract reads as a swarm without one (`kind=None`, so `member_scores()` reports `kind_not_declared`). The record has no generated TypeScript type; it is read through `swarm_result()`.
- **Controllers.** `MemberIdle.verdict` defaults to `None`, so code that builds the event is unchanged.
- **Logged parameters.** `swarm()` logs `result` and `leaderless()` logs `stop_on_verified` and `final`, so a task's `task_identifier()` can differ between inspect_swarm versions that gained them, as for any solver that gains a parameter; arms in one eval set run one installed inspect_swarm.
- **Names.** `member_scores`, `MEMBER_METRICS`, `best_member`, `mean_member`, `baseline`, `attempt_rows`, `select_at_k`, `AttemptRow` and the record's fields are an interface the analysis and users' notebooks depend on; changing them is a breaking change. The member scorer's own name is the task's.
- **inspect_ai.** Public scorer, reducer and analysis APIs, and private ones inspect_swarm may use: `registry_value()` and `model_create_from_dict()` for `Synthesize.model`, `condense_sample()`, `resolve_events_attachments()`, `to_json_safe()` and `get_deserializing_context()` for the record's round trip, `MAX_TOOL_CALL_ARGUMENTS_DEPTH`, `TerminateSampleError`, `eval_log_location()` for `AttemptRow.log`, and the transcript's private `_event()`.
- **Tasks.** Tasks need no change: their scorers score the swarm's output. A task without `result=` makes no per-member claims, and with `final=None` and no controller choice its answer is `first`. A task whose scorer can read the answer from the environment declares `kind="artifact"` or supplies an answer-only adapter.

## Security

Untrusted input reaching this code:

- **Member outputs are model output**, a persistent member's answers included. They reach the returned output exactly as a single agent's would; the record, as each member's `output`, written when the swarm ends, so no member reads a peer's output from it; `answer_key`, task code that must accept any text and return `None` rather than raise on unparseable input; the verifier, which may execute the answer and must do so only inside the sandbox; the member scorer, as from a single agent and only after the solve; and `synthesize`'s prompt, fenced as a label and not a boundary, in a separate call outside every member's context. That is weaker than tool output, so `synthesize` is never a default.
- **Model text in the log.** Outputs, keys and explanations are stored as JSON data in the record's event and in score metadata, not as Markdown. An output the record cannot hold fails the sample ([Model text in the record](#model-text-in-the-record)). The chosen answer reaches the sample's output unchanged, and Inspect logs it as it logs a single agent's. Objects only task code can put in an output fail the sample rather than being logged.
- **Answer attribution.** The answer-scored contract forbids scorers that read the answer from the environment, and the empty-output backstop keeps the commonest case from reaching the scorer.
- **The target.** Neither the verifier, nor `answer_key`, nor any member tool is given it. Agents that consult the task's scorer in the loop are flagged `target_assisted`.
- **Controllers.** Handles and control events carry no member text, and a verdict, a persistent answer's included, reaches a controller only as `VerdictSummary(passed, value)`, on the handle, `MemberEnded` or `MemberIdle`. Boundary callbacks are the exception: they see copies of a member's conversation and output, and can return messages for its conversation, as trusted eval-author code whose changes the turn seam records ([swarm-turn-seam.md Security](swarm-turn-seam.md#security)). The verdict's explanation, which may quote the answer or a judge model, stays in the record, so a controller cannot relay member text through a verdict. The `verdict` event carries no explanation either.
- **Shared-sandbox attribution.** Where members can write each other's work (any shared location without users isolation, a writable share with it), any member can alter another's worktree or branch, so per-member artifact scores are attributions by convention; under users isolation a member's private directory is writable only by that member and root ([swarm-sandbox-isolation.md Scoring](swarm-sandbox-isolation.md#scoring)). `materialize` receives only roster names, so a member cannot steer which path is materialized.
- **Reward hacking.** Members can read whatever the sandbox holds, scorer test files included, exactly as a single agent can. Nothing here adds or removes that exposure.

## Not this design

- **Persistent baselines.** `baseline()` and `select_at_k()` refuse a persistent member. Reading its attempt would take one of its answers (the one `final.answer` names, with its time, key and verdict from `answer_times` and `answer_results`), keep the member's stop time for artifact tasks, and handle records written without `answer_results`; that can be its own design if a comparison needs persistent baselines (decision: Ransom, 2026-11-10).
- **A `reporter` mode** and the coordinator topology that would accept it.
- **Scoring under per-member sandboxes** in the result contract: artifact team@k becomes an oracle over k artifacts there ([swarm-lean-open-problems.md Recommendation 8](swarm-lean-open-problems.md#8-after-m2-per-member-sandboxes-in-the-scoring-contract)).
- **A best-of-k reducer in inspect_ai**, generalising `pass_at(k)` to graded scores, if wanted.
- **Per-scorer epoch reducers** (inspect_ai). Today every reducer applies to every scorer and key, which fills logs with meaningless cells such as `pass_at` of `mean_member`.
- **A no-recovery option for Frontier-CS's scorer** (inspect_evals), so swarm tasks need no adapter.
- **Re-scoring with a sandbox.** Inspect cannot re-score sandbox scorers; snapshots of the final environment would be needed.
- **Per-member trajectory scoring.** Scorers that read tool calls or intermediate messages would need each member's conversation in a scorer-readable form.
- **Harness-validity and cost as scores**, so a member that never acted, or realized cost, appears in eval-set summaries.
- **A verifier library**: common verifiers (run visible tests, check a format, a model judge with no target) as reusable functions.
- **The user docs' account of logged model text.** [Scoring, The record belongs to the swarm](../docs/scoring.qmd#the-record-belongs-to-the-swarm) says the record keeps model text as a log keeps a sample's own output, so a scorer reads the same values during the eval and when it re-scores; with `log_images=False` a data URI differs between the two ([Model text in the record](#model-text-in-the-record)).
- **The user docs' member states.** [Scoring, The result record](../docs/scoring.qmd#the-result-record) lists a member's `state` as `pending`, `running` or `ended`; a provisional record can also hold a persistent member's `idle` or `ending`.
