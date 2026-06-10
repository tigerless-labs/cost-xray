import os
import shutil
import subprocess
import tempfile
from pathlib import Path

RUN_SH = Path(__file__).resolve().parent.parent / "run.sh"


def _run_status(remove_env=()):
    home = tempfile.mkdtemp()
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
    result = _run_status()
    assert result.returncode == 0, result.stderr
    assert "sessions captured:" in result.stdout


def test_status_survives_unset_USER():
    result = _run_status(remove_env=["USER"])
    assert result.returncode == 0, result.stderr
    assert "sessions captured:" in result.stdout


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
    _fake(bindir, "uname", 'echo Darwin')
    _fake(bindir, "launchctl", 'echo "$@" >> "$LAUNCHCTL_LOG"')
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
    assert "bootstrap" in log.read_text()
    rc = (home / ".zshrc").read_text()
    assert "# >>> cost-xray >>>" in rc and "claude()" in rc and "cx()" in rc
    assert not (home / ".bashrc").exists()
    assert not (home / ".config" / "systemd").exists()


def test_macos_reinstall_is_idempotent(tmp_path):
    r1, home, _ = _macos_run(tmp_path, "install", shell="/bin/zsh")
    assert r1.returncode == 0, r1.stderr
    r2, home, log = _macos_run(tmp_path, "install", shell="/bin/zsh")
    assert r2.returncode == 0, r2.stderr
    rc = (home / ".zshrc").read_text()
    assert rc.count("# >>> cost-xray >>>") == 1 and "claude()" in rc
    assert not (home / ".zshrc.cx.tmp").exists()
    calls = log.read_text()
    assert calls.index("bootout") < calls.index("bootstrap")


def test_macos_bash_wrapper_goes_to_bash_profile(tmp_path):
    res, home, _ = _macos_run(tmp_path, "install", shell="/bin/bash")
    assert res.returncode == 0, res.stderr
    assert "# >>> cost-xray >>>" in (home / ".bash_profile").read_text()


def test_macos_status_reports_launchd_and_uninstall_removes_agent(tmp_path):
    res, home, _ = _macos_run(tmp_path, "install")
    assert res.returncode == 0, res.stderr
    st, _, _ = _macos_run(tmp_path, "status")
    assert st.returncode == 0, st.stderr
    assert "(launchd)" in st.stdout
    un, _, log = _macos_run(tmp_path, "uninstall")
    assert un.returncode == 0, un.stderr
    assert not (home / "Library" / "LaunchAgents" / "ai.tigerless.cost-xray.plist").exists()
    assert "bootout" in log.read_text()
    assert "# >>> cost-xray >>>" not in (home / ".zshrc").read_text()


def _linux_run(tmp_path, *args, shell="/bin/bash", agents="claude"):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "systemctl.log"
    _fake(bindir, "uname", 'echo Linux')
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
    rc = (home / ".bashrc").read_text()
    assert "# >>> cost-xray >>>" in rc and "claude()" in rc and "cx()" in rc
    assert not (home / "Library" / "LaunchAgents").exists()


def test_linux_status_reports_systemd_and_uninstall_removes_unit(tmp_path):
    res, home, _ = _linux_run(tmp_path, "install")
    assert res.returncode == 0, res.stderr
    st, _, _ = _linux_run(tmp_path, "status")
    assert st.returncode == 0, st.stderr
    assert "(systemd)" in st.stdout
    un, _, log = _linux_run(tmp_path, "uninstall")
    assert un.returncode == 0, un.stderr
    assert not (home / ".config" / "systemd" / "user" / "cost-xray.service").exists()
    assert "disable" in log.read_text()
    assert "# >>> cost-xray >>>" not in (home / ".bashrc").read_text()


def test_linux_reinstall_restarts_running_service(tmp_path):
    r1, _, log = _linux_run(tmp_path, "install")
    assert r1.returncode == 0, r1.stderr
    r2, _, log = _linux_run(tmp_path, "install")
    assert r2.returncode == 0, r2.stderr
    assert "restart cost-xray.service" in log.read_text()
