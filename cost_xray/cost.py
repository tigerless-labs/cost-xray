"""Cost layer: turn the captured response `usage` into per-turn $ by cost-type.

This owns the **cost-type axis** of the dashboard (rewrote / cached / fresh /
output). The *source* axis (System / tools / messages) comes from analyze.py; the
v2 cross of the two (per-source cache attribution, the `rewrote↯` cells) is the
job of the span ∩ cache-boundary intersection — TODO, see design/tui-design.md (Roadmap).

Only real completion turns carry usage; count_tokens calls and errors don't, so
`is_completion()` filters them out (they must not count as turns).
"""
from __future__ import annotations

PRICING = {
    "opus":   {"input": 15.0, "output": 75.0},
    "sonnet": {"input": 3.0,  "output": 15.0},
    "haiku":  {"input": 1.0,  "output": 5.0},
}
_DEFAULT = PRICING["opus"]

CACHE_READ_MULT = 0.1
CACHE_WRITE_MULT = 1.25


def _family(model: str) -> str:
    m = (model or "").lower()
    for fam in ("opus", "sonnet", "haiku"):
        if fam in m:
            return fam
    return "opus"


_LITELLM_CACHE: dict = {}
_MAX_PER_TOKEN = 1.0


def _safe_rate(x):
    """Clamp a per-token $ rate to a sane, non-negative value, or None if unusable.

    Defense in depth (after codeburn): a tampered / malformed LiteLLM entry shipping a
    negative `input_cost_per_token` would otherwise subtract from totals, and a stray
    decimal shift would wildly inflate them. Reject NaN/Inf/negative; cap at $1/token."""
    if not isinstance(x, (int, float)) or isinstance(x, bool):
        return None
    if x != x or x in (float("inf"), float("-inf")) or x < 0:
        return None
    return min(float(x), _MAX_PER_TOKEN)


def _parse_litellm_entry(entry):
    """A LiteLLM `model_cost` entry → {input, output, cache_read, cache_write} $ per 1M
    tokens, or None if unusable. Cache costs are taken per-model from LiteLLM
    (`cache_read_input_token_cost` / `cache_creation_input_token_cost`) — so Anthropic's
    0.1× read and Codex's ~0.5× read are both correct — falling back to the published
    Anthropic ratios when LiteLLM omits them. Pure + testable (no import, no network)."""
    if not isinstance(entry, dict):
        return None
    inp = _safe_rate(entry.get("input_cost_per_token"))
    out = _safe_rate(entry.get("output_cost_per_token"))
    if inp is None or out is None:
        return None
    cr = _safe_rate(entry.get("cache_read_input_token_cost"))
    cw = _safe_rate(entry.get("cache_creation_input_token_cost"))
    return {"input": inp * 1_000_000, "output": out * 1_000_000,
            "cache_read": (cr if cr is not None else inp * CACHE_READ_MULT) * 1_000_000,
            "cache_write": (cw if cw is not None else inp * CACHE_WRITE_MULT) * 1_000_000}


def _litellm_rates(model: str):
    """Base {input, output} $ per 1M tokens from LiteLLM's `model_cost`, or None.

    LiteLLM is an optional dependency: if it isn't installed or doesn't know the model, we
    return None and the caller falls back to PRICING. Lookups are **exact keys only** (the
    model, its `[1m]`-stripped form, and its provider-prefix-stripped form) — never a
    substring match, so `gpt-4o-mini` can't accidentally price as `gpt-4o`. Cached per
    model (the import is heavy)."""
    if not model:
        return None
    if model in _LITELLM_CACHE:
        return _LITELLM_CACHE[model]
    out = None
    try:
        import litellm
        mc = getattr(litellm, "model_cost", {}) or {}
        for key in (model, model.split("[", 1)[0], model.split("/", 1)[-1]):
            out = _parse_litellm_entry(mc.get(key))
            if out:
                break
    except Exception:
        out = None
    _LITELLM_CACHE[model] = out
    return out


def rates(model: str) -> dict:
    """{input, output, cache_read, cache_write} $ per 1M tokens. **LiteLLM first**
    (accurate, auto-updated, per-model cache costs), hardcoded PRICING + the published
    ratios as the offline fallback. The reconciliation conservation laws hold for any rate
    source because the wire bill and our split both go through this one function."""
    lit = _litellm_rates(model)
    if lit:
        return lit
    base = PRICING.get(_family(model), _DEFAULT)
    inp = base["input"]
    return {"input": inp, "output": base["output"],
            "cache_read": inp * CACHE_READ_MULT, "cache_write": inp * CACHE_WRITE_MULT}


def is_completion(rec: dict) -> bool:
    """A real generation turn (not a count_tokens call / error): has usage."""
    if rec.get("status") not in (200, None):
        return False
    return bool(rec.get("usage"))


def _u(usage: dict, *keys) -> int:
    for k in keys:
        v = usage.get(k)
        if isinstance(v, int):
            return v
    return 0


def turn_cost(usage: dict, model: str) -> dict:
    """Per-turn breakdown by cost-type, in tokens and $.

    Returns {tokens: {...}, usd: {...}, total_usd, cache_hit} where the keys are
    fresh / cached / rewrote / output."""
    r = rates(model)
    inp = r["input"] / 1_000_000
    out = r["output"] / 1_000_000

    fresh = _u(usage, "input_tokens")
    cached = _u(usage, "cache_read_input_tokens")
    rewrote = _u(usage, "cache_creation_input_tokens")
    output = _u(usage, "output_tokens")

    usd = {
        "fresh": fresh * inp,
        "cached": cached * inp * CACHE_READ_MULT,
        "rewrote": rewrote * inp * CACHE_WRITE_MULT,
        "output": output * out,
    }
    total_in = fresh + cached + rewrote
    return {
        "tokens": {"fresh": fresh, "cached": cached, "rewrote": rewrote, "output": output},
        "usd": usd,
        "total_usd": sum(usd.values()),
        "cache_hit": (cached / total_in) if total_in else 0.0,
    }


def session_cost(records: list[dict], model: str | None = None) -> dict:
    """Aggregate a session's raw.jsonl records (completion turns only)."""
    turns = [r for r in records if is_completion(r)]
    agg = {"fresh": 0.0, "cached": 0.0, "rewrote": 0.0, "output": 0.0}
    tok = {"fresh": 0, "cached": 0, "rewrote": 0, "output": 0}
    for r in turns:
        c = turn_cost(r.get("usage") or {}, model or r.get("model") or "")
        for k in agg:
            agg[k] += c["usd"][k]
            tok[k] += c["tokens"][k]
    total_in = tok["fresh"] + tok["cached"] + tok["rewrote"]
    rewrote_per_turn = tok["rewrote"] / len(turns) if turns else 0
    return {
        "n_turns": len(turns),
        "usd": agg,
        "tokens": tok,
        "total_usd": sum(agg.values()),
        "cache_hit": (tok["cached"] / total_in) if total_in else 0.0,
        "rewrote_per_turn": rewrote_per_turn,
    }
