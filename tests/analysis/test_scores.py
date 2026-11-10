import math
import os
from pathlib import Path
from typing import Any, cast

import anyio
import pytest
from inspect_ai import (
    Epochs,
    Task,
    eval,  # pyright: ignore[reportUnknownVariableType]
)
from inspect_ai.agent import Agent, AgentState, agent, as_solver, react
from inspect_ai.analysis import SampleSummary, samples_df
from inspect_ai.dataset import Sample
from inspect_ai.log import EvalLog, read_eval_log
from inspect_ai.log._samples import sample_active
from inspect_ai.model import ModelOutput
from inspect_ai.scorer import (
    Score,
    Scorer,
    Target,
    accuracy,
    match,
    mean,
    pass_at,
    scorer,
)
from inspect_ai.solver import TaskState
from inspect_ai.util import store

from inspect_swarm import (
    ResultKind,
    Verdict,
    answer_result,
    artifact_result,
    baseline,
    member,
)
from inspect_swarm._record import FinalRecord, MemberResult
from inspect_swarm.analysis import AttemptRow, SwarmColumns, attempt_rows, select_at_k
from inspect_swarm.scorer import MEMBER_METRICS, member_scores
from tests.scorer.rescore_task import rescore_members, says
from tests.swarm_runtime import Script, scripted_model

# Synthetic rows: one sample's baseline() epochs, built as attempt_rows() gives them.


def _attempt(
    epoch: int,
    value: float | None,
    *,
    time: float | None = None,
    submitted: bool = True,
    key: str | None = None,
    verdict: Verdict | None = None,
    final_verdict: Verdict | None = None,
    kind: ResultKind | None = "answer",
    id: str = "s1",
    log: str = "baseline.eval",
    members: int = 1,
    persistent: bool = False,
    record: bool = True,
) -> AttemptRow:
    solver = MemberResult(
        name="solver",
        role=None,
        state="ended",
        status="submitted" if submitted else "unsubmitted",
        submitted=submitted,
        time=float(epoch) if time is None else time,
        answer_key=key,
        verdict=verdict,
        persistent=persistent,
    )
    roster = [solver.model_copy(update={"name": f"solver-{i}"}) for i in range(members)]
    final = FinalRecord(mode=None, member=None, reason=None, provisional=False)
    return AttemptRow(
        log=log,
        id=id,
        epoch=epoch,
        task_args={},
        result_kind=kind if record else None,
        final_value=value,
        final=final if record else None,
        final_verdict=final_verdict,
        best_member=value,
        mean_member=value,
        complete=True,
        members=roster if record else [],
        members_total=members if record else None,
        members_started=members if record else None,
        sample_total_usage={},
        target_assisted=False,
        no_solve_usage=True,
    )


_PASS = Verdict(passed=True)
_FAIL = Verdict(passed=False)


@pytest.mark.parametrize(
    "attempts,k,chain,expected",
    [
        pytest.param(
            [_attempt(1, 0, time=3), _attempt(2, 1, time=1), _attempt(3, 0.5, time=2)],
            2,
            "first",
            (1 + 0.5 + 1) / 3,
            id="first-takes-the-earliest-record-time",
        ),
        pytest.param(
            [_attempt(1, 0, time=3), _attempt(2, 1, time=1), _attempt(3, 0.5, time=2)],
            1,
            ["first"],
            0.5,
            id="k-1-is-the-mean-attempt",
        ),
        pytest.param(
            [_attempt(1, 0, time=3), _attempt(2, 1, time=1), _attempt(3, 0.5, time=2)],
            2,
            ["last"],
            (0 + 0 + 0.5) / 3,
            id="last-takes-the-latest-record-time",
        ),
        pytest.param(
            [_attempt(1, 0, time=1), _attempt(2, 1, time=1)],
            2,
            ["first"],
            0,
            id="equal-times-go-to-the-earlier-epoch",
        ),
        pytest.param(
            [
                _attempt(1, 0, verdict=Verdict(passed=True, value=0.5)),
                _attempt(2, 1, verdict=Verdict(passed=True, value=0.9)),
                _attempt(3, 1, verdict=_FAIL),
            ],
            2,
            ["verify"],
            (1 + 0 + 1) / 3,
            id="verify-takes-the-best-passed-verdict",
        ),
        pytest.param(
            [
                _attempt(1, 0, verdict=Verdict(passed=True)),
                _attempt(2, 1, verdict=Verdict(passed=True, value=0.1)),
            ],
            2,
            ["verify"],
            1,
            id="verify-ranks-no-value-below-a-number",
        ),
        pytest.param(
            [_attempt(1, 1, verdict=_FAIL), _attempt(2, 1, verdict=_FAIL)],
            2,
            ["verify"],
            0,
            id="strict-verify-with-none-passed-is-empty",
        ),
        pytest.param(
            [_attempt(1, 1, verdict=_FAIL), _attempt(2, 0, verdict=_FAIL)],
            2,
            ["verify", "vote", "first"],
            1,
            id="the-default-chain-falls-through-to-first",
        ),
        pytest.param(
            [_attempt(1, 0, key="a"), _attempt(2, 1, key="b"), _attempt(3, 1, key="b")],
            3,
            ["vote"],
            1,
            id="vote-takes-the-plurality",
        ),
        pytest.param(
            [_attempt(1, 0, key="a"), _attempt(2, 1, key="b"), _attempt(3, 1, key="b")],
            2,
            ["vote"],
            (0 + 0 + 1) / 3,
            id="a-tied-vote-goes-to-the-earliest-group",
        ),
        pytest.param(
            [_attempt(1, 0), _attempt(2, 1, key="b")],
            2,
            ["vote"],
            1,
            id="attempts-without-a-key-do-not-vote",
        ),
    ],
)
def test_select_at_k_applies_the_chain_to_every_subset(
    attempts: list[AttemptRow], k: int, chain: str | list[str], expected: float
) -> None:
    assert select_at_k(attempts, k, chain) == pytest.approx(expected)


