# Contributing to Inspect Swarm

Thanks for your interest in contributing. Bug reports, doc fixes, and core changes are all welcome.

## Development setup

Inspect Swarm uses [uv](https://docs.astral.sh/uv/) for environment and dependency management, and requires Python 3.10+.

The development environment pins 3.13 (`.python-version`). CI covers both ends of the supported range: 3.10 proves the floor still works, and 3.14 exercises the newest interpreter. `make check` only ever sees the 3.13 environment, so a construct that is new in 3.11 or later passes locally and fails in CI on 3.10. To reproduce the floor locally:

```bash
uv venv --python 3.10 /tmp/py310 && uv pip install --python /tmp/py310/bin/python -e . --group dev
.venv/bin/pyright --pythonpath /tmp/py310/bin/python
```

```bash
git clone https://github.com/meridianlabs-ai/inspect_swarm
cd inspect_swarm
uv sync --group dev
```

This installs the package in editable mode along with the dev tools (ruff, pyright, pytest).

Development tracks `inspect_ai` from `main`: the dependency in `pyproject.toml` is a git reference, so `uv sync` clones and builds inspect_ai's current `main` rather than installing a PyPI release. That is deliberate, because swarm and inspect_ai are expected to co-develop. To pick up new inspect_ai commits, run `uv lock --upgrade-package inspect-ai && uv sync --group dev`.

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

### The inspect_ai pin at release time

A published package cannot depend on a git reference (PyPI rejects it, and a moving ref is not reproducible), so two workflows handle the pin on the Release Please release PR:

- `release-dep-guard.yml` fails the release PR while any runtime dependency still points at a git ref.
- `release-pin-deps.yml` rewrites the `inspect-ai` git reference to `>=<latest PyPI version>`, re-locks, and pushes to the release PR. It runs on release-PR events and every two hours, because an inspect_ai release produces no event in this repository.

If the pinned release PR then fails its build or tests, swarm needs an inspect_ai change that has not shipped: cut an inspect_ai release first, and the scheduled run re-pins once it is on PyPI.

## Reporting issues

Use the [issue tracker](https://github.com/meridianlabs-ai/inspect_swarm/issues). For bugs, include the `inspect_swarm` and `inspect_ai` versions (`pip show inspect-swarm inspect-ai`) and a minimal repro.
