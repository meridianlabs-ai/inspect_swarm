import json
from collections.abc import Callable, Iterator, Mapping
from typing import Any, Literal
from uuid import uuid4

from inspect_ai._util.json import to_json_safe
from inspect_ai.event import Event, InfoEvent, SwarmEvent
from inspect_ai.log import EvalSample, transcript
from inspect_ai.log._condense import (
    ATTACHMENT_PROTOCOL,
    condense_event,
    resolve_events_attachments,
)
from inspect_ai.solver import TaskState
from inspect_ai.util import Store, store
from pydantic import BaseModel, JsonValue, ValidationError

MemberState = Literal["pending", "running", "idle", "ending", "ended"]
"""A member's lifecycle: not started, running, idle, ending, or ended. Only persistent members are ever `idle` (answered and waiting for `SwarmControl.wake()`) or `ending` (their ending has begun, and the member has not yet ended)."""

RECORD_TYPE = "result_record"
RESULT_ID_KEY = "inspect_swarm:result_id"
START_TYPE = "swarm_start"

# where the record was kept before it moved to the transcript; the reader refuses it
_LEGACY_RECORD_KEY = "inspect_swarm:result"


class MemberReap(BaseModel):
    """What stopping a member's leftover processes found, under users isolation.

    When a member ends, the swarm kills every process in the sandbox that runs as the member's user, in up to five rounds, since a process can start another between rounds. Processes that have fully exited and wait for their parent to collect them (zombies) are not counted.
    """

    status: Literal["ok", "survivors", "timeout", "error"]
    """`ok`: no process of the member's user is left. `survivors`: processes were still running after five rounds. `timeout`: the swarm stopped waiting for the sandbox. `error`: the sandbox could not run the command. Any status but `ok` makes the record's `isolation_incomplete` `True`."""

    killed: int = 0
    """How many processes were killed: work the member left running after its tool calls returned."""

    killed_names: list[str] = []
    """The names of the first 50 processes killed (`/proc/<pid>/comm`, at most 15 characters), with every character other than printable ASCII, and every `:`, as `?`."""

    survivors: int = 0
    """How many processes were still running after five rounds."""

    survivor_names: list[str] = []
    """The names of the first 50 of them, as for `killed_names`."""

    message: str | None = None
    """For `timeout` and `error`, what went wrong."""


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

    user: str | None = None
    """Under users isolation, the member's Linux user; `None` otherwise."""

    uid: int | None = None
    """Under users isolation, the member's user id; `None` otherwise."""

    home: str | None = None
    """Under users isolation, the member's private directory; `None` otherwise."""

    reap: MemberReap | None = None
    """Under users isolation, what stopping the processes the member left running found, once the member has ended; `None` before, and without isolation."""

    foreign_execs: int | None = None
    """Under users isolation, how many commands the member ran in the sandbox as another user, once the member has ended; `None` before, and without isolation. These are `exec()` calls in the member's spans that named a user other than the member's, such as a custom tool's `exec(..., user="root")`, other than Inspect's own sandbox operations (tool injection and its launcher, in-loop scoring). It does not see calls a tool makes on the provider object (`sandbox().as_type(...)`) or file reads and writes. The count includes named-user commands across all sandbox environments in the sample, so it does not by itself identify grants in the users-isolated sandbox."""


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

    answer: int | None = None
    """Which of a persistent member's answers is the swarm's answer, from 0. `None` for other members, and when there is no answer."""


class SwarmResultRecord(BaseModel):
    """What the swarm did in a sample: each member's result and the final answer, written to the transcript when the swarm ends and read with `swarm_result()`."""

    version: Literal[1] = 1
    """The record's format version."""

    members: list[MemberResult]
    """The members, in roster order."""

    final: FinalRecord
    """The final answer."""

    isolation_incomplete: bool | None = None
    """Under users isolation, `True` when some member that started may have left processes running: its reaping found survivors, timed out or failed, or did not run. Analyses of what members left in the sandbox exclude or report such samples. `None` without isolation."""


# Only the runtime writes the record: once, when the swarm ends, in the swarm's span,
# with its id in the store the swarm ran with, which ties the record to that store
def write_record(record: SwarmResultRecord) -> None:
    # first, so that a write that fails leaves no id rather than an earlier swarm's,
    # such as one nested in a member
    forget_record()
    id = uuid4().hex
    event = SwarmEvent(
        kind=RECORD_TYPE, member=None, data={"id": id, **record.model_dump(mode="json")}
    )
    if _changed_by_logging(event):
        raise ValueError(
            "The swarm's result record cannot be written: a string in it would read back from the log as other text. Inspect reads a string that begins with attachment:// or tc:// as a reference to an attachment, removes base64 data: URIs when an eval does not log images, and escapes text that is not valid UTF-8, such as a lone surrogate. Check the members' names, roles and sandboxes, and the reason given to choose_answer()."
        )
    transcript()._event(event)  # pyright: ignore[reportPrivateUsage]
    store().set(RESULT_ID_KEY, id)


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
        return SwarmResultRecord.model_validate(data)
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
