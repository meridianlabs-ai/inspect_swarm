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

from inspect_swarm import (
    Controller,
    MemberInfo,
    StopReason,
    SwarmControl,
    Topic,
    member,
    swarm,
    swarm_controller,
    swarm_tools,
    topics,
)
from inspect_swarm._channel.topics import (
    _Topics,  # pyright: ignore[reportPrivateUsage]
)
from inspect_swarm._registry import resolve

from .swarm_runtime import delivery, send
from .test_shared_memory_channel import (
    _comm,  # pyright: ignore[reportPrivateUsage]
    _eval,  # pyright: ignore[reportPrivateUsage]
    _identifier,  # pyright: ignore[reportPrivateUsage]
    _scripted,  # pyright: ignore[reportPrivateUsage]
)


def _config(channel: object) -> dict[str, Any]:
    assert isinstance(channel, _Topics)
    return {
        "topics": [t.model_dump() for t in channel.topics],
        "max_bytes": channel.storm.max_bytes,
        "rate": channel.storm.rate,
        "max_wait": channel.max_wait,
    }


GENERAL = {
    "name": "general",
    "readers": "all",
    "writers": "all",
    "wake": False,
}


@pytest.mark.parametrize("name", ["topics", "inspect_swarm/topics"])
def test_names_resolve_with_one_room_for_everyone(name: str) -> None:
    channel = resolve("swarm_channel", name)
    assert _config(channel) == {
        "topics": [GENERAL],
        "max_bytes": 8_000,
        "rate": (20, 60.0),
        "max_wait": 120,
    }
    assert (channel.name, channel.prefix, channel.reach) == ("topics", "p", "loop")


def test_topics_and_mappings_of_their_fields_give_the_same_channel() -> None:
    declared = [
        Topic(name="red", readers=["a", "b"], writers=["a"]),
        Topic(name="news", wake=True),
    ]
    given = [
        {"name": "red", "readers": ["a", "b"], "writers": ["a"]},
        {"name": "news", "wake": True},
    ]
    assert _config(topics(declared)) == _config(topics(given))


def test_the_plan_step_logs_the_topics_and_replay_rebuilds_them() -> None:
    declared = [Topic(name="red", readers=["a", "b"])]
    channel = topics(declared, rate=(3, 10.0))
    members = [member(react(), name="a"), member(react(), name="b")]
    plan = Plan([as_solver(swarm(members, channels=[channel]))])
    [step] = plan_to_eval_plan(plan, GenerateConfig()).steps
    [logged] = step.params["channels"]
    assert (logged["type"], logged["name"]) == ("swarm_channel", "inspect_swarm/topics")
    rebuilt = create_registry_object("swarm_channel", logged["name"], logged["params"])
    assert _config(rebuilt) == _config(channel)


def test_eval_set_arms_that_differ_in_one_topic_are_distinct() -> None:
    def arm(*declared: Topic) -> Agent:
        return swarm(
            [member(react(tools=[swarm_tools()]), name=f"a-{n}") for n in (1, 2)],
            channels=[topics(list(declared))],
        )

    arms = [
        arm(Topic(name="room")),
        arm(Topic(name="room", wake=True)),
        arm(Topic(name="room", readers=["a-1"])),
        arm(Topic(name="room"), Topic(name="other")),
    ]
    assert len({_identifier(a) for a in arms}) == len(arms)


@pytest.mark.parametrize(
    "arguments,error",
    [
        pytest.param({"topics": []}, "give at least one topic", id="none"),
        pytest.param({"topics": "general"}, "topics must be a list", id="not-a-list"),
        pytest.param({"topics": [3]}, "must be a Topic or a mapping", id="not-a-topic"),
        pytest.param(
            {"topics": [{"name": "a", "colour": "red"}]},
            "topic 'a' is not valid: colour: Extra inputs are not permitted",
            id="unknown-field",
        ),
        pytest.param(
            {"topics": [{"name": "a", "private": True}]},
            "topic 'a' is not valid: private: Extra inputs are not permitted",
            id="no-private-field",
        ),
        pytest.param(
            {"topics": [Topic(name="Orders")]},
            "topic name 'Orders' must start",
            id="case",
        ),
        pytest.param(
            {"topics": [Topic(name="o" * 49)]}, "must start with a lowercase", id="long"
        ),
        pytest.param(
            {"topics": [Topic(name="c-red"), Topic(name="c-3")]},
            "topic name c-3 is reserved for swarm-assigned ids",
            id="reserved",
        ),
        pytest.param(
            {"topics": [{"name": "c-3"}]},
            "topic name c-3 is reserved for swarm-assigned ids",
            id="reserved-mapping",
        ),
        pytest.param(
            {"topics": [Topic(name="a"), {"name": "a"}]},
            "topic a is given more than once",
            id="repeated",
        ),
        pytest.param(
            {"topics": [Topic(name="a", readers=[])]},
            'topic a: readers must be "all" or a non-empty list',
            id="no-readers",
        ),
        pytest.param(
            {"topics": [Topic(name="a", writers=["x", "y", "x"])]},
            "topic a: writers names 'x' more than once",
            id="repeated-writer",
        ),
        pytest.param({"max_bytes": 0}, "max_bytes must be", id="max-bytes"),
        pytest.param({"max_wait": 0}, "max_wait must be", id="max-wait"),
        pytest.param({"rate": (0, 60.0)}, "rate must be", id="rate"),
    ],
)
def test_each_argument_is_checked(arguments: dict[str, Any], error: str) -> None:
    with pytest.raises(ValueError, match=r"^topics\(\): .*" + re.escape(error)):
        topics(**arguments)


