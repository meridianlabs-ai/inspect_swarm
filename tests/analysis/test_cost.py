from pathlib import Path
from typing import Any

import pytest
from inspect_ai import (
    Task,
    eval,  # pyright: ignore[reportUnknownVariableType]
)
from inspect_ai.dataset import Sample
from inspect_ai.log import (
    EvalConfig,
    EvalDataset,
    EvalLog,
    EvalSample,
    EvalSpec,
    read_eval_log,
    write_eval_log,
)
from inspect_ai.model import ModelUsage

from inspect_swarm import Budget, member, swarm
from inspect_swarm._evidence import LEDGER_KEY
from inspect_swarm._ledger import Calls, MemberLedger, Meter, SwarmLedger
from inspect_swarm.analysis import SampleCost, sample_costs
from tests.swarm_runtime import generator, scripted_model


def _ledger(
    members: int = 2,
    complete: bool = True,
    resumed: bool = False,
    cost_cap: float | None = None,
) -> dict[str, Any]:
    meter = Meter(usage=ModelUsage(), cost=0.0, suspended_cost=0.0)
    ledger = SwarmLedger(
        budget=Budget(),
        caps={"cost": cost_cap, "tokens": None, "time": None},
        sample_limits={},
        members=[
            MemberLedger(
                name=f"m{i}",
                status="submitted",
                bridged=False,
                meter=meter,
                calls=Calls(),
                cancelled=False,
            )
            for i in range(members)
        ],
        runtime=meter,
        final=meter,
        swarm=meter,
        calls=Calls(),
        unpriced_calls_recorded=0,
        sample_delta=ModelUsage(),
        unattributed=ModelUsage(),
        unpriced_models=[],
        native_compaction_classes=[],
        overshoot={},
        stop_reason=None,
        ended_by=None,
        interrupted=False,
        final_cancelled=False,
        resumed=resumed,
        total_complete=complete,
        attribution_complete=complete,
    )
    return ledger.model_dump(mode="json")


def _sample(
    id: int | str,
    usage: dict[str, ModelUsage],
    ledger: dict[str, Any] | None = None,
    epoch: int = 1,
) -> EvalSample:
    return EvalSample(
        id=id,
        epoch=epoch,
        input="x",
        target="",
        model_usage=usage,
        store={LEDGER_KEY: ledger} if ledger is not None else {},
    )


def _log(samples: list[EvalSample]) -> EvalLog:
    return EvalLog(
        eval=EvalSpec(
            created="2026-10-19T00:00:00+00:00",
            task="arm_task",
            dataset=EvalDataset(),
            model="mockllm/model",
            config=EvalConfig(),
        ),
        samples=samples,
    )


def _priced(cost: float, tokens: int = 10) -> ModelUsage:
    return ModelUsage(total_tokens=tokens, total_cost=cost)


def test_sample_costs_read_realized_cost_and_the_ledger(tmp_path: Path) -> None:
    path = tmp_path / "arm.eval"
    write_eval_log(
        _log(
            [
                # a complete swarm sample over two priced models
                _sample(
                    1,
                    {"a": _priced(1.5), "b": _priced(0.5)},
                    _ledger(members=2, cost_cap=9.5),
                ),
                # a complete ledger, but a scorer model with tokens and no price
                _sample(
                    "two",
                    {"a": _priced(1.0), "grader": ModelUsage(total_tokens=7)},
                    _ledger(members=2, cost_cap=9.5),
                ),
                # no ledger: not a swarm, so nothing judged its spend
                _sample(3, {"a": _priced(0.25)}, epoch=2),
            ]
        ),
        str(path),
    )
    swarm_fields: dict[str, Any] = dict(
        members=2,
        cost_cap=9.5,
        errored=0,
        no_usage=0,
        unpriced=0,
        unpriced_calls_recorded=0,
        native_compaction_classes=[],
        cancelled_members=[],
        interrupted=False,
        final_cancelled=False,
    )
    # a directory, as an eval set's log_dir; the keys are spelled as samples_df()'s
    assert sample_costs(str(tmp_path)) == [
        SampleCost(
            log=str(path),
            id="1",
            epoch=1,
            cost=2.0,
            tokens=20,
            complete=True,
            unpriced_models=[],
            **swarm_fields,
        ),
        SampleCost(
            log=str(path),
            id="two",
            epoch=1,
            cost=1.0,
            tokens=17,
            complete=False,
            unpriced_models=["grader"],
            **swarm_fields,
        ),
        SampleCost(
            log=str(path),
            id="3",
            epoch=2,
            cost=0.25,
            tokens=10,
            complete=False,
            unpriced_models=[],
        ),
    ]


