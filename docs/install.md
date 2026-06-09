# Install — full reference

The [README](../README.md#install) has the one-line install and basic use. This is the deep
reference: what `install` writes to disk, systemd specifics, GUI agents, manual mode, and
troubleshooting.

```bash
curl -fsSL https://raw.githubusercontent.com/tigerless-labs/cost-xray/master/install.sh | bash
```

Set `COST_XRAY_AGENTS=claude|codex|all` to skip the agent prompt (e.g. CI). Picking Codex sets up a
**scoped local mitm CA**, used only by the `codex` command and never added to your OS trust store —
selecting it is your consent. Already cloned? Run `./install.sh` (or `./run.sh install`).

## What `install` sets up

| Thing | Where |
| --- | --- |
| systemd user services | `~/.config/systemd/user/cost-xray.service` (Claude, reverse) and `cost-xray-codex.service` (Codex, forward) |
| linger | `loginctl enable-linger $USER` — start at boot before you log in |
| config | `~/.cost-xray/env` (`UPSTREAM` / `MITMDUMP` / `PORT` / `CODEX_PORT`) |
| live ports | `~/.cost-xray/port` (:8788), `~/.cost-xray/codex-port` (:8789) |
| codex CA bundle | `~/.cost-xray/codex-ca-bundle.pem` (system roots + local mitm CA) |
| shell wrappers | `claude()`/`codex()` + `cx` in `~/.bashrc` — scoped, not a global proxy |
| pause marker | `~/.cost-xray/paused` — present after `cx stop`; wrappers then run agents direct |

The wrappers are **self-healing**: each run probes the proxy and restarts it if it's down, so
capture survives crashes and reboots. `claude` needs **no CA** (reverse-proxy mode re-encrypts to
the real API); `codex` routes through the forward proxy with the scoped CA. Each wrapper affects
only its own command — your `curl` / `git` / `pip` and the OS trust store are untouched.

## Manage (from any directory)

```bash
cx status              # both services' state + live ports + sessions captured
cx stop                # pause — claude/codex now run direct (uncaptured)
cx start               # resume
cx restart             # bounce both proxies (e.g. after editing ~/.cost-xray/env)
```

`cx` is the whole CLI (it forwards to the repo's `run.sh`). One-off direct run without pausing
globally: `CX_OFF=1 claude` (or `codex`) — the guaranteed escape hatch if a proxy misbehaves.
Re-running `cx install` is **authoritative**: it installs your selected agents' proxies and
removes the rest.

## Why it survives reboots & port clashes

- **Reboot** → the systemd user service + linger bring it back (plain `nohup` would not).
- **Crash** → `Restart=always` (3s backoff).
- **Port taken** → the proxy scans up to the next free port, writes it to `~/.cost-xray/port`, and
  the wrapper reads that file on every launch — so the client always finds it.

## GUI agents (Cursor, etc.)

Desktop apps don't read your shell, so the wrapper can't reach them. Point them at the proxy in
their own settings:

```
Base URL:  http://127.0.0.1:8788     # or whatever cx status shows
```

Cursor and other base-url agents use the reverse proxy (:8788); Codex hard-locks its endpoint to
`chatgpt.com` and uses the forward proxy (:8789) + the codex CA bundle.

## Verify

```bash
cx status
#   reverse(claude): active (systemd)   :8788 -> https://api.anthropic.com
#   forward(codex):  active (systemd)   :8789 -> chatgpt.com
# sessions captured: N

systemctl --user status cost-xray.service cost-xray-codex.service
journalctl --user -u cost-xray-codex.service -n 20
```

## Manual mode (no systemd)

If `systemd --user` isn't available (some containers, WSL), skip `install` and run it detached. It
still self-adapts the port but **won't survive a reboot** — re-run `start` after each boot.

```bash
./run.sh start     # nohup background proxy
./run.sh stop
./run.sh status
```

Then set the base URL yourself (it prints the line): `export ANTHROPIC_BASE_URL=http://127.0.0.1:8788`.

## Uninstall

```bash
cx uninstall
```

Removes the services and the `# >>> cost-xray >>>` … `# <<< cost-xray <<<` block in `~/.bashrc`.
Captured data under `~/.cost-xray/` is **kept** — `rm -rf ~/.cost-xray` to clear it.

## Troubleshooting

- **`mitmdump not found`** — the venv wasn't built; re-run `./install.sh` (it provisions the venv, fetching a Python via uv when the system one is missing or too old).
- **linger needs root** — on some distros: `sudo loginctl enable-linger $USER` (once).
- **zsh** — wrappers are written to `~/.bashrc`; copy the `# >>> cost-xray >>>` block into `~/.zshrc`.
- **change port / upstream** — edit `~/.cost-xray/env` (`PORT` / `CODEX_PORT` / `UPSTREAM`), then `cx restart`.
