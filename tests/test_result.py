import math
import re
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Literal, Self, cast

import anyio
import pytest
from inspect_ai import (
    Task,
    eval,  # pyright: ignore[reportUnknownVariableType]
    eval_async,  # pyright: ignore[reportUnknownVariableType]
)
from inspect_ai._util.hash import mm3_hash
from inspect_ai._util.registry import (
    create_registry_object,
    registry_info,
    registry_params,
)
from inspect_ai.agent import Agent, AgentState, agent, react
from inspect_ai.agent import run as run_agent
from inspect_ai.dataset import Sample
from inspect_ai.event import SwarmEvent
from inspect_ai.log import read_eval_log
from inspect_ai.model import (
    ChatMessageAssistant,
    ChatMessageUser,
    ContentText,
    Logprob,
    Logprobs,
    Model,
    ModelOutput,
    UrlCitation,
)
from inspect_ai.model._call_tools import parse_tool_call
from inspect_ai.scorer import Score, Scorer, Target, scorer
from inspect_ai.solver import TaskState
from inspect_ai.tool import ToolCall, ToolInfo, ToolParam, ToolParams
from inspect_ai.util import LimitExceededError, store, token_limit

from inspect_swarm import (
    AnswerResult,
    Budget,
    FinalRecord,
    MemberEnded,
    MemberResult,
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
from inspect_swarm._evidence import LEDGER_KEY, METRICS_KEY
from inspect_swarm._ledger import SwarmLedger
from inspect_swarm._record import (
    RECORD_TYPE,
    SwarmResultRecord,
    _transcript_result,
    write_record,
)
from inspect_swarm._result import verifier_label

from .swarm_runtime import (
    TOKENS_PER_CALL,
    Script,
    controller,
    generator,
    output,
    run_swarm,
    sample,
    scripted_model,
    statuses,
    swarm_events,
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


@pytest.mark.parametrize(
    "result",
    [
        pytest.param(answer_result(answer_key=_key), id="answer-key"),
        pytest.param(answer_result(verifier=Checker()), id="verifier"),
    ],
)
def test_a_contract_that_keys_or_verifies_refuses_a_persistent_member(
    result: ResultSpec,
) -> None:
    members = [
        member(answers("42"), name="plain"),
        member(react(), name="kept", persistent=True),
    ]
    with pytest.raises(ValueError, match="Member 'kept' is persistent"):
        swarm(members, channels=[], result=result)
    # a contract that only names the kind applies no per-submission work
    swarm(members, channels=[], result=answer_result())


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
    assert (m.output, m.answer_key, m.verdict, m.verdict_time, m.answer_results) == (
        None,
        None,
        None,
        None,
        [],
    )


@pytest.mark.anyio
async def test_each_answers_model_text_is_kept_or_left_out_by_itself() -> None:
    reference = "attachment://0123"
    kept = AnswerResult(
        answer_key="42",
        verdict=Verdict(True, 1.0, "EXPLANATION-42"),
        verdict_time=1.5,
    )
    persistent = MemberResult(
        name="a",
        role=None,
        state="ended",
        status="submitted",
        submitted=True,
        time=4.0,
        persistent=True,
        answer_times=[1.0, 2.0, 3.0],
        answer_results=[
            kept,
            AnswerResult(
                answer_key=reference,
                verdict=Verdict(False, 0.0, reference),
                verdict_time=2.5,
            ),
            AnswerResult(answer_key="bad \ud800 key"),
        ],
    )
    async with sample():
        write_record(
            SwarmResultRecord(
                members=[persistent],
                final=FinalRecord(
                    mode="first", member="a", reason=None, provisional=False, answer=0
                ),
            )
        )
        record = _transcript_result(store())
    assert record is not None
    [m] = record.members
    # only the answer whose text reads as a reference loses it, and names it
    assert m.left_out == []
    assert m.answer_results == [
        kept,
        AnswerResult(
            verdict=Verdict(False, 0.0),
            verdict_time=2.5,
            left_out=["answer_key", "explanation"],
        ),
        # escaped, as a log escapes text that is not valid UTF-8
        AnswerResult(answer_key="bad \\ud800 key"),
    ]


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
            assert isinstance(event, MemberEnded)
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

    events = swarm_events(stop.transcript, "verdict")
    assert sorted((e.member, e.data["passed"], e.data["value"]) for e in events) == [
        ("right", True, 1.0),
        ("wrong", False, 0.0),
    ]
    # written in the swarm's span
    evidence = [e for e in stop.transcript.events if isinstance(e, SwarmEvent)]
    [swarm_span] = {e.span_id for e in evidence if e.kind == "final"}
    assert {e.span_id for e in evidence if e.kind == "verdict"} == {swarm_span}
    # the explanation is in the record alone, written when the swarm ends
    assert "EXPLANATION" not in repr(
        [e.data for e in evidence if e.kind != RECORD_TYPE]
    )
    assert "EXPLANATION" not in repr(seen)


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
async def test_the_final_step_verifies_a_submission_whose_check_the_stop_refused() -> (
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
    # the closed runtime lever refused the late check's model call, and the final step
    # checked it again
    assert [answer for answer, _ in checker.calls] == ["42", "42", "42"]
    early, late = record.members
    assert early.verdict == late.verdict == Verdict(True, 1.0, "EXPLANATION-42")
    assert late.verdict_time is not None and early.verdict_time is not None
    assert late.verdict_time > early.verdict_time
    # the verifier's calls are the swarm's: under the runtime lever during the run, and
    # the final step's after it
    assert (ledger.runtime.cost, ledger.final.cost) == (1.0, 1.0)
    assert [m.meter.cost for m in ledger.members] == [0.0, 0.0]


class _Held:
    """A verifier whose first call waits until released; cancelled first, it either lets the cancellation propagate or catches it and returns a passing verdict all the same. Later calls return a failing verdict at once."""

    def __init__(self, catches: bool) -> None:
        self.catches = catches
        self.calls: list[str] = []
        self.started = anyio.Event()
        self.release = anyio.Event()

    async def __call__(self, answer: str, metadata: dict[str, Any]) -> Verdict:
        self.calls.append(answer)
        if len(self.calls) > 1:
            return Verdict(passed=False)
        self.started.set()
        try:
            await self.release.wait()
        except anyio.get_cancelled_exc_class():
            if not self.catches:
                raise
        return Verdict(passed=True)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "stop,behaviour",
    [
        # the swarm's time cap, a controller's stop and the sample's own cancellation
        # each end the verification: whether the verifier lets the cancellation
        # propagate or catches it and returns, the submission stays unverified, and
        # the queued one is not verified in the run. The final step then verifies
        # both, unless the sample's cancellation ended the swarm
        *[
            pytest.param(stop, behaviour, id=f"{stop}-{behaviour}")
            for stop in ("swarm-time", "controller", "sample")
            for behaviour in ("propagates", "catches")
        ],
        # verifications that finish during the stop's grace keep their verdicts
        pytest.param("controller", "finishes", id="controller-finishes-in-grace"),
    ],
)
async def test_the_hard_stop_ends_a_verification_in_progress(
    stop: str, behaviour: str
) -> None:
    held = _Held(catches=behaviour == "catches")

    async def run(control: SwarmControl) -> StopReason:
        control.start("one")
        control.start("two")
        await held.started.wait()
        # the other member ends while the first submission is being verified
        while control.member("two").state != "ended" or (
            control.member("one").state != "ended"
        ):
            await anyio.sleep(0.01)
        # both have ended, and their events wait for their verdicts
        for name in ("one", "two"):
            handle = control.member(name)
            assert (handle.state, handle.status, handle.verdict) == (
                "ended",
                "submitted",
                None,
            )
        with anyio.move_on_after(0.05):
            async for event in control.events():
                raise AssertionError(f"{event} came before its verdict")
        if stop == "sample":
            outer.cancel()
        if stop != "controller":
            # the time cap or the sample's cancellation ends the controller
            await anyio.sleep_forever()
        if behaviour == "finishes":
            held.release.set()
        return "done"

    agent = swarm(
        [member(answers("1"), name="one"), member(answers("2"), name="two")],
        controller=controller(run, "result_test_held"),
        channels=[],
        budget=Budget(time=0.2 if stop == "swarm-time" else None, grace=0.05),
        result=answer_result(verifier=held),
    )
    async with sample() as transcript:
        with anyio.CancelScope() as outer:
            await run_agent(agent, "task")
        record = _transcript_result(store())
    assert outer.cancelled_caught == (stop == "sample")
    assert record is not None
    assert [m.status for m in record.members] == ["submitted", "submitted"]
    by_answer = {m.output.completion: m for m in record.members if m.output}
    # the first verified, then the one queued behind it
    first = by_answer.pop(held.calls[0])
    [queued] = by_answer.values()
    verdicts = swarm_events(transcript, "verdict")
    if behaviour == "finishes":
        assert len(held.calls) == 2
        assert (first.verdict, queued.verdict) == (
            Verdict(passed=True),
            Verdict(passed=False),
        )
        assert first.verdict_time is not None and queued.verdict_time is not None
        assert len(verdicts) == 2
    elif stop == "sample":
        # the queued submission's verifier is never called
        assert len(held.calls) == 1
        for m in (first, queued):
            assert (m.verdict, m.verdict_time) == (None, None)
        assert verdicts == []
    else:
        # the final step verifies both, in the order they ended
        assert first.output is not None and queued.output is not None
        assert held.calls == [
            first.output.completion,
            first.output.completion,
            queued.output.completion,
        ]
        assert first.verdict == queued.verdict == Verdict(passed=False)
        assert len(verdicts) == 2
    if stop == "swarm-time":
        # the verification held the submissions' events, so the time cap stopped the
        # swarm
        [final] = swarm_events(transcript, "final")
        assert final.data["stop_reason"] == "swarm_time"


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
    ],
)
async def test_a_failing_contract_fails_the_sample(
    result: ResultSpec, error: type[Exception], match: str
) -> None:
    with pytest.raises(error, match=match):
        await run_swarm(swarm(answers("42"), channels=[], result=result))


