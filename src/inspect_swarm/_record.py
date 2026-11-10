import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import is_dataclass
from datetime import date, datetime, timedelta, timezone
from functools import cache
from typing import Any, Literal, cast, get_args, get_type_hints
from uuid import uuid4

from inspect_ai._util.constants import get_deserializing_context
from inspect_ai._util.json import to_json_safe
from inspect_ai.event import Event, InfoEvent, SwarmEvent
from inspect_ai.log import EvalSample, transcript
from inspect_ai.log._condense import (
    ATTACHMENT_PROTOCOL,
    condense_sample,
    resolve_events_attachments,
)
from inspect_ai.model import ModelOutput
from inspect_ai.model._call_tools import MAX_TOOL_CALL_ARGUMENTS_DEPTH
from inspect_ai.solver import TaskState
from inspect_ai.util import Store, store
from pydantic import (
    BaseModel,
    ConfigDict,
    JsonValue,
    ValidationError,
    ValidatorFunctionWrapHandler,
    field_validator,
)

from ._result import ResultKind, Verdict

MemberState = Literal["pending", "running", "idle", "ending", "ended"]
"""A member's lifecycle: not started, running, idle, ending, or ended. Only persistent members are ever `idle` (answered and waiting for `SwarmControl.wake()`) or `ending` (their ending has begun, and the member has not yet ended)."""

LeftOut = Literal["output", "answer_key", "explanation"]
"""A member's value that the record leaves out, because the record cannot hold it: its output, its answer key, or its verdict's explanation."""

RECORD_TYPE = "result_record"
RESULT_ID_KEY = "inspect_swarm:result_id"
START_TYPE = "swarm_start"

# where the record was kept before it moved to the transcript; the reader refuses it
_LEGACY_RECORD_KEY = "inspect_swarm:result"


class MemberResult(BaseModel):
    """One member in the swarm's result record."""

    name: str
    """The member's roster name."""

    role: str | None
    """The member's role, if it has one."""

    choose_name: bool = False
    """Whether the member chose its own name, and was not told `name`. The name it chose is in its transcript, not here."""

    state: MemberState
    """The member's lifecycle state."""

    status: str | None
    """How the member ended, once it has; `None` before."""

    submitted: bool
    """Whether the member ended with a submission (status `submitted`)."""

    time: float | None
    """Swarm time (seconds from the swarm's start) when the member ended; `None` until then."""

    sandbox: str | None = None
    """The member's own sandbox environment, by name; `None` when it shares the sample's."""

    gate_checks: int = 0
    """How many of the member's own submissions were checked in its loop, as `react()` checks them with `attempts`: intermediate scores from the task's first scorer, made in the member's own agent span and not a subagent's. Set when the member ends."""

    gate_passes: int = 0
    """How many of those checks passed: a score of 1.0 under Inspect's default `value_to_float()`, which is `react()`'s default `score_value`. Passes assume that default converter, so a member with its own `score_value` can record passes that differ from its own gate. Set when the member ends."""

    first_pass_time: float | None = None
    """Swarm time of the member's first passing check; `None` without one. A pass assumes the default `value_to_float()` converter, as for `gate_passes`."""

    persistent: bool = False
    """Whether the member is persistent (`member(..., persistent=True)`)."""

    answer_times: list[float] = []
    """The swarm times of a persistent member's answers, in order. Empty for other members, whose answer is their submission at `time`."""

    output: ModelOutput | None = None
    """The member's output when it ended, however it ended, as Inspect's log keeps a sample's output; `None` until then."""

    answer_key: str | None = None
    """The answer key of the member's submission, from the result contract's `answer_key`; `None` without a submission, a key function or a comparable answer."""

    verdict: Verdict | None = None
    """The verifier's verdict on the member's submission; `None` without a submission or a verifier, before the verdict is taken, or when the swarm's stop left it unverified."""

    verdict_time: float | None = None
    """Swarm time when the verdict was taken; `None` without one."""

    left_out: list[LeftOut] = []
    """The member's values left out of the record, which are `None` here (for `explanation`, the verdict's explanation): text that begins with `attachment://` or `tc://`, which a log reads as a reference to an attachment, and an output nested too deep for a JSON log, with a cycle, that reuses the same lists or dicts so often that writing it would expand it past 10,000 copies, or that a log cannot write or read back, such as a dict key with more digits than Python prints. Empty when nothing was left out. An output that holds any other Python object, which only task code puts there, ends its member `errored` and fails the sample; the record is still written, with that output left out."""

    # Validating a `ModelOutput` fills an empty completion from its message, both when
    # the record is made and when it is read; the record keeps the completion the
    # member returned.
    @field_validator("output", mode="wrap")
    @classmethod
    def _keep_completion(
        cls, value: object, handler: ValidatorFunctionWrapHandler
    ) -> ModelOutput | None:
        if isinstance(value, ModelOutput):
            completion: object = value.completion
        elif isinstance(value, dict):
            completion = cast(dict[str, object], value).get("completion")
        else:
            completion = None
        output = cast(ModelOutput | None, handler(value))
        if output is not None and isinstance(completion, str):
            output.completion = completion
        return output

    end_origin: str | None = None
    """Who asked the member to end, when an end request decided how it ended: `operator` for `inspect ctl sample agent stop`, or the origin a caller gave `MemberHandle.end()`. `None` when no end request did, as for a submission, a member limit or the swarm's own stop."""


