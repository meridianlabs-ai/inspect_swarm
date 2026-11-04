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
    after: str | None = None
    """A root command run after the swarm, even one that failed; its output is the case's `after` result, and the swarm's error its `error` result."""


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
            if run.after is None:
                state = await as_solver(team)(state, generate)
                continue
            results = _CASES[case].results
            try:
                state = await as_solver(team)(state, generate)
            except Exception as ex:
                results["error"] = [str(ex)]
            result = await sandbox().exec(["sh", "-c", run.after], user="root")
            results["after"] = [result.stdout + result.stderr]
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
        (
            "bash",
            "id -un; id -u; pwd; echo $HOME $TMPDIR; getent group inspect-swarm | cut -d: -f3",
        ),
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
    _eval(tmp_path, cases)
    results = cases["tools"].results
    uids: set[int] = set()
    gids: set[int] = set()
    for name in ("w-1", "w-2"):
        user, home = f"swarm-{name}", f"/scratch/{name}"
        bash_out, python_out, session_out, edit_out, stat_out = results[name]
        who, uid, pwd, env, gid = _ok(bash_out).splitlines()
        assert (who, pwd, env) == (user, home, f"{home} {home}/tmp")
        uids.add(int(uid))
        gids.add(int(gid))
        assert _ok(python_out) == f"{user} {home}"
        assert f"{user}\n{home}" in _ok(session_out)
        assert _ok(edit_out) == f"File created successfully at: {home}/note.txt"
        assert _ok(stat_out) == f"{user} 644"
    assert len(uids) == 2 and len(gids) == 1
    assert not (uids | gids) & {0, 1000, 61000, 61001, 61002, 61003}


@pytest.mark.slow
def test_members_get_their_own_home_when_the_image_sets_another(
    tmp_path: Path,
) -> None:
    # the container's HOME is root's, which commands run as another user keep unless
    # the tool sets it; its working directory is root's too, which no member can enter
    steps: list[Step] = [
        ("bash", "echo $HOME $TMPDIR"),
        ("python", "import os; print(os.environ['HOME'], os.environ['TMPDIR'])"),
        ("session", "echo $HOME $TMPDIR"),
    ]
    # with a share, so that the self-test scans it, from the member's home
    rw = {"shared": [{"path": "/shared", "access": "rw"}]}
    cases = {"home": Case(runs=[Run(rw, {"w-1": steps})])}
    _eval(
        tmp_path,
        cases,
        compose_extra="    working_dir: /root\n    environment:\n      HOME: /root\n",
    )
    bash_out, python_out, session_out = cases["home"].results["w-1"]
    # nothing on stderr either, such as bash failing to read /root/.bash_profile
    assert _ok(bash_out) == "/scratch/w-1 /scratch/w-1/tmp"
    assert _ok(python_out) == "/scratch/w-1 /scratch/w-1/tmp"
    assert "/scratch/w-1 /scratch/w-1/tmp" in _ok(session_out)


@pytest.mark.slow
def test_homes_start_without_the_images_skeleton(tmp_path: Path) -> None:
    # with peers="read", a skeleton entry open to everyone would let every member
    # write it in every other member's home
    cases = {
        "skeleton": Case(
            runs=[
                Run(
                    {"shared": [], "peers": "read"},
                    {
                        "w-1": [("bash", "ls -A ~")],
                        "w-2": [("bash", "ls -A /scratch/w-1")],
                    },
                )
            ],
            setup="mkdir -p /etc/skel && echo x > /etc/skel/open-file && chmod 666 /etc/skel/open-file && mkdir -m 777 /etc/skel/open-dir",
        )
    }
    _eval(tmp_path, cases)
    for name in ("w-1", "w-2"):
        [listing] = cases["skeleton"].results[name]
        assert _ok(listing).split() == [".bashrc", ".profile", "tmp"]


