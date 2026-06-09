#!/usr/bin/env bash
# cost-xray: always-on capture proxy + live TUI for coding agents.
#
# Two supervised proxies, one per capture mode (docs/design.md §3) — install sets up BOTH:
#   - Claude / base-url-overridable agents -> reverse:api.anthropic.com on :8788 (no CA cert).
#   - Codex (endpoint hard-locked to chatgpt.com) -> forward/regular mode on :8789 + a scoped
#     CA bundle (system roots + the local mitm CA, built by install). The injected wrappers are
#     SELF-HEALING: they restart a down proxy and route through it; they run the agent DIRECT only
#     when monitoring is paused (./run.sh stop) or the proxy can't start — so capture being off
#     never breaks the agent (model WS, MCP, web search, auth all keep working).
# Each coding session lands in ~/.cost-xray/sessions/<agent>/<session_id>/.
#
# One-time, zero-maintenance install (recommended) — see docs/install.md:
#   ./run.sh install     # systemd --user services (both proxies): boot-start + auto-restart
#   ./run.sh uninstall   # remove the services + shell wrappers (keeps captured data)
#
# Manual / no-systemd:
#   ./run.sh start       # start the proxy in the background (stays up until stop)
#   ./run.sh stop        # stop the background proxy
#   ./run.sh status      # running? which port? how many sessions captured?
#   ./run.sh restart
#   ./run.sh tui         # attach the live TUI (Ctrl-C just detaches; proxy stays up)
#   ./run.sh             # start (if needed) + attach the TUI
#
# Port is self-adapting: it prefers 8788 but falls back to the next free port if
# taken, and writes the live port to ~/.cost-xray/port. The installed shell
# wrapper reads that file, so the client always finds the proxy (no hardcoding).
set -euo pipefail
: "${USER:=$(id -un)}"   # CI/docker/cron may not export USER; set -u makes a bare $USER fatal

DEFAULT_PORT="${PORT:-8788}"
DEFAULT_UPSTREAM="${UPSTREAM:-https://api.anthropic.com}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="$HERE/run.sh"
PYTHON="$HERE/.venv/bin/python"; [ -x "$PYTHON" ] || PYTHON="python3"
VENV_MITM="$HERE/.venv/bin/mitmdump"   # preferred: addon.py's tiktoken import resolves here
STATE="${HOME}/.cost-xray"
ENVFILE="${STATE}/env"      # config + systemd EnvironmentFile (UPSTREAM/MITMDUMP/PORT)
PORTFILE="${STATE}/port"    # resolved live port, for client discovery (DevToolsActivePort-style)
PIDFILE="${STATE}/proxy.pid"
LOGFILE="${STATE}/proxy.log"
UNIT="${HOME}/.config/systemd/user/cost-xray.service"
SERVICE="cost-xray.service"
# codex forward-proxy track (forward/regular mode + scoped CA), supervised the same way
DEFAULT_CODEX_PORT="${CODEX_PORT:-8789}"
CODEX_PORTFILE="${STATE}/codex-port"   # resolved live forward-proxy port, for the codex() wrapper
CODEX_PIDFILE="${STATE}/codex-proxy.pid"
CODEX_LOGFILE="${STATE}/codex-proxy.log"
CODEX_UNIT="${HOME}/.config/systemd/user/cost-xray-codex.service"
CODEX_SERVICE="cost-xray-codex.service"
CA_BUNDLE="${STATE}/codex-ca-bundle.pem"   # system roots + mitm CA; what the codex() wrapper trusts
# Materialize is event-driven (the capture proxy spawns a one-shot sweep per turn — see addon.py),
# NOT a service. These two only let install/uninstall tear down a materializer unit left by an
# older version that ran it as an always-on poll daemon.
MAT_UNIT="${HOME}/.config/systemd/user/cost-xray-materializer.service"
MAT_SERVICE="cost-xray-materializer.service"
PAUSEFILE="${STATE}/paused"   # present = user ran `stop`; wrappers run agents DIRECT (no auto-restart)
REPOFILE="${STATE}/repo"      # absolute repo dir, so the injected cx() can find the TUI from anywhere
MARK_BEGIN="# >>> cost-xray >>>"
MARK_END="# <<< cost-xray <<<"
mkdir -p "$STATE"