class FinalRecord(BaseModel):
    """The swarm's final answer in its result record."""

    mode: str | None
    """The mode that chose the answer: a mode of the controller's `final` chain (`verify`, `vote`, `first` or `last`), or `controller` (the controller's `SwarmControl.choose_answer()`). `None` when there is no answer."""

    member: str | None
    """The member whose output is the answer. `None` when there is no answer."""

    reason: str | None
    """Why there is no answer: `no_submissions`, or, when there were submissions, `none_verified` for a chain that ends with `verify` and `no_answer_keys` for one that ends with `vote`. Under the mode `controller`, the reason the controller gave for its choice. Otherwise `None`."""

    provisional: bool
    """`True` when the swarm ended without finalising its answer: a limit, error or cancellation ended it first, or the answer did not fit the sample's message limit."""

    answer: int | None = None
    """Which of a persistent member's answers is the swarm's answer, from 0. `None` for other members, and when there is no answer."""

    chain: list[str] | None = None
    """The final-answer chain's modes, by name, such as `["verify", "vote", "first"]`. `None` in records written before the chain existed."""

    votes: dict[str, int] | None = None
    """When the chain's `vote` ran: the number of members that submitted each answer key, as the members' keys are in the record (a key the record leaves out is not counted). `None` otherwise."""

    tie: bool = False
    """`vote` chose the answer from a tied plurality, by the earliest submission."""


class SwarmResultRecord(BaseModel):
    """What the swarm did in a sample: each member's result and the final answer, written to the transcript when the swarm ends and read with `swarm_result()`."""

    version: Literal[1] = 1
    """The record's format version."""

    members: list[MemberResult]
    """The members, in roster order."""

    final: FinalRecord
    """The final answer."""

    kind: ResultKind | None = None
    """The result contract's kind, from `swarm(result=...)`; `None` without a contract."""

    verifier: str | None = None
    """A label for the result contract's verifier, for audit: its `name`, else its qualified name; `None` without one."""

    final_verdict: Verdict | None = None
    """For an `artifact` task with a verifier: the verifier's verdict on the environment after the members have stopped, called with the final answer's completion, or `""` without one. `None` otherwise."""

    left_out: list[LeftOut] = []
    """The record's own values left out, as for a member's `left_out`: `explanation`, the final verdict's explanation. Empty when nothing was left out."""

    # serialized as Inspect serializes a sample (`EvalSample`), which keeps NaN and
    # infinities, the members' outputs included
    model_config = ConfigDict(ser_json_inf_nan="constants")


