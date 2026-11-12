# pyproject.toml holds back releases younger than seven days, or six hours for
# a list of model provider SDKs and Inspect packages. Dependabot applies the
# seven days to its uv.lock bumps and leaves that list to uv. Development tracks
# inspect_ai main through [tool.uv.sources], and every CI job installs from
# uv.lock, except that inspect-ai moves to main's latest commit first (or, on
# the release PR, to the newest release). The build toolchain comes from a
# hashed requirements file. Testing against the newest releases happens in
# meridianlabs-ai/canaries, outside this repository.

import importlib.util
import os
import pathlib
import re
import tomllib
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
import yaml

REPO = pathlib.Path(__file__).parents[1]
GITHUB = REPO / ".github"
WORKFLOWS = GITHUB / "workflows"
UV_SYNC = GITHUB / "actions" / "uv-sync" / "action.yml"
UV_SYNC_ACTION = "./.github/actions/uv-sync"
SHARED_ACTIONS = "meridianlabs-ai/actions/"
RELEASE_BRANCH = "release-please--branches--main"
INSPECT_AI_GIT = "https://github.com/UKGovernmentBEIS/inspect_ai"
MATRIX_PYTHON = "${{ matrix.python-version }}"
LOCKED_JOBS = {
    "py-ruff": {"python-version": "3.11", "project-install": "skip", "extras": "dev"},
    "py-pyright": {"python-version": MATRIX_PYTHON, "extras": "dev"},
    "py-test": {"python-version": MATRIX_PYTHON, "extras": "dev"},
    "py-build": {
        "python-version": MATRIX_PYTHON,
        "project-install": "skip",
        "default-dependency-groups": "exclude",
        "also-install-without-deps": "${{ steps.build.outputs.wheel }}",
    },
}
BUILD_REQUIREMENTS = "scripts/build-dist/requirements.txt"
SHARED_BUILD = SHARED_ACTIONS + ".github/workflows/build-python-dist.yml@"
PUBLISH_STEPS = ("actions/download-artifact@", "pypa/gh-action-pypi-publish@")
DOWNLOAD_INPUTS = {
    "artifact-ids": "${{ needs.build.outputs.artifact-id }}",
    "path": "dist/",
}
EXEMPT_COOLDOWN = "6 hours"
# CI moves it to inspect_ai main, so Dependabot leaves it alone.
MAIN_TRACKED = "inspect-ai"

spec = importlib.util.spec_from_file_location(
    "inspect_ai_floor", GITHUB / "scripts" / "inspect_ai_floor.py"
)
assert spec and spec.loader
inspect_ai_floor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inspect_ai_floor)


def pyproject() -> dict[str, Any]:
    return tomllib.loads((REPO / "pyproject.toml").read_text())


def uv_config() -> dict[str, Any]:
    config: dict[str, Any] = pyproject()["tool"]["uv"]
    return config


def normalize(package: str) -> str:
    return re.sub(r"[-_.]+", "-", package).lower()


def direct_dependencies() -> set[str]:
    project = pyproject()["project"]
    requirements = [
        *project["dependencies"],
        *(r for extra in project["optional-dependencies"].values() for r in extra),
    ]
    return {
        normalize(re.split(r"[^A-Za-z0-9._-]", requirement, maxsplit=1)[0])
        for requirement in requirements
    }


def cooldown_days(exclude_newer: str) -> int:
    days = re.fullmatch(r"(\d+) days", exclude_newer)
    assert days, exclude_newer
    return int(days[1])


def dependabot_uv() -> dict[str, Any]:
    config = yaml.safe_load((GITHUB / "dependabot.yml").read_text())
    root = [
        u
        for u in config["updates"]
        if u["package-ecosystem"] == "uv" and u["directory"] == "/"
    ]
    assert len(root) == 1, "expected one uv entry for directory / in dependabot.yml"
    entry: dict[str, Any] = root[0]
    return entry


def workflows() -> list[pathlib.Path]:
    return sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])