# --- supervisor: systemd (Linux) | launchd (macOS) | none (manual) ------------
# Detected once. Every supervisor touch-point routes through the _sv_* helpers, so the Linux
# path stays byte-for-byte the old behaviour and macOS gets a real LaunchAgent (login-start +
# KeepAlive auto-restart) instead of falling back to manual mode.
LAUNCH_DIR="${HOME}/Library/LaunchAgents"
LABEL="ai.tigerless.cost-xray"
CODEX_LABEL="ai.tigerless.cost-xray-codex"
PLIST="${LAUNCH_DIR}/${LABEL}.plist"
CODEX_PLIST="${LAUNCH_DIR}/${CODEX_LABEL}.plist"
REVREF="${STATE}/reverse-ref"   # supervisor id the claude() wrapper restarts (unit name / launchd label)
CODEXREF="${STATE}/codex-ref"
if [ "$(uname -s)" = "Darwin" ] && command -v launchctl >/dev/null 2>&1; then
  _SV=launchd
elif systemctl --user show-environment >/dev/null 2>&1; then
  _SV=systemd
else
  _SV=none
fi

# Wrappers go to the rc the user's shell actually sources: ~/.bashrc on Linux bash, ~/.zshrc for
# zsh, ~/.bash_profile for macOS bash (its login shells skip ~/.bashrc).
_rc_file() {
  if [ "$(uname -s)" = "Darwin" ] && [ "$(basename "${SHELL:-/bin/zsh}")" = bash ]; then
    printf '%s' "${HOME}/.bash_profile"
  elif [ "$(basename "${SHELL:-/bin/bash}")" = zsh ]; then
    printf '%s' "${HOME}/.zshrc"
  else
    printf '%s' "${HOME}/.bashrc"
  fi
}
RC="$(_rc_file)"

# --- port self-adaptation -----------------------------------------------------
_pick_port() {  # echo first free port at/above $1 (scans up to +100)
  python3 - "${1:-$DEFAULT_PORT}" <<'PY'
import socket, sys
start = int(sys.argv[1] or 8788)
for p in range(start, start + 100):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", p)); print(p); s.close(); break
    except OSError:
        s.close()
else:
    sys.exit(1)
PY
}

# --- the actual server entrypoint (used by both systemd and manual start) ------
_serve() {
  mkdir -p "$STATE"
  # shellcheck disable=SC1090
  [ -f "$ENVFILE" ] && . "$ENVFILE"
  local pref="${PORT:-$DEFAULT_PORT}"
  local up="${UPSTREAM:-$DEFAULT_UPSTREAM}"
  local md="${MITMDUMP:-$VENV_MITM}"
  local chosen
  if ! chosen="$(_pick_port "$pref")"; then
    echo "cost-xray: no free port near $pref" >&2; exit 1
  fi
  printf '%s' "$chosen" > "$PORTFILE"
  echo "cost-xray: serving 127.0.0.1:$chosen -> $up" >&2
  exec "$md" --mode "reverse:$up" -p "$chosen" -s "$HERE/cost_xray/addon.py"
}

# Codex forward proxy: regular (forward) mode, no upstream pin. The addon tags codex by
# host/path and only *records* matched paths while forwarding every host untouched, so MCP /
# web-search / auth all pass through. Needs the scoped CA (the codex() wrapper supplies it).
_serve_codex() {
  mkdir -p "$STATE"
  # shellcheck disable=SC1090
  [ -f "$ENVFILE" ] && . "$ENVFILE"
  local pref="${CODEX_PORT:-$DEFAULT_CODEX_PORT}"
  local md="${MITMDUMP:-$VENV_MITM}"
  local chosen
  if ! chosen="$(_pick_port "$pref")"; then
    echo "cost-xray: no free port near $pref" >&2; exit 1
  fi
  printf '%s' "$chosen" > "$CODEX_PORTFILE"
  echo "cost-xray: codex forward proxy on 127.0.0.1:$chosen (regular mode)" >&2
  exec "$md" --mode regular -p "$chosen" -s "$HERE/cost_xray/addon.py"
}

