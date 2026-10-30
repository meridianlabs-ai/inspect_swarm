from pathlib import Path
from typing import Any

import anyio
import pytest
from inspect_ai import Task, eval  # pyright: ignore[reportUnknownVariableType]
from inspect_ai._util.registry import (
    create_registry_object,
    registry_info,
    registry_params,
)
from inspect_ai.agent import Agent, AgentState, agent
from inspect_ai.agent import run as run_agent
from inspect_ai.dataset import Sample
from inspect_ai.event import InfoEvent
from inspect_ai.log import read_eval_log
from inspect_ai.model import Model, ModelOutput
from inspect_ai.util import store, token_limit

from inspect_swarm import (
    Budget,
    MemberEnded,
    ResultSpec,
    StopReason,
    SwarmControl,
    Verdict,
    VerdictSummary,
    answer_result,
    artifact_result,
    member,
    swarm,
    swarm_result,
)
from inspect_swarm._evidence import LEDGER_KEY
from inspect_swarm._ledger import SwarmLedger
from inspect_swarm._record import SwarmResultRecord, _is_record, _transcript_result
from inspect_swarm._result import verifier_label

from .swarm_runtime import (
    TOKENS_PER_CALL,
    Script,
    controller,
    evidence,
    generator,
    output,
    run_swarm,
    sample,
    scripted_model,
    statuses,
)


@agent
def answers(text: str, gate: anyio.Event | None = None) -> Agent:
    """Answer `text`, once `gate` is set if given, making no model call."""

    async def execute(state: AgentState) -> AgentState:
        if gate is not None:
            await gate.wait()
        state.output = ModelOutput.from_content("mockllm", text)
        return state

    return execute


class Checker:
    """A verifier that passes the answer `42` and records its calls."""

    def __init__(self, delay: float = 0.0, model: Model | None = None) -> None:
        self.delay = delay
        self.model = model
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.in_flight = 0
        self.most_in_flight = 0

    async def __call__(self, answer: str, metadata: dict[str, Any]) -> Verdict:
        self.calls.append((answer, metadata))
        self.in_flight += 1
        self.most_in_flight = max(self.most_in_flight, self.in_flight)
        try:
            await anyio.sleep(self.delay)
            if self.model is not None:
                await self.model.generate("check")
        finally:
            self.in_flight -= 1
        passed = answer == "42"
        return Verdict(
            passed=passed,
            value=1.0 if passed else 0.0,
            explanation=f"EXPLANATION-{answer}",
        )


def _key(text: str) -> str | None:
    return text.strip() or None


# ---------------------------------------------------------------------------
# The contract