def _stored_inputs(path: Path) -> dict[str, Any]:
    return {
        # what eval_set() returns: logs read header only, their samples on disk
        "header": read_eval_log(str(path), header_only=True),
        "headers": [read_eval_log(str(path), header_only=True)],
        # a sample passed twice gets one row, as in samples_df()
        "log-twice": [read_eval_log(str(path))] * 2,
        "dir-and-file": [str(path.parent), str(path)],
    }


@pytest.mark.parametrize("case", ["header", "headers", "log-twice", "dir-and-file"])
def test_sample_costs_read_each_stored_sample_once(tmp_path: Path, case: str) -> None:
    path = tmp_path / "arm.eval"
    write_eval_log(_log([_sample(1, {"a": _priced(1.0)}, _ledger())]), str(path))
    [row] = sample_costs(str(path))
    assert sample_costs(_stored_inputs(path)[case]) == [row]


def test_sample_costs_take_completeness_from_the_ledger() -> None:
    # every model priced, so only the ledger's judgment makes the cost a lower bound
    ledger = _ledger(complete=False)
    ledger["calls"]["no_usage"] = 1
    [row] = sample_costs(_log([_sample(1, {"a": _priced(1.0)}, ledger)]))
    assert (row.complete, row.no_usage, row.unpriced_models) == (False, 1, [])


def test_sample_costs_report_why_a_cost_is_a_lower_bound() -> None:
    ledger = _ledger(members=3, complete=False, resumed=True)
    ledger["calls"].update(errored=3, no_usage=2, unpriced=4)
    ledger["members"][1]["cancelled"] = True
    ledger.update(
        caps={"cost": 5.0, "tokens": 1000.0, "time": 60.0},
        unpriced_calls_recorded=5,
        unpriced_models=["swarm-model"],
        native_compaction_classes=["OpenAIAPI"],
        interrupted=True,
        final_cancelled=True,
    )
    [row] = sample_costs(_log([_sample(1, {"a": _priced(1.0)}, ledger)]))
    assert row.model_dump(exclude={"log", "id", "epoch", "cost", "tokens"}) == dict(
        complete=False,
        members=3,
        resumed=True,
        cost_cap=5.0,
        token_cap=1000.0,
        time_cap=60.0,
        errored=3,
        no_usage=2,
        unpriced=4,
        unpriced_calls_recorded=5,
        unpriced_models=["swarm-model"],
        native_compaction_classes=["OpenAIAPI"],
        cancelled_members=["m1"],
        interrupted=True,
        final_cancelled=True,
    )


def test_sample_costs_refuse_a_ledger_they_cannot_read() -> None:
    sample = _sample(1, {}, {**_ledger(), "version": 2})
    with pytest.raises(ValueError, match="ledger this inspect_swarm cannot read"):
        sample_costs(_log([sample]))


def test_sample_costs_read_the_ledger_a_swarm_writes(tmp_path: Path) -> None:
    model = scripted_model(cost=1.5)
    task = Task(
        dataset=[Sample(input="task")],
        solver=swarm(
            member(generator, args={"model": model, "answer": "42"}, name="m", count=2),
            channels=[],
            budget=Budget(cost=10.0),
        ),
    )
    [log] = eval(task, model="mockllm/model", log_dir=str(tmp_path), display="none")
    [row] = sample_costs(log.location)
    assert (row.members, row.complete, row.resumed) == (2, True, False)
    assert row.cost == pytest.approx(3.0)
    assert (row.cost_cap, row.errored, row.cancelled_members) == (10.0, 0, [])