# --- supervisor ops, by "kind" (reverse=Claude proxy, codex=Codex proxy) ------
# One seam for both supervisors: systemd writes a user unit, launchd writes a LaunchAgent plist;
# everything above calls _sv_* and never names systemctl/launchctl directly.
_k_unit()  { case "$1" in reverse) printf '%s' "$UNIT" ;; codex) printf '%s' "$CODEX_UNIT" ;; esac; }
_k_svc()   { case "$1" in reverse) printf '%s' "$SERVICE" ;; codex) printf '%s' "$CODEX_SERVICE" ;; esac; }
_k_plist() { case "$1" in reverse) printf '%s' "$PLIST" ;; codex) printf '%s' "$CODEX_PLIST" ;; esac; }
_k_label() { case "$1" in reverse) printf '%s' "$LABEL" ;; codex) printf '%s' "$CODEX_LABEL" ;; esac; }
_k_entry() { case "$1" in reverse) printf '%s' "_serve" ;; codex) printf '%s' "_serve_codex" ;; esac; }
_k_log()   { case "$1" in reverse) printf '%s' "$LOGFILE" ;; codex) printf '%s' "$CODEX_LOGFILE" ;; esac; }
_k_desc()  { case "$1" in
               reverse) printf '%s' "cost-xray capture proxy for coding agents (reverse:anthropic)" ;;
               codex)   printf '%s' "cost-xray codex forward proxy (regular mode, self-healing)" ;;
             esac; }

# The launchd domain we can actually reach: gui/<uid> when a GUI session exists (the canonical
# LaunchAgent domain), else user/<uid> (works headless / over SSH). A given context stays on one.
_ld_domain() {
  if launchctl print "gui/$(id -u)" >/dev/null 2>&1; then printf 'gui/%s' "$(id -u)"
  else printf 'user/%s' "$(id -u)"; fi
}

_systemd_write() {  # kind → user unit (Linux)
  cat > "$(_k_unit "$1")" <<EOF
[Unit]
Description=$(_k_desc "$1")
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=-$ENVFILE
ExecStart=$SELF $(_k_entry "$1")
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF
  echo "wrote unit $(_k_unit "$1")"
}

_launchd_write() {  # kind → LaunchAgent plist (macOS): RunAtLoad + KeepAlive = login-start + auto-restart
  mkdir -p "$LAUNCH_DIR"
  cat > "$(_k_plist "$1")" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$(_k_label "$1")</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$SELF</string><string>$(_k_entry "$1")</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$(_k_log "$1")</string>
  <key>StandardErrorPath</key><string>$(_k_log "$1")</string>
</dict>
</plist>
EOF
  echo "wrote launch agent $(_k_plist "$1")"
}

_sv_enable() {  # kind → install (login/boot-start + auto-restart) and start now
  case "$_SV" in
    launchd) _launchd_write "$1"
             launchctl bootout "$(_ld_domain)/$(_k_label "$1")" 2>/dev/null || true
             launchctl bootstrap "$(_ld_domain)" "$(_k_plist "$1")" 2>/dev/null \
               || launchctl load -w "$(_k_plist "$1")" 2>/dev/null || true ;;
    systemd) _systemd_write "$1"; systemctl --user daemon-reload
             systemctl --user enable --now "$(_k_svc "$1")" ;;
  esac
}

_sv_disable() {  # kind → remove the unit/agent (keeps captured data)
  case "$_SV" in
    launchd) launchctl bootout "$(_ld_domain)/$(_k_label "$1")" 2>/dev/null || true
             rm -f "$(_k_plist "$1")" ;;
    systemd) systemctl --user disable --now "$(_k_svc "$1")" 2>/dev/null || true
             rm -f "$(_k_unit "$1")"; systemctl --user daemon-reload 2>/dev/null || true ;;
  esac
}

_sv_start() {  # kind → start the running instance (no install change)
  case "$_SV" in
    launchd) launchctl kickstart "$(_ld_domain)/$(_k_label "$1")" 2>/dev/null \
               || launchctl bootstrap "$(_ld_domain)" "$(_k_plist "$1")" 2>/dev/null || true ;;
    systemd) systemctl --user start "$(_k_svc "$1")" ;;
  esac
}

_sv_stop() {  # kind → stop the running instance (launchd KeepAlive respawns unless booted out)
  case "$_SV" in
    launchd) launchctl bootout "$(_ld_domain)/$(_k_label "$1")" 2>/dev/null || true ;;
    systemd) systemctl --user stop "$(_k_svc "$1")" ;;
  esac
}

_sv_is_active() {  # kind → active|inactive|unknown
  case "$_SV" in
    launchd) if launchctl print "$(_ld_domain)/$(_k_label "$1")" >/dev/null 2>&1; then echo active; else echo inactive; fi ;;
    systemd) systemctl --user is-active "$(_k_svc "$1")" 2>/dev/null || echo unknown ;;
    *) echo unknown ;;
  esac
}

