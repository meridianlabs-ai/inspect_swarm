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
uv sync --locked --extra dev
```

This needs uv 0.13.4 or later. It installs the package in editable mode along with the dev tools (ruff, pyright, pytest). `--locked` installs exactly the versions in `uv.lock` and fails if `uv.lock` is out of date with `pyproject.toml`.

### Dependencies

[DEPENDENCIES.md](https://github.com/meridianlabs-ai/actions/blob/main/DEPENDENCIES.md) in meridianlabs-ai/actions tells how the Inspect repositories install and change dependencies. It covers the lock file, the cooldown, the canaries, inspect_ai `main`, the release build tools, and the procedures to add or update a dependency. Follow it. It is the same for every Inspect repository, so this file does not repeat it.

For this repository:

- inspect_swarm needs unreleased inspect_ai changes. `[tool.uv.sources]` in `pyproject.toml` points `inspect-ai` at inspect_ai `main`.
- The 6-hour packages are in `[tool.uv.exclude-newer-package]` in `pyproject.toml`.
- When uv builds the project itself, for example for the editable install in `uv sync`, it resolves the build backend, still within the cooldown.

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
