"""Users isolation in Docker: setup, the members' identities, access between members, unsafe paths, refusals and git.

Each test runs one eval whose samples are cases: one or more swarm runs, each with users isolation and members that run their steps one after another, so a later member sees what an earlier one left. What each step returned is kept by case, member and step.
"""

from contextlib import aclosing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from inspect_ai import Task, eval  # pyright: ignore[reportUnknownVariableType]
from inspect_ai.agent import Agent, AgentState, agent, as_solver
from inspect_ai.dataset import Sample
from inspect_ai.log import EvalSample
from inspect_ai.solver import Generate, Solver, TaskState, solver
from inspect_ai.tool import bash, bash_session, python, text_editor
from inspect_ai.util import sandbox

from inspect_swarm import (
    Controller,
    StopReason,
    SwarmControl,
    member,
    swarm,
    swarm_controller,
    users,
)
from inspect_swarm._budget import current_member

_IMAGE = Path(__file__).parent / "isolation"

# a tmpfs at the homes' parent, so that setup scans it as a filesystem of its own
_COMPOSE = """\
services:
  default:
    build:
      context: {context}
    image: inspect-swarm-isolation-test
    command: tail -f /dev/null
    init: true
    network_mode: none
    tmpfs:
      - /scratch:mode=755
{extra}"""

Step = tuple[str, str]
"""A member's step: the tool and its input. `bash`, `python`, `session` (`bash_session()`), `team` (`bash_session(instance="team")`), `root` (`bash(user="root")`) or `edit` (`text_editor()` creating the file)."""


@dataclass
class Run:
    """One swarm run in a case: its users isolation and its members' steps, in the order they run."""

    isolation: dict[str, Any]
    members: dict[str, list[Step]]
    before: str | None = None
    """A root command run before the swarm."""


@dataclass
class Case:
    runs: list[Run]
    setup: str | None = None
    error: str | None = None
    """What the sample's error contains, when it must fail."""
    results: dict[str, list[str]] = field(default_factory=lambda: {})


_CASES: dict[str, Case] = {}


async def _in_turn(control: SwarmControl) -> StopReason:
    # one member at a time, in roster order
    for m in control.members:
        control.start(m)
        events = control.events()
        async with aclosing(events):
            async for _ in events:
                pass
    return "all_done"


@swarm_controller(name="isolation_test_in_turn")
def in_turn() -> Controller:
    return Controller(_in_turn)


@agent
def actor(case: str, steps: dict[str, list[Step]]) -> Agent:
    """Run the member's steps with built-in tools, keeping what each returned or raised."""

    async def execute(state: AgentState) -> AgentState:
        context = current_member.get()
        assert context is not None
        results = _CASES[case].results.setdefault(context.name, [])
        for tool, value in steps[context.name]:
            try:
                if tool == "bash":
                    output = await bash()(value)
                elif tool == "python":
                    output = await python()(value)
                elif tool == "session":
                    output = await bash_session()(action="type_submit", input=value)
                elif tool == "team":
                    output = await bash_session(instance="team")(
                        action="type_submit", input=value
                    )
                elif tool == "root":
                    output = await bash(user="root")(value)
                else:
                    output = await text_editor()(
                        command="create", path=value, file_text="x"
                    )
                results.append(f"ok: {output}")
            except Exception as ex:
                results.append(f"{type(ex).__name__}: {ex}")
        return state

    return execute


@solver
def case_solver() -> Solver:
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        case = str(state.sample_id)
        for run in _CASES[case].runs:
            if run.before is not None:
                result = await sandbox().exec(["sh", "-c", run.before], user="root")
                assert result.success, result.stderr
            team = swarm(
                [
                    member(actor, args={"case": case, "steps": run.members}, name=name)
                    for name in run.members
                ],
                controller=in_turn(),
                channels=[],
                isolation=users(**run.isolation),
            )
            state = await as_solver(team)(state, generate)
        return state

    return solve


