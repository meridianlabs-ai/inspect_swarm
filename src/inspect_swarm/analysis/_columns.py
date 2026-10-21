from inspect_ai.analysis import Column, SampleColumn
from inspect_ai.log import EvalSample
from inspect_ai.model import ModelUsage
from pydantic import ValidationError

from .._evidence import LEDGER_KEY
from .._ledger import (
    _TOKEN_FIELDS,  # pyright: ignore[reportPrivateUsage]
    Calls,
    SwarmLedger,
)


def swarm_ledger(sample: EvalSample) -> SwarmLedger | None:
    """Read the swarm's ledger from a sample.

    Args:
        sample: A sample read from a log with its store, for example from `read_eval_log()`. A sample that runs two swarms keeps only the last one's ledger.

    Returns:
        The ledger, or `None` for a sample with no ledger.

    Raises:
        ValueError: The sample has a ledger this inspect_swarm cannot read, such as a version other than 1.
    """
    data = sample.store.get(LEDGER_KEY)
    if data is None:
        return None
    try:
        return SwarmLedger.model_validate(data)
    except ValidationError as ex:
        raise ValueError(
            f"Sample {sample.id} (epoch {sample.epoch}) has a ledger this inspect_swarm cannot read: {ex}"
        ) from ex


def _priced(usage: ModelUsage) -> bool:
    # a model with tokens and no cost is unpriced; a provider may report component
    # counts with a total of zero
    return usage.total_cost is not None or not any(
        getattr(usage, f) for f in _TOKEN_FIELDS
    )


def _unpriced_models(sample: EvalSample) -> list[str]:
    unpriced = {m for m, u in sample.model_usage.items() if not _priced(u)}
    ledger = swarm_ledger(sample)
    if ledger is not None:
        unpriced.update(ledger.unpriced_models)
    return sorted(unpriced)


def _cost(sample: EvalSample) -> float:
    return sum(u.total_cost or 0.0 for u in sample.model_usage.values())


def _complete(sample: EvalSample) -> bool:
    ledger = swarm_ledger(sample)
    return ledger is not None and ledger.total_complete and not _unpriced_models(sample)


def _resumed(sample: EvalSample) -> bool:
    ledger = swarm_ledger(sample)
    return ledger is not None and ledger.resumed


# Column functions are module-level so that samples_df(parallel=...) can pickle them
# into its worker processes.
def _members(sample: EvalSample) -> int | None:
    ledger = swarm_ledger(sample)
    return len(ledger.members) if ledger is not None else None


def _cap(sample: EvalSample, kind: str) -> float | None:
    ledger = swarm_ledger(sample)
    return ledger.caps.get(kind) if ledger is not None else None


def _cost_cap(sample: EvalSample) -> float | None:
    return _cap(sample, "cost")


def _token_cap(sample: EvalSample) -> float | None:
    return _cap(sample, "tokens")


def _time_cap(sample: EvalSample) -> float | None:
    return _cap(sample, "time")


def _calls(sample: EvalSample) -> Calls | None:
    ledger = swarm_ledger(sample)
    return ledger.calls if ledger is not None else None


def _errored(sample: EvalSample) -> int | None:
    calls = _calls(sample)
    return calls.errored if calls is not None else None


def _no_usage(sample: EvalSample) -> int | None:
    calls = _calls(sample)
    return calls.no_usage if calls is not None else None


def _unpriced(sample: EvalSample) -> int | None:
    calls = _calls(sample)
    return calls.unpriced if calls is not None else None


SwarmColumns: list[Column] = [
    SampleColumn("swarm_cost", path=_cost, type=float, full=True),
    SampleColumn("swarm_complete", path=_complete, type=bool, full=True),
    SampleColumn("swarm_members", path=_members, type=int, full=True),
    SampleColumn("swarm_resumed", path=_resumed, type=bool, full=True),
    SampleColumn("swarm_cost_cap", path=_cost_cap, type=float, full=True),
    SampleColumn("swarm_token_cap", path=_token_cap, type=float, full=True),
    SampleColumn("swarm_time_cap", path=_time_cap, type=float, full=True),
    SampleColumn("swarm_errored", path=_errored, type=int, full=True),
    SampleColumn("swarm_no_usage", path=_no_usage, type=int, full=True),
    SampleColumn("swarm_unpriced", path=_unpriced, type=int, full=True),
    SampleColumn("swarm_unpriced_models", path=_unpriced_models, full=True),
]
"""Columns for `inspect_ai.analysis.samples_df()` with each sample's realized cost and what the swarm's ledger says about it, as in `samples_df(logs, columns=SampleSummary + SwarmColumns)`. The `inspect_swarm.analysis` module docstring lists every column."""