@pytest.mark.anyio
async def test_a_failing_answer_key_fails_the_sample_after_the_members_end() -> None:
    state = AgentState(messages=[ChatMessageUser(content="task")])
    async with sample() as transcript:
        with pytest.raises(RuntimeError, match="contract failed"):
            await swarm(
                answers("42"),
                channels=[],
                result=answer_result(answer_key=lambda text: _raise()),
            )(state)
        record = _transcript_result(store())
    # the member's end is recorded and its output is the answer, as in the record
    assert [e.data["status"] for e in swarm_events(transcript, "member_result")] == [
        "submitted"
    ]
    assert state.output.completion == "42"
    assert record is not None
    [m] = record.members
    assert record.final.member == m.name and record.final.provisional
    assert m.output is not None and m.output.completion == "42"
    assert m.answer_key is None


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


@pytest.mark.parametrize(
    "text,logged",
    [
        # read back as a reference to an attachment, so left out; the explanation only
        # quotes it
        pytest.param("attachment://0123", None, id="reference"),
        # not valid UTF-8, which the log escapes, as it does in a sample's own output
        pytest.param("bad \ud800 text", "bad \\ud800 text", id="surrogate"),
    ],
)
def test_model_text_is_recorded_as_a_log_gives_it_back(
    tmp_path: Path, text: str, logged: str | None
) -> None:
    task = Task(
        dataset=[Sample(input="task")],
        solver=swarm(
            [member(answers("42"), name="plain"), member(answers(text), name="odd")],
            channels=[],
            result=answer_result(answer_key=lambda text: text, verifier=Checker()),
        ),
    )
    [log] = eval(task, model="mockllm/model", log_dir=str(tmp_path), display="none")
    assert log.status == "success"
    samples = read_eval_log(log.location).samples
    assert samples is not None
    [sample] = samples
    record = swarm_result(sample)
    assert record is not None
    plain, odd = record.members
    assert plain.left_out == []
    assert plain.output is not None and plain.answer_key == "42"
    if logged is None:
        assert odd.left_out == ["output", "answer_key"]
        assert odd.output is None and odd.answer_key is None
    else:
        assert odd.left_out == []
        assert odd.output is not None and odd.output.completion == logged
        assert odd.answer_key == logged
    assert odd.verdict is not None
    assert (odd.verdict.passed, odd.verdict.value) == (False, 0.0)
    assert odd.verdict.explanation == f"EXPLANATION-{logged or text}"


