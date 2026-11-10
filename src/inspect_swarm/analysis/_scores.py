import math
import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import combinations
from typing import Any, cast, get_args

from inspect_ai.analysis._dataframe.evals.extract import eval_log_location
from inspect_ai.event import ScoreEvent
from inspect_ai.log import EvalLog, EvalSample, read_eval_log
from inspect_ai.model import ModelUsage
from inspect_ai.scorer import Score, value_to_float

from .._final import FinalName, answer
from .._record import FinalRecord, MemberResult, swarm_result
from .._result import ResultKind, Verdict

# The modes select_at_k() applies: those with an analogue over independent attempts.
# `synthesize` makes a new model call, so it has none.
_SELECT_MODES = tuple(name for name in get_args(FinalName) if name != "synthesize")


@dataclass(frozen=True)
class AttemptRow:
    """One sample of one log, as `attempt_rows()` reads it: the swarm's result record and the sample's scores."""

    log: str
    """The log's location as a local path, as `samples_df()` gives it in its `log` column."""

    id: str
    """The sample's id as text, as `samples_df()` gives it in its `id` column, so an integer id `1` is `"1"`."""

    epoch: int
    """The sample's epoch."""

    task_args: dict[str, Any]
    """The task's arguments (`eval.task_args`), which label the arm."""

    result_kind: ResultKind | None
    """The result contract's kind, from the record; `None` without a contract or a record."""

    final_value: float | None
    """The task scorer's value, converted with Inspect's default `value_to_float()`; `None` when the sample has no such score or it is NaN (unscored)."""

    final: FinalRecord | None
    """The record's final answer: its mode, member and reason. `None` for a sample with no record, such as a plain agent's."""

    final_verdict: Verdict | None
    """For an `artifact` task with a verifier, the verifier's verdict on the environment after the members stopped; `None` otherwise."""

    best_member: float | None
    """The member scorer's `best_member`; `None` when the sample has no such score or it is NaN."""

    mean_member: float | None
    """The member scorer's `mean_member`; `None` when the sample has no such score or it is NaN."""

    complete: bool | None
    """The member scorer's `complete`: no started member was unavailable. `None` when the sample has no member score."""

    members: list[MemberResult]
    """The record's members, in roster order, with their states, statuses, answer keys, verdicts, end times and whether they submitted. Empty for a sample with no record."""

    members_total: int | None
    """The members in the roster; `None` for a sample with no record."""

    members_started: int | None
    """The members that started (state other than `pending`); `None` for a sample with no record."""

    sample_total_usage: dict[str, ModelUsage]
    """The sample's usage by model (`model_usage`), scorer calls included."""

    target_assisted: bool
    """An agent consulted the task's scorers during the solve (an intermediate score event, as `react(attempts=...)` makes), so it was given feedback that uses the target. In a swarm that feedback can reach its peers. Report these samples separately."""

    no_solve_usage: bool
    """The row has no usage for the solve alone, apart from scoring, so `sample_total_usage` includes the scorers' calls. Leave such rows out of solve-cost comparisons. The swarm does not record solve usage yet, so every row has it."""


def attempt_rows(
    logs: Sequence[str | EvalLog], *, scorer: str, member_scorer: str
) -> list[AttemptRow]:
    """Read one row per sample and epoch from logs, with what the swarm recorded and the sample's scores.

    Each row holds the sample's result record (`swarm_result(sample)`): the members' states, statuses, answer keys, verdicts, times and submissions, the final answer and the final verdict. It also holds the task scorer's value, the member scorer's `best_member`, `mean_member` and `complete`, the sample's usage, and whether an agent consulted the task's scorers during the solve.

    A sample with no swarm, a plain agent, gives a row with no record: `final` is `None` and `members` empty. `select_at_k()` leaves it out.

    Args:
        logs: Log files, or logs read with their samples (`read_eval_log()`). A log file is read whole, with its samples' events, where the record is.
        scorer: The name of the task's own scorer, as in `sample.scores`.
        member_scorer: The name of the task's member scorer, the factory that returns `member_scores()`.

    Returns:
        The rows, by log, then in each log's sample order.

    Raises:
        ValueError: A log was read without its samples; no scored sample in a log has `scorer` or `member_scorer`; the task scorer's value is not a single value; the member scorer's value is not `member_scores()`'s; or `swarm_result()` refuses a sample's record.
    """
    rows: list[AttemptRow] = []
    for source in logs:
        log = read_eval_log(source) if isinstance(source, str) else source
        if log.samples is None:
            raise ValueError(
                f"{_name(log)}: the log was read without its samples. Read it with read_eval_log(), or pass its path."
            )
        _check_scorer(log, scorer)
        _check_scorer(log, member_scorer)
        rows.extend(_row(log, sample, scorer, member_scorer) for sample in log.samples)
    return rows


