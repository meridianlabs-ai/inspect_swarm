from pathlib import Path
from typing import Any, cast

import pytest
from inspect_ai import (
    Task,
    eval,  # pyright: ignore[reportUnknownVariableType]
)
from inspect_ai.analysis import SampleSummary, samples_df
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
from inspect_swarm.analysis import (
    Calls,
    MemberLedger,
    Meter,
    SwarmColumns,
    SwarmLedger,
    swarm_ledger,
)
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


_SWARM = [str(c.name) for c in SwarmColumns]


def _rows(logs: str, parallel: bool | int = False) -> dict[str, dict[str, Any]]:
    df = samples_df(logs, columns=SampleSummary + SwarmColumns, parallel=parallel)
    records = cast(
        list[dict[str, Any]],
        df.to_dict("records"),  # pyright: ignore[reportUnknownMemberType]
    )
    return {row["id"]: {name: row[name] for name in _SWARM} for row in records}


def _caps(
    cost: float | None = None, tokens: float | None = None, time: float | None = None
) -> dict[str, float | None]:
    return {"cost": cost, "tokens": tokens, "time": time}


def test_swarm_columns_read_realized_cost_and_the_ledger(tmp_path: Path) -> None:
    capped = _ledger(members=3)
    capped["caps"] = _caps(9.5, 1000.0, 60.0)
    # every model priced, so only the ledger's judgment makes the cost a lower bound
    incomplete = _ledger(complete=False)
    incomplete["calls"]["no_usage"] = 1
    counted = _ledger(complete=False, resumed=True)
    counted["calls"].update(errored=3, no_usage=2, unpriced=4)
    counted["unpriced_models"] = ["swarm-model"]
    write_eval_log(
        _log(
            [
                _sample("complete", {"a": _priced(1.5), "b": _priced(0.5)}, capped),
                # a complete ledger, but a scorer model with tokens and no price
                _sample(
                    "unpriced",
                    {"a": _priced(1.0), "grader": ModelUsage(total_tokens=7)},
                    _ledger(),
                ),
                _sample("incomplete", {"a": _priced(1.0)}, incomplete),
                _sample("counted", {"a": _priced(1.0)}, counted),
                # not a swarm, so nothing judged its spend
                _sample("no-ledger", {"a": _priced(0.25)}),
            ]
        ),
        str(tmp_path / "arm.eval"),
    )
    swarm: dict[str, Any] = dict(
        swarm_complete=True,
        swarm_members=2,
        swarm_resumed=False,
        swarm_cost_cap=None,
        swarm_token_cap=None,
        swarm_time_cap=None,
        swarm_errored=0,
        swarm_no_usage=0,
        swarm_unpriced=0,
        swarm_unpriced_models="[]",
    )
    rows = _rows(str(tmp_path))
    assert rows == {
        "complete": swarm
        | dict(
            swarm_cost=2.0,
            swarm_members=3,
            swarm_cost_cap=9.5,
            swarm_token_cap=1000.0,
            swarm_time_cap=60.0,
        ),
        "unpriced": swarm
        | dict(
            swarm_cost=1.0, swarm_complete=False, swarm_unpriced_models='["grader"]'
        ),
        "incomplete": swarm
        | dict(swarm_cost=1.0, swarm_complete=False, swarm_no_usage=1),
        "counted": swarm
        | dict(
            swarm_cost=1.0,
            swarm_complete=False,
            swarm_resumed=True,
            swarm_errored=3,
            swarm_no_usage=2,
            swarm_unpriced=4,
            swarm_unpriced_models='["swarm-model"]',
        ),
        "no-ledger": {name: None for name in _SWARM}
        | dict(
            swarm_cost=0.25,
            swarm_complete=False,
            swarm_resumed=False,
            swarm_unpriced_models="[]",
        ),
    }
    # parallel reads pickle the columns into worker processes
    assert _rows(str(tmp_path), parallel=2) == rows


def test_swarm_columns_refuse_a_ledger_they_cannot_read(tmp_path: Path) -> None:
    sample = _sample(1, {}, {**_ledger(), "version": 2})
    write_eval_log(_log([sample]), str(tmp_path / "arm.eval"))
    with pytest.raises(
        ValueError, match="Sample 1 .*ledger this inspect_swarm cannot read"
    ):
        samples_df(str(tmp_path), columns=SampleSummary + SwarmColumns)


def test_swarm_columns_read_the_ledger_a_swarm_writes(tmp_path: Path) -> None:
    model = scripted_model(cost=1.5)
    task = Task(
        dataset=[Sample(input="task")],
        solver=swarm(
            member(generator, args={"model": model, "answer": "42"}, name="m", count=2),
            channels=[],
            budget=Budget(cost=10.0),
        ),
    )
    eval(task, model="mockllm/model", log_dir=str(tmp_path), display="none")
    [row] = _rows(str(tmp_path)).values()
    assert row["swarm_cost"] == pytest.approx(3.0)
    assert row | {"swarm_cost": None} == dict(
        swarm_cost=None,
        swarm_complete=True,
        swarm_members=2,
        swarm_resumed=False,
        swarm_cost_cap=10.0,
        swarm_token_cap=None,
        swarm_time_cap=None,
        swarm_errored=0,
        swarm_no_usage=0,
        swarm_unpriced=0,
        swarm_unpriced_models="[]",
    )


def test_swarm_ledger_reads_the_ledger_a_swarm_writes(tmp_path: Path) -> None:
    model = scripted_model(cost=1.5)
    task = Task(
        dataset=[Sample(input="task")],
        solver=swarm(
            member(generator, args={"model": model, "answer": "42"}, name="m", count=2),
            channels=[],
        ),
    )
    [log] = eval(task, model="mockllm/model", log_dir=str(tmp_path), display="none")
    [sample] = read_eval_log(log.location).samples or []
    ledger = swarm_ledger(sample)
    assert isinstance(ledger, SwarmLedger)
    assert [m.name for m in ledger.members] == ["m-1", "m-2"]
    assert [m.meter.cost for m in ledger.members] == [
        pytest.approx(1.5),
        pytest.approx(1.5),
    ]


def test_swarm_ledger_is_none_without_a_ledger() -> None:
    assert swarm_ledger(_sample(1, {})) is None


def test_swarm_ledger_refuses_a_ledger_it_cannot_read() -> None:
    sample = _sample(1, {}, {**_ledger(), "version": 2})
    with pytest.raises(
        ValueError, match="Sample 1 .*ledger this inspect_swarm cannot read"
    ):
        swarm_ledger(sample)


def test_ledger_schema_describes_every_field() -> None:
    schema = SwarmLedger.model_json_schema()
    models = {"SwarmLedger": schema} | {
        name: schema["$defs"][name]
        for name in ("MemberLedger", "Meter", "Calls", "Budget")
    }
    for name, model in models.items():
        assert model.get("description"), name
        for field, prop in model["properties"].items():
            assert prop.get("description"), f"{name}.{field}"
