# Provider Docs

One file per supported agent. A "provider" here is a coding agent whose wire traffic cost-xray
reads — translated by a thin per-agent **adapter** under `cost_xray/adapters/`, the only place code
forks by agent. Each file tells you, for that agent: what cost-xray captures and where it lands on
disk, how its adapter turns the raw wire into canonical events, and the quirks that have bitten us.

For the cross-agent architecture — the adapter seam, its uniform contract, and the per-agent
raw → event mapping (type → bucket, usage, window) — see [../architecture.md](../architecture.md).

## Provider Index

| Provider | Capture | Wire path | Adapter | Tests |
|---|---|---|---|---|
| [Claude Code](claude-code.md) | reverse proxy (base-URL override, no CA) | `/v1/messages` (HTTP+SSE) | `cost_xray/adapters/anthropic.py` | `tests/test_adapters.py`, `tests/test_verify_anthropic.py` |
| [Codex](codex.md) | forward proxy + scoped local CA | Responses WebSocket | `cost_xray/adapters/openai.py` | `tests/test_adapters.py`, `tests/test_verify_codex.py` |

Roadmap: Cursor and other base-URL-overridable agents speaking the Anthropic or OpenAI-Responses
shape are close to drop-in — one new adapter module, zero shared edits.

## Shared

| Helper | Used by | Source |
|---|---|---|
| canonical Event + bucket map | both adapters (the target they emit) | `cost_xray/events.py` |
| reconcile / calibrate / roll-up | both (downstream of the event, agent-agnostic) | `cost_xray/classify.py` |
| exact Claude tokenizer | Claude Code (input + output thinking pins) | `cost_xray/count_tokens.py` |

## File Format

Every provider doc shares the same structure:

1. **Summary** — one line on what the agent is and how it's captured.
2. **Metadata** — adapter module · capture transport · tests.
3. **Where it captures from** — the wire path / transport cost-xray intercepts.
4. **Storage format** — what a `raw.jsonl` record looks like for this agent.
5. **Adapter** — raw → events: turn iteration, type → bucket, usage, window, incrementality.
6. **Tokenizer** — how this agent's tokens are sized and calibrated (the pins vs the proportioned rest).
7. **Pricing** — how the $ axis is derived for this agent.
8. **Quirks** — the gotchas that have bitten us before.
9. **When fixing a bug here** — a checklist pointing at the right layer.

## Adding a provider

Copy `claude-code.md` as a template and fill in the specifics; add the row to the index above; add a
real captured fixture under `tests/` (don't mock the wire). The matching code change is one module
under `cost_xray/adapters/` implementing the contract in
[../architecture.md](../architecture.md). Per the project's change order: docs → tests → code.