# Only the runtime writes the record: once, when the swarm ends, in the swarm's span,
# with its id in the store the swarm ran with, which ties the record to that store
def write_record(record: SwarmResultRecord) -> None:
    # first, so that a write that fails leaves no id rather than an earlier swarm's,
    # such as one nested in a member
    forget_record()
    id = uuid4().hex
    members = [_logged_member(record, m) for m in record.members]
    record = _logged_final(record.model_copy(update={"members": members}))
    data: dict[str, JsonValue] = {"id": id, **record.model_dump(mode="json")}
    logged = _as_logged(data)
    # the members' model text is held as a log gives it back; the rest is the task
    # author's, refused if a log would change it
    if any(
        _unresolved(written) or _authored(written) != _authored(data)
        for written in (logged, _as_logged(data, log_images=False))
    ):
        raise ValueError(
            "The swarm's result record cannot be written: a string in it would read back from the log as other text. Inspect reads a string that begins with attachment:// or tc:// as a reference to an attachment, removes base64 data: URIs when an eval does not log images, and escapes text that is not valid UTF-8, such as a lone surrogate. Check the members' names, roles and sandboxes, and the reason given to choose_answer()."
        )
    transcript()._event(  # pyright: ignore[reportPrivateUsage]
        SwarmEvent(kind=RECORD_TYPE, member=None, data=logged)
    )
    store().set(RESULT_ID_KEY, id)


# A member's values that come from its model: its output, its answer key, and its
# verdict's explanation (the verdict as a whole, since its other values are the task's).
_MODEL_TEXT = ("output", "answer_key", "verdict")


# The record's data without the model text: the members', the votes' answer keys and
# the final verdict.
def _authored(data: dict[str, JsonValue]) -> dict[str, JsonValue]:
    members = cast(list[dict[str, JsonValue]], data.get("members", []))
    final = cast(dict[str, JsonValue], data.get("final", {}))
    return {
        **data,
        "members": [
            {k: v for k, v in m.items() if k not in _MODEL_TEXT} for m in members
        ],
        "final": {k: v for k, v in final.items() if k != "votes"},
        "final_verdict": None,
    }


# The record, its members already as a log gives them back, with its own model text as
# a log gives it back: the votes counted from the members' keys as the record holds
# them, and the final verdict, whose explanation is left out, and named in the record's
# left_out, when the record could not hold it.
def _logged_final(record: SwarmResultRecord) -> SwarmResultRecord:
    update: dict[str, Any] = {}
    if record.final.votes is not None:
        votes: dict[str, int] = {}
        for m in record.members:
            if m.submitted and m.answer_key is not None:
                votes[m.answer_key] = votes.get(m.answer_key, 0) + 1
        update["final"] = record.final.model_copy(update={"votes": votes})
    verdict = record.final_verdict
    if verdict is not None:
        # the verdict's declared fields alone, as for a member's verdict
        logged = Verdict(verdict.passed, verdict.value)
        if verdict.explanation is not None:
            explained = _logged_verdict(
                record, Verdict(verdict.passed, verdict.value, verdict.explanation)
            )
            if explained is None:
                update["left_out"] = [*record.left_out, "explanation"]
            else:
                logged = explained
        update["final_verdict"] = logged
    return record.model_copy(update=update)


# The final verdict as the record gives it back from a log, or None when the log
# cannot write or read it, or its explanation reads as an attachment reference. It is
# written alone in a copy of the record, at its place in the record.
def _logged_verdict(record: SwarmResultRecord, verdict: Verdict) -> Verdict | None:
    try:
        alone = record.model_copy(
            update={
                "members": [],
                "final": record.final.model_copy(update={"votes": None}),
                "final_verdict": verdict,
            }
        )
        data = alone.model_dump(mode="json")
        if _unresolved(data["final_verdict"]):
            return None
        logged = _as_logged(data)
        if _unresolved(logged["final_verdict"]):
            return None
        return SwarmResultRecord.model_validate(
            logged, context=get_deserializing_context()
        ).final_verdict
    except ValueError:
        return None


