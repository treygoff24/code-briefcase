PYTHON ?= $(shell if [ -x ./.venv/bin/python ]; then echo ./.venv/bin/python; elif command -v python3 >/dev/null 2>&1; then command -v python3; else command -v python; fi)
PYTHON_TARGETS := code_briefcase tests scripts

.PHONY: format format-check lint typecheck test quickcheck check install-hooks

format:
	$(PYTHON) -m black $(PYTHON_TARGETS)

format-check:
	$(PYTHON) -m black --check $(PYTHON_TARGETS)

lint:
	$(PYTHON) -m ruff check $(PYTHON_TARGETS)

typecheck:
	$(PYTHON) -m mypy $(PYTHON_TARGETS)

test:
	$(PYTHON) -m pytest

quickcheck:
	./scripts/check.sh --quick

check:
	./scripts/check.sh --full

install-hooks:
	git config core.hooksPath .githooks
	@echo "Git hooks installed from .githooks"
