from __future__ import annotations

from cost_xray import pricing_map

CACHE_READ_MULT = 0.1
CACHE_WRITE_MULT = 1.25
_MAX_PER_TOKEN = 1.0

_OVERRIDES = {
    "claude-fable-5": {"input": 10.0, "output": 50.0},
}
_DEFAULT = {"input": 5.0, "output": 25.0}


def _keys(model: str):
    return (model, model.split("[", 1)[0], model.split("/", 1)[-1])


def _safe_rate(x):
    if not isinstance(x, (int, float)) or isinstance(x, bool):
        return None
    if x != x or x in (float("inf"), float("-inf")) or x < 0:
        return None
    return min(float(x), _MAX_PER_TOKEN)


def _parse_litellm_entry(entry):
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
    if not model:
        return None
    mc = pricing_map.load()
    for key in _keys(model):
        out = _parse_litellm_entry(mc.get(key))
        if out:
            return out
    return None


def _flat(base):
    inp = base["input"]
    return {"input": inp, "output": base["output"],
            "cache_read": inp * CACHE_READ_MULT, "cache_write": inp * CACHE_WRITE_MULT}


def _override(model: str):
    if not model:
        return None
    for key in _keys(model):
        base = _OVERRIDES.get(key)
        if base:
            return _flat(base)
    return None


def rates(model: str) -> dict:
    return _litellm_rates(model) or _override(model) or _flat(_DEFAULT)


def is_completion(rec: dict) -> bool:
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
    r = rates(model)
    inp = r["input"] / 1_000_000
    out = r["output"] / 1_000_000

    fresh = _u(usage, "input_tokens")
    cached = _u(usage, "cache_read_input_tokens")
    rewrote = _u(usage, "cache_creation_input_tokens")
    output = _u(usage, "output_tokens")

    usd = {
        "fresh": fresh * inp,
        "cached": cached * r["cache_read"] / 1_000_000,
        "rewrote": rewrote * r["cache_write"] / 1_000_000,
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
