import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, cast

from inspect_ai.tool import Tool, ToolDef, ToolError
from inspect_ai.util._span import current_agent_span_id, current_span_id
from pydantic import JsonValue

from .._bus._record import Draft, FrozenJson, Record, thaw_json
from .._bus._storm import StormControls
from .._controller._controller import MemberInfo
from .._evidence import comm_read
from .._fence import FENCE_TAGS, fence, fence_bytes, has_marker, utf8_bytes
from ._channel import (
    Channel,
    ChannelRun,
    ChannelState,
    NoticeLine,
    WaitNews,
    WaitSpec,
    swarm_channel,
)

CHANNEL = "shared_memory"
ROOT = "/shared_memory"

Command = Literal[
    "view", "create", "str_replace", "insert", "append", "delete", "rename"
]

_WRITES = ("create", "str_replace", "insert", "append", "delete", "rename")

# the path grammar: no quote, angle bracket, space or newline, so a path is safe in a
# fence attribute as it is
_COMPONENT = re.compile(r"[A-Za-z0-9._-]{1,64}")
_MAX_COMPONENTS = 8
_MAX_PATH_BYTES = 160
_PATH_RULE = f"paths are {ROOT} followed by 1 to {_MAX_COMPONENTS} components separated by /, each 1 to 64 of the characters A-Z a-z 0-9 . _ - and not . or .., with at most {_MAX_PATH_BYTES} bytes in all"
_TAGS = [f"<{tag}" for tag in FENCE_TAGS]
_MARKER_RULE = f"Files on the board cannot contain a {', '.join(_TAGS[:-1])} or {_TAGS[-1]} tag, opening or closing, in any letter case."

_FILE_ID = re.compile(r"f-[1-9][0-9]{0,13}")

# the addressing grammar of member names, which appear in fence attributes and notices
_MEMBER_NAME = re.compile(r"[a-z][a-z0-9_-]{0,47}")

# file and record ids, versions and counts are given at most this many bytes when the
# read budget is computed
_ID_BYTES = 16

# at most this many file ids in a status line or notice
_LISTED_IDS = 10

# a loop key that is no loop's agent span, for a member whose own loop has not called
# the tool
_NO_LOOP = "\0"

_INSTRUCTIONS = (
    f"Every member of the swarm, and their subagents, share one board of files, which you reach with the shared_memory tool under {ROOT}. "
    "Files are shown between <peer_file> tags whose writer attribute names the agent that last wrote them. "
    "Notices name changed files by ids such as f-3, which the view command accepts as its path. "
    "View a file in its current version before you overwrite it with create, insert into it, rename it or delete it. "
    "To wait for another member's work, call swarm_wait rather than polling."
)

_DESCRIPTION = (
    f"A board of files under {ROOT}, shared by every agent in the swarm and their subagents. "
    "Files are shown between <peer_file> tags whose writer attribute names the agent that last wrote them. "
    "View a file in its current version before you overwrite it with create, insert into it, rename it or delete it; "
    "str_replace needs no view, since it fails unless old_str occurs exactly once in the file as it is now. "
    "To add an entry to a shared log or list, use append, which needs no view: it adds append_text on a new line at the end of the file as it is when the write lands, and creates the file if it does not exist. "
    "Use insert only when the position matters. "
    "Write commands return a short acknowledgment, not the file; view a file to see it. "
    "Every result ends with the files other agents changed since you last viewed them, by ids such as f-3, which view accepts as its path."
)


@dataclass(frozen=True)
class _Limits:
    max_files: int
    max_file_bytes: int
    max_file_lines: int
    max_board_bytes: int


class _SharedMemory(Channel):
    name = CHANNEL
    prefix = "w"
    reach = "member"

    def __init__(
        self, limits: _Limits, rate: tuple[int, float] | None, max_wait: int
    ) -> None:
        self.limits = limits
        self.storm = StormControls(rate=rate)
        self.max_wait = max_wait

    def check(self, roster: Sequence[MemberInfo]) -> None:
        for info in roster:
            if not _MEMBER_NAME.fullmatch(info.name):
                raise ValueError(
                    f"shared_memory(): member {info.name!r} has a name the channel cannot use: names must start with a lowercase letter, followed by at most 47 lowercase letters, digits, _ or -. Give the member such a name with member(..., name=...)."
                )

    def open(self, run: ChannelRun) -> ChannelState | None:
        return _SharedMemoryState(self.limits, run)

    def instructions(self, member: MemberInfo) -> str | None:
        return _INSTRUCTIONS