async def _echo(answer: str, metadata: dict[str, Any]) -> Verdict:
    return Verdict(passed=False, value=0.0, explanation=answer)


@pytest.mark.anyio
@pytest.mark.parametrize("prefix", ["attachment://", "tc://"])
async def test_model_text_naming_an_attachment_of_the_sample_is_left_out(
    prefix: str,
) -> None:
    # long enough that the log keeps it as an attachment, which the reference names
    role = "a role " * 50
    reference = f"{prefix}{mm3_hash(role)}"
    stop = await run_swarm(
        swarm(
            [
                member(answers("42"), name="plain"),
                member(answers(reference), name="odd", role=role),
            ],
            channels=[],
            result=answer_result(answer_key=lambda text: text, verifier=_echo),
        )
    )
    _, odd = stop.record.members
    assert odd.role == role
    assert odd.left_out == ["output", "answer_key", "explanation"]
    assert (odd.output, odd.answer_key) == (None, None)
    assert odd.verdict is not None and odd.verdict.explanation is None


@agent
def returns(build: Callable[[], ModelOutput]) -> Agent:
    """Return the output `build` gives as the member's output, making no model call."""

    async def execute(state: AgentState) -> AgentState:
        # built here, so that the agent's logged arguments never hold it
        state.output = build()
        return state

    return execute


def _with_metadata(metadata: dict[str, Any]) -> ModelOutput:
    output = ModelOutput.from_content("mockllm", "41")
    output.metadata = metadata
    return output


def _tool_call(arguments: dict[str, Any]) -> ModelOutput:
    output = ModelOutput.for_tool_call(
        "mockllm", "submit", arguments, tool_call_id="call"
    )
    output.message.id = "odd"
    return output


# A tool call whose arguments hold the call itself, which only task code can build.
def _tool_call_in_its_arguments() -> ModelOutput:
    output = _tool_call({})
    [call] = output.message.tool_calls or []
    call.arguments["call"] = call
    return output


# An output nested in another's metadata, carrying an iterator that must not be read.
def _output_in_metadata() -> ModelOutput:
    inner = ModelOutput.from_content("mockllm", "inner")
    inner.metadata = {"rows": iter([1, 2])}
    return _with_metadata({"inner": inner})


# A tool call as Inspect parses it from the model's text: JSON, or YAML for a tool
# whose one parameter takes an object.
def _parsed_tool_call(arguments: str) -> ModelOutput:
    tool = ToolInfo(
        name="submit",
        description="Submit an answer.",
        parameters=ToolParams(properties={"answer": ToolParam(type="object")}),
    )
    call = parse_tool_call("call", "submit", arguments, [tool])
    assert call.arguments and call.parse_error is None
    output = _tool_call({})
    output.message.tool_calls = [call]
    return output


# An integer with more digits than Python prints, which YAML reads from hex.
_HUGE_KEY = "? 0x" + "f" * 4000 + "\n: value"


# Subclasses of the data classes, as task code might define, whose methods must never
# run: each raises.
# Classes as task code might define, each with a `__class__` property and methods that
# record that they ran and raise: none of them must run before the refusal.
_hook_calls: list[str] = []


def _ran(hook: str) -> AssertionError:
    _hook_calls.append(hook)
    return AssertionError(f"{hook} of the task's class ran")


class _Hooked:
    @property
    def __class__(self) -> type:  # pyright: ignore[reportIncompatibleMethodOverride]
        raise _ran("__class__")

    def __copy__(self) -> Self:
        raise _ran("__copy__")

    def __deepcopy__(self, memo: dict[int, Any]) -> Self:
        raise _ran("__deepcopy__")


class _List(_Hooked, list[Any]):
    def __iter__(self) -> Iterator[Any]:
        raise _ran("__iter__")

    def __len__(self) -> int:
        raise _ran("__len__")

    def __getitem__(self, index: Any) -> Any:
        raise _ran("__getitem__")

    def __repr__(self) -> str:
        raise _ran("__repr__")


class _Tuple(_Hooked, tuple[Any, ...]):
    def __iter__(self) -> Iterator[Any]:
        raise _ran("__iter__")


class _Set(_Hooked, set[Any]):
    def __iter__(self) -> Iterator[Any]:
        raise _ran("__iter__")