# A member with its output, answer key and verdict explanation as a log gives them
# back. A value the record cannot hold is left out of its member and named in its
# left_out, so that one member's model output never costs the sample its record: one a
# log cannot write or read, one with text that reads as an attachment reference, and
# an output whose serialization is not bounded (`_bounded()`). An object the task's
# code put in an output fails the sample first (`_refuse_author_objects()`).
def _logged_member(record: SwarmResultRecord, m: MemberResult) -> MemberResult:
    if m.output is not None:
        _refuse_author_objects(m.output, m.name)
    verdict = m.verdict
    # the verdict's declared fields alone, built as a plain `Verdict`, so that a
    # subclass's own constructor never runs on the copy
    base = None if verdict is None else Verdict(verdict.passed, verdict.value)
    # the member with each value alone, so that each is kept or left out by itself
    alone = m.model_copy(update={"output": None, "answer_key": None, "verdict": base})
    values: list[tuple[LeftOut, dict[str, Any]]] = []
    if m.output is not None:
        values.append(("output", {"output": m.output}))
    if m.answer_key is not None:
        values.append(("answer_key", {"answer_key": m.answer_key}))
    if verdict is not None and verdict.explanation is not None:
        explained = Verdict(verdict.passed, verdict.value, verdict.explanation)
        values.append(("explanation", {"verdict": explained}))
    logged = alone
    left_out = list(m.left_out)
    for name, value in values:
        kept = _logged_value(record, alone.model_copy(update=value))
        if kept is None:
            left_out.append(name)
        else:
            field = "verdict" if name == "explanation" else name
            logged = logged.model_copy(update={field: getattr(kept, field)})
    return logged.model_copy(update={"left_out": left_out})


# `m` as the record gives it back from a log, or None when the log cannot write or read
# it, or it holds text that reads as an attachment reference, before the log resolves
# it (as the text of another attachment, when one matches) or after. `m` is written in
# a copy of the record, which serializes as a sample does, at its place in the record.
def _logged_value(record: SwarmResultRecord, m: MemberResult) -> MemberResult | None:
    try:
        if m.output is not None and not _bounded(m.output):
            return None
        data = record.model_copy(update={"members": [m]}).model_dump(mode="json")
        if _unresolved(_model_text(data)):
            return None
        logged = _as_logged(data)
        if _unresolved(_model_text(logged)):
            return None
        [member] = cast(list[dict[str, JsonValue]], logged["members"])
        return MemberResult.model_validate(member, context=get_deserializing_context())
    except ValueError:
        return None


# The model text of the one member in a record's data.
def _model_text(data: dict[str, JsonValue]) -> dict[str, JsonValue]:
    [member] = cast(list[dict[str, JsonValue]], data["members"])
    return {k: v for k, v in member.items() if k in _MODEL_TEXT}


# The record's data as a log gives it back: in a sample's event, written as Inspect
# writes a sample to its log (`condense_sample()`, then `to_json_safe()` with the
# sample's serialization settings, which keep NaN and infinities) and read as a log is
# read, with the event's attachments resolved, as `swarm_result()` resolves them.
def _as_logged(
    data: dict[str, JsonValue], log_images: bool = True
) -> dict[str, JsonValue]:
    event = SwarmEvent(kind=RECORD_TYPE, member=None, data=data)
    sample = EvalSample(id=0, epoch=1, input="", target="", events=[event])
    written = to_json_safe(condense_sample(sample, log_images), indent=None)
    read = EvalSample.model_validate(
        json.loads(written), context=get_deserializing_context()
    )
    [resolved] = resolve_events_attachments(read.events, read.attachments)
    assert isinstance(resolved, SwarmEvent)
    return resolved.data


