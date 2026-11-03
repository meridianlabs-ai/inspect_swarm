"""The examples in `examples/`, run as `inspect eval examples/<file>.py` runs them, with scripted mockllm members."""

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import anyio
import pytest
from inspect_ai import eval  # pyright: ignore[reportUnknownVariableType]
from inspect_ai.log import EvalSample
from inspect_ai.model import (
    ChatMessage,
    ChatMessageAssistant,
    ChatMessageTool,
    GenerateConfig,
    Model,
    ModelCost,
    ModelInfo,
    ModelOutput,
    get_model,
    set_model_info,
)
from inspect_ai.tool import ToolCall, ToolChoice, ToolInfo

from inspect_swarm import swarm_result
from inspect_swarm._evidence import METRICS_KEY
from inspect_swarm._metrics import SwarmMetrics
from inspect_swarm._record import SwarmResultRecord
from inspect_swarm.analysis import swarm_ledger

EXAMPLES = Path(__file__).parent.parent / "examples"

# what a member does next, from its name and the conversation so far
Policy = Callable[[str, list[ChatMessage]], ModelOutput]

_NAME = re.compile(r"You are ([a-z][a-z0-9_-]*), (?:one of|the only)")


def _model(
    policy: Policy, cost: float | None = None, delays: dict[str, float] | None = None
) -> Model:
    """A mockllm model that answers each member's calls with `policy`, each call of a member in `delays` taking that many seconds."""
    name = f"mockllm/examples-{uuid4().hex}"
    if cost is not None:
        set_model_info(
            name,
            ModelInfo(
                cost=ModelCost(
                    input=cost, output=cost, input_cache_write=0, input_cache_read=0
                )
            ),
        )

    async def outputs(
        input: list[ChatMessage],
        tools: list[ToolInfo],
        tool_choice: ToolChoice,
        config: GenerateConfig,
    ) -> ModelOutput:
        match = next(
            (m for msg in input if (m := _NAME.search(msg.text)) is not None), None
        )
        assert match is not None, "a member's input has its preamble"
        await anyio.sleep((delays or {}).get(match.group(1), 0))
        return policy(match.group(1), input)

    return get_model(name, memoize=False, custom_outputs=outputs)


def _turn(messages: list[ChatMessage]) -> int:
    return sum(isinstance(m, ChatMessageAssistant) for m in messages)


def _call(function: str, **arguments: Any) -> ModelOutput:
    return ModelOutput.for_tool_call("mockllm", function, arguments)


def _run(
    example: str, model: Model, tmp_path: Path, **task_args: Any
) -> list[EvalSample]:
    # `example` is `<file>@<task>`; the file is found from its absolute path
    [log] = eval(
        str(EXAMPLES / example),
        model=model,
        task_args=task_args,
        log_dir=str(tmp_path),
        display="none",
    )
    assert log.status == "success", log.error
    assert log.samples is not None
    return log.samples


def _record(sample: EvalSample) -> SwarmResultRecord:
    record = swarm_result(sample)
    assert record is not None
    return record


def _answers(name: str, messages: list[ChatMessage]) -> ModelOutput:
    # the questions' answers, by a phrase of each question
    answers = {"greater than 1000": "1009", "100!": "24", "2 to the power 15": "26"}
    conversation = "\n".join(m.text for m in messages)
    [answer] = [a for phrase, a in answers.items() if phrase in conversation]
    return _call("submit", answer=answer)


def test_getting_started(tmp_path: Path) -> None:
    samples = _run(
        "getting_started.py@getting_started", _model(_answers), tmp_path, k=2
    )
    assert len(samples) == 3
    for sample in samples:
        assert sample.scores is not None
        assert [s.value for s in sample.scores.values()] == ["C"]
        record = _record(sample)
        assert [m.name for m in record.members] == ["react-1", "react-2"]
        assert record.final.member is not None


