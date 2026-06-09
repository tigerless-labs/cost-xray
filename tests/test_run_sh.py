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


# --- launchd (macOS) supervisor -------------------------------------------------
# These force the macOS code path on any OS by shadowing `uname`/`launchctl` with fakes on PATH,
# so the launchd install/status/uninstall is exercised in CI without a real Mac. Same approach as
# above: a real `run.sh` subprocess with HOME pointed at a temp dir — no network, no real proxy.

def _fake(bindir, name, body):
    p = bindir / name
    p.write_text("#!/bin/sh\n" + body + "\n")
    p.chmod(0o755)


def _macos_run(tmp_path, *args, shell="/bin/zsh", agents="claude"):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "launchctl.log"
    _fake(bindir, "uname", 'echo Darwin')                       # pretend macOS
    _fake(bindir, "launchctl", 'echo "$@" >> "$LAUNCHCTL_LOG"')  # record + succeed (no real load)
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    env["SHELL"] = shell
    env["COST_XRAY_AGENTS"] = agents
    env["LAUNCHCTL_LOG"] = str(log)
    res = subprocess.run(["bash", str(RUN_SH), *args], env=env, capture_output=True, text=True)
    return res, home, log


def test_macos_install_writes_launchagent_and_wrapper(tmp_path):
    res, home, log = _macos_run(tmp_path, "install", shell="/bin/zsh")
    assert res.returncode == 0, res.stderr
    plist = home / "Library" / "LaunchAgents" / "ai.tigerless.cost-xray.plist"
    assert plist.exists(), "launchd agent plist not written"
    body = plist.read_text()
    assert "RunAtLoad" in body and "KeepAlive" in body and "_serve" in body
    assert "bootstrap" in log.read_text()                       # loaded via launchctl, not systemctl
    rc = (home / ".zshrc").read_text()                          # wrapper went to zsh rc, not .bashrc
    assert "# >>> cost-xray >>>" in rc and "claude()" in rc and "cx()" in rc
    assert not (home / ".bashrc").exists()
    assert not (home / ".config" / "systemd").exists()          # no systemd unit on macOS


def test_macos_bash_wrapper_goes_to_bash_profile(tmp_path):
    # macOS login bash reads ~/.bash_profile, not ~/.bashrc — wrappers must land where they're sourced.
    res, home, _ = _macos_run(tmp_path, "install", shell="/bin/bash")
    assert res.returncode == 0, res.stderr
    assert "# >>> cost-xray >>>" in (home / ".bash_profile").read_text()


def test_macos_status_reports_launchd_and_uninstall_removes_agent(tmp_path):
    res, home, _ = _macos_run(tmp_path, "install")
    assert res.returncode == 0, res.stderr
    st, _, _ = _macos_run(tmp_path, "status")                   # reuses the same HOME/bin/fakes
    assert st.returncode == 0, st.stderr
    assert "(launchd)" in st.stdout
    un, _, log = _macos_run(tmp_path, "uninstall")
    assert un.returncode == 0, un.stderr
    assert not (home / "Library" / "LaunchAgents" / "ai.tigerless.cost-xray.plist").exists()
    assert "bootout" in log.read_text()
    assert "# >>> cost-xray >>>" not in (home / ".zshrc").read_text()


# --- systemd (Linux) supervisor: regression guard that the Linux path is unchanged ----------------
# Forces the systemd branch with fake `systemctl`/`loginctl` (so no real units are touched) and a
# real Linux `uname`, then asserts the unit body, wrapper rc, and status text are exactly as before.

def _linux_run(tmp_path, *args, shell="/bin/bash", agents="claude"):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "systemctl.log"
    _fake(bindir, "uname", 'echo Linux')                        # force the non-macOS path
    _fake(bindir, "systemctl",
          'case "$2" in show-environment) exit 0 ;; is-active) echo active; exit 0 ;; esac\n'
          'echo "$@" >> "$SYSTEMCTL_LOG"')
    _fake(bindir, "loginctl", 'exit 0')
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    env["SHELL"] = shell
    env["COST_XRAY_AGENTS"] = agents
    env["SYSTEMCTL_LOG"] = str(log)
    res = subprocess.run(["bash", str(RUN_SH), *args], env=env, capture_output=True, text=True)
    return res, home, log


def test_linux_install_unit_and_wrapper_unchanged(tmp_path):
    res, home, log = _linux_run(tmp_path, "install", shell="/bin/bash")
    assert res.returncode == 0, res.stderr
    unit = home / ".config" / "systemd" / "user" / "cost-xray.service"
    assert unit.exists(), "systemd unit not written"
    body = unit.read_text()
    assert "ExecStart=" in body and "_serve" in body
    assert "Restart=always" in body and "RestartSec=3" in body and "WantedBy=default.target" in body
    assert "enable" in log.read_text() and "daemon-reload" in log.read_text()
    rc = (home / ".bashrc").read_text()                         # Linux bash → ~/.bashrc, not zsh/launchd
    assert "# >>> cost-xray >>>" in rc and "claude()" in rc and "cx()" in rc
    assert not (home / "Library" / "LaunchAgents").exists()


def test_linux_status_reports_systemd_and_uninstall_removes_unit(tmp_path):
    res, home, _ = _linux_run(tmp_path, "install")
    assert res.returncode == 0, res.stderr
    st, _, _ = _linux_run(tmp_path, "status")
    assert st.returncode == 0, st.stderr
    assert "(systemd)" in st.stdout                             # Linux status text preserved
    un, _, log = _linux_run(tmp_path, "uninstall")
    assert un.returncode == 0, un.stderr
    assert not (home / ".config" / "systemd" / "user" / "cost-xray.service").exists()
    assert "disable" in log.read_text()
    assert "# >>> cost-xray >>>" not in (home / ".bashrc").read_text()