@swarm_channel
def shared_memory(
    max_files: int = 100,
    max_file_bytes: int = 16_000,
    max_file_lines: int = 1_000,
    max_board_bytes: int = 400_000,
    rate: tuple[int, float] | list[int | float] | None = (60, 60.0),
    max_wait: int = 600,
) -> Channel:
    """A board of files that every member of the swarm, and their subagents, read and write.

    Each member gets a `shared_memory` tool with the commands and parameters of inspect_ai's `memory()` over files under `/shared_memory`. Every write goes through the swarm's bus as a record from the member that made it. A view shows a file inside a fence that names its last writer and version, and lists of changed files name them by ids such as `f-3`. A loop must view a file in its current version before it overwrites it with `create`, inserts into it, renames it or deletes it. `append` needs no view: it adds text on a new line at the end of a file, creating the file if it does not exist.

    The board lives for one run of the swarm. Its bounds keep every file viewable whole in one result.

    Each member, and each of its subagents, also gets `swarm_wait`, which blocks until a file it watches has a change it has not viewed, another member ends, every member is waiting, the swarm stops, or a timeout passes. A wait is wall-clock time inside a tool call: it counts towards the member's time limit, the swarm's time cap, and the sample's time and working limits. It makes no model call, so it adds no tokens, cost or steps.

    Args:
        max_files: The most files on the board, from 1 to 10,000.
        max_file_bytes: The most UTF-8 bytes in one file, from 1 to 262,144.
        max_file_lines: The most lines in one file, from 1 to 100,000.
        max_board_bytes: The most UTF-8 bytes in all files together, from `max_file_bytes` to 16,777,216.
        rate: The most writes one member may make, all its loops together, in a sliding window of seconds, as `(count, seconds)`; `None` for no limit.
        max_wait: The longest `swarm_wait` in seconds, from 1 to 3,600, and its default timeout.

    Returns:
        The channel.

    Raises:
        ValueError: If a bound is out of its range, or `rate` is not a positive count and a finite, positive window.
    """
    _check_bound("max_files", max_files, 1, 10_000)
    _check_bound("max_file_bytes", max_file_bytes, 1, 262_144)
    _check_bound("max_file_lines", max_file_lines, 1, 100_000)
    _check_bound("max_board_bytes", max_board_bytes, max_file_bytes, 16_777_216)
    _check_bound("max_wait", max_wait, 1, 3_600)
    return _SharedMemory(
        _Limits(max_files, max_file_bytes, max_file_lines, max_board_bytes),
        None if rate is None else _rate(rate),
        max_wait,
    )


def _check_bound(name: str, value: object, low: int, high: int) -> None:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not low <= value <= high
    ):
        raise ValueError(
            f"shared_memory(): {name} must be a whole number from {low:,} to {high:,}, got {value!r}."
        )


def _rate(rate: object) -> tuple[int, float]:
    # a tuple, or the list a log gives back
    if isinstance(rate, tuple | list) and len(cast(Sequence[object], rate)) == 2:
        count, window = cast(Sequence[object], rate)
        if (
            isinstance(count, int)
            and not isinstance(count, bool)
            and count >= 1
            and isinstance(window, int | float)
            and not isinstance(window, bool)
            and 0 < window < math.inf
        ):
            return (count, float(window))
    raise ValueError(
        f"shared_memory(): rate must be (count, seconds), a positive whole count and a finite, positive window in seconds, or None; got {rate!r}."
    )


# One file of the board. A deleted file stays as a tombstone, with no content and its
# path freed, so its id is never reused and its deletion is a version like any write.
@dataclass(frozen=True)
class _BoardFile:
    id: str
    # the current path; for a tombstone, the path it had
    path: str
    content: str | None
    version: int
    writer: str
    record: str
    # the agent span of the loop that wrote this version
    loop: str | None
    # the latest version written by another loop than `loop`, and its writer: what
    # `loop` may not have seen when its own last write was a blind append
    peer_version: int = 0
    peer_writer: str | None = None

    @property
    def deleted(self) -> bool:
        return self.content is None


# What a write does to one file: its new path (None once deleted) and old path (None
# for a new file), the version it was computed against (None for a new file) and the
# version and content it produces (None for a delete).
@dataclass(frozen=True)
class _Change:
    file: str
    path: str | None
    old_path: str | None
    base: int | None
    version: int
    content: str | None

    def entry(self, command: str) -> dict[str, JsonValue]:
        # as the record's op and the `delivered` evidence give it
        if command == "rename":
            return {
                "file": self.file,
                "old_path": self.old_path,
                "new_path": self.path,
                "base": self.base,
                "version": self.version,
            }
        entry: dict[str, JsonValue] = {
            "file": self.file,
            "path": self.path if self.path is not None else self.old_path,
            "base": self.base,
            "version": self.version,
        }
        if self.content is None:
            entry["deleted"] = True
        return entry


# A write's arguments, as the record's op carries them.
@dataclass(frozen=True)
class _Args:
    path: str | None = None
    old_str: str | None = None
    insert_line: int | None = None
    old_path: str | None = None
    new_path: str | None = None

    def op(self, command: str, changes: Sequence[_Change]) -> dict[str, JsonValue]:
        op: dict[str, JsonValue] = {"command": command}
        for name in ("path", "old_str", "insert_line", "old_path", "new_path"):
            value = getattr(self, name)
            if value is not None:
                op[name] = value
        op["files"] = [change.entry(command) for change in changes]
        return op

    @staticmethod
    def from_op(op: Mapping[str, FrozenJson]) -> "_Args":
        def text(name: str) -> str | None:
            value = op.get(name)
            if value is not None and not isinstance(value, str):
                raise ToolError(f"a shared_memory record's {name} must be text")
            return value

        line = op.get("insert_line")
        if line is not None and (not isinstance(line, int) or isinstance(line, bool)):
            raise ToolError("a shared_memory record's insert_line must be a number")
        return _Args(
            path=text("path"),
            old_str=text("old_str"),
            insert_line=line,
            old_path=text("old_path"),
            new_path=text("new_path"),
        )