def local_actions() -> list[pathlib.Path]:
    return sorted((GITHUB / "actions").glob("*/action.y*ml"))


def run_lines(job: dict[str, Any]) -> list[str]:
    # Joins backslash continuations so a command's flags stay on one line.
    return [
        line.strip()
        for step in job.get("steps", [])
        for line in step.get("run", "").replace("\\\n", " ").splitlines()
        if line.strip()
    ]


def jobs() -> list[tuple[str, dict[str, Any], Any]]:
    return [
        (
            f"{path.name}:{name}",
            job,
            job.get("permissions", workflow.get("permissions")),
        )
        for path in workflows()
        for workflow in [yaml.safe_load(path.read_text())]
        for name, job in workflow["jobs"].items()
    ]


def all_steps() -> list[tuple[str, dict[str, Any]]]:
    return [
        *(
            (f"{path.name}:{name}", step)
            for path in workflows()
            for name, job in yaml.safe_load(path.read_text())["jobs"].items()
            for step in job.get("steps", [])
        ),
        *(
            (path.parent.name, step)
            for path in local_actions()
            for step in yaml.safe_load(path.read_text())["runs"].get("steps", [])
        ),
    ]


def workflow_run_lines() -> list[tuple[str, str]]:
    return [
        *(
            (f"{path.name}:{name}", line)
            for path in workflows()
            for name, job in yaml.safe_load(path.read_text())["jobs"].items()
            for line in run_lines(job)
        ),
        *(
            (path.parent.name, line)
            for path in local_actions()
            for line in run_lines(yaml.safe_load(path.read_text())["runs"])
        ),
    ]


def verify_exclude_newer_package(
    config: dict[str, Any], direct: set[str], now: datetime
) -> None:
    # Each entry is "6 hours" or a dated entry younger than the cooldown.
    max_age = timedelta(days=cooldown_days(config["exclude-newer"]))
    for package, cooldown in config["exclude-newer-package"].items():
        assert normalize(package) in direct, f"{package} is not a direct dependency"
        if cooldown != EXEMPT_COOLDOWN:
            age = now - datetime.fromisoformat(cooldown)
            assert timedelta(0) <= age < max_age, (
                f'dated entry {package} = "{cooldown}" is in the future or stale'
            )


def verify_dependabot_cooldown(
    config: dict[str, Any], dependabot: dict[str, Any]
) -> None:
    cooldown = dependabot["cooldown"]
    assert cooldown["default-days"] == cooldown_days(config["exclude-newer"])
    assert dependabot["ignore"] == [{"dependency-name": MAIN_TRACKED}]
    # Dependabot's cooldown counts whole days, so it leaves the "6 hours"
    # entries to uv. Dated entries are locked by hand.
    exempt = sorted(
        package
        for package, cooldown in config["exclude-newer-package"].items()
        if cooldown == EXEMPT_COOLDOWN and package != MAIN_TRACKED
    )
    assert sorted(cooldown.get("exclude", [])) == exempt
    # The exempt packages have their own group, listed before the catch-all.
    groups = dependabot["groups"]
    assert list(groups) == [
        *(["six-hour-cooldown"] if exempt else []),
        "python-packages",
    ]
    if exempt:
        assert sorted(groups["six-hour-cooldown"]["patterns"]) == exempt
    assert groups["python-packages"]["patterns"] == ["*"]


def test_exclude_newer_package_entries() -> None:
    verify_exclude_newer_package(uv_config(), direct_dependencies(), datetime.now(UTC))


def test_dependabot_cooldown_matches_pyproject() -> None:
    verify_dependabot_cooldown(uv_config(), dependabot_uv())


def test_fresh_dated_entry_passes() -> None:
    config = uv_config()
    now = datetime.now(UTC)
    config["exclude-newer-package"]["pydantic"] = (now - timedelta(days=6)).isoformat()
    verify_exclude_newer_package(config, direct_dependencies(), now)
    verify_dependabot_cooldown(config, dependabot_uv())