class _Dict(_Hooked, dict[str, Any]):
    def __iter__(self) -> Iterator[str]:
        raise _ran("__iter__")

    def items(self) -> Any:
        raise _ran("items")

    def keys(self) -> Any:
        raise _ran("keys")

    def values(self) -> Any:
        raise _ran("values")


class _Str(_Hooked, str):
    def __repr__(self) -> str:
        raise _ran("__repr__")

    def __str__(self) -> str:
        raise _ran("__str__")


class _Int(_Hooked, int):
    pass


class _Opaque(_Hooked):
    pass


class _ToolCall(_Hooked, ToolCall):
    pass


def _with_tool_call_subtype() -> ModelOutput:
    output = _tool_call({})
    [call] = output.message.tool_calls or []
    output.message.tool_calls = [
        _ToolCall(id=call.id, function=call.function, arguments={})
    ]
    return output


def _with_message_metadata(metadata: dict[Any, Any]) -> ModelOutput:
    output = ModelOutput.from_content("mockllm", "41")
    output.message.metadata = metadata
    return output


def _in_arguments(value: object) -> ModelOutput:
    output = _tool_call({})
    [call] = output.message.tool_calls or []
    call.arguments["value"] = value
    return output


def _choices_in_a_list_subclass() -> ModelOutput:
    output = ModelOutput.from_content("mockllm", "41")
    output.choices = _List(output.choices)
    return output


def _list_cycle() -> list[Any]:
    cycle: list[Any] = []
    cycle.append(cycle)
    return cycle


def _cycle() -> dict[str, Any]:
    cycle: dict[str, Any] = {}
    cycle["self"] = cycle
    return cycle


def _nested(depth: int) -> list[Any]:
    value: list[Any] = []
    for _ in range(depth):
        value = [value]
    return value


# Shared at every level, so that serializing it would expand it to 2 ** depth lists.
def _aliased(depth: int) -> list[Any]:
    value: list[Any] = []
    for _ in range(depth):
        value = [value, value]
    return value


@pytest.mark.anyio
@pytest.mark.parametrize(
    "odd",
    [
        pytest.param(lambda: _with_metadata(_cycle()), id="cycle"),
        # within the depth limit, refused before it is serialized
        pytest.param(lambda: _with_metadata({"value": _aliased(60)}), id="aliased"),
        pytest.param(
            lambda: _tool_call({"loop": _list_cycle()}), id="tool-arguments-cycle"
        ),
        # a key the log cannot write
        pytest.param(lambda: _parsed_tool_call(_HUGE_KEY), id="huge-yaml-key"),
    ],
)
async def test_an_output_the_record_cannot_keep_whole_is_left_out(odd: Any) -> None:
    stop = await run_swarm(_plain_then(odd, "result_test_left_out"))
    _assert_left_out(stop.record)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "odd,where",
    [
        pytest.param(
            lambda: _with_metadata({"diagnostic": object()}),
            r"type object at output\.metadata\['diagnostic'\]",
            id="object",
        ),
        # refused whatever else the output holds: here data the record would leave out
        pytest.param(
            lambda: _with_metadata(
                {"deep": _nested(194), "loop": _cycle(), "diagnostic": object()}
            ),
            r"type object at output\.metadata\['diagnostic'\]",
            id="after-data-left-out",
        ),
        # one of Inspect's models, outside the places Inspect puts it
        pytest.param(
            _output_in_metadata,
            r"type ModelOutput at output\.metadata\['inner'\]",
            id="model-in-metadata",
        ),
        # under a key too large to print, which the path names by its type
        pytest.param(
            lambda: _with_metadata({16**4000: {"x": object()}}),
            r"type object at output\.metadata\[<int key>\]\['x'\]",
            id="under-a-huge-key",
        ),
        # subclasses of the data classes, refused before any of their methods runs
        pytest.param(
            lambda: _with_metadata({"rows": _List([1])}),
            r"type _List at output\.metadata\['rows'\]",
            id="list-subclass",
        ),
        pytest.param(
            lambda: _in_arguments(_Dict(a=1)),
            r"type _Dict at output\.choices\[0\]\.message\.tool_calls\[0\]\.arguments\['value'\]",
            id="dict-subclass",
        ),
        pytest.param(
            lambda: _with_message_metadata({"pair": _Tuple((1, 2))}),
            r"type _Tuple at output\.choices\[0\]\.message\.metadata\['pair'\]",
            id="tuple-subclass",
        ),
        pytest.param(
            lambda: _with_metadata({"tags": _Set({1})}),
            r"type _Set at output\.metadata\['tags'\]",
            id="set-subclass",
        ),
        pytest.param(
            lambda: _with_metadata({"note": _Str("x")}),
            r"type _Str at output\.metadata\['note'\]",
            id="str-subclass",
        ),
        pytest.param(
            lambda: _with_metadata({_Str("key"): 1}),
            r"type _Str at output\.metadata\. ",
            id="str-subclass-key",
        ),
        pytest.param(
            _choices_in_a_list_subclass,
            r"type _List at output\.choices\. ",
            id="structural-list-subclass",
        ),
        pytest.param(
            lambda: _with_metadata({"count": _Int(3)}),
            r"type _Int at output\.metadata\['count'\]",
            id="scalar-subclass",
        ),
        pytest.param(
            lambda: _with_metadata({"diagnostic": _Opaque()}),
            r"type _Opaque at output\.metadata\['diagnostic'\]",
            id="opaque-object",
        ),
        pytest.param(
            _with_tool_call_subtype,
            r"type _ToolCall at output\.choices\[0\]\.message\.tool_calls\[0\]\. ",
            id="tool-call-subtype",
        ),
        pytest.param(
            _tool_call_in_its_arguments,
            r"type ToolCall at output\.choices\[0\]\.message\.tool_calls\[0\]\.arguments\['call'\]",
            id="tool-call-in-arguments",
        ),
    ],
)
async def test_an_object_the_task_put_in_an_output_fails_the_sample(
    odd: Callable[[], ModelOutput], where: str
) -> None:
    async with sample():
        with pytest.raises(
            TypeError, match=f"output of member 'odd' holds a value of {where}"
        ):
            await run_agent(_plain_then(odd, "result_test_author_object"), "task")


