import itertools
import json
import math
import subprocess
import sys
from contextlib import AbstractAsyncContextManager, aclosing, nullcontext
from pathlib import Path
from typing import Any

import pytest
from inspect_ai import Task, eval, score  # pyright: ignore[reportUnknownVariableType]
from inspect_ai.agent import Agent, AgentState, agent
from inspect_ai.dataset import Sample
from inspect_ai.event import SpanBeginEvent
from inspect_ai.log import EvalLog, EvalSample, read_eval_log
from inspect_ai.model import ModelOutput
from inspect_ai.scorer import Score, Scorer, Target, match, scorer
from inspect_ai.solver import Solver, TaskState
from inspect_ai.util import sandbox

from inspect_swarm import (
    ResultSpec,
    StopReason,
    SwarmControl,
    answer_result,
    artifact_result,
    leaderless,
    member,
    swarm,
)
from inspect_swarm.scorer import MEMBER_METRICS, member_scores

from ..swarm_runtime import controller
from ..test_record import _VERSION_2, forge, writes_the_old_record
from .rescore_task import rescore_members, rescore_result

NAMES = ("a", "b", "c", "d")
TASK_FILE = Path(__file__).parent / "rescore_task.py"


def _key(text: str) -> str | None:
    return text.strip() or None


def _result(kind: str) -> ResultSpec:
    return answer_result(answer_key=_key) if kind == "answer" else artifact_result()


@agent
def from_input(name: str) -> Agent:
    """Answer what the sample's input gives this member: `answers: {"a": "1", ...}`."""

    async def execute(state: AgentState) -> AgentState:
        text = next(m.text for m in state.messages if "answers: " in m.text)
        answers = json.loads(text.split("answers: ", 1)[1])
        state.output = ModelOutput.from_content("mockllm", answers[name])
        return state

    return execute


def _input(answers: dict[str, str]) -> str:
    return f"answers: {json.dumps(answers)}"


@scorer(metrics=[])
def graded(calls: list[TaskState]) -> Scorer:
    """Grade a completion by what it says: a number, or `none`, `nan`, `dict` or `list` for a grade with no value."""

    async def score(state: TaskState, target: Target) -> Score | None:
        calls.append(state)
        text = state.output.completion
        if text == "none":
            return None
        if text == "nan":
            return Score.unscored(reason="grader_failed")
        if text == "dict":
            return Score(value={"x": 1.0})
        if text == "list":
            return Score(value=[1.0])
        return Score(value=float(text), answer=text, explanation=f"graded {text}")

    return score


@scorer(metrics=MEMBER_METRICS)
def members(
    calls: list[TaskState], kind: str = "answer", empty_value: float = 0.0
) -> Scorer:
    return member_scores(graded(calls), _result(kind), empty_value=empty_value)


@scorer(metrics=MEMBER_METRICS)
def artifact_members(calls: list[TaskState]) -> Scorer:
    return member_scores(graded(calls), _result("artifact"))


def _swarm(names: tuple[str, ...] = NAMES, **kwargs: Any) -> Agent:
    return swarm(
        [member(from_input(name), name=name) for name in names],
        controller=kwargs.pop("controller", leaderless(stop_on_submission=False)),
        channels=[],
        **kwargs,
    )


def _eval(
    tmp_path: Path, samples: list[Sample], solver: Agent | Solver, scorers: list[Scorer]
) -> EvalLog:
    [log] = eval(
        Task(dataset=samples, solver=solver, scorer=scorers),
        model="mockllm/model",
        log_dir=str(tmp_path),
        display="none",
    )
    # read back, so that every value is as the log keeps it
    return read_eval_log(log.location)


def _samples(log: EvalLog) -> dict[str, EvalSample]:
    assert log.samples is not None
    return {str(s.id): s for s in log.samples}


def _score(sample: EvalSample, name: str = "members") -> Score:
    assert sample.scores is not None
    return sample.scores[name]


def _same(actual: object, expected: float | None) -> bool:
    if expected is None or actual is None:
        return actual is expected
    assert isinstance(actual, float)
    return math.isnan(actual) if math.isnan(expected) else actual == expected


NAN = math.nan