def test_shared_memory(tmp_path: Path) -> None:
    def policy(name: str, messages: list[ChatMessage]) -> ModelOutput:
        match _turn(messages):
            case 0:
                return _call(
                    "shared_memory",
                    command="append",
                    path="/shared_memory/counts.md",
                    append_text=f"{name}: 1-999: 168",
                )
            case 1:
                return _call("swarm_wait", timeout=10)
            case _:
                return _call("submit", answer="430")

    [sample] = _run(
        "shared_memory.py@shared_memory_count", _model(policy), tmp_path, k=3
    )
    assert sample.scores is not None
    assert [s.value for s in sample.scores.values()] == ["C"]
    record = _record(sample)
    assert record.final.member is not None
    metrics = SwarmMetrics.model_validate(sample.store[METRICS_KEY])
    [answered] = [m for m in metrics.members if m.name == record.final.member]
    # the member that answered appended once and waited once
    assert answered.comm["shared_memory"].records == 1
    assert answered.waits == 1


def test_stop_on_proof(tmp_path: Path) -> None:
    def policy(name: str, messages: list[ChatMessage]) -> ModelOutput:
        # checked_solver-1 never answers correctly; checked_solver-2 does on its
        # second submission
        right = name == "checked_solver-2" and _turn(messages) >= 1
        return _answers(name, messages) if right else _call("submit", answer="0")

    samples = _run(
        "stop_on_proof.py@stop_on_proof", _model(policy), tmp_path, k=2, attempts=3
    )
    for sample in samples:
        ledger = swarm_ledger(sample)
        assert ledger is not None
        assert ledger.stop_reason == "controller:proved"
        record = _record(sample)
        assert record.final.member == "checked_solver-2"
        first, second = record.members
        # the unchecked last attempt is not a proof, so the member stopped itself
        assert first.status in ("member_limit:custom", "stopped")
        assert (second.status, second.gate_checks, second.gate_passes) == (
            "submitted",
            2,
            1,
        )


def test_stop_on_proof_needs_two_attempts(tmp_path: Path) -> None:
    # with one attempt react() checks no submission, so no member could prove anything
    with pytest.raises(ValueError, match="attempts must be at least 2"):
        _run("stop_on_proof.py@stop_on_proof", _model(_answers), tmp_path, attempts=1)


def test_budget_sweep(tmp_path: Path) -> None:
    def policy(name: str, messages: list[ChatMessage]) -> ModelOutput:
        if _turn(messages) == 0:
            return _call("swarm_resources")
        return _answers(name, messages)

    samples = _run(
        "budget_sweep.py@budget_sweep",
        _model(policy, cost=1.0),
        tmp_path,
        k=2,
        budget=2.0,
        split="equal",
    )
    for sample in samples:
        ledger = swarm_ledger(sample)
        assert ledger is not None
        assert ledger.budget.split == "equal"
        assert ledger.caps["cost"] == pytest.approx(1.9)
        assert [m.name for m in ledger.members] == ["react-1", "react-2"]


_TASK = re.compile(r"(worker-\d+): (\d+): count the primes from (\d+) to (\d+)")
_RESULT = re.compile(r"(worker-\d+): (\d+): (\d+)")
_TASKS_FILE = 'path="/shared_memory/tasks.md"'
_RESULTS_FILE = 'path="/shared_memory/results.md"'


def _primes(first: int, last: int) -> int:
    return sum(
        n > 1 and all(n % d for d in range(2, int(n**0.5) + 1))
        for n in range(first, last + 1)
    )


def _last_call(messages: list[ChatMessage]) -> ToolCall | None:
    # the call whose result ends the conversation, if a tool result ends it
    if not isinstance(messages[-1], ChatMessageTool):
        return None
    assistant = next(
        m for m in reversed(messages) if isinstance(m, ChatMessageAssistant)
    )
    assert assistant.tool_calls
    return assistant.tool_calls[0]


def _appended(messages: list[ChatMessage], path: str) -> str:
    # the text the member appended to `path`, every append of the conversation
    return "\n".join(
        str(call.arguments["append_text"])
        for m in messages
        if isinstance(m, ChatMessageAssistant)
        for call in m.tool_calls or []
        if call.function == "shared_memory"
        and call.arguments.get("command") == "append"
        and call.arguments.get("path") == path
    )


