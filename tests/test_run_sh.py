"""Install-robustness regressions for run.sh.

Both bugs were found in a clean-sandbox end-to-end (fresh Docker, README install):
the installer ends by calling `status`, so a `status` that aborts under
`set -euo pipefail` makes a fully-successful fresh install exit non-zero AND truncates
the final "open a new terminal / run your agent" instructions.

These exercise `run.sh status` as a subprocess with HOME pointed at an empty dir, so no
systemd, venv, or network is needed — they run anywhere CI does.
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

RUN_SH = Path(__file__).resolve().parent.parent / "run.sh"


def _run_status(remove_env=()):
    home = tempfile.mkdtemp()  # exists, but has no ~/.cost-xray/sessions yet (fresh install)
    env = dict(os.environ)
    env["HOME"] = home
    for key in remove_env:
        env.pop(key, None)
    try:
        return subprocess.run(
            ["bash", str(RUN_SH), "status"],
            env=env,
            capture_output=True,
            text=True,
        )
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_status_on_fresh_state_exits_zero_and_prints_sessions_line():
    # Bug A: before the proxy creates ~/.cost-xray/sessions, `find` over it fails;
    # under pipefail+set -e that aborted status (and the install) before this line.
    result = _run_status()
    assert result.returncode == 0, result.stderr
    assert "sessions captured:" in result.stdout


def test_status_survives_unset_USER():
    # Bug B: CI / docker exec / cron may not export USER; under set -u a bare $USER is
    # fatal. run.sh must tolerate an unset USER without dying.
    result = _run_status(remove_env=["USER"])
    assert result.returncode == 0, result.stderr
    assert "sessions captured:" in result.stdout
