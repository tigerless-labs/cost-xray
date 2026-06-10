from __future__ import annotations

import json
import pathlib
import time
import urllib.request

_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
_TTL = 86400
_BUNDLED = pathlib.Path(__file__).with_name("data") / "litellm_prices.json"
_state: dict = {"map": None, "at": 0.0}


def _cache_file() -> pathlib.Path:
    return pathlib.Path.home() / ".cost-xray" / "litellm_prices.json"


def _fetch() -> dict:
    with urllib.request.urlopen(_URL, timeout=10) as resp:
        return json.loads(resp.read().decode())


def _valid(m) -> bool:
    return isinstance(m, dict) and "claude-opus-4-8" in m


def _bundled() -> dict:
    return json.loads(_BUNDLED.read_text())


def _resolve() -> dict:
    f = _cache_file()
    try:
        if f.exists() and time.time() - f.stat().st_mtime < _TTL:
            cached = json.loads(f.read_text())
            if _valid(cached):
                return cached
    except Exception:
        pass
    try:
        fetched = _fetch()
        if _valid(fetched):
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(fetched))
            return fetched
    except Exception:
        pass
    return _bundled()


def load() -> dict:
    now = time.time()
    if _state["map"] is not None and now - _state["at"] < _TTL:
        return _state["map"]
    resolved = _resolve()
    _state["map"] = resolved
    _state["at"] = now
    return resolved


def reset() -> None:
    _state["map"] = None
    _state["at"] = 0.0
