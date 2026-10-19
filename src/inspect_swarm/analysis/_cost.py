import math
from collections.abc import Sequence
from typing import Any

from inspect_ai import task_identifier
from inspect_ai.analysis._dataframe.util import LogPaths, resolve_logs
from inspect_ai.log import EvalLog, EvalSample, read_eval_log
from inspect_ai.model import ModelUsage
from inspect_ai.scorer import value_to_float
from pydantic import BaseModel

from .._evidence import LEDGER_KEY
from .._ledger import (
    _TOKEN_FIELDS,  # pyright: ignore[reportPrivateUsage]
    SwarmLedger,
)

# the ledger version these helpers read
_LEDGER_VERSION = 1

# sample fields the helpers never read, skipped when a log is read from a file
_EXCLUDE_FIELDS = {"events", "messages", "attachments"}

_to_float = value_to_float()


class SampleCost(BaseModel):
    """One sample's score and realized cost."""

    log: str
    """Location of the log the sample was read from."""

    arm: str
    """The arm: the log's task identifier (`inspect_ai.task_identifier()`)."""

    task: str
    """Task name."""

    task_args: dict[str, Any]
    """Task arguments."""

    model: str
    """The eval's model."""

    id: str | int
    """Sample id."""

    epoch: int
    """Sample epoch."""

    score: float | None
    """The scorer's value as a float, or `None` when the sample has no score from it."""

    cost: float
    """Realized cost in dollars: the priced cost in the sample's `model_usage`, scorer calls included."""

    tokens: int
    """Total tokens in the sample's `model_usage`."""

    complete: bool
    """`cost` is the sample's whole spend: the swarm's ledger has `total_complete`, and every model in `model_usage` is priced. Always `False` for a sample with no ledger, since nothing judged its spend."""

    members: int | None
    """Members in the swarm's roster, or `None` for a sample with no ledger."""

    resumed: bool
    """The sample resumed from a checkpoint, so its swarm restarted its caps from zero."""

    cost_limit: float | None
    """The eval's sample cost limit: the condition, not the spend."""

    swarm_cost_cap: float | None
    """The swarm's cost cap from the ledger, derived or explicit."""


class ArmCost(BaseModel):
    """One arm's mean score and mean realized cost over its scored samples."""

    arm: str
    """The arm's task identifier."""

    task: str
    """Task name."""

    task_args: dict[str, Any]
    """Task arguments."""

    model: str
    """The eval's model."""

    members: int | None
    """Members in the swarm's roster, or `None` when no sample has a ledger."""

    samples: int
    """Samples in the means: scored and not resumed."""

    unscored: int
    """Samples left out because they have no score from the scorer."""

    resumed: int
    """Scored samples left out because they resumed from a checkpoint."""

    score: float | None
    """Mean score, or `None` with no samples in the means."""

    cost: float | None
    """Mean realized cost in dollars, or `None` with no samples in the means."""

    complete: bool
    """Every sample in the means has a complete cost. When `False`, `cost` is a lower bound."""

    cost_limit: float | None
    """The eval's sample cost limit."""

    swarm_cost_cap: float | None
    """The swarm's cost cap from the ledger."""


class LambdaFit(BaseModel):
    """A fit of the parallelisability exponent λ over arms that vary agent count and per-agent cost.

    The fit is `score = intercept + slope * (ln(cost / members) + lambda_ * ln(members))`: `members` agents with per-agent cost `b` score as one agent with `members ** lambda_ * b`.
    """

    lambda_: float
    """The parallelisability exponent λ."""

    slope: float
    """Score per unit of log per-agent cost."""

    intercept: float
    """Score at a per-agent cost of one dollar for one agent."""

    arms: int
    """Arms in the fit."""

    complete: bool
    """Every arm's cost is complete. When `False`, some costs are lower bounds."""