# Raises `TypeError` naming the member and the path when an output holds a value a
# model cannot make. Inspect's own models are trusted where Inspect puts them: the
# output, its choices, messages, content, citations and tool calls. Their free-form
# fields (metadata, tool arguments, provider data) hold what a model returns, the data
# JSON and YAML parsers give; anything else there, one of Inspect's models included,
# was put there by the task's code, and is refused as the record's other values from
# the task's author are. So is any value elsewhere that is neither one of Inspect's
# models nor such data, such as a subclass defined by task code of one of Inspect's
# models, or of a list, dict or str. The whole output is checked, each container once
# in each position, without recursion, so that a refusal never depends on what else
# the output holds, shared data is checked once and a cycle ends; nothing is
# serialized, no iterator is advanced, and no method of a task's class runs.
def _refuse_author_objects(output: ModelOutput, member: str) -> None:
    seen: set[tuple[int, bool]] = set()
    # for each value reached: the index of its container's entry, and its step, an
    # attribute or index, or else the dict key it is reached by
    trail: list[tuple[int, str | None, object]] = []
    # the value, whether it is free-form data, and its index in `trail`
    stack: list[tuple[object, bool, int]] = [(output, False, -1)]

    def refuse(value: object, at: int) -> TypeError:
        steps: list[str] = []
        while at >= 0:
            at, step, key = trail[at]
            steps.append(_key_step(key) if step is None else step)
        steps.reverse()
        # a deeply nested path is shortened in the middle
        if len(steps) > 20:
            steps = [*steps[:10], f"...({len(steps) - 15} steps)...", *steps[-5:]]
        return TypeError(
            f"The swarm's result record cannot be written: the output of member {member!r} holds a value of type {_class_name(type(value))} at output{''.join(steps)}. A member's output must be built from Inspect's own classes, and its metadata and tool arguments must hold data a model can return: dicts, lists, strings, numbers, booleans and None, or the dates, times, bytes and sets YAML gives."
        )

    def push(
        child: object, data: bool, at: int, step: str | None, key: object = None
    ) -> None:
        trail.append((at, step, key))
        stack.append((child, data, len(trail) - 1))

    while stack:
        value, data, at = stack.pop()
        model = _inspect_model(value)
        if model and data:
            raise refuse(value, at)
        if not model and not _container(value):
            if not _data(value):
                raise refuse(value, at)
            continue
        if (id(value), data) in seen:
            continue
        seen.add((id(value), data))
        if model:
            free_form = _free_form_fields(type(value))
            for name, child in vars(value).items():
                push(child, name in free_form, at, f".{name}")
            for name, child in _extra(value).items():
                push(child, True, at, f".{name}")
        elif isinstance(value, dict):
            for key, child in cast(dict[object, object], value).items():
                if not _data(key):
                    raise refuse(key, at)
                push(child, data, at, None, key)
        else:
            for i, child in enumerate(cast(Iterable[object], value)):
                push(child, data, at, f"[{i}]")


# Whether serializing an output is bounded, measured without serializing it: nested no
# deeper than `_MAX_OUTPUT_DEPTH`, with no cycle, and with containers reached by more
# than one path (as YAML aliases in tool arguments give) adding at most
# `_MAX_SHARED_EXPANSION` containers, since serializing writes a container once for
# each path. Each container is measured once, by identity, so a shared one is never
# expanded; Inspect's `exceeds_max_depth()` measures depth the same way but does not
# bound the expansion. It runs after `_refuse_author_objects()`, so it meets only
# Inspect's models where Inspect puts them, and data.
def _bounded(output: ModelOutput) -> bool:
    # by container id: its height in levels and the containers serializing it writes
    measured: dict[int, tuple[int, int]] = {}
    in_progress: set[int] = set()

    # None when `value`, at `depth`, is not bounded
    def measure(value: object, depth: int) -> tuple[int, int] | None:
        children = _children(value)
        if children is None:
            return (0, 0)
        if depth > _MAX_OUTPUT_DEPTH or id(value) in in_progress:
            return None
        known = measured.get(id(value))
        if known is None:
            in_progress.add(id(value))
            height, written = 0, 1
            for child in children:
                child_measure = measure(child, depth + 1)
                if child_measure is None:
                    return None
                height = max(height, child_measure[0])
                written += child_measure[1]
            in_progress.remove(id(value))
            known = measured[id(value)] = (height + 1, written)
        return None if depth + known[0] - 1 > _MAX_OUTPUT_DEPTH else known

    root = measure(output, 1)
    return root is not None and root[1] - len(measured) <= _MAX_SHARED_EXPANSION