# id: (answers, {member: (outcome, value, reason)})
GRADES: dict[str, tuple[dict[str, str], dict[str, tuple[str, float, str | None]]]] = {
    "graded": (
        {"a": "1", "b": "0.5", "c": " ", "d": "0"},
        {
            "a": ("graded", 1.0, None),
            "b": ("graded", 0.5, None),
            "c": ("empty", 0.25, "empty_output"),
            "d": ("graded", 0.0, None),
        },
    ),
    "unavailable": (
        {"a": "none", "b": "nan", "c": "dict", "d": "list"},
        {
            "a": ("unavailable", NAN, "scorer_returned_none"),
            "b": ("unavailable", NAN, "grader_failed"),
            "c": ("unavailable", NAN, "non_scalar_value"),
            "d": ("unavailable", NAN, "non_scalar_value"),
        },
    ),
    "left_out": (
        {"a": "0.5", "b": "1", "c": "", "d": "attachment://x"},
        {
            "a": ("graded", 0.5, None),
            "b": ("graded", 1.0, None),
            "c": ("empty", 0.25, "empty_output"),
            "d": ("unavailable", NAN, "output_left_out"),
        },
    ),
}

# every placement of one valued, one unavailable and one empty member, beside a fourth
# that is unavailable, gives the same aggregates
PERMUTED: dict[str, dict[str, str]] = {
    f"permuted-{i}": dict(zip(NAMES, (*answers, "nan"), strict=True))
    for i, answers in enumerate(itertools.permutations(("1", "none", " ")))
}


def test_each_member_is_graded_and_aggregated_over_the_members_with_a_value(
    tmp_path: Path,
) -> None:
    calls: list[TaskState] = []
    answers = {id: case[0] for id, case in GRADES.items()} | PERMUTED
    log = _eval(
        tmp_path,
        [
            Sample(id=id, input=_input(given), target="1")
            for id, given in answers.items()
        ],
        _swarm(result=_result("answer")),
        [match(), members(calls, empty_value=0.25)],
    )
    samples = _samples(log)
    expected = {
        "graded": (1.0, 1.75 / 4, True, 4, None),
        "unavailable": (NAN, NAN, False, 0, "no_member_valued"),
        "left_out": (1.0, 1.75 / 3, False, 3, None),
    } | {id: (1.0, 1.25 / 2, False, 2, None) for id in PERMUTED}
    for id, (best, mean, complete, valued, reason) in expected.items():
        result = _score(samples[id])
        assert isinstance(result.value, dict)
        assert _same(result.value["best_member"], best), id
        assert _same(result.value["mean_member"], mean), id
        assert result.reason == reason, id
        metadata = result.metadata
        assert metadata is not None
        assert (metadata["complete"], metadata["members_valued"]) == (complete, valued)
        assert (metadata["members_total"], metadata["members_started"]) == (4, 4)
        assert metadata["kind"] == "answer"
        assert metadata["final"]["mode"] == "first"
        # the copies' scores are not the sample's
        assert set(samples[id].scores or {}) == {"match", "members"}
    for id, (_, outcomes) in GRADES.items():
        metadata = _score(samples[id]).metadata
        assert metadata is not None
        for m in metadata["members"]:
            outcome, value, reason = outcomes[m["name"]]
            assert (m["outcome"], m["reason"]) == (outcome, reason), (id, m)
            assert _same(m["value"], value), (id, m)
            # an answer key that reads as an attachment is left out with its output
            left_out = reason == "output_left_out"
            assert m["answer_key"] == (
                None if left_out else _key(answers[id][m["name"]])
            ), (id, m)
            assert m["verdict"] is None and m["state"] == "ended"
            if outcome == "graded":
                answer = answers[id][m["name"]]
                assert (m["answer"], m["explanation"]) == (answer, f"graded {answer}")
    # each grade sees the sample's input and the member's answer, after the solve; no
    # empty or left-out output reaches the scorer
    for state in calls:
        assert state.completed
        completion = state.output.completion
        assert completion.strip() and not completion.startswith("attachment://")
        assert [m.role for m in state.messages] == ["user", "assistant"]
        assert state.messages[0].text.startswith("answers: ")
        assert state.messages[1].text == completion
    graded_names = {
        id: {name for name, answer in given.items() if answer.strip()} - {"d"}
        if id == "left_out"
        else {name for name, answer in given.items() if answer.strip()}
        for id, given in answers.items()
    }
    assert len(calls) == sum(len(names) for names in graded_names.values())
    # each grade runs in a span named after its member, within the scorer's span
    for id, sample in samples.items():
        [scorer_span] = [
            e
            for e in sample.events
            if isinstance(e, SpanBeginEvent)
            and e.type == "scorer"
            and e.name == "members"
        ]
        assert {
            e.name
            for e in sample.events
            if isinstance(e, SpanBeginEvent) and e.parent_id == scorer_span.id
        } == graded_names[id], id


