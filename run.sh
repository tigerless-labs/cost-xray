#!/usr/bin/env bash
set -euo pipefail
: "${USER:=$(id -un)}"

DEFAULT_PORT="${PORT:-8788}"
DEFAULT_UPSTREAM="${UPSTREAM:-https://api.anthropic.com}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="$HERE/run.sh"
PYTHON="$HERE/.venv/bin/python"; [ -x "$PYTHON" ] || PYTHON="python3"
VENV_MITM="$HERE/.venv/bin/mitmdump"
STATE="${HOME}/.cost-xray"
ENVFILE="${STATE}/env"
PORTFILE="${STATE}/port"
PIDFILE="${STATE}/proxy.pid"
LOGFILE="${STATE}/proxy.log"
UNIT="${HOME}/.config/systemd/user/cost-xray.service"
SERVICE="cost-xray.service"
DEFAULT_CODEX_PORT="${CODEX_PORT:-8789}"
CODEX_PORTFILE="${STATE}/codex-port"
CODEX_PIDFILE="${STATE}/codex-proxy.pid"
CODEX_LOGFILE="${STATE}/codex-proxy.log"
CODEX_UNIT="${HOME}/.config/systemd/user/cost-xray-codex.service"
CODEX_SERVICE="cost-xray-codex.service"
CA_BUNDLE="${STATE}/codex-ca-bundle.pem"
MAT_UNIT="${HOME}/.config/systemd/user/cost-xray-materializer.service"
MAT_SERVICE="cost-xray-materializer.service"
PAUSEFILE="${STATE}/paused"
REPOFILE="${STATE}/repo"
MARK_BEGIN="# >>> cost-xray >>>"
MARK_END="# <<< cost-xray <<<"
mkdir -p "$STATE"

LAUNCH_DIR="${HOME}/Library/LaunchAgents"
LABEL="ai.tigerless.cost-xray"
CODEX_LABEL="ai.tigerless.cost-xray-codex"
PLIST="${LAUNCH_DIR}/${LABEL}.plist"
CODEX_PLIST="${LAUNCH_DIR}/${CODEX_LABEL}.plist"
if [ "$(uname -s)" = "Darwin" ] && command -v launchctl >/dev/null 2>&1; then
  _SV=launchd
elif systemctl --user show-environment >/dev/null 2>&1; then
  _SV=systemd
else
  _SV=none
fi

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

_pick_port() {
  "$PYTHON" - "${1:-$DEFAULT_PORT}" <<'PY'
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

_ld_domain() {
  if launchctl print "gui/$(id -u)" >/dev/null 2>&1; then printf 'gui/%s' "$(id -u)"
  else printf 'user/%s' "$(id -u)"; fi
}

_systemd_write() {
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

_launchd_write() {
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

_listening() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
_kind_port() { case "$1" in reverse) _live_port ;; codex) _codex_live_port ;; esac; }
_kind_start_manual() {
  case "$1" in
    reverse) _start_manual "claude reverse proxy" _serve "$PIDFILE" "$LOGFILE" ;;
    codex)   _start_manual "codex forward proxy"  _serve_codex "$CODEX_PIDFILE" "$CODEX_LOGFILE" ;;
  esac
}
_kind_stop_manual() {
  case "$1" in
    reverse) _stop_manual "claude reverse proxy" "$PIDFILE" ;;
    codex)   _stop_manual "codex forward proxy"  "$CODEX_PIDFILE" ;;
  esac
}

_launchd_start() {
  launchctl bootout "$(_ld_domain)/$(_k_label "$1")" 2>/dev/null || true
  if launchctl bootstrap "$(_ld_domain)" "$(_k_plist "$1")" 2>/dev/null; then return 0; fi
  _kind_start_manual "$1"
}

_sv_enable() {
  case "$_SV" in
    launchd) _launchd_write "$1"; _kind_stop_manual "$1" >/dev/null 2>&1 || true
             _launchd_start "$1" ;;
    systemd) _systemd_write "$1"; systemctl --user daemon-reload
             systemctl --user enable "$(_k_svc "$1")"
             systemctl --user restart "$(_k_svc "$1")" ;;
  esac
}

