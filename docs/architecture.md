# cost-xray Architecture

A map of the codebase — read once before a non-trivial PR. This doc is the *where*: which module
owns what, and how data flows between them. Per-agent specifics live in
[providers/](providers/README.md); contribution conventions in [../CONTRIBUTING.md](../CONTRIBUTING.md).

## Three stages, one source of truth

cost-xray is a single Python package (`cost_xray/`) running three stages around one append-only
store. The principle: **capture raw, derive at read time** — the proxy only writes bytes; every
view is computed later, so improving the logic re-analyzes old sessions with no re-capture.

```
 coding agent                                                              you
  (Claude Code / Codex)                                                     │
        │ HTTP / WebSocket                                                  │ keys, mouse
        ▼                                                                   ▼
 ┌──────────────┐      raw store       ┌────────────────────┐   derived   ┌──────────┐
 │ 1 CAPTURE    │ ───────────────────▶ │ 2 MATERIALIZE      │ ──────────▶ │ 3 TUI    │
 │ addon.py     │  ~/.cost-xray/...    │ separate process   │  +summary   │ reader   │
 │ (proxy hop)  │  raw.jsonl/blocks    │ raw→events→$        │             │ (group)  │
 └──────────────┘                      └────────────────────┘             └──────────┘
        writes bytes only        the only CPU work; never in the proxy        zero compute
                          ╰─── 🔑 per-agent seam: cost_xray/adapters/ ───╯
```

The three stages share **no agent-specific code**. Everything that differs by agent lives behind
the canonical event in `cost_xray/adapters/` — the one seam ([providers/](providers/README.md)).

## The raw store

Capture and materialize meet on disk under `~/.cost-xray/sessions/<agent>/<session_id>/`, keyed by
the real agent and session id so a long-lived daemon keeps each coding session separate.

```
~/.cost-xray/sessions/
  <agent>/                       claude-code | codex
    <session_id>/
      meta.json                  agent / model / first_seen / last_seen / n_turns
      raw.jsonl                  truth, append-only — one delta record per turn (or WS frame); secrets redacted
      blocks.jsonl               dedup side-store: each unique content block once, referenced by raw (raw_codec)
      derived.jsonl              per-turn calibrated events (materialize output)
      summary.json               cumulative roll-up the TUI reads
    rollup.json                  per-agent project/session totals cache
```

`raw.jsonl` + `blocks.jsonl` are written by capture and read by materialize; `derived.jsonl` +
`summary.json` are written by materialize and read by the TUI. The dedup codec lives in
`raw_codec.py`.

## Stage 1 — capture (`addon.py`)

A mitmproxy addon, the only process in the request hot path. It reuses mitmproxy as plumbing —
reverse-proxy mode for Claude Code (base-URL override, no CA cert), forward proxy + scoped local CA
for Codex. It **only records bytes**: match the request path, redact `authorization` / `x-api-key`
and secret body keys, buffer SSE responses and the Codex WebSocket frame stream, then hand off to a
background writer thread. No tokenization runs here — that would steal the GIL from live relay
(invariant 1).

| concern | where |
|---|---|
| path match / redaction | `_matched`, `_redact_headers`, `_redact_body` |
| session id / dir / meta | `_session_id`, `_session_dir`, `_update_meta` |
| SSE + WebSocket records | `_parse_sse`, `_ws_record`, `response`, `websocket_message` |
| start the warm consumer once + signal per turn | `_ensure_consumer`, `_signal_materialize` |
| append-only write (via the dedup codec) | `raw_codec.append_record` |

## Stage 2 — materialize (the consumer process)

A warm, long-lived consumer the proxy starts once and signals per turn — never the proxy, never
gated on the TUI. It pays the tokenizer import once, blocks on the signal channel when idle, and
sweeps every stale session per signal.

- **`materialize_daemon.py`** — the loop. `stale_sessions` (disk walk: raw newer than summary),
  `sweep_once` (one pass), `consume` (the warm blocking loop), `main` (manual one-shot CLI).
- **`materialize.py`** — one session, per-turn, in fixed order: adapter → canonical events →
  `reconcile_turn` (calibrate every event + cut at the cache boundary) → append one self-contained
  `derived` line → fold into `summary`. Incremental; a `LOGIC_VERSION` bump or any inconsistency
  falls back to a full rebuild. Also owns the per-agent `rollup` cache. Per-session `fcntl` lock
  makes concurrent sweeps safe.

The shared classification and pricing it calls never branch on agent:

| module | owns |
|---|---|
| `events.py` | the canonical **Event** + the wire-type → bucket map; the classification taxonomy |
| `classify.py` | `reconcile_turn` — calibrate tiktoken event sizes to the wire `usage` total, separate `thinking` first, cut the input axis at the cache boundary; then `rollup` |
| `cost.py` | the $ axis — per-model rates (LiteLLM snapshot + fallback), fresh / cached / rewrote / output |
| `analyze.py` | tiktoken sizing (`ntok`) + window detection |
| `count_tokens.py` | the **exact** Claude tokenizer via Anthropic's `count_tokens` endpoint (gated on auth, lazy, cached) |
| `claude_login.py` | locate the Claude Code OAuth login so a Max/Pro user needs no API key |
| `detectors.py` | the thin overlay beside the harvested tree — skill ads / skill loads |

Why calibrate, why separate thinking, why two tokenizers: the per-agent **Tokenizer** sections
under [providers/](providers/README.md).

## Stage 3 — TUI (pure reader)

Reads `derived` / `summary` and only **groups** — it never recalibrates (invariant 2).

- **`tui.py`** — data + formatting (sessions list, rollup, summary/derived loaders, bars).
- **`tui_app.py`** — the interactive Textual app: `HomeScreen` (agent → project → session),
  `DetailScreen` (per-source cost + window panels), `DrillTable` (the expand-in-place drill tree).
- **`drill.py`** — drill-down queries + lazy content fetch: resolve a summary cell down to its
  per-server / per-tool / per-call rows, and fetch the raw block behind an event on demand.

## The seam — `cost_xray/adapters/`

The **only** place code forks by agent. `adapters/__init__.py` is the registry: dispatch by the
captured agent / request path, exposing one uniform contract (iterate turns, turn → events, usage,
window, thinking correction, incrementality, locate, session/project name). Each agent is one
module — `anthropic.py` (Claude Code, `/v1/messages`), `openai.py` (Codex, the Responses WebSocket).
Adding an agent is one module, **zero shared edits**.

Per-agent reading detail — the contract and the wire→event mapping — lives in
**[providers/](providers/README.md)** (one file per agent).

## Verification (`verify.py`)

Off the hot path: the accuracy + completeness board. `bench_turn` / `aggregate` compare our
per-event/tool/bucket numbers against the wire `usage` and a coverage check, rendered to a markdown
report. The runnable board is `verify.py` and `experiments/benchmark.py`.

## Tests

`tests/test_<module>.py`, one per module, run with `make ci`. Use the `make_flow` /
`isolated_sessions` fixtures (no real mitmproxy) and assert **relationships / invariants** — drilling
a cell sums back to its summary, server A > server B, value > 0 — never hardcoded token numbers,
which break when the tokenizer changes. The reconciliation conservation laws live in
`tests/test_reconcile.py`. Conventions: [CONTRIBUTING.md](../CONTRIBUTING.md).
