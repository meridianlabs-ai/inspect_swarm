from pathlib import Path
from typing import Any

import anyio
import pytest
from inspect_ai import Task, eval  # pyright: ignore[reportUnknownVariableType]
from inspect_ai.agent import Agent, AgentState, agent
from inspect_ai.dataset import Sample
from inspect_ai.log import EvalLog
from inspect_ai.model import ChatMessage, ModelOutput
from inspect_ai.scorer import (
    CORRECT,
    INCORRECT,
    Score,
    Scorer,
    Target,
    accuracy,
    match,
    model_graded_qa,
    scorer,
)
from inspect_ai.solver import TaskState
from inspect_ai.util import sandbox

from inspect_swarm import (
    Budget,
    Controller,
    StopReason,
    SwarmControl,
    member,
    swarm,
    swarm_controller,
)

from .swarm_runtime import Script, generator, output, scripted_model


def _controller(run: Any, name: str) -> Controller:
    @swarm_controller(name=name)
    def factory() -> Controller:
        return Controller(run)

    return factory()


def _eval(task: Task, tmp_path: Path) -> EvalLog:
    [log] = eval(task, model="mockllm/model", log_dir=str(tmp_path), display="none")
    assert log.status == "success", log.error
    return log


def _score(log: EvalLog) -> Score:
    assert log.samples is not None
    [sample] = log.samples
    assert sample.scores is not None
    [score] = sample.scores.values()
    return score


def _members(first: str, second: str) -> list[Any]:
    # `first` answers at once; `second` after a model call
    return [
        member(generator(scripted_model(), calls=0, answer=first), name="first"),
        member(
            generator(scripted_model(Script(delay=0.05)), calls=1, answer=second),
            name="second",
        ),
    ]


@pytest.mark.parametrize(
    "first,second,expected",
    [("42", "41", CORRECT), ("41", "42", INCORRECT)],
)
def test_match_scores_the_first_submission(
    tmp_path: Path, first: str, second: str, expected: str
) -> None:
    log = _eval(
        Task(
            dataset=[Sample(input="What is the answer?", target="42")],
            solver=swarm(_members(first, second), channels=[]),
            scorer=match(),
        ),
        tmp_path,
    )
    assert _score(log).value == expected


def test_a_model_grader_sees_only_the_final_answer(tmp_path: Path) -> None:
    prompts: list[str] = []

    def grade(input: list[ChatMessage]) -> ModelOutput:
        prompts.append("\n".join(message.text for message in input))
        return output("GRADE: C")

    log = _eval(
        Task(
            dataset=[Sample(input="What is the capital of France?", target="Paris")],
            solver=swarm(_members("It is Paris.", "It is London."), channels=[]),
            scorer=model_graded_qa(model=scripted_model(Script(respond=grade))),
        ),
        tmp_path,
    )
    assert _score(log).value == CORRECT
    [prompt] = prompts
    assert "It is Paris." in prompt and "London" not in prompt


@agent
def writer(written: anyio.Event) -> Agent:
    # leaves its work in the sandbox, then works on until it is cancelled
    async def execute(state: AgentState) -> AgentState:
        await sandbox().write_file("result.txt", "done")
        written.set()
        await anyio.sleep(3600)
        return state

    return execute


@scorer(metrics=[accuracy()])
def file_check() -> Scorer:
    async def score(state: TaskState, target: Target) -> Score:
        text = await sandbox().read_file("result.txt")
        return Score(value=CORRECT if text == target.text else INCORRECT)

    return score


def test_a_file_scorer_scores_what_the_drained_swarm_left(tmp_path: Path) -> None:
    written = anyio.Event()

    async def run(control: SwarmControl) -> StopReason:
        control.start("writer")
        await written.wait()
        return "enough"

    log = _eval(
        Task(
            dataset=[Sample(input="Write the file.", target="done")],
            solver=swarm(
                member(writer(written), name="writer"),
                controller=_controller(run, "task_scorers_test_files"),
                channels=[],
                budget=Budget(grace=0),
            ),
            scorer=file_check(),
            sandbox="local",
        ),
        tmp_path,
    )
    assert log.samples is not None
    [sample] = log.samples
    # the writer was cancelled, so there is no answer, but its files are scored
    assert sample.output.empty
    assert _score(log).value == CORRECT
