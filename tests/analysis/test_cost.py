import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from inspect_ai import (
    Task,
    eval,  # pyright: ignore[reportUnknownVariableType]
    task_identifier,
)
from inspect_ai.dataset import Sample
from inspect_ai.log import (
    EvalConfig,
    EvalDataset,
    EvalLog,
    EvalSample,
    EvalSpec,
    write_eval_log,
)
from inspect_ai.model import ModelUsage
from inspect_ai.scorer import Score, Value

from inspect_swarm import Budget, member, swarm
from inspect_swarm._evidence import LEDGER_KEY
from inspect_swarm._ledger import Calls, MemberLedger, Meter, SwarmLedger
from inspect_swarm.analysis import (
    ArmCost,
    SampleCost,
    arm_costs,
    fit_lambda,
    sample_costs,
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
    id: int,
    usage: dict[str, ModelUsage],
    scores: dict[str, Value] | None = None,
    ledger: dict[str, Any] | None = None,
    epoch: int = 1,
) -> EvalSample:
    return EvalSample(
        id=id,
        epoch=epoch,
        input="x",
        target="",
        model_usage=usage,
        scores={k: Score(value=v) for k, v in (scores or {}).items()},
        store={LEDGER_KEY: ledger} if ledger is not None else {},
    )


def _log(
    samples: list[EvalSample],
    task_args: dict[str, Any] | None = None,
    cost_limit: float | None = None,
) -> EvalLog:
    return EvalLog(
        eval=EvalSpec(
            created="2026-10-19T00:00:00+00:00",
            task="arm_task",
            dataset=EvalDataset(),
            model="mockllm/model",
            config=EvalConfig(cost_limit=cost_limit),
            task_args=task_args or {},
        ),
        samples=samples,
    )


def _priced(cost: float, tokens: int = 10) -> ModelUsage:
    return ModelUsage(total_tokens=tokens, total_cost=cost)


def _write(log: EvalLog, path: Path) -> EvalLog:
    write_eval_log(log, str(path))
    return log


def test_sample_costs_read_realized_cost_and_the_ledger(tmp_path: Path) -> None:
    log = _write(
        _log(
            [
                # a complete swarm sample over two priced models
                _sample(
                    1,
                    {"a": _priced(1.5), "b": _priced(0.5)},
                    {"match": "C"},
                    _ledger(members=2, cost_cap=9.5),
                ),
                # a complete ledger, but a scorer model with tokens and no price
                _sample(
                    2,
                    {"a": _priced(1.0), "grader": ModelUsage(total_tokens=7)},
                    {"match": "I"},
                    _ledger(members=2, cost_cap=9.5),
                ),
                # no ledger: not a swarm, so nothing judged its spend
                _sample(3, {"a": _priced(0.25)}, {"match": 0.5}),
            ],
            task_args={"k": 2},
            cost_limit=10.0,
        ),
        tmp_path / "arm.eval",
    )
    # a directory, as an eval set's log_dir
    rows = sample_costs(str(tmp_path))
    assert rows[0].log.endswith("/arm.eval")
    common: dict[str, Any] = dict(
        log=rows[0].log,
        arm=task_identifier(log, None),
        task="arm_task",
        task_args={"k": 2},
        model="mockllm/model",
        epoch=1,
        resumed=False,
        cost_limit=10.0,
    )
    assert rows == [
        SampleCost(
            **common,
            id=1,
            score=1.0,
            cost=2.0,
            tokens=20,
            complete=True,
            members=2,
            swarm_cost_cap=9.5,
        ),
        SampleCost(
            **common,
            id=2,
            score=0.0,
            cost=1.0,
            tokens=17,
            complete=False,
            members=2,
            swarm_cost_cap=9.5,
        ),
        SampleCost(
            **common,
            id=3,
            score=0.5,
            cost=0.25,
            tokens=10,
            complete=False,
            members=None,
            swarm_cost_cap=None,
        ),
    ]


def test_arm_costs_compare_arms_on_realized_cost() -> None:
    swarm_arm = _log(
        [
            # derived caps differ with what each sample spent before the swarm
            _sample(
                1, {"a": _priced(3.0)}, {"s": 1.0}, _ledger(members=4, cost_cap=8.5)
            ),
            _sample(
                2, {"a": _priced(5.0)}, {"s": 0.0}, _ledger(members=4, cost_cap=7.5)
            ),
            # left out: no score
            _sample(3, {"a": _priced(100.0)}, None, _ledger(members=4, cost_cap=1.0)),
            # left out: Inspect's unscored value, as a failed model grade writes
            _sample(
                4,
                {"a": _priced(100.0)},
                {"s": float("nan")},
                _ledger(members=4, cost_cap=1.0),
            ),
            # left out: resumed, so its caps restarted from zero
            _sample(
                5,
                {"a": _priced(100.0)},
                {"s": 1.0},
                _ledger(members=4, resumed=True, cost_cap=1.0),
            ),
        ],
        task_args={"k": 4},
        cost_limit=8.0,
    )
    single_arm = _log(
        [
            _sample(1, {"a": _priced(2.0)}, {"s": 1.0}, _ledger(members=1)),
            _sample(
                2, {"a": _priced(4.0)}, {"s": 1.0}, _ledger(members=1, complete=False)
            ),
        ],
        task_args={"k": 1},
        cost_limit=8.0,
    )
    rows = sample_costs([swarm_arm, single_arm], scorer="s")
    arms = arm_costs(rows)
    assert [
        (a.task_args, a.members, a.samples, a.unscored, a.resumed, a.score, a.cost)
        for a in arms
    ] == [
        ({"k": 4}, 4, 2, 2, 1, 0.5, 4.0),
        ({"k": 1}, 1, 2, 0, 0, 1.0, 3.0),
    ]
    # the caps of the samples in the means, whatever the row order
    for ordered in (arms, arm_costs(rows[::-1])):
        caps = {a.arm: (a.swarm_cost_cap_min, a.swarm_cost_cap_max) for a in ordered}
        assert caps == {
            task_identifier(swarm_arm, None): (7.5, 8.5),
            task_identifier(single_arm, None): (None, None),
        }
    assert [a.complete for a in arms] == [True, False]
    assert [a.arm for a in arms] == [
        task_identifier(swarm_arm, None),
        task_identifier(single_arm, None),
    ]
    assert all(a.cost_limit == 8.0 for a in arms)