@pytest.mark.slow
def test_members_cannot_reach_each_others_homes_and_share_the_shares(
    tmp_path: Path,
) -> None:
    writer: list[Step] = [
        (
            "bash",
            'echo secret > ~/secret && echo temp > "$TMPDIR/temp" && mkdir /shared/d && echo one > /shared/d/f',
        ),
        ("bash", "touch /shared/top /shared/nested/n"),
    ]
    peer: list[Step] = [
        ("bash", "cat /scratch/w-1/secret"),
        ("bash", "cat /scratch/w-1/tmp/temp"),
        ("bash", "touch /scratch/w-1/tmp/planted"),
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
        (read, read_temp, plant, write, rename, share, data, append, create, move) = (
            case.results["w-2"]
        )
        if peers == "read":
            assert _ok(read) == "secret"
            assert _ok(read_temp) == "temp"
        else:
            _refused(read)
            _refused(read_temp)
        for result in (plant, write, rename, append, create):
            _refused(result)
        assert _ok(share) == "one\ntwo"
        assert _ok(data) == "data"
        assert _ok(move) == "m"


@pytest.mark.slow
def test_files_already_in_a_share_are_kept_and_members_edit_them(
    tmp_path: Path,
) -> None:
    # a directory that was there before setup, in which a member creates and edits a file
    setup = "mkdir -p /shared/inherited && touch /shared/existing"
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
                        ],
                    },
                )
            ],
            setup=setup,
        )
    }
    _eval(tmp_path, cases)
    [edits] = cases["existing"].results["w-2"]
    assert _ok(edits).split() == ["a", "d", "b", "c", "e"]


# a command that runs `action` (by default, fails) for the arguments matching a case
# pattern, in place of the image's own, which it runs otherwise as `$real`; the probe's
# scripts find commands only in the system directories
def _failing(command: str, pattern: str, action: str = "exit 1") -> str:
    real = f"/usr/bin/{command}.real"
    return (
        f"set -e; mv /usr/bin/{command} {real}; cat > /usr/bin/{command} <<'EOF'\n"
        f"#!/bin/sh\nreal={real}\n"
        f'case "$*" in {pattern}) {action} ;; esac\n'
        'exec "$real" "$@"\n'
        f"EOF\nchmod 755 /usr/bin/{command}"
    )


# what find reports for an entry removed while it runs, in English in the C locale
# and in German otherwise, as a translated findutils would
_REMOVED = (
    '"$real" "$@" || exit; if [ "$LC_ALL" = C ]; then'
    " echo \"find: '/tmp/gone': No such file or directory\" >&2; else"
    " echo \"find: '/tmp/gone': Datei oder Verzeichnis nicht gefunden\" >&2; fi; exit 1"
)


@pytest.mark.slow
def test_the_self_test_removes_only_the_directories_it_reserved(
    tmp_path: Path,
) -> None:
    # entries with names like the self-test's, which it must leave alone whether it
    # passes, fails to create its probes, or fails to rename one
    sentinels = ["/shared/.swarm-probe-sentinel", "/shared/.swarm-probe-sentinel.moved"]
    setup = (
        f"mkdir /shared && for f in {' '.join(sentinels)}; do echo sentinel > $f; done"
    )
    after = f"find /scratch /shared -name '.swarm-probe-*' | sort && cat {' '.join(sentinels)}"
    rw = {"shared": [{"path": "/shared", "access": "rw"}]}
    idle: dict[str, list[Step]] = {"w-1": [], "w-2": []}
    cases = {
        "passes": Case(runs=[Run(rw, idle, after=after)], setup=setup),
        "creation-fails": Case(
            runs=[Run(rw, idle, before=_failing("mktemp", "*/shared/*"), after=after)],
            setup=setup,
        ),
        "rename-fails": Case(
            runs=[Run(rw, idle, before=_failing("mv", "*.moved*"), after=after)],
            setup=setup,
        ),
    }
    _eval(tmp_path, cases)
    errors = {
        "passes": None,
        "creation-fails": "the member could not create a file in the shared directory /shared",
        "rename-fails": "a member could not rename a file another member created in the shared directory /shared",
    }
    for name, error in errors.items():
        results = cases[name].results
        if error is None:
            assert "error" not in results
        else:
            [message] = results["error"]
            assert error in message, message
        assert results["after"][0].splitlines() == [*sentinels, "sentinel", "sentinel"]


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
def test_a_second_swarm_with_users_isolation_in_a_sample_is_refused(
    tmp_path: Path,
) -> None:
    rw = {"shared": [{"path": "/shared", "access": "rw"}]}
    cases = {
        "second": Case(
            runs=[
                Run(rw, {"a-1": [("bash", "echo one > /shared/f")]}),
                Run(
                    rw,
                    {"b-1": [("bash", "echo two >> /shared/f")]},
                    after="cat /shared/f && getent passwd swarm-b-1",
                ),
            ]
        )
    }
    _eval(tmp_path, cases)
    results = cases["second"].results
    [error] = results["error"]
    assert error.startswith(
        "users isolation sets up a sandbox once per sample, and another swarm with users isolation in this sample has set up sandbox 'default', or tried to."
    ), error
    # the second made no user and its member did not run
    assert results["after"] == ["one\n"]
    assert "b-1" not in results


