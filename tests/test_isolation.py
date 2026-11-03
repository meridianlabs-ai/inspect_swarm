from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal, overload

import anyio
import pytest
from inspect_ai import Task, eval  # pyright: ignore[reportUnknownVariableType]
from inspect_ai.agent import Agent, AgentState, agent
from inspect_ai.dataset import Sample
from inspect_ai.util import (
    ExecResult,
    SandboxEnvironment,
    sandbox,
    store,
    tool_state_instance,
)
from inspect_ai.util._sandbox._privileged import SHELL_PATH
from inspect_ai.util._sandbox.context import (
    sandbox_default_context_var,
    sandbox_environments_context_var,
    sandbox_with_environments_context_var,
)
from inspect_ai.util._sandbox.identity import (
    sandbox_identity_cwd,
    sandbox_identity_user,
)

from inspect_swarm import Share, UserIsolation, filesystem, member, swarm, users
from inspect_swarm._budget import current_member

from .swarm_runtime import run_swarm

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
    ) -> None:
        super().__init__()
        self.stall_on = stall_on
        self.stall_timeouts: list[int | None] = []
        self.calls: list[tuple[str | None, str, list[str]]] = []
        self.uid = uid
        self.fail = fail
        self.fail_on = fail_on
        self.probe = probe

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


@contextmanager
def environments(
    **stubs: IsolationStub,
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
    user, script, removed = stub.calls[-1]
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
        assert stub.calls[-2] == (
            "swarm-w-2",
            stub.calls[-2][1],
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
        record = store().get("inspect_swarm:isolation")
    assert {name: (s["user"], s["cwd"], s["instance"]) for name, s in seen.items()} == {
        "w-1": ("swarm-w-1", "/scratch/w-1", "@w-1"),
        "w-2": ("swarm-w-2", "/scratch/w-2", "@w-2"),
    }
    # setup runs as root; the self-test and then the probes run as each member, and a
    # peer test as the second; the probes are removed as root
    users = [str(user) for user, _, _ in stub.calls]
    assert users[:3] == ["root", "root", "root"]
    assert sorted(users[3:5]) == sorted(users[5:7]) == ["swarm-w-1", "swarm-w-2"]
    assert users[7:] == ["swarm-w-2", "root"]
    # every path and name reaches the scripts as an argument
    _, setup, args = stub.calls[2]
    assert "swarm-w-1" not in setup and "/scratch" not in setup
    assert args == [
        "inspect-swarm",
        "61000",
        "1",
        "none",
        "/scratch",
        "61001 61002",
        *("user", "swarm-w-1", "61001", "/scratch/w-1"),
        *("user", "swarm-w-2", "61002", "/scratch/w-2"),
        *("share", "rw", "/shared"),
    ]
    assert record == {
        "default": {
            "gid": 61000,
            "users": {"swarm-w-1": 61001, "swarm-w-2": 61002},
        }
    }


@pytest.mark.anyio
async def test_members_of_a_swarm_without_isolation_run_as_the_default_user() -> None:
    seen: dict[str, dict[str, str | None]] = {}
    stub = IsolationStub()
    with environments(default=stub):
        await run_swarm(swarm(_scoped(seen), channels=[]))
    assert {s["user"] for s in seen.values()} == {None}
    assert stub.calls == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "isolation,stub,error,message",
    [
        pytest.param(
            users(sandbox="workspace"),
            IsolationStub(),
            ValueError,
            "users(sandbox='workspace'): this sample has no such sandbox",
            id="unknown-sandbox",
        ),
        pytest.param(
            users(),
            # what a provider that keeps only the last bytes of a long output returns
            IsolationStub(probe="id 61003\nid 61004\nend\n"),
            RuntimeError,
            "users isolation: the id scan's output in sandbox 'default' came back incomplete",
            id="truncated-probe",
        ),
        pytest.param(
            users(),
            IsolationStub(uid="1000"),
            RuntimeError,
            "users isolation needs root in sandbox 'default' to create users; its root access is unusable (commands run as uid 1000, not as root)",
            id="no-root",
        ),
        pytest.param(
            users(),
            IsolationStub(
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
    stub: IsolationStub,
    error: type[Exception],
    message: str,
) -> None:
    seen: dict[str, dict[str, str | None]] = {}
    with environments(default=stub), pytest.raises(error) as raised:
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
            "You run as the Linux user swarm-w-1, without root. Your private directory is /scratch/w-1 (your $HOME); other members cannot read it. Shared with all members: /shared (read and write), /data (read only). Use $TMPDIR for temporary files. Git refuses a repository another user created until you run git config --global --add safe.directory <path>.",
            id="none",
        ),
        pytest.param(
            users(
                home="/home/{member}",
                shared=[Share(path="/data", access="ro")],
                peers="read",
            ),
            "You run as the Linux user swarm-w-1, without root. Your private directory is /home/w-1 (your $HOME); other members can read it but not change it. Shared with all members: /data (read only). Use $TMPDIR for temporary files.",
            id="read",
        ),
        pytest.param(
            users(shared=()),
            "You run as the Linux user swarm-w-1, without root. Your private directory is /scratch/w-1 (your $HOME); other members cannot read it. Use $TMPDIR for temporary files.",
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
        record = store().get("inspect_swarm:isolation")
    assert record == {
        "default": {"gid": 61001, "users": {"swarm-w-1": 61003, "swarm-w-2": 61004}}
    }