# A dict key's step in a path: a key too large to print, such as an integer YAML gives
# with more digits than Python prints, is named by its type.
def _key_step(key: object) -> str:
    try:
        return f"[{key!r}]"
    except ValueError:
        return f"[<{type(key).__name__} key>]"


# What JSON and YAML parsers give inside a container, which `to_json_safe()` writes as
# JSON; YAML also gives dates and times, bytes, sets, and keys that are not strings
# (decision: Ransom, 2026-11-03).
_DATA = (str, int, float, bool, type(None), bytes, date, datetime)


# One of the data classes, a `datetime` included only with no time zone or one of the
# fixed offsets YAML gives (`timezone`, with its own offset and name), so that
# writing or printing it never calls a time zone, offset or name from task code.
def _data(value: object) -> bool:
    if not _one_of(type(value), _DATA):
        return False
    if type(value) is not datetime:
        return True
    zone = value.tzinfo
    if zone is None:
        return True
    # `timezone`'s own methods, which return the offset and name it was made with
    return (
        type(zone) is timezone
        and type(zone.utcoffset(None)) is timedelta
        and type(zone.tzname(None)) is str
    )


def _container(value: object) -> bool:
    return _one_of(type(value), (dict, list, tuple, set))


# One of Inspect's own output models, the classes a `ModelOutput` is built from.
def _inspect_model(value: object) -> bool:
    return _one_of(type(value), _inspect_models())


# Whether `cls`, a value's actual class from `type()`, is one of `classes`. Compared by
# identity, so that a value is approved without calling isinstance(), reading any of
# its attributes or running any method of a class from task code (a `__class__`
# property, `__instancecheck__`, a metaclass's `__eq__` or `__hash__`), and a subclass
# is never approved.
def _one_of(cls: type, classes: tuple[type, ...]) -> bool:
    return any(cls is approved for approved in classes)


# A class's name, read from the class itself: a metaclass from task code may give
# `__name__` a property of its own.
def _class_name(cls: type) -> str:
    return cast(str, type.__dict__["__name__"].__get__(cls, type))


# The classes, all Inspect's, that a `ModelOutput` is built from: its own and those its
# fields' types name, found from the type hints.
@cache
def _inspect_models() -> tuple[type, ...]:
    found: list[type] = []
    pending: list[object] = [ModelOutput]
    while pending:
        hint = pending.pop()
        if not isinstance(hint, type):
            pending.extend(get_args(hint))
            continue
        cls: type[object] = hint
        if _model_class(cls) and cls.__module__.startswith("inspect_ai."):
            if cls not in found:
                found.append(cls)
                pending.extend(get_type_hints(cls).values())
    return tuple(found)


def _model_class(cls: type[object]) -> bool:
    return issubclass(cls, BaseModel) or is_dataclass(cls)


# A Pydantic model's extra fields, which are free-form data.
def _extra(value: object) -> dict[str, object]:
    extra = value.__pydantic_extra__ if isinstance(value, BaseModel) else None
    return dict(extra or {})


# The fields of one of Inspect's models typed as free-form data (`Any` or `JsonValue`):
# `metadata`, a tool call's `arguments`, content's `data` and providers' `internal`.
@cache
def _free_form_fields(cls: type) -> frozenset[str]:
    hints = get_type_hints(cls)
    return frozenset(name for name, hint in hints.items() if _free_form(hint))


def _free_form(hint: object) -> bool:
    return (
        hint is Any or hint is JsonValue or any(_free_form(a) for a in get_args(hint))
    )


# The values in `value` that serializing it writes, when it is a container.
def _children(value: object) -> Iterable[object] | None:
    if isinstance(value, dict):
        return cast(dict[object, object], value).values()
    if isinstance(value, list | tuple | set):
        return cast(Iterable[object], value)
    if _inspect_model(value):
        return [*vars(value).values(), *_extra(value).values()]
    return None


