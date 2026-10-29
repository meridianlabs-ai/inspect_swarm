import re
from pathlib import Path
from typing import Any

import pytest
from inspect_ai import Task
from inspect_ai._eval.task.log import plan_to_eval_plan
from inspect_ai._util.registry import create_registry_object
from inspect_ai.agent import Agent, as_solver, react
from inspect_ai.dataset import Sample
from inspect_ai.event import ModelEvent, ToolEvent
from inspect_ai.log import EvalSample, resolve_sample_attachments
from inspect_ai.model import ChatMessageUser, GenerateConfig
from inspect_ai.solver import Plan

from inspect_swarm import member, messages, swarm, swarm_tools
from inspect_swarm._channel.messages import (
    _Messages,  # pyright: ignore[reportPrivateUsage]
)
from inspect_swarm._registry import resolve

from .test_shared_memory_channel import (
    _comm,  # pyright: ignore[reportPrivateUsage]
    _eval,  # pyright: ignore[reportPrivateUsage]
    _identifier,  # pyright: ignore[reportPrivateUsage]
    _scripted,  # pyright: ignore[reportPrivateUsage]
)

DEFAULTS: dict[str, Any] = {
    "max_bytes": 8_000,
    "max_unread": 50,
    "rate": (20, 60.0),
    "dedupe": True,
    "max_wait": 120,
}


def _config(channel: object) -> dict[str, Any]:
    assert isinstance(channel, _Messages)
    return {
        "max_bytes": channel.storm.max_bytes,
        "max_unread": channel.max_unread,
        "rate": channel.storm.rate,
        "dedupe": channel.dedupe,
        "max_wait": channel.max_wait,
    }


@pytest.mark.parametrize("name", ["messages", "inspect_swarm/messages"])
def test_names_resolve_with_the_defaults(name: str) -> None:
    channel = resolve("swarm_channel", name)
    assert _config(channel) == DEFAULTS
    assert (channel.name, channel.prefix, channel.reach) == ("messages", "m", "loop")


def test_the_plan_step_logs_the_channel_and_replay_rebuilds_it() -> None:
    plan = Plan(
        [
            as_solver(
                swarm(
                    react(),
                    channels=["filesystem", messages(max_bytes=2_000, rate=(3, 10.0))],
                )
            )
        ]
    )
    [step] = plan_to_eval_plan(plan, GenerateConfig()).steps
    name = "inspect_swarm/messages"
    params: dict[str, Any] = {"max_bytes": 2_000, "rate": [3, 10.0]}
    assert step.params["channels"][1] == {
        "type": "swarm_channel",
        "name": name,
        "params": params,
    }
    # the tuple comes back from the log as a list
    rebuilt = create_registry_object("swarm_channel", name, params)
    assert _config(rebuilt) == {**DEFAULTS, "max_bytes": 2_000, "rate": (3, 10.0)}


def test_eval_set_arms_are_distinct() -> None:
    def arm(channels: Any) -> Agent:
        return swarm(react(tools=[swarm_tools()]), channels=channels)

    arms = [
        arm(["filesystem"]),
        arm(["filesystem", "messages"]),
        arm([messages(max_bytes=2_000)]),
        arm([messages(max_unread=5)]),
        arm([messages(rate=None)]),
        arm([messages(dedupe=False)]),
        arm([messages(max_wait=30)]),
    ]
    identifiers = [_identifier(a) for a in arms]
    assert len(set(identifiers)) == len(arms)


@pytest.mark.parametrize(
    "arguments",
    [
        {"max_bytes": 0},
        {"max_unread": 0},
        {"max_unread": 1.5},
        {"rate": (0, 60.0)},
        {"rate": (1, float("inf"))},
        {"max_wait": 0},
    ],
)
def test_each_bound_is_checked(arguments: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=r"^messages\(\): "):
        messages(**arguments)


