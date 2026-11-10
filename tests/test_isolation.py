import subprocess
from collections.abc import Callable, Generator
from contextlib import aclosing, contextmanager
from pathlib import Path
from typing import Any, Literal, cast, overload

import anyio
import pytest
from inspect_ai import Task, eval  # pyright: ignore[reportUnknownVariableType]
from inspect_ai.agent import Agent, AgentState, agent, as_solver, run
from inspect_ai.dataset import Sample
from inspect_ai.log import transcript
from inspect_ai.log._transcript import Transcript
from inspect_ai.model import ChatMessageUser, ModelName
from inspect_ai.solver import TaskState, fork
from inspect_ai.solver._fork import set_task_generate
from inspect_ai.util import (
    ExecResult,
    SandboxEnvironment,
    sandbox,
    store,
    subtask,
    tool_state_instance,
)
from inspect_ai.util._sandbox._framework_operation import framework_sandbox_operation
from inspect_ai.util._sandbox._privileged import SHELL_PATH
from inspect_ai.util._sandbox.context import (
    sandbox_default_context_var,
    sandbox_environments_context_var,
    sandbox_with_environments_context_var,
)
from inspect_ai.util._sandbox.events import SandboxEnvironmentProxy
from inspect_ai.util._sandbox.identity import (
    sandbox_identity,
    sandbox_identity_cwd,
    sandbox_identity_user,
)

from inspect_swarm import (
    Boundary,
    Budget,
    MemberReap,
    Share,
    StopReason,
    SwarmControl,
    SwarmResultRecord,
    UserIsolation,
    filesystem,
    leaderless,
    member,
    swarm,
    users,
)
from inspect_swarm._budget import current_member
from inspect_swarm._isolation import _CENSUS as _CENSUS_SCRIPT
from inspect_swarm._record import _transcript_result

from .swarm_runtime import (
    controller,
    generator,
    run_swarm,
    sample,
    scripted_model,
    swarm_events,
)

# what tells the reaping and census scripts from setup's
_REAP_MARK = "proc_uid()"
_CENSUS_MARK = "-mindepth 1"

_ROOT_PROBE = "Uid:\t0\t0\t0\t0\nCapEff:\t00000000a80425fb\nsetgroups: allow\n"


class IsolationStub(SandboxEnvironment):
    """A sandbox environment whose commands succeed, with root that can switch users; it records each command's user and script."""

    def __init__(
        self,
        uid: str = "0",
        fail: ExecResult[str] | None = None,
        fail_on: str = "add_users()",
        probe: str = "begin\nend\n",
        stall_on: str | None = None,
        reap: str = "begin\nkilled 0\nleft 0\nend\n",
        census: str | None = None,
        raises: tuple[str, Exception] | None = None,
    ) -> None:
        super().__init__()
        # the group and users setup made
        self.group: int | None = None
        self.users: dict[str, int] = {}
        self.stall_on = stall_on
        self.stall_timeouts: list[int | None] = []
        self.calls: list[tuple[str | None, str, list[str]]] = []
        self.uid = uid
        self.fail = fail
        self.fail_on = fail_on
        self.probe = probe
        self.reap = reap
        self.census = census
        self.raises = raises
        # for each reaping, the uid it was for and the members whose result was written
        # before it ran
        self.reaped: list[tuple[str, set[str | None]]] = []
        # set when a reaping starts; a reaping waits for `hold_reaping` when it is set
        self.reaping: anyio.Event | None = None
        self.hold_reaping: anyio.Event | None = None
        # seconds a reaping takes, by uid
        self.reap_delay: dict[str, float] = {}

    async def exec(
        self,
        cmd: list[str],
        input: str | bytes | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        user: str | None = None,
        timeout: int | None = None,
        timeout_retry: bool = True,
        concurrency: bool = True,
    ) -> ExecResult[str]:
        # privileged_shell(): [sh, -c, script, sh, *args]
        assert cmd[0] == SHELL_PATH
        self.calls.append((user, cmd[2], cmd[4:]))
        if self.stall_on is not None and self.stall_on in cmd[2]:
            # a provider that does not answer, for longer than a test waits
            self.stall_timeouts.append(timeout)
            await anyio.sleep(5)
        if self.raises is not None and self.raises[0] in cmd[2]:
            raise self.raises[1]
        if _REAP_MARK in cmd[2]:
            ended = {e.member for e in swarm_events(transcript(), "member_result")}
            self.reaped.append((cmd[4], ended))
            if self.reaping is not None:
                self.reaping.set()
            if self.hold_reaping is not None:
                await self.hold_reaping.wait()
            await anyio.sleep(self.reap_delay.get(cmd[4], 0))
            if self.fail is not None and self.fail_on == _REAP_MARK:
                return self.fail
            return ExecResult(True, 0, self.reap, "")
        if _CENSUS_MARK in cmd[2]:
            # $1 the cap, $2 the member uids, then the roots
            output = self.census
            if output is None:
                output = "begin\n" + "root\n" * len(cmd[6:]) + "end\n"
            return ExecResult(True, 0, output, "")
        if "/proc/self/status" in cmd[2]:
            return ExecResult(
                True, 0, _ROOT_PROBE.replace("Uid:\t0", f"Uid:\t{self.uid}"), ""
            )
        if "echo begin" in cmd[2]:
            return ExecResult(True, 0, self.probe, "")
        if "reserve()" in cmd[2]:
            # the probes print the directories they reserve, in the member's home and in
            # each rw share, as mktemp names them; a failing one reserves only the first
            home, items = cmd[4], cmd[5:]
            rw = [
                path
                for access, path in zip(items[::2], items[1::2], strict=True)
                if access == "rw"
            ]
            dirs = [f"{d}/.swarm-probe-{str(user)[-1]}0000000\n" for d in [home, *rw]]
            if self.fail is not None and self.fail_on in cmd[2]:
                return ExecResult(
                    False, self.fail.returncode, dirs[0], self.fail.stderr
                )
            return ExecResult(True, 0, "".join(dirs) + "/etc\n", "")
        if self.fail is not None and self.fail_on in cmd[2]:
            return self.fail
        if "add_users()" in cmd[2]:
            # setup: $2 the gid, then the users from $5
            self.group, items = int(cmd[5]), cmd[8:]
            while items:
                if items[0] == "user":
                    self.users[items[1]] = int(items[2])
                    items = items[4:]
                else:
                    items = items[3:]
        return ExecResult(True, 0, "", "")

    async def write_file(
        self, file: str, contents: str | bytes, *, user: str | None = None
    ) -> None:
        raise NotImplementedError

    @overload
    async def read_file(
        self, file: str, text: Literal[True] = True, *, user: str | None = None
    ) -> str: ...

    @overload
    async def read_file(
        self, file: str, text: Literal[False], *, user: str | None = None
    ) -> bytes: ...

    async def read_file(
        self, file: str, text: bool = True, *, user: str | None = None
    ) -> str | bytes:
        raise NotImplementedError

    @classmethod
    async def sample_cleanup(
        cls,
        task_name: str,
        config: Any,
        environments: dict[str, SandboxEnvironment],
        interrupted: bool,
    ) -> None:
        pass