# An output's own nesting, which holds tool arguments as deep as Inspect accepts within
# the output's containers, well below the depth at which the record inside its sample
# no longer reads from a JSON log.
_MAX_OUTPUT_DEPTH = MAX_TOOL_CALL_ARGUMENTS_DEPTH + 20

# Far more containers than an output repeats, and far fewer than an aliased graph
# expands to.
_MAX_SHARED_EXPANSION = 10_000


# Removes the store's record id, so that a swarm that leaves no record never leaves an
# earlier swarm's as its own.
def forget_record() -> None:
    if RESULT_ID_KEY in store():
        store().delete(RESULT_ID_KEY)


# Only the runtime writes it: when a swarm starts, in the swarm's span, before any
# member can start, so a sample that ran a swarm says so however early it was cut off
def write_start() -> None:
    transcript()._event(  # pyright: ignore[reportPrivateUsage]
        SwarmEvent(kind=START_TYPE, member=None, data={"version": 1})
    )


# swarms running in each sample, by the id of the sample's transcript, which the
# running swarm's context keeps alive, so the id is not reused while counted
_running: dict[int, int] = {}


def _swarm_started() -> int:  # pyright: ignore[reportUnusedFunction]
    key = id(transcript())
    _running[key] = _running.get(key, 0) + 1
    return key


def _swarm_ended(key: int) -> None:  # pyright: ignore[reportUnusedFunction]
    if _running[key] == 1:
        del _running[key]
    else:
        _running[key] -= 1


def swarm_result(source: TaskState | EvalSample) -> SwarmResultRecord | None:
    """Read the swarm's result record.

    The swarm writes its record to the transcript once, when it ends, however it ends, and its id to the store it ran with. This returns the record the store of `source` names: the last swarm to end with that store, which is the outermost of nested swarms. A forked branch or a subtask has its own store, and so its own record.

    Args:
        source: The `TaskState` of the sample being run or scored, as a scorer has it in final scoring and when a log is re-scored, or as a solver has it after the swarm has returned; or a sample read from a log, with its events and attachments.

    Returns:
        The record, or `None` when no swarm has ended with this store and the sample holds no event of a swarm.

    Raises:
        RuntimeError: A swarm in the sample is still running, so the record of the swarm being scored does not exist yet; a scorer that the solve can call returns `None` while `state.completed` is `False`.
        ValueError: The store names a record the transcript or sample does not hold (as for a sample read without its events), the record holds a string that reads as an attachment reference (one the sample was read without, or text beginning with `attachment://` or `tc://`), it is one this inspect_swarm cannot read, such as a version other than 1, the sample was written by an inspect_swarm that kept the record in the store, or the sample holds a swarm's events but its store names no record (as for a sample recovered from a crashed eval, whose store was not kept).
    """
    if isinstance(source, EvalSample):
        return _sample_result(source)
    if _running.get(id(transcript()), 0):
        raise RuntimeError(
            "swarm_result() was called while a swarm in this sample is running; a swarm's result record is written when it ends. A scorer that the solve can call (react(attempts=...), basic_agent, the human agent's /score, live control scoring) returns None while state.completed is False."
        )
    return _transcript_result(source.store, _where(source.sample_id, source.epoch))


# The record the store names, from the current transcript.
def _transcript_result(
    store: Store, where: str = "the transcript"
) -> SwarmResultRecord | None:
    return _read(
        store,
        transcript().history.find_last,
        lambda event: event,
        where,
        missing="it is a state from another sample, or the record's event lost its id",
    )


def _sample_result(sample: EvalSample) -> SwarmResultRecord | None:
    def find_last(predicate: Callable[[Event], bool]) -> Event | None:
        return next((e for e in reversed(sample.events) if predicate(e)), None)

    # only the record's own event, so a sample read with or without its attachments
    # resolved gives the same record
    def resolve(event: Event) -> Event:
        [resolved] = resolve_events_attachments([event], sample.attachments)
        return resolved

    return _read(
        sample.store,
        find_last,
        resolve,
        _where(sample.id, sample.epoch),
        missing='it was read without its events (exclude_fields={"events"})',
    )