def test_a_member_that_never_started_has_no_value_and_leaves_the_sample_complete(
    tmp_path: Path,
) -> None:
    async def run(control: SwarmControl) -> StopReason:
        control.start("a")
        async with aclosing(control.events()) as events:
            async for _ in events:
                return "a_only"
        return "a_only"

    calls: list[TaskState] = []
    log = _eval(
        tmp_path,
        [Sample(id="s1", input=_input({"a": "1", "b": "0"}), target="1")],
        _swarm(
            ("a", "b"),
            controller=controller(run, "member_scores_test_a_only"),
            result=_result("answer"),
        ),
        [members(calls)],
    )
    result = _score(_samples(log)["s1"])
    assert result.value == {"best_member": 1.0, "mean_member": 1.0}
    metadata = result.metadata
    assert metadata is not None
    assert (
        metadata["members_total"],
        metadata["members_started"],
        metadata["members_valued"],
        metadata["complete"],
    ) == (2, 1, 1, True)
    b = metadata["members"][1]
    assert (b["name"], b["outcome"], b["value"], b["reason"], b["state"]) == (
        "b",
        "not_started",
        None,
        "not_started",
        "pending",
    )
    assert [state.output.completion for state in calls] == ["1"]


@pytest.mark.parametrize(
    "solver,answer_reason,artifact_reason",
    [
        pytest.param("no_contract", "kind_not_declared", "kind_not_declared"),
        pytest.param("artifact", "kind_mismatch", "artifact_without_member_artifacts"),
        pytest.param("plain", None, "artifact_without_member_artifacts"),
    ],
)
def test_members_are_graded_only_under_a_matching_answer_contract(
    tmp_path: Path, solver: str, answer_reason: str | None, artifact_reason: str
) -> None:
    built: Agent = {
        "no_contract": lambda: _swarm(("a", "b")),
        "artifact": lambda: _swarm(("a", "b"), result=_result("artifact")),
        "plain": lambda: from_input("a"),
    }[solver]()
    answer_calls: list[TaskState] = []
    artifact_calls: list[TaskState] = []
    log = _eval(
        tmp_path,
        [Sample(id="s1", input=_input({"a": "1", "b": "0"}), target="1")],
        built,
        [members(answer_calls), artifact_members(artifact_calls)],
    )
    sample = _samples(log)["s1"]
    for name, reason, calls in (
        ("members", answer_reason, answer_calls),
        ("artifact_members", artifact_reason, artifact_calls),
    ):
        result = _score(sample, name)
        metadata = result.metadata
        assert metadata is not None
        assert result.reason == reason
        if reason is None:
            continue
        assert isinstance(result.value, dict)
        assert all(_same(v, NAN) for v in result.value.values())
        assert {(m["outcome"], m["reason"]) for m in metadata["members"]} == {
            ("unavailable", reason)
        }
        assert not metadata["complete"]
        assert calls == []
    if solver == "plain":
        # no swarm: the sample's output is one member, keyed and never verified
        result = _score(sample)
        assert result.value == {"best_member": 1.0, "mean_member": 1.0}
        metadata = result.metadata
        assert metadata is not None
        assert (metadata["kind"], metadata["final"]) == (None, None)
        [plain] = metadata["members"]
        assert (
            plain["name"],
            plain["outcome"],
            plain["answer_key"],
            plain["verdict"],
            plain["status"],
        ) == ("solver", "graded", "1", None, None)


class _Artifacts:
    def materialize(self, member: str) -> AbstractAsyncContextManager[None]:
        return nullcontext()


def test_member_artifacts_are_refused() -> None:
    with pytest.raises(ValueError, match="member artifacts"):
        member_scores(match(), artifact_result(member_artifacts=_Artifacts()))


@pytest.mark.parametrize(
    "solver,error",
    [
        pytest.param(forge(_VERSION_2), "can read", id="version-2"),
        pytest.param(
            forge({"version": 1, "members": "a, b", "final": {}}),
            "can read",
            id="malformed",
        ),
        pytest.param(writes_the_old_record(), "inspect_swarm:result", id="old-store"),
    ],
)
def test_a_record_this_inspect_swarm_cannot_read_fails_scoring(
    tmp_path: Path, solver: Solver, error: str
) -> None:
    calls: list[TaskState] = []
    scored = _samples(
        _eval(tmp_path, [Sample(id="s1", input="task")], solver, [members(calls)])
    )["s1"]
    assert scored.error is not None and error in scored.error.message
    # never scored as a plain agent when the log is re-scored
    log = _eval(tmp_path / "unscored", [Sample(id="s1", input="task")], solver, [])
    with pytest.raises(ValueError, match=error):
        score(log, members(calls), display="none")
    assert calls == []


