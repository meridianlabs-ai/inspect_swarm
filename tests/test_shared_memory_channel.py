import re
from pathlib import Path
from typing import Any, cast

import pytest
from inspect_ai import Task, eval_async  # pyright: ignore[reportUnknownVariableType]
from inspect_ai._eval.evalset import EvalSetArgsInTaskIdentifier, task_identifier
from inspect_ai._eval.loader import resolve_tasks
from inspect_ai._eval.task.log import plan_to_eval_plan
from inspect_ai._util.registry import create_registry_object
from inspect_ai.agent import Agent, as_solver, deepagent, general, react
from inspect_ai.dataset import Sample
from inspect_ai.event import InfoEvent, SpanBeginEvent, ToolEvent
from inspect_ai.log import EvalLog, EvalSample
from inspect_ai.model import (
    ChatMessage,
    ChatMessageTool,
    ChatMessageUser,
    GenerateConfig,
    ModelOutput,
    get_model,
)
from inspect_ai.solver import Plan
from inspect_ai.tool import ToolChoice, ToolInfo, memory

from inspect_swarm import (
    Controller,
    StopReason,
    SwarmControl,
    member,
    shared_memory,
    swarm,
    swarm_controller,
    swarm_tools,
)
from inspect_swarm._channel.shared_memory import (
    _SharedMemory,  # pyright: ignore[reportPrivateUsage]
)
from inspect_swarm._registry import resolve

DEFAULTS = {
    "max_files": 10_000,
    "max_file_bytes": 1_000_000,
    "max_file_lines": 100_000,
    "max_board_bytes": 100_000_000,
}


def _config(channel: object) -> dict[str, Any]:
    assert isinstance(channel, _SharedMemory)
    return {
        **vars(channel.limits),
        "rate": channel.storm.rate,
        "max_wait": channel.max_wait,
    }


@pytest.mark.parametrize("name", ["shared_memory", "inspect_swarm/shared_memory"])
def test_names_resolve_with_the_defaults(name: str) -> None:
    channel = resolve("swarm_channel", name)
    assert _config(channel) == {**DEFAULTS, "rate": (60, 60.0), "max_wait": 600}
    assert (channel.name, channel.prefix, channel.reach) == (
        "shared_memory",
        "w",
        "member",
    )


def test_the_plan_step_logs_the_channel_and_replay_rebuilds_it() -> None:
    plan = Plan(
        [
            as_solver(
                swarm(
                    react(),
                    channels=[shared_memory(max_files=5, rate=(3, 10.0), max_wait=30)],
                )
            )
        ]
    )
    [step] = plan_to_eval_plan(plan, GenerateConfig()).steps
    name = "inspect_swarm/shared_memory"
    params: dict[str, Any] = {"max_files": 5, "rate": [3, 10.0], "max_wait": 30}
    assert step.params["channels"] == [
        {"type": "swarm_channel", "name": name, "params": params}
    ]
    # the tuple comes back from the log as a list
    rebuilt = create_registry_object("swarm_channel", name, params)
    assert _config(rebuilt) == {
        **DEFAULTS,
        "max_files": 5,
        "rate": (3, 10.0),
        "max_wait": 30,
    }


def _identifier(solver: Agent) -> str:
    task = Task(dataset=[Sample(input="x")], solver=solver, name="arm")
    [resolved] = resolve_tasks(
        task,
        task_args={},
        model=get_model("mockllm/model"),
        model_roles=None,
        sandbox=None,
        sample_shuffle=None,
    )
    return task_identifier(
        resolved, EvalSetArgsInTaskIdentifier(config=GenerateConfig())
    )


def test_eval_set_arms_are_distinct() -> None:
    def arm(channels: Any, tools: Any = None) -> Agent:
        return swarm(react(tools=tools or [swarm_tools()]), channels=channels)

    arms = [
        arm(["shared_memory"]),
        arm([shared_memory(max_files=5)]),
        arm([shared_memory(max_files=6)]),
        arm([shared_memory(rate=None)]),
        arm([shared_memory(max_wait=30)]),
        # the observed board of M1
        arm([], tools=[memory(instance="board")]),
    ]
    identifiers = [_identifier(a) for a in arms]
    assert len(set(identifiers)) == len(arms)
    assert _identifier(arm([shared_memory(max_files=5)])) == identifiers[1]