def _eval(
    tmp_path: Path, cases: dict[str, Case], compose_extra: str = ""
) -> dict[str, EvalSample]:
    _CASES.update(cases)
    compose = tmp_path / "compose.yaml"
    compose.write_text(_COMPOSE.format(context=_IMAGE, extra=compose_extra))
    [log] = eval(
        Task(
            dataset=[
                Sample(id=name, input="x", setup=case.setup)
                for name, case in cases.items()
            ],
            solver=case_solver(),
            sandbox=("docker", str(compose)),
        ),
        model="mockllm/model",
        log_dir=str(tmp_path / "logs"),
        display="none",
        fail_on_error=False,
    )
    assert log.samples is not None
    samples = {str(s.id): s for s in log.samples}
    for name, case in cases.items():
        error = samples[name].error
        if case.error is None:
            assert error is None, f"{name}: {error.message if error else ''}"
        else:
            assert error is not None, f"{name} did not fail"
            assert case.error in error.message, f"{name}: {error.message}"
    return samples


def _ok(result: str) -> str:
    assert result.startswith("ok: "), result
    return result.removeprefix("ok: ").strip()


def _refused(result: str) -> None:
    # bash() returns a failed command's output
    assert "Permission denied" in _ok(result), result


_RW_RO = {
    "shared": [{"path": "/shared", "access": "rw"}, {"path": "/data", "access": "ro"}]
}

_DATA = "mkdir -p /data && echo data > /data/existing"


@pytest.mark.slow
def test_members_run_as_their_users_with_ids_that_own_no_file(tmp_path: Path) -> None:
    steps: list[Step] = [
        ("bash", "id -un; id -u; pwd; echo $HOME $TMPDIR"),
        ("python", "import getpass, os; print(getpass.getuser(), os.getcwd())"),
        ("session", "id -un; pwd"),
        ("edit", "note.txt"),
        ("bash", "stat -c '%U %a' note.txt"),
    ]
    cases = {
        "tools": Case(
            runs=[Run(_RW_RO, {"w-1": steps, "w-2": steps})],
            # ids in the window taken by files on other filesystems: /dev/shm, and the
            # tmpfs at the homes' parent; the image has files of 1000, 61000 and 61001
            setup=f"{_DATA} && touch /dev/shm/a /scratch/b && chown 61002 /dev/shm/a && chown 61003 /scratch/b",
        )
    }
    samples = _eval(tmp_path, cases)
    results = cases["tools"].results
    uids: set[int] = set()
    for name in ("w-1", "w-2"):
        user, home = f"swarm-{name}", f"/scratch/{name}"
        bash_out, python_out, session_out, edit_out, stat_out = results[name]
        who, uid, pwd, env = _ok(bash_out).splitlines()
        assert (who, pwd, env) == (user, home, f"{home} {home}/tmp")
        uids.add(int(uid))
        assert _ok(python_out) == f"{user} {home}"
        assert f"{user}\n{home}" in _ok(session_out)
        assert _ok(edit_out) == f"File created successfully at: {home}/note.txt"
        assert _ok(stat_out) == f"{user} 644"
    assert not uids & {0, 1000, 61000, 61001, 61002, 61003}
    record = samples["tools"].store["inspect_swarm:isolation"]
    assert set(record["default"]["users"].values()) == uids
    assert record["default"]["gid"] not in {61000, 61001, 61002, 61003}


@pytest.mark.slow
def test_members_cannot_reach_each_others_homes_and_share_the_shares(
    tmp_path: Path,
) -> None:
    writer: list[Step] = [
        ("bash", "echo secret > ~/secret && mkdir /shared/d && echo one > /shared/d/f"),
        ("bash", "touch /shared/top /shared/nested/n"),
    ]
    peer: list[Step] = [
        ("bash", "cat /scratch/w-1/secret"),
        ("bash", "echo x >> /scratch/w-1/secret"),
        ("bash", "mv /scratch/w-1 /scratch/stolen"),
        ("bash", "echo two >> /shared/d/f && cat /shared/d/f"),
        ("bash", "cat /data/existing"),
        ("bash", "echo x >> /data/existing"),
        ("bash", "touch /data/new"),
        (
            "bash",
            "rm /shared/top && mv /shared/nested/n /shared/nested/m && ls /shared/nested",
        ),
    ]
    cases = {
        peers: Case(
            runs=[Run({**_RW_RO, "peers": peers}, {"w-1": writer, "w-2": peer})],
            # sticky directories, which setup must make plain so that members can
            # delete and rename each other's files
            setup=f"{_DATA} && mkdir -p /shared/nested && chmod 1777 /shared /shared/nested",
        )
        for peers in ("none", "read")
    }
    _eval(tmp_path, cases)
    for peers, case in cases.items():
        read, write, rename, share, data, append, create, move = case.results["w-2"]
        if peers == "read":
            assert _ok(read) == "secret"
        else:
            _refused(read)
        for result in (write, rename, append, create):
            _refused(result)
        assert _ok(share) == "one\ntwo"
        assert _ok(data) == "data"
        assert _ok(move) == "m"


