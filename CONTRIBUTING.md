# Contributing to Inspect Swarm

Thanks for your interest in contributing. Bug reports, doc fixes, and core changes are all welcome.

## Development setup

Inspect Swarm uses [uv](https://docs.astral.sh/uv/) for environment and dependency management, and requires Python 3.11+.

The development environment pins 3.11 (`.python-version`), the supported floor, so `make check` and `make test` run on it. CI runs pyright, the tests and the build on 3.11 and on 3.14, the newest version every dependency ships wheels for. To run the tests on 3.14 locally without touching `.venv`:

```bash
uv run --isolated --python 3.14 --extra dev pytest
```

```bash
git clone https://github.com/meridianlabs-ai/inspect_swarm
cd inspect_swarm
uv sync --extra dev
```

This needs uv 0.13.4 or later. It installs the package in editable mode along with the dev tools (ruff, pyright, pytest).

### inspect_ai main

Development and CI track inspect_ai's `main` branch. `[tool.uv.sources]` in `pyproject.toml` points `inspect-ai` at its git repository, and `uv.lock` records a commit. Only uv reads `[tool.uv.sources]`: the published package requires the `inspect-ai>=` floor in `[project]`.

- CI moves inspect-ai to the latest `main` commit before every install (`.github/actions/uv-sync`), so it always tests against inspect_ai main. Every other package stays at its locked version.
- `uv sync --extra dev` installs the commit in `uv.lock`. To move to the latest `main` commit, run `uv sync --upgrade-package inspect-ai --extra dev`. Committing the updated `uv.lock` is optional.
- On the release PR, CI ignores the git source (`UV_NO_SOURCES=1`) and installs the newest inspect-ai release that has cleared its cooldown, so the release is tested against the inspect_ai users will get. `.github/workflows/release-pin-deps.yml` raises the floor to that release. Main keeps the git source. If Inspect Swarm needs an inspect_ai feature no release has yet, the release PR's checks fail, which is the signal to cut an inspect_ai release first.

To work against a local inspect_ai checkout, install it over the locked one:

```bash
uv sync --extra dev
uv pip install -e ../inspect_ai
```

Then run commands with `.venv/bin/<command>` or `uv run --no-sync <command>`. A plain `uv run` or `uv sync` reinstalls the locked inspect_ai.

### Dependencies and the cooldown

`uv.lock` pins every Python dependency except the build backend (hatchling, hatch-vcs). CI's type-check, test and build jobs install their dependencies from it, and fail when `uv.lock` is out of date with `pyproject.toml`. CI's build job and the release build use the build toolchain pinned with hashes in `scripts/build-dist/requirements.txt`. When uv builds the project itself, for example for the editable install in `uv sync`, it resolves the build backend, still within the cooldown. When you change dependencies in `pyproject.toml`, run `uv lock` and commit both files.

`pyproject.toml` sets `exclude-newer = "7 days"` under `[tool.uv]`, so `uv lock`, `uv sync` and `uv pip` only pick releases that have been on PyPI for at least seven days. A compromised release is usually found and removed within that time, before it reaches `uv.lock`, CI or your machine. Only a direct dependency that is a model provider SDK or an Inspect package gets a 6-hour cooldown instead, under `[tool.uv.exclude-newer-package]`; today that is only `inspect-ai`, and no direct dependency is a provider SDK. The 6 hours apply to inspect-ai releases, which the release PR installs. The inspect_ai `main` commit that development and CI use is outside the cooldown, because `exclude-newer` applies only to releases on an index. The dependencies of inspect-ai keep the cooldown.

Dependabot proposes `uv.lock` updates daily, once a release has cleared its cooldown. It ignores `inspect-ai`, which CI moves to inspect_ai `main` on every run. To adopt a release sooner, run `uv lock --upgrade-package <package>` and commit `uv.lock`. A release still inside its cooldown can't be locked; wait for it rather than overriding `exclude-newer`, unless it's an emergency (below).

In an emergency, for example a security fix, you can adopt a release that is less than seven days old with a one-time dated entry:

1. Under `[tool.uv.exclude-newer-package]` in `pyproject.toml`, add `<package> = "<timestamp>"`. The package must be a direct dependency. Use an RFC 3339 timestamp just after the upload time of the fixed release. Don't add a dated entry to `.github/dependabot.yml`.
2. Run `uv lock --upgrade-package <package>` and commit both files.
3. Within seven days, once the normal cooldown includes the release, delete the entry, run `uv lock --upgrade-package <package>`, and commit both files. A plain `uv lock` doesn't reliably remove the entry from `uv.lock`, and CI then fails. No test checks the age of the entry, so you must remove it yourself.

The nightly canary in [meridianlabs-ai/canaries](https://github.com/meridianlabs-ai/canaries) tests against the newest release of every dependency. Inspect Swarm's own CI does not need to.

## Checks and tests

Before opening a PR, make sure these pass:

```bash
make check   # pyright + ruff check --fix + ruff format
make test    # pytest
```

To run a single test:

```bash
uv run pytest tests/path/to/test.py::test_name -v
```

## Documentation

The documentation site's source is in `docs/`. Building it needs [Quarto](https://quarto.org/docs/download/) 1.9.36 or later on your `PATH`; `make docs` checks the version, installs the `doc` dependency group and renders the site into `docs/_site`. CI does not build the docs. To publish the site to GitHub Pages, after `make docs` has installed the `doc` group:

```bash
cd docs && PATH="$PWD/../.venv/bin:$PATH" quarto publish gh-pages
```

## Code style

- Type-annotate all functions (including tests). Use modern syntax (`X | None`, `list[str]`).
- Google-style docstrings for public APIs.
- `ruff` handles formatting and import order; don't fight it.
- Don't catch exceptions defensively — let unexpected errors propagate.

See [`AGENTS.md`](AGENTS.md) for the full conventions used in this repo.

## Pull requests

- Branch from `main`; we squash-merge.
- Keep PRs focused on one change.
- Add or update tests for behavior changes.

## Commit messages and releases

We use [Conventional Commits](https://www.conventionalcommits.org/). Because we squash-merge, **the PR title becomes the commit message**, so the title is what matters. Format it as `<type>: <description>`; `pr-title-lint` enforces it.

Releases are automated with [Release Please](https://github.com/googleapis/release-please): **don't edit `CHANGELOG.md` or bump the version by hand.** Release Please reads the merged commit types, opens a release PR that updates the changelog and version, and merging that PR tags the release; the publish then runs once a maintainer approves the deployment.

Choose the type deliberately. `feat:` and `fix:` drive the version bump and headline the release notes:

| Type | Effect |
| --- | --- |
| `feat:` | a user-facing feature; bumps the minor version |
| `fix:` | a user-facing bug fix; bumps the patch version |
| `perf:`, `revert:` | appear in the release notes (no bump on their own) |
| `docs:`, `refactor:`, `chore:`, `build:`, `ci:`, `test:`, `style:` | hidden from the release notes |

Anything that isn't a user-facing feature or fix should avoid `feat:`/`fix:` so it stays out of the headline sections.


## Reporting issues

Use the [issue tracker](https://github.com/meridianlabs-ai/inspect_swarm/issues). For bugs, include the `inspect_swarm` and `inspect_ai` versions (`pip show inspect-swarm inspect-ai`) and a minimal repro.