# The board of one swarm run, kept on the run's bus.
class _SharedMemoryState:
    def __init__(self, limits: _Limits, run: ChannelRun) -> None:
        self._limits = limits
        self._bus = run.bus
        self._roster = [info.name for info in run.roster]
        # by id, tombstones included, in the order the files were created
        self._files: dict[str, _BoardFile] = {}
        # live paths
        self._paths: dict[str, str] = {}
        self._bytes = 0
        self._last_file = 0
        # loop agent span -> file id -> the version that loop has seen; 0 when it knows
        # the file only from its own blind append, which shows it none of the content
        self._seen: dict[str | None, dict[str, int]] = {}
        # member -> file id -> the version its last notice covered
        self._covered: dict[str, dict[str, int]] = {}
        # member -> the agent span of its own loop, once that loop has called the tool
        self._top: dict[str, str | None] = {}
        # the changes admission computed for the record about to be applied, and those
        # applied, by record id, for the writer's acknowledgment
        self._admitted: tuple[str, list[_Change]] | None = None
        self._applied: dict[str, list[_Change]] = {}
        self._tool: Tool | None = None
        self.read_budget = _read_budget(limits, self._roster)

    def tools(self) -> list[Tool]:
        if self._tool is None:
            self._tool = self._make_tool()
        return [self._tool]

    def validate(self, record: Record) -> None:
        op = record.op
        files = None if op is None else op.get("files")
        if (
            record.kind not in _WRITES
            or op is None
            or op.get("command") != record.kind
            or not isinstance(files, tuple)
        ):
            raise ToolError("a shared_memory record must carry its write's operation")
        _Args.from_op(op)

    def admit(self, record: Record) -> None:
        # the whole write again, with the tool's code, against the board as it is now
        assert record.op is not None
        files = thaw_json(record.op["files"])
        assert isinstance(files, list)
        new_id = next(
            (
                entry["file"]
                for entry in files
                if isinstance(entry, dict)
                and entry.get("base") is None
                and isinstance(entry.get("file"), str)
            ),
            None,
        )
        changes = self._plan(
            record.kind,
            _Args.from_op(record.op),
            record.payload,
            record.sender_span_id,
            cast(str | None, new_id),
        )
        computed: JsonValue = [change.entry(record.kind) for change in changes]
        given: JsonValue = files
        if record.kind in ("str_replace", "append"):
            # the edit applies to the file's current version: a str_replace if it still
            # finds its text, an append always
            computed, given = _without_versions(computed), _without_versions(given)
        if computed != given:
            raise ToolError(
                "the files this write was computed against changed before it was committed; view them and retry."
            )
        self._check_limits(changes)
        self._admitted = (record.id, changes)

    def apply(self, record: Record) -> tuple[list[str], Mapping[str, JsonValue] | None]:
        assert self._admitted is not None and self._admitted[0] == record.id
        changes = self._admitted[1]
        self._admitted = None
        loop = record.sender_span_id
        seen = self._seen.setdefault(loop, {})
        for change in changes:
            old = self._files.get(change.file)
            if old is not None and not old.deleted:
                del self._paths[old.path]
                self._bytes -= utf8_bytes(cast(str, old.content))
        for change in changes:
            path = change.path if change.path is not None else change.old_path
            assert path is not None
            old = self._files.get(change.file)
            peer_version, peer_writer = (
                (0, None) if old is None else _by_others(old, loop)
            )
            self._files[change.file] = _BoardFile(
                id=change.file,
                path=path,
                content=change.content,
                version=change.version,
                writer=record.sender,
                record=record.id,
                loop=loop,
                peer_version=peer_version,
                peer_writer=peer_writer,
            )
            if change.content is not None:
                self._paths[path] = change.file
                self._bytes += utf8_bytes(change.content)
            # a blind append acknowledges nothing it was not shown: the loop has seen
            # the new version only if it had seen the one the append extended, and
            # otherwise only knows the file, so its deletion is news to the loop
            if (
                record.kind != "append"
                or change.base is None
                or seen.get(change.file) == change.base
            ):
                seen[change.file] = change.version
            else:
                seen.setdefault(change.file, 0)
        self._applied[record.id] = changes
        written = [self._files[change.file] for change in changes]
        notify = [
            name
            for name in self._roster
            if self._bus.running(name)
            and any(
                f.loop != self._top_loop(name)
                and self._is_news(name, self._top_loop(name), f)
                for f in written
            )
        ]
        return notify, {
            "files": [change.entry(record.kind) for change in changes],
        }

    def notice_lines(self, member: str) -> list[NoticeLine]:
        # the files whose current versions are news to the member's own loop and no
        # earlier notice covered, which this notice then covers
        files = self._news(member, self._top_loop(member), own=True)
        if not files:
            return []
        labels = self._labels(member, self._top_loop(member), files, own=True)
        covered = self._covered.setdefault(member, {})
        for f in files:
            covered[f.id] = f.version
        writers = _writers(files, self._top_loop(member))
        return [
            NoticeLine(
                f"[Swarm notice] {_files_changed(len(files), 'shared memory ')} ({labels}; by {writers}). "
                f'View one with shared_memory(command="view", path="{files[0].id}"), or list them with path="{ROOT}".',
                {
                    "files": [
                        {"file": f.id, "version": f.version, "record": f.record}
                        for f in files
                    ]
                },
            )
        ]

    # waiting: what a swarm_wait of a loop watches, by D-1's `seen` alone; nothing here
    # marks anything

    def wait_check(self, spec: WaitSpec) -> None:
        if spec.path is not None and not _FILE_ID.fullmatch(spec.path):
            _path(spec.path)

    def wakes(self, spec: WaitSpec, record: Record) -> bool:
        # only the files the record changed, so the cost is per write, not per board
        assert record.op is not None
        files = cast(tuple[Mapping[str, FrozenJson], ...], record.op["files"])
        written = [self._files[cast(str, entry["file"])] for entry in files]
        return any(f.loop != spec.loop and self._reportable(spec, f) for f in written)

    def news(self, spec: WaitSpec) -> WaitNews | None:
        files = [f for f in self._files.values() if self._reportable(spec, f)]
        if not files:
            return None
        seen = self._seen.get(spec.loop, {})
        labels = [
            f"{f.id} deleted"
            if f.deleted
            else f"{f.id} new"
            if not seen.get(f.id)
            else f.id
            for f in files
        ]
        writers = _writers(files, spec.loop)
        line = _changed_where_you_waited(
            len(files), _id_list(labels, len(files)), writers, files[0].id
        )
        return WaitNews(
            lines=[line],
            files=[
                {"file": f.id, "version": f.version, "deleted": f.deleted}
                for f in files[:_LISTED_IDS]
            ],
        )

    def _reportable(self, spec: WaitSpec, f: _BoardFile) -> bool:
        # a version under the filter written by another loop and above what the waiting
        # loop has seen; a tombstone only when the loop saw an earlier version
        if not _watched(spec.path, f):
            return False
        seen = self._seen.get(spec.loop, {})
        if _by_others(f, spec.loop)[0] <= seen.get(f.id, 0):
            return False
        return not f.deleted or f.id in seen

    # the tool

    def _make_tool(self) -> Tool:
        async def execute(
            command: Command,
            path: str | None = None,
            file_text: str | None = None,
            old_str: str | None = None,
            new_str: str | None = None,
            insert_line: int | None = None,
            insert_text: str | None = None,
            view_range: list[int] | None = None,
            old_path: str | None = None,
            new_path: str | None = None,
            append_text: str | None = None,
        ) -> str:
            """Shared memory.

            Args:
                command: Command to execute (view, create, str_replace, insert, append, delete, rename).
                path: Required for `view`, `create`, `str_replace`, `insert`, `append` and `delete`. Path to a file or directory under /shared_memory, e.g. `/shared_memory/notes.md` or `/shared_memory/notes`. For `view`, also a file id such as `f-3`.
                file_text: Required for `create`: the content of the file. It replaces the file if it exists.
                old_str: Required for `str_replace`: the text in `path` to replace, which must occur there exactly once.
                new_str: Optional for `str_replace`: the new text (if not given, `old_str` is deleted).
                insert_line: Required for `insert`: `insert_text` is inserted AFTER line `insert_line` of `path`; 0 inserts at the start.
                insert_text: Required for `insert`: the text to insert.
                view_range: Optional for `view` of a file: the lines to show, e.g. [11, 12] shows lines 11 and 12. Indexing starts at 1. `[start_line, -1]` shows from `start_line` to the end of the file.
                old_path: Required for `rename`: the current path of a file or directory.
                new_path: Required for `rename`: the new path, which must not exist.
                append_text: Required for `append`: the text to add on a new line at the end of `path`, which is created if it does not exist.
            """
            return await self._call(
                command,
                path=path,
                file_text=file_text,
                old_str=old_str,
                new_str=new_str,
                insert_line=insert_line,
                insert_text=insert_text,
                append_text=append_text,
                view_range=view_range,
                old_path=old_path,
                new_path=new_path,
            )

        return ToolDef(
            execute,
            name="shared_memory",
            description=_DESCRIPTION,
            parallel=True,
            # the read budget bounds every view, so nothing truncates a fence
            max_output=0,
        ).as_tool()

    async def _call(
        self,
        command: str,
        *,
        path: str | None,
        file_text: str | None,
        old_str: str | None,
        new_str: str | None,
        insert_line: int | None,
        insert_text: str | None,
        append_text: str | None,
        view_range: list[int] | None,
        old_path: str | None,
        new_path: str | None,
    ) -> str:
        context = self._bus.member()
        if context is None:
            raise RuntimeError(
                "The shared_memory tool was called outside a member of its swarm."
            )
        member = context.name
        if not self._bus.running(member):
            raise ToolError(f"{member} has ended.")
        loop = current_agent_span_id()
        own = not self._bus.nested(context)
        if own:
            self._top[member] = loop
        if command == "view":
            result = self._view(member, context.span_id, loop, own, path, view_range)
        elif command in _WRITES:
            result = await self._write(
                command,
                loop,
                path=path,
                file_text=file_text,
                old_str=old_str,
                new_str=new_str,
                insert_line=insert_line,
                insert_text=insert_text,
                append_text=append_text,
                old_path=old_path,
                new_path=new_path,
            )
        else:
            raise ToolError(f"Unknown command: {command}")
        status = self._status_line(member, loop, own)
        return f"{result}\n{status}" if status else result

    async def _write(
        self,
        command: str,
        loop: str | None,
        *,
        path: str | None,
        file_text: str | None,
        old_str: str | None,
        new_str: str | None,
        insert_line: int | None,
        insert_text: str | None,
        append_text: str | None,
        old_path: str | None,
        new_path: str | None,
    ) -> str:
        if self._bus.stopping:
            raise ToolError(
                "the swarm is stopping; shared memory can no longer be written."
            )
        match command:
            case "create":
                args = _Args(path=_required(path, "path", command))
                payload = _required(file_text, "file_text", command, empty=True)
            case "str_replace":
                args = _Args(
                    path=_required(path, "path", command),
                    old_str=_required(old_str, "old_str", command),
                )
                payload = new_str or ""
            case "insert":
                if insert_line is None:
                    raise ToolError("insert_line is required for insert command")
                args = _Args(
                    path=_required(path, "path", command), insert_line=insert_line
                )
                payload = _required(insert_text, "insert_text", command, empty=True)
            case "append":
                args = _Args(path=_required(path, "path", command))
                payload = _required(append_text, "append_text", command)
            case "delete":
                args = _Args(path=_required(path, "path", command))
                payload = ""
            case _:
                args = _Args(
                    old_path=_required(old_path, "old_path", command),
                    new_path=_required(new_path, "new_path", command),
                )
                payload = ""
        changes = self._plan(command, args, payload, loop, None)
        delivery = await self._bus.deliver(
            Draft(
                CHANNEL,
                command,
                payload=payload,
                op=args.op(command, changes),
                # any write may end a wait whose filter it matches
                wake=True,
            )
        )
        applied = self._applied.pop(delivery.record.id)
        return _acknowledgment(command, args, applied)

    # computing a write

    def _plan(
        self,
        command: str,
        args: _Args,
        payload: str,
        loop: str | None,
        new_id: str | None,
    ) -> list[_Change]:
        # every check of a write against the board, for the tool and again at
        # admission; a new file's id is allocated here at the tool (new_id None), and
        # admission passes the one allocated then
        match command:
            case "create":
                return [
                    self._plan_create(_path(args.path), _content(payload), loop, new_id)
                ]
            case "str_replace":
                f = self._file_at(_path(args.path))
                content = cast(str, f.content)
                old = cast(str, args.old_str)
                occurrences = content.count(old)
                if occurrences == 0:
                    raise ToolError(
                        f"No replacement was performed, old_str `{old}` did not appear verbatim in {f.path}."
                    )
                if occurrences > 1:
                    lines = [
                        n + 1
                        for n, line in enumerate(content.split("\n"))
                        if old in line
                    ]
                    raise ToolError(
                        f"No replacement was performed. Multiple occurrences of old_str `{old}` in lines {lines}. Please ensure it is unique"
                    )
                return [_edit(f, _content(content.replace(old, payload)))]
            case "insert":
                f = self._file_at(_path(args.path))
                self._require_seen(loop, f, "inserting into it")
                lines = cast(str, f.content).split("\n")
                at = cast(int, args.insert_line)
                if at < 0 or at > len(lines):
                    raise ToolError(
                        f"Invalid `insert_line` parameter: {at}. It should be within the range of lines of the file: {[0, len(lines)]}"
                    )
                inserted = lines[:at] + payload.split("\n") + lines[at:]
                return [_edit(f, _content("\n".join(inserted)))]
            case "append":
                # computed from nothing the loop has seen, so it needs no view
                path = _path(args.path)
                f = self._live(path)
                if f is None:
                    return [self._plan_create(path, _content(payload), loop, new_id)]
                return [_edit(f, _content(_appended(cast(str, f.content), payload)))]
            case "delete":
                path = _path(args.path)
                if path == ROOT:
                    raise ToolError(
                        f"{ROOT} itself cannot be deleted; delete the files or directories under it."
                    )
                targets = self._targets(path)
                for f in targets:
                    self._require_seen(loop, f, "deleting it")
                return [
                    _Change(f.id, None, f.path, f.version, f.version + 1, None)
                    for f in targets
                ]
            case "rename":
                return self._plan_rename(
                    _path(args.old_path), _path(args.new_path), loop
                )
            case _:
                raise ToolError(f"Unknown command: {command}")

    def _plan_create(
        self, path: str, content: str, loop: str | None, new_id: str | None
    ) -> _Change:
        if path == ROOT:
            raise ToolError(
                f"{ROOT} is the root directory; a file needs a path under it."
            )
        existing = self._live(path)
        if existing is not None:
            self._require_seen(loop, existing, "replacing it", exists=True)
            return _edit(existing, content)
        self._check_free(path)
        if new_id is None:
            self._last_file += 1
            new_id = f"f-{self._last_file}"
        elif new_id in self._files:
            raise ToolError(f"{new_id} is already in use; retry the create.")
        return _Change(new_id, path, None, None, 1, content)

    def _plan_rename(self, old: str, new: str, loop: str | None) -> list[_Change]:
        if old == ROOT:
            raise ToolError(f"{ROOT} itself cannot be renamed.")
        targets = self._targets(old)
        if new == old or new == ROOT or self._live(new) or self._is_dir(new):
            raise ToolError(
                f"{new} already exists; delete it first or choose another path."
            )
        if new.startswith(old + "/"):
            raise ToolError(f"{old} cannot be renamed into itself.")
        self._check_ancestors(new)
        changes: list[_Change] = []
        for f in targets:
            moved = new + f.path[len(old) :]
            if not _is_path(moved):
                raise ToolError(
                    f"Renaming {old} to {new} would move {f.id} to a path that breaks the rule: {_PATH_RULE}."
                )
            changes.append(
                _Change(f.id, moved, f.path, f.version, f.version + 1, f.content)
            )
        for f in targets:
            self._require_seen(loop, f, "renaming it")
        return changes

    def _check_free(self, path: str) -> None:
        if self._is_dir(path):
            raise ToolError(
                f"{path} is a directory; a file cannot be created at a directory's path."
            )
        self._check_ancestors(path)

    def _check_ancestors(self, path: str) -> None:
        parts = path.split("/")
        for n in range(3, len(parts)):
            ancestor = "/".join(parts[:n])
            if ancestor in self._paths:
                raise ToolError(
                    f"{ancestor} is a file, so nothing can be created under it."
                )

    def _require_seen(
        self, loop: str | None, f: _BoardFile, action: str, exists: bool = False
    ) -> None:
        if self._seen.get(loop, {}).get(f.id) == f.version:
            return
        state = "exists" if exists else "is"
        raise ToolError(
            f"{f.id} {state} at version {f.version}, last written by {f.writer}, which you have not viewed; view it before {action}."
        )

    def _check_limits(self, changes: Sequence[_Change]) -> None:
        limits = self._limits
        files, size = len(self._paths), self._bytes
        for change in changes:
            old = self._files.get(change.file)
            if old is not None and old.content is not None:
                files -= 1
                size -= utf8_bytes(old.content)
            if change.content is None:
                continue
            files += 1
            file_bytes = utf8_bytes(change.content)
            size += file_bytes
            if file_bytes > limits.max_file_bytes:
                raise ToolError(
                    f"{change.file} would be {file_bytes:,} bytes; the limit is {limits.max_file_bytes}"
                )
            lines = change.content.count("\n") + 1
            if lines > limits.max_file_lines:
                raise ToolError(
                    f"{change.file} would have {lines:,} lines; the limit is {limits.max_file_lines}"
                )
        if files > limits.max_files:
            raise ToolError(
                f"the board has {len(self._paths)} files, the limit; delete one first"
            )
        if size > limits.max_board_bytes:
            raise ToolError(
                f"the board would hold {size:,} bytes; the limit is {limits.max_board_bytes}"
            )

    # the board's namespace

    def _live(self, path: str) -> _BoardFile | None:
        file = self._paths.get(path)
        return None if file is None else self._files[file]

    def _is_dir(self, path: str) -> bool:
        prefix = path + "/"
        return path == ROOT or any(p.startswith(prefix) for p in self._paths)

    def _under(self, path: str) -> list[_BoardFile]:
        prefix = path + "/"
        return [
            self._files[file]
            for p, file in sorted(self._paths.items())
            if path == ROOT or p.startswith(prefix)
        ]

    def _file_at(self, path: str) -> _BoardFile:
        f = self._live(path)
        if f is not None:
            return f
        if self._is_dir(path):
            raise ToolError(
                f"The path {path} is a directory and only the `view` command can be used on directories"
            )
        raise ToolError(f"The path {path} does not exist. Please provide a valid path.")

    def _targets(self, path: str) -> list[_BoardFile]:
        # a file, or every file under a directory
        f = self._live(path)
        if f is not None:
            return [f]
        files = self._under(path)
        if not files:
            raise ToolError(
                f"The path {path} does not exist. Please provide a valid path."
            )
        return files

    # reading

    def _view(
        self,
        member: str,
        span_id: str | None,
        loop: str | None,
        own: bool,
        path: str | None,
        view_range: list[int] | None,
    ) -> str:
        path = _required(path, "path", "view")
        if _FILE_ID.fullmatch(path):
            f = self._files.get(path)
            if f is None:
                raise ToolError(f"There is no file {path}.")
        else:
            path = _path(path)
            f = self._live(path)
            if f is None:
                if not self._is_dir(path):
                    raise ToolError(
                        f"The path {path} does not exist. Please provide a valid path."
                    )
                return self._list(member, span_id, loop, own, path)
        if f.deleted:
            self._seen.setdefault(loop, {})[f.id] = f.version
            self._read(member, span_id, loop, own, [_shown(f, None)], listing=False)
            return f"{f.id} was deleted by {f.writer}."
        content = f.content
        assert content is not None
        lines = content.split("\n")
        start, end = 1, len(lines)
        if view_range:
            start, end = _view_range(view_range, len(lines))
        shown = lines[start - 1 : end]
        attributes = [
            ("id", f.id),
            ("path", f.path),
            ("version", str(f.version)),
            ("writer", f.writer),
            ("record", f.record),
        ]
        line_range = f"{start}-{end}" if view_range else None
        if line_range is not None:
            attributes.append(("lines", line_range))
        self._seen.setdefault(loop, {})[f.id] = f.version
        self._read(member, span_id, loop, own, [_shown(f, line_range)], listing=False)
        return fence("peer_file", attributes, _numbered(shown, start))

    def _list(
        self,
        member: str,
        span_id: str | None,
        loop: str | None,
        own: bool,
        path: str,
    ) -> str:
        # every file under the directory, at any depth; a listing marks nothing seen
        files = self._under(path)
        self._read(
            member, span_id, loop, own, [_shown(f, None) for f in files], listing=True
        )
        if not files:
            return f"{ROOT} has no files."
        body = "\n".join(
            _listing_line(f.id, str(f.version), f.writer, f.path) for f in files
        )
        return fence("peer_listing", [("path", path)], body)

    def _read(
        self,
        member: str,
        span_id: str | None,
        loop: str | None,
        own: bool,
        files: list[JsonValue],
        listing: bool,
    ) -> None:
        comm_read(
            span_id,
            member,
            CHANNEL,
            self._bus.read_id(),
            loop,
            not own,
            current_span_id(),
            files,
            listing,
        )

    # news: what changed since a loop last viewed it

    def _top_loop(self, member: str) -> str | None:
        return self._top.get(member, _NO_LOOP)

    def _is_news(self, member: str, loop: str | None, f: _BoardFile) -> bool:
        # a version written by another loop, newer than any this loop has seen; a
        # tombstone only when the loop saw, or its member was notified of, the file
        # before
        seen = self._seen.get(loop, {})
        if _by_others(f, loop)[0] <= seen.get(f.id, 0):
            return False
        heard = f.id in seen or (
            loop == self._top_loop(member) and f.id in self._covered.get(member, {})
        )
        return not f.deleted or heard

    def _news(self, member: str, loop: str | None, own: bool) -> list[_BoardFile]:
        # for the member's own loop, without what a notice has already covered
        covered = self._covered.get(member, {}) if own else {}
        return [
            f
            for f in self._files.values()
            if self._is_news(member, loop, f)
            and covered.get(f.id, 0) < _by_others(f, loop)[0]
        ]

    def _labels(
        self, member: str, loop: str | None, files: Sequence[_BoardFile], own: bool
    ) -> str:
        seen = self._seen.get(loop, {})
        covered = self._covered.get(member, {}) if own else {}
        labels = [
            f"{f.id} deleted"
            if f.deleted
            else f"{f.id} new"
            if not seen.get(f.id) and f.id not in covered
            else f.id
            for f in files
        ]
        return _id_list(labels, len(files))

    def _status_line(self, member: str, loop: str | None, own: bool) -> str | None:
        files = self._news(member, loop, own)
        if not files:
            return None
        return _status(len(files), self._labels(member, loop, files, own))