@pytest.mark.parametrize(
    "package,cooldown,error",
    [
        ("certifi", EXEMPT_COOLDOWN, "not a direct dependency"),
        ("pydantic", timedelta(days=-7), "dated entry"),
        ("pydantic", timedelta(days=1), "dated entry"),
    ],
)
def test_bad_exclude_newer_package_entry_is_caught(
    package: str, cooldown: str | timedelta, error: str
) -> None:
    config = uv_config()
    now = datetime.now(UTC)
    config["exclude-newer-package"][package] = (
        (now + cooldown).isoformat() if isinstance(cooldown, timedelta) else cooldown
    )
    with pytest.raises(AssertionError, match=error):
        verify_exclude_newer_package(config, direct_dependencies(), now)


def test_other_cooldown_value_is_caught() -> None:
    config = uv_config()
    config["exclude-newer-package"]["pydantic"] = "1 day"
    with pytest.raises(ValueError):
        verify_exclude_newer_package(config, direct_dependencies(), datetime.now(UTC))


def test_inspect_ai_is_a_floor_tracked_from_main_through_sources() -> None:
    # PyPI rejects direct references, and only uv reads [tool.uv.sources].
    inspect_ai = [
        d
        for d in pyproject()["project"]["dependencies"]
        if normalize(re.split(r"[^A-Za-z0-9._-]", d, maxsplit=1)[0]) == MAIN_TRACKED
    ]
    assert len(inspect_ai) == 1 and re.fullmatch(r"inspect-ai>=[\d.]+", inspect_ai[0])
    assert uv_config()["sources"] == {
        MAIN_TRACKED: {"git": INSPECT_AI_GIT, "branch": "main"}
    }
    # The committed lock comes from the source, not a PEP 508 git reference.
    (locked,) = [
        p
        for p in tomllib.loads((REPO / "uv.lock").read_text())["package"]
        if p["name"] == MAIN_TRACKED
    ]
    assert locked["source"]["git"].startswith(f"{INSPECT_AI_GIT}?branch=main#")


def test_uv_comes_from_the_shared_setup_uv() -> None:
    # meridianlabs-ai/actions/setup-uv holds the uv pin. One commit for every
    # shared action means one uv.
    uses = [(where, step["uses"]) for where, step in all_steps() if "uses" in step]
    assert [u for u in uses if u[1].startswith("astral-sh/setup-uv@")] == []
    refs = {
        ref.split("@")[1].split()[0]
        for _, ref in uses
        if ref.startswith(
            (
                SHARED_ACTIONS + "setup-uv@",
                SHARED_ACTIONS + "install-python-dependencies@",
            )
        )
    }
    assert len(refs) == 1, refs
    assert re.fullmatch(r"[0-9a-f]{40}", refs.pop())


@pytest.mark.parametrize("name,inputs", LOCKED_JOBS.items())
def test_locked_jobs_install_from_the_lockfile(
    name: str, inputs: dict[str, str]
) -> None:
    job = yaml.safe_load((WORKFLOWS / "build.yaml").read_text())["jobs"][name]
    installs = [
        step.get("with", {})
        for step in job["steps"]
        if step.get("uses") == UV_SYNC_ACTION
    ]
    assert installs == [inputs]
    assert not [line for line in run_lines(job) if line.startswith("uv sync")]


def test_only_uv_sync_installs_from_the_lockfile() -> None:
    # Outside uv-sync, a locked install would skip moving inspect-ai to main.
    locked = [
        where
        for where, step in all_steps()
        if step.get("uses", "").startswith(SHARED_ACTIONS)
        and step.get("with", {}).get("versions") == "locked"
    ]
    assert locked == ["uv-sync"]