class _RewrittenOutput(ModelOutput):
    """A `ModelOutput` subtype whose copy is another output."""

    def __copy__(self) -> Self:
        _ran("__copy__")
        return cast(Self, ModelOutput.from_content("mockllm", "rewritten"))


class _RaisingOutput(ModelOutput):
    """A `ModelOutput` subtype whose copy raises."""

    def __copy__(self) -> Self:
        raise _ran("__copy__")


class _TaskZone(_Hooked, tzinfo):
    def utcoffset(self, dt: datetime | None) -> timedelta:
        raise _ran("utcoffset")

    def tzname(self, dt: datetime | None) -> str:
        raise _ran("tzname")

    def dst(self, dt: datetime | None) -> timedelta:
        raise _ran("dst")


class _Offset(_Hooked, timedelta):
    def __repr__(self) -> str:
        raise _ran("__repr__")


def _at(zone: tzinfo) -> datetime:
    return datetime(2024, 1, 2, 3, 4, 5, tzinfo=zone)


class _RaisingMessage(ChatMessageAssistant):
    """A message subtype whose copy raises, as the final conversation copies it."""

    def __copy__(self) -> Self:
        raise _ran("__copy__")


def _subtype(cls: type[ModelOutput]) -> Callable[[], ModelOutput]:
    def build() -> ModelOutput:
        base = ModelOutput.from_content("mockllm", "original")
        return cls(model=base.model, choices=base.choices)

    return build


def _raising_message() -> ModelOutput:
    output = ModelOutput.from_content("mockllm", "original")
    output.choices[0].message = _RaisingMessage(content="original")
    return output


# The member's output is checked when it ends, before anything copies it, and the sample fails with the member `errored` and the
# record, the ledger and the metrics kept.
@pytest.mark.anyio
@pytest.mark.parametrize(
    "odd,where",
    [
        pytest.param(
            _subtype(_RewrittenOutput),
            r"type _RewrittenOutput at output\. ",
            id="copy-rewrites",
        ),
        pytest.param(
            _subtype(_RaisingOutput),
            r"type _RaisingOutput at output\. ",
            id="copy-raises",
        ),
        pytest.param(
            _raising_message,
            r"type _RaisingMessage at output\.choices\[0\]\.message\. ",
            id="answer-message-copy-raises",
        ),
        # a timestamp whose time zone, or its offset or name, is from task code
        pytest.param(
            lambda: _with_metadata({"at": _at(_TaskZone())}),
            r"type datetime at output\.metadata\['at'\]",
            id="task-time-zone",
        ),
        pytest.param(
            lambda: _in_arguments(
                {_at(timezone(timedelta(hours=1), _Str("x"))): object()}
            ),
            r"type datetime at output\.choices\[0\]\.message\.tool_calls\[0\]\.arguments\['value'\]\. ",
            id="time-zone-name-in-a-key",
        ),
        pytest.param(
            lambda: _with_message_metadata({"at": _at(timezone(_Offset(hours=1)))}),
            r"type datetime at output\.choices\[0\]\.message\.metadata\['at'\]",
            id="time-zone-offset",
        ),
    ],
)
async def test_an_output_is_checked_before_anything_copies_it(
    odd: Callable[[], ModelOutput], where: str
) -> None:
    _hook_calls.clear()
    async with sample():
        with pytest.raises(
            TypeError, match=f"output of member 'odd' holds a value of {where}"
        ):
            await run_agent(
                swarm(member(returns(odd), name="odd"), channels=[]), "task"
            )
        record = _transcript_result(store())
        accounting = (store().get(LEDGER_KEY), store().get(METRICS_KEY))
    assert _hook_calls == []
    assert record is not None and None not in accounting
    [m] = record.members
    assert (m.status, m.output, m.left_out) == ("errored", None, ["output"])


class _Uncopyable:
    """An object whose copy records that it ran. It has no `__class__` hook, since Inspect's own log writer reads its class."""

    def __deepcopy__(self, memo: dict[int, Any]) -> Self:
        raise _ran("__deepcopy__")


# An answer is checked when it is recorded; a reply that is not one, when the member
# ends. Either way the sample fails as for a returned output.
@pytest.mark.anyio
@pytest.mark.parametrize(
    "stop_reason", ["stop", "model_length"], ids=["answer", "not-an-answer"]
)
async def test_a_persistent_answer_is_checked_before_it_is_kept(
    tmp_path: Path, stop_reason: Literal["stop", "model_length"]
) -> None:
    _hook_calls.clear()
    reply = ModelOutput.from_content("mockllm", "41", stop_reason=stop_reason)
    reply.metadata = {"diagnostic": _Uncopyable()}
    persistent = member(
        react(model=scripted_model(Script(outputs=[reply])), submit=False),
        name="odd",
        persistent=True,
    )
    # react needs a running sample
    [log] = await eval_async(
        Task(dataset=[Sample(input="task")], solver=swarm(persistent, channels=[])),
        model="mockllm/model",
        log_dir=str(tmp_path),
    )
    assert log.samples is not None
    [ran] = log.samples
    assert ran.error is not None and "TypeError" in ran.error.traceback
    assert re.search(
        r"output of member 'odd' holds a value of type _Uncopyable at output\.metadata\['diagnostic'\]",
        ran.error.message,
    )
    assert _hook_calls == []
    record = swarm_result(ran)
    assert record is not None
    assert LEDGER_KEY in ran.store and METRICS_KEY in ran.store
    [m] = record.members
    assert (m.status, m.output, m.left_out, m.answer_times) == (
        "errored",
        None,
        ["output"],
        [],
    )