def _viewed(messages: list[ChatMessage], fence: str) -> str:
    # the latest view of the file whose fence names `fence`
    return next(
        (
            m.text
            for m in reversed(messages)
            if isinstance(m, ChatMessageTool) and fence in m.text
        ),
        "",
    )


def test_lead_and_workers(tmp_path: Path) -> None:
    ranges = [
        (1, 500),
        (501, 1000),
        (1001, 1500),
        (1501, 2000),
        (2001, 2500),
        (2501, 2999),
    ]

    def lead(messages: list[ChatMessage]) -> ModelOutput:
        tasks = _TASK.findall(_appended(messages, "/shared_memory/tasks.md"))
        results = {
            (w, int(n)): int(c)
            for w, n, c in _RESULT.findall(_viewed(messages, _RESULTS_FILE))
        }
        call = _last_call(messages)
        if call is not None and call.function == "swarm_wait":
            return _call(
                "shared_memory", command="view", path="/shared_memory/results.md"
            )
        if call is not None and call.arguments.get("command") == "append":
            return _call("swarm_wait", path="/shared_memory/results.md")
        if len(results) == len(ranges):
            return _call("submit", answer=str(sum(results.values())))
        # at the start and after each view of the results: each worker whose tasks
        # are done gets the next range
        given = {int(t[1]) for t in tasks}
        free = [n for n in range(1, len(ranges) + 1) if n not in given]
        lines: list[str] = []
        for worker in ("worker-1", "worker-2"):
            mine = [int(t[1]) for t in tasks if t[0] == worker]
            if free and all((worker, n) in results for n in mine):
                n = free.pop(0)
                first, last = ranges[n - 1]
                lines.append(f"{worker}: {n}: count the primes from {first} to {last}")
        if not lines:
            return _call("swarm_wait", path="/shared_memory/results.md")
        return _call(
            "shared_memory",
            command="append",
            path="/shared_memory/tasks.md",
            append_text="\n".join(lines),
        )

    def worker(name: str, messages: list[ChatMessage]) -> ModelOutput:
        # the worker protocol: do every task for it, then view whatever the status
        # line names before it submits
        call = _last_call(messages)
        if call is None:
            return _call(
                "shared_memory", command="view", path="/shared_memory/tasks.md"
            )
        done = {
            int(n)
            for w, n, _ in _RESULT.findall(
                _appended(messages, "/shared_memory/results.md")
            )
            if w == name
        }
        pending = [
            t
            for t in _TASK.findall(_viewed(messages, _TASKS_FILE))
            if t[0] == name and int(t[1]) not in done
        ]
        if pending:
            _, n, first, last = pending[0]
            return _call(
                "shared_memory",
                command="append",
                path="/shared_memory/results.md",
                append_text=f"{name}: {n}: {_primes(int(first), int(last))}",
            )
        if call.arguments.get("command") == "changes":
            changed = re.findall(r"f-\d+", messages[-1].text)
            if changed:
                return _call("shared_memory", command="view", file_id=changed[0])
            return _call("submit", answer="done")
        return _call("shared_memory", command="changes")

    def policy(name: str, messages: list[ChatMessage]) -> ModelOutput:
        return lead(messages) if name == "lead" else worker(name, messages)

    # the lead's turns are slow, so each worker has answered and is idle when the lead
    # gives it its next task
    [sample] = _run(
        "lead_and_workers.py@lead_and_workers_count",
        _model(policy, delays={"lead": 0.2}),
        tmp_path,
    )
    assert sample.scores is not None
    assert [s.value for s in sample.scores.values()] == ["C"]
    record = _record(sample)
    assert (record.final.mode, record.final.member) == ("controller", "lead")
    ledger = swarm_ledger(sample)
    assert ledger is not None and ledger.stop_reason == "submitted"
    metrics = {
        m.name: m
        for m in SwarmMetrics.model_validate(sample.store[METRICS_KEY]).members
    }
    # the lead's appends to the task list
    writes = metrics["lead"].comm["shared_memory"].records
    for name in ("worker-1", "worker-2"):
        # the controller woke the worker for the lead's writes, never again for a
        # write it had already read
        assert 3 <= metrics[name].activations <= writes + 1
        assert metrics[name].answers == metrics[name].activations