def test_uv_sync_action_moves_only_inspect_ai() -> None:
    # Moving inspect-ai re-locks, which would also absorb a pyproject.toml
    # change nobody locked, so the committed lock is checked first. The
    # release PR resolves without sources, and later steps see the committed
    # uv.lock.
    action = yaml.safe_load(UV_SYNC.read_text())
    setup, move, install, restore = action["runs"]["steps"]
    assert setup["uses"].startswith(SHARED_ACTIONS + "setup-uv@")
    # A job that installed through the shared action first already ran setup-uv.
    assert setup["if"] == "env.MERIDIAN_SETUP_UV == ''"
    assert setup["with"] == {"python-version": "${{ inputs.python-version }}"}
    assert move["env"]["HEAD_REF"] == "${{ github.head_ref }}"
    assert run_lines({"steps": [move]}) == [
        "uv lock --check",
        f'if [ "$HEAD_REF" = "{RELEASE_BRANCH}" ]; then',
        "export UV_NO_SOURCES=1",
        'echo "UV_NO_SOURCES=1" >> "$GITHUB_ENV"',
        "fi",
        "uv lock --upgrade-package inspect-ai",
    ]
    assert install["uses"].startswith(SHARED_ACTIONS + "install-python-dependencies@")
    assert install["with"] == {
        "versions": "locked",
        "python-version": "${{ inputs.python-version }}",
        "extras": "${{ inputs.extras }}",
        "project-install": "${{ inputs.project-install }}",
        "default-dependency-groups": "${{ inputs.default-dependency-groups }}",
        "also-install-without-deps": "${{ inputs.also-install-without-deps }}",
    }
    assert run_lines({"steps": [restore]}) == ["git checkout -- uv.lock"]


def test_release_branch_name_matches_everywhere() -> None:
    # A misspelt name silently runs the release PR as a dev PR.
    names = {
        where: set(re.findall(r"release-please--branches--[\w-]+", text))
        for where, text in [
            *[(path.name, path.read_text()) for path in workflows()],
            ("uv-sync", UV_SYNC.read_text()),
        ]
    }
    assert set().union(*names.values()) == {RELEASE_BRANCH}
    assert names["uv-sync"] and names["release-pin-deps.yml"]


def test_push_floor_pushes_only_the_release_branch_the_floor_job_read() -> None:
    # The floor job runs `uv lock`, which can run dependency code and change
    # the commit it reports. So the job holding the App token checks out the
    # release branch by name and stops unless it is that commit.
    workflow = yaml.safe_load((WORKFLOWS / "release-pin-deps.yml").read_text())
    floor_steps = workflow["jobs"]["floor"]["steps"]
    (floor,) = [step for step in floor_steps if step.get("id") == "floor"]
    floor_lines = run_lines({"steps": [floor]})
    sha = floor_lines.index('echo "sha=$(git rev-parse HEAD)" >> "$GITHUB_OUTPUT"')
    assert sha < next(
        i for i, line in enumerate(floor_lines) if "inspect_ai_floor.py" in line
    )
    steps = workflow["jobs"]["push-floor"]["steps"]
    (checkout,) = [
        s for s in steps if s.get("uses", "").startswith("actions/checkout@")
    ]
    assert checkout["with"]["ref"] == "${{ env.RELEASE_BRANCH }}"
    assert workflow["env"]["RELEASE_BRANCH"] == RELEASE_BRANCH
    check = steps[steps.index(checkout) + 1]
    assert check["env"] == {"FLOOR_SHA": "${{ needs.floor.outputs.sha }}"}
    assert run_lines({"steps": [check]})[0] == (
        'if [ "$(git rev-parse HEAD)" != "$FLOOR_SHA" ]; then'
    )


# A bare `uv run` or `uv sync` re-resolves an out-of-date uv.lock and installs
# the result. `uv run --no-project` ignores the project and uv.lock. `--with`
# installs unlocked packages whatever the other flags.
def test_uv_never_relocks() -> None:
    unlocked = [
        (where, line)
        for where, line in workflow_run_lines()
        if re.search(r"\buv (run|sync)\b", line)
        and (
            "--with" in line
            or not {"--locked", "--no-sync", "--no-project"} & set(line.split())
        )
    ]
    assert unlocked == []