@pytest.mark.parametrize("lam", [0.48, 1.0])
def test_fit_lambda_recovers_a_known_lambda(lam: float) -> None:
    # Ord's grid: agent count k and per-agent cost b, total cost k * b, with scores on
    # the model's iso-performance surface
    intercept, slope = 0.2, 0.05
    logs = [
        _log(
            [
                _sample(
                    1,
                    {"a": _priced(k * b)},
                    {"s": intercept + slope * (math.log(b) + lam * math.log(k))},
                    _ledger(members=k),
                )
            ],
            task_args={"k": k, "b": b},
        )
        for k in (1, 2, 4, 8)
        for b in (0.5, 1.0, 2.0, 4.0)
    ]
    fit = fit_lambda(arm_costs(sample_costs(logs)))
    assert fit.lambda_ == pytest.approx(lam)
    assert fit.slope == pytest.approx(slope)
    assert fit.intercept == pytest.approx(intercept)
    assert (fit.arms, fit.complete) == (16, True)


def _arm(members: int | None, cost: float, score: float = 0.5) -> ArmCost:
    return ArmCost(
        arm=f"arm-{members}-{cost}",
        task="arm_task",
        task_args={},
        model="mockllm/model",
        members=members,
        samples=1,
        unscored=0,
        resumed=0,
        score=score,
        cost=cost,
        complete=True,
        cost_limit=None,
        swarm_cost_cap_min=None,
        swarm_cost_cap_max=None,
    )


def _grid(score: Callable[[int, float], float]) -> list[ArmCost]:
    # agent count k and per-agent cost b
    return [
        _arm(k, k * b, score(k, b)) for k in (1, 2, 4, 8) for b in (0.5, 1.0, 2.0, 4.0)
    ]


@pytest.mark.parametrize(
    ("arms", "error"),
    [
        # a plain agent's log has no ledger, so no agent count
        ([_arm(None, 1.0), _arm(2, 2.0), _arm(4, 4.0)], "no swarm ledger"),
        # one agent count: λ is not identified
        ([_arm(2, 1.0), _arm(2, 2.0), _arm(2, 4.0)], "vary agent count"),
        # per-agent cost fixed by agent count: on one line
        ([_arm(1, 1.0), _arm(2, 4.0), _arm(4, 16.0)], "vary agent count"),
        # one per-agent cost, which roundoff in ln(cost) - ln(k) must not hide
        (
            [_arm(k, k * 0.5, 0.2 + 0.05 * math.log(k)) for k in (1, 2, 4, 8)],
            "vary agent count",
        ),
        ([_arm(1, 1.0), _arm(2, 2.0)], "at least three arms"),
        ([_arm(1, 0.0), _arm(2, 2.0), _arm(4, 4.0)], "positive cost"),
        # the score does not move with per-agent cost
        ([_arm(1, 1.0), _arm(1, 2.0), _arm(2, 2.0)], "λ is undefined"),
        # the same, where roundoff leaves a slope that is not exactly zero
        (_grid(lambda k, b: 0.2), "λ is undefined"),
        (_grid(lambda k, b: 0.2 + 0.05 * math.log(k)), "λ is undefined"),
    ],
)
def test_fit_lambda_refuses_what_it_cannot_fit(arms: list[ArmCost], error: str) -> None:
    with pytest.raises(ValueError, match=error):
        fit_lambda(arms)


@pytest.mark.parametrize(
    ("sample", "error"),
    [
        (
            _sample(1, {}, {"s": 1.0}, {**_ledger(), "version": 2}),
            "ledger this inspect_swarm cannot read",
        ),
        (_sample(1, {}, {"s": {"best": 1.0}}), "dict score value"),
        (_sample(1, {}, {"s": 1.0, "t": 0.0}), "name the one to use"),
    ],
)
def test_sample_costs_refuse_what_they_cannot_read(
    sample: EvalSample, error: str
) -> None:
    with pytest.raises(ValueError, match=error):
        sample_costs(_log([sample]))


def test_arm_costs_refuse_a_sample_counted_twice() -> None:
    log = _log([_sample(1, {"a": _priced(1.0)}, {"s": 1.0})])
    with pytest.raises(ValueError, match="more than once"):
        arm_costs(sample_costs([log, log]))


def test_arm_costs_refuse_an_arm_with_two_roster_sizes() -> None:
    log = _log(
        [
            _sample(1, {"a": _priced(1.0)}, {"s": 1.0}, _ledger(members=2)),
            _sample(2, {"a": _priced(1.0)}, {"s": 1.0}, _ledger(members=4)),
        ]
    )
    with pytest.raises(ValueError, match="different roster sizes"):
        arm_costs(sample_costs(log))


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
    assert (row.swarm_cost_cap, row.cost_limit) == (10.0, None)
