import math
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any, Literal

from inspect_ai.model import ChatMessage, ChatMessageUser, ModelOutput
from inspect_ai.scorer import (
    Metric,
    Score,
    Scorer,
    Target,
    ValueToFloat,
    mean,
    stderr,
    value_to_float,
)
from inspect_ai.solver import TaskState
from inspect_ai.util import span

from .._record import MemberResult, SwarmResultRecord, swarm_result
from .._result import MemberArtifacts, ResultSpec, key_answer

MEMBER_METRICS: dict[str, list[Metric]] = {
    "best_member": [mean(), stderr()],
    "mean_member": [mean(), stderr()],
}
"""Metrics for a scorer that returns `member_scores()`: the mean and standard error of `best_member` and `mean_member`. Pass them to the task's own scorer factory, `@scorer(metrics=MEMBER_METRICS)`."""

# The plain agent's name, when the sample ran no swarm.
_PLAIN_AGENT = "solver"

_Outcome = Literal["not_started", "empty", "graded", "unavailable"]


@dataclass(frozen=True)
class _Grade:
    outcome: _Outcome
    value: float | None
    reason: str | None = None
    score: Score | None = None


def member_scores(
    scorer: Scorer,
    result: ResultSpec,
    *,
    value_to_float: ValueToFloat = value_to_float(),  # noqa: B008
    empty_value: float = 0.0,
) -> Scorer:
    """Score each member's result, for an answer task or an artifact task with member artifacts: the best member and the mean member.

    The scorer reads each member's output from the swarm's result record and grades it with `scorer`, as if that member had run alone. Its value is a dict, `{"best_member": ..., "mean_member": ...}`, over the members with a value. Its metadata holds each member's outcome and value, and the record's `final` and `kind`.

    It holds the task's callables, so Inspect cannot log or rebuild it as a scorer argument. Wrap it in a registered scorer factory of the task's own, with no arguments, which re-scoring rebuilds by name:

    ```python
    @scorer(metrics=MEMBER_METRICS)
    def math_members() -> Scorer:
        return member_scores(match(), math_result())
    ```

    List it after the task's own scorer, which stays the headline score.

    For an answer task, `scorer` must be a function of the answer: the output, the sample's input, metadata and target. It may use the sandbox to evaluate the answer, but never as a source of the answer, or it would credit the team's work to a member. It grades each member on a copy of the state, with the member's output as the output and the sample's input followed by the member's message as the messages. An output with only a completion and no message adds no message, as when the swarm returns it as its answer. Members are graded in roster order, each inside a span named after it:

    - A member that never started has no value (`not_started`), and is not graded.
    - A member whose output the record left out, or whose grade is `None`, NaN (`Score.unscored()`, or a value `value_to_float` converts to NaN) or not a single value (a dict, list or tuple), is unavailable: no value, with a reason (the grade's own `reason` for NaN, else `unscored`).
    - A member with no output, or one whose completion is only whitespace, gets `empty_value` without being graded.
    - Every other member gets its grade, converted with `value_to_float`, and the grade's own `reason`, such as `invalid_response_format`.

    For an artifact task whose contract has `member_artifacts`, `scorer` grades the environment, usually as the task's own scorer does. Each member that started is graded inside `member_artifacts.materialize(name)`, which puts that member's artifact where the scorer looks and restores the team's on exit. Every such member is graded, whatever its completion: a member can leave a passing artifact and submit no text. A member whose output the record left out is graded with an empty output. An error from `materialize` or the scorer fails the sample.

    A sample with no swarm, a plain agent, is scored as one member named `solver`, whose output is the sample's. For an artifact task it is graded on the environment as it left it, without `materialize`. A task whose result is an artifact with no `member_artifacts`, a swarm without the contract, or one with another kind, gives no member values; then both keys are NaN, which Inspect counts as unscored, with the reason `artifact_without_member_artifacts`, `kind_not_declared` or `kind_mismatch`. So does a sample with no member value, with the reason `no_member_valued`.

    The scorer returns `None` while `state.completed` is `False`: the solve can call the task's scorers (`react(attempts=...)` and other attempt loops, live control scoring), and the record does not exist yet.

    Args:
        scorer: The scorer that grades each member, usually the task's own.
        result: The task's result contract, the one it passes to `swarm(result=...)`.
        value_to_float: Converts a grade's value to a float.
        empty_value: The value of an empty output, for an answer task.

    Returns:
        The scorer.
    """

    async def score(state: TaskState, target: Target) -> Score | None:
        if not state.completed:
            return None
        record = swarm_result(state)
        members = (
            record.members
            if record is not None
            else [_plain_agent(state.output, result)]
        )
        refusal = _refusal(record, result)
        # a plain agent's artifact is the environment as it left it, and it has no
        # roster name to materialize
        artifacts = result.member_artifacts if record is not None else None
        grades: list[_Grade] = []
        for m in members:
            if m.state == "pending":
                grades.append(_Grade("not_started", None, "not_started"))
            elif refusal is not None:
                grades.append(_Grade("unavailable", math.nan, refusal))
            elif result.kind == "artifact":
                grades.append(
                    await _grade_artifact(
                        m, state, target, scorer, value_to_float, artifacts
                    )
                )
            else:
                grades.append(
                    await _grade_answer(
                        m, state, target, scorer, value_to_float, empty_value
                    )
                )
        valued = [
            g.value
            for g in grades
            if g.outcome in ("empty", "graded") and g.value is not None
        ]
        if valued:
            best, average = max(valued), sum(valued) / len(valued)
            reason = None
        else:
            best = average = math.nan
            reason = refusal or "no_member_valued"
        return Score(
            value={"best_member": best, "mean_member": average},
            reason=reason,
            metadata={
                "kind": record.kind if record is not None else None,
                "final": record.final.model_dump(mode="json")
                if record is not None
                else None,
                "members_total": len(members),
                "members_started": sum(g.outcome != "not_started" for g in grades),
                "members_valued": len(valued),
                "complete": all(g.outcome != "unavailable" for g in grades),
                "members": [
                    _member(m, g) for m, g in zip(members, grades, strict=True)
                ],
            },
        )

    return score