@agent
def holds(
    build: Callable[[], ModelOutput],
    ending: str,
    ready: anyio.Event,
    release: anyio.Event,
) -> Agent:
    """Set the output `build` gives, then end as `ending` says: stop itself with a custom limit, or wait to be released or cancelled."""

    async def execute(state: AgentState) -> AgentState:
        state.output = build()
        ready.set()
        if ending == "custom-limit":
            raise LimitExceededError("custom", value=1, limit=1)
        await release.wait()
        return state

    return execute


# An output refused when its member ends some other way than by returning it: the
# member ends `errored` in the record, its event and the ledger, as when it returns it.
@pytest.mark.anyio
@pytest.mark.parametrize(
    "ending,native",
    [
        ("custom-limit", "member_limit:custom"),
        ("cancelled", "cancelled"),
        ("end-request", "stopped"),
    ],
)
@pytest.mark.parametrize("refused", [True, False], ids=["refused", "native"])
async def test_a_refused_output_ends_its_member_errored_however_it_ends(
    ending: str, native: str, refused: bool
) -> None:
    ready, release = anyio.Event(), anyio.Event()

    def build() -> ModelOutput:
        return _with_metadata({"diagnostic": object()} if refused else {"x": 1})

    async def run(control: SwarmControl) -> StopReason:
        control.start("odd")
        await ready.wait()
        if ending == "end-request":
            control.member("odd").end(origin="test")
            release.set()
        if ending != "cancelled":
            async for _ in control.events():
                pass
        return "all_done"

    solver = swarm(
        member(holds(build, ending, ready, release), name="odd"),
        controller=controller(run, f"result_test_refused_{ending}"),
        channels=[],
        budget=Budget(grace=0.05),
    )
    async with sample() as transcript:
        if refused:
            with pytest.raises(TypeError, match="output of member 'odd' holds"):
                await run_agent(solver, "task")
        else:
            await run_agent(solver, "task")
        record = _transcript_result(store())
        ledger = SwarmLedger.model_validate(store().get(LEDGER_KEY))
    status = "errored" if refused else native
    assert record is not None
    [m] = record.members
    assert (m.status, m.left_out) == (status, ["output"] if refused else [])
    [ended] = swarm_events(transcript, "member_result")
    assert ended.data["status"] == status
    assert [entry.status for entry in ledger.members] == [status]


class _TaskOutput(ModelOutput):
    """A `ModelOutput` subclass, as task code might define."""


@pytest.mark.anyio
async def test_a_subclass_of_an_inspect_model_fails_the_sample_before_it_is_read() -> (
    None
):
    rows = iter([1, 2])

    def build() -> ModelOutput:
        base = ModelOutput.from_content("mockllm", "41")
        # data that would be left out, and data that would be refused, inside it
        return _TaskOutput(
            model=base.model,
            choices=base.choices,
            metadata={"value": _aliased(60), "rows": rows},
        )

    async with sample():
        with pytest.raises(
            TypeError,
            match=r"output of member 'odd' holds a value of type _TaskOutput at output\. ",
        ):
            await run_agent(_plain_then(build, "result_test_subclass"), "task")
    assert next(rows) == 1


@pytest.mark.parametrize(
    "deep",
    [
        pytest.param(lambda: _with_metadata({"value": _nested(194)}), id="metadata"),
        pytest.param(lambda: _tool_call({"value": _nested(194)}), id="tool-arguments"),
    ],
)
def test_an_output_too_deep_for_a_json_log_is_left_out(
    tmp_path: Path, deep: Callable[[], ModelOutput]
) -> None:
    task = Task(
        dataset=[Sample(input="task")],
        solver=_plain_then(deep, "result_test_too_deep"),
    )
    [log] = eval(
        task,
        model="mockllm/model",
        log_dir=str(tmp_path),
        log_format="json",
        display="none",
    )
    assert log.status == "success"
    samples = read_eval_log(log.location).samples
    assert samples is not None
    record = swarm_result(samples[0])
    assert record is not None
    _assert_left_out(record)


def _cited_range() -> ModelOutput:
    citation = UrlCitation(url="https://example.com", cited_text=(0, 2))
    output = ModelOutput.from_content(
        "mockllm", [ContentText(text="41", citations=[citation])]
    )
    output.message.id = "odd"
    return output


def _infinite_logprob() -> ModelOutput:
    output = ModelOutput.from_content("mockllm", "41")
    output.message.id = "odd"
    output.choices[0].logprobs = Logprobs(
        content=[Logprob(token="41", logprob=float("-inf"))]
    )
    return output


def _odd_metadata(value: Any) -> Callable[[], ModelOutput]:
    def build() -> ModelOutput:
        output = _with_metadata({"value": value})
        output.message.id = "odd"
        return output

    return build


