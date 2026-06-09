# Contributing to cost-xray

Thanks for helping. This guide is the contract for changes; the architecture is mapped in
[docs/architecture.md](docs/architecture.md) — read it before a non-trivial PR.

## Dev setup

```bash
python3 -m venv .venv && source .venv/bin/activate
make install        # runtime + dev deps (pip install -e ".[dev]")
make ci             # lint then test — the exact gate CI runs
```

No install is strictly required to run the suite from a checkout (pytest/ruff read config
from `pyproject.toml` with the repo root on the path); installing just pulls the deps.

## Code style

- **Decouple, never duplicate.** One functional block → one object → one file. Extract and reuse
  the shared piece (DRY).
- **The shared layer never branches on agent.** Anything agent-specific lives in its
  [adapter](docs/architecture.md) — that is the only place code forks by agent. Adding an
  agent is one small module.
- Lint with `ruff` (`make lint` / `make fmt`); config is in `pyproject.toml`.

## Invariants you must not break

These are load-bearing (the full architecture map is [docs/architecture.md](docs/architecture.md)):

1. **The proxy never tokenizes or derives.** All tokenization/calibration runs in a separate
   consumer process, never in the live-relay hot path.
2. **Redaction before disk.** The proxy strips `authorization` / `x-api-key` before anything
   is written; the read layer reads credentials from the user's own config, never from capture.
3. **Calibrate once, at derive, per event.** Summary and TUI only sum and group — they never
   recalibrate.

## Tests in depth — reconciliation

The cost layer rests on one idea: **wire `usage` is ground truth for totals; tiktoken event
sizes are only proportions.** Calibration scales every event to the wire total, then cuts at the
cache boundary, so every roll-up (per-source, per-tool, per-MCP, the bill) equals the wire
exactly — the only approximation left is the split between sibling leaves. `tests/test_reconcile.py`
pins these conservation laws; a failure there is a real bug, not a tolerance to widen.

## Pull requests

- Run `make ci` before opening the PR — green lint + tests across the Python matrix.
- Keep the change scoped; one concern per PR.
- If you discover a follow-up you're not doing now, note it in the PR rather than silently
  leaving a gap.

## Reporting bugs / security

Functional bugs → GitHub issues. **Security issues → see [SECURITY.md](SECURITY.md)** (private
disclosure), not a public issue.