class UnhashableStub(IsolationStub):
    """A provider whose objects cannot be hashed, as with a dataclass that defines equality."""

    __hash__ = None  # pyright: ignore[reportAssignmentType]


class EqualStub(IsolationStub):
    """A provider whose distinct objects compare equal, as with a dataclass of equal fields."""

    def __eq__(self, other: object) -> bool:
        return isinstance(other, EqualStub)

    def __hash__(self) -> int:
        return 0


@contextmanager
def environments(
    **stubs: SandboxEnvironment,
) -> Generator[None, None, None]:
    """A sample's sandbox environments, the first its default."""
    tokens = (
        sandbox_environments_context_var.set(dict(stubs)),
        sandbox_with_environments_context_var.set({}),
        sandbox_default_context_var.set(next(iter(stubs))),
    )
    try:
        yield
    finally:
        sandbox_default_context_var.reset(tokens[2])
        sandbox_with_environments_context_var.reset(tokens[1])
        sandbox_environments_context_var.reset(tokens[0])


@agent
def scoped(seen: dict[str, dict[str, str | None]]) -> Agent:
    """Record the user and working directory the member's tools resolve, and its tool-state scope."""

    async def execute(state: AgentState) -> AgentState:
        context = current_member.get()
        assert context is not None
        seen[context.name] = {
            "user": sandbox_identity_user(sandbox(), None, strict=True),
            "cwd": sandbox_identity_cwd(sandbox(), None),
            "instance": tool_state_instance(None),
            "preamble": state.messages[-1].text,
        }
        return state

    return execute


def _scoped(seen: dict[str, dict[str, str | None]], count: int = 2) -> Any:
    return member(scoped, args={"seen": seen}, name="w", count=count)


@pytest.mark.anyio
@pytest.mark.parametrize("fails", [False, True])
async def test_the_self_test_removes_only_the_directories_it_reserved(
    fails: bool,
) -> None:
    seen: dict[str, dict[str, str | None]] = {}
    stub = IsolationStub(
        fail=ExecResult(False, 3, "", "the member cannot read /shared\n")
        if fails
        else None,
        fail_on="reserve()",
    )
    with environments(default=stub):
        if fails:
            with pytest.raises(RuntimeError, match="the member cannot read"):
                await run_swarm(swarm(_scoped(seen), channels=[], isolation="users"))
        else:
            await run_swarm(swarm(_scoped(seen), channels=[], isolation="users"))
    # setup's calls: the reaping and census come after
    setup = [
        c for c in stub.calls if _REAP_MARK not in c[1] and _CENSUS_MARK not in c[1]
    ]
    user, script, removed = setup[-1]
    assert user == "root" and script.endswith('\nrm -rf -- "$@"')
    if fails:
        # each failed after reserving the directory in its home
        assert sorted(removed) == [
            "/scratch/w-1/.swarm-probe-10000000",
            "/scratch/w-2/.swarm-probe-20000000",
        ]
    else:
        # the pair check uses the first member's probes; the line that names no
        # reservation is not removed
        assert setup[-2] == (
            "swarm-w-2",
            setup[-2][1],
            [
                "/scratch/w-1",
                "/scratch/w-1/.swarm-probe-10000000/probe",
                "none",
                "/shared/.swarm-probe-10000000/probe",
            ],
        )
        assert sorted(removed) == [
            "/scratch/w-1/.swarm-probe-10000000",
            "/scratch/w-2/.swarm-probe-20000000",
            "/shared/.swarm-probe-10000000",
            "/shared/.swarm-probe-20000000",
        ]