@pytest.mark.anyio
@pytest.mark.parametrize(
    "built,kept",
    [
        # a citation's range is a tuple, which JSON holds as a list
        pytest.param(_cited_range, _cited_range, id="cited-range"),
        # a float field keeps an infinite number in JSON
        pytest.param(_infinite_logprob, _infinite_logprob, id="infinite-logprob"),
        # tool arguments as deep as Inspect accepts them
        pytest.param(
            lambda: _tool_call({"value": _nested(98)}),
            lambda: _tool_call({"value": _nested(98)}),
            id="deepest-tool-arguments",
        ),
        # a date, as YAML tool arguments give, which the log writes as text
        pytest.param(
            lambda: _tool_call({"when": date(2024, 1, 2)}),
            lambda: _tool_call({"when": "2024-01-02"}),
            id="date-in-tool-arguments",
        ),
        # YAML's other types, of their own classes, which the log writes as JSON
        pytest.param(
            _odd_metadata(
                {"at": datetime(2024, 1, 2, 3, 4, 5), "raw": b"hi", "tags": {1}}
            ),
            _odd_metadata({"at": "2024-01-02T03:04:05", "raw": "hi", "tags": [1]}),
            id="yaml-types-in-metadata",
        ),
        # the time zones YAML gives: UTC and fixed offsets
        pytest.param(
            _odd_metadata(
                {
                    "utc": _at(timezone.utc),
                    "offset": _at(timezone(timedelta(hours=1, minutes=30))),
                }
            ),
            _odd_metadata(
                {"utc": "2024-01-02T03:04:05Z", "offset": "2024-01-02T03:04:05+01:30"}
            ),
            id="yaml-time-zones-in-metadata",
        ),
        pytest.param(
            _odd_metadata((1, 2)), _odd_metadata([1, 2]), id="tuple-in-metadata"
        ),
    ],
)
async def test_an_output_is_recorded_as_a_log_keeps_a_samples_output(
    built: Callable[[], ModelOutput], kept: Callable[[], ModelOutput]
) -> None:
    stop = await run_swarm(_plain_then(built, "result_test_kept"))
    _, odd = stop.record.members
    assert odd.left_out == [] and odd.output == kept()


def _argument(output: ModelOutput) -> Any:
    [call] = output.message.tool_calls or []
    return call.arguments["value"]


def _metadata(output: ModelOutput) -> Any:
    return (output.metadata or {})["value"]


@pytest.mark.parametrize(
    "odd,value,kept",
    [
        pytest.param(
            lambda: _parsed_tool_call('{"value": NaN}'),
            _argument,
            "nan",
            id="nan-in-tool-arguments",
        ),
        pytest.param(
            _odd_metadata(float("-inf")),
            _metadata,
            "-inf",
            id="infinity-in-metadata",
        ),
    ],
)
def test_the_live_record_is_the_record_its_logged_sample_gives_back(
    tmp_path: Path,
    odd: Callable[[], ModelOutput],
    value: Callable[[ModelOutput], Any],
    kept: str,
) -> None:
    live: list[SwarmResultRecord] = []

    @scorer(metrics=[])
    def keep_record() -> Scorer:
        async def score(state: TaskState, target: Target) -> Score:
            record = swarm_result(state)
            assert record is not None
            live.append(record)
            return Score(value=0)

        return score

    task = Task(
        dataset=[Sample(input="task")],
        solver=_plain_then(odd, "result_test_live_and_logged"),
        scorer=keep_record(),
    )
    [log] = eval(task, model="mockllm/model", log_dir=str(tmp_path), display="none")
    samples = read_eval_log(log.location).samples
    assert samples is not None
    logged = swarm_result(samples[0])
    [record] = live
    assert logged is not None
    # compared as JSON, in which a NaN is equal to itself
    assert logged.model_dump_json() == record.model_dump_json()
    _, member = record.members
    assert member.left_out == [] and member.output is not None
    assert repr(value(member.output)) == kept


def test_a_message_without_an_id_reads_back_without_one(tmp_path: Path) -> None:
    def no_id() -> ModelOutput:
        output = ModelOutput.from_content("mockllm", "41")
        output.message.id = None
        return output

    task = Task(
        dataset=[Sample(input="task")], solver=_plain_then(no_id, "result_test_no_id")
    )
    [log] = eval(task, model="mockllm/model", log_dir=str(tmp_path), display="none")
    samples = read_eval_log(log.location).samples
    assert samples is not None
    # each read gives the same record, with no id made up for the message
    for _ in range(2):
        record = swarm_result(samples[0])
        assert record is not None
        _, odd = record.members
        assert odd.output is not None and odd.output.message.id is None


# A swarm whose plain member's submission comes first, so that it is the answer, and
# whose odd member then returns what `odd` builds.
def _plain_then(odd: Callable[[], ModelOutput], name: str) -> Agent:
    async def run(control: SwarmControl) -> StopReason:
        for member_name in ("plain", "odd"):
            control.start(member_name)
            async for _ in control.events():
                pass
        return "all_done"

    return swarm(
        [member(answers("42"), name="plain"), member(returns(odd), name="odd")],
        controller=controller(run, name),
        channels=[],
    )


def _assert_left_out(record: SwarmResultRecord) -> None:
    plain, left = record.members
    assert record.final.member == "plain"
    assert plain.left_out == [] and plain.output is not None
    assert plain.output.completion == "42"
    assert (left.status, left.output, left.left_out) == ("submitted", None, ["output"])


