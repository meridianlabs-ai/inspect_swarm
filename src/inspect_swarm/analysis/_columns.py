from collections.abc import Callable

from inspect_ai.analysis import Column, SampleColumn
from inspect_ai.log import EvalSample
from inspect_ai.model import ModelUsage
from pydantic import JsonValue, ValidationError

from .._evidence import LEDGER_KEY
from .._ledger import (
    _TOKEN_FIELDS,  # pyright: ignore[reportPrivateUsage]
    SwarmLedger,
)


def _ledger(sample: EvalSample) -> SwarmLedger | None:
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
    ledger = _ledger(sample)
    if ledger is not None:
        unpriced.update(ledger.unpriced_models)
    return sorted(unpriced)


def _cost(sample: EvalSample) -> float:
    return sum(u.total_cost or 0.0 for u in sample.model_usage.values())


def _complete(sample: EvalSample) -> bool:
    ledger = _ledger(sample)
    return ledger is not None and ledger.total_complete and not _unpriced_models(sample)


def _from_ledger(
    read: Callable[[SwarmLedger], JsonValue],
) -> Callable[[EvalSample], JsonValue]:
    def extract(sample: EvalSample) -> JsonValue:
        ledger = _ledger(sample)
        return read(ledger) if ledger is not None else None

    return extract


def _resumed(sample: EvalSample) -> bool:
    ledger = _ledger(sample)
    return ledger is not None and ledger.resumed


SwarmColumns: list[Column] = [
    SampleColumn("swarm_cost", path=_cost, type=float, full=True),
    SampleColumn("swarm_complete", path=_complete, type=bool, full=True),
    SampleColumn(
        "swarm_members",
        path=_from_ledger(lambda ledger: len(ledger.members)),
        type=int,
        full=True,
    ),
    SampleColumn("swarm_resumed", path=_resumed, type=bool, full=True),
    SampleColumn(
        "swarm_cost_cap",
        path=_from_ledger(lambda ledger: ledger.caps.get("cost")),
        type=float,
        full=True,
    ),
    SampleColumn(
        "swarm_token_cap",
        path=_from_ledger(lambda ledger: ledger.caps.get("tokens")),
        type=float,
        full=True,
    ),
    SampleColumn(
        "swarm_time_cap",
        path=_from_ledger(lambda ledger: ledger.caps.get("time")),
        type=float,
        full=True,
    ),
    SampleColumn(
        "swarm_errored",
        path=_from_ledger(lambda ledger: ledger.calls.errored),
        type=int,
        full=True,
    ),
    SampleColumn(
        "swarm_no_usage",
        path=_from_ledger(lambda ledger: ledger.calls.no_usage),
        type=int,
        full=True,
    ),
    SampleColumn(
        "swarm_unpriced",
        path=_from_ledger(lambda ledger: ledger.calls.unpriced),
        type=int,
        full=True,
    ),
    SampleColumn("swarm_unpriced_models", path=_unpriced_models, full=True),
]
"""Columns for `inspect_ai.analysis.samples_df()` with each sample's realized cost and what the swarm's ledger says about it.

Use them beside Inspect's own columns, as in `samples_df(logs, columns=SampleSummary + SwarmColumns)`. They read the full sample (its store and `model_usage`):

- `swarm_cost`: realized cost in dollars, the priced cost in the sample's `model_usage`, scorer calls included. Tokens are in `SampleSummary`'s `total_tokens`.
- `swarm_complete`: no call is known to be missing from `swarm_cost`. The swarm's ledger marks its interval complete, and every model in the sample's `model_usage` is priced. The ledger judges only the swarm's interval; calls outside it, scorer calls included, are not checked for missing usage. `False` for a sample with no ledger, since nothing judged its spend.
- `swarm_members`: members in the swarm's roster.
- `swarm_resumed`: the sample resumed from a checkpoint, so its swarm restarted its caps from zero. `False` for a sample with no ledger.
- `swarm_cost_cap`, `swarm_token_cap`, `swarm_time_cap`: the swarm's caps, derived or explicit; the time cap in seconds.
- `swarm_errored`, `swarm_no_usage`, `swarm_unpriced`: model calls in the swarm that ended in an error or cancellation (retries included), that completed with no usage, and whose usage has no cost. Each makes the cost a lower bound. The ledger keeps the other reasons.
- `swarm_unpriced_models`: models with tokens and no cost, the ledger's and any in the sample's `model_usage`, as a JSON list.

The columns read from the ledger are null for a sample with no ledger. A sample that runs two swarms keeps only the last one's ledger, while its `model_usage` covers both. A ledger this inspect_swarm cannot read (a version other than 1) raises a `ValueError` naming the sample.
"""
