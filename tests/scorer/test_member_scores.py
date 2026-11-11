import itertools
import json
import math
import os
import re
import shutil
import subprocess
import sys
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing, asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from inspect_ai import Task, eval, score  # pyright: ignore[reportUnknownVariableType]
from inspect_ai.agent import Agent, AgentState, agent
from inspect_ai.dataset import Sample
from inspect_ai.event import SandboxEvent, SpanBeginEvent
from inspect_ai.log import EvalLog, EvalSample, read_eval_log
from inspect_ai.model import ModelOutput
from inspect_ai.scorer import Score, Scorer, Target, match, scorer
from inspect_ai.solver import Solver, TaskState
from inspect_ai.util import ExecResult, SandboxEnvironment, sandbox
from inspect_ai.util._concurrency import init_concurrency
from inspect_ai.util._sandbox.local import LocalSandboxEnvironment

from inspect_swarm import (
    AnswerResult,
    FinalRecord,
    MemberResult,
    ResultSpec,
    StopReason,
    SwarmControl,
    SwarmResultRecord,
    Verdict,
    answer_result,
    artifact_result,
    baseline,
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
    """Answer what the sample's input gives this member: `answers: {"a": "1", ...}`. An answer `bare:<text>` is an output with only a completion, and no message."""

    async def execute(state: AgentState) -> AgentState:
        text = next(m.text for m in state.messages if "answers: " in m.text)
        answer: str = json.loads(text.split("answers: ", 1)[1])[name]
        if answer.startswith("bare:"):
            state.output = ModelOutput(model="mockllm", completion=answer[5:])
        else:
            state.output = ModelOutput.from_content("mockllm", answer)
        return state

    return execute


def _input(answers: dict[str, str]) -> str:
    return f"answers: {json.dumps(answers)}"


@scorer(metrics=[])
def graded(calls: list[TaskState]) -> Scorer:
    """Grade a completion by what it says: a number, `PASS` or `FAIL` as given, `badformat` as 0 with a reason, or `none`, `nan`, `dict`, `list` or `tuple` for a grade with no single value."""

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
        if text == "tuple":
            return Score(value=(1.0, 0.5))
        if text == "badformat":
            return Score(
                value=0.0,
                answer=text,
                explanation=f"graded {text}",
                reason="invalid_response_format",
            )
        if text in ("PASS", "FAIL"):
            return Score(value=text)
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
        {"a": "none", "b": "nan", "c": "dict", "d": "tuple"},
        {
            "a": ("unavailable", NAN, "scorer_returned_none"),
            "b": ("unavailable", NAN, "grader_failed"),
            "c": ("unavailable", NAN, "non_scalar_value"),
            "d": ("unavailable", NAN, "non_scalar_value"),
        },
    ),
    "reasons": (
        {"a": "badformat", "b": "1", "c": "", "d": "nan"},
        {
            "a": ("graded", 0.0, "invalid_response_format"),
            "b": ("graded", 1.0, None),
            "c": ("empty", 0.25, "empty_output"),
            "d": ("unavailable", NAN, "grader_failed"),
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
    f"permuted-{i}": dict(zip(NAMES, (*answers, "list"), strict=True))
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
        "reasons": (1.0, 1.25 / 3, False, 3, None),
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
        assert metadata["final"]["mode"] == "vote"
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


# Files standing in for branches: the scorer grades `artifact.txt`, the team's
# artifact, and each member's is `branches/<member>`.
TEAM = "0.25"


class _FileArtifacts:
    def __init__(self, log: list[tuple[Any, ...]]) -> None:
        self.log = log

    @asynccontextmanager
    async def materialize(self, member: str) -> AsyncGenerator[None]:
        team = await sandbox().read_file("artifact.txt")
        branch = await sandbox().read_file(f"branches/{member}")
        await sandbox().write_file("artifact.txt", branch)
        self.log.append(("enter", member))
        try:
            yield
        except Exception:
            self.log.append(("error", member))
            raise
        finally:
            await sandbox().write_file("artifact.txt", team)
            self.log.append(("exit", member))


@scorer(metrics=[])
def artifact_tests(log: list[tuple[Any, ...]]) -> Scorer:
    """Grade `artifact.txt`, which holds a number or `raise`."""

    async def score(state: TaskState, target: Target) -> Score:
        artifact = await sandbox().read_file("artifact.txt")
        log.append(("score", artifact, state.output.completion, state.messages))
        if artifact == "raise":
            raise RuntimeError("the tests crashed")
        return Score(value=float(artifact))

    return score


@scorer(metrics=MEMBER_METRICS)
def branch_members(log: list[tuple[Any, ...]]) -> Scorer:
    return member_scores(
        artifact_tests(log), artifact_result(member_artifacts=_FileArtifacts(log))
    )


def _artifact_sample(answers: dict[str, str], branches: dict[str, str]) -> Sample:
    return Sample(
        id="s1",
        input=_input(answers),
        files={"artifact.txt": TEAM}
        | {f"branches/{name}": text for name, text in branches.items()},
    )


def _artifact_eval(
    tmp_path: Path, sample: Sample, solver: Agent, scorers: list[Scorer]
) -> EvalSample:
    [run] = eval(
        Task(dataset=[sample], solver=solver, scorer=scorers, sandbox="local"),
        model="mockllm/model",
        log_dir=str(tmp_path),
        display="none",
    )
    return _samples(read_eval_log(run.location))["s1"]


def _all_but_d() -> Agent:
    async def run(control: SwarmControl) -> StopReason:
        for name in ("a", "b", "c"):
            control.start(name)
        async with aclosing(control.events()) as events:
            async for _ in events:
                pass
        return "d_left_pending"

    return _swarm(
        controller=controller(run, "member_scores_test_all_but_d"),
        result=artifact_result(member_artifacts=_FileArtifacts([])),
    )


def test_each_member_is_graded_with_its_artifact_in_place_after_the_team(
    tmp_path: Path,
) -> None:
    log: list[tuple[Any, ...]] = []
    # a submits no text, and the record leaves c's output out; d never starts
    sample = _artifact_eval(
        tmp_path,
        _artifact_sample(
            {"a": "", "b": "done", "c": "attachment://x", "d": "done"},
            {"a": "1", "b": "0", "c": "0.5", "d": "1"},
        ),
        _all_but_d(),
        [artifact_tests(log), branch_members(log)],
    )
    assert sample.error is None, sample.error
    # the team is graded first; then each member that started, in roster order, with
    # its own artifact and output, and the team's artifact put back after each
    assert [entry[:3] for entry in log] == [
        ("score", TEAM, ""),
        ("enter", "a"),
        ("score", "1", ""),
        ("exit", "a"),
        ("enter", "b"),
        ("score", "0", "done"),
        ("exit", "b"),
        ("enter", "c"),
        ("score", "0.5", ""),
        ("exit", "c"),
    ]
    messages = [[m.role for m in entry[3]] for entry in log[1:] if entry[0] == "score"]
    # a's empty completion still has its message; c's left-out output has none
    assert messages == [["user", "assistant"], ["user", "assistant"], ["user"]]
    result = _score(sample, "branch_members")
    assert result.value == {"best_member": 1.0, "mean_member": 0.5}
    metadata = result.metadata
    assert metadata is not None
    assert [(m["name"], m["outcome"], m["value"]) for m in metadata["members"]] == [
        ("a", "graded", 1.0),
        ("b", "graded", 0.0),
        ("c", "graded", 0.5),
        ("d", "not_started", None),
    ]
    assert (metadata["kind"], metadata["members_started"], metadata["complete"]) == (
        "artifact",
        3,
        True,
    )
    # each member's artifact is put in place and back within its span
    spans = {
        e.id: e.name
        for e in sample.events
        if isinstance(e, SpanBeginEvent) and e.name in NAMES
    }
    writes = [
        spans[e.span_id]
        for e in sample.events
        if isinstance(e, SandboxEvent)
        and e.action == "write_file"
        and e.span_id in spans
    ]
    assert writes == ["a", "a", "b", "b", "c", "c"]


@pytest.mark.parametrize(
    "failing,error",
    [
        pytest.param("scorer", "the tests crashed", id="scorer"),
        pytest.param("materialize", "FileNotFoundError", id="materialize"),
    ],
)
def test_an_error_while_a_member_is_graded_fails_the_sample(
    tmp_path: Path, failing: str, error: str
) -> None:
    log: list[tuple[Any, ...]] = []
    # b's artifact crashes the tests, or b has no branch for `materialize` to read
    branches = {"a": "1"} | ({"b": "raise"} if failing == "scorer" else {}) | {"c": "1"}
    sample = _artifact_eval(
        tmp_path,
        _artifact_sample({"a": "", "b": "", "c": ""}, branches),
        _swarm(
            ("a", "b", "c"), result=artifact_result(member_artifacts=_FileArtifacts([]))
        ),
        [branch_members(log)],
    )
    assert sample.error is not None and error in sample.error.message
    steps = [entry[:2] for entry in log if entry[0] != "score"]
    if failing == "scorer":
        # the team's artifact is put back for the error, and c is never graded
        assert steps == [
            ("enter", "a"),
            ("exit", "a"),
            ("enter", "b"),
            ("error", "b"),
            ("exit", "b"),
        ]
    else:
        assert steps == [("enter", "a"), ("exit", "a")]


@pytest.mark.parametrize(
    "solver,reason",
    [
        pytest.param("no_contract", "kind_not_declared"),
        pytest.param("answer", "kind_mismatch"),
        pytest.param("plain", None),
    ],
)
def test_member_artifacts_are_materialized_only_for_a_swarm_under_an_artifact_contract(
    tmp_path: Path, solver: str, reason: str | None
) -> None:
    built: Agent = {
        "no_contract": lambda: _swarm(("a", "b")),
        "answer": lambda: _swarm(("a", "b"), result=_result("answer")),
        "plain": lambda: from_input("a"),
    }[solver]()
    log: list[tuple[Any, ...]] = []
    sample = _artifact_eval(
        tmp_path,
        _artifact_sample({"a": "", "b": ""}, {"a": "1", "b": "1"}),
        built,
        [branch_members(log)],
    )
    result = _score(sample, "branch_members")
    assert result.reason == reason
    assert isinstance(result.value, dict)
    if reason is not None:
        assert all(_same(v, NAN) for v in result.value.values())
        assert log == []
    else:
        # a plain agent is graded on the environment as it left it
        assert result.value == {"best_member": 0.25, "mean_member": 0.25}
        assert [entry[:2] for entry in log] == [("score", TEAM)]


# The worked example in the docs, run as written against a git repository on the host.
DOCS = Path(__file__).parents[2] / "docs" / "scoring.qmd"


def _docs_member_branches(
    sandbox: Callable[[], SandboxEnvironment] | None = None,
) -> Any:
    [code] = [
        block
        for block in re.findall(r"```python\n(.*?)```", DOCS.read_text(), re.DOTALL)
        if "class MemberBranches" in block
    ]
    namespace: dict[str, Any] = {}
    exec(code, namespace)
    if sandbox is not None:
        namespace["sandbox"] = sandbox
    return namespace["MemberBranches"]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def _team_state(repo: Path) -> dict[str, Any]:
    return {
        "head": _git(repo, "symbolic-ref", "HEAD"),
        "status": _git(repo, "status", "--porcelain", "--ignored"),
        "files": {
            str(path.relative_to(repo)): path.read_text()
            for path in sorted(repo.rglob("*"))
            if path.is_file() and ".git" not in path.parts
        },
    }


@scorer(metrics=[])
def repo_tests(repo: str, log: list[tuple[str, list[str]]]) -> Scorer:
    """Grade the number in `answer.txt`, noting which files the checkout has and leaving one of its own."""

    async def score(state: TaskState, target: Target) -> Score:
        listing = await sandbox().exec(
            ["find", ".", "-name", ".git", "-prune", "-o", "-type", "f", "-print"],
            cwd=repo,
        )
        answer = await sandbox().read_file(f"{repo}/answer.txt")
        log.append((answer, sorted(f[2:] for f in listing.stdout.split())))
        await sandbox().write_file(f"{repo}/scorer.txt", "")
        return Score(value=float(answer))

    return score


@scorer(metrics=MEMBER_METRICS)
def repo_members(repo: str, log: list[tuple[str, list[str]]]) -> Scorer:
    branches = _docs_member_branches()(repo=repo)
    return member_scores(
        repo_tests(repo, log), artifact_result(member_artifacts=branches)
    )


def _member_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The team's checkout, with each member's committed work on its branch in its own worktree."""
    for name, value in {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@localhost",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@localhost",
    }.items():
        monkeypatch.setenv(name, value)
    repo = tmp_path / "testbed"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / ".gitignore").write_text("build/\n")
    (repo / "answer.txt").write_text("0")
    (repo / "keep.txt").write_text("kept")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    for name, answer in (("a", "1"), ("b", "0.5")):
        worktree = tmp_path / "worktrees" / name
        _git(repo, "worktree", "add", "-q", "-b", f"swarm/{name}", str(worktree))
        (worktree / "answer.txt").write_text(answer)
        if name == "b":
            # b stops ignoring build/ and commits a file where the team has an ignored one
            (worktree / "b.txt").write_text(name)
            (worktree / ".gitignore").write_text("")
            (worktree / "build").mkdir()
            (worktree / "build" / "out").write_text(name)
        _git(worktree, "add", "-A")
        _git(worktree, "commit", "-q", "-m", name)
        # work it left uncommitted is not its artifact
        (worktree / "answer.txt").write_text("0")
    # the team's checkout: changed, deleted, staged, untracked and ignored files
    (repo / "answer.txt").write_text(TEAM)
    (repo / "keep.txt").unlink()
    (repo / "staged.txt").write_text("staged")
    _git(repo, "add", "staged.txt")
    (repo / "notes.txt").write_text("notes")
    (repo / "build").mkdir()
    (repo / "build" / "out").write_text("built")
    return repo


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_the_docs_member_branches_grade_each_members_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _member_repo(tmp_path, monkeypatch)
    before = _team_state(repo)

    log: list[tuple[str, list[str]]] = []
    sample = _artifact_eval(
        tmp_path / "logs",
        Sample(id="s1", input=_input({"a": "", "b": ""})),
        _swarm(("a", "b"), result=artifact_result()),
        [repo_tests(str(repo), log), repo_members(str(repo), log)],
    )
    assert sample.error is None, sample.error
    # each member's checkout has its committed files, and the team's ignored ones
    committed = [".gitignore", "answer.txt", "build/out", "keep.txt"]
    assert log == [
        (TEAM, [".gitignore", "answer.txt", "build/out", "notes.txt", "staged.txt"]),
        ("1", committed),
        ("0.5", sorted([*committed, "b.txt"])),
    ]
    assert _score(sample, "repo_members").value == {
        "best_member": 1.0,
        "mean_member": 0.75,
    }
    # the team's checkout is back as the team scorer left it: its ignored file and its
    # staged one as they were
    after = _team_state(repo)
    assert after["files"] == before["files"] | {"scorer.txt": ""}
    assert after["head"] == before["head"]
    assert set(after["status"].splitlines()) == set(before["status"].splitlines()) | {
        "?? scorer.txt"
    }
    assert not (tmp_path / "testbed.team").exists()


class _ReplayingSandbox(LocalSandboxEnvironment):
    """Once `replay` is set, runs each command it may retry a second time, as a sandbox does when it loses a command's response."""

    replay = False

    async def exec(
        self,
        cmd: list[str],
        input: str | bytes | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        user: str | None = None,
        timeout: int | None = None,
        timeout_retry: bool = True,
        concurrency: bool = True,
    ) -> ExecResult[str]:
        args = (cmd, input, cwd, env, user, timeout, timeout_retry, concurrency)
        result = await super().exec(*args)
        if self.replay and timeout_retry:
            result = await super().exec(*args)
        return result


@pytest.mark.anyio
@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
async def test_the_docs_member_branches_never_run_a_restore_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _member_repo(tmp_path, monkeypatch)
    before = _team_state(repo)
    # the subprocess limiter is bound to the backend that created it
    init_concurrency()
    environment = _ReplayingSandbox()
    try:
        branches = _docs_member_branches(lambda: environment)(repo=str(repo))
        async with branches.materialize("a"):
            assert (repo / "answer.txt").read_text() == "1"
            environment.replay = True
    finally:
        environment.directory.cleanup()
    assert _team_state(repo) == before
    assert not (tmp_path / "testbed.team").exists()


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


def _pass_fail(value: object) -> float:
    return float(value == "PASS")


@scorer(metrics=MEMBER_METRICS)
def pass_fail_members(calls: list[TaskState]) -> Scorer:
    return member_scores(graded(calls), _result("answer"), value_to_float=_pass_fail)


def test_an_unscored_grade_stays_unavailable_under_a_converter_that_gives_zero(
    tmp_path: Path,
) -> None:
    calls: list[TaskState] = []
    log = _eval(
        tmp_path,
        [Sample(id="s1", input=_input({"a": "PASS", "b": "nan"}), target="1")],
        _swarm(("a", "b"), result=_result("answer")),
        [pass_fail_members(calls)],
    )
    result = _score(_samples(log)["s1"], "pass_fail_members")
    assert result.value == {"best_member": 1.0, "mean_member": 1.0}
    metadata = result.metadata
    assert metadata is not None
    assert (metadata["members_valued"], metadata["complete"]) == (1, False)
    b = metadata["members"][1]
    assert (b["outcome"], b["reason"]) == ("unavailable", "grader_failed")


@pytest.mark.parametrize("solver", ["swarm", "baseline", "plain"])
def test_an_output_with_only_a_completion_is_graded(
    tmp_path: Path, solver: str
) -> None:
    built = {
        "swarm": lambda: _swarm(("a",), result=_result("answer")),
        "baseline": lambda: baseline(
            member(from_input("a"), name="a"), result=_result("answer")
        ),
        "plain": lambda: from_input("a"),
    }[solver]()
    calls: list[TaskState] = []
    log = _eval(
        tmp_path,
        [Sample(id="s1", input=_input({"a": "bare:0.5"}), target="1")],
        built,
        [members(calls)],
    )
    result = _score(_samples(log)["s1"])
    assert result.value == {"best_member": 0.5, "mean_member": 0.5}
    # the member's output has no message, so the grade sees the sample's input alone,
    # as the swarm's answer would give it
    [state] = calls
    assert state.output.completion == "0.5"
    assert [m.role for m in state.messages] == ["user"]


def _persistent(
    name: str, status: str, completion: str, *, kept: bool = True
) -> dict[str, Any]:
    # answered 42, then 7, each keyed and verified; the last key left out unless kept
    last = (
        AnswerResult(answer_key="7", verdict=Verdict(False, 0.0), verdict_time=2.5)
        if kept
        else AnswerResult(
            verdict=Verdict(False, 0.0), verdict_time=2.5, left_out=["answer_key"]
        )
    )
    return MemberResult(
        name=name,
        role=None,
        state="ended",
        status=status,
        submitted=status == "submitted",
        time=3.0,
        persistent=True,
        answer_times=[1.0, 2.0],
        answer_results=[
            AnswerResult(answer_key="42", verdict=Verdict(True, 1.0), verdict_time=1.5),
            last,
        ],
        output=ModelOutput.from_content("mockllm", completion),
    ).model_dump(mode="json")


@scorer(metrics=MEMBER_METRICS)
def contracted_members(calls: list[TaskState], checked: list[str]) -> Scorer:
    def key(text: str) -> str | None:
        checked.append(f"key:{text}")
        return text

    async def verifier(answer: str, metadata: dict[str, Any]) -> Verdict:
        checked.append(f"verifier:{answer}")
        return Verdict(True)

    return member_scores(
        graded(calls), answer_result(answer_key=key, verifier=verifier)
    )


def test_a_persistent_members_key_and_verdict_are_its_last_answers_when_it_submitted(
    tmp_path: Path,
) -> None:
    old = _persistent("old", "submitted", "7")
    # a record written before answers kept their keys and verdicts
    del old["answer_results"]
    record = SwarmResultRecord(
        members=[],
        final=FinalRecord(
            mode="first", member="submitted", reason=None, provisional=False, answer=0
        ),
        kind="answer",
    ).model_dump(mode="json")
    record["members"] = [
        _persistent("submitted", "submitted", "7"),
        _persistent("stopped", "stopped", "8"),
        _persistent("left_out", "submitted", "7", kept=False),
        old,
    ]
    calls: list[TaskState] = []
    checked: list[str] = []
    log = _eval(
        tmp_path,
        [Sample(id="s1", input="task")],
        forge(record),
        [contracted_members(calls, checked)],
    )
    metadata = _score(_samples(log)["s1"], "contracted_members").metadata
    assert metadata is not None
    assert [
        (m["name"], m["outcome"], m["value"], m["answer_key"], m["verdict"])
        for m in metadata["members"]
    ] == [
        (
            "submitted",
            "graded",
            7.0,
            "7",
            {"passed": False, "value": 0.0, "explanation": None},
        ),
        ("stopped", "graded", 8.0, None, None),
        (
            "left_out",
            "graded",
            7.0,
            None,
            {"passed": False, "value": 0.0, "explanation": None},
        ),
        ("old", "graded", 7.0, None, None),
    ]
    # the record's values are read, never computed again
    assert checked == []