_sv_installed() {  # kind → is the unit/agent present on disk?
  case "$_SV" in
    launchd) [ -f "$(_k_plist "$1")" ] ;;
    systemd) [ -f "$(_k_unit "$1")" ] ;;
    *) return 1 ;;
  esac
}

# --- mode helpers -------------------------------------------------------------
_unit_installed() { _sv_installed reverse; }
_codex_unit_installed() { _sv_installed codex; }

_running() {  # manual-mode: echoes the live PID, or nothing
  [ -f "$PIDFILE" ] || return 1
  local pid; pid="$(cat "$PIDFILE" 2>/dev/null || true)"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && { echo "$pid"; return 0; }
  rm -f "$PIDFILE"; return 1
}

_codex_running() {
  [ -f "$CODEX_PIDFILE" ] || return 1
  local pid; pid="$(cat "$CODEX_PIDFILE" 2>/dev/null || true)"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && { echo "$pid"; return 0; }
  rm -f "$CODEX_PIDFILE"; return 1
}

_mat_unit_installed() { [ -f "$MAT_UNIT" ]; }   # only for tearing down an old poll-daemon unit

_live_port() { cat "$PORTFILE" 2>/dev/null || echo "$DEFAULT_PORT"; }
_codex_live_port() { cat "$CODEX_PORTFILE" 2>/dev/null || echo "$DEFAULT_CODEX_PORT"; }

# manual-mode helpers (no systemd): start/stop one background proxy by entrypoint + pidfile
_start_manual() {  # label entrypoint pidfile logfile
  local label="$1" entry="$2" pidfile="$3" logfile="$4" pid
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    echo "$label: already running (pid $pid)"; return 0
  fi
  rm -f "$pidfile"
  nohup bash "$SELF" "$entry" >>"$logfile" 2>&1 &
  echo $! > "$pidfile"
  sleep 1
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    echo "$label: running (pid $pid). Logs: $logfile"
  else
    echo "$label: failed to start; see $logfile" >&2
  fi
}

_stop_manual() {  # label pidfile
  local label="$1" pidfile="$2" pid
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true; rm -f "$pidfile"
    echo "$label: stopped (was pid $pid)."
  else
    rm -f "$pidfile"; echo "$label: not running."
  fi
}

# --- start / stop / status (both proxies) -------------------------------------
start() {
  rm -f "$PAUSEFILE"                                 # resume monitoring (wrappers route again)
  if _unit_installed || _codex_unit_installed; then
    if _unit_installed; then _sv_start reverse; fi
    if _codex_unit_installed; then _sv_start codex; fi
    sleep 1
    case "$_SV" in launchd) echo "Started (launchd)." ;; *) echo "Started (systemd --user)." ;; esac
    status; return 0
  fi
  echo "Starting capture proxies (detached)..."
  _start_manual "claude reverse proxy" _serve "$PIDFILE" "$LOGFILE"
  _start_manual "codex forward proxy"  _serve_codex "$CODEX_PIDFILE" "$CODEX_LOGFILE"
  echo
  echo "Point your agents at it (any shell):"
  echo "    ANTHROPIC_BASE_URL=http://127.0.0.1:$(_live_port) claude"
  echo "    HTTP_PROXY=http://127.0.0.1:$(_codex_live_port) HTTPS_PROXY=http://127.0.0.1:$(_codex_live_port) \\"
  echo "        SSL_CERT_FILE=$CA_BUNDLE NODE_EXTRA_CA_CERTS=$CA_BUNDLE codex"
}

# stop the proxies without touching the pause marker (shared by stop + restart)
_stop_services() {
  if _unit_installed || _codex_unit_installed || _mat_unit_installed; then
    if _unit_installed; then _sv_stop reverse; fi
    if _codex_unit_installed; then _sv_stop codex; fi
    if _mat_unit_installed; then systemctl --user stop "$MAT_SERVICE"; fi
    return 0
  fi
  _stop_manual "claude reverse proxy" "$PIDFILE"
  _stop_manual "codex forward proxy"  "$CODEX_PIDFILE"
}

# `stop` == stop monitoring: drop the proxies AND mark paused so the wrappers stop auto-restarting
# them and run claude/codex DIRECT (uncaptured). Resume with `start`.
stop() {
  _stop_services
  touch "$PAUSEFILE"
  echo "Monitoring stopped — claude/codex now run DIRECT (uncaptured); the wrappers won't restart"
  echo "the proxy until you resume.  Resume capture:  cx start"
}

