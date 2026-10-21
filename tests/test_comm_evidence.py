from pathlib import Path
from typing import Any

import pytest
from inspect_ai import Task, eval_async  # pyright: ignore[reportUnknownVariableType]
from inspect_ai.agent import Agent, react
from inspect_ai.dataset import Sample
from inspect_ai.event import InfoEvent
from inspect_ai.model import ModelOutput

from inspect_swarm import member, shared_memory, swarm, swarm_tools
from inspect_swarm._metrics import CommMetrics, MemberMetrics, SwarmMetrics

from .swarm_runtime import Script, scripted_model


async def _members(swarm: Agent, tmp_path: Path) -> list[MemberMetrics]:
    # each member's metrics from the swarm's metrics event in an eval of one sample
    [log] = await eval_async(
        Task(dataset=[Sample(input="task")], solver=swarm),
        model="mockllm/model",
        log_dir=str(tmp_path),
    )
    assert log.status == "success", log.error
    assert log.samples is not None
    [data] = [
        e.data
        for e in log.samples[0].events
        if isinstance(e, InfoEvent)
        and e.source == "inspect_swarm"
        and isinstance(e.data, dict)
        and e.data.get("type") == "metrics"
    ]
    assert isinstance(data, dict)
    record = {k: v for k, v in data.items() if k != "type"}
    return SwarmMetrics.model_validate(record).members


def _member(name: str, *calls: dict[str, Any]) -> Any:
    # a react member that makes these shared_memory calls in turn, then submits
    outputs = [
        *(ModelOutput.for_tool_call("mockllm", "shared_memory", c) for c in calls),
        ModelOutput.for_tool_call("mockllm", "submit", {"answer": "done"}),
    ]
    model = scripted_model(Script(outputs))
    return member(react(model=model, tools=[swarm_tools()]), name=name)


@pytest.mark.anyio
async def test_records_rejections_and_reads_are_counted_per_member_and_channel(
    tmp_path: Path,
) -> None:
    def create(path: str, text: str) -> dict[str, Any]:
        return {
            "command": "create",
            "path": f"/shared_memory/{path}",
            "file_text": text,
        }

    metrics = await _members(
        swarm(
            [
                _member(
                    "p",
                    create("a.md", "héllo"),
                    # over max_file_bytes, refused at admission
                    create("big.md", "z" * 21),
                    create("b.md", "x" * 10),
                    # the rate's third write in its window
                    create("c.md", "y"),
                    {"command": "view", "path": "/shared_memory/a.md"},
                    {"command": "view", "path": "/shared_memory"},
                ),
                _member("w", {"command": "view", "path": "/shared_memory"}),
                _member("q"),
            ],
            channels=[shared_memory(max_file_bytes=20, rate=(2, 3600.0))],
        ),
        tmp_path,
    )
    assert {m.name: m.comm for m in metrics} == {
        "p": {
            "shared_memory": CommMetrics(
                records=2,
                # "héllo" is 6 UTF-8 bytes
                record_bytes=16,
                rejections={"admit": 1, "rate": 1},
                reads=1,
                listings=1,
            )
        },
        "w": {"shared_memory": CommMetrics(listings=1)},
        "q": {"shared_memory": CommMetrics()},
    }


@pytest.mark.anyio
async def test_a_swarm_without_a_channel_with_records_has_no_comm_metrics(
    tmp_path: Path,
) -> None:
    [metrics] = await _members(swarm(_member("w"), channels=[]), tmp_path)
    assert metrics.comm == {}