_sv_disable() {
  case "$_SV" in
    launchd) launchctl bootout "$(_ld_domain)/$(_k_label "$1")" 2>/dev/null || true
             _kind_stop_manual "$1" >/dev/null 2>&1 || true
             rm -f "$(_k_plist "$1")" ;;
    systemd) systemctl --user disable --now "$(_k_svc "$1")" 2>/dev/null || true
             rm -f "$(_k_unit "$1")"; systemctl --user daemon-reload 2>/dev/null || true ;;
  esac
}

_sv_start() {
  case "$_SV" in
    launchd) _launchd_start "$1" ;;
    systemd) systemctl --user start "$(_k_svc "$1")" ;;
  esac
}

_sv_stop() {
  case "$_SV" in
    launchd) launchctl bootout "$(_ld_domain)/$(_k_label "$1")" 2>/dev/null || true
             _kind_stop_manual "$1" >/dev/null 2>&1 || true ;;
    systemd) systemctl --user stop "$(_k_svc "$1")" ;;
  esac
}

_sv_is_active() {
  case "$_SV" in
    launchd) if _listening "$(_kind_port "$1")"; then echo active; else echo inactive; fi ;;
    systemd) systemctl --user is-active "$(_k_svc "$1")" 2>/dev/null || echo unknown ;;
    *) echo unknown ;;
  esac
}

_sv_installed() {
  case "$_SV" in
    launchd) [ -f "$(_k_plist "$1")" ] ;;
    systemd) [ -f "$(_k_unit "$1")" ] ;;
    *) return 1 ;;
  esac
}

_unit_installed() { _sv_installed reverse; }
_codex_unit_installed() { _sv_installed codex; }