def sample_costs(
    logs: LogPaths | EvalLog | Sequence[EvalLog], scorer: str | None = None
) -> list[SampleCost]:
    """Read each sample's score and realized cost from a set of logs.

    The cost is the sample's `model_usage`, which every arm has, scorer calls included. A swarm's ledger says whether that cost is complete, and gives its roster size and cap.

    Args:
        logs: Log files, directories of logs, or logs already read.
        scorer: The scorer whose value is the score. `None` uses a sample's only score. Its values must be scalars (numbers, booleans, or strings such as `"C"` and `"I"`), converted by `value_to_float()`.

    Returns:
        One row per sample, in log order.

    Raises:
        ValueError: When `scorer` is `None` and a sample has more than one score, a score value is a list or dict, or a ledger has a version these helpers do not read.
    """
    rows: list[SampleCost] = []
    for entry in resolve_logs(logs):
        log = (
            read_eval_log(entry, exclude_fields=_EXCLUDE_FIELDS)
            if isinstance(entry, str)
            else entry
        )
        arm = task_identifier(log, None)
        for sample in log.samples or []:
            rows.append(_sample_cost(log, arm, sample, scorer))
    return rows


def arm_costs(samples: Sequence[SampleCost]) -> list[ArmCost]:
    """Compare arms on realized cost: each arm's mean score and mean realized cost.

    Samples with no score, and samples that resumed from a checkpoint, are left out of the means and counted.

    Args:
        samples: Rows from `sample_costs()`.

    Returns:
        One row per arm, in the order arms first appear in `samples`.

    Raises:
        ValueError: When a sample appears twice in one arm (two logs of the same arm), or an arm's ledgers disagree on the roster size.
    """
    arms: dict[str, list[SampleCost]] = {}
    for row in samples:
        arms.setdefault(row.arm, []).append(row)
    return [_arm_cost(rows) for rows in arms.values()]


def fit_lambda(arms: Sequence[ArmCost]) -> LambdaFit:
    """Fit λ over arms that vary agent count and per-agent cost (Ord's method).

    Fits `score = intercept + slope * ln(cost / members) + agents * ln(members)` to the arms' mean scores and mean realized costs by least squares, and returns `lambda_ = agents / slope`: the slope of score against log total cost as members are added at a fixed per-agent cost, over the slope as one member's cost grows.

    Args:
        arms: Rows from `arm_costs()`, each a swarm (a swarm of one for the single-agent arm) with a scored mean.

    Returns:
        The fit.

    Raises:
        ValueError: When an arm has no ledger, no samples in its means or no positive cost, the arms do not vary both agent count and per-agent cost independently, or the score does not change with per-agent cost.
    """
    points: list[tuple[float, float, float]] = []
    for arm in arms:
        if arm.members is None:
            raise ValueError(
                f"Arm {arm.arm} has no swarm ledger, so its agent count is unknown. Run the single-agent arm as a swarm of one."
            )
        if arm.score is None or arm.cost is None:
            raise ValueError(f"Arm {arm.arm} has no scored samples.")
        if arm.cost <= 0:
            raise ValueError(
                f"Arm {arm.arm} has a realized cost of {arm.cost}; the fit needs a positive cost."
            )
        members = math.log(arm.members)
        points.append((math.log(arm.cost) - members, members, arm.score))

    # least squares on the centred points: two regressors and an intercept
    n = len(points)
    if n < 3:
        raise ValueError(f"fit_lambda() needs at least three arms, got {n}.")
    mean_x = sum(p[0] for p in points) / n
    mean_z = sum(p[1] for p in points) / n
    mean_y = sum(p[2] for p in points) / n
    sxx = sum((p[0] - mean_x) ** 2 for p in points)
    szz = sum((p[1] - mean_z) ** 2 for p in points)
    sxz = sum((p[0] - mean_x) * (p[1] - mean_z) for p in points)
    sxy = sum((p[0] - mean_x) * (p[2] - mean_y) for p in points)
    szy = sum((p[1] - mean_z) * (p[2] - mean_y) for p in points)
    det = sxx * szz - sxz * sxz
    if det <= 1e-12 * sxx * szz:
        raise ValueError(
            "fit_lambda() needs arms that vary agent count and per-agent cost independently: at least three arms, with more than one agent count, not all on one line in log per-agent cost and log agent count."
        )
    slope = (sxy * szz - szy * sxz) / det
    agents = (szy * sxx - sxy * sxz) / det
    if slope == 0:
        raise ValueError(
            "The score does not change with per-agent cost, so λ is undefined."
        )
    return LambdaFit(
        lambda_=agents / slope,
        slope=slope,
        intercept=mean_y - slope * mean_x - agents * mean_z,
        arms=n,
        complete=all(arm.complete for arm in arms),
    )


