from collections.abc import Sequence
from typing import Any

from inspect_ai.analysis._dataframe.evals.extract import eval_log_location
from inspect_ai.analysis._dataframe.samples.extract import auto_sample_id
from inspect_ai.analysis._dataframe.util import LogPaths, resolve_logs
from inspect_ai.log import EvalLog, EvalSample, read_eval_log
from inspect_ai.model import ModelUsage
from pydantic import BaseModel, ValidationError

from .._evidence import LEDGER_KEY
from .._ledger import (
    _TOKEN_FIELDS,  # pyright: ignore[reportPrivateUsage]
    SwarmLedger,
)

# sample fields the helper never reads, skipped when a log is read from a file
_EXCLUDE_FIELDS = {"events", "messages", "attachments"}


class SampleCost(BaseModel):
    """One sample's realized cost and what the swarm's ledger says about it.

    `log`, `id` and `epoch` match the columns of the same names in `inspect_ai.analysis.samples_df()`, so the rows join to it on those three. With no ledger, the fields read from it are `None`, and `resumed` is `False`.
    """

    log: str
    """Location of the log the sample was read from, as in `samples_df()`."""

    id: str
    """Sample id as a string, as in `samples_df()`."""

    epoch: int
    """Sample epoch."""

    cost: float
    """Realized cost in dollars: the priced cost in the sample's `model_usage`, scorer calls included."""

    tokens: int
    """Total tokens in the sample's `model_usage`."""

    complete: bool
    """No call is known to be missing from `cost`: the ledger marks its interval complete (`total_complete`), and every model in the sample's `model_usage` is priced. The ledger judges only the swarm's interval; calls outside it, scorer calls included, are not checked for missing usage. Always `False` for a sample with no ledger, since nothing judged its spend. When `False`, `cost` is a lower bound and the fields below say why."""

    members: int | None = None
    """Members in the swarm's roster."""

    resumed: bool = False
    """The sample resumed from a checkpoint, so its swarm restarted its caps from zero."""

    cost_cap: float | None = None
    """The swarm's cost cap, derived or explicit, or `None` with no cost cap."""

    token_cap: float | None = None
    """The swarm's token cap, or `None` with no token cap."""

    time_cap: float | None = None
    """The swarm's time cap in seconds, or `None` with no time cap."""

    errored: int | None = None
    """Model calls in the swarm that ended in an error or cancellation, retries included: usage unknown."""

    no_usage: int | None = None
    """Model calls in the swarm that completed with no usage."""

    unpriced: int | None = None
    """Model calls in the swarm whose usage has no cost."""

    unpriced_calls_recorded: int | None = None
    """Unpriced calls the swarm's token meter recorded, the cross-check on `unpriced`."""

    unpriced_models: list[str]
    """Models with tokens and no cost: the ledger's, over the swarm's interval, and any in the sample's `model_usage`."""

    native_compaction_classes: list[str] | None = None
    """Loaded model API classes with native compaction, which leaves no trace of its usage."""

    cancelled_members: list[str] | None = None
    """Members whose agent loop was cancelled."""

    interrupted: bool | None = None
    """A foreign error or outside cancellation ended the swarm."""

    final_cancelled: bool | None = None
    """The swarm's final step was cancelled."""


def sample_costs(logs: LogPaths | EvalLog | Sequence[EvalLog]) -> list[SampleCost]:
    """Read each sample's realized cost and the swarm's ledger from a set of logs.

    The cost is the sample's `model_usage`, which every arm has, scorer calls included. A swarm's ledger says whether that cost is complete, and gives its roster size, caps and the reasons a cost is a lower bound. Scores are in `samples_df()`; join the rows to it on `log`, `id` and `epoch`.

    A sample that runs two swarms keeps only the last one's ledger, while its `model_usage` covers both.

    Args:
        logs: Log files, directories of logs, or logs already read.

    Returns:
        One row per sample, in log order. A sample in more than one of `logs` (a log passed twice, or a log and its directory) gets one row, as in `samples_df()`.

    Raises:
        ValueError: When a ledger does not validate (a version other than 1, for one).
    """
    rows: list[SampleCost] = []
    seen: set[str] = set()
    for entry in resolve_logs(logs):
        if isinstance(entry, str):
            log = read_eval_log(entry, exclude_fields=_EXCLUDE_FIELDS)
        elif not entry.samples and entry.location:
            # a log read header only (as eval_set() returns them) has its samples on
            # disk, where samples_df() reads them too
            log = read_eval_log(entry.location, exclude_fields=_EXCLUDE_FIELDS)
        else:
            log = entry
        location = eval_log_location(log)
        for sample in log.samples or []:
            # samples_df()'s key, so that a log passed twice (or a file and its
            # directory) gives one row per sample, as there
            key = sample.uuid or auto_sample_id(log.eval.eval_id, sample)
            if key not in seen:
                seen.add(key)
                rows.append(_sample_cost(log, location, sample))
    return rows


def _sample_cost(log: EvalLog, location: str, sample: EvalSample) -> SampleCost:
    ledger = _ledger(log, sample)
    unpriced = {m for m, u in sample.model_usage.items() if not _priced(u)}
    fields: dict[str, Any] = {}
    if ledger is not None:
        unpriced.update(ledger.unpriced_models)
        fields = dict(
            members=len(ledger.members),
            resumed=ledger.resumed,
            cost_cap=ledger.caps.get("cost"),
            token_cap=ledger.caps.get("tokens"),
            time_cap=ledger.caps.get("time"),
            errored=ledger.calls.errored,
            no_usage=ledger.calls.no_usage,
            unpriced=ledger.calls.unpriced,
            unpriced_calls_recorded=ledger.unpriced_calls_recorded,
            native_compaction_classes=ledger.native_compaction_classes,
            cancelled_members=[m.name for m in ledger.members if m.cancelled],
            interrupted=ledger.interrupted,
            final_cancelled=ledger.final_cancelled,
        )
    return SampleCost(
        log=location,
        id=str(sample.id),
        epoch=sample.epoch,
        cost=sum(u.total_cost or 0.0 for u in sample.model_usage.values()),
        tokens=sum(u.total_tokens for u in sample.model_usage.values()),
        complete=ledger is not None and ledger.total_complete and not unpriced,
        unpriced_models=sorted(unpriced),
        **fields,
    )


def _ledger(log: EvalLog, sample: EvalSample) -> SwarmLedger | None:
    data = sample.store.get(LEDGER_KEY)
    if data is None:
        return None
    try:
        return SwarmLedger.model_validate(data)
    except ValidationError as ex:
        raise ValueError(
            f"Sample {sample.id} (epoch {sample.epoch}) in {log.location} has a ledger this inspect_swarm cannot read: {ex}"
        ) from ex


def _priced(usage: ModelUsage) -> bool:
    # a model with tokens and no cost is unpriced; a provider may report component
    # counts with a total of zero
    return usage.total_cost is not None or not any(
        getattr(usage, f) for f in _TOKEN_FIELDS
    )