_running() {
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

_mat_unit_installed() { [ -f "$MAT_UNIT" ]; }

_live_port() { cat "$PORTFILE" 2>/dev/null || echo "$DEFAULT_PORT"; }
_codex_live_port() { cat "$CODEX_PORTFILE" 2>/dev/null || echo "$DEFAULT_CODEX_PORT"; }

_start_manual() {
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

_stop_manual() {
  local label="$1" pidfile="$2" pid
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true; rm -f "$pidfile"
    echo "$label: stopped (was pid $pid)."
  else
    rm -f "$pidfile"; echo "$label: not running."
  fi
}

start() {
  rm -f "$PAUSEFILE"
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

stop() {
  _stop_services
  touch "$PAUSEFILE"
  echo "Monitoring stopped — claude/codex now run DIRECT (uncaptured); the wrappers won't restart"
  echo "the proxy until you resume.  Resume capture:  cx start"
}

_status_one() {
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

_inject_shell() {
  local inc_claude="${1:-0}" inc_codex="${2:-0}"
  touch "$RC"
  if grep -qF "$MARK_BEGIN" "$RC" 2>/dev/null; then
    sed "/$MARK_BEGIN/,/$MARK_END/d" "$RC" > "$RC.cx.tmp" && mv "$RC.cx.tmp" "$RC"
  fi
  printf '\n%s\n' "$MARK_BEGIN" >> "$RC"
  cat >> "$RC" <<'CTXRAY_COMMON'
_ctxray_listening() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
_ctxray_up() {
  [ -n "${CX_OFF:-}" ] && return 1
  [ -e "$HOME/.cost-xray/paused" ] && return 1
  _ctxray_listening "$1" && return 0
  if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    systemctl --user start "$2" 2>/dev/null || true
  else
    local repo; repo="$(cat "$HOME/.cost-xray/repo" 2>/dev/null)"
    [ -n "$repo" ] && bash "$repo/run.sh" start >/dev/null 2>&1 || true
  fi
  local i=0; while [ "$i" -lt 8 ]; do _ctxray_listening "$1" && return 0; sleep 0.25; i=$((i+1)); done
  return 1
}
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
claude() {
  local s="$HOME/.cost-xray" p; p="$(cat "$s/port" 2>/dev/null || echo 8788)"
  if _ctxray_up "$p" cost-xray.service; then
    ANTHROPIC_BASE_URL="http://127.0.0.1:$p" command claude "$@"
  else
    command claude "$@"
  fi
}
CLAUDEBLOCK
  fi
  if [ "$inc_codex" = 1 ]; then
    cat >> "$RC" <<'CODEXBLOCK'
codex() {
  local s="$HOME/.cost-xray" p; p="$(cat "$s/codex-port" 2>/dev/null || echo 8789)"
  local ca="$s/codex-ca-bundle.pem"
  if [ -f "$ca" ] && _ctxray_up "$p" cost-xray-codex.service; then
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
    sed "/$MARK_BEGIN/,/$MARK_END/d" "$RC" > "$RC.cx.tmp" && mv "$RC.cx.tmp" "$RC"
    echo "removed cost-xray block from $RC"
  fi
}

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
  if [ ! -f "$ENVFILE" ]; then
    { echo "UPSTREAM=$DEFAULT_UPSTREAM"; echo "MITMDUMP=$md"; echo "PORT=$DEFAULT_PORT"
      echo "CODEX_PORT=$DEFAULT_CODEX_PORT"; } > "$ENVFILE"
    echo "wrote config $ENVFILE"
  fi
  rm -f "$PAUSEFILE"
  printf '%s' "$HERE" > "$REPOFILE"

  local claude=0 codex=0
  _choose_agents
  case " $SELECTED_AGENTS " in *" claude "*) claude=1 ;; esac
  case " $SELECTED_AGENTS " in *" codex "*)  codex=1 ;; esac
  if [ "$claude" = 1 ]; then _install_reverse; else _remove_reverse; fi
  if [ "$codex"  = 1 ]; then _install_codex "$md"; else _remove_codex; fi
  _remove_mat

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

_install_reverse() { _sv_enable reverse; }

_remove_reverse() {
  if _unit_installed; then _sv_disable reverse; echo "claude not selected — removed (data kept)"; fi
}

SELECTED_AGENTS=""
_choose_agents() {
  local v="${COST_XRAY_AGENTS:-}"
  if [ -n "$v" ]; then
    case "$v" in [Aa]ll|ALL) SELECTED_AGENTS="claude codex" ;; *) SELECTED_AGENTS="${v//,/ }" ;; esac
    return
  fi
  if [ "${COST_XRAY_NO_CODEX:-}" = 1 ]; then SELECTED_AGENTS="claude"; return; fi
  if (exec 3</dev/tty) 2>/dev/null; then
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
      *)                       SELECTED_AGENTS="claude codex" ;;
    esac
    return
  fi
  local default="claude codex"; command -v codex >/dev/null 2>&1 || default="claude"
  SELECTED_AGENTS="$default"
}

_build_ca_bundle() {
  local mitm_ca="$HOME/.mitmproxy/mitmproxy-ca-cert.pem" i=0 sys=""
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

_install_codex() {
  grep -q '^CODEX_PORT=' "$ENVFILE" 2>/dev/null || { echo "CODEX_PORT=$DEFAULT_CODEX_PORT" >> "$ENVFILE"
    echo "added CODEX_PORT=$DEFAULT_CODEX_PORT to $ENVFILE"; }
  _sv_enable codex
  _build_ca_bundle
}

_remove_codex() {
  if _codex_unit_installed; then _sv_disable codex; echo "codex not selected — removed (data kept)"; fi
}

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
  _remove_mat
  { [ "$_SV" = systemd ] && systemctl --user daemon-reload 2>/dev/null; } || true
  _remove_shell
  echo "Uninstalled. Captured data kept at $STATE  (rm -rf to clear)."
}

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
  restart)         _stop_services; start ;;
  _serve)          _serve ;;
  _serve_codex)    _serve_codex ;;
  tui)             _run_tui ;;
  ""|run)          start; echo; echo "Live breakdown (Ctrl-C detaches; proxies stay up):"; echo
                   _run_tui ;;
  *) echo "usage: $0 {install|uninstall|start|stop|restart|status|tui}" >&2; exit 2 ;;
esac