@pytest.mark.anyio
@pytest.mark.parametrize("fails", [False, True])
async def test_a_stalled_probe_cleanup_does_not_hold_up_cancellation(
    fails: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("inspect_swarm._isolation._CLEANUP_BOUND", 0.2)
    seen: dict[str, dict[str, str | None]] = {}
    stub = IsolationStub(
        fail=ExecResult(False, 3, "", "the member cannot read /shared\n")
        if fails
        else None,
        fail_on="reserve()",
        stall_on='rm -rf -- "$@"',
    )
    start = anyio.current_time()
    with environments(default=stub):
        # cancelled while the cleanup waits on the provider
        with anyio.move_on_after(0.1):
            if fails:
                with pytest.raises(RuntimeError, match="the member cannot read"):
                    await run_swarm(
                        swarm(_scoped(seen), channels=[], isolation="users")
                    )
            else:
                await run_swarm(swarm(_scoped(seen), channels=[], isolation="users"))
    assert anyio.current_time() - start < 2
    assert stub.stall_timeouts == [30]


@pytest.mark.anyio
async def test_each_member_runs_as_its_user_after_setup_and_self_test() -> None:
    seen: dict[str, dict[str, str | None]] = {}
    stub = IsolationStub()
    with environments(default=stub):
        await run_swarm(swarm(_scoped(seen), channels=[], isolation="users"))
    assert {name: (s["user"], s["cwd"], s["instance"]) for name, s in seen.items()} == {
        "w-1": ("swarm-w-1", "/scratch/w-1", "@w-1"),
        "w-2": ("swarm-w-2", "/scratch/w-2", "@w-2"),
    }
    # setup runs as root; the self-test and then the probes run as each member, and a
    # peer test as the second; the probes are removed as root
    users = [str(user) for user, _, _ in stub.calls]
    assert users[:3] == ["root", "root", "root"]
    assert sorted(users[3:5]) == sorted(users[5:7]) == ["swarm-w-1", "swarm-w-2"]
    # then each member is reaped as root when it ends, and the census runs as root
    assert users[7:] == ["swarm-w-2", "root", "root", "root", "root"]
    assert sorted(uid for uid, _ in stub.reaped) == ["61001", "61002"]
    assert _CENSUS_MARK in stub.calls[-1][1]
    # every path and name reaches the scripts as an argument
    _, setup, args = stub.calls[2]
    assert "swarm-w-1" not in setup and "/scratch" not in setup
    assert args == [
        "inspect-swarm",
        "61000",
        "none",
        "/scratch",
        *("user", "swarm-w-1", "61001", "/scratch/w-1"),
        *("user", "swarm-w-2", "61002", "/scratch/w-2"),
        *("share", "rw", "/shared"),
    ]


@pytest.mark.anyio
async def test_members_of_a_swarm_without_isolation_run_as_the_default_user() -> None:
    seen: dict[str, dict[str, str | None]] = {}
    stub = IsolationStub()
    with environments(default=stub):
        outcome = await run_swarm(swarm(_scoped(seen), channels=[]))
    assert {s["user"] for s in seen.values()} == {None}
    assert stub.calls == []
    # the record and metrics hold no isolation evidence
    assert outcome.record.isolation_incomplete is None
    for result in outcome.record.members:
        assert (result.user, result.uid, result.home) == (None, None, None)
        assert (result.reap, result.foreign_execs) == (None, None)
    [metrics] = swarm_events(outcome.transcript, "metrics")
    assert metrics.data["reap_complete"] is None
    assert swarm_events(outcome.transcript, "isolation") == []
    assert swarm_events(outcome.transcript, "census") == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "isolation,stub,error,message",
    [
        pytest.param(
            users(sandbox="workspace"),
            lambda: IsolationStub(),
            ValueError,
            "users(sandbox='workspace'): this sample has no such sandbox",
            id="unknown-sandbox",
        ),
        pytest.param(
            users(),
            # what a provider that keeps only the last bytes of a long output returns
            lambda: IsolationStub(probe="id 61003\nid 61004\nend\n"),
            RuntimeError,
            "users isolation: the id scan's output in sandbox 'default' came back incomplete",
            id="truncated-probe",
        ),
        pytest.param(
            users(),
            lambda: IsolationStub(probe="begin\ngroupname 1000\nend\n"),
            RuntimeError,
            "users isolation: the group inspect-swarm already exists in sandbox 'default'. Users isolation creates it, so the image must not have it.",
            id="existing-group",
        ),
        pytest.param(
            users(),
            lambda: IsolationStub(probe="begin\nusername swarm-w-2\nend\n"),
            RuntimeError,
            "users isolation: the user swarm-w-2 already exists in sandbox 'default'. Users isolation creates its members' users, so the image must not have users named swarm-<member>.",
            id="existing-user",
        ),
        pytest.param(
            users(),
            lambda: IsolationStub(probe="begin\nmembergroup swarm-w-1\nend\n"),
            RuntimeError,
            "users isolation: the group swarm-w-1 already exists in sandbox 'default'. Users isolation creates each member's own group, so the image must not have groups named swarm-<member>.",
            id="existing-member-group",
        ),
        pytest.param(
            users(),
            lambda: IsolationStub(uid="1000"),
            RuntimeError,
            "users isolation needs root in sandbox 'default' to create users; its root access is unusable (commands run as uid 1000, not as root)",
            id="no-root",
        ),
        pytest.param(
            users(),
            lambda: IsolationStub(
                fail=ExecResult(
                    False,
                    3,
                    "",
                    "chmod: noise\n/scratch is mode 1777; users isolation needs every directory above a member's home to be root-owned and not group- or world-writable\n",
                )
            ),
            RuntimeError,
            "users isolation could not set up sandbox 'default': /scratch is mode 1777; users isolation needs every directory above a member's home to be root-owned and not group- or world-writable",
            id="script-refusal",
        ),
    ],
)
async def test_setup_refusals(
    isolation: UserIsolation,
    stub: Callable[[], IsolationStub],
    error: type[Exception],
    message: str,
) -> None:
    seen: dict[str, dict[str, str | None]] = {}
    with environments(default=stub()), pytest.raises(error) as raised:
        await run_swarm(swarm(_scoped(seen), channels=[], isolation=isolation))
    assert str(raised.value).startswith(message)
    assert seen == {}


def test_the_local_sandbox_is_refused(tmp_path: Path) -> None:
    seen: dict[str, dict[str, str | None]] = {}
    [log] = eval(
        Task(
            dataset=[Sample(input="x")],
            solver=swarm(_scoped(seen), channels=[], isolation="users"),
            sandbox="local",
        ),
        model="mockllm/model",
        log_dir=str(tmp_path),
        display="none",
    )
    assert log.status == "error"
    assert log.error is not None
    assert (
        "users isolation needs a sandbox that can run commands as other users; 'local' runs everything as the host user."
        in log.error.message
    )
    assert seen == {}


@pytest.mark.anyio
@pytest.mark.parametrize(
    "isolation,text",
    [
        pytest.param(
            users(
                shared=[
                    Share(path="/shared", access="rw"),
                    {"path": "/data", "access": "ro"},
                ]
            ),
            "You run as the Linux user swarm-w-1, without root. Your private directory is /scratch/w-1 (your $HOME); other members cannot read it. Shared with all members: /shared (read and write), /data (read only). Use $TMPDIR for temporary files. Git refuses a repository another user created until you run git config --global --add safe.directory <path>. Processes you leave running are stopped when you finish.",
            id="none",
        ),
        pytest.param(
            users(
                home="/home/{member}",
                shared=[Share(path="/data", access="ro")],
                peers="read",
            ),
            "You run as the Linux user swarm-w-1, without root. Your private directory is /home/w-1 (your $HOME); other members can read it but not change it. Shared with all members: /data (read only). Use $TMPDIR for temporary files. Processes you leave running are stopped when you finish.",
            id="read",
        ),
        pytest.param(
            users(shared=()),
            "You run as the Linux user swarm-w-1, without root. Your private directory is /scratch/w-1 (your $HOME); other members cannot read it. Use $TMPDIR for temporary files. Processes you leave running are stopped when you finish.",
            id="no-shares",
        ),
    ],
)
async def test_the_preamble_ends_with_the_members_user_and_directories(
    isolation: UserIsolation, text: str
) -> None:
    seen: dict[str, dict[str, str | None]] = {}
    with environments(default=IsolationStub()):
        await run_swarm(swarm(_scoped(seen), channels=[], isolation=isolation))
    preamble = seen["w-1"]["preamble"]
    assert preamble is not None
    assert preamble.split("\n\n")[-1] == text