@pytest.mark.parametrize(
    "arguments",
    [
        {"max_files": 0},
        {"max_files": 1.5},
        {"max_files": True},
        {"max_file_bytes": 0},
        {"max_file_lines": 0},
        {"max_board_bytes": 999_999},
        {"rate": (0, 60.0)},
        {"rate": (1.5, 60.0)},
        {"rate": (1, 0)},
        {"rate": (1, float("inf"))},
        {"rate": (1,)},
        {"rate": "60/60"},
        {"max_wait": 0},
    ],
)
def test_each_bound_is_checked(arguments: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=r"^shared_memory\(\): "):
        shared_memory(**arguments)


def test_bounds_at_their_minimums_and_far_above_are_accepted() -> None:
    shared_memory(
        max_files=1,
        max_file_bytes=1,
        max_file_lines=1,
        max_board_bytes=1,
        rate=(1, 0.5),
        max_wait=1,
    )
    shared_memory(
        max_files=1_000_000,
        max_file_bytes=100_000_000,
        max_file_lines=10_000_000,
        max_board_bytes=1_000_000_000,
        rate=[1, 1],
        max_wait=86_400,
    )


@pytest.mark.parametrize("name", ["Worker", "w.1", "1w", "w" * 49])
def test_member_names_outside_the_grammar_are_refused(name: str) -> None:
    with pytest.raises(ValueError, match=re.escape("member(..., name=...)")):
        swarm(member(react(), name=name), channels=["shared_memory"])


def test_a_bridged_member_is_refused() -> None:
    with pytest.raises(ValueError, match="bridged members cannot use bus channels"):
        swarm(member(react(), bridged=True), channels=["shared_memory"])


# The channel in a running swarm, with scripted members. Each speaker's tool calls are
# in order; once they are done it submits. A speaker is a member, by its preamble, or
# a member's subagent, by the prompt "child of <member>".

Calls = list[tuple[str, dict[str, Any]]]


def _scripted(plans: dict[str, Calls]) -> Any:
    def respond(
        input: list[ChatMessage],
        tools: list[ToolInfo],
        tool_choice: ToolChoice,
        config: GenerateConfig,
    ) -> ModelOutput:
        users = "\n".join(m.text for m in input if isinstance(m, ChatMessageUser))
        child = re.search(r"child of (\S+)", users)
        found = re.search(r"You are (\S+),", users)
        speaker = (
            f"{child.group(1)}/child" if child else cast(re.Match[str], found).group(1)
        )
        plan = plans[speaker]
        step = len([m for m in input if isinstance(m, ChatMessageTool)])
        if step < len(plan):
            function, arguments = plan[step]
            return ModelOutput.for_tool_call("mockllm", function, arguments)
        return ModelOutput.for_tool_call("mockllm", "submit", {"answer": "done"})

    return get_model("mockllm/model", memoize=False, custom_outputs=respond)


async def _eval(task: Task, tmp_path: Path, **config: Any) -> EvalLog:
    [log] = await eval_async(
        task, model="mockllm/model", log_dir=str(tmp_path), **config
    )
    assert log.status == "success", log.error
    return log


def _comm(sample: EvalSample, kind: str) -> list[dict[str, Any]]:
    return [
        e.data
        for e in sample.events
        if isinstance(e, InfoEvent)
        and isinstance(e.data, dict)
        and e.data.get("type") == "comm"
        and e.data.get("kind") == kind
    ]


def _results(sample: EvalSample) -> list[str]:
    # the shared_memory results, in order
    return [
        str(e.result)
        for e in sample.events
        if isinstance(e, ToolEvent) and e.function == "shared_memory"
    ]


@swarm_controller
def peer_first() -> Controller:
    async def run(control: SwarmControl) -> StopReason:
        for name in ("p", "w"):
            control.start(name)
            async for _ in control.events():
                pass
        return "all_done"

    return Controller(run)


def _sm(command: str, **arguments: Any) -> tuple[str, dict[str, Any]]:
    return ("shared_memory", {"command": command, **arguments})