@pytest.mark.anyio
@pytest.mark.parametrize(
    "result,match",
    [
        pytest.param(
            answer_result(answer_key=lambda text: 42),  # pyright: ignore[reportArgumentType]
            "answer_key must return a str or None, got a int",
            id="answer-key-not-a-str",
        ),
        pytest.param(
            answer_result(verifier=lambda answer, metadata: _verdict(value="high")),
            "a Verdict whose passed is a bool, value a float",
            id="verdict-value-not-a-float",
        ),
        # a float cannot hold them: one would be rounded, the other overflows
        pytest.param(
            answer_result(verifier=lambda answer, metadata: _verdict(value=2**53 + 1)),
            "an int a float holds exactly",
            id="verdict-value-rounded-as-a-float",
        ),
        pytest.param(
            answer_result(verifier=lambda answer, metadata: _verdict(value=10**400)),
            "an int a float holds exactly",
            id="verdict-value-too-large-for-a-float",
        ),
        # too long to print, so the error names its type, not its value
        pytest.param(
            answer_result(verifier=lambda answer, metadata: _verdict(value=16**4000)),
            "value of type int and explanation of type NoneType for member",
            id="verdict-value-too-long-to-print",
        ),
        # checked by their actual classes, not the ones they report
        pytest.param(
            answer_result(answer_key=lambda text: _Posing(str)),  # pyright: ignore[reportArgumentType]
            "answer_key must return a str or None, got a _Posing",
            id="answer-key-posing-as-a-str",
        ),
        pytest.param(
            answer_result(
                verifier=lambda answer, metadata: _returning(
                    _Posing(Verdict, passed=True, value=None, explanation=None)
                )
            ),
            "must return a Verdict, got a _Posing",
            id="verdict-posing-as-a-verdict",
        ),
        *[
            pytest.param(
                answer_result(
                    verifier=lambda answer, metadata, field=field, cls=cls: _returning(
                        replace(Verdict(passed=True), **{field: _Posing(cls)})
                    )
                ),
                f"{field} of type _Posing",
                id=f"verdict-{field}-posing-as-a-{cls.__name__}",
            )
            for field, cls in [
                ("passed", bool),
                ("value", float),
                ("explanation", str),
            ]
        ],
    ],
)
async def test_a_mistyped_contract_result_fails_the_sample_with_its_record(
    result: ResultSpec, match: str
) -> None:
    async with sample():
        with pytest.raises(TypeError, match=match):
            await run_agent(swarm(answers("42"), channels=[], result=result), "task")
        record = _transcript_result(store())
        ledger = store().get(LEDGER_KEY)
    # the member's end, its output, the record and the accounting are kept
    assert record is not None and ledger is not None
    [m] = record.members
    assert (m.state, m.status) == ("ended", "submitted")
    assert m.output is not None and m.output.completion == "42"
    assert (m.answer_key, m.verdict) == (None, None)


async def _verdict(value: Any) -> Verdict:
    return Verdict(passed=True, value=value)


async def _returning(value: Any) -> Any:
    return value


class _Posing:
    """An object that reports another class as its own, with the given attributes."""

    def __init__(self, cls: type, **attributes: Any) -> None:
        self.cls = cls
        self.__dict__.update(attributes)

    @property
    def __class__(self) -> type:  # pyright: ignore[reportIncompatibleMethodOverride]
        return self.cls


@pytest.mark.anyio
async def test_a_verdict_of_nan_is_recorded_whole() -> None:
    async def nan_check(answer: str, metadata: dict[str, Any]) -> Verdict:
        return Verdict(passed=True, value=math.nan, explanation="graded")

    stop = await run_swarm(
        swarm(answers("42"), channels=[], result=answer_result(verifier=nan_check))
    )
    [m] = stop.record.members
    assert m.verdict is not None and math.isnan(m.verdict.value or 0.0)
    assert (m.verdict.passed, m.verdict.explanation, m.left_out) == (
        True,
        "graded",
        [],
    )


class _Passed(Verdict):
    """A verdict whose constructor takes no `passed`: it always passes."""

    def __init__(self, explanation: str | None = None) -> None:
        super().__init__(passed=True, explanation=explanation)


class _Explained(Verdict):
    """A verdict whose constructor makes it pass only when it has an explanation."""

    def __init__(
        self,
        passed: bool = False,
        value: float | None = None,
        explanation: str | None = None,
    ) -> None:
        super().__init__(explanation is not None, value, explanation)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "verdict,recorded,left_out",
    [
        pytest.param(
            Verdict(True, 1.0, "fine"), Verdict(True, 1.0, "fine"), [], id="plain"
        ),
        # text that reads as an attachment reference: the explanation alone is left out
        pytest.param(
            Verdict(True, 1.0, "attachment://0123"),
            Verdict(True, 1.0),
            ["explanation"],
            id="plain-explanation-left-out",
        ),
        # a subclass's own constructor never runs on the record's copy
        pytest.param(_Passed(), Verdict(True), [], id="constructor-without-fields"),
        pytest.param(
            _Passed("fine"), Verdict(True, None, "fine"), [], id="constructor-explained"
        ),
        pytest.param(
            _Explained(value=1.0, explanation="attachment://0123"),
            Verdict(True, 1.0),
            ["explanation"],
            id="constructor-recomputes-passed",
        ),
    ],
)
async def test_a_verdict_is_recorded_by_its_fields(
    verdict: Verdict, recorded: Verdict, left_out: list[str]
) -> None:
    stop = await run_swarm(
        swarm(
            answers("42"),
            channels=[],
            result=answer_result(verifier=lambda answer, metadata: _returning(verdict)),
        )
    )
    [m] = stop.record.members
    assert (m.verdict, m.left_out) == (recorded, left_out)
    [event] = swarm_events(stop.transcript, "verdict")
    assert (event.data["passed"], event.data["value"]) == (
        recorded.passed,
        recorded.value,
    )