@pytest.mark.parametrize(
    "kwargs,message",
    [
        pytest.param(
            {"home": "scratch/{member}"},
            "users(): home must be an absolute, normalised path",
            id="relative-home",
        ),
        pytest.param(
            {"home": "/scratch/../{member}"},
            "users(): home must be an absolute, normalised path",
            id="dotdot",
        ),
        pytest.param(
            {"home": "/scratch/{member}/"},
            "users(): home must be an absolute, normalised path",
            id="trailing-slash",
        ),
        pytest.param(
            {"home": "/scr\natch/{member}"},
            "users(): home must be an absolute, normalised path",
            id="newline",
        ),
        pytest.param(
            {"shared": [Share(path="/sh\0ared", access="rw")]},
            "users(): shared must be an absolute, normalised path",
            id="nul",
        ),
        pytest.param(
            {"shared": [Share(path="/shared\rdata", access="rw")]},
            "users(): shared must be an absolute, normalised path",
            id="carriage-return",
        ),
        pytest.param(
            {"home": "/scratch\u2028x/{member}"},
            "users(): home must be an absolute, normalised path",
            id="line-separator",
        ),
        pytest.param(
            {"home": "/scr\\tatch/{member}"},
            "users(): home must be an absolute, normalised path",
            id="backslash-home",
        ),
        pytest.param(
            {"shared": [Share(path="/data\\t", access="ro")]},
            "users(): shared must be an absolute, normalised path",
            id="backslash-share",
        ),
        pytest.param(
            {"shared": [{"path": "shared", "access": "rw"}]},
            "users(): shared must be an absolute, normalised path",
            id="relative-share",
        ),
        pytest.param(
            {"home": "/scratch/w"},
            "users(): home must have {member} once, in its last path component",
            id="no-member",
        ),
        pytest.param(
            {"home": "/scratch/{member}/work"},
            "users(): home must have {member} once, in its last path component",
            id="member-not-last",
        ),
        pytest.param(
            {"home": "/scratch/{member}-{x}"},
            "users(): home must have {member} once, in its last path component, and no other braces",
            id="other-braces",
        ),
        pytest.param(
            {"shared": [Share(path="/scratch", access="rw")]},
            "users(): the share '/scratch' and the members' homes",
            id="share-is-homes-parent",
        ),
        pytest.param(
            {"shared": [Share(path="/scratch/common", access="ro")]},
            "users(): the share '/scratch/common' and the members' homes",
            id="share-in-homes-parent",
        ),
        pytest.param(
            {"home": "/shared/homes/{member}"},
            "users(): the share '/shared' and the members' homes",
            id="homes-in-share",
        ),
        pytest.param(
            {
                "shared": [
                    Share(path="/data", access="ro"),
                    Share(path="/data/out", access="rw"),
                ]
            },
            "users(): the shares '/data' and '/data/out' overlap",
            id="share-in-share",
        ),
        pytest.param(
            {
                "shared": [
                    Share(path="/data", access="ro"),
                    Share(path="/data", access="rw"),
                ]
            },
            "users(): the shares '/data' and '/data' overlap",
            id="same-share",
        ),
        *(
            pytest.param(
                {"shared": [Share(path=path, access="rw")]},
                f"users(): the share {path!r} overlaps {root!r}, a directory every user writes",
                id=f"transient-{path}",
            )
            for path, root in [
                ("/tmp", "/tmp"),
                ("/var/tmp", "/var/tmp"),
                ("/dev/shm", "/dev/shm"),
                ("/run/lock", "/run/lock"),
                ("/var", "/var/tmp"),
                ("/dev/shm/team", "/dev/shm"),
            ]
        ),
    ],
)
def test_users_refuses_unsafe_or_ambiguous_paths(
    kwargs: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError) as raised:
        users(**kwargs)
    assert str(raised.value).startswith(message)


@pytest.mark.parametrize(
    "build,message",
    [
        pytest.param(
            lambda: swarm(
                member(scoped, args={"seen": {}}, name="Worker"), isolation="users"
            ),
            "swarm(): under users isolation each member runs as the Linux user swarm-<name>, so member names must be lowercase letters",
            id="uppercase-name",
        ),
        pytest.param(
            lambda: swarm(
                member(scoped, args={"seen": {}}, name="w" * 27), isolation="users"
            ),
            "swarm(): under users isolation each member runs as the Linux user swarm-<name>",
            id="long-name",
        ),
        pytest.param(
            lambda: swarm(
                member(scoped, args={"seen": {}}, name="w", choose_name=True),
                isolation="users",
            ),
            "swarm(): member 'w' chooses its own name (choose_name=True)",
            id="choose-name",
        ),
        pytest.param(
            lambda: swarm(
                member(
                    scoped, args={"seen": {}}, name="w", count=2, sandbox="{member}"
                ),
                channels=[],
                isolation="users",
            ),
            "swarm(): users isolation runs the members in one shared sandbox",
            id="own-sandboxes",
        ),
        pytest.param(
            lambda: swarm(
                member(scoped, args={"seen": {}}, name="w"),
                channels=[filesystem(scratch="/work/{member}")],
                isolation="users",
            ),
            "swarm(): the filesystem channel's scratch ('/work/{member}') is not the members' home under users isolation ('/scratch/{member}')",
            id="filesystem-scratch",
        ),
        pytest.param(
            lambda: swarm(
                member(scoped, args={"seen": {}}, name="w"),
                isolation=users(shared=[Share(path="/shared", access="ro")]),
            ),
            "swarm(): the filesystem channel's shared directory ('/shared') is not a read-write share under users isolation (none)",
            id="filesystem-shared-ro",
        ),
        *(
            pytest.param(
                lambda notes=notes: swarm(
                    member(scoped, args={"seen": {}}, name="w"),
                    channels=[filesystem(notes=notes)],
                    isolation=users(
                        shared=[
                            Share(path="/shared", access="rw"),
                            Share(path="/data", access="ro"),
                        ]
                    ),
                ),
                f"swarm(): the filesystem channel's notes file ({path!r}) is not inside a read-write share under users isolation ('/shared')",
                id=f"filesystem-notes-{case}",
            )
            for case, notes, path in [
                ("absolute", "/notes.md", "/notes.md"),
                ("escaping", "../notes.md", "/shared/../notes.md"),
                ("ro-share", "/data/notes.md", "/data/notes.md"),
            ]
        ),
        pytest.param(
            lambda: swarm(
                member(scoped, args={"seen": {}}, name="w"),
                isolation="groups",  # pyright: ignore[reportArgumentType]
            ),
            "swarm(): isolation must be None, 'users' or users(...), got 'groups'.",
            id="unknown-isolation",
        ),
        pytest.param(
            lambda: swarm(
                member(scoped, args={"seen": {}}, name="w"),
                channels=[],
                isolation=UserIsolation(home="/scratch/w"),
            ),
            "swarm(): isolation: home must have {member} once",
            id="model-checked",
        ),
    ],
)
def test_swarm_refuses_rosters_and_channels_users_isolation_cannot_serve(
    build: Any, message: str
) -> None:
    with pytest.raises(ValueError) as raised:
        build()
    assert str(raised.value).startswith(message)