def _sample_cost(
    log: EvalLog, arm: str, sample: EvalSample, scorer: str | None
) -> SampleCost:
    ledger = _ledger(log, sample)
    priced = all(_priced(usage) for usage in sample.model_usage.values())
    return SampleCost(
        log=log.location,
        arm=arm,
        task=log.eval.task,
        task_args=log.eval.task_args,
        model=log.eval.model,
        id=sample.id,
        epoch=sample.epoch,
        score=_score(log, sample, scorer),
        cost=sum(u.total_cost or 0.0 for u in sample.model_usage.values()),
        tokens=sum(u.total_tokens for u in sample.model_usage.values()),
        complete=ledger is not None and ledger.total_complete and priced,
        members=len(ledger.members) if ledger is not None else None,
        resumed=ledger is not None and ledger.resumed,
        cost_limit=log.eval.config.cost_limit,
        swarm_cost_cap=ledger.caps.get("cost") if ledger is not None else None,
    )


def _ledger(log: EvalLog, sample: EvalSample) -> SwarmLedger | None:
    data = sample.store.get(LEDGER_KEY)
    if data is None:
        return None
    version = data.get("version")
    if version != _LEDGER_VERSION:
        raise ValueError(
            f"Sample {sample.id} (epoch {sample.epoch}) in {log.location} has a ledger of version {version}; this inspect_swarm reads version {_LEDGER_VERSION}."
        )
    return SwarmLedger.model_validate(data)


def _priced(usage: ModelUsage) -> bool:
    # a model with tokens and no cost is unpriced; a provider may report component
    # counts with a total of zero
    return usage.total_cost is not None or not any(
        getattr(usage, f) for f in _TOKEN_FIELDS
    )


def _score(log: EvalLog, sample: EvalSample, scorer: str | None) -> float | None:
    scores = sample.scores or {}
    if scorer is None:
        if len(scores) > 1:
            raise ValueError(
                f"Sample {sample.id} (epoch {sample.epoch}) in {log.location} has scores from {sorted(scores)}; name the one to use with `scorer`."
            )
        score = next(iter(scores.values()), None)
    else:
        score = scores.get(scorer)
    if score is None:
        return None
    value = score.value
    if not isinstance(value, str | int | float | bool):
        raise ValueError(
            f"Sample {sample.id} (epoch {sample.epoch}) in {log.location} has a {type(value).__name__} score value; the helpers read scalar values only."
        )
    return _to_float(value)


def _arm_cost(rows: list[SampleCost]) -> ArmCost:
    first = rows[0]
    seen: set[tuple[str | int, int]] = set()
    for row in rows:
        key = (row.id, row.epoch)
        if key in seen:
            raise ValueError(
                f"Sample {row.id} (epoch {row.epoch}) appears more than once in arm {row.arm}; pass one log per arm."
            )
        seen.add(key)
    rosters = {row.members for row in rows if row.members is not None}
    if len(rosters) > 1:
        raise ValueError(
            f"Arm {first.arm} has ledgers with different roster sizes: {sorted(rosters)}."
        )
    scored = [row for row in rows if row.score is not None]
    included = [row for row in scored if not row.resumed]
    caps = [row.swarm_cost_cap for row in rows if row.swarm_cost_cap is not None]
    return ArmCost(
        arm=first.arm,
        task=first.task,
        task_args=first.task_args,
        model=first.model,
        members=next(iter(rosters), None),
        samples=len(included),
        unscored=len(rows) - len(scored),
        resumed=len(scored) - len(included),
        score=_mean([row.score for row in included if row.score is not None]),
        cost=_mean([row.cost for row in included]),
        complete=bool(included) and all(row.complete for row in included),
        cost_limit=first.cost_limit,
        swarm_cost_cap=caps[0] if caps else None,
    )


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None