_status_one() {  # label kind unit_check_fn run_check_fn port target
  local label="$1" kind="$2" unit_check="$3" run_check="$4" port="$5" target="$6" pid st
  if "$unit_check"; then
    st="$(_sv_is_active "$kind")"
    echo "  $label: ${st:-unknown} ($_SV)   :$port -> $target"
  elif pid="$("$run_check" 2>/dev/null)"; then
    echo "  $label: running pid $pid   :$port -> $target"
  else
    echo "  $label: stopped   (:$port -> $target)"
  fi
}

status() {
  _status_one "reverse(claude)" reverse _unit_installed _running \
              "$(_live_port)" "${UPSTREAM:-$DEFAULT_UPSTREAM}"
  _status_one "forward(codex)" codex _codex_unit_installed _codex_running \
              "$(_codex_live_port)" "chatgpt.com (regular mode, self-healing)"
  echo "  materializer:    event-driven (a warm consumer process, started by the proxy, signalled per turn)"
  local n; n="$(find "$STATE/sessions" -mindepth 2 -maxdepth 2 -type d 2>/dev/null | wc -l | tr -d ' ' || true)"
  echo "sessions captured: ${n:-0}  (under $STATE/sessions/)"
}

# --- shell wrappers (self-healing, pause-aware) + the one-word `cx` TUI launcher --------
# Regenerated on every install (old block stripped first). Wrappers are SELF-HEALING: each run
# probes the local capture proxy and, if it's down, restarts it and routes through — so capture
# stays on across crashes/reboots. They run the agent DIRECT only when the user paused monitoring
# (`./run.sh stop` → ~/.cost-xray/paused) or the proxy can't be brought up; either way the
# agent never breaks. Only the selected agents' wrappers are written; `cx` is always written.
_inject_shell() {  # $1 include claude?  $2 include codex?  (1/0)
  local inc_claude="${1:-0}" inc_codex="${2:-0}"
  touch "$RC"
  if grep -qF "$MARK_BEGIN" "$RC" 2>/dev/null; then
    sed -i "/$MARK_BEGIN/,/$MARK_END/d" "$RC"
  fi
  printf '\n%s\n' "$MARK_BEGIN" >> "$RC"
  # shared helpers + cx (always). cx reads the repo dir from ~/.cost-xray/repo so it stays
  # fully static (no path baked into the heredoc) and works from any directory.
  cat >> "$RC" <<'CTXRAY_COMMON'
# is the local capture proxy on $1 listening?
_ctxray_listening() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
# ensure proxy ($1 port, $2 systemd unit) is up unless monitoring is paused. 0 = route through it.
_ctxray_up() {
  [ -n "${CX_OFF:-}" ] && return 1                        # CX_OFF=1 → force direct (one-shot escape hatch)
  [ -e "$HOME/.cost-xray/paused" ] && return 1            # user stopped monitoring → run direct
  _ctxray_listening "$1" && return 0
  local dom
  if command -v launchctl >/dev/null 2>&1; then           # down (crash/boot) → bring it back
    if launchctl print "gui/$(id -u)" >/dev/null 2>&1; then dom="gui/$(id -u)"; else dom="user/$(id -u)"; fi
    launchctl kickstart "$dom/$2" 2>/dev/null \
      || launchctl bootstrap "$dom" "$HOME/Library/LaunchAgents/$2.plist" 2>/dev/null || true
  else
    systemctl --user start "$2" 2>/dev/null || true
  fi
  local i=0; while [ "$i" -lt 8 ]; do _ctxray_listening "$1" && return 0; sleep 0.25; i=$((i+1)); done
  return 1                                                   # couldn't start → run direct (never break)
}
# cx: the one command, from any directory.
#   cx                 open the live TUI (mouse Textual app; rich fallback if textual is absent)
#   cx start|stop|restart|status|install|uninstall   manage capture (forwards to run.sh)
cx() {
  local d py; d="$(cat "$HOME/.cost-xray/repo" 2>/dev/null || echo .)"
  py="$d/.venv/bin/python"; [ -x "$py" ] || py=python3
  case "${1:-}" in
    ""|tui)        ( cd "$d" 2>/dev/null || return 1
                     if "$py" -c 'import textual' 2>/dev/null; then PYTHONPATH=. "$py" -m cost_xray.tui_app
                     else PYTHONPATH=. "$py" -m cost_xray.tui; fi ) ;;
    start|stop|restart|status|install|uninstall)
                   bash "$d/run.sh" "$@" ;;
    -h|--help|help) printf 'cx                 open the live TUI\ncx start|stop|restart|status   manage capture\ncx install|uninstall           (re)install / remove\n' ;;
    *)             echo "cx: unknown command '$1' (try: cx, cx start|stop|restart|status)" >&2; return 2 ;;
  esac
}
CTXRAY_COMMON
  if [ "$inc_claude" = 1 ]; then
    cat >> "$RC" <<'CLAUDEBLOCK'