def test_the_notes_file_may_be_in_any_read_write_share() -> None:
    swarm(
        member(scoped, args={"seen": {}}, name="w"),
        channels=[filesystem(notes="/notes/team.md")],
        isolation=users(
            shared=[
                Share(path="/shared", access="rw"),
                Share(path="/notes", access="rw"),
            ]
        ),
    )


def test_isolation_resolves_from_a_name_or_its_fields() -> None:
    default = users()
    assert default == UserIsolation()
    data = users(home="/home/{member}", peers="read").model_dump()
    assert UserIsolation.model_validate(data) == users(
        home="/home/{member}", peers="read"
    )
    # accepted by swarm() as a name, a model and its fields
    for isolation in ("users", default, data):
        swarm(
            member(scoped, args={"seen": {}}, name="w"),
            channels=[],
            isolation=isolation,  # pyright: ignore[reportArgumentType]
        )


@pytest.mark.anyio
async def test_ids_the_probe_reports_taken_are_skipped() -> None:
    seen: dict[str, dict[str, str | None]] = {}
    stub = IsolationStub(probe="begin\nid 61000\nid 61002\nend\n")
    with environments(default=stub):
        await run_swarm(swarm(_scoped(seen), channels=[], isolation="users"))
    assert stub.group == 61001
    assert stub.users == {"swarm-w-1": 61003, "swarm-w-2": 61004}


def _team(
    seen: dict[str, dict[str, str | None]], name: str, sandbox: str = "default"
) -> Agent:
    return swarm(
        member(scoped, args={"seen": seen}, name=name, count=2),
        channels=[],
        isolation=users(sandbox=sandbox),
    )


_ONCE = "users isolation sets up a sandbox once per sample, and another swarm with users isolation in this sample has set up sandbox 'default', or tried to."


@pytest.mark.anyio
@pytest.mark.parametrize("how", ["after", "subtask", "fork"])
@pytest.mark.parametrize("provider", [IsolationStub, UnhashableStub])
async def test_a_second_swarm_with_users_isolation_in_a_sandbox_is_refused(
    how: str, provider: type[IsolationStub]
) -> None:
    # each subtask and each fork has a store of its own; the sandbox is the sample's
    seen: dict[str, dict[str, str | None]] = {}
    stub = provider()

    @subtask
    async def child() -> str:
        await run(_team(seen, "b"), "task")
        return "b"

    async def generate(state: TaskState, *args: Any, **kwargs: Any) -> TaskState:
        return state

    state = TaskState(
        model=ModelName("mockllm/model"),
        sample_id=1,
        epoch=1,
        input="task",
        messages=[ChatMessageUser(content="task")],
    )
    with environments(default=stub):
        async with sample():
            set_task_generate(generate)
            await run(_team(seen, "a"), "task")
            calls = len(stub.calls)
            with pytest.raises(RuntimeError) as raised:
                if how == "after":
                    await run(_team(seen, "b"), "task")
                elif how == "subtask":
                    await child()
                else:
                    await fork(state, as_solver(_team(seen, "b")))
    assert str(raised.value).startswith(_ONCE)
    # refused before it runs anything in the sandbox
    assert len(stub.calls) == calls
    assert sorted(stub.users) == ["swarm-a-1", "swarm-a-2"]
    assert sorted(seen) == ["a-1", "a-2"]


@pytest.mark.anyio
async def test_one_of_two_swarms_starting_together_in_a_sandbox_is_refused() -> None:
    seen: dict[str, dict[str, str | None]] = {}
    stub = IsolationStub()
    errors: dict[str, str] = {}

    async def attempt(name: str) -> None:
        try:
            await run(_team(seen, name), "task")
        except RuntimeError as ex:
            errors[name] = str(ex)

    with environments(default=stub):
        async with sample():
            async with anyio.create_task_group() as tg:
                for name in ("a", "b"):
                    tg.start_soon(attempt, name)
    [(refused, error)] = errors.items()
    assert error.startswith(_ONCE)
    made = "b" if refused == "a" else "a"
    assert sorted(stub.users) == [f"swarm-{made}-1", f"swarm-{made}-2"]
    assert sorted(seen) == [f"{made}-1", f"{made}-2"]
    # one root-access check, one probe and one setup in the sandbox
    scripts = [
        script
        for _, script, _ in stub.calls
        if _REAP_MARK not in script and _CENSUS_MARK not in script
    ]
    for part in ("/proc/self/status", "echo begin", "add_users()"):
        assert sum(part in script for script in scripts) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("provider", [IsolationStub, UnhashableStub, EqualStub])
async def test_swarms_in_two_sandboxes_each_set_up_their_own(
    provider: type[IsolationStub],
) -> None:
    seen: dict[str, dict[str, str | None]] = {}
    stubs = {"default": provider(), "other": provider()}
    with environments(**stubs):
        async with sample():
            async with anyio.create_task_group() as tg:
                for name, sandbox in (("a", "default"), ("b", "other")):
                    tg.start_soon(run, _team(seen, name, sandbox), "task")
    assert sorted(stubs["default"].users) == ["swarm-a-1", "swarm-a-2"]
    assert sorted(stubs["other"].users) == ["swarm-b-1", "swarm-b-2"]


