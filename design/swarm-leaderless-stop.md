# Inspect Swarm: leaderless's stop follows the answer chain

## Overview

`leaderless()` stops the swarm at the first submission by default. That is right when the answer is the first submission, and wrong for every other final-answer chain: a swarm whose result contract has a verifier or an answer key gets the default chain `verify`, `vote`, `first`, but stops before there is anything to verify or vote over, and an explicit `leaderless(final="vote")` is refused until the task also passes `stop_on_submission=False`.

This design replaces `leaderless()`'s two stop flags, `stop_on_submission` and `stop_on_verified`, with one keyword-only parameter, `stop`, that takes two values. `stop="answer"`, the default, stops the swarm as soon as its answer can no longer change, which the leading mode of the chain the swarm applies decides: a chain led by `first` stops at the first submission, as today; a chain led by `verify` stops at the first submission that passes the verifier, and when nothing passes every member runs to its end and the rest of the chain chooses from all their submissions; any other chain (`vote`, `last`, `synthesize`) runs every member to its end. `stop="end"` always runs every member to its end, for shared artifacts, team@k, graded verifiers and member scores. Since `final=None` is resolved against the task's result contract only inside `swarm()`, the controller learns the resolved chain at run time from a new read-only `SwarmControl.final_chain`. The old flags go without an alias, a breaking change.

## Why