def test_select_at_k_never_selects_an_unsubmitted_attempt() -> None:
    attempts = [_attempt(1, 1, submitted=False), _attempt(2, 0)]
    assert select_at_k(attempts, 2, "first") == 0
    # a subset with no submission has no answer, valued at empty_value
    assert select_at_k(attempts, 1, "first", empty_value=0.25) == pytest.approx(
        (0.25 + 0) / 2
    )


def test_select_at_k_is_nan_for_a_pool_smaller_than_k() -> None:
    # out of the pool: an attempt with no task score, and a sample with no record
    attempts = [_attempt(1, 1), _attempt(2, None), _attempt(3, 1, record=False)]
    assert select_at_k(attempts, 1, "first") == 1
    assert math.isnan(select_at_k(attempts, 2, "first"))
    assert math.isnan(select_at_k([], 1, "first"))


def test_select_at_k_samples_subsets_above_max_subsets() -> None:
    # 20 attempts, 10 at a time: C(20, 10) = 184,756 subsets, above the 10,000 default
    n, k = 20, 10
    times = {epoch: float((7 * epoch) % 23) for epoch in range(1, n + 1)}
    values = {epoch: (epoch % 3) / 2 for epoch in range(1, n + 1)}
    attempts = [_attempt(e, values[e], time=times[e]) for e in range(1, n + 1)]
    # under first, the i-th earliest of n is the earliest of a random k-subset with
    # probability C(n - i, k - 1) / C(n, k)
    by_time = [values[e] for e in sorted(times, key=times.__getitem__)]
    exact = sum(
        v * math.comb(n - i, k - 1) / math.comb(n, k)
        for i, v in enumerate(by_time, start=1)
    )
    sampled = select_at_k(attempts, k, "first")
    assert sampled == pytest.approx(exact, abs=0.03)
    assert select_at_k(attempts, k, "first") == sampled
    assert select_at_k(attempts, k, "first", seed=1) != sampled


@pytest.mark.parametrize(
    "attempts,expected",
    [
        pytest.param(
            [_attempt(1, 1, submitted=False), _attempt(2, 0)],
            1,
            id="an-unsubmitted-attempt-left-an-environment",
        ),
        pytest.param(
            [
                _attempt(1, 0, verdict=_PASS, final_verdict=_FAIL),
                _attempt(2, 1, verdict=_FAIL, final_verdict=_PASS),
            ],
            1,
            id="verify-reads-the-final-verdict",
        ),
        pytest.param(
            [_attempt(1, 1, final_verdict=_FAIL), _attempt(2, 0, final_verdict=_FAIL)],
            1,
            id="a-strict-chain-still-chooses-an-environment",
        ),
    ],
)
def test_artifact_select_at_k_chooses_an_environment(
    attempts: list[AttemptRow], expected: float
) -> None:
    attempts = [
        _attempt(
            a.epoch,
            a.final_value,
            submitted=a.members[0].submitted,
            verdict=a.members[0].verdict,
            final_verdict=a.final_verdict,
            kind="artifact",
        )
        for a in attempts
    ]
    # empty_value is never used for an artifact task
    assert select_at_k(attempts, 2, "verify", empty_value=-1.0) == expected