def _read(
    store: Store | Mapping[str, Any],
    find_last: Callable[[Callable[[Event], bool]], Event | None],
    resolve: Callable[[Event], Event],
    where: str,
    *,
    missing: str,
) -> SwarmResultRecord | None:
    record_id = store.get(RESULT_ID_KEY)
    if record_id is None:
        if _LEGACY_RECORD_KEY in store:
            raise ValueError(
                f"{where}: this sample was written by an inspect_swarm that kept the swarm's result record in the sample store under {_LEGACY_RECORD_KEY}; swarm_result() does not read it: read sample.store[{_LEGACY_RECORD_KEY!r}] by hand, or score the log with that inspect_swarm."
            )
        if find_last(_is_swarm_event) is not None:
            raise ValueError(
                f"{where}: this sample holds events of an inspect_swarm swarm, but its store names no result record: the store was not kept (a sample recovered from a crashed eval logs an empty store), or the swarm ran with another store (a forked branch or a subtask); swarm_result() cannot tell which record, if any, belongs to it."
            )
        return None

    # .get(), so that a record event without an id is skipped rather than raising
    def named(event: Event) -> bool:
        data = _record_data(event)
        return data is not None and data.get("id") == record_id

    event = find_last(named)
    if event is None:
        raise ValueError(
            f"{where}: the store names the swarm's result record {record_id}, but no such event is in the sample: {missing}."
        )
    resolved = _record_data(resolve(event))
    assert resolved is not None
    data = {k: v for k, v in resolved.items() if k not in ("type", "id")}
    if _unresolved(data):
        raise ValueError(
            f"{where}: the swarm's result record holds a string that reads as an attachment reference (attachment:// or tc://). Either the sample was read without its attachments, so read it, or the log being re-scored, with them (read_eval_log() without exclude_fields={{'attachments'}}), or the record's own text begins with such a prefix, which Inspect's attachment resolution cannot tell from a reference."
        )
    try:
        # as a log is read, so that a message keeps a missing id rather than gaining one
        return SwarmResultRecord.model_validate(
            data, context=get_deserializing_context()
        )
    except ValidationError as ex:
        raise ValueError(
            f"{where}: the swarm's result record is not one this inspect_swarm can read: {ex}"
        ) from ex


# The data of a result record event: a `SwarmEvent` of kind `result_record`, or the
# `InfoEvent` an inspect_swarm wrote before the swarm's evidence had its own event types.
def _record_data(event: Event) -> dict[str, JsonValue] | None:
    if isinstance(event, SwarmEvent):
        return event.data if event.kind == RECORD_TYPE else None
    if (
        isinstance(event, InfoEvent)
        and event.source == "inspect_swarm"
        and isinstance(event.data, dict)
        and event.data.get("type") == RECORD_TYPE
    ):
        return event.data
    return None


# Any event the runtime writes as a swarm runs, in either form.
def _is_swarm_event(event: Event) -> bool:
    return isinstance(event, SwarmEvent) or (
        isinstance(event, InfoEvent) and event.source == "inspect_swarm"
    )


# Inspect's attachment resolution reads a string with either prefix as a reference, and
# rewrites the older `tc://` as `attachment://`, so a record string with either one
# cannot read the same from a live transcript, a log and a re-scored sample
_REFERENCE_PREFIXES = (ATTACHMENT_PROTOCOL, "tc://")


# Whether any string in the record reads as an attachment reference.
def _unresolved(value: JsonValue) -> bool:
    if isinstance(value, str):
        return value.startswith(_REFERENCE_PREFIXES)
    if isinstance(value, dict):
        return any(_unresolved(v) for v in value.values())
    if isinstance(value, list):
        return any(_unresolved(v) for v in value)
    return False


def _where(sample_id: int | str, epoch: int) -> str:
    return f"sample {sample_id!r} (epoch {epoch})"