@pytest.mark.slow
def test_files_already_in_a_share_are_kept_and_members_edit_them(
    tmp_path: Path,
) -> None:
    # files at the names the self-test's probes once had, which it must not touch; and
    # a directory that was there before setup, in which a member creates and edits a file
    sentinels = "/shared/.swarm-probe-swarm-w-1 /shared/.swarm-probe-swarm-w-1.moved"
    setup = (
        "mkdir -p /shared/inherited && touch /shared/existing"
        f" && for f in {sentinels}; do echo sentinel > $f; done"
    )
    cases = {
        "existing": Case(
            runs=[
                Run(
                    {"shared": [{"path": "/shared", "access": "rw"}]},
                    {
                        "w-1": [
                            (
                                "bash",
                                "echo a >> /shared/existing && echo b > /shared/inherited/new && echo c >> /shared/inherited/new",
                            )
                        ],
                        "w-2": [
                            (
                                "bash",
                                "echo d >> /shared/existing && echo e >> /shared/inherited/new && cat /shared/existing /shared/inherited/new",
                            ),
                            ("bash", f"cat {sentinels} && ls -a /shared"),
                        ],
                    },
                )
            ],
            setup=setup,
        )
    }
    _eval(tmp_path, cases)
    edits, listing = cases["existing"].results["w-2"]
    assert _ok(edits).split() == ["a", "d", "b", "c", "e"]
    lines = _ok(listing).splitlines()
    assert lines[:2] == ["sentinel", "sentinel"]
    assert [n for n in lines[2:] if n.startswith(".swarm-probe")] == [
        ".swarm-probe-swarm-w-1",
        ".swarm-probe-swarm-w-1.moved",
    ]


@pytest.mark.slow
def test_mounts_inside_shares_and_under_the_homes_parent_are_refused(
    tmp_path: Path,
) -> None:
    # setup would not reach the files of a mount inside a share or under the homes'
    # parent, which -xdev keeps it on one filesystem for; a mount at a share's or the
    # homes' parent's own path is supported, as /scratch is in every test here
    idle: dict[str, list[Step]] = {"w-1": [], "w-2": []}
    refused = "users isolation could not set up sandbox 'default': "
    cases = {
        "ro-share": Case(
            runs=[
                Run(
                    {
                        "home": "/home/{member}",
                        "shared": [{"path": "/data", "access": "ro"}],
                    },
                    idle,
                )
            ],
            error=f"{refused}users isolation does not support a mount inside the shared directory /data, as at /data/nested",
        ),
        "homes": Case(
            runs=[Run({"shared": []}, idle)],
            error=f"{refused}users isolation does not support a mount under the members' homes' parent /scratch, as at /scratch/other",
        ),
    }
    _eval(
        tmp_path,
        cases,
        compose_extra="      - /data/nested\n      - /scratch/other\n",
    )


