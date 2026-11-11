from pathlib import Path
from typing import Any

import pytest
from inspect_ai import Task, eval  # pyright: ignore[reportUnknownVariableType]
from inspect_ai._util.registry import registry_info, registry_params
from inspect_ai.agent import Agent, AgentState, agent
from inspect_ai.dataset import Sample
from inspect_ai.log import read_eval_log
from inspect_ai.model import ModelOutput
from inspect_ai.scorer import match

from inspect_swarm import Verdict, answer_result, baseline, member, swarm_result

from .rescore_task import rescore_members, rescore_result, says


@agent
def answers_with(text: str) -> Agent:
    async def execute(state: AgentState) -> AgentState:
        state.output = ModelOutput.from_content("mockllm", text)
        return state

    return execute


async def _check(answer: str, metadata: dict[str, Any]) -> Verdict:
    return Verdict(passed=answer == "42", value=1.0)


def test_a_baseline_is_a_swarm_of_one_that_records_its_attempt(
    tmp_path: Path,
) -> None:
    result = answer_result(answer_key=rescore_result().answer_key, verifier=_check)
    solver = baseline(member(says("42"), name="solver"), result=result)
    assert registry_info(solver).name == "inspect_swarm/swarm"
    params = registry_params(solver)
    assert (params["channels"], params["controller"]) == (
        [],
        {
            "type": "swarm_controller",
            "name": "inspect_swarm/leaderless",
            "params": {"final": "first"},
        },
    )
    [run] = eval(
        Task(
            dataset=[Sample(id="s1", input="What is six times seven?", target="42")],
            solver=solver,
            scorer=[match(), rescore_members()],
        ),
        model="mockllm/model",
        log_dir=str(tmp_path),
        display="none",
    )
    log = read_eval_log(run.location)
    assert log.samples is not None
    [sample] = log.samples
    record = swarm_result(sample)
    assert record is not None and record.kind == "answer"
    [attempt] = record.members
    assert (attempt.name, attempt.submitted, attempt.answer_key, attempt.verdict) == (
        "solver",
        True,
        "42",
        Verdict(passed=True, value=1.0),
    )
    assert (record.final.mode, record.final.member) == ("first", "solver")
    assert sample.scores is not None
    # one attempt: the best and mean member are its value under the member scorer
    assert sample.scores["rescore_members"].value == {
        "best_member": 1.0,
        "mean_member": 1.0,
    }


@pytest.mark.parametrize(
    "members,count",
    [
        pytest.param(
            lambda: member(answers_with, args={"text": "42"}, count=2), 2, id="count"
        ),
        pytest.param(lambda: [says("42"), says("41")], 2, id="list"),
    ],
)
def test_a_baseline_of_more_than_one_member_is_refused(
    members: Any, count: int
) -> None:
    with pytest.raises(ValueError, match=f"stands for {count}"):
        baseline(members())


def test_a_baseline_of_a_persistent_member_is_refused() -> None:
    with pytest.raises(ValueError, match="member 'kept' is persistent"):
        baseline(member(says("42"), name="kept", persistent=True))
