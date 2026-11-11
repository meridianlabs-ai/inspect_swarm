# Inspect Swarm: leaderless's default stop follows the answer chain

## Overview

`leaderless()` stops the swarm at the first submission by default. That is right when the answer is the first submission, and wrong for every other final-answer chain: a swarm whose result contract has a verifier or an answer key gets the default chain `verify`, `vote`, `first`, but stops before there is anything to verify or vote over, and an explicit `leaderless(final="vote")` is refused until the task also passes `stop_on_submission=False`.

This design makes the default stop follow the chain the swarm actually applies. With the chain `first` the swarm stops at the first submission, as today. With a chain that starts with `verify` it stops at the first submission that passes the verifier, so a failing submission no longer ends the swarm and, when nothing passes, every member runs to its end and the rest of the chain has all their submissions. With any other chain (`vote`, `last`, `synthesize`) every member runs to its end. `stop_on_submission` and `stop_on_verified` become `None` by default, meaning "follow the chain"; an explicit `stop_on_submission` keeps today's meaning, and adding `stop_on_submission=True` keeps the old stop at the first submission for any chain that allows it. Since `final=None` is resolved against the task's result contract only inside `swarm()`, the controller learns the resolved chain at run time from a new read-only `SwarmControl.final_chain`.

## Why