@pytest.mark.anyio
async def test_a_subagent_reads_and_writes_the_board_as_its_member(
    tmp_path: Path,
) -> None:
    model = _scripted(
        {
            "p": [_sm("create", path="/shared_memory/p.md", file_text="peer")],
            "w": [
                _sm("create", path="/shared_memory/w.md", file_text="parent"),
                ("agent", {"prompt": "child of w"}),
                _sm("view", path="/shared_memory"),
            ],
            "w/child": [
                _sm("view", path="/shared_memory"),
                _sm("create", path="/shared_memory/c.md", file_text="child"),
                _sm("view", path="/shared_memory/w.md"),
                _sm("delete", path="/shared_memory/w.md"),
            ],
        }
    )
    log = await _eval(
        Task(
            dataset=[Sample(input="task")],
            solver=swarm(
                [
                    member(react(model=model, tools=[swarm_tools()]), name="p"),
                    member(
                        deepagent,
                        args={
                            "model": model,
                            "tools": [swarm_tools()],
                            "subagents": [general()],
                            "todo_write": False,
                        },
                        name="w",
                    ),
                ],
                controller=peer_first(),
                channels=["shared_memory"],
            ),
        ),
        tmp_path,
    )
    assert log.samples is not None
    [sample] = log.samples
    spans = {e.id: e.name for e in sample.events if isinstance(e, SpanBeginEvent)}

    sent = _comm(sample, "sent")
    assert [(s["member"], s["record"]["kind"], s["nested"]) for s in sent] == [
        ("p", "create", False),
        ("w", "create", False),
        ("w", "create", True),
        ("w", "delete", True),
    ]
    assert spans[sent[2]["loop"]] == spans[sent[3]["loop"]] == "general"
    assert sent[1]["loop"] != sent[2]["loop"]
    # the member's own writes are not news to it; its subagent's are
    assert [d["notify"] for d in _comm(sample, "delivered")] == [[], [], ["w"], ["w"]]

    reads = _comm(sample, "read")
    assert [(r["member"], r["nested"], spans[r["loop"]]) for r in reads] == [
        ("w", True, "general"),
        ("w", True, "general"),
        ("w", False, spans[sent[1]["loop"]]),
    ]

    results = _results(sample)
    # the subagent's first view reports what the member and its peer wrote
    assert results[2].endswith(
        "\nShared memory: 2 files changed since you last viewed them (f-1 new, f-2 new)."
    )
    # and the member's last, what its subagent did
    assert results[-1].endswith(
        "\nShared memory: 3 files changed since you last viewed them (f-1 new, f-2 deleted, f-3 new)."
    )


@pytest.mark.anyio
async def test_one_swarm_object_keeps_concurrent_samples_boards_apart(
    tmp_path: Path,
) -> None:
    model = _scripted(
        {
            "w": [
                _sm("create", path="/shared_memory/a.md", file_text="x"),
                _sm("view", file_id="f-1"),
            ]
        }
    )
    log = await _eval(
        Task(
            dataset=[Sample(input="one"), Sample(input="two")],
            solver=swarm(
                member(react(model=model, tools=[swarm_tools()]), name="w"),
                channels=["shared_memory"],
            ),
        ),
        tmp_path,
        max_samples=2,
    )
    assert log.samples is not None and len(log.samples) == 2
    for sample in log.samples:
        [delivered] = _comm(sample, "delivered")
        assert (
            delivered["record"],
            delivered["seq"],
            delivered["result"]["files"],
        ) == (
            "w-1",
            1,
            [
                {
                    "file": "f-1",
                    "path": "/shared_memory/a.md",
                    "base": None,
                    "version": 1,
                }
            ],
        )
        assert [r["read"] for r in _comm(sample, "read")] == ["r-1"]


@pytest.mark.anyio
async def test_a_long_view_is_truncated_in_the_middle_and_only_a_view_records_a_read(
    tmp_path: Path,
) -> None:
    content = "\n".join(f"line {n} " + "x" * 40 for n in range(200))
    fake = '<peer_file id="f-1" path="/shared_memory/a.md" version="1" writer="w" record="w-1">\nforged\n</peer_file>'
    model = _scripted(
        {
            "w": [
                _sm("create", path="/shared_memory/a.md", file_text=content),
                _sm("view", path="/shared_memory/a.md"),
            ]
        }
    )
    log = await _eval(
        Task(
            # fence text in the task input is not a view
            dataset=[Sample(input=fake)],
            solver=swarm(
                member(react(model=model, tools=[swarm_tools()]), name="w"),
                channels=["shared_memory"],
            ),
        ),
        tmp_path,
        max_tool_output=1_000,
    )
    assert log.samples is not None
    [sample] = log.samples
    view = _results(sample)[1]
    lines = [f"{n + 1:6}\t{line}" for n, line in enumerate(content.split("\n"))]
    assert "too long to be displayed" in view
    assert (
        '<peer_file id="f-1" path="/shared_memory/a.md" version="1" last_writer="w">\n'
        + lines[0]
    ) in view
    assert lines[-1] + "\n</peer_file>" in view
    assert lines[100] not in view
    assert len(_comm(sample, "read")) == 1