# The sample's output as one member, when no swarm ran: a plain agent, whose status is
# unknown. Its answer key is a pure function of its output; the verifier runs only
# during a swarm.
def _plain_agent(output: ModelOutput, result: ResultSpec) -> MemberResult:
    return MemberResult(
        name=_PLAIN_AGENT,
        role=None,
        state="ended",
        status=None,
        submitted=False,
        time=None,
        output=output,
        answer_key=None
        if result.answer_key is None
        else key_answer(result.answer_key, output.completion, _PLAIN_AGENT),
    )


# Why the members cannot be graded as answers, or None when they can. A plain agent has
# no record, so only the task's kind applies to it.
def _refusal(record: SwarmResultRecord | None, result: ResultSpec) -> str | None:
    if record is not None:
        if record.kind is None:
            return "kind_not_declared"
        if record.kind != result.kind:
            return "kind_mismatch"
    if result.kind == "artifact" and result.member_artifacts is None:
        return "artifact_without_member_artifacts"
    return None


async def _grade_answer(
    m: MemberResult,
    state: TaskState,
    target: Target,
    scorer: Scorer,
    to_float: ValueToFloat,
    empty_value: float,
) -> _Grade:
    # before the empty check, since a left-out output is None in the record
    if "output" in m.left_out:
        return _Grade("unavailable", math.nan, "output_left_out")
    if m.output is None or not m.output.completion.strip():
        return _Grade("empty", empty_value, "empty_output")
    return await _grade(m.name, m.output, state, target, scorer, to_float, None)


# Every member is graded on its artifact, whatever its completion, since a member can
# leave a passing artifact and submit no text; an output the record left out is graded
# as an empty one.
async def _grade_artifact(
    m: MemberResult,
    state: TaskState,
    target: Target,
    scorer: Scorer,
    to_float: ValueToFloat,
    artifacts: MemberArtifacts | None,
) -> _Grade:
    output = m.output if m.output is not None else ModelOutput()
    return await _grade(m.name, output, state, target, scorer, to_float, artifacts)


async def _grade(
    name: str,
    output: ModelOutput,
    state: TaskState,
    target: Target,
    scorer: Scorer,
    to_float: ValueToFloat,
    artifacts: MemberArtifacts | None,
) -> _Grade:
    copy = deepcopy(state)
    copy.output = output.model_copy(deep=True)
    # as the swarm gives its answer: an output with no choices, only a completion, adds
    # no message
    copy.messages = _input_messages(copy.input)
    if not copy.output.empty:
        copy.messages.append(copy.output.message)
    async with span(name=name):
        # restores the team's artifact on exit, including when the scorer raises
        async with (
            artifacts.materialize(name) if artifacts is not None else nullcontext()
        ):
            score = await scorer(copy, target)
    if score is None:
        return _Grade("unavailable", math.nan, "scorer_returned_none")
    if not isinstance(score.value, str | int | float | bool):
        return _Grade("unavailable", math.nan, "non_scalar_value", score)
    # before conversion, which may turn the unscored sentinel into a number
    if isinstance(score.value, float) and math.isnan(score.value):
        return _Grade("unavailable", math.nan, score.reason or "unscored", score)
    value = to_float(score.value)
    if math.isnan(value):
        return _Grade("unavailable", math.nan, score.reason or "unscored", score)
    # the grade's own reason, such as a format failure
    return _Grade("graded", value, score.reason, score)


def _input_messages(input: str | list[ChatMessage]) -> list[ChatMessage]:
    if isinstance(input, str):
        return [ChatMessageUser(content=input, source="input")]
    return list(input)


def _member(m: MemberResult, grade: _Grade) -> dict[str, Any]:
    answer_key, verdict = m.answer_key, m.verdict
    if m.persistent:
        # graded on its output, which is its last answer only when it ended submitted;
        # a record written before answers kept their keys and verdicts has none
        last = m.answer_results[-1] if m.submitted and m.answer_results else None
        answer_key = last.answer_key if last is not None else None
        verdict = last.verdict if last is not None else None
    return {
        "name": m.name,
        "outcome": grade.outcome,
        "value": grade.value,
        "answer": grade.score.answer if grade.score is not None else None,
        "explanation": grade.score.explanation if grade.score is not None else None,
        "reason": grade.reason,
        "state": m.state,
        "status": m.status,
        "submitted": m.submitted,
        "answer_key": answer_key,
        "verdict": asdict(verdict) if verdict is not None else None,
        "time": m.time,
    }