@pytest.mark.anyio
@pytest.mark.parametrize("isolated", [True, False])
async def test_users_isolation_is_refused_inside_an_identity_scope(
    isolated: bool,
) -> None:
    seen: dict[str, dict[str, str | None]] = {}
    stub = IsolationStub()
    team = swarm(_scoped(seen), channels=[], isolation="users" if isolated else None)
    with environments(default=stub), sandbox_identity("outer", cwd="/outer"):
        if isolated:
            with pytest.raises(
                RuntimeError,
                match="users isolation cannot start inside a sandbox identity scope",
            ):
                await run_swarm(team)
        else:
            await run_swarm(team)
    # refused before setup runs anything
    assert stub.calls == []
    if not isolated:
        # a swarm without isolation keeps the scope it runs in
        assert {(s["user"], s["cwd"]) for s in seen.values()} == {("outer", "/outer")}


@agent
def ends(how: str) -> Agent:
    """End by returning, by raising, or by waiting until the swarm stops or is cancelled."""

    async def execute(state: AgentState) -> AgentState:
        if how == "raises":
            raise RuntimeError("the member's own error")
        if how == "waits":
            await anyio.sleep(3600)
        return state

    return execute


def _pair(first: str, second: str, isolation: str | None = "users") -> Agent:
    return swarm(
        [
            member(ends, args={"how": first}, name="a"),
            member(ends, args={"how": second}, name="b"),
        ],
        channels=[],
        budget=Budget(grace=0),
        isolation=isolation,  # pyright: ignore[reportArgumentType]
    )


# Runs the swarm in a sample, cancelling it after `cancel_after` seconds if given; returns
# its record, what it raised, and the sample's transcript.
async def _run(
    team: Agent, cancel_after: float | None = None
) -> tuple[SwarmResultRecord, Exception | None, Transcript]:
    error: Exception | None = None
    async with sample() as t:
        with anyio.move_on_after(cancel_after if cancel_after is not None else 60):
            try:
                await run(team, "task")
            except Exception as ex:
                error = ex
        record = _transcript_result(store())
    assert record is not None
    return record, error, t