# Claude → cost-xray reverse proxy; self-healing; direct only when paused/un-restartable.
claude() {
  local s="$HOME/.cost-xray" p ref; p="$(cat "$s/port" 2>/dev/null || echo 8788)"
  ref="$(cat "$s/reverse-ref" 2>/dev/null || echo cost-xray.service)"
  if _ctxray_up "$p" "$ref"; then
    ANTHROPIC_BASE_URL="http://127.0.0.1:$p" command claude "$@"
  else
    command claude "$@"
  fi
}
CLAUDEBLOCK
  fi
  if [ "$inc_codex" = 1 ]; then
    cat >> "$RC" <<'CODEXBLOCK'
# Codex → cost-xray forward proxy (+ scoped CA); self-healing; direct only when paused/un-restartable.
codex() {
  local s="$HOME/.cost-xray" p ref; p="$(cat "$s/codex-port" 2>/dev/null || echo 8789)"
  local ca="$s/codex-ca-bundle.pem"
  ref="$(cat "$s/codex-ref" 2>/dev/null || echo cost-xray-codex.service)"
  if [ -f "$ca" ] && _ctxray_up "$p" "$ref"; then
    HTTP_PROXY="http://127.0.0.1:$p" HTTPS_PROXY="http://127.0.0.1:$p" \
    SSL_CERT_FILE="$ca" NODE_EXTRA_CA_CERTS="$ca" command codex "$@"
  else
    command codex "$@"
  fi
}
CODEXBLOCK
  fi
  echo "$MARK_END" >> "$RC"
  local wrote="cx"
  [ "$inc_claude" = 1 ] && wrote="claude() + $wrote"
  [ "$inc_codex"  = 1 ] && wrote="$wrote + codex()"
  echo "wrote shell wrappers to $RC: $wrote"
}

_remove_shell() {
  [ -f "$RC" ] || return 0
  if grep -qF "$MARK_BEGIN" "$RC"; then
    sed -i "/$MARK_BEGIN/,/$MARK_END/d" "$RC"
    echo "removed cost-xray block from $RC"
  fi
}

# --- install / uninstall (systemd --user, zero-maintenance) -------------------
install_service() {
  local md="$VENV_MITM"; [ -x "$md" ] || md="$(command -v mitmdump || true)"
  if [ -z "$md" ]; then
    echo "mitmdump not found. Build the venv + deps first:  ./install.sh  (provisions Python via uv)" >&2
    exit 1
  fi
  if [ "$_SV" = none ]; then
    echo "No service supervisor (systemd --user / launchd) available here." >&2
    echo "Fall back to manual mode: ./run.sh start  (and add the claude() wrapper by hand)." >&2
    exit 1
  fi
  mkdir -p "$STATE" "$STATE/sessions"
  [ "$_SV" = systemd ] && mkdir -p "$(dirname "$UNIT")"
  [ "$_SV" = launchd ] && mkdir -p "$LAUNCH_DIR"
  true
  # config: only write if absent, so user edits survive re-install
  if [ ! -f "$ENVFILE" ]; then
    { echo "UPSTREAM=$DEFAULT_UPSTREAM"; echo "MITMDUMP=$md"; echo "PORT=$DEFAULT_PORT"
      echo "CODEX_PORT=$DEFAULT_CODEX_PORT"; } > "$ENVFILE"
    echo "wrote config $ENVFILE"
  fi
  rm -f "$PAUSEFILE"                                 # install (re)enables monitoring
  printf '%s' "$HERE" > "$REPOFILE"                  # so the injected cx() finds the TUI anywhere
  if [ "$_SV" = launchd ]; then                      # what the wrappers tell _ctxray_up to restart
    printf '%s' "$LABEL" > "$REVREF"; printf '%s' "$CODEX_LABEL" > "$CODEXREF"
  else
    printf '%s' "$SERVICE" > "$REVREF"; printf '%s' "$CODEX_SERVICE" > "$CODEXREF"
  fi

  # Which agent(s) to capture? Both services are conditional, and the choice is AUTHORITATIVE:
  # a re-install sets up the selected ones and tears the rest down.
  local claude=0 codex=0
  _choose_agents                                     # sets SELECTED_AGENTS
  case " $SELECTED_AGENTS " in *" claude "*) claude=1 ;; esac
  case " $SELECTED_AGENTS " in *" codex "*)  codex=1 ;; esac
  if [ "$claude" = 1 ]; then _install_reverse; else _remove_reverse; fi
  if [ "$codex"  = 1 ]; then _install_codex "$md"; else _remove_codex; fi
  _remove_mat                                        # materialize is per-turn now; kill any old poll daemon

  if [ "$_SV" = systemd ] && ! loginctl enable-linger "$USER" 2>/dev/null; then
    echo "  note: to also start before you log in, run once:"
    echo "        sudo loginctl enable-linger $USER"
  fi
  _inject_shell "$claude" "$codex"
  echo
  case "$_SV" in
    launchd) echo "Installed. launchd agent(s): login-start + KeepAlive auto-restart." ;;
    *)       echo "Installed. systemd --user service(s): boot-start + auto-restart on crash." ;;
  esac
  echo "This does NOT change what your agent can do, its results, or cost — it's a local hop you"
  echo "can pause anytime with  cx stop . (Codex streaming is untouched; Claude streaming is"
  echo "currently buffered — you see the full response at once.)"
  status
  echo
  echo "Open a NEW terminal (or:  source ~/.bashrc ), then just run your agent:"
  [ "$claude" = 1 ] && echo "    claude            # captured"
  [ "$codex"  = 1 ] && echo "    codex             # captured"
  echo "    cx                # open the live TUI (from any directory)"
  echo "Manage capture from anywhere:  cx status | cx stop | cx start | cx restart"
  if [ "$claude" = 1 ]; then
    echo "GUI agents (e.g. Cursor) don't read your shell — set their base URL to"
    echo "    http://127.0.0.1:$(_live_port)"
  fi
}