@pytest.mark.slow
def test_a_second_run_takes_over_the_first_runs_share(tmp_path: Path) -> None:
    first = Run(
        {"shared": [{"path": "/shared", "access": "rw"}]},
        {
            "a-1": [("bash", "echo one > /shared/f && mkdir /shared/d")],
            "a-2": [("bash", "echo two >> /shared/f && touch /shared/d/g")],
        },
    )
    # an ACL that grants the first run's group write, which a ro share must remove
    grant = "setfacl -m g:inspect-swarm:rw /shared/f"
    cases = {
        "rw": Case(
            runs=[
                first,
                Run(
                    {"shared": [{"path": "/shared", "access": "rw"}]},
                    {
                        "b-1": [
                            ("bash", "echo three >> /shared/f && echo b > /shared/b")
                        ],
                        "b-2": [
                            (
                                "bash",
                                "echo four >> /shared/f && echo c >> /shared/b && cat /shared/f",
                            )
                        ],
                    },
                ),
            ]
        ),
        "ro": Case(
            runs=[
                first,
                Run(
                    {"shared": [{"path": "/shared", "access": "ro"}]},
                    {
                        "b-1": [
                            ("bash", "echo three >> /shared/f"),
                            ("bash", "touch /shared/d/h"),
                            ("bash", "rm /shared/d/g"),
                            ("bash", "cat /shared/f"),
                        ]
                    },
                    before=grant,
                ),
            ]
        ),
    }
    _eval(tmp_path, cases)
    assert _ok(cases["rw"].results["b-2"][0]) == "one\ntwo\nthree\nfour"
    append, create, remove, read = cases["ro"].results["b-1"]
    for result in (append, create, remove):
        _refused(result)
    assert _ok(read) == "one\ntwo"


@pytest.mark.slow
def test_unsafe_paths_and_unsupported_images_are_refused(tmp_path: Path) -> None:
    idle: dict[str, list[Step]] = {"w-1": [], "w-2": []}
    refused = "users isolation could not set up sandbox 'default': "
    cases = {
        "world-writable-parent": Case(
            runs=[Run({"home": "/tmp/{member}", "shared": []}, idle)],
            error=f"{refused}/tmp is mode 1777; users isolation needs every directory above a member's home to be root-owned and not group- or world-writable",
        ),
        "symlinked-share": Case(
            runs=[Run({"shared": [{"path": "/linked", "access": "rw"}]}, idle)],
            setup="mkdir /real && ln -s /real /linked",
            error=f"{refused}/linked is a symlink or not a directory; users isolation needs each shared directory to be a real directory",
        ),
        "group-writable-parent": Case(
            runs=[Run({"shared": [{"path": "/team/shared", "access": "rw"}]}, idle)],
            setup="mkdir -m 775 /team",
            error=f"{refused}/team is mode 775; users isolation needs every directory above a shared directory to be root-owned and not group- or world-writable",
        ),
        "existing-home": Case(
            runs=[Run({"shared": []}, idle)],
            setup="mkdir /scratch/w-1",
            error=f"{refused}/scratch/w-1 already exists; users isolation creates each member's home",
        ),
        "no-setfacl": Case(
            runs=[Run({"shared": [{"path": "/shared", "access": "rw"}]}, idle)],
            setup="rm /usr/bin/setfacl",
            error=f"{refused}users isolation needs setfacl and getfacl in the sandbox for shared directories (package acl)",
        ),
        "acl-on-homes-parent": Case(
            runs=[Run({"home": "/home/{member}", "shared": []}, idle)],
            setup="setfacl -d -m u:61005:rwx /home",
            error=f"{refused}users isolation does not support ACLs on the members' homes' parent /home, which homes would inherit",
        ),
        "named-acl-on-a-file": Case(
            runs=[Run({"shared": [{"path": "/shared", "access": "rw"}]}, idle)],
            setup="mkdir /shared && touch /shared/existing && setfacl -m u:61003:r-- /shared/existing",
            error=f"{refused}users isolation does not support ACL entries it did not set in a shared directory: /shared/existing: user:61003:r--",
        ),
        "named-default-acl": Case(
            runs=[Run({"shared": [{"path": "/shared", "access": "rw"}]}, idle)],
            setup="mkdir -p /shared/inherited && setfacl -d -m u:61003:r-- /shared/inherited",
            error="/shared/inherited: default:user:61003:r--",
        ),
        "default-owner-acl": Case(
            runs=[Run({"shared": [{"path": "/shared", "access": "rw"}]}, idle)],
            setup="mkdir -p /shared/inherited && setfacl -d -m u::r-x /shared/inherited",
            error=f"{refused}users isolation does not support ACL entries it did not set in a shared directory: /shared/inherited: default:user::r-x",
        ),
        "unreadable-ro-share": Case(
            runs=[Run({"shared": [{"path": "/inputs/data", "access": "ro"}]}, idle)],
            setup="mkdir -m 700 /inputs && mkdir /inputs/data && echo x > /inputs/data/f",
            error="users isolation: the self-test failed in sandbox 'default' as swarm-w-",
        ),
        "sudo": Case(
            runs=[Run({"shared": []}, idle)],
            setup="echo 'ALL ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/all && chmod 0440 /etc/sudoers.d/all",
            error="users isolation: the self-test failed in sandbox 'default' as swarm-w-",
        ),
    }
    samples = _eval(tmp_path, cases)
    sudo = samples["sudo"].error
    assert sudo is not None
    assert "the member can become root with sudo" in sudo.message
    unreadable = samples["unreadable-ro-share"].error
    assert unreadable is not None
    assert (
        "the member cannot read all of the shared directory /inputs/data: find: "
        in unreadable.message
    )


