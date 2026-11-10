from pathlib import Path

import pytest
from inspect_ai import Task, eval, task  # pyright: ignore[reportUnknownVariableType]
from inspect_ai._eval.evalset import EvalSetArgsInTaskIdentifier, task_identifier
from inspect_ai._eval.loader import resolve_tasks
from inspect_ai.agent import react
from inspect_ai.dataset import Sample
from inspect_ai.event import ScoreEvent
from inspect_ai.model import GenerateConfig, Model, ModelOutput, get_model
from inspect_ai.scorer import (
    CORRECT,
    INCORRECT,
    Score,
    Scorer,
    Target,
    Value,
    accuracy,
    scorer,
)
from inspect_ai.solver import TaskState

from inspect_swarm import answer_result, member, swarm
from inspect_swarm.scorer import MEMBER_METRICS, member_scores

from .swarm_runtime import Script, scripted_model


@scorer(metrics=[accuracy()])
def verdict(passing: bool) -> Scorer:
    async def score(state: TaskState, target: Target) -> Score:
        return Score(value=CORRECT if passing else INCORRECT)

    return score


def _submit() -> ModelOutput:
    return ModelOutput.for_tool_call("mockllm", "submit", {"answer": "42"})


def _task(passing: bool, model: Model) -> Task:
    # members are react(attempts=3): each submission is scored by the task's scorer
    return Task(
        dataset=[Sample(input="What is the answer?", target="42")],
        solver=swarm(
            member(react, args={"model": model, "attempts": 3}, count=2),
            channels=[],
        ),
        scorer=verdict(passing),
        name="scoring_loop",
    )


_MODEL = scripted_model(Script(respond=lambda _: _submit()))


@task
def scorer_by_argument(passing: bool = True) -> Task:
    return _task(passing, _MODEL)


@pytest.mark.parametrize("passing,calls", [(True, 2), (False, 6)])
def test_the_task_scorer_changes_each_members_trajectory(
    tmp_path: Path, passing: bool, calls: int
) -> None:
    # identical submissions: a passing scorer ends each member after one call, and a
    # failing one makes it retry until its attempts run out
    script = Script(respond=lambda _: _submit())
    [log] = eval(
        _task(passing, scripted_model(script)),
        model="mockllm/model",
        log_dir=str(tmp_path),
        display="none",
    )
    assert log.status == "success"
    assert script.calls == calls


def _identifier(task: Task) -> str:
    [resolved] = resolve_tasks(
        task,
        task_args={},
        model=get_model("mockllm/model"),
        model_roles=None,
        sandbox=None,
        sample_shuffle=None,
    )
    return task_identifier(
        resolved, EvalSetArgsInTaskIdentifier(config=GenerateConfig())
    )


def test_arms_differing_in_scorer_are_distinct_only_by_task_argument() -> None:
    # chosen by a task argument, the two arms are distinct in an eval set
    assert _identifier(scorer_by_argument(passing=True)) != _identifier(
        scorer_by_argument(passing=False)
    )
    # swapped in code, they are not
    assert _identifier(_task(True, _MODEL)) == _identifier(_task(False, _MODEL))


@scorer(metrics=[accuracy()])
def recorded(passing: bool, completed: list[bool]) -> Scorer:
    async def score(state: TaskState, target: Target) -> Score:
        completed.append(state.completed)
        return Score(value=CORRECT if passing else INCORRECT)

    return score


@scorer(metrics=MEMBER_METRICS)
def loop_members(passing: bool, completed: list[bool]) -> Scorer:
    return member_scores(recorded(passing, completed), answer_result())


def _attempt_loop(
    tmp_path: Path, passing: bool, completed: list[bool] | None
) -> tuple[int, list[Value]]:
    # the model calls the members made, and the in-loop grades they got
    script = Script(respond=lambda _: _submit())
    scorers = [verdict(passing)]
    if completed is not None:
        scorers.append(loop_members(passing, completed))
    [log] = eval(
        Task(
            dataset=[Sample(input="What is the answer?", target="42")],
            solver=swarm(
                member(
                    react,
                    args={"model": scripted_model(script), "attempts": 3},
                    count=2,
                ),
                channels=[],
                result=answer_result(),
            ),
            scorer=scorers,
        ),
        model="mockllm/model",
        log_dir=str(tmp_path),
        display="none",
    )
    assert log.status == "success", log.error
    assert log.samples is not None
    [sample] = log.samples
    grades = [
        e.score.value
        for e in sample.events
        if isinstance(e, ScoreEvent) and e.intermediate
    ]
    return script.calls, grades


@pytest.mark.parametrize("passing", [True, False])
def test_member_scores_leaves_the_attempt_loop_unchanged(
    tmp_path: Path, passing: bool
) -> None:
    completed: list[bool] = []
    without = _attempt_loop(tmp_path / "without", passing, None)
    assert _attempt_loop(tmp_path / "with", passing, completed) == without
    # its scorer grades the members only after the solve
    assert completed and all(completed)