@pytest.mark.parametrize("name", ["Worker", "w.1", "w" * 49, "all", "swarm"])
def test_member_names_the_channel_cannot_address_are_refused(name: str) -> None:
    with pytest.raises(ValueError, match=re.escape("member(..., name=...)")):
        swarm(member(react(), name=name), channels=["messages"])


def _results(sample: EvalSample, function: str) -> list[str]:
    return [
        str(e.result)
        for e in sample.events
        if isinstance(e, ToolEvent) and e.function == function
    ]


def _preambles(sample: EvalSample) -> list[str]:
    # the user messages of the members' model calls
    return [
        m.text
        for e in resolve_sample_attachments(sample).events
        if isinstance(e, ModelEvent)
        for m in e.input
        if isinstance(m, ChatMessageUser)
    ]


@pytest.mark.anyio
async def test_react_members_message_each_other(tmp_path: Path) -> None:
    # p waits for a message; w sends one that a small tool output limit would cut,
    # then waits until p has ended
    text = "x" * 500
    model = _scripted(
        {
            "p": [("swarm_wait", {}), ("read_messages", {})],
            "w": [
                ("list_members", {}),
                ("send_message", {"to": "p", "message": text}),
                ("swarm_wait", {}),
            ],
        }
    )
    log = await _eval(
        Task(
            dataset=[Sample(input="task")],
            solver=swarm(
                [
                    member(react(model=model, tools=[swarm_tools()]), name="p"),
                    member(react(model=model, tools=[swarm_tools()]), name="w"),
                ],
                channels=["messages"],
            ),
        ),
        tmp_path,
        max_tool_output=100,
    )
    assert log.samples is not None
    [sample] = log.samples
    assert _results(sample, "swarm_wait")[0] == (
        "1 unread message (from w). Call read_messages() to read them."
    )
    assert _results(sample, "read_messages") == [
        f'1 message.\n\n<peer_message id="m-1" from="w" to="p">\n{text}\n</peer_message>'
    ]
    assert _results(sample, "list_members") == ["p: running\nw (you): running"]
    assert [d["recipients"] for d in _comm(sample, "delivered")] == [["p"]]
    assert any("`send_message`" in text for text in _preambles(sample))


@pytest.mark.anyio
async def test_one_swarm_object_keeps_concurrent_samples_inboxes_apart(
    tmp_path: Path,
) -> None:
    model = _scripted(
        {
            "p": [("swarm_wait", {}), ("read_messages", {})],
            "w": [("send_message", {"to": "p", "message": "hi"}), ("swarm_wait", {})],
        }
    )
    log = await _eval(
        Task(
            dataset=[Sample(input="one"), Sample(input="two")],
            solver=swarm(
                [
                    member(react(model=model, tools=[swarm_tools()]), name="p"),
                    member(react(model=model, tools=[swarm_tools()]), name="w"),
                ],
                channels=["messages"],
            ),
        ),
        tmp_path,
        max_samples=2,
    )
    assert log.samples is not None and len(log.samples) == 2
    for sample in log.samples:
        assert [d["record"] for d in _comm(sample, "delivered")] == ["m-1"]
        assert [(r["read"], r["records"]) for r in _comm(sample, "read")] == [
            ("r-1", ["m-1"])
        ]


@pytest.mark.anyio
async def test_a_swarm_without_the_channel_offers_no_message_tools(
    tmp_path: Path,
) -> None:
    model = _scripted({"p": [("send_message", {"to": "w", "message": "hi"})], "w": []})
    log = await _eval(
        Task(
            dataset=[Sample(input="task")],
            solver=swarm(
                [
                    member(react(model=model, tools=[swarm_tools()]), name="p"),
                    member(react(model=model, tools=[swarm_tools()]), name="w"),
                ],
                channels=["filesystem"],
            ),
        ),
        tmp_path,
    )
    assert log.samples is not None
    [sample] = log.samples
    assert _comm(sample, "sent") == []
    assert not any("send_message" in text for text in _preambles(sample))
