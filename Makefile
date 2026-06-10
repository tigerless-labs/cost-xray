.PHONY: install test lint fmt ci refresh-pricing

install:        ## install runtime + dev deps into the active environment
	pip install -e ".[dev]"

test:           ## run the test suite
	pytest

lint:           ## static checks (ruff)
	ruff check cost_xray tests

fmt:            ## auto-fix lint issues (import order, simple rewrites)
	ruff check --fix cost_xray tests

ci: lint test   ## what CI runs: lint then test

refresh-pricing:  ## refresh the bundled LiteLLM price snapshot
	python scripts/refresh_pricing.py