def select_at_k(
    rows: Sequence[AttemptRow],
    k: int,
    chain: str | Sequence[str],
    *,
    empty_value: float = 0.0,
    max_subsets: int = 10_000,
    seed: int = 0,
) -> float:
    """The expected value of applying the swarm's final-answer chain to k independent attempts, for one sample.

    `rows` are one sample's epochs from a `baseline()` arm, as `attempt_rows()` reads them. Each is one attempt. Its pool is the rows with a result record and a task score. For each subset of k attempts in the pool, the swarm's own selector applies `chain` to the subset, ordering attempts by their swarm time, then by epoch in place of roster order. The subset's value is the chosen attempt's task score. select@k is the mean over the subsets: every subset when there are at most `max_subsets`, otherwise `max_subsets` subsets sampled with `seed`.

    For an `answer` task, or a record with no result contract, only attempts that submitted are chosen, as the swarm only chooses submissions. A subset where the chain chooses nothing has the value `empty_value`, as the swarm returns an empty answer.

    For an `artifact` task, every attempt left an environment, so every attempt can be chosen, submitted or not. `verify` reads each attempt's `final_verdict`, the verifier on its environment after it stopped, not the verdict taken when it submitted. `vote` reads the answer keys of the attempts that submitted. `first` and `last` read the end times. `first` is added to the end of the chain when it is not already last, so every subset chooses an environment.

    Nothing here sees the target: the chain reads the record's times, answer keys and verdicts, and the task scores are read only for the chosen attempt.

    Args:
        rows: One sample's rows from a `baseline()` arm.
        k: The number of attempts in each subset.
        chain: The modes to apply in order: `verify`, `vote`, `first` or `last`, or one of them. `synthesize` makes a new model call, so it has no analogue over attempts.
        empty_value: The value of a subset where the chain chooses nothing, for an `answer` task.
        max_subsets: The most subsets to enumerate before sampling them.
        seed: The seed for sampling subsets.

    Returns:
        select@k for the sample, or NaN when the pool has fewer than k attempts.

    Raises:
        ValueError: `k` or `max_subsets` is below 1; the chain is empty or has a mode select@k cannot apply; the rows are of more than one sample or repeat an epoch; a row's record has more than one member, or a persistent one, so it is not a `baseline()` attempt; or the rows' records disagree on `result_kind`.
    """
    if k < 1:
        raise ValueError(f"select_at_k(): k must be at least 1, got {k}.")
    if max_subsets < 1:
        raise ValueError(
            f"select_at_k(): max_subsets must be at least 1, got {max_subsets}."
        )
    names: list[FinalName] = _chain(chain)
    kind = _kind(rows)
    _check_rows(rows)
    pool = sorted(
        (r for r in rows if r.final is not None and r.final_value is not None),
        key=lambda r: r.epoch,
    )
    if len(pool) < k:
        return math.nan
    if kind == "artifact":
        attempts = [_artifact_attempt(r) for r in pool]
        if names[-1] != "first":
            names = [*names, "first"]
    else:
        attempts = [_answer_attempt(r) for r in pool]
    values = {a.name: r.final_value for a, r in zip(attempts, pool, strict=True)}

    def value(subset: Iterable[int]) -> float:
        chosen = answer([attempts[i] for i in subset], names, None, provisional=False)
        if chosen.member is None:
            return empty_value
        selected = values[chosen.member]
        assert selected is not None
        return selected

    n = len(pool)
    if math.comb(n, k) <= max_subsets:
        subsets: Iterable[Iterable[int]] = combinations(range(n), k)
    else:
        rng = random.Random(seed)
        subsets = (sorted(rng.sample(range(n), k)) for _ in range(max_subsets))
    total = 0.0
    count = 0
    for subset in subsets:
        total += value(subset)
        count += 1
    return total / count


def _row(
    log: EvalLog, sample: EvalSample, scorer: str, member_scorer: str
) -> AttemptRow:
    record = swarm_result(sample)
    scores = sample.scores or {}
    final_value = _task_value(scores.get(scorer), scorer, sample)
    best, average, complete = _member_values(
        scores.get(member_scorer), member_scorer, sample
    )
    return AttemptRow(
        # the keys samples_df() gives, so that rows join with its rows
        log=eval_log_location(log),
        id=str(sample.id),
        epoch=sample.epoch,
        task_args=dict(log.eval.task_args),
        result_kind=record.kind if record is not None else None,
        final_value=final_value,
        final=record.final if record is not None else None,
        final_verdict=record.final_verdict if record is not None else None,
        best_member=best,
        mean_member=average,
        complete=complete,
        members=list(record.members) if record is not None else [],
        members_total=len(record.members) if record is not None else None,
        members_started=sum(m.state != "pending" for m in record.members)
        if record is not None
        else None,
        sample_total_usage=dict(sample.model_usage),
        target_assisted=any(
            isinstance(e, ScoreEvent) and e.intermediate for e in sample.events
        ),
        # the swarm does not record solve usage apart from scoring yet
        no_solve_usage=True,
    )