# Set up the Claude reverse-proxy track (reverse:anthropic, no cert). Conditional on selection.
_install_reverse() { _sv_enable reverse; }

# Tear down the reverse track (claude not selected). Keeps captured data.
_remove_reverse() {
  if _unit_installed; then _sv_disable reverse; echo "claude not selected — removed (data kept)"; fi
}

# Pick which agents to capture → sets the global SELECTED_AGENTS ("claude" or "claude codex").
# Precedence: explicit env (for curl|bash / CI) → interactive prompt on the controlling terminal
# → auto-detect default. We set a global (not echo via $(...)): command substitution makes fd 1
# a pipe, which would defeat any tty check and capture the menu text. We probe /dev/tty by
# actually opening it — robust under `curl … | bash` (stdin is the piped script) and safe under
# set -e when no terminal exists (no half-written prompt, no aborting redirection).
SELECTED_AGENTS=""
_choose_agents() {
  local v="${COST_XRAY_AGENTS:-}"
  if [ -n "$v" ]; then
    case "$v" in [Aa]ll|ALL) SELECTED_AGENTS="claude codex" ;; *) SELECTED_AGENTS="${v//,/ }" ;; esac
    return
  fi
  if [ "${COST_XRAY_NO_CODEX:-}" = 1 ]; then SELECTED_AGENTS="claude"; return; fi
  if (exec 3</dev/tty) 2>/dev/null; then          # a controlling terminal is available → prompt
    { echo "cost-xray: capture which agent(s)?"
      echo "  1) Claude Code   (reverse proxy, no certificate)"
      echo "  2) Codex         (forward proxy + a scoped local mitm CA, used only by the codex command)"
      echo "  3) All           (both)"
      printf 'choose [3]: '; } > /dev/tty
    local ans=""; read -r ans < /dev/tty || ans=""
    [ -n "$ans" ] || ans=3
    case "$ans" in
      1|claude|"claude code")  SELECTED_AGENTS="claude" ;;
      2|codex)                 SELECTED_AGENTS="codex" ;;
      *)                       SELECTED_AGENTS="claude codex" ;;   # 3 / all / Enter / anything else
    esac
    return
  fi
  # non-interactive, no override: default to all, but only codex if it's actually installed
  local default="claude codex"; command -v codex >/dev/null 2>&1 || default="claude"
  SELECTED_AGENTS="$default"
}