@pytest.mark.parametrize(
    "build,error",
    [
        pytest.param(
            lambda: ResultSpec(kind="answers"),  # pyright: ignore[reportArgumentType]
            "kind must be one of 'answer', 'artifact'",
            id="unknown-kind",
        ),
        pytest.param(
            lambda: ResultSpec(kind="answer", member_artifacts=object()),  # pyright: ignore[reportArgumentType]
            "member_artifacts is for kind='artifact' only",
            id="artifacts-on-an-answer-task",
        ),
    ],
)
def test_a_contract_the_swarm_cannot_use_is_refused(build: Any, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        build()


def test_the_builders_set_the_kind() -> None:
    assert answer_result(answer_key=_key) == ResultSpec(kind="answer", answer_key=_key)
    assert artifact_result() == ResultSpec(kind="artifact")


class _Named:
    name = "named-check"

    async def __call__(self, answer: str, metadata: dict[str, Any]) -> Verdict:
        return Verdict(passed=True)


async def _check_answer(answer: str, metadata: dict[str, Any]) -> Verdict:
    return Verdict(passed=True)


@pytest.mark.parametrize(
    "verifier,label",
    [
        pytest.param(_Named(), "named-check", id="name"),
        pytest.param(_check_answer, "_check_answer", id="function"),
        pytest.param(Checker(), "Checker", id="instance"),
    ],
)
def test_the_record_labels_the_verifier(verifier: Any, label: str) -> None:
    assert verifier_label(verifier) == label


@pytest.mark.parametrize(
    "result,error",
    [
        pytest.param(
            "ResultSpec", "the type name a log records for a result contract", id="log"
        ),
        pytest.param({"kind": "answer"}, "must be a ResultSpec", id="dict"),
    ],
)
def test_a_result_that_is_not_a_contract_is_refused(result: Any, error: str) -> None:
    with pytest.raises(TypeError, match=error):
        swarm(answers("42"), channels=[], result=result)


def test_raw_replay_of_a_swarm_with_a_contract_is_refused() -> None:
    original = swarm(
        answers("42"), channels=[], result=answer_result(verifier=Checker())
    )
    params = registry_params(original)
    assert params["result"] == "ResultSpec"
    with pytest.raises(TypeError, match="cannot be rebuilt from a log"):
        create_registry_object("agent", registry_info(original).name, params)


def test_a_record_without_the_contracts_fields_reads_with_them_absent() -> None:
    record = SwarmResultRecord.model_validate(
        {
            "version": 1,
            "members": [
                {
                    "name": "a",
                    "role": None,
                    "state": "ended",
                    "status": "submitted",
                    "submitted": True,
                    "time": 1.0,
                }
            ],
            "final": {
                "mode": "first",
                "member": "a",
                "reason": None,
                "provisional": False,
            },
        }
    )
    [m] = record.members
    assert (record.kind, record.verifier) == (None, None)
    assert (m.output, m.answer_key, m.verdict, m.verdict_time) == (
        None,
        None,
        None,
        None,
    )


# ---------------------------------------------------------------------------
# Verdicts in the run


@pytest.mark.anyio
async def test_submissions_are_keyed_and_verified_before_their_events() -> None:
    # slow enough that every agent has returned before the last verdict is taken
    checker = Checker(delay=0.05)
    refusal = Script(
        outputs=[
            ModelOutput.from_content(
                "mockllm", "I can't help with that.", stop_reason="content_filter"
            )
        ]
    )
    seen: list[MemberEnded] = []
    handles: dict[str, VerdictSummary | None] = {}

    async def run(control: SwarmControl) -> StopReason:
        for m in control.members:
            control.start(m)
        async for event in control.events():
            seen.append(event)
            handles[event.member] = control.member(event.member).verdict
        return "all_done"

    stop = await run_swarm(
        swarm(
            [
                member(answers("42"), name="right"),
                member(answers(" 7 "), name="wrong"),
                member(generator(scripted_model(refusal)), name="refuser"),
            ],
            controller=controller(run, "result_test_verified"),
            channels=[],
            result=answer_result(answer_key=_key, verifier=checker),
        )
    )
    assert statuses(stop) == {
        "right": "submitted",
        "wrong": "submitted",
        "refuser": "unsubmitted",
    }
    # each submission once, one at a time, with no sample metadata outside a sample
    assert sorted(checker.calls) == [(" 7 ", {}), ("42", {})]
    assert checker.most_in_flight == 1
    verdicts = {
        "right": VerdictSummary(passed=True, value=1.0),
        "wrong": VerdictSummary(passed=False, value=0.0),
        "refuser": None,
    }
    assert {e.member: e.verdict for e in seen} == verdicts
    assert handles == verdicts

    record = stop.record
    assert (record.kind, record.verifier) == ("answer", "Checker")
    by_name = {m.name: m for m in record.members}
    for name, completion, key in [
        ("right", "42", "42"),
        ("wrong", " 7 ", "7"),
        ("refuser", "I can't help with that.", None),
    ]:
        m = by_name[name]
        assert m.output is not None and m.output.completion == completion
        assert m.answer_key == key
    assert by_name["right"].verdict == Verdict(True, 1.0, "EXPLANATION-42")
    assert by_name["wrong"].verdict == Verdict(False, 0.0, "EXPLANATION- 7 ")
    assert by_name["refuser"].verdict is None
    for name in ("right", "wrong"):
        m = by_name[name]
        assert m.verdict_time is not None and m.time is not None
        assert m.verdict_time > m.time

    events = evidence(stop.transcript, "verdict")
    assert sorted((e["member"], e["passed"], e["value"]) for e in events) == [
        ("right", True, 1.0),
        ("wrong", False, 0.0),
    ]
    # written in the swarm's span
    swarm_events = [
        e
        for e in stop.transcript.events
        if isinstance(e, InfoEvent) and e.source == "inspect_swarm"
    ]
    [swarm_span] = {e.span_id for e in swarm_events if _kind(e) == "final"}
    assert {e.span_id for e in swarm_events if _kind(e) == "verdict"} == {swarm_span}
    # the explanation is in the record alone, written when the swarm ends
    assert "EXPLANATION" not in repr(
        [e.data for e in swarm_events if not _is_record(e)]
    )
    assert "EXPLANATION" not in repr(seen)


def _kind(event: InfoEvent) -> object:
    return event.data.get("kind") if isinstance(event.data, dict) else None


def test_the_verifier_gets_the_answer_and_the_samples_metadata(
    tmp_path: Path,
) -> None:
    checker = Checker()
    long = "x" * 300
    task = Task(
        dataset=[Sample(input="task", target="TARGET-1729", metadata={"level": 3})],
        solver=swarm(
            [member(answers("42"), name="short"), member(answers(long), name="long")],
            channels=[],
            result=answer_result(verifier=checker),
        ),
    )
    [log] = eval(task, model="mockllm/model", log_dir=str(tmp_path), display="none")
    assert log.status == "success"
    assert sorted(checker.calls) == [("42", {"level": 3}), (long, {"level": 3})]
    # from the log, with the long output and explanation left as attachments
    samples = read_eval_log(log.location).samples
    assert samples is not None
    [sample] = samples
    record = swarm_result(sample)
    assert record is not None
    short_result, long_result = record.members
    assert short_result.verdict == Verdict(True, 1.0, "EXPLANATION-42")
    assert long_result.verdict == Verdict(False, 0.0, f"EXPLANATION-{long}")
    assert long_result.output is not None and long_result.output.completion == long


@pytest.mark.anyio
async def test_the_stop_leaves_a_submission_unverified_when_it_refuses_its_check() -> (
    None
):
    model = scripted_model(cost=1.0)
    checker = Checker(model=model)
    gate = anyio.Event()

    async def run(control: SwarmControl) -> StopReason:
        for m in control.members:
            control.start(m)
        async for event in control.events():
            if event.member == "early":
                break
        # nothing awaits between here and the stop, so the late member submits during
        # grace, after the runtime lever has closed
        gate.set()
        return "submitted"

    async with sample():
        await run_agent(
            swarm(
                [
                    member(answers("42"), name="early"),
                    member(answers("42", gate), name="late"),
                ],
                controller=controller(run, "result_test_refused_check"),
                channels=[],
                result=answer_result(verifier=checker),
            ),
            "task",
        )
        record = _transcript_result(store())
        ledger = SwarmLedger.model_validate(store().get(LEDGER_KEY))
    assert record is not None
    assert [m.status for m in record.members] == ["submitted", "submitted"]
    # the closed runtime lever refused the late check's model call
    assert [answer for answer, _ in checker.calls] == ["42", "42"]
    early, late = record.members
    assert early.verdict == Verdict(True, 1.0, "EXPLANATION-42")
    assert (late.verdict, late.verdict_time) == (None, None)
    # the verifier's call is the swarm's, under the runtime lever
    assert ledger.runtime.cost == 1.0
    assert [m.meter.cost for m in ledger.members] == [0.0, 0.0]


@pytest.mark.anyio
async def test_the_hard_stop_ends_a_verification_in_progress() -> None:
    checker = Checker(delay=3600)
    stop = await run_swarm(
        swarm(
            answers("42"),
            channels=[],
            budget=Budget(time=0.1, grace=0.05),
            result=answer_result(verifier=checker),
        )
    )
    # the verification held the submission's event, so the time cap stopped the swarm
    assert stop.reason == "swarm_time"
    [m] = stop.record.members
    assert (m.status, m.verdict) == ("submitted", None)
    assert stop.record.final.member == m.name


@pytest.mark.anyio
@pytest.mark.parametrize(
    "result,error,match",
    [
        pytest.param(
            answer_result(verifier=lambda answer, metadata: _raise()),  # pyright: ignore[reportArgumentType]
            RuntimeError,
            "contract failed",
            id="verifier-raises",
        ),
        pytest.param(
            answer_result(verifier=lambda answer, metadata: _true()),  # pyright: ignore[reportArgumentType]
            TypeError,
            "must return a Verdict, got a bool",
            id="verifier-returns-no-verdict",
        ),
        pytest.param(
            answer_result(answer_key=lambda text: _raise()),
            RuntimeError,
            "contract failed",
            id="answer-key-raises",
        ),
    ],
)
async def test_a_failing_contract_fails_the_sample(
    result: ResultSpec, error: type[Exception], match: str
) -> None:
    with pytest.raises(error, match=match):
        await run_swarm(swarm(answers("42"), channels=[], result=result))


def _raise() -> Any:
    raise RuntimeError("contract failed")


async def _true() -> bool:
    return True


# ---------------------------------------------------------------------------
# Outputs in the record


@agent
def generate_then_wait(model: Model) -> Agent:
    """Generate once, updating the state in place, then wait without a model call."""

    async def execute(state: AgentState) -> AgentState:
        state.output = await model.generate(state.messages)
        await anyio.sleep(3600)
        return state

    return execute


@pytest.mark.anyio
async def test_the_record_keeps_each_members_output_however_it_ended() -> None:
    waiting = Script(outputs=[output("waiting")])

    async def run(control: SwarmControl) -> StopReason:
        for m in control.members:
            control.start(m)
        async for event in control.events():
            if event.member == "limited":
                break
        while waiting.calls < 1:
            await anyio.sleep(0)
        return "enough"

    stop = await run_swarm(
        swarm(
            [
                member(
                    generator(
                        scripted_model(Script(outputs=[output("limited")])), calls=None
                    ),
                    name="limited",
                    limits=[token_limit(TOKENS_PER_CALL)],
                ),
                member(generate_then_wait(scripted_model(waiting)), name="waiting"),
            ],
            controller=controller(run, "result_test_outputs"),
            channels=[],
            budget=Budget(grace=0.05),
        )
    )
    assert statuses(stop) == {"limited": "member_limit:token", "waiting": "cancelled"}
    limited, cancelled = stop.record.members
    assert limited.output is not None and limited.output.completion == "limited"
    assert cancelled.output is not None and cancelled.output.completion == "waiting"
    # no contract: no kind, keys or verdicts
    assert (stop.record.kind, stop.record.verifier) == (None, None)
    assert all(m.answer_key is None and m.verdict is None for m in stop.record.members)


@pytest.mark.anyio
async def test_an_output_a_log_would_change_fails_the_sample() -> None:
    # a log would read this output back as a reference to an attachment
    with pytest.raises(ValueError, match="the members' outputs"):
        await run_swarm(swarm(answers("attachment://0123"), channels=[]))
