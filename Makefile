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

# The docs need Quarto QUARTO_MIN or later on PATH (the docs extension's floor)
# and the Python tools in the `doc` group, which `dev` leaves out.
QUARTO_MIN := 1.9.36

.PHONY: docs
docs:
	@command -v quarto >/dev/null || { echo "make docs needs Quarto $(QUARTO_MIN) or later on PATH: https://quarto.org/docs/download/"; exit 1; }
	@python3 -c 'import sys; v, m = (tuple(int(p) for p in s.split(".")[:3]) for s in sys.argv[1:]); sys.exit(v < m)' "$$(quarto --version)" $(QUARTO_MIN) || { echo "make docs needs Quarto $(QUARTO_MIN) or later; the quarto on PATH is $$(quarto --version): https://quarto.org/docs/download/"; exit 1; }
	uv sync --group dev --group doc
	cd docs && PATH="$(CURDIR)/.venv/bin:$$PATH" quarto render
