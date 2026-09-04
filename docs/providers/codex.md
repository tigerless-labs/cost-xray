# Codex

OpenAI's Codex coding agent, speaking the **Responses API over a WebSocket**. Captured as a
**forward proxy** with a scoped local CA — trusted only by the `codex` command, never added to the
OS trust store.

- **Adapter:** `cost_xray/adapters/openai.py`
- **Capture:** forward proxy + scoped local CA, WebSocket frame stream
- **Tests:** `tests/test_adapters.py`, `tests/test_verify_codex.py`

## Where it captures from

Codex pins its endpoint, so capture is a forward proxy with a local CA that only the `codex` process
trusts (set up by the installer; self-healing wrapper). The intercepted path is the Codex Responses
endpoint, carried as a **WebSocket frame stream** rather than one request/response per turn.

### Desktop Remote and the shared app-server

Codex Remote installs a managed standalone Codex binary under
`~/.codex/packages/standalone/current/` and runs a shared app-server daemon from it. The Xray shell
wrapper prefers that managed binary when present, so terminal clients and the daemon use the same
Codex version; Homebrew or another `codex` on `PATH` remains the fallback.

The daemon is long-lived, so proxy variables from a later shell do not change its environment.
After enabling Remote, installing Xray, or changing the Codex proxy configuration, restart the
shared backend explicitly:

```sh
cx codex-daemon-restart
```

This starts the Codex capture proxy if necessary, stops the existing managed app-server, and starts
it fresh with Xray's forward-proxy and scoped CA environment. A simple daemon `restart` is not
enough because the existing updater process retains its original environment. Active Codex clients
may briefly reconnect. Ordinary `codex` launches never restart the shared daemon implicitly.

Remote permits only one live app-server for a registered Mac. If ChatGPT desktop already has
**Settings → Connections → Control this Mac** enabled, a separately managed CLI daemon receives
`409 Conflict` (`Remote app server already online`). Turn that desktop setting off before starting
Remote on the managed daemon. The desktop app and the managed daemon are separate live backends;
sharing Codex's on-disk thread history does not make them the same running agent process.

For terminal sessions that must stay visible and controllable from a paired phone, use the shared
daemon instead of launching a separate local Codex process:

```sh
cx remote
```

Pass normal Codex arguments after the command, for example `cx remote resume <thread-id>`. The first
run prepares the daemon under Xray, enables Remote, and connects the terminal over Codex's default
Unix control socket. Later terminals reuse that daemon without restarting it. `cx codex-shared` is
an equivalent, more explicit alias.

If ChatGPT desktop still owns Remote, `cx remote` stops with the exact setting to disable. After the
managed daemon becomes the sole Remote owner, the terminal and phone are clients of the same
app-server and can observe or steer the same live thread. A plain `codex` command still starts its
own writer, so use `cx remote` for work you expect to continue from your phone.

## Storage format

`raw.jsonl` is **one frame per line**, appended in real time as the WebSocket delivers them — not a
record-per-turn. A turn spans from its first frame to its completion frame and is reassembled at read
time. Secrets redacted; append-only.

## Adapter

Because raw is a frame stream, the adapter **iterates turns** itself (`iter_turns`: reassemble each
turn from its frames) and is **non-incremental** (`INCREMENTAL = False` — materialize needs the whole
stream, not just new bytes).

- **turn → events** (`to_events`): input static = the instructions (re-sent every turn, so Static is
  fully recoverable) + each tool entry (name, else its type for builtins); input messages = each
  input item, but **only the NEW items** this turn (see the asymmetry); output = each output item,
  role assistant, a function call joined to its output by id.
- **type → bucket**: reasoning → thinking; message content → text; function call → tool_use;
  function-call output → tool_result; custom-tool and web-search call/output variants map to the same
  tool_use / tool_result. **Compaction is deliberately unmapped** — it surfaces as its own row, an
  opaque server-side history marker worth seeing.
- **usage**: **no write premium**; the input total **includes** the cached part (fresh = input −
  cached); output as given; plus the reasoning-token count as **exact output thinking, from the wire**.
- **window**: from the model name (extended vs default).

## Tokenizer

o200k **is** Codex's tokenizer, so raw sizes are already exact — it **never calls the Claude
tokenizer**:

- **output thinking (reasoning)** — pinned to the wire's reasoning-token count; visible reasoning is
  encrypted (near-zero raw), so `THINKING_R = 1.0` and separation is a no-op.
- **everything else** — tiktoken-proportional to `usage`, absorbing serialization / history framing.

Near-exact per-event split, **exact bill**. The accuracy board scores Codex **total-only** — o200k
*is* the tokenizer, so a per-source "truth" would just be our own number. Detail:
[../architecture.md](../architecture.md).

## The one real asymmetry — server-side history

Codex keeps conversation history **server-side**, so each turn's wire input carries only the *new*
items. Two consequences the adapter handles, and the only place it differs structurally from
Anthropic:

- Per-source attribution of messages needs **cross-turn accumulation** (Anthropic re-sends
  everything, so its turn is self-contained).
- A **compaction** item resets local reconstruction: pre-compact history becomes opaque server
  state, so the adapter starts a fresh post-compact history from that turn and treats the
  unobservable usage delta as compaction / tokenizer overhead — **not** re-attributed to old tool
  results. The static prefix stays recoverable because it is re-sent.

## Pricing

Same shared `cost.py` rate path as Claude Code — per-model rates from the LiteLLM price map —
applied to Codex's usage split. There is no cache-write premium on this wire.

## Quirks

- Raw is frames, not turns — anything that assumes record-per-turn will mis-handle Codex.
- The stream may open with an empty-input create (a startup probe), and the first real user item
  arrives after injected developer / environment wrappers — session naming scans past both.
- Builtins may lack a tool name; they fall back to their type as the leaf.
- Reasoning items are empty on the wire (encrypted plaintext); rely on the reasoning-token count, not
  item text, for output thinking — see [../architecture.md](../architecture.md).

## When fixing a bug here

1. **Frames missing / mis-ordered?** Capture — `addon.py` (`websocket_message`, `_ws_record`).
2. **Turn boundaries wrong?** `iter_turns` in the adapter (frame reassembly, compaction detection).
3. **Source/bucket attribution wrong?** `to_events` + the raw→event mapping in
   [../architecture.md](../architecture.md). Grouping stays out of the adapter.
4. **Messages don't add up across turns?** The server-side-history accumulation above — a Codex-only
   concern, still inside the adapter.
5. **Numbers don't reconcile to the wire?** Shared — `classify.py` and `tests/test_reconcile.py`.
6. Add a **real captured fixture**; don't mock the frame stream.