@pytest.mark.parametrize(
    "attempts,k,chain,options,message",
    [
        pytest.param([_attempt(1, 1)], 0, "first", {}, "k must be", id="k"),
        pytest.param(
            [_attempt(1, 1)],
            1,
            "first",
            {"max_subsets": 0},
            "max_subsets must be",
            id="max-subsets",
        ),
        pytest.param([_attempt(1, 1)], 1, [], {}, "chain is empty", id="empty-chain"),
        pytest.param(
            [_attempt(1, 1)],
            1,
            ["synthesize"],
            {},
            "'synthesize', which select@k cannot apply",
            id="synthesize",
        ),
        pytest.param(
            [_attempt(1, 1), _attempt(2, 1, id="s2")],
            1,
            "first",
            {},
            "more than one sample",
            id="two-samples",
        ),
        pytest.param(
            [_attempt(1, 1), _attempt(1, 1, log="retry.eval")],
            1,
            "first",
            {},
            "repeat an epoch",
            id="repeated-epoch",
        ),
        pytest.param(
            [_attempt(1, 1, members=2)],
            1,
            "first",
            {},
            "has 2 members",
            id="a-swarm-arm",
        ),
        pytest.param(
            [_attempt(1, 1, persistent=True)],
            1,
            "first",
            {},
            "persistent member",
            id="persistent",
        ),
        pytest.param(
            [_attempt(1, 1), _attempt(2, 1, kind="artifact", log="artifact.eval")],
            1,
            "first",
            {},
            "disagree on result_kind: 'answer' in baseline.eval; 'artifact' in artifact.eval",
            id="kinds-disagree",
        ),
    ],
)
def test_select_at_k_refuses(
    attempts: list[AttemptRow],
    k: int,
    chain: str | list[str],
    options: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        select_at_k(attempts, k, chain, **options)


# Written logs: baseline() arms run with eval(), read back with attempt_rows().

# Per epoch: the member's answer and how long it takes, and how long the grader takes.
# Epoch 2 answers first and is graded slowest, so its working time is the longer one.
_ANSWERS = {1: ("42", 0.3), 2: ("41", 0.0)}
_GRADING = {1: 0.0, 2: 1.0}


@agent
def answers_by_epoch() -> Agent:
    async def execute(state: AgentState) -> AgentState:
        active = sample_active()
        assert active is not None
        answer, delay = _ANSWERS[active.epoch]
        await anyio.sleep(delay)
        state.output = ModelOutput.from_content("mockllm", answer)
        return state

    return execute


@scorer(metrics=[accuracy()])
def slow_match() -> Scorer:
    inner = match()

    async def score(state: TaskState, target: Target) -> Score | None:
        await anyio.sleep(_GRADING[state.epoch])
        return await inner(state, target)

    return score


async def _is_42(answer: str, metadata: dict[str, Any]) -> Verdict:
    return Verdict(passed=answer == "42", value=1.0)


def _answer_key(text: str) -> str | None:
    return text.strip() or None


def _run(task: Task, log_dir: Path) -> EvalLog:
    [log] = eval(task, model="mockllm/model", log_dir=str(log_dir), display="none")
    assert log.status == "success", log.error
    return log


def _answer_arm(log_dir: Path) -> EvalLog:
    return _run(
        Task(
            dataset=[Sample(id="s1", input="What is six times seven?", target="42")],
            solver=baseline(
                answers_by_epoch(),
                result=answer_result(answer_key=_answer_key, verifier=_is_42),
            ),
            scorer=[slow_match(), rescore_members()],
            epochs=2,
        ),
        log_dir,
    )


def test_attempt_rows_read_a_baseline_arm(tmp_path: Path) -> None:
    log = _answer_arm(tmp_path)
    rows = attempt_rows(
        [log.location], scorer="slow_match", member_scorer="rescore_members"
    )
    assert [(r.log, r.id, r.epoch) for r in rows] == [
        (log.location, "s1", 1),
        (log.location, "s1", 2),
    ]
    first, second = rows
    assert (first.final_value, second.final_value) == (1.0, 0.0)
    assert (first.best_member, first.mean_member, first.complete) == (1.0, 1.0, True)
    assert (second.best_member, second.mean_member) == (0.0, 0.0)
    for row, key, passed in [(first, "42", True), (second, "41", False)]:
        assert row.result_kind == "answer"
        assert row.task_args == {}
        assert row.final is not None
        assert (row.final.mode, row.final.member) == ("first", "answers_by_epoch")
        assert row.final_verdict is None
        assert (row.members_total, row.members_started) == (1, 1)
        [solver] = row.members
        assert (solver.submitted, solver.answer_key) == (True, key)
        assert solver.verdict == Verdict(passed=passed, value=1.0)
        assert (row.target_assisted, row.no_solve_usage) == (False, True)
        assert row.sample_total_usage == {}

    # the answer the task scorer passed, by the verdict, and the attempt that submitted
    # first, by its record time, though the grader made its working time the longer
    working = [s.working_time or 0.0 for s in read_eval_log(log.location).samples or []]
    assert working[0] < working[1]
    assert (first.members[0].time or 0.0) > (second.members[0].time or 0.0)
    assert select_at_k(rows, 2, ["verify", "vote", "first"]) == 1.0
    assert select_at_k(rows, 2, "first") == 0.0
    assert select_at_k(rows, 1, "first") == 0.5


# Per epoch: the environment the member leaves, which the task scorer reads and the
# member's (empty) answer does not change.
_ENVIRONMENT = {1: ("done", 0.0), 2: ("", 1.0)}


@agent
def edits_environment() -> Agent:
    async def execute(state: AgentState) -> AgentState:
        active = sample_active()
        assert active is not None
        answer, environment = _ENVIRONMENT[active.epoch]
        store().set("environment", environment)
        state.output = ModelOutput.from_content("mockllm", answer)
        return state

    return execute


@scorer(metrics=[mean()])
def environment() -> Scorer:
    async def score(state: TaskState, target: Target) -> Score:
        return Score(value=state.store.get("environment", 0.0))

    return score


@scorer(metrics=MEMBER_METRICS)
def environment_members() -> Scorer:
    return member_scores(environment(), artifact_result())


# Passes the run-time check of the environment that fails, then fails the final check of
# the environment that passes, and the reverse: the run-time verdict and final_verdict
# disagree in both epochs.
async def _checks_environment(answer: str, metadata: dict[str, Any]) -> Verdict:
    calls = store().get("checks", 0)
    store().set("checks", calls + 1)
    return Verdict(passed=(calls == 0) != (store().get("environment") == 1.0))


def _artifact_arm(log_dir: Path) -> EvalLog:
    return _run(
        Task(
            dataset=[Sample(id="s1", input="Fix the repository.")],
            solver=baseline(
                edits_environment(),
                result=artifact_result(verifier=_checks_environment),
            ),
            scorer=[environment(), environment_members()],
            epochs=Epochs(2, pass_at(2)),
        ),
        log_dir,
    )


def test_artifact_rows_read_the_environment_score_and_final_verdict(
    tmp_path: Path,
) -> None:
    log = _artifact_arm(tmp_path)
    # the oracle source: pass@2 on the task scorer, while best_member has no value
    assert log.results is not None
    reduced = {(s.scorer, s.name): s for s in log.results.scores}
    assert reduced[("environment", "environment")].reducer == "pass_at_2"
    assert reduced[("environment", "environment")].metrics["mean"].value == 1.0
    assert math.isnan(
        reduced[("environment_members", "best_member")].metrics["mean"].value
    )

    rows = attempt_rows(
        [log.location], scorer="environment", member_scorer="environment_members"
    )
    assert [r.result_kind for r in rows] == ["artifact", "artifact"]
    assert [r.final_value for r in rows] == [0.0, 1.0]
    assert [r.best_member for r in rows] == [None, None]
    # a passing environment with an empty final output
    samples = read_eval_log(log.location).samples or []
    assert samples[1].output.completion == ""
    assert [r.members[0].verdict for r in rows] == [_PASS, _FAIL]
    assert [r.final_verdict for r in rows] == [_FAIL, _PASS]
    # verify chooses by final_verdict, never by the run-time verdict
    assert select_at_k(rows, 2, "verify") == 1.0
    assert select_at_k(rows, 1, "verify") == 0.5

    # result_kind survives into select_at_k: the answer arm's rows of the same sample
    answer_rows = attempt_rows(
        [_answer_arm(tmp_path / "answer")],
        scorer="slow_match",
        member_scorer="rescore_members",
    )
    with pytest.raises(ValueError, match="disagree on result_kind") as refused:
        select_at_k([*rows, *answer_rows], 1, "first")
    assert log.location in str(refused.value)
    assert answer_rows[0].log in str(refused.value)


def test_attempt_rows_flag_an_agent_that_consulted_the_task_scorer(
    tmp_path: Path,
) -> None:
    model = scripted_model(
        Script(
            respond=lambda _: ModelOutput.for_tool_call(
                "mockllm", "submit", {"answer": "42"}
            )
        )
    )
    log = _run(
        Task(
            dataset=[Sample(id="s1", input="What is six times seven?", target="42")],
            solver=baseline(
                member(react, args={"model": model, "attempts": 2}),
                result=answer_result(answer_key=_answer_key),
            ),
            scorer=[match(), rescore_members()],
        ),
        tmp_path,
    )
    [row] = attempt_rows([log], scorer="match", member_scorer="rescore_members")
    assert row.target_assisted


def test_attempt_rows_join_the_samples_dataframe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a dataset without ids, so Inspect numbers the samples, and a log named by a path
    # relative to the working directory
    log = _run(
        Task(
            dataset=[Sample(input="What is six times seven?", target="42")],
            solver=baseline(says("42"), result=answer_result()),
            scorer=[match(), rescore_members()],
            epochs=2,
        ),
        tmp_path / "logs",
    )
    monkeypatch.chdir(tmp_path)
    relative = os.path.relpath(log.location)
    df = samples_df("logs", columns=SampleSummary + SwarmColumns)
    records = cast(
        list[dict[str, Any]],
        df.to_dict("records"),  # pyright: ignore[reportUnknownMemberType]
    )
    keys = {(r["log"], r["id"], r["epoch"]) for r in records}
    for source in [relative, read_eval_log(relative)]:
        rows = attempt_rows([source], scorer="match", member_scorer="rescore_members")
        assert {(r.log, r.id, r.epoch) for r in rows} == keys
        assert [r.id for r in rows] == ["1", "1"]


def test_attempt_rows_read_a_plain_agent_without_a_record(tmp_path: Path) -> None:
    log = _run(
        Task(
            dataset=[Sample(id="s1", input="What is six times seven?", target="42")],
            solver=as_solver(says("42")),
            scorer=[match(), rescore_members()],
        ),
        tmp_path,
    )
    [row] = attempt_rows(
        [log.location], scorer="match", member_scorer="rescore_members"
    )
    assert (row.final, row.members, row.members_total, row.result_kind) == (
        None,
        [],
        None,
        None,
    )
    assert (row.final_value, row.best_member) == (1.0, 1.0)
    # select@k reads baseline() attempts only
    assert math.isnan(select_at_k([row], 1, "first"))


@pytest.mark.parametrize(
    "scorer_name,member_scorer,message",
    [
        pytest.param(
            "matches",
            "rescore_members",
            "no sample has a score named 'matches'",
            id="scorer",
        ),
        pytest.param(
            "match",
            "members",
            "no sample has a score named 'members'",
            id="member-scorer",
        ),
        pytest.param(
            "rescore_members",
            "rescore_members",
            "'rescore_members' is not a single value",
            id="member-scorer-as-scorer",
        ),
        pytest.param(
            "match",
            "match",
            "'match' is not one member_scores",
            id="scorer-as-member-scorer",
        ),
    ],
)
def test_attempt_rows_refuse_scorers_they_cannot_read(
    tmp_path: Path, scorer_name: str, member_scorer: str, message: str
) -> None:
    log = _run(
        Task(
            dataset=[Sample(id="s1", input="What is six times seven?", target="42")],
            solver=baseline(says("42"), result=answer_result()),
            scorer=[match(), rescore_members()],
        ),
        tmp_path,
    )
    with pytest.raises(ValueError, match=message):
        attempt_rows([log], scorer=scorer_name, member_scorer=member_scorer)


def test_attempt_rows_refuse_a_log_without_samples(tmp_path: Path) -> None:
    log = _run(
        Task(
            dataset=[Sample(id="s1", input="What is six times seven?", target="42")],
            solver=baseline(says("42"), result=answer_result()),
            scorer=[match(), rescore_members()],
        ),
        tmp_path,
    )
    header = read_eval_log(log.location, header_only=True)
    with pytest.raises(ValueError, match="read without its samples"):
        attempt_rows([header], scorer="match", member_scorer="rescore_members")