def _by_others(f: _BoardFile, loop: str | None) -> tuple[int, str | None]:
    # the latest version of a file written by another loop than `loop`, and its writer
    if f.loop != loop:
        return f.version, f.writer
    return f.peer_version, f.peer_writer


def _writers(files: Sequence[_BoardFile], loop: str | None) -> str:
    return ", ".join(sorted({cast(str, _by_others(f, loop)[1]) for f in files}))


def _required(value: str | None, name: str, command: str, empty: bool = False) -> str:
    if value is None or (not value and not empty):
        raise ToolError(f"{name} is required for {command} command")
    return value


def _content(composed: str) -> str:
    # a file as a write composes it: line endings normalised as memory() does, in the
    # whole file since a carriage return at the payload's end joins the newline after
    # it; and refused with a fence marker, which str_replace can assemble from old and
    # new text
    content = composed.replace("\r\n", "\n").replace("\r", "\n")
    if has_marker(content):
        raise ToolError(_MARKER_RULE)
    return content


def _appended(content: str, text: str) -> str:
    # the text starts a new line, unless the file is empty or already ends with one
    if content and not content.endswith("\n"):
        content += "\n"
    return content + text


def _is_path(path: str) -> bool:
    # the grammar is ASCII, so it is checked before the path is encoded: a model's
    # path can hold a lone surrogate, which does not encode
    if not path.startswith(ROOT + "/"):
        return False
    parts = path[len(ROOT) + 1 :].split("/")
    return (
        len(parts) <= _MAX_COMPONENTS
        and all(
            _COMPONENT.fullmatch(part) and part not in (".", "..") for part in parts
        )
        and utf8_bytes(path) <= _MAX_PATH_BYTES
    )


