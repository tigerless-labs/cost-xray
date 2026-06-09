# Local dev shortcuts. CI runs the same commands (see .github/workflows/ci.yml),
# so `make ci` locally == the gate that runs on every push/PR.
.PHONY: install test lint fmt ci

install:        ## install runtime + dev deps into the active environment
	pip install -e ".[dev]"

test:           ## run the test suite
	pytest

lint:           ## static checks (ruff)
	ruff check cost_xray tests

fmt:            ## auto-fix lint issues (import order, simple rewrites)
	ruff check --fix cost_xray tests

ci: lint test   ## what CI runs: lint then test