# As `inspect score` re-scores a log: the scorers rebuilt from their logged names, the
# task's own from its file.
RESCORE = """
import json, sys
from inspect_ai import score
from inspect_ai._eval.score import resolve_scorers
from inspect_ai.log import read_eval_log

log = read_eval_log(sys.argv[1])
[sample] = score(log, resolve_scorers(log), action="overwrite", display="none").samples
print(json.dumps({name: s.model_dump(mode="json", include={"value", "reason", "metadata"}) for name, s in sample.scores.items()}))
"""


def _dumped(sample: EvalSample) -> dict[str, Any]:
    assert sample.scores is not None
    return {
        name: s.model_dump(mode="json", include={"value", "reason", "metadata"})
        for name, s in sample.scores.items()
    }


def test_rescoring_rebuilds_the_task_files_member_scorer(tmp_path: Path) -> None:
    [run] = eval(
        f"{TASK_FILE}@rescore_task",
        model="mockllm/model",
        log_dir=str(tmp_path),
        display="none",
    )
    assert run.status == "success", run.error
    log = read_eval_log(run.location)
    assert log.samples is not None
    [sample] = log.samples
    original = _dumped(sample)
    assert original["rescore_members"]["value"] == {
        "best_member": 1.0,
        "mean_member": 1.0 / 3,
    }
    # in a fresh process, as `inspect score` does; from where the eval ran, since the
    # log names the task file relative to it
    rescored = subprocess.run(
        [sys.executable, "-c", RESCORE, run.location],
        capture_output=True,
        text=True,
        cwd=Path.cwd(),
    )
    assert rescored.returncode == 0, rescored.stderr
    assert json.loads(rescored.stdout) == original
    # with freshly built scorers
    [again] = score(log, [rescore_members()], display="none").samples or []
    assert _dumped(again)["rescore_members"] == original["rescore_members"]


@scorer(metrics=[])
def reads_the_sandbox() -> Scorer:
    async def score(state: TaskState, target: Target) -> Score:
        await sandbox().read_file("answer.txt")
        return Score(value=1)

    return score


@scorer(metrics=MEMBER_METRICS)
def sandbox_members() -> Scorer:
    return member_scores(reads_the_sandbox(), rescore_result())


def test_rescoring_a_scorer_that_needs_the_sandbox_raises(tmp_path: Path) -> None:
    [run] = eval(
        f"{TASK_FILE}@rescore_task",
        model="mockllm/model",
        log_dir=str(tmp_path),
        display="none",
    )
    with pytest.raises(ProcessLookupError):
        score(read_eval_log(run.location), sandbox_members(), display="none")


@scorer(metrics=[])
def extracted_code(recover: bool) -> Scorer:
    """Grade the last fenced block of the answer, Frontier-CS-shaped: with `recover`, an empty block falls back to the team's file in the sandbox; without it, the answer-only adapter, an empty block scores 0."""

    async def score(state: TaskState, target: Target) -> Score:
        blocks = state.output.completion.split("```")
        code = blocks[-2].strip() if len(blocks) >= 3 else ""
        if not code and recover:
            code = (await sandbox().read_file("solution.txt")).strip()
        return Score(value=1.0 if code == target.text else 0.0)

    return score


@scorer(metrics=MEMBER_METRICS)
def code_members(recover: bool) -> Scorer:
    return member_scores(extracted_code(recover), _result("answer"))


@pytest.mark.parametrize("recover,credited", [(True, 1.0), (False, 0.0)])
def test_an_answer_only_scorer_never_credits_a_member_with_the_teams_files(
    tmp_path: Path, recover: bool, credited: float
) -> None:
    # a holds an empty fenced block, and the team's file in the sandbox passes
    answers = {"a": "```\n```", "b": "```\n41\n```"}
    [run] = eval(
        Task(
            dataset=[
                Sample(
                    id="s1",
                    input=_input(answers),
                    target="42",
                    files={"solution.txt": "42"},
                )
            ],
            solver=_swarm(("a", "b"), result=_result("answer")),
            scorer=code_members(recover),
            sandbox="local",
        ),
        model="mockllm/model",
        log_dir=str(tmp_path),
        display="none",
    )
    assert run.status == "success", run.error
    result = _score(_samples(read_eval_log(run.location))["s1"], "code_members")
    assert result.metadata is not None
    assert [m["value"] for m in result.metadata["members"]] == [credited, 0.0]
