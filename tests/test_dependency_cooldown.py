# pyproject.toml holds back releases younger than seven days, six hours for a
# list of model provider SDKs, and no time for the in-house packages, which come
# from PyPI only. Dependabot applies the seven days to its uv.lock bumps and
# leaves that list to uv. Every CI job installs exactly uv.lock. The build
# toolchain comes from a hashed requirements file. Testing against the newest
# releases happens in meridianlabs-ai/canaries, outside this repository.

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
SHARED_ACTIONS = "meridianlabs-ai/actions/"
INSTALL_ACTION = SHARED_ACTIONS + "install-python-dependencies@"
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
# `false`: Meridian Labs publishes the in-house packages, so they have no cooldown.
IN_HOUSE_COOLDOWN = False
IN_HOUSE = re.compile(r"inspect-.*|petri.*")


def pyproject() -> dict[str, Any]:
    return tomllib.loads((REPO / "pyproject.toml").read_text())


def uv_config() -> dict[str, Any]:
    config: dict[str, Any] = pyproject()["tool"]["uv"]
    return config


def normalize(package: str) -> str:
    return re.sub(r"[-_.]+", "-", package).lower()


def requirement_name(requirement: str) -> str:
    return normalize(re.split(r"[^A-Za-z0-9._-]", requirement, maxsplit=1)[0])


def direct_dependencies() -> set[str]:
    project = pyproject()["project"]
    requirements = [
        *project["dependencies"],
        *(r for extra in project["optional-dependencies"].values() for r in extra),
    ]
    return {requirement_name(requirement) for requirement in requirements}


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
    # Each in-house package has no cooldown. Each other entry is "6 hours" or a
    # dated entry younger than the cooldown.
    max_age = timedelta(days=cooldown_days(config["exclude-newer"]))
    entries = config["exclude-newer-package"]
    for package in sorted(p for p in direct if IN_HOUSE.fullmatch(p)):
        assert entries.get(package) is IN_HOUSE_COOLDOWN, (
            f"in-house {package} needs {package} = false"
        )
    for package, cooldown in entries.items():
        assert normalize(package) in direct, f"{package} is not a direct dependency"
        if cooldown is IN_HOUSE_COOLDOWN:
            assert IN_HOUSE.fullmatch(normalize(package)), (
                f"{package} = false, but it is not an in-house package"
            )
        elif cooldown != EXEMPT_COOLDOWN:
            age = now - datetime.fromisoformat(cooldown)
            assert timedelta(0) <= age < max_age, (
                f'dated entry {package} = "{cooldown}" is in the future or stale'
            )


def verify_dependabot_cooldown(
    config: dict[str, Any], dependabot: dict[str, Any]
) -> None:
    cooldown = dependabot["cooldown"]
    assert cooldown["default-days"] == cooldown_days(config["exclude-newer"])
    assert "ignore" not in dependabot
    # Dependabot's cooldown counts whole days, so it leaves the "6 hours" and
    # `false` entries to uv. Dated entries are locked by hand.
    exempt = sorted(
        package
        for package, cooldown in config["exclude-newer-package"].items()
        if cooldown in (EXEMPT_COOLDOWN, IN_HOUSE_COOLDOWN)
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
        ("pydantic", IN_HOUSE_COOLDOWN, "not an in-house package"),
        ("inspect-ai", EXEMPT_COOLDOWN, "needs inspect-ai = false"),
    ],
)
def test_bad_exclude_newer_package_entry_is_caught(
    package: str, cooldown: str | bool | timedelta, error: str
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


def test_in_house_packages_come_from_pypi() -> None:
    # No git or path source may merge for an in-house package: each is a PyPI
    # floor, locked from the registry.
    dependencies = pyproject()["project"]["dependencies"]
    in_house = sorted(
        d for d in dependencies if IN_HOUSE.fullmatch(requirement_name(d))
    )
    assert (
        in_house
        and [d for d in in_house if not re.fullmatch(r"[\w-]+>=[\d.]+", d)] == []
    )
    sources = uv_config().get("sources", {})
    assert [p for p in sources if IN_HOUSE.fullmatch(normalize(p))] == []
    locked = {
        p["name"]: p["source"]
        for p in tomllib.loads((REPO / "uv.lock").read_text())["package"]
    }
    assert [
        (name, locked[name])
        for name in map(requirement_name, in_house)
        if "registry" not in locked[name]
    ] == []


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
                INSTALL_ACTION,
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
        if step.get("uses", "").startswith(INSTALL_ACTION)
        and step["with"]["versions"] == "locked"
    ]
    assert installs == [{"versions": "locked", **inputs}]
    assert not [line for line in run_lines(job) if line.startswith("uv sync")]


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


# The docs deploy waits for the PyPI publish. Its id-token is for the shared
# workflow's Pages deploy, and it is the only job besides publish that holds one.
def test_docs_job_runs_only_the_shared_docs_workflow() -> None:
    holders = [
        where
        for where, _, permissions in jobs()
        if isinstance(permissions, dict)
        and cast(dict[str, str], permissions).get("id-token") == "write"
    ]
    assert holders == ["publish.yaml:publish", "publish.yaml:docs"]
    docs = yaml.safe_load((WORKFLOWS / "publish.yaml").read_text())["jobs"]["docs"]
    assert docs["permissions"] == {
        "contents": "read",
        "pages": "write",
        "id-token": "write",
    }
    assert docs["needs"] == "publish"
    assert docs["if"] == "${{ github.event_name == 'release' }}"
    workflow, ref = docs["uses"].split("@")
    assert workflow == "meridianlabs-ai/actions/.github/workflows/publish-docs.yml"
    assert re.fullmatch(r"[0-9a-f]{40}", ref.split()[0]), "pin a 40-character SHA"
    assert docs["with"]["extras"] == "doc"


def test_no_workflow_opts_out_of_the_cooldown() -> None:
    floating = [
        (where, line)
        for where, line in workflow_run_lines()
        if re.search(r"--exclude-newer(=|\s+)false", line)
    ]
    assert floating == []