@pytest.mark.slow
def test_shared_sandbox(tmp_path: Path) -> None:
    script = "import sys\\nprint(len(open(sys.argv[1]).read().split()))\\n"

    def policy(name: str, messages: list[ChatMessage]) -> ModelOutput:
        match _turn(messages):
            case 0:
                return _call(
                    "bash",
                    command=f"mkdir -p /scratch/{name} && printf '{script}' > /shared/wordcount.py && echo '{name}: wrote it' >> /shared/notes.md",
                )
            case _:
                return _call("submit", answer="done")

    [sample] = _run("shared_sandbox.py@shared_sandbox", _model(policy), tmp_path, k=2)
    assert sample.scores is not None
    [score] = sample.scores.values()
    assert (score.value, score.answer) == ("C", "9")
    # every member ran to its end
    assert [m.status for m in _record(sample).members] == ["submitted", "submitted"]


@pytest.mark.slow
@pytest.mark.parametrize(
    "wrong,failure",
    [
        # 39 * 39 + 39 + 41 = 1601, which is prime
        ("echo 39 > /workspace/answer.txt", "1601 is prime"),
        ("echo forty > /workspace/answer.txt", "not a whole number"),
        ("true", "no answer file"),
    ],
)
def test_proof_swarm_scores_each_members_sandbox(
    tmp_path: Path, wrong: str, failure: str
) -> None:
    def policy(name: str, messages: list[ChatMessage]) -> ModelOutput:
        # agent-1 leaves a wrong answer, or none; agent-2 proves 40
        write = wrong if name == "agent-1" else "echo 40 > /workspace/answer.txt"
        match _turn(messages):
            case 0:
                return _call(
                    "shared_memory",
                    command="append",
                    path="/shared_memory/ruled_out.md",
                    append_text=f"{name} is testing",
                )
            case 1:
                return _call("bash", command=write)
            case 2:
                return _call("check")
            case _:
                return _call("submit", answer="40")

    # every member runs to its end, so both answers are there to score
    [sample] = _run(
        "proof_swarm.py@proof_swarm", _model(policy), tmp_path, k=2, stop="all_done"
    )
    assert sample.scores is not None
    [score] = sample.scores.values()
    assert score.value == "C"
    assert score.metadata is not None
    assert score.metadata["agent-2"].startswith("PASS")
    assert score.metadata["agent-1"].startswith("FAIL")
    assert failure in score.metadata["agent-1"]
    record = _record(sample)
    assert record.final.member == "agent-2"
    assert [(m.sandbox, m.status) for m in record.members] == [
        ("agent-1", "member_limit:custom"),
        ("agent-2", "submitted"),
    ]


@pytest.mark.slow
@pytest.mark.parametrize(
    "writes,status,stop_reason,value",
    [
        # a passing check, then a submission: the first proof stops the swarm
        (["40"], "submitted", "controller:proved", "C"),
        # a passing check, then the answer replaced with no further check: the answer
        # it leaves is checked again, and fails
        (["40", "39"], "member_limit:custom", "all_done", "I"),
    ],
)
def test_proof_swarm_of_one_checks_the_answer_it_leaves(
    tmp_path: Path, writes: list[str], status: str, stop_reason: str, value: str
) -> None:
    def policy(name: str, messages: list[ChatMessage]) -> ModelOutput:
        script = [
            _call("bash", command=f"echo {writes[0]} > /workspace/answer.txt"),
            _call("check"),
            *(
                _call("bash", command=f"echo {n} > /workspace/answer.txt")
                for n in writes[1:]
            ),
            _call("submit", answer=writes[0]),
        ]
        return script[min(_turn(messages), len(script) - 1)]

    [sample] = _run("proof_swarm.py@proof_swarm", _model(policy), tmp_path, k=1)
    [member] = _record(sample).members
    assert (member.name, member.sandbox, member.status) == (
        "agent-1",
        "agent-1",
        status,
    )
    ledger = swarm_ledger(sample)
    assert ledger is not None and ledger.stop_reason == stop_reason
    assert sample.scores is not None
    assert [s.value for s in sample.scores.values()] == [value]