The final-answer chain and the default stop were decided separately, and they do not compose ([swarm-scoring.md The final-answer chain](swarm-scoring.md#the-final-answer-chain)):

- **The default chain has nothing to choose from.** A task that passes `result=answer_result(answer_key=..., verifier=...)` and the default controller gets the chain `["verify", "vote", "first"]`, but `leaderless()` returns `submitted` at the first submission and the other members are stopped. `verify` sees one submission (and any made during grace); when it fails, `vote` votes over that one, so the answer is the first submission whatever the verifier said. `tests/test_final.py::test_leaderless_stopping_at_the_first_submission_applies_the_default_chain` pins exactly this today: member `a` submits a failing `7`, the swarm stops with reason `submitted`, the slow member is cancelled, and `vote` chooses `a`. The docs example in [Scoring, The result contract](../docs/scoring.qmd#the-result-contract) is such a task: a format-checking verifier and an answer key under the default controller.
- **An explicit chain needs a second argument.** `leaderless(final="vote")`, `final="last"`, `final="verify"` and `stop_on_verified=True` each raise `ValueError` unless `stop_on_submission=False` is also passed, because the default stop contradicts them. The user has already said what they want; the extra flag is ceremony.
- **The refusal cannot cover the default.** `leaderless()` refuses only what it sees. `final=None` is resolved against the contract inside `swarm()`, after the controller is built, so the contradiction above is accepted silently and documented instead ([Controllers, leaderless](../docs/controllers.qmd#leaderless): "that chain chooses from the submissions made before the members stopped, usually just the first").
- **Two flags describe more stops than anyone needs.** Once the default follows the chain, the only stop a task still has to ask for is "run every member to its end". Two flags, or a value per stop, would add combinations that contradict the chain (stop at the first submission under `vote`; stop at the first pass but answer with `last`) and need rules and refusals of their own, and nothing in the repository uses them.

Ransom chose to fix the first three by making the stop at the first submission the default only when the chain is `first`, with an explicit `final=` seen when the controller is built and `final=None` learnt at run time through a new `SwarmControl` surface (decision: Ransom, 2026-11-10), and the last by replacing the two flags with one keyword-only `stop` that takes `"answer"` or `"end"`, decided by the chain's leading mode, with no alias for the old flags (decisions: Ransom, 2026-11-11 and 2026-11-12).

## Goals and non-goals

Goals:

- `leaderless()` with no `stop` stops as soon as its answer can no longer change, for every chain `swarm()` accepts, explicit or resolved from the contract.
- `stop="end"` runs every member to its end whatever the chain.
- The controller learns the resolved chain through `SwarmControl`, so other controllers can use it too.

Non-goals:

- A new stop rule. The stops are the two `leaderless()` already has (`submitted`, `verified`) and running every member to its end; an early stop for `vote` once its plurality is decided is not designed here ([Not this design](#not-this-design)).
- Stops that contradict the chain. "Stop at the first submission and vote over what came in" is, in effect, `final="first"`; "stop at the first pass but answer with another mode" is a custom controller's, as `examples/stop_on_proof.py` writes one (decision: Ransom, 2026-11-12).
- A deprecation path for `stop_on_submission` and `stop_on_verified` (decision: Ransom, 2026-11-11). inspect_swarm is experimental ([swarm-api.md](swarm-api.md)), and an alias would bring the flags' combination rules back.
- The default stop for `artifact` contracts as such. A shared-artifact task with neither a verifier nor an answer key keeps the chain `first` and so the stop at the first submission, which the docs already tell such tasks to turn off.
- Any change to the chain, its modes, verification, the record, or the stop procedure.
- Recording the resolved stop in the result record. The record's `final.chain` and the ledger's `stop_reason` already say which chain applied and which stop fired.

## Current behaviour

**`leaderless()`** ([`leaderless.py`](../src/inspect_swarm/_controller/leaderless.py)) takes `stop_on_submission: bool = True`, `final: FinalSpec | None = None` and `stop_on_verified: bool = False` (`leaderless.py:9`). At construction it validates `final` with `check_final()`, turns an explicit `final` into mode names with `final_chain(final, None)` and `chain_names()`, and raises `ValueError` for a `reporter`, for an explicit chain other than `["first"]` while `stop_on_submission` is true (`leaderless.py:40`), and for `stop_on_verified` with `stop_on_submission` (`leaderless.py:44`). `final=None` is accepted with the default stop. `run` (`leaderless.py:49`) starts every member and iterates `events()`: with `stop_on_verified` it returns `verified` at a `MemberEnded` or `MemberIdle` whose verdict passed; with `stop_on_submission` it returns `submitted` at a `MemberIdle` or a submitted `MemberEnded`; when the iterator ends it returns `quiescence` if any member is idle, else `all_done`. It passes `final` to `Controller` unchanged (`leaderless.py:72`).

**Resolving the chain.** `swarm()` resolves the controller (`_swarm.py:104`), calls its `check` with the roster only (`_swarm.py:105`), and later builds the chain with `final_chain(resolved_controller.final, resolved_result)` and checks it against the contract with `check_chain()` (`_swarm.py:118`). `final_chain()` (`_final.py:134`) gives, for `final=None`, `verify` if the contract has a verifier, then `vote` if it has an answer key, then `first`; `check_chain()` refuses `verify` without a verifier and `vote` without an answer key. The chain is passed to `SwarmRun`, which keeps it as `self._chain` (`_control.py:647`) and uses it for the provisional and final answers; the record writes its names as `final.chain`. `first` answers whenever anyone submitted, so the modes after a leading `first` never choose.

**What a controller sees.** `SwarmControl` (`_control.py:267`) has `members`, `member()`, `start()`, `wake()`, `events()` and `choose_answer()`. Nothing on it says which chain the swarm applies, and `Controller.check` sees only the roster. A `Controller` is configuration shared by every swarm built with it, and the same object can be passed to two `swarm()` calls with different contracts, so the chain cannot be stored on it.

**Logged parameters and reconstruction.** A controller factory logs only the arguments it was called with: `registry_params(leaderless())` is `{}`, `registry_params(leaderless(final="first"))` is `{"final": "first"}`, and `leaderless(stop_on_submission=False)` logs `{"stop_on_submission": false}` (checked against this branch's environment). Those params are called again only when the solver is rebuilt from its logged arguments (`--solver`, inspect_flow solver specs, `create_registry_object()` on a plan step); `eval-retry` of a task file or registered task re-runs the task's current code instead ([swarm-api.md Logging and replay](swarm-api.md#logging-and-replay)).

**Who uses the stop flags.** `stop_on_submission` and `stop_on_verified` are used in `src/`, `examples/`, `docs/`, `README.md` and `tests/`; `baseline()` builds `leaderless(final="first")` with neither. Most uses are `stop_on_submission=False`, to run every member to its end, and every `stop_on_verified=True` runs under a default chain led by `verify`. Two cases need judgment rather than a rename:

- The tests in `tests/test_result.py` (and `tests/test_final.py`'s pin test above) that build a swarm with a verifier or key and the default controller rely on today's stop at the first submission.
- ts-mono's viewer end-to-end fixture `apps/inspect/e2e/fixtures/communication/generate_log.py`, in another repository, builds `leaderless(stop_on_submission=False)` with no result contract; it is the only sibling that imports `inspect_swarm`.

The only verifier in the docs and examples, Scoring's `check_answer`, returns pass or fail with no `value`. No other class implements `SwarmControl`; `SwarmRun` is the only one.

## Design

### `SwarmControl.final_chain`

A read-only property on the `SwarmControl` protocol, implemented by `SwarmRun`:

```python
# src/inspect_swarm/_controller/_control.py, on SwarmControl
@property
def final_chain(self) -> Sequence[str]:
    """The final-answer chain the swarm applies, by mode name, such as `["verify", "vote", "first"]`.

    It is the controller's `final` or, when that is `None`, the default for the swarm's result contract. The result record's `final.chain` gives the same names.
    """
    ...
```

`SwarmRun.final_chain` returns `tuple(chain_names(self._chain))`: the names only, so a controller sees neither a `synthesize` mode's model nor its prompt, and a tuple, so it cannot change the chain the runtime applies. It is available from the start of `run` (the chain is fixed when `SwarmRun` is built) and unchanged for the sample. A controller's choice through `choose_answer()` goes before the chain as today; `final_chain` does not reflect it.

### `leaderless()`

```python
# src/inspect_swarm/_controller/leaderless.py
@swarm_controller
def leaderless(
    *,
    stop: Literal["answer", "end"] = "answer",
    final: FinalSpec | None = None,
) -> Controller: ...
```

Both parameters are keyword-only; `stop_on_submission` and `stop_on_verified` are gone (decision: Ransom, 2026-11-12).

| `stop` | Chain's leading mode | The swarm stops at | Stop reason |
|---|---|---|---|
| `"answer"` (default) | `first` | the first submission: a submitted `MemberEnded` or a persistent member's `MemberIdle` | `submitted` |
| `"answer"` | `verify` | the first submission whose verdict passed, on `MemberEnded` or `MemberIdle`; every member's end when none passes | `verified` when a passing submission stops it; otherwise `all_done` or `quiescence` |
| `"answer"` | anything else (`vote`, `last`, `synthesize`) | every member's end, or quiescence when members are left idle | `all_done` or `quiescence` |
| `"end"` | any | every member's end, or quiescence when members are left idle | `all_done` or `quiescence` |

When `run` starts, `leaderless` reads `swarm.final_chain` and decides from its first mode alone:

```python
# Which early stop leaderless arms: the first submission, the first passing submission,
# or none, from the leading mode of the chain the swarm applies.
def _early_stop(chain: Sequence[str], stop: Literal["answer", "end"]) -> Literal["submission", "verified"] | None:
    if stop == "end":
        return None
    return {"first": "submission", "verify": "verified"}.get(chain[0])
```

`run` then iterates `events()` as today, with one comparison in place of the two flags: with `"verified"` armed it returns `verified` at a `MemberEnded` or `MemberIdle` whose verdict passed; with `"submission"` armed it returns `submitted` at a `MemberIdle` or a submitted `MemberEnded`; when the iterator ends it returns `quiescence` if any member is idle, else `all_done`. A chain is never empty (`check_final()` refuses an empty one), so `chain[0]` exists, and `check_chain()` refuses `verify` without a verifier, so a chain led by `verify` always gets verdicts.

**The leading mode decides** (decision: Ransom, 2026-11-12). The chain tries its modes in order and the first that produces an answer wins, so the leading mode alone says when the answer can no longer change. `first` answers whenever anyone submitted, so a chain led by `first`, such as `["first", "last"]`, is decided at the first submission: every mode after it would choose from the same submissions and is never reached. A chain led by `vote` runs every member to its end even when `verify` comes later in it.

**A chain led by `verify` stops at the first passing submission** (decision: Ransom, 2026-11-11). `verify` answers with the best passing submission, so a submission that fails cannot be the answer while any passes, and stopping on it, as today, throws the verifier's work away. Two stops fit: the first passing submission, or every member's end. The first passing submission is the default because it is what `leaderless`'s default already is for `first`, applied to the verified chain: stop as soon as the chain's leading mode has its answer, and save the rest of the budget (decision: Ransom, 2026-10-21, for the stop at the first submission). When nothing passes, no stop fires, so every member runs to its end and `vote` and `first` choose from all their submissions, which is what the fall-through chain is for. A verifier that grades passes with `Verdict.value`, where a later pass could rank higher, is the case it gives up; the only verifier in the docs and examples is pass or fail, and such a task passes `stop="end"`.

**Any other chain runs every member to its end**, or ends at `quiescence` when persistent members are left idle (decision: Ransom, 2026-11-11). `vote` needs votes, so a stop at the first submission makes it `first`. Stopping when a plurality can no longer change would need the controller to compare answer keys, which are model text and never reach a controller, and a persistent member can change its vote by answering again; neither stop exists today. `last` and `synthesize` need every submission by definition.

**`stop="end"`** runs every member to its end whatever the chain: for tasks whose scorer reads a shared artifact, arms that measure each member's result (team@k, `member_scores()`), graded verifiers, and agents for which any normal return counts as a submission.

**Refusals.** `leaderless()` raises `ValueError` for a `reporter` in `final`, as today, and for a `stop` other than `"answer"` or `"end"`, naming the two values, as `check_final()` does for `final`. A positional argument is Python's own `TypeError`. Nothing is refused at run time.

**Docstring.** The `leaderless` docstring's summary becomes "Start every member with the swarm's input; stop when the answer can no longer change, or when none is still working". Its body states what `"answer"` does for each leading mode and when to pass `stop="end"`. The `Args` describe `stop`'s two values; the `Raises` lists the two refusals. `Controller`'s `final` argument notes that `run` can read the resolved chain from `SwarmControl.final_chain`.

### What else is unchanged

`baseline()` builds `leaderless(final="first")`, whose chain is `["first"]`, so it stops at its member's submission as today. The stop procedure, grace, verification through the stop, the provisional answer and finalisation are as in [swarm-scoring.md](swarm-scoring.md#the-final-answer) and [swarm-limits.md](swarm-limits.md#stopping). A persistent member's `MemberIdle` still counts as a submission for the `submitted` stop and carries its verdict for the `verified` one ([swarm-persistent-members.md](swarm-persistent-members.md#the-swarms-answer)); `leaderless` still never wakes a member, so under a `vote` chain a persistent member answers once and the swarm ends at `quiescence`. The stop reasons and the ledger's `stop_reason` are unchanged.

## Alternatives considered

**A value per stop** (`stop: Literal["submission", "verified", "end"] | None`, `None` following the chain). Each stop could be asked for with any chain, but two of the three values are reachable only by contradicting the chain (stop at the first submission under `vote`, stop at the first pass under `last`), which needs a rule of its own, an edge case for `"verified"` without a verifier, and tests for combinations nothing in the repository uses. `"answer"` and `"end"` cover every use (decision: Ransom, 2026-11-12).

**Keep the two flags with `None` defaults.** Each flag would default to "follow the chain" and an explicit value would keep its meaning. It breaks no call, but the flags' four combinations need a coupling table, refusals for the contradictory ones, and a compatibility note for `stop_on_verified=False` on its own, which would change behaviour under a keying or verifying contract anyway.

**Keep the old flags as deprecated aliases of `stop`.** Old logs and calls would still work, but the aliases bring back the combination rules for as long as they last, `stop_on_verified` has no released callers to protect, and inspect_swarm is experimental (decision: Ransom, 2026-11-11).

**`stop` as the first positional parameter**, where `stop_on_submission` was. It would accept `leaderless("end")`, but an old positional `leaderless(False)` would then need a value check of its own; keyword-only lets Python refuse it (decision: Ransom, 2026-11-12).

**Decide `first` only for the exact chain `["first"]`.** It treats `first` differently from `verify` and runs a chain such as `["first", "last"]` to every member's end, though nothing after `first` can change its answer.

**Every member's end for a `verify` chain.** Simpler to explain (only `first` stops early) and right for a graded verifier, but a pass/fail verifier, the only kind the docs and examples use, gains nothing from the members that keep working after the first pass, and the swarm spends up to k times the budget for the same answer. A task that wants it passes `stop="end"`.

**Pass the chain to the controller when the swarm is built** (a hook such as `Controller.check(roster, chain)`). The chain is known in `swarm()`, but a `Controller` is shared configuration, possibly passed to swarms with different contracts, so the hook would have to return a per-swarm controller or the result would have to be stored per swarm somewhere else. `run` already receives a per-sample `SwarmControl`; a property there needs no new lifecycle.

**Pass the chain as an argument to `run`.** It would change the signature of every controller's `run`, a breaking change for an additive need.

**Expose the result contract** (`SwarmControl.result`, or flags for a verifier and an answer key). More general, but the stop depends on the chain, not the contract: an explicit `final` overrides the contract's default, and the contract's callables do not belong in a controller's hands. The chain's names are the narrowest fact that decides the stop.

**Keep refusing and only document it** (the rule before this design). It leaves the default chain without submissions to choose from, which is the problem.

## Compatibility and migration

- **Breaking: the old keywords are gone.** `leaderless(stop_on_submission=...)` and `leaderless(stop_on_verified=...)` raise `TypeError` (an unexpected keyword argument), and so does any positional argument. `stop_on_submission=False` becomes `stop="end"`. `stop_on_verified=True` with `stop_on_submission=False` becomes the default under a chain led by `verify`, so it is dropped. `stop_on_submission=True` becomes the default under a chain led by `first`; under any other chain it has no equivalent (`final="first"` is the nearest).
- **Logs made with the old keywords.** A rebuild that passes the logged controller params, `--solver` replay or an inspect_flow solver spec, fails with that `TypeError` against the new version, and so does a retry of task code that still calls the old keywords. `eval-retry` of a task file or registered task re-runs the task's current code ([swarm-api.md Logging and replay](swarm-api.md#logging-and-replay)), so once that code uses `stop`, it retries from a log made with the old keywords. Reading the log (the record, the ledger, `swarm_result()`, `member_scores()` re-scoring) is unaffected.
- **Behaviour of the default.** A swarm with the default controller and a result contract that has a verifier now runs until a submission passes, or until every member ends; one with an answer key and no verifier runs every member to its end. Both can spend more than before (up to every member's budget) and give a different answer, since the chain now sees more submissions; the stop reason `verified`, `all_done` or `quiescence` replaces `submitted` in their ledgers. A swarm with no contract, or a contract with neither part (including `artifact_result()` with only `member_artifacts`), stops at the first submission as before.
- **Accepted calls.** `leaderless(final=...)` with any chain the contract supports builds, where an explicit non-`first` chain needed `stop_on_submission=False` before.
- **Task identity.** The default call still logs `{}`, so a task built with `leaderless()` has the same `task_identifier()` before and after, while its stop changes when its contract keys or verifies. An eval set retried across the upgrade could mix the two stops under one identifier; as for any behaviour change, arms in one eval set run one installed inspect_swarm ([swarm-scoring.md Compatibility](swarm-scoring.md#compatibility)). A task that passed a stop flag gets a new identifier, since its logged params change from the flag to `stop`.
- **ts-mono.** The viewer's end-to-end fixture `apps/inspect/e2e/fixtures/communication/generate_log.py` passes `leaderless(stop_on_submission=False)`. When ts-mono moves to an inspect_swarm with this change, that call becomes `leaderless(stop="end")` in a ts-mono PR; its committed `swarm.eval` need not be regenerated, since the swarm behaves the same.
- **Public API.** The controller interfaces are experimental and may change in any release ([swarm-api.md](swarm-api.md)). `SwarmControl` gains `final_chain`. Code that calls `SwarmControl` is unaffected; a class of its own that implements the protocol would need the property to type-check, and there is none in this repo or its siblings.
- **Stored formats.** None: the record, its `final.chain`, the evidence events and the ledger fields are unchanged. No viewer type is generated for any of them.

## Security

The new property returns mode names fixed by the eval author's code or by `final_chain()`'s defaults; no model output, tool argument or member text reaches it, and it hides a `synthesize` mode's model and prompt. `stop` is the eval author's own argument, checked against its two values. The stop decision reads only those names, `stop` and the existing `VerdictSummary.passed`, which is already the controller's ([swarm-scoring.md Security](swarm-scoring.md#security)). Running more members to their end under a verifying or voting contract adds no exposure of its own: every member runs under the swarm's caps and the sample's limits as it would with `stop="end"`.

## Testing

All in-process with scripted models (no network, Docker or provider); the AnyIO tests run on asyncio and trio through `tests/conftest.py`.

- `tests/test_final.py`:
  - The stop rule, table-driven, through `leaderless`'s observable behaviour in a run rather than the private helper, asserting the stop reason, the members' statuses and the chosen member. Two member compositions keep the rows apart:
    - Rows that expect an early stop use a finite `_team()` (a failing `7` first, then a passing `42`) plus a blocked late member, `sleeper()`, as `test_leaderless_stops_at_the_first_verified_submission` does, with a short `Budget(grace=...)`, so the late member is ended by the stop and cannot submit during grace: no contract (`submitted`, `first`, the sleeper cancelled); `final=["first", "last"]` (`submitted`, `first`); verifier and key (`verified`, the passing member, the sleeper cancelled).
    - Rows that expect every member's end use finite `_team()` members only, with no sleeper, so nothing waits on a member that never ends: key only (`all_done`, `vote` over every member); verifier and key with nothing passing (`all_done`, fall-through to `vote`); `final="vote"` with an answer key (`all_done`); `stop="end"` with a verifying contract (`all_done`, no `verified` stop); `stop="end"` with no contract (`all_done`, `first`).
  - `test_leaderless_stopping_at_the_first_submission_applies_the_default_chain` goes: it pins the stop this design removes, and the row for a verifier and key replaces it.
  - `test_a_final_rule_the_swarm_cannot_apply_is_refused` and `test_a_chain_the_swarm_cannot_apply_is_refused`: the `last-stopping-at-first`, `chain-stopping-at-first` and `stop-on-verified-stopping-at-first` cases go, and the `unknown-mode` case passes `stop="end"`; acceptance cases assert that `leaderless(final="last")`, `leaderless(final="vote")` and `leaderless(final=["verify", "first"])` build; refusal cases assert `ValueError` for `leaderless(stop="first")`, and `TypeError` for `leaderless(stop_on_submission=False)`.
  - A persistent member under the default `vote` chain answers once and the swarm returns `quiescence`; under a default `verify` chain a passing `MemberIdle` returns `verified` (the existing persistent `stop_on_verified` test, with the explicit arguments dropped).
- `tests/test_controller.py`: a custom controller reads `swarm.final_chain` and sees `("first",)` with no contract, `("verify", "vote", "first")` with both contract parts, and its own explicit chain; the value is a tuple.
- `tests/test_result.py`: the tests that build a swarm with a verifier or key and the default controller and rely on the stop at the first submission are reshaped, either to `stop="end"` with scripted members whose submissions the test controls, or to the new default's assertions (`verified` at the first pass, or every member's end).
- `tests/test_registry.py`: `registry_params(leaderless())` stays `{}`, and `leaderless(stop="end")` logs `{"stop": "end"}` and rebuilds to the same stop.
- Every other test that uses the old flags migrates: `stop_on_submission=False` becomes `stop="end"`, `stop_on_verified=True` (under a chain led by `verify`) is dropped, and a test parametrized over the old flag maps `True` to `"answer"` and `False` to `"end"`, keeping any expectations that branch on it aligned.

## Implementation plan

One PR, titled as a breaking change (`feat!:`), since the property, the new parameter and the removed flags are one user-visible change. It ships as a breaking change before 0.4.1, so `stop_on_verified` is never released.

1. `src/inspect_swarm/_controller/_control.py`: `final_chain` on `SwarmControl` and `SwarmRun`.
2. `src/inspect_swarm/_controller/leaderless.py`: the keyword-only `stop` in place of the two flags, `_early_stop()`, the value check, the construction refusals reduced to `reporter` and an unknown `stop`, `run` deciding from `swarm.final_chain`, the docstring.
3. `src/inspect_swarm/_controller/_controller.py`: the `final` docstring's note on `final_chain`.
4. Every use of `stop_on_submission` or `stop_on_verified` in `src/`, `examples/`, `docs/`, `README.md` and `tests/`, found with a search and migrated as [Compatibility and migration](#compatibility-and-migration) and [Testing](#testing) say, with the `tests/test_result.py` cases reshaped.
5. Docs: [Controllers, leaderless](../docs/controllers.qmd#leaderless) (`stop` and its two values; what `"answer"` does for each leading mode; `stop="end"` for shared artifacts, team@k, graded verifiers and agents whose any return is a submission; `final_chain` under Writing a controller; and the `leaderless` bullet under Persistent members, which says the first answer stops the swarm by default and verified stopping is opt-in: the first answer stops it by default only under a chain led by `first`, the first passing answer under a chain led by `verify`, and otherwise the swarm ends at `quiescence` when no stop fires), [Scoring](../docs/scoring.qmd) (the `last` line, the result-contract example's stop, Scoring what members leave), `README.md` (its default-stop and default-answer lines say that a verifier or an answer key changes the default chain and so the stop), and the reference page picks up the docstrings. The release notes come from the PR title and body: the body names the keyword mapping and that a rebuild passing the old keywords, or unchanged task code calling them, fails.
6. Designs: [swarm-scoring.md](swarm-scoring.md#the-final-answer-chain), [swarm-api.md](swarm-api.md#controllers) and the other designs that name the stop already use `stop`; when the change is built, an as-built pass folds this document into swarm-scoring.md.

After the release, ts-mono's `apps/inspect/e2e/fixtures/communication/generate_log.py` changes to `leaderless(stop="end")` when ts-mono moves to it.

## Not this design

- **An early stop for `vote`** once no outstanding member can change the plurality: it would need answer-key equality to reach the controller (keys are model text and do not today) and a rule for persistent members, who can change their vote.
- **A default stop for `artifact` contracts**: a shared-artifact task scored on the environment arguably wants every member's end by default whatever its chain, which would need the contract's kind, not just the chain, to reach the controller.
- **Recording the resolved stop** (for instance in the ledger beside `stop_reason`), so a log says which stop was armed even when none fired.