def _path(path: str | None) -> str:
    # checked against the grammar and never normalised; the error does not repeat what
    # was typed
    assert path is not None
    if path != ROOT and not _is_path(path):
        raise ToolError(f"Invalid path: {_PATH_RULE}.")
    return path


def _edit(f: _BoardFile, content: str) -> _Change:
    return _Change(f.id, f.path, f.path, f.version, f.version + 1, content)


def _without_versions(files: JsonValue) -> JsonValue:
    if not isinstance(files, list):
        return files
    return [
        {k: v for k, v in entry.items() if k not in ("base", "version")}
        if isinstance(entry, dict)
        else entry
        for entry in files
    ]


def _view_range(view_range: list[int], count: int) -> tuple[int, int]:
    # memory()'s rules
    if len(view_range) != 2:
        raise ToolError("Invalid `view_range`. It should be a list of two integers.")
    start, end = view_range
    if start < 1 or start > count:
        raise ToolError(
            f"Invalid `view_range`: {view_range}. Its first element `{start}` should be within the range of lines of the file: {[1, count]}"
        )
    if end > count:
        end = -1
    if end != -1 and end < start:
        raise ToolError(
            f"Invalid `view_range`: {view_range}. Its second element `{end}` should be larger or equal than its first `{start}`"
        )
    return start, count if end == -1 else end