The final-answer chain and the default stop were decided separately, and they do not compose ([swarm-scoring.md The final-answer chain](swarm-scoring.md#the-final-answer-chain)):

- **The default chain has nothing to choose from.** A task that passes `result=answer_result(answer_key=..., verifier=...)` and the default controller gets the chain `["verify", "vote", "first"]`, but `leaderless()` returns `submitted` at the first submission and the other members are stopped. `verify` sees one submission (and any made during grace); when it fails, `vote` votes over that one, so the answer is the first submission whatever the verifier said. `tests/test_final.py::test_leaderless_stopping_at_the_first_submission_applies_the_default_chain` pins exactly this today: member `a` submits a failing `7`, the swarm stops with reason `submitted`, the slow member is cancelled, and `vote` chooses `a`. The docs example in [Scoring, The result contract](../docs/scoring.qmd#the-result-contract) is such a task: a format-checking verifier and an answer key under the default controller.
- **An explicit chain needs a second argument.** `leaderless(final="vote")`, `final="last"`, `final="verify"` and `stop_on_verified=True` each raise `ValueError` unless `stop_on_submission=False` is also passed, because the default stop contradicts them. The user has already said what they want; the extra flag is ceremony.
- **The refusal cannot cover the default.** `leaderless()` refuses only what it sees. `final=None` is resolved against the contract inside `swarm()`, after the controller is built, so the contradiction above is accepted silently and documented instead ([Controllers, leaderless](../docs/controllers.qmd#leaderless): "that chain chooses from the submissions made before the members stopped, usually just the first").

Ransom chose to fix this by making `stop_on_submission` default on only when the chain is `first`, with an explicit `final=` seen when the controller is built and `final=None` learnt at run time through a new `SwarmControl` surface (decision: Ransom, 2026-11-10).

## Goals and non-goals

Goals:

- `leaderless()` with no stop arguments stops in the way its chain needs, for every chain `swarm()` accepts, explicit or resolved from the contract.
- An explicit `stop_on_submission` keeps today's meaning, so every call that passes it behaves as before. The one existing call whose stop changes passes `stop_on_verified=False` alone with a keying or verifying contract ([Compatibility and migration](#compatibility-and-migration)).
- The controller learns the resolved chain through `SwarmControl`, so other controllers can use it too.

Non-goals:

- A new stop rule. The defaults are the two stops `leaderless()` already has (`submitted`, `verified`) and running every member to its end; an early stop for `vote` once its plurality is decided is not designed here ([Not this design](#not-this-design)).
- The default stop for `artifact` contracts as such. A shared-artifact task without a verifier keeps the chain `first` and so the stop at the first submission, which the docs already tell such tasks to turn off.
- Any change to the chain, its modes, verification, the record, or the stop procedure.
- Recording the resolved stop in the result record. The record's `final.chain` and the ledger's `stop_reason` already say which chain applied and which stop fired.

## Current behaviour

**`leaderless()`** ([`leaderless.py`](../src/inspect_swarm/_controller/leaderless.py)) takes `stop_on_submission: bool = True`, `final: FinalSpec | None = None` and `stop_on_verified: bool = False` (`leaderless.py:9`). At construction it validates `final` with `check_final()`, turns an explicit `final` into mode names with `final_chain(final, None)` and `chain_names()`, and raises `ValueError` for a `reporter`, for an explicit chain other than `["first"]` while `stop_on_submission` is true (`leaderless.py:40`), and for `stop_on_verified` with `stop_on_submission` (`leaderless.py:44`). `final=None` is accepted with the default stop. `run` (`leaderless.py:49`) starts every member and iterates `events()`: with `stop_on_verified` it returns `verified` at a `MemberEnded` or `MemberIdle` whose verdict passed; with `stop_on_submission` it returns `submitted` at a `MemberIdle` or a submitted `MemberEnded`; when the iterator ends it returns `quiescence` if any member is idle, else `all_done`. It passes `final` to `Controller` unchanged (`leaderless.py:72`).

**Resolving the chain.** `swarm()` resolves the controller (`_swarm.py:104`), calls its `check` with the roster only (`_swarm.py:105`), and later builds the chain with `final_chain(resolved_controller.final, resolved_result)` and checks it against the contract with `check_chain()` (`_swarm.py:118`). `final_chain()` (`_final.py:134`) gives, for `final=None`, `verify` if the contract has a verifier, then `vote` if it has an answer key, then `first`; `check_chain()` refuses `verify` without a verifier and `vote` without an answer key. The chain is passed to `SwarmRun`, which keeps it as `self._chain` (`_control.py:647`) and uses it for the provisional and final answers; the record writes its names as `final.chain`.

**What a controller sees.** `SwarmControl` (`_control.py:267`) has `members`, `member()`, `start()`, `wake()`, `events()` and `choose_answer()`. Nothing on it says which chain the swarm applies, and `Controller.check` sees only the roster. A `Controller` is configuration shared by every swarm built with it, and the same object can be passed to two `swarm()` calls with different contracts, so the chain cannot be stored on it.

**Logged parameters.** A controller factory logs only the arguments it was called with: `registry_params(leaderless())` is `{}` and `registry_params(leaderless(final="first"))` is `{"final": "first"}` (checked against this branch's environment).

**Who uses which shape** (this repo's `src/`, `examples/`, `docs/` and `tests/`, and the sibling checkouts under `~/git`):

- `baseline()` builds `leaderless(final="first")` (`_swarm.py:228`).
- ts-mono's viewer end-to-end fixture `apps/inspect/e2e/fixtures/communication/generate_log.py` builds `leaderless(stop_on_submission=False)` with no result contract; it is the only sibling that imports `inspect_swarm`.
- `examples/shared_sandbox.py`, `examples/proof_swarm.py`, `examples/stop_on_proof.py`, and the docs' shared-sandbox, team@k and per-member-artifact examples pass `leaderless(stop_on_submission=False)` explicitly; none of them has a verifier or an answer key.
- [Controllers](../docs/controllers.qmd#leaderless) documents `leaderless(stop_on_submission=False, final="last")` and `leaderless(stop_on_submission=False, stop_on_verified=True)`; [swarm-api.md](swarm-api.md#controllers) shows `leaderless(final="verify", stop_on_verified=True, stop_on_submission=False)`.
- The default controller with a keying or verifying contract appears in the docs' result-contract example and in tests (`tests/test_final.py`, `tests/test_result.py`), which rely on today's stop at the first submission.
- The only verifier in the docs and examples, Scoring's `check_answer`, returns pass or fail with no `value`; graded values appear only in tests of `verify`'s ranking, the record and the analysis helpers.
- No other class implements `SwarmControl`; `SwarmRun` is the only one.

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
@swarm_controller
def leaderless(
    stop_on_submission: bool | None = None,
    *,
    final: FinalSpec | None = None,
    stop_on_verified: bool | None = None,
) -> Controller: ...
```

`None` means "the default for the chain". When `run` starts, `leaderless` resolves both stops from `swarm.final_chain` and the arguments as given, with one private helper in `leaderless.py`:

```python
# The stops leaderless applies: each argument as given, else the default for the chain
# the swarm applies.
def _stops(
    chain: Sequence[str], stop_on_submission: bool | None, stop_on_verified: bool | None
) -> tuple[bool, bool]:
    on_submission = (
        stop_on_submission
        if stop_on_submission is not None
        else list(chain) == ["first"] and stop_on_verified is not True
    )
    on_verified = (
        stop_on_verified
        if stop_on_verified is not None
        else stop_on_submission is None and chain[0] == "verify"
    )
    return on_submission, on_verified
```

The resulting stops:

| `stop_on_submission` | `stop_on_verified` | Chain | Stops at |
|---|---|---|---|
| `None` | `None` | `["first"]` | the first submission (`submitted`) |
| `None` | `None` | starts with `verify` | the first passing submission (`verified`), else every member's end |
| `None` | `None` | anything else (`vote`, `last`, `synthesize`, ...) | every member's end (`all_done` or `quiescence`) |
| `None` | `True` | any | the first passing submission |
| `None` | `False` | `["first"]` | the first submission |
| `None` | `False` | anything else | every member's end |
| `True` | `None` or `False` | `["first"]`, or resolved from `final=None` | the first submission |
| `False` | `None` or `False` | any | every member's end |
| `False` | `True` | any | the first passing submission |

`run` then behaves exactly as today with the two resolved booleans: the event loop, the stop reasons and the order of the two checks do not change.

**The default for a chain that starts with `verify`: the first passing submission** (decision: Ransom, 2026-11-11). `verify` answers with the best passing submission, so a submission that fails cannot be the answer while any passes, and stopping on it, as today, throws the verifier's work away. Two stops fit: the first passing submission, or every member's end. The first passing submission is the default because it is what `leaderless`'s default already is for `first`, applied to the verified chain: stop as soon as the chain's leading mode has its answer, and save the rest of the budget (decision: Ransom, 2026-10-21, for the stop at the first submission). When nothing passes, no stop fires, so every member runs to its end and `vote` and `first` choose from all their submissions, which is what the fall-through chain is for. A verifier that grades passes with `Verdict.value`, where a later pass could rank higher, is the case it gives up; the only verifier in the docs and examples is pass or fail, and such a task passes `stop_on_verified=False` to run every member to its end.

**The default for any other chain: every member's end**, or `quiescence` when persistent members are left idle (decision: Ransom, 2026-11-11). `vote` needs votes, so a stop at the first submission makes it `first`. Stopping when a plurality can no longer change would need the controller to compare answer keys, which are model text and never reach a controller, and a persistent member can change its vote by answering again; neither stop exists today. `last` and `synthesize` need every submission by definition. A chain led by `vote` runs every member to its end even when `verify` comes later in it, since the leading mode decides.

**Explicit arguments win.** An explicit `stop_on_submission` keeps its meaning in both directions: `True` stops at the first submission, including under a resolved `verify` or `vote` chain, and `False` runs every member to its end unless `stop_on_verified=True`; neither turns on the `verified` stop by default. An explicit `stop_on_verified=True` turns the default `stop_on_submission` off, since stopping on any submission would make it moot. An explicit `stop_on_verified=False` leaves `stop_on_submission` to the chain: on only for `first`, so under a keying or verifying contract it runs every member to its end, where today's `True` default stopped at the first submission; adding `stop_on_submission=True` keeps that old stop.

**Refusals at construction**, where the arguments are visible:

- a `reporter` in `final` (as today);
- `stop_on_submission=True` with an explicit chain other than `["first"]` (as today, now only when `True` is passed);
- `stop_on_submission=True` with `stop_on_verified=True` (as today, now only when both are passed).

Everything else is accepted: `leaderless(final="vote")`, `leaderless(final="verify")` and `leaderless(stop_on_verified=True)` no longer need `stop_on_submission=False`. `stop_on_submission=True` with `final=None` stays accepted and stops at the first submission whatever the contract, as today; it is an explicit request. Nothing is refused at run time: `check_chain()` has already refused a chain the contract cannot support, so a chain led by `verify` always has a verifier, and `stop_on_verified=True` without a verifier never fires, as today.

**Docstring.** The `leaderless` docstring's summary becomes "Start every member with the swarm's input; stop when the answer chain has its answer, or when none is still working", its body states the three defaults above, and the `Args` give `None` as "the default for the chain". `Controller`'s `final` argument notes that `run` can read the resolved chain from `SwarmControl.final_chain`.

### What else is unchanged

`baseline()` builds `leaderless(final="first")`, whose chain is `["first"]`, so it stops at its member's submission as today. The stop procedure, grace, verification through the stop, the provisional answer and finalisation are as in [swarm-scoring.md](swarm-scoring.md#the-final-answer) and [swarm-limits.md](swarm-limits.md#stopping). A persistent member's `MemberIdle` still counts as a submission for the `submitted` stop and carries its verdict for the `verified` one ([swarm-persistent-members.md](swarm-persistent-members.md#the-swarms-answer)); `leaderless` still never wakes a member, so under a `vote` chain a persistent member answers once and the swarm ends at `quiescence`.

## Alternatives considered

**Every member's end for a `verify` chain.** Simpler to explain (only `first` stops early) and right for a graded verifier, but a pass/fail verifier, the only kind the docs and examples use, gains nothing from the members that keep working after the first pass, and the swarm spends up to k times the budget for the same answer. A task that wants it passes `stop_on_verified=False`.

**Pass the chain to the controller when the swarm is built** (a hook such as `Controller.check(roster, chain)`). The chain is known in `swarm()`, but a `Controller` is shared configuration, possibly passed to swarms with different contracts, so the hook would have to return a per-swarm controller or the result would have to be stored per swarm somewhere else. `run` already receives a per-sample `SwarmControl`; a property there needs no new lifecycle.

**Pass the chain as an argument to `run`.** It would change the signature of every controller's `run`, a breaking change for an additive need.

**Expose the result contract** (`SwarmControl.result`, or flags for a verifier and an answer key). More general, but the stop depends on the chain, not the contract: an explicit `final` overrides the contract's default, and the contract's callables do not belong in a controller's hands. The chain's names are the narrowest fact that decides the stop.

**One stop parameter** (`stop: Literal["submission", "verified", "end"] | None`). Cleaner than two booleans whose combinations need a table, but it renames a parameter every explicit caller and logged eval uses. The two booleans with `None` defaults keep every existing call and log valid.

**Keep refusing and only document it** (the rule before this design). It leaves the default chain without submissions to choose from, which is the problem.

## Compatibility and migration

- **Behaviour.** A swarm with the default controller and a result contract that has a verifier now runs until a submission passes, or until every member ends; one with an answer key and no verifier runs every member to its end. Both can spend more than before (up to every member's budget) and give a different answer, since the chain now sees more submissions. A swarm with no contract, a contract with neither (including `artifact_result()` with only `member_artifacts`), or any explicit `stop_on_submission` behaves as before. The stop reason `verified`, `all_done` or `quiescence` replaces `submitted` in those runs' ledgers. A task that wants the old stop adds `stop_on_submission=True`.
- **`stop_on_verified=False` alone.** Today it leaves `stop_on_submission` at `True`, so with a keying or verifying contract the swarm stops at the first submission. Under this design the chain is not `first`, so the swarm runs every member to its end and can choose a different answer. No call in this repo or its siblings passes it alone; such a call adds `stop_on_submission=True` to keep the old stop.
- **Accepted calls.** Calls that raised `ValueError` before are now accepted: an explicit non-`first` chain, or `stop_on_verified=True`, without `stop_on_submission`. No call that worked before is refused.
- **Logged parameters and task identity.** The default call still logs `{}`, so a task built with `leaderless()` has the same `task_identifier()` before and after, while its stop changes when its contract keys or verifies. An eval set retried across the upgrade could mix the two stops under one identifier; as for any behaviour change, arms in one eval set run one installed inspect_swarm ([swarm-scoring.md Compatibility](swarm-scoring.md#compatibility)). A log with an explicit `stop_on_submission` rebuilds to the same stops as before; one with `stop_on_verified` alone (`{"stop_on_verified": false}`) rebuilds with the new chain default, as above, and so do logs with neither. An explicit `None` logs as `null` and rebuilds as the default.
- **Public API.** The controller interfaces are experimental and may change in any release ([swarm-api.md](swarm-api.md)). `SwarmControl` gains `final_chain`. Code that calls `SwarmControl` is unaffected; a class of its own that implements the protocol would need the property to type-check, and there is none in this repo or its siblings. ts-mono's end-to-end fixture passes `stop_on_submission=False` with no contract, so it resolves to the same stops (both off) and needs no change. The parameter types widen from `bool` to `bool | None`.
- **Stored formats.** None: the record, its `final.chain`, the evidence events and the ledger fields are unchanged. No viewer type is generated for any of them.

## Security

The new property returns mode names fixed by the eval author's code or by `final_chain()`'s defaults; no model output, tool argument or member text reaches it, and it hides a `synthesize` mode's model and prompt. The stop decision reads only those names and the existing `VerdictSummary.passed`, which is already the controller's ([swarm-scoring.md Security](swarm-scoring.md#security)). Running more members to their end under a verifying or voting contract adds no exposure of its own: every member runs under the swarm's caps and the sample's limits as it would with `stop_on_submission=False` today.

## Testing

All in-process with scripted models (no network, Docker or provider); the AnyIO tests run on asyncio and trio through `tests/conftest.py`.

- `tests/test_final.py`:
  - The stop rule, table-driven over the table above, through `leaderless`'s observable behaviour in a run rather than the private helper: members that submit in a fixed order with `_team()` (a failing `7` first, then a passing `42`, then a late member), under `Budget(time=...)` as `tests/test_controller.py`'s stop test uses so no row can hang, asserting the stop reason, the members' statuses and the chosen member. Rows: no contract (`submitted`, `first`); verifier and key with defaults (`verified`, the passing member, the late member ended by the stop); key only with defaults (`all_done`, `vote` over every member); verifier and key with defaults and nothing passing (`all_done`, fall-through to `vote`); explicit `stop_on_submission=True` with a verifying contract (`submitted`, as today); explicit `stop_on_submission=False` with a verifying contract (`all_done`, no `verified` stop); `stop_on_verified=True` alone (`verified`); `stop_on_verified=False` alone with a verifying contract (`all_done`); `final="vote"` with an answer key (`all_done`).
  - `test_leaderless_stopping_at_the_first_submission_applies_the_default_chain` changes to pass `stop_on_submission=True` explicitly, keeping its check that an explicit stop still applies the default chain to what was submitted.
  - `test_a_chain_the_swarm_cannot_apply_is_refused`: the `chain-stopping-at-first` and `stop-on-verified-stopping-at-first` cases pass `stop_on_submission=True` explicitly; new cases assert that `leaderless(final="vote")`, `leaderless(final=["verify", "first"])` and `leaderless(stop_on_verified=True)` are accepted.
  - A persistent member under the default `vote` chain answers once and the swarm returns `quiescence`; under a default `verify` chain a passing `MemberIdle` returns `verified` (the existing persistent `stop_on_verified` test, with the explicit arguments dropped).
- `tests/test_controller.py`: a custom controller reads `swarm.final_chain` and sees `("first",)` with no contract, `("verify", "vote", "first")` with both contract parts, and its own explicit chain; the value is a tuple. `test_leaderless_stops_at_the_first_submission_unless_told_not_to` has no contract, so it passes unchanged and pins the `first` default.
- `tests/test_result.py`: the tests that build a swarm with a verifier or key and the default controller (around lines 214, 406, 727, 768, 1688 and 1749) are reviewed; those that depend on the stop at the first submission pass `stop_on_submission=True`, the rest stay as they are.
- `tests/test_registry.py`: `registry_params(leaderless())` stays `{}`, and a controller logged with explicit `None` arguments rebuilds with the default stops.

## Implementation plan

One PR (`feat:`), since the property and the controller change are one user-visible behaviour:

1. `src/inspect_swarm/_controller/_control.py`: `final_chain` on `SwarmControl` and `SwarmRun`.
2. `src/inspect_swarm/_controller/leaderless.py`: the `None` defaults, `_stops()`, the construction refusals narrowed to explicit `True`, `run` resolving the stops from `swarm.final_chain`, the docstring.
3. `src/inspect_swarm/_controller/_controller.py`: the `final` docstring's note on `final_chain`.
4. Tests as above: `tests/test_final.py`, `tests/test_controller.py`, `tests/test_result.py`, and any other test the full suite shows relying on the default stop under a keying or verifying contract.
5. Docs: [Controllers, leaderless](../docs/controllers.qmd#leaderless) (the default stop per chain, the table in prose, `stop_on_submission=False` still for shared artifacts, team@k and agents whose any return is a submission; the refusals; `final_chain` under Writing a controller), [Scoring](../docs/scoring.qmd) (the `last` line, the result-contract example's stop, Scoring what members leave), and the reference page picks up the docstrings.
6. Designs: [swarm-scoring.md](swarm-scoring.md#the-final-answer-chain) and [swarm-api.md](swarm-api.md#controllers) describe the rule and link here; when the change is built, an as-built pass folds this document into swarm-scoring.md.

## Not this design

- **An early stop for `vote`** once no outstanding member can change the plurality: it would need answer-key equality to reach the controller (keys are model text and do not today) and a rule for persistent members, who can change their vote.
- **A default stop for `artifact` contracts**: a shared-artifact task scored on the environment arguably wants every member's end by default whatever its chain, which would need the contract's kind, not just the chain, to reach the controller.
- **Recording the resolved stops** (for instance in the ledger beside `stop_reason`), so a log says which stop was armed even when none fired.