@pytest.mark.slow
def test_an_input_mounted_read_only_is_shared_as_it_is(tmp_path: Path) -> None:
    # one volume, mounted read-write at /seed, where the sample's setup fills it, and
    # read-only at the share; root-owned, with no ACL or hard link
    ro = {"shared": [{"path": "/data", "access": "ro"}]}
    seed = "mkdir /seed/dir && echo data > /seed/dir/f && chmod 755 /seed /seed/dir"
    read = "cat /data/dir/f && stat -c '%a %U %G' /data /data/dir /data/dir/f"
    cases = {
        "safe": Case(
            runs=[
                Run(
                    ro,
                    {"w-1": [("bash", read), ("bash", "touch /data/new")], "w-2": []},
                )
            ],
            setup=f"{seed} && chmod 644 /seed/dir/f",
        ),
        # a mode setup must change, which it cannot on a read-only filesystem
        "private-file": Case(
            runs=[Run(ro, {"w-1": [], "w-2": []})],
            setup=f"{seed} && chmod 600 /seed/dir/f",
            error="users isolation could not set up sandbox 'default': users isolation could not change the modes of the files in /data",
        ),
    }
    _eval(
        tmp_path,
        cases,
        compose_extra="    volumes:\n      - input:/seed\n      - input:/data:ro\nvolumes:\n  input:\n",
    )
    read_out, create = cases["safe"].results["w-1"]
    assert _ok(read_out).splitlines() == [
        "data",
        "755 root root",
        "755 root root",
        "644 root root",
    ]
    assert "Read-only file system" in _ok(create), create


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
        # refused before setup changes anything: no share, no group
        "existing-member-group": Case(
            runs=[
                Run(
                    {"shared": [{"path": "/shared", "access": "rw"}]},
                    idle,
                    after="ls -d /shared 2>&1; getent group inspect-swarm; echo done",
                )
            ],
            setup="groupadd swarm-w-1",
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
            error=f"{refused}users isolation does not support ACLs in a shared directory: /shared/existing: user:61003:r--",
        ),
        "named-default-acl": Case(
            runs=[Run({"shared": [{"path": "/shared", "access": "rw"}]}, idle)],
            setup="mkdir -p /shared/inherited && setfacl -d -m u:61003:r-- /shared/inherited",
            error="/shared/inherited: default:user:61003:r--",
        ),
        "default-owner-acl": Case(
            runs=[Run({"shared": [{"path": "/shared", "access": "rw"}]}, idle)],
            setup="mkdir -p /shared/inherited && setfacl -d -m u::r-x /shared/inherited",
            error=f"{refused}users isolation does not support ACLs in a shared directory: /shared/inherited: default:user::r-x",
        ),
        "missing-share-under-acl": Case(
            runs=[Run({"shared": [{"path": "/work/shared", "access": "rw"}]}, idle)],
            setup="mkdir /work && setfacl -d -m u::r-x /work",
            error=f"{refused}users isolation would create /work/shared inside /work, whose ACL its new directories would inherit; remove it (setfacl -b)",
        ),
        "missing-homes-parent-under-acl": Case(
            runs=[
                Run(
                    {"home": "/work/homes/{member}", "shared": [], "peers": "read"},
                    idle,
                )
            ],
            setup="mkdir /work && setfacl -d -m u:61005:rwx /work",
            error=f"{refused}users isolation would create /work/homes inside /work, whose ACL its new directories would inherit; remove it (setfacl -b)",
        ),
        "rw-share-root-without-owner-rwx": Case(
            runs=[Run({"shared": [{"path": "/shared", "access": "rw"}]}, idle)],
            setup="mkdir -m 555 /shared",
            error=f"{refused}users isolation does not support a directory without owner rwx in a read-write shared directory: /shared is mode 555",
        ),
        "rw-share-subdirectory-without-owner-rwx": Case(
            runs=[Run({"shared": [{"path": "/shared", "access": "rw"}]}, idle)],
            setup="mkdir -p /shared/nested && chmod 555 /shared/nested",
            error=f"{refused}users isolation does not support a directory without owner rwx in a read-write shared directory: /shared/nested is mode 555",
        ),
        "hard-link-in-rw-share": Case(
            runs=[Run({"shared": [{"path": "/shared", "access": "rw"}]}, idle)],
            setup="mkdir /shared && ln /etc/profile /shared/profile",
            error=f"{refused}users isolation does not support a hard-linked file in a shared directory: /shared/profile has 2 links",
        ),
        "id-filter-fails": Case(
            runs=[Run({"shared": []}, idle, before=_failing("awk", "*lo=*"))],
            error=f"{refused}users isolation could not list the ids it found",
        ),
        "id-sort-fails": Case(
            runs=[Run({"shared": []}, idle, before=_failing("sort", "*window*"))],
            error=f"{refused}users isolation could not list the ids it found",
        ),
        # an id scan that leaves out ids, silently or when killed, and a share scan
        # that fails with a diagnostic other than a removed entry
        "id-scan-fails-silently": Case(
            runs=[
                Run(
                    {"shared": []},
                    idle,
                    before=_failing("find", "*-uid*", '"$real" "$@"; exit 1'),
                )
            ],
            error=f"{refused}users isolation could not scan all of / for the user ids its files take, so it cannot choose ids that own nothing: find exited with status 1",
        ),
        "id-scan-killed": Case(
            runs=[
                Run(
                    {"shared": []},
                    idle,
                    before=_failing("find", "*-uid*", "kill -9 $$"),
                )
            ],
            error=f"{refused}users isolation could not scan all of / for the user ids its files take, so it cannot choose ids that own nothing: find exited with status 137",
        ),
        "share-scan-fails": Case(
            runs=[
                Run(
                    {"shared": [{"path": "/shared", "access": "rw"}]},
                    idle,
                    before=_failing(
                        "find",
                        "*-links*",
                        "echo 'find: /shared/x: Input/output error' >&2; exit 1",
                    ),
                )
            ],
            setup="mkdir /shared",
            error=f"{refused}users isolation could not scan the files in /shared: find: /shared/x: Input/output error",
        ),
        "hard-link-in-ro-share": Case(
            runs=[Run({"shared": [{"path": "/data", "access": "ro"}]}, idle)],
            setup="mkdir /outside /data && touch /outside/file && chmod 666 /outside/file && ln /outside/file /data/file",
            error=f"{refused}users isolation does not support a hard-linked file in a shared directory: /data/file has 2 links",
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
    group = cases["existing-member-group"].results
    assert group["error"] == [
        "users isolation: the group swarm-w-1 already exists in sandbox 'default'. Users isolation creates each member's own group, so the image must not have groups named swarm-<member>."
    ]
    assert group["after"] == [
        "ls: cannot access '/shared': No such file or directory\ndone\n"
    ]
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
def test_files_removed_while_setup_scans_do_not_refuse_it(tmp_path: Path) -> None:
    # processes that keep creating and removing files under a scanned root, as the
    # sandbox tools' request files or a process an earlier member left running do; a
    # scan meets a removed file about one time in five, so eight samples set up with it
    churn = " ".join(
        f"nohup sh -c 'while :; do mkdir -p /tmp/churn-{d}; i=0;"
        f" while [ $i -lt 300 ]; do : > /tmp/churn-{d}/$i; i=$((i+1)); done;"
        f" rm -rf /tmp/churn-{d}; done' >/dev/null 2>&1 &"
        for d in "abcdefgh"
    )
    cases = {
        f"churn-{i}": Case(runs=[Run({"shared": []}, {"w-1": []}, before=churn)])
        for i in range(8)
    }
    # a removed entry reported in the image's own language, which setup reads in C
    cases["localized"] = Case(
        runs=[
            Run(
                {"shared": []}, {"w-1": []}, before=_failing("find", "*-uid*", _REMOVED)
            )
        ]
    )
    _eval(
        tmp_path,
        cases,
        compose_extra="    environment:\n      LANG: de_DE.UTF-8\n      LC_ALL: de_DE.UTF-8\n",
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

    # a worktree linked to the repository before setup, which a .git file marks
    linked: Step = ("bash", "git -C /shared/linked status --short --branch")
    cases = {
        "git": Case(
            runs=[
                Run(
                    {"shared": [{"path": "/shared", "access": "rw"}]},
                    {"w-1": [*work("w-1", None), linked], "w-2": work("w-2", "w-1")},
                )
            ],
            setup=f"mkdir -p /shared/repo && cd /shared/repo && git init -q -b main && echo base > base.txt && git add base.txt && {git} commit -qm base && git worktree add -q /shared/linked -b linked",
        )
    }
    _eval(tmp_path, cases)
    results = cases["git"].results
    for name in ("w-1", "w-2"):
        for result in results[name][:2]:
            _ok(result)
    assert _ok(results["w-2"][2]).split() == ["base.txt", "w-1.txt", "w-2.txt"]
    assert _ok(results["w-1"][2]).startswith("## linked")