def _numbered(lines: Sequence[str], start: int) -> str:
    # memory()'s `cat -n` numbering
    return "\n".join(f"{n + start:6}\t{line}" for n, line in enumerate(lines))


def _listing_line(file: str, version: str, writer: str, path: str) -> str:
    return f"{file}  version {version}  {writer}  {path}"


def _shown(f: _BoardFile, lines: str | None) -> JsonValue:
    return {
        "file": f.id,
        "version": f.version,
        "record": f.record,
        "lines": lines,
        "deleted": f.deleted,
    }


def _files_changed(count: int, kind: str = "") -> str:
    if count == 1:
        return f"1 {kind}file changed since you last viewed it"
    return f"{count} {kind}files changed since you last viewed them"


def _id_list(labels: Sequence[str], count: int) -> str:
    listed = ", ".join(labels[:_LISTED_IDS])
    more = count - min(count, _LISTED_IDS)
    return f"{listed}, and {more} more" if more else listed


def _status(count: int, labels: str) -> str:
    return f"Shared memory: {_files_changed(count)} ({labels})."


def _watched(path: str | None, f: _BoardFile) -> bool:
    # whether a wait's filter covers a file: the whole board, the file's id, or its
    # current path (a tombstone's, the path it had) at or under the filter's
    if path is None:
        return True
    if _FILE_ID.fullmatch(path):
        return f.id == path
    return f.path == path or f.path.startswith(path + "/")