# Build the codex CA bundle = system root store + the local mitmproxy CA. The wrapper trusts
# this so that, routed through the forward proxy, codex validates mitm-re-encrypted certs for
# EVERY host (chatgpt.com, MCP, web search); the system roots keep any direct TLS working too.
# Always rebuilt on install, so a regenerated mitm CA can never drift out of sync.
_build_ca_bundle() {
  local mitm_ca="$HOME/.mitmproxy/mitmproxy-ca-cert.pem" i=0 sys=""
  # mitmdump writes the CA on first start; the codex service was just started — wait for it.
  while [ ! -f "$mitm_ca" ] && [ "$i" -lt 20 ]; do sleep 0.5; i=$((i+1)); done
  if [ ! -f "$mitm_ca" ]; then
    echo "  warn: $mitm_ca not generated yet — re-run ./run.sh install once the codex proxy is up" >&2
    return 0
  fi
  for c in /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt; do
    [ -f "$c" ] && { sys="$c"; break; }
  done
  [ -n "$sys" ] || sys="$("$PYTHON" -c 'import certifi;print(certifi.where())' 2>/dev/null || true)"
  if [ -n "$sys" ] && [ -f "$sys" ]; then
    cat "$sys" "$mitm_ca" > "$CA_BUNDLE"
    echo "built codex CA bundle $CA_BUNDLE (system roots + mitm CA)"
  else
    cat "$mitm_ca" > "$CA_BUNDLE"
    echo "  warn: no system CA bundle found; wrote mitm-only $CA_BUNDLE (direct TLS may fail)" >&2
  fi
}

# Set up the Codex forward-proxy track: pin its port, write+start its supervised unit, and
# (re)build the scoped CA bundle. Called only when codex is among the selected agents.
_install_codex() {  # $1 = mitmdump path (unused here; kept for symmetry/future)
  grep -q '^CODEX_PORT=' "$ENVFILE" 2>/dev/null || { echo "CODEX_PORT=$DEFAULT_CODEX_PORT" >> "$ENVFILE"
    echo "added CODEX_PORT=$DEFAULT_CODEX_PORT to $ENVFILE"; }
  _sv_enable codex
  _build_ca_bundle   # waits for the codex proxy to generate ~/.mitmproxy CA, then bundles it
}

# Tear down any prior codex setup (claude-only install is authoritative). Keeps captured data.
_remove_codex() {
  if _codex_unit_installed; then _sv_disable codex; echo "codex not selected — removed (data kept)"; fi
}

# Tear down a materializer poll-daemon unit left by an older version. Materialize is now event-driven
# (the capture proxy spawns a one-shot sweep per turn — addon.py), so there is no service to install.
_remove_mat() {
  if _mat_unit_installed; then
    systemctl --user disable --now "$MAT_SERVICE" 2>/dev/null || true
    rm -f "$MAT_UNIT"; systemctl --user daemon-reload 2>/dev/null || true
    echo "removed old $MAT_SERVICE (materialize is now per-turn; data kept)"
  fi
}

uninstall_service() {
  if _unit_installed; then _sv_disable reverse; echo "removed claude proxy"; fi
  if _codex_unit_installed; then _sv_disable codex; echo "removed codex proxy"; fi
  _remove_mat                                        # also drop an old poll-daemon unit if present
  { [ "$_SV" = systemd ] && systemctl --user daemon-reload 2>/dev/null; } || true
  _remove_shell
  echo "Uninstalled. Captured data kept at $STATE  (rm -rf to clear)."
}

# open the TUI: prefer the mouse Textual app, fall back to the rich one if textual isn't installed
_run_tui() {
  if "$PYTHON" -c 'import textual' 2>/dev/null; then
    exec env PYTHONPATH="$HERE" "$PYTHON" -m cost_xray.tui_app
  fi
  exec env PYTHONPATH="$HERE" "$PYTHON" -m cost_xray.tui
}

case "${1:-}" in
  install)         install_service ;;
  uninstall)       uninstall_service ;;
  start)           start ;;
  stop)            stop ;;
  status)          status ;;
  restart)         _stop_services; start ;;   # start clears the pause marker → resumes monitoring
  _serve)          _serve ;;            # internal: systemd/nohup entrypoint (reverse:anthropic)
  _serve_codex)    _serve_codex ;;      # internal: systemd/nohup entrypoint (codex forward proxy)
  tui)             _run_tui ;;
  ""|run)          start; echo; echo "Live breakdown (Ctrl-C detaches; proxies stay up):"; echo
                   _run_tui ;;
  *) echo "usage: $0 {install|uninstall|start|stop|restart|status|tui}" >&2; exit 2 ;;
esac
