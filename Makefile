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

# The docs tools are in the `doc` group, which `dev` leaves out, so install it
# first.
.PHONY: docs
docs:
	uv sync --group dev --group doc
	cd docs && PATH="$(CURDIR)/.venv/bin:$$PATH" ../.venv/bin/quarto render