def _changed_where_you_waited(count: int, labels: str, writers: str, first: str) -> str:
    files = "1 shared memory file" if count == 1 else f"{count} shared memory files"
    return f'{files} changed where you waited ({labels}; by {writers}). View one with shared_memory(command="view", path="{first}").'


def _acknowledgment(command: str, args: _Args, changes: Sequence[_Change]) -> str:
    # names only the caller's own paths and the board's ids and versions, never content
    first = changes[0]
    match command:
        case "create" | "append" if first.base is None:
            return f"Created {first.path} ({first.file}, version 1)."
        case "create":
            return f"Replaced {first.path} ({first.file}, now version {first.version})."
        case "str_replace":
            return f"Edited {first.path} ({first.file}, now version {first.version})."
        case "append":
            return (
                f"Appended to {first.path} ({first.file}, now version {first.version})."
            )
        case "insert":
            return f"Inserted text after line {args.insert_line} of {first.path} ({first.file}, now version {first.version})."
        case "delete" if len(changes) == 1 and first.old_path == args.path:
            return f"Deleted {args.path} ({first.file})."
        case "delete":
            ids = ", ".join(change.file for change in changes)
            return f"Deleted {len(changes)} file{'' if len(changes) == 1 else 's'} under {args.path} ({ids})."
        case _ if len(changes) == 1 and first.old_path == args.old_path:
            return f"Renamed {args.old_path} to {args.new_path} ({first.file}, now version {first.version})."
        case _:
            return f"Renamed {args.old_path} to {args.new_path} ({len(changes)} file{'' if len(changes) == 1 else 's'})."


