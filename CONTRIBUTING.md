# Contributing to Inspect Swarm

Thanks for your interest in contributing. Bug reports, doc fixes, and core changes are all welcome.

## Development setup

Inspect Swarm uses [uv](https://docs.astral.sh/uv/) for environment and dependency management, and requires Python 3.11+.

The development environment pins 3.11 (`.python-version`), the supported floor, so `make check` and `make test` run on it. CI runs pyright, the tests and the build on 3.11 and on 3.14, the newest version every dependency ships wheels for. To run the tests on 3.14 locally without touching `.venv`:

```bash
uv run --isolated --python 3.14 --group dev pytest
```

```bash
git clone https://github.com/meridianlabs-ai/inspect_swarm
cd inspect_swarm
uv sync --group dev
```

This installs the package in editable mode along with the dev tools (ruff, pyright, pytest).

Inspect Swarm depends on released versions of `inspect_ai`: `pyproject.toml` sets a minimum version, and `uv.lock` pins the release that development and CI use. To move to a newer inspect_ai release, run `uv lock --upgrade-package inspect-ai && uv sync --group dev`.

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

Releases are paused for now: `release.yaml` has no `push` trigger, so nothing is released until a maintainer turns it on. Once on, releases are automated with [Release Please](https://github.com/googleapis/release-please): **don't edit `CHANGELOG.md` or bump the version by hand.** Release Please reads the merged commit types, opens a release PR that updates the changelog and version, and merging that PR tags the release; the publish then runs once a maintainer approves the deployment.

Choose the type deliberately. `feat:` and `fix:` drive the version bump and headline the release notes:

| Type | Effect |
| --- | --- |
| `feat:` | a user-facing feature; bumps the minor version |
| `fix:` | a user-facing bug fix; bumps the patch version |
| `perf:`, `revert:` | appear in the release notes (no bump on their own) |
| `docs:`, `refactor:`, `chore:`, `build:`, `ci:`, `test:`, `style:` | hidden from the release notes |

Anything that isn't a user-facing feature or fix should avoid `feat:`/`fix:` so it stays out of the headline sections.

### The inspect_ai version

The `inspect-ai` dependency is a minimum version, never a git reference, so a release can go to PyPI as it is. Raise the minimum only when code in `src/` needs a newer inspect_ai, and say in the PR which code needs it. When only tests need a newer release, update `uv.lock` instead. If a change needs an inspect_ai feature that is not released yet, it waits for that release.

## Reporting issues

Use the [issue tracker](https://github.com/meridianlabs-ai/inspect_swarm/issues). For bugs, include the `inspect_swarm` and `inspect_ai` versions (`pip show inspect-swarm inspect-ai`) and a minimal repro.
