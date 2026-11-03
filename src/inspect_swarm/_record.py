import json
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import replace
from typing import Any, Literal, cast
from uuid import uuid4

from inspect_ai._util.constants import get_deserializing_context
from inspect_ai._util.json import exceeds_max_depth, to_json_safe
from inspect_ai.event import Event, InfoEvent, SwarmEvent
from inspect_ai.log import EvalSample, transcript
from inspect_ai.log._condense import (
    ATTACHMENT_PROTOCOL,
    condense_event,
    resolve_events_attachments,
)
from inspect_ai.model import ModelOutput
from inspect_ai.model._call_tools import MAX_TOOL_CALL_ARGUMENTS_DEPTH
from inspect_ai.solver import TaskState
from inspect_ai.tool import ToolCall
from inspect_ai.util import Store, store
from pydantic import (
    BaseModel,
    JsonValue,
    ValidationError,
    ValidatorFunctionWrapHandler,
    field_validator,
)

from ._result import ResultKind, Verdict

MemberState = Literal["pending", "running", "ended"]
"""A member's lifecycle: not started, running, or ended."""

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

    output: ModelOutput | None = None
    """The member's output when it ended, however it ended, as Inspect's log keeps a sample's output; `None` until then."""

    answer_key: str | None = None
    """The answer key of the member's submission, from the result contract's `answer_key`; `None` without a submission, a key function or a comparable answer."""

    verdict: Verdict | None = None
    """The verifier's verdict on the member's submission; `None` without a submission or a verifier, before the verdict is taken, or when the swarm's stop left it unverified."""

    verdict_time: float | None = None
    """Swarm time when the verdict was taken; `None` without one."""

    left_out: list[LeftOut] = []
    """The member's values left out of the record, which are `None` here (for `explanation`, the verdict's explanation): text that begins with `attachment://` or `tc://`, which a log reads as a reference to an attachment, and an output nested too deep for a JSON log, with a cycle, that reuses the same lists, dicts or models so often that writing it would expand it past 10,000 copies, or that Inspect cannot write to a log. Empty when nothing was left out."""

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


class FinalRecord(BaseModel):
    """The swarm's final answer in its result record."""

    mode: str | None
    """The mode that chose the answer: `first` or `last` (the controller's `final` rule), or `controller` (the controller's `SwarmControl.choose_answer()`). `None` when there is no answer."""

    member: str | None
    """The member whose output is the answer. `None` when there is no answer."""

    reason: str | None
    """Why there is no answer: `no_submissions`. Under the mode `controller`, the reason the controller gave for its choice. Otherwise `None`."""

    provisional: bool
    """`True` when the swarm ended without finalising its answer: a limit, error or cancellation ended it first, or the answer did not fit the sample's message limit."""


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


# Only the runtime writes the record: once, when the swarm ends, in the swarm's span,
# with its id in the store the swarm ran with, which ties the record to that store
def write_record(record: SwarmResultRecord) -> None:
    # first, so that a write that fails leaves no id rather than an earlier swarm's,
    # such as one nested in a member
    forget_record()
    id = uuid4().hex
    logged = _logged_members(record)
    # the members' model text is held as a log gives it back; the rest is the task
    # author's, refused if a log would change it
    authored = logged.model_dump(mode="json", exclude=_MODEL_TEXT)
    if _changed_by_logging(SwarmEvent(kind=RECORD_TYPE, member=None, data=authored)):
        raise ValueError(
            "The swarm's result record cannot be written: a string in it would read back from the log as other text. Inspect reads a string that begins with attachment:// or tc:// as a reference to an attachment, removes base64 data: URIs when an eval does not log images, and escapes text that is not valid UTF-8, such as a lone surrogate. Check the members' names, roles and sandboxes, and the reason given to choose_answer()."
        )
    event = SwarmEvent(
        kind=RECORD_TYPE, member=None, data={"id": id, **logged.model_dump(mode="json")}
    )
    transcript()._event(event)  # pyright: ignore[reportPrivateUsage]
    store().set(RESULT_ID_KEY, id)


_MODEL_TEXT = {"members": {"__all__": {"output", "answer_key", "verdict"}}}


# The members' outputs, answer keys and verdict explanations, as a log gives them back.
# A value the record cannot hold is left out of its member and named in its left_out,
# so that one member's output never costs the sample its record.
def _logged_members(record: SwarmResultRecord) -> SwarmResultRecord:
    return record.model_copy(
        update={"members": [_logged_member(m) for m in record.members]}
    )


def _logged_member(m: MemberResult) -> MemberResult:
    update: dict[str, Any] = {}
    left_out = list(m.left_out)
    if m.output is not None:
        update["output"] = _logged_output(m.output)
        if update["output"] is None:
            left_out.append("output")
    if m.answer_key is not None:
        update["answer_key"] = _logged_text(m.answer_key)
        if update["answer_key"] is None:
            left_out.append("answer_key")
    if m.verdict is not None and m.verdict.explanation is not None:
        explanation = _logged_text(m.verdict.explanation)
        update["verdict"] = replace(m.verdict, explanation=explanation)
        if explanation is None:
            left_out.append("explanation")
    return m.model_copy(update={**update, "left_out": left_out})


