.PHONY: typecheck
typecheck:
	uv run pyright

.PHONY: check
check: typecheck
	uv run ruff check --fix
	uv run ruff format

.PHONY: test
test:
	uv run pytest

# The docs build, Quarto included, is in the `doc` extra, which `dev` leaves out.
.PHONY: docs
docs:
	uv sync --group dev --extra doc
	cd docs && PATH="$(CURDIR)/.venv/bin:$$PATH" quarto render