# A misspelled scorer name would otherwise give every row None.
def _check_scorer(log: EvalLog, name: str) -> None:
    scored = [s.scores for s in log.samples or [] if s.scores]
    if scored and not any(name in scores for scores in scored):
        found = sorted({n for scores in scored for n in scores})
        raise ValueError(
            f"{_name(log)}: no sample has a score named {name!r}; its scores are named {', '.join(map(repr, found))}."
        )


def _task_value(score: Score | None, name: str, sample: EvalSample) -> float | None:
    if score is None:
        return None
    if not isinstance(score.value, str | int | float | bool):
        raise ValueError(
            f"Sample {sample.id!r} (epoch {sample.epoch}): the score {name!r} is not a single value, so it has no task value; name the task's own scorer."
        )
    return _valued(value_to_float()(score.value))


def _member_values(
    score: Score | None, name: str, sample: EvalSample
) -> tuple[float | None, float | None, bool | None]:
    if score is None:
        return None, None, None
    value = score.value
    complete = (score.metadata or {}).get("complete")
    if (
        not isinstance(value, dict)
        or not {"best_member", "mean_member"} <= value.keys()
        or not isinstance(complete, bool)
    ):
        raise ValueError(
            f"Sample {sample.id!r} (epoch {sample.epoch}): the score {name!r} is not one member_scores() gives; name the task's member scorer."
        )
    return (
        _valued(value["best_member"]),
        _valued(value["mean_member"]),
        complete,
    )


def _valued(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return None if math.isnan(value) else float(value)


def _chain(chain: str | Sequence[str]) -> list[FinalName]:
    names = [chain] if isinstance(chain, str) else list(chain)
    if not names:
        raise ValueError("select_at_k(): the chain is empty; give at least one mode.")
    for name in names:
        if name not in _SELECT_MODES:
            modes = ", ".join(repr(mode) for mode in _SELECT_MODES)
            raise ValueError(
                f"select_at_k(): the chain has {name!r}, which select@k cannot apply; use {modes}."
            )
    return cast(list[FinalName], names)


def _check_rows(rows: Sequence[AttemptRow]) -> None:
    ids = {r.id for r in rows}
    if len(ids) > 1:
        raise ValueError(
            f"select_at_k(): the rows are of more than one sample ({', '.join(sorted(map(repr, ids)))}); pass one sample's rows."
        )
    epochs = [r.epoch for r in rows]
    if len(set(epochs)) < len(epochs):
        raise ValueError(
            "select_at_k(): the rows repeat an epoch; pass one arm's rows for the sample."
        )
    for r in rows:
        if r.final is None:
            continue
        if len(r.members) != 1:
            raise ValueError(
                f"select_at_k(): {r.log}, sample {r.id!r} (epoch {r.epoch}), has {len(r.members)} members; select@k reads the attempts of a baseline() arm, which has one."
            )
        if r.members[0].persistent:
            raise ValueError(
                f"select_at_k(): {r.log}, sample {r.id!r} (epoch {r.epoch}), is a persistent member's; select@k reads attempts that each give one submission."
            )


def _kind(rows: Sequence[AttemptRow]) -> ResultKind | None:
    kinds: dict[ResultKind | None, list[str]] = {}
    for r in rows:
        if r.final is not None:
            kinds.setdefault(r.result_kind, []).append(r.log)
    if len(kinds) > 1:
        found = "; ".join(
            f"{kind!r} in {', '.join(sorted(set(logs)))}"
            for kind, logs in kinds.items()
        )
        raise ValueError(f"select_at_k(): the rows disagree on result_kind: {found}.")
    return next(iter(kinds), None)


# An attempt as the swarm's selector reads a member: the attempt's member under a name
# unique in the pool, since every baseline() member has the same roster name.
def _answer_attempt(row: AttemptRow) -> MemberResult:
    return row.members[0].model_copy(update={"name": _attempt_name(row)})


# An artifact attempt: chosen whether or not it submitted, verified on its environment
# after it stopped. One cut off before it ended has no end time, so it orders last.
def _artifact_attempt(row: AttemptRow) -> MemberResult:
    m = row.members[0]
    return m.model_copy(
        update={
            "name": _attempt_name(row),
            "submitted": True,
            "time": m.time if m.time is not None else math.inf,
            "answer_key": m.answer_key if m.submitted else None,
            "verdict": row.final_verdict,
        }
    )


def _attempt_name(row: AttemptRow) -> str:
    return f"epoch-{row.epoch}"


def _name(log: EvalLog) -> str:
    return log.location or f"the log of task {log.eval.task!r}"
