# Claude Code

Anthropic's CLI coding agent, speaking the `/v1/messages` wire. Captured as a **reverse proxy** via
a base-URL override — no CA certificate, no OS trust changes. This is the reference adapter; copy it
when adding an agent.

- **Adapter:** `cost_xray/adapters/anthropic.py`
- **Capture:** reverse proxy (`ANTHROPIC_BASE_URL` → the local hop), HTTP + SSE
- **Tests:** `tests/test_adapters.py`, `tests/test_verify_anthropic.py`

## Where it captures from

The agent's HTTPS calls to `api.anthropic.com` are pointed at the local proxy by base URL, so no
certificate is needed. cost-xray matches the completion path `/v1/messages`; `count_tokens` probes
and error responses carry no `usage` and are filtered as non-turns. The session id comes straight
from the `X-Claude-Code-Session-Id` request header — the same UUID as the
`~/.claude/projects/.../<uuid>.jsonl` transcript — with fallbacks.

## Storage format

`raw.jsonl` is **one record per completed turn** (request + buffered response/usage). Streaming
responses are buffered and the SSE body parsed into structured blocks before the record is written;
the agent still receives each complete response, so tool loops are unaffected. Repeated content
blocks are de-duplicated into `blocks.jsonl` by the raw codec. Append-only, never rewritten.

## Adapter

A raw record **is one turn** and the whole conversation is re-sent each turn, so a turn is
**self-contained** (`INCREMENTAL = True` — the materializer may parse only new bytes).

- **turn → events** (`to_events`): input static = the system block array **directly** (no content
  wrapper) + each tool entry (schema, MCP server from the name prefix); input messages = each
  message content block, bucket from its type, role from the message; output = each response block,
  or SSE-reconstructed blocks.
- **type → bucket**: text → text; thinking / redacted → thinking; tool_use / server tool use →
  tool_use; tool_result / search results → tool_result. Unknown types pass through verbatim.
- **usage**: fresh (uncached input), cached (cache-read), rewrote (cache-creation, incl. the nested
  ephemeral split), output; plus a flag when the long-TTL 1-hour write bucket is non-zero.
- **window**: the extended-context signal is a **request header** (or a model tag) — not the model
  name, not max-tokens.
- **thinking shape**: Claude streams thinking only as a **signature delta** (no readable text), so
  SSE reconstruction must capture the signature, or output thinking measures near-zero and its cost
  leaks into text. Sizing that signature is the Tokenizer section.

## Tokenizer

o200k is only *approximate* for Claude (its tokenizer is private), so sizes are **calibrated to the
wire `usage`** — pin what tiktoken mis-sizes, proportion the rest:

- **thinking** — separated **first**: tiktoken over-counts the base64 signature, opposite-signed to
  the ~uniform under-count elsewhere, so it can't ride the global scale. Pinned exact via
  `count_tokens` when auth is present, else the `THINKING_R` correction.
- **tool schema** — exact in-context marginal when auth is present (cached — tools are static), else
  proportional.
- **everything else** — tiktoken scaled so the calibrated total = wire usage − pins.

Calibrated total = wire usage (**bill is exact**); the only approximation is the split between
sibling events — sizing every event via `count_tokens` would add per-event API load and rate-limit
exposure, so the default calibrates instead and the residuals are benchmarked continuously. Exact
pins are gated on `count_tokens` auth (API key or the Claude Code OAuth login via
`claude_login.py`) and **fail-open** — no auth ⇒ zero network calls, identical output, less
precise. Why pin, and the accuracy board: [../architecture.md](../architecture.md).

**Planned:** an opt-in exact mode — every event sized by full `count_tokens` differencing —
manually enabled for precision-critical use.

## Pricing

`cost.py` resolves per-model rates from the LiteLLM price map (a small override covers models
LiteLLM doesn't list yet) and prices the calibrated usage split: fresh input, cache-read
(discounted), cache-write — with the 1-hour-TTL write Claude Code uses priced above the 5-minute
write rate — and output. Cache rates are LiteLLM's own per-model values.

## Quirks

- The system prompt is the block array **directly** — no `content` wrapper, unlike messages/output.
- Tool entries carry name + description + schema but **no type and no content**.
- Intermittent **skill ads** are carved out of system text (and system-role messages) by
  `detectors.py`; an injected `SKILL.md` body becomes a per-skill "Skill loads" row.
- Output thinking is a signature-only stream — easy to drop in SSE reconstruction.

## When fixing a bug here

1. **Wrong bytes / missing turn?** Capture — `addon.py` (`response`, `_parse_sse`, `_session_id`).
2. **Wrong source/bucket attribution?** The adapter — `to_events` and the raw→event mapping in
   [../architecture.md](../architecture.md). Keep grouping out of the adapter; it ends at "emit
   an event."
3. **Numbers don't reconcile to the wire?** That's shared, not the adapter — `classify.py`
   (`reconcile_turn`) and the conservation laws in `tests/test_reconcile.py`.
4. **Thinking over/under-counted?** `THINKING_R` and the exact pins ([../architecture.md](../architecture.md)).
5. Add a **real captured fixture**; don't mock the wire.