def _read_budget(limits: _Limits, roster: Sequence[str]) -> int:
    # the most UTF-8 bytes a view can return for this configuration and roster: the
    # larger of a whole file of the largest size and a listing of the most files, with
    # ids, versions and counts of up to 16 bytes, the longest path and member name
    writer = max(utf8_bytes(name) for name in roster)
    count = "9" * _ID_BYTES
    status = utf8_bytes(
        "\n"
        + _status(
            int(count),
            _id_list([f"{'x' * _ID_BYTES} deleted"] * (_LISTED_IDS + 1), int(count)),
        )
    )
    line_range = len(str(limits.max_file_lines)) * 2 + 1
    file_view = (
        fence_bytes(
            "peer_file",
            [
                ("id", _ID_BYTES),
                ("path", _MAX_PATH_BYTES),
                ("version", _ID_BYTES),
                ("writer", writer),
                ("record", _ID_BYTES),
                ("lines", line_range),
            ],
        )
        # each line numbered in six columns and a tab
        + limits.max_file_bytes
        + 7 * limits.max_file_lines
    )
    listing_line = utf8_bytes(
        _listing_line("x" * _ID_BYTES, count, "x" * writer, "x" * _MAX_PATH_BYTES)
    )
    listing = fence_bytes(
        "peer_listing", [("path", _MAX_PATH_BYTES)]
    ) + limits.max_files * (listing_line + 1)
    tombstone = utf8_bytes(f"{'x' * _ID_BYTES} was deleted by {'x' * writer}.")
    return max(file_view, listing, tombstone) + status