@pytest.mark.parametrize(
    "names,declared,error",
    [
        pytest.param(
            ["Worker", "b"],
            [Topic(name="general")],
            "member(..., name=...)",
            id="member-name",
        ),
        pytest.param(
            ["a", "b"],
            [Topic(name="red", readers=["a", "c"])],
            "topic red lists 'c' in its readers, which is not a member of the swarm; the members are a, b.",
            id="unknown-member",
        ),
        pytest.param(
            ["a", "b"],
            [Topic(name="solo", readers=["a"], writers=["a"])],
            "topic solo has no writer with a reader other than itself",
            id="no-reader-but-the-writer",
        ),
    ],
)
def test_swarm_checks_the_roster_against_the_topics(
    names: list[str], declared: list[Topic], error: str
) -> None:
    with pytest.raises(ValueError, match=re.escape(error)):
        swarm(
            [member(react(), name=name) for name in names],
            channels=[topics(declared)],
        )


@pytest.mark.parametrize(
    "refused,error",
    [
        pytest.param(
            {"bridged": True},
            "swarm_bridged_tools() serves only the messages and shared_memory channels, not inspect_swarm/topics",
            id="bridged",
        ),
        pytest.param(
            {"choose_name": True},
            "inspect_swarm/topics is not supported with it",
            id="choose-name",
        ),
    ],
)
def test_swarm_refuses_a_bridged_or_naming_member_beside_topics(
    refused: dict[str, Any], error: str
) -> None:
    with pytest.raises(ValueError, match=re.escape(error)):
        swarm(
            [member(react(), name="a"), member(react(), name="b", **refused)],
            channels=["topics"],
        )


def test_instructions_name_the_members_own_topics_and_none_it_is_not_in() -> None:
    channel = topics(
        [
            Topic(
                name="orders-a",
                readers=["a"],
                writers=["lead"],
                wake=True,
            ),
            Topic(
                name="orders-b",
                readers=["b"],
                writers=["lead"],
                wake=True,
            ),
            Topic(name="reports", readers=["lead"], writers=["a", "b"]),
            Topic(name="general"),
            Topic(name="leads", readers=["lead"], writers=["lead", "a"]),
        ]
    )
    text = channel.instructions(MemberInfo("a", None))
    assert text is not None
    assert (
        "You read and post in #general; you read #orders-a; you post in #reports, #leads."
        in text
    )
    assert "A post in #orders-a ends `swarm_wait`" in text
    assert "orders-b" not in text and "lead," not in text
    outsider = topics([Topic(name="pair", readers=["a"], writers=["b"])]).instructions(
        MemberInfo("c", None)
    )
    assert outsider is not None
    assert "You are in no topic." in outsider and "pair" not in outsider


@swarm_controller
def pipeline_controller() -> Controller:
    # each stage starts once the one before it has ended
    async def run(control: SwarmControl) -> StopReason:
        for name in ("extract", "analyse"):
            control.start(name)
            async for _ in control.events():
                pass
        return "all_done"

    return Controller(run)


def _results(sample: EvalSample, function: str) -> list[str]:
    return [
        str(e.result)
        for e in sample.events
        if isinstance(e, ToolEvent) and e.function == function
    ]


def _preambles(sample: EvalSample) -> dict[str, str]:
    # each member's first user message
    found: dict[str, str] = {}
    for e in resolve_sample_attachments(sample).events:
        if isinstance(e, ModelEvent):
            for m in e.input:
                if isinstance(m, ChatMessageUser):
                    name = re.search(r"You are (\S+),", m.text)
                    if name:
                        found.setdefault(name.group(1), m.text)
    return found


@pytest.mark.anyio
async def test_a_pipeline_stage_finds_its_input_when_it_starts(tmp_path: Path) -> None:
    model = _scripted(
        {
            "extract": [
                ("list_topics", {}),
                ("post_topic", {"topic": "extract-to-analyse", "message": "rows"}),
            ],
            "analyse": [("read_topics", {})],
        }
    )
    stages = ["extract", "analyse"]
    log = await _eval(
        Task(
            dataset=[Sample(input="task")],
            solver=swarm(
                [
                    member(react(model=model, tools=[swarm_tools()]), name=name)
                    for name in stages
                ],
                controller=pipeline_controller(),
                channels=[
                    topics(
                        [
                            Topic(
                                name="extract-to-analyse",
                                readers=["analyse"],
                                writers=["extract"],
                                wake=True,
                            )
                        ]
                    )
                ],
            ),
        ),
        tmp_path,
        # below the read's result, which the limit does not cut
        max_tool_output=70,
    )
    assert log.samples is not None
    [sample] = log.samples
    assert _results(sample, "list_topics") == [
        "#extract-to-analyse: readers analyse; writers extract; you post."
    ]
    assert _results(sample, "post_topic") == [
        "Posted p-1 to #extract-to-analyse, read by analyse."
    ]
    assert _results(sample, "read_topics") == [
        '1 post.\n\n<topic_post id="p-1" topic="#extract-to-analyse" from="extract">\nrows\n</topic_post>'
    ]
    [sent] = _comm(sample, "sent")
    assert (send(sent).record.to, send(sent).record.recipients) == (
        ["#extract-to-analyse"],
        ["analyse"],
    )
    assert [delivery(d).recipients for d in _comm(sample, "delivered")] == [["analyse"]]
    preambles = _preambles(sample)
    assert "You post in #extract-to-analyse." in preambles["extract"]
    assert "You read #extract-to-analyse." in preambles["analyse"]