@pytest.mark.slow
def test_an_incomplete_id_scan_is_refused(tmp_path: Path) -> None:
    # without these capabilities root cannot enter a directory another user keeps
    # private, so the scan cannot see the id of the file in it
    cases = {
        "hidden": Case(
            runs=[Run({"shared": []}, {"w-1": []})],
            setup="mkdir /opt/hidden && touch /opt/hidden/f && chown 61000 /opt/hidden/f && chown 1000 /opt/hidden && chmod 700 /opt/hidden",
            # the first unreadable directory may be the image's own, such as apt's
            error="users isolation could not set up sandbox 'default': users isolation could not scan all of / for the user ids its files take, so it cannot choose ids that own nothing: find: ",
        )
    }
    _eval(
        tmp_path,
        cases,
        compose_extra="    cap_drop:\n      - DAC_OVERRIDE\n      - DAC_READ_SEARCH\n",
    )


@pytest.mark.slow
def test_tools_configured_for_another_user_and_shared_shells_are_refused(
    tmp_path: Path,
) -> None:
    cases = {
        "refusals": Case(
            runs=[
                Run(
                    {"shared": []},
                    {
                        "w-1": [("root", "id -un"), ("team", "id -un")],
                        "w-2": [("team", "id -un")],
                    },
                )
            ]
        )
    }
    _eval(tmp_path, cases)
    root, team = cases["refusals"].results["w-1"]
    assert root.startswith("SandboxIdentityError: A tool configured to run as 'root'")
    assert "swarm-w-1" in _ok(team)
    [shared] = cases["refusals"].results["w-2"]
    assert shared.startswith("SandboxIdentityError: "), shared


@pytest.mark.slow
def test_members_work_in_worktrees_of_a_shared_repository(tmp_path: Path) -> None:
    git = "git -c user.name=m -c user.email=m@example.com"

    def work(name: str, merge: str | None) -> list[Step]:
        steps: list[Step] = [
            ("bash", f"git -C /shared/repo worktree add ~/repo -b swarm/{name}"),
            (
                "bash",
                f"cd ~/repo && echo {name} > {name}.txt && git add {name}.txt && {git} commit -qm {name}",
            ),
        ]
        if merge is not None:
            steps.append(
                ("bash", f"cd ~/repo && {git} merge -q --no-edit swarm/{merge} && ls")
            )
        return steps

    cases = {
        "git": Case(
            runs=[
                Run(
                    {"shared": [{"path": "/shared", "access": "rw"}]},
                    {"w-1": work("w-1", None), "w-2": work("w-2", "w-1")},
                )
            ],
            setup=f"mkdir -p /shared/repo && cd /shared/repo && git init -q -b main && echo base > base.txt && git add base.txt && {git} commit -qm base",
        )
    }
    _eval(tmp_path, cases)
    results = cases["git"].results
    for name in ("w-1", "w-2"):
        for result in results[name][:2]:
            _ok(result)
    assert _ok(results["w-2"][2]).split() == ["base.txt", "w-1.txt", "w-2.txt"]