_REAPED = "begin\nkilled 2\nleft 0\nkilled-name sleep\nkilled-name python3\nend\n"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "first,second,cancel_after",
    [
        # the second is still running when the first's submission stops the swarm
        pytest.param("returns", "waits", None, id="returned-and-stopped"),
        pytest.param("raises", "waits", None, id="raised"),
        pytest.param("waits", "waits", 0.5, id="cancelled-from-outside"),
    ],
)
async def test_each_member_is_reaped_when_it_ends_and_keeps_its_outcome(
    first: str, second: str, cancel_after: float | None
) -> None:
    stub = IsolationStub(reap=_REAPED)
    with environments(default=stub):
        record, error, _ = await _run(_pair(first, second), cancel_after)
    with environments(default=IsolationStub()):
        plain, plain_error, _ = await _run(_pair(first, second, None), cancel_after)
    # each member is reaped once, as root, before its result is written
    assert sorted(uid for uid, _ in stub.reaped) == ["61001", "61002"]
    for uid, ended in stub.reaped:
        assert {"61001": "a", "61002": "b"}[uid] not in ended
    assert all(user == "root" for user, script, _ in stub.calls if _REAP_MARK in script)
    assert [(m.user, m.uid, m.home) for m in record.members] == [
        ("swarm-a", 61001, "/scratch/a"),
        ("swarm-b", 61002, "/scratch/b"),
    ]
    for result in record.members:
        assert result.reap == MemberReap(
            status="ok", killed=2, killed_names=["sleep", "python3"]
        )
    assert record.isolation_incomplete is False
    # the members end as they do without isolation, with the same error
    assert [m.status for m in record.members] == [m.status for m in plain.members]
    assert repr(error) == repr(plain_error)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "stub,reap",
    [
        pytest.param(
            lambda: IsolationStub(),
            MemberReap(status="ok"),
            id="ok",
        ),
        pytest.param(
            lambda: IsolationStub(
                reap="begin\nkilled 3\nleft 2\nkilled-name sleep\nleft-name spinner\nleft-name spinner\nend\n"
            ),
            MemberReap(
                status="survivors",
                killed=3,
                killed_names=["sleep"],
                survivors=2,
                survivor_names=["spinner", "spinner"],
            ),
            id="survivors",
        ),
        pytest.param(
            lambda: IsolationStub(stall_on=_REAP_MARK),
            MemberReap(
                status="timeout",
                message="the sandbox did not finish stopping the member's processes within 0.2 seconds",
            ),
            id="stalled",
        ),
        pytest.param(
            lambda: IsolationStub(raises=(_REAP_MARK, TimeoutError("timed out"))),
            MemberReap(status="timeout", message="timed out"),
            id="exec-timeout",
        ),
        pytest.param(
            lambda: IsolationStub(raises=(_REAP_MARK, ConnectionError("lost"))),
            MemberReap(status="error", message="ConnectionError: lost"),
            id="provider-error",
        ),
        pytest.param(
            lambda: IsolationStub(
                fail=ExecResult(False, 2, "", "sh: 1: mktemp: not found\n"),
                fail_on=_REAP_MARK,
            ),
            MemberReap(status="error", message="sh: 1: mktemp: not found"),
            id="failed-command",
        ),
        pytest.param(
            # what a provider that keeps only the last bytes of a long output returns
            lambda: IsolationStub(reap="left 0\nend\n"),
            MemberReap(
                status="error",
                message="the output of the command that stops the member's processes came back incomplete",
            ),
            id="cut-short",
        ),
    ],
)
async def test_reaping_records_what_it_found_and_never_replaces_the_outcome(
    stub: Callable[[], IsolationStub],
    reap: MemberReap,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("inspect_swarm._isolation._REAP_BOUND", 0.2)
    for how in ("returns", "raises"):
        team = swarm(
            member(ends, args={"how": how}, name="w"), channels=[], isolation="users"
        )
        start = anyio.current_time()
        with environments(default=stub()):
            record, error, t = await _run(team)
        assert anyio.current_time() - start < 3
        [result] = record.members
        assert result.reap == reap
        # the member's own status and error
        if how == "returns":
            assert (result.status, error) == ("submitted", None)
        else:
            assert result.status == "errored"
            assert repr(error) == repr(RuntimeError("the member's own error"))
        assert record.isolation_incomplete is (reap.status != "ok")
        [metrics] = swarm_events(t, "metrics")
        assert metrics.data["reap_complete"] is (reap.status == "ok")


@agent
def custom_tool() -> Agent:
    """Run sandbox commands as a custom tool would: as root, as a peer, as the member and with no user; and as one of Inspect's own operations would."""

    async def execute(state: AgentState) -> AgentState:
        context = current_member.get()
        assert context is not None
        if context.name == "w-1":
            for user in ("root", "swarm-w-2", "swarm-w-1", None):
                await sandbox().exec([SHELL_PATH, "-c", "true"], user=user)
            # Inspect's own operations, such as the injected tools' launcher
            with framework_sandbox_operation():
                await sandbox().exec([SHELL_PATH, "-c", "true"], user="root")
        return state

    return execute


@pytest.mark.anyio
async def test_foreign_execs_count_a_members_commands_as_another_user() -> None:
    stub = IsolationStub()
    team = swarm(
        member(custom_tool, name="w", count=2),
        controller=leaderless(stop_on_submission=False),
        channels=[],
        isolation="users",
    )
    # the sample's proxy, which records each command's user
    with environments(default=SandboxEnvironmentProxy(stub)):
        outcome = await run_swarm(team)
    assert {m.name: m.foreign_execs for m in outcome.record.members} == {
        "w-1": 2,
        "w-2": 0,
    }


# a world-writable directory whose name holds a CR and what would read as an id line
_PROBE_WRITABLE = (
    "begin\nwritable /tmp\nwritable /opt/drop\rid 61001\nwritable /shared/inner\nend\n"
)

_CENSUS = """begin
root
path 61001 f /shared/a b
owner 61001 1 5
owner 0 2 10
owner 1000 1 7
root
path 61002 l /tmp/link
path 61002 d /tmp/d
owner 61002 2 0
capped
root
absent
root
path 61001 f /dev/shm/s
owner 61001 1 3
error find: '/dev/shm/x': Permission denied
root
root
end
"""


@pytest.mark.anyio
async def test_the_census_counts_owners_under_the_roots_setup_recorded() -> None:
    seen: dict[str, dict[str, str | None]] = {}
    stub = IsolationStub(probe=_PROBE_WRITABLE, census=_CENSUS)
    isolation = users(
        shared=[Share(path="/shared", access="rw"), Share(path="/data", access="ro")]
    )
    with environments(default=stub):
        outcome = await run_swarm(
            swarm(_scoped(seen), channels=[], isolation=isolation)
        )
    roots = [
        "/shared",
        "/tmp",
        "/var/tmp",
        "/dev/shm",
        "/run/lock",
        "/opt/drop\rid 61001",
    ]
    kinds = ["share", "transient", "transient", "transient", "transient"]
    # a world-writable directory that overlaps a root is left out
    [isolation_event] = swarm_events(outcome.transcript, "isolation")
    assert isolation_event.data == {
        "version": 1,
        "sandbox": "default",
        "members": [
            {"name": "w-1", "user": "swarm-w-1", "uid": 61001, "home": "/scratch/w-1"},
            {"name": "w-2", "user": "swarm-w-2", "uid": 61002, "home": "/scratch/w-2"},
        ],
        "census_roots": [
            {"path": path, "kind": kind}
            for path, kind in zip(roots, [*kinds, "world_writable"], strict=True)
        ],
        "census_roots_capped": False,
    }
    # once the members have ended, as root, with the cap and the member uids
    [(user, _, args)] = [c for c in stub.calls if _CENSUS_MARK in c[1]]
    assert (user, args) == ("root", ["10000", "61001 61002", *roots])
    [census] = swarm_events(outcome.transcript, "census")

    def root(path: str, kind: str, **fields: Any) -> dict[str, Any]:
        return {
            "path": path,
            "kind": kind,
            "status": "ok",
            "error": None,
            "capped": False,
            "members": {},
            "others": {"entries": 0, "bytes": 0},
            **fields,
        }

    assert census.data == {
        "version": 1,
        "status": "ok",
        "roots": [
            root(
                "/shared",
                "share",
                members={
                    "w-1": {
                        "entries": 1,
                        "bytes": 5,
                        "paths": [{"type": "file", "path": "/shared/a b"}],
                    }
                },
                others={"entries": 3, "bytes": 17},
            ),
            root(
                "/tmp",
                "transient",
                capped=True,
                members={
                    "w-2": {
                        "entries": 2,
                        "bytes": 0,
                        "paths": [
                            {"type": "symlink", "path": "/tmp/link"},
                            {"type": "directory", "path": "/tmp/d"},
                        ],
                    }
                },
            ),
            root("/var/tmp", "transient", status="absent"),
            root(
                "/dev/shm",
                "transient",
                status="unreadable",
                error="find: '/dev/shm/x': Permission denied",
                members={
                    "w-1": {
                        "entries": 1,
                        "bytes": 3,
                        "paths": [{"type": "file", "path": "/dev/shm/s"}],
                    }
                },
            ),
            root("/run/lock", "transient"),
            root("/opt/drop\rid 61001", "world_writable"),
        ],
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    "stub,data",
    [
        pytest.param(
            lambda: IsolationStub(raises=(_CENSUS_MARK, ConnectionError("lost"))),
            {"version": 1, "status": "error", "message": "ConnectionError: lost"},
            id="provider-error",
        ),
        pytest.param(
            # a line the census script does not write
            lambda: IsolationStub(
                census="begin\n" + "root\nowner broken\n" * 5 + "end\n"
            ),
            {
                "version": 1,
                "status": "error",
                "message": "the census's output could not be read",
            },
            id="unreadable-line",
        ),
        pytest.param(
            lambda: IsolationStub(census="begin\nroot\nend\n"),
            {
                "version": 1,
                "status": "error",
                "message": "the census's output came back incomplete",
            },
            id="cut-short",
        ),
    ],
)
async def test_a_census_that_fails_is_recorded_and_the_swarm_ends_as_usual(
    stub: Callable[[], IsolationStub], data: dict[str, Any]
) -> None:
    seen: dict[str, dict[str, str | None]] = {}
    with environments(default=stub()):
        outcome = await run_swarm(swarm(_scoped(seen), channels=[], isolation="users"))
    [census] = swarm_events(outcome.transcript, "census")
    assert census.data == data
    assert outcome.record.final.member is not None
    assert outcome.record.final.provisional is False


@pytest.mark.anyio
@pytest.mark.parametrize("separator", ["\r", "\v", "\f", "\x1c", "\x85", "\u2028"])
async def test_a_path_cannot_add_records_to_the_census(separator: str) -> None:
    # a member's file whose name holds a character Python reads as a line break, then
    # what would read as a peer's count; the census script ends records with LF only
    path = f"/tmp/x{separator}owner 61002 50 500"
    census = f"begin\nroot\npath 61001 f {path}\nowner 61001 1 4\n" + "root\n" * 3
    stub = IsolationStub(census=census + "end\n")
    seen: dict[str, dict[str, str | None]] = {}
    with environments(default=stub):
        outcome = await run_swarm(
            swarm(_scoped(seen), channels=[], isolation=users(shared=()))
        )
    [census_event] = swarm_events(outcome.transcript, "census")
    tmp = cast(dict[str, Any], census_event.data)["roots"][0]
    assert tmp["path"] == "/tmp"
    assert tmp["members"] == {
        "w-1": {"entries": 1, "bytes": 4, "paths": [{"type": "file", "path": path}]}
    }
    assert outcome.record.final.provisional is False


@pytest.mark.anyio
async def test_no_callback_can_be_registered_while_a_member_is_reaped() -> None:
    # the member's agent never reaches a boundary; a callback accepted after the
    # wrapper's last check would never run, and the member would pass as supervised
    stub = IsolationStub()
    stub.reaping, stub.hold_reaping = anyio.Event(), anyio.Event()
    refused: list[str] = []

    async def callback(b: Boundary) -> None:
        return None

    async def run_controller(control: SwarmControl) -> StopReason:
        control.start("w")
        assert stub.reaping is not None and stub.hold_reaping is not None
        await stub.reaping.wait()
        # its agent has ended, and its end waits for the reaping
        assert control.member("w").state == "running"
        try:
            control.member("w").on_boundary(callback, origin="operator")
        except RuntimeError as ex:
            refused.append(str(ex))
        stub.hold_reaping.set()
        events = control.events()
        async with aclosing(events):
            async for _ in events:
                pass
        return "all_done"

    team = swarm(
        member(generator(scripted_model(), calls=1), name="w"),
        controller=controller(run_controller, "isolation_test_reaping_callback"),
        channels=[],
        isolation="users",
    )
    with environments(default=stub):
        outcome = await run_swarm(team)
    assert refused == ["on_boundary('w'): the member has ended."]
    assert [m.status for m in outcome.record.members] == ["submitted"]
    assert outcome.record.final.member == "w"


@pytest.mark.anyio
@pytest.mark.parametrize("stop_on_submission", [True, False])
async def test_a_member_stopped_while_it_is_reaped_is_not_counted_cancelled(
    stop_on_submission: bool,
) -> None:
    # both submit; the first's end stops the swarm while the second is still reaped,
    # which is cleanup, not cancelled work
    stub = IsolationStub()
    stub.reap_delay = {"61002": 0.3}
    team = swarm(
        member(ends, args={"how": "returns"}, name="w", count=2),
        controller=leaderless(stop_on_submission=stop_on_submission),
        channels=[],
        budget=Budget(grace=0),
        isolation="users",
    )
    with environments(default=stub):
        outcome = await run_swarm(team)
    assert [m.status for m in outcome.record.members] == ["submitted", "submitted"]
    [ledger] = swarm_events(outcome.transcript, "ledger")
    data = cast(dict[str, Any], ledger.data)
    assert [m["cancelled"] for m in data["members"]] == [False, False]
    assert data["total_complete"] is True


# The census script, run here by /bin/sh with `find` replaced by a function that writes
# the given records and exits with the given status, after writing `errors` to stderr.
def _census_script(
    root: Path, records: str, status: int, errors: str = "", cap: int = 3
) -> list[str]:
    fake = (
        "find() {\n"
        f"  printf '{errors}' >&2\n"
        f"  printf '{records}'\n"
        f"  return {status}\n"
        "}\n"
    )
    script = _CENSUS_SCRIPT.replace("export LC_ALL=C\n", "export LC_ALL=C\n" + fake, 1)
    result = subprocess.run(
        ["/bin/sh", "-c", script, "sh", str(cap), "61001", str(root)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.split("\n")[1:-2]


_ROW = r"61001 f 4 /r/a\000"


@pytest.mark.parametrize(
    "records,status,errors,lines",
    [
        pytest.param(_ROW, 0, "", ["path 61001 f /r/a", "owner 61001 1 4"], id="ok"),
        # killed (as by the kernel's out-of-memory killer), before and after a row
        pytest.param("", 137, "", ["error find exited with status 137"], id="killed"),
        pytest.param(
            _ROW,
            137,
            "",
            [
                "path 61001 f /r/a",
                "owner 61001 1 4",
                "error find exited with status 137",
            ],
            id="killed-after-a-row",
        ),
        pytest.param(
            _ROW,
            1,
            "",
            ["path 61001 f /r/a", "owner 61001 1 4", "error find exited with status 1"],
            id="silent-failure",
        ),
        # an entry removed while it ran
        pytest.param(
            _ROW,
            1,
            r"find: /r/gone: No such file or directory\n",
            ["path 61001 f /r/a", "owner 61001 1 4"],
            id="removed-entry",
        ),
        pytest.param(
            _ROW,
            1,
            r"find: /r/x: Permission denied\n",
            [
                "path 61001 f /r/a",
                "owner 61001 1 4",
                "error find: /r/x: Permission denied",
            ],
            id="reported-error",
        ),
        # stopped at the cap, with the status a write to a closed pipe gives
        pytest.param(_ROW * 5, 141, "", None, id="capped"),
    ],
)
def test_the_census_reports_a_scan_that_failed(
    tmp_path: Path, records: str, status: int, errors: str, lines: list[str] | None
) -> None:
    out = _census_script(tmp_path, records, status, errors)
    assert out[0] == "root"
    if lines is None:
        assert "capped" in out and not any(line.startswith("error") for line in out)
        assert "owner 61001 3 12" in out
    else:
        assert out[1:] == lines