# The build toolchain comes from the hashed file, as wheels, and the build
# resolves nothing more. The release build runs the same way in the shared
# workflow.
def test_builds_use_the_hashed_toolchain_without_isolation() -> None:
    hashed = [
        step["with"]
        for _, step in all_steps()
        if step.get("with", {}).get("versions") == "hashed-requirements"
    ]
    assert len(hashed) == 1
    for inputs in hashed:
        assert inputs["requirements-file"] == BUILD_REQUIREMENTS
        assert inputs["package-format"] == "wheels-only"
    builds = [line for _, line in workflow_run_lines() if " -m build" in line]
    assert len(builds) == 1
    assert [line for line in builds if "--no-isolation" not in line.split()] == []
    shared = [
        where
        for where, job, _ in jobs()
        if job.get("uses", "").startswith(SHARED_BUILD)
    ]
    assert shared == ["publish.yaml:build"]


# The build runs in the shared workflow, in a job with no publishing
# credential. A job with `id-token: write` only downloads that build and
# publishes it. A job that calls a shared workflow, such as a docs deploy, has
# no steps here.
def test_publish_jobs_only_upload_the_shared_build() -> None:
    publishers = [
        (where, job)
        for where, job, permissions in jobs()
        if isinstance(permissions, dict)
        and cast(dict[str, str], permissions).get("id-token") == "write"
        and "uses" not in job
    ]
    assert publishers, "no job holds id-token: write"
    wrong = [
        where
        for where, job in publishers
        if len(job["steps"]) != len(PUBLISH_STEPS)
        or not all(
            step.get("uses", "").startswith(prefix)
            for step, prefix in zip(job["steps"], PUBLISH_STEPS, strict=True)
        )
    ]
    assert wrong == [], f"only download the dist and publish it in {wrong}"
    unlinked = [
        where
        for where, job in publishers
        for needs in [job.get("needs", [])]
        if "build" not in ([needs] if isinstance(needs, str) else needs)
        or job["steps"][0].get("with") != DOWNLOAD_INPUTS
    ]
    assert unlinked == [], f"download only the shared build artifact in {unlinked}"
    build = yaml.safe_load((WORKFLOWS / "publish.yaml").read_text())["jobs"]["build"]
    assert build["uses"].startswith(SHARED_BUILD), build["uses"]
    ref = build["uses"].split("@")[1].split()[0]
    assert re.fullmatch(r"[0-9a-f]{40}", ref), "pin a 40-character SHA"


def test_no_workflow_opts_out_of_the_cooldown() -> None:
    floating = [
        (where, line)
        for where, line in workflow_run_lines()
        if re.search(r"--exclude-newer(=|\s+)false", line)
    ]
    assert floating == []


GIT_LOCK = f"""
[[package]]
name = "inspect-ai"
version = "0.3.288.dev42"
source = {{ git = "{INSPECT_AI_GIT}?branch=main#abc" }}
"""
RELEASED_LOCK = """
[[package]]
name = "inspect-ai"
version = "0.3.287"
source = { registry = "https://pypi.org/simple" }
"""


def test_floor_script_requires_a_released_inspect_ai() -> None:
    with pytest.raises(ValueError, match="not locked from PyPI"):
        inspect_ai_floor.locked_inspect_ai(GIT_LOCK)
    assert inspect_ai_floor.locked_inspect_ai(RELEASED_LOCK) == "0.3.287"


@pytest.mark.skipif(os.name != "posix", reason="the fake uv is a shell script")
def test_floor_script_locks_without_sources_and_restores_the_lock(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A fake uv that relocks inspect-ai from PyPI only when sources are off.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / "released.lock").write_text(RELEASED_LOCK)
    uv = bin_dir / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        '[ "$*" = "lock --no-sources --upgrade-package inspect-ai" ] || exit 1\n'
        "cp released.lock uv.lock\n"
    )
    uv.chmod(0o755)
    (tmp_path / "uv.lock").write_text(GIT_LOCK)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    inspect_ai_floor.main()
    assert capsys.readouterr().out == "0.3.287\n"
    assert (tmp_path / "uv.lock").read_text() == GIT_LOCK
