"""A task whose member scorer is a registered factory in the task file, as re-scoring needs. Loaded by path, so it imports nothing from the tests."""

from inspect_ai import Task, task
from inspect_ai.agent import Agent, AgentState, agent
from inspect_ai.dataset import Sample
from inspect_ai.model import ModelOutput
from inspect_ai.scorer import Scorer, match, scorer

from inspect_swarm import ResultSpec, answer_result, leaderless, member, swarm
from inspect_swarm.scorer import MEMBER_METRICS, member_scores


def _key(text: str) -> str | None:
    return text.strip() or None


def rescore_result() -> ResultSpec:
    return answer_result(answer_key=_key)


@scorer(metrics=MEMBER_METRICS)
def rescore_members() -> Scorer:
    return member_scores(match(), rescore_result())


@agent
def says(text: str) -> Agent:
    async def execute(state: AgentState) -> AgentState:
        state.output = ModelOutput.from_content("mockllm", text)
        return state

    return execute


@task
def rescore_task() -> Task:
    return Task(
        dataset=[Sample(id="s1", input="What is six times seven?", target="42")],
        solver=swarm(
            [
                member(says("42"), name="right"),
                member(says("41"), name="wrong"),
                member(says(""), name="blank"),
            ],
            controller=leaderless(stop="end"),
            channels=[],
            result=rescore_result(),
        ),
        scorer=[match(), rescore_members()],
    )
