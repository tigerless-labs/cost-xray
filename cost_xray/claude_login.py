from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time

CONFIG_ENV = "CLAUDE_CONFIG_DIR"
DEFAULT_DIR = "~/.claude"
CREDENTIALS_NAME = ".credentials.json"
KEYCHAIN_SERVICE = "Claude Code-credentials"
SKEW_MS = 60_000

_warned = False


def _config_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get(CONFIG_ENV) or DEFAULT_DIR).expanduser()


def _blob_from_file():
    try:
        d = json.loads((_config_dir() / CREDENTIALS_NAME).read_text())
        return d.get("claudeAiOauth") or None
    except Exception:
        return None


def _keychain_raw():
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout if out.returncode == 0 else None
    except Exception:
        return None


def _blob_from_keychain():
    if sys.platform != "darwin":
        return None
    try:
        raw = _keychain_raw()
        return (json.loads(raw).get("claudeAiOauth") or None) if raw else None
    except Exception:
        return None


def _warn_expired():
    global _warned
    if _warned:
        return
    _warned = True
    print(
        "cost-xray: Claude Code login has expired — exact tokenization is off until you "
        "re-authenticate in Claude Code (or set ANTHROPIC_API_KEY).",
        file=sys.stderr,
    )


def access_token(now: float | None = None):
    blob = _blob_from_file() or _blob_from_keychain()
    if not blob:
        return None
    tok = blob.get("accessToken")
    if not tok:
        return None
    exp = blob.get("expiresAt")
    if isinstance(exp, (int, float)):
        now = time.time() * 1000 if now is None else now
        if now >= exp - SKEW_MS:
            _warn_expired()
            return None
    return tok
