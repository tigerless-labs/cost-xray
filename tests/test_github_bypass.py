import os
import subprocess

import pytest

from test_run_sh import RUN_SH, _fake, _macos_run


@pytest.mark.parametrize("upper,lower", [(None, None), ("localhost,.internal", None), (None, "127.0.0.1"), ("localhost", "internal.example")])
@pytest.mark.parametrize("launcher", ["daemon", "shell"])
def test_github_bypass_preserves_exclusions_and_ai_proxy(tmp_path, upper, lower, launcher):
    result, home, _ = _macos_run(tmp_path, "install", agents="codex")
    assert result.returncode == 0, result.stderr
    fake_dir = tmp_path / "fake"
    fake_dir.mkdir()
    log = tmp_path / "environment"
    _fake(fake_dir, "codex", 'printf "%s\\n" "$NO_PROXY" "$no_proxy" "$HTTP_PROXY" "$HTTPS_PROXY" "$SSL_CERT_FILE" "$*" > "$TEST_LOG"')
    env = dict(os.environ, HOME=str(home), TEST_LOG=str(log), CODEX_MANAGED_BIN=str(fake_dir / "codex"))
    for key, value in [("NO_PROXY", upper), ("no_proxy", lower)]:
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    if launcher == "shell":
        command = f'source "{home / ".zshrc"}"; _ctxray_up() {{ return 0; }}; codex direct exec test'
        shell = "/bin/zsh"
    else:
        # Load the real launcher functions without running its command dispatcher.
        command = RUN_SH.read_text().rsplit('\ncase "${1:-}" in', 1)[0]
        command += '\n_run_codex_proxied "$CODEX_MANAGED_BIN" exec test\n'
        shell = "/bin/bash"
    result = subprocess.run([shell, "-c", command], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    no_proxy, lower_no_proxy, http, https, ca, args = log.read_text().splitlines()
    expected = [value for value in [upper, lower] if value]
    expected += ["github.com", "githubusercontent.com", "githubassets.com"]
    assert no_proxy == lower_no_proxy == ",".join(expected)
    assert http == https == "http://127.0.0.1:8789"
    assert "chatgpt.com" not in no_proxy and "openai.com" not in no_proxy
    assert ca == str(home / ".cost-xray" / "codex-ca-bundle.pem")
    assert args == "exec test"
