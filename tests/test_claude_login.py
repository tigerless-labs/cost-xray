from __future__ import annotations

import json

from cost_xray import claude_login


def _write_creds(dir_path, *, token="tok", expires_at):
    blob = {"claudeAiOauth": {"accessToken": token, "expiresAt": expires_at}}
    (dir_path / ".credentials.json").write_text(json.dumps(blob))


def test_reads_from_config_dir_override(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _write_creds(tmp_path, token="from-file", expires_at=10_000_000)
    assert claude_login.access_token(now=0) == "from-file"


def test_expired_login_withheld(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _write_creds(tmp_path, expires_at=1_000)
    assert claude_login.access_token(now=5_000) is None


def test_expiry_boundary_respects_skew(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    exp = 1_000_000
    _write_creds(tmp_path, token="t", expires_at=exp)
    assert claude_login.access_token(now=exp - 2 * claude_login.SKEW_MS) == "t"
    assert claude_login.access_token(now=exp - claude_login.SKEW_MS // 2) is None
    assert claude_login.access_token(now=exp) is None


def test_keychain_fallback_on_macos(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(claude_login.sys, "platform", "darwin")
    raw = json.dumps({"claudeAiOauth": {"accessToken": "from-keychain", "expiresAt": 10_000_000}})
    monkeypatch.setattr(claude_login, "_keychain_raw", lambda: raw)
    assert claude_login.access_token(now=0) == "from-keychain"


def test_file_wins_over_keychain(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _write_creds(tmp_path, token="from-file", expires_at=10_000_000)
    monkeypatch.setattr(claude_login.sys, "platform", "darwin")

    def _boom():
        raise AssertionError("keychain must not be consulted when the file resolves")

    monkeypatch.setattr(claude_login, "_keychain_raw", _boom)
    assert claude_login.access_token(now=0) == "from-file"


def test_keychain_skipped_off_macos(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(claude_login.sys, "platform", "linux")
    monkeypatch.setattr(claude_login, "_keychain_raw", lambda: "{}")
    assert claude_login.access_token(now=0) is None


def test_fail_open_on_garbage_file(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    (tmp_path / ".credentials.json").write_text("not json {{{")
    assert claude_login.access_token(now=0) is None