# An output in the form Inspect's log writer gives a sample's output (`to_json_safe()`),
# read back as a log is read, or None when the record cannot hold it: one whose
# serialization is not bounded (`_bounded()`), one Inspect cannot serialize, or one
# with text that reads as an attachment reference.
def _logged_output(output: ModelOutput) -> ModelOutput | None:
    if not _bounded(output):
        return None
    try:
        data = json.loads(to_json_safe(output))
        if _unresolved(data):
            return None
        logged = ModelOutput.model_validate(data, context=get_deserializing_context())
    except ValueError:
        return None
    # validation fills an empty completion from the message; the record keeps it empty
    logged.completion = data["completion"]
    return logged


# Whether serializing an output is bounded, measured without serializing it: nested no
# deeper than `_MAX_OUTPUT_DEPTH`, with no cycle, and with containers reached by more
# than one path (as YAML aliases in tool arguments give) adding at most
# `_MAX_SHARED_EXPANSION` containers, since serializing writes a container once for
# each path. Inspect's `exceeds_max_depth()` measures the depth without expanding
# shared containers, but skips a tool call, a dataclass, so a tool call's arguments are
# measured at their own depth; it does not bound the expansion, which is counted here
# by identity.
def _bounded(output: ModelOutput) -> bool:
    if exceeds_max_depth(output, _MAX_OUTPUT_DEPTH):
        return False
    calls = [c for choice in output.choices for c in choice.message.tool_calls or []]
    if any(
        exceeds_max_depth(c.arguments, _MAX_OUTPUT_DEPTH - _ABOVE_TOOL_ARGUMENTS)
        for c in calls
    ):
        return False
    sizes: dict[int, int] = {}

    # the containers serializing `value` writes, each shared one counted once
    def written(value: object) -> int:
        children = _children(value)
        if children is None:
            return 0
        if id(value) not in sizes:
            sizes[id(value)] = 1 + sum(written(child) for child in children)
        return sizes[id(value)]

    return written(output) - len(sizes) <= _MAX_SHARED_EXPANSION


# The values `exceeds_max_depth()` descends into, and a tool call's arguments.
def _children(value: object) -> Iterable[object] | None:
    if isinstance(value, dict):
        return cast(dict[object, object], value).values()
    if isinstance(value, list | tuple | set | frozenset):
        return cast(Iterable[object], value)
    if isinstance(value, BaseModel):
        return [*value.__dict__.values(), *(value.__pydantic_extra__ or {}).values()]
    if isinstance(value, ToolCall):
        return [value.arguments]
    return None


# Text as the log writer gives it back (text that is not valid UTF-8 escaped), or None
# when it reads as an attachment reference.
def _logged_text(text: str) -> str | None:
    logged: str = json.loads(to_json_safe(text))
    return None if _unresolved(logged) else logged


# An output's own nesting, which holds tool arguments as deep as Inspect accepts within
# the output's containers, well below the depth at which the record inside its sample
# no longer reads from a JSON log.
_MAX_OUTPUT_DEPTH = MAX_TOOL_CALL_ARGUMENTS_DEPTH + 20

# The output, its choices, a choice, its message, its tool calls and a tool call.
_ABOVE_TOOL_ARGUMENTS = 6

# Far more containers than an output repeats, and far fewer than an aliased graph
# expands to.
_MAX_SHARED_EXPANSION = 10_000


# Removes the store's record id, so that a swarm that leaves no record never leaves an
# earlier swarm's as its own.
def forget_record() -> None:
    if RESULT_ID_KEY in store():
        store().delete(RESULT_ID_KEY)


# Whether a log would give back other text than `event` holds. Inspect condenses an
# event's strings when it logs it (long text into attachments, and base64 data: URIs
# removed under log_images=False), writes the event and its attachments as JSON (which
# escapes text that is not valid UTF-8, such as a lone surrogate), and resolves them
# when a log is read or re-scored,
# rewriting tc:// as attachment:// and replacing a reference with the attachment it
# names. Resolving against the event's own attachments only, where any other reference
# reads as something else, finds text that would read back as another attachment of
# the sample or not at all.
def _changed_by_logging(event: SwarmEvent) -> bool:
    for log_images in (True, False):
        attachments: dict[str, str] = {}
        condensed = condense_event(event, attachments, log_images)
        # written and read back as the log writes and reads them
        resolved: Event = SwarmEvent.model_validate_json(to_json_safe(condensed))
        own = _OwnAttachments(json.loads(to_json_safe(attachments)))
        # twice, as a sample read with its attachments resolved is resolved again by
        # swarm_result()
        for _ in range(2):
            [resolved] = resolve_events_attachments([resolved], own)
        assert isinstance(resolved, SwarmEvent)
        if resolved.data != event.data:
            return True
    return False


# An event's own attachments, which give any other hash a value no string holds.
class _OwnAttachments(Mapping[str, str]):
    def __init__(self, own: dict[str, str]) -> None:
        self._own = own

    def __getitem__(self, key: str) -> str:
        return self._own.get(key, "\0")

    def __iter__(self) -> Iterator[str]:
        return iter(self._own)

    def __len__(self) -> int:
        return len(self._own)


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
