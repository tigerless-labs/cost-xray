#!/usr/bin/env bash
# One-line installer for cost-xray (proxy daemon + Claude Code skill).
#
#   # remote (clones to ~/.local/share/cost-xray, then installs):
#   curl -fsSL https://raw.githubusercontent.com/tigerless-labs/cost-xray/master/install.sh | bash
#
#   # from a local clone:
#   ./install.sh
#
# Does everything: fetch code -> uv-provisioned venv (its own Python >=3.10) + deps ->
# systemd --user service (proxy, self-adapting port) -> claude() shell wrapper -> /cost-xray skill.
set -euo pipefail

REPO="${COST_XRAY_REPO:-https://github.com/tigerless-labs/cost-xray.git}"
DEST="${COST_XRAY_HOME:-$HOME/.local/share/cost-xray}"

# Are we already running from inside a clone (has run.sh)? else fetch one.
SRC=""
_self="${BASH_SOURCE[0]:-}"
if [ -n "$_self" ] && [ -f "$(dirname "$_self")/run.sh" ]; then
  SRC="$(cd "$(dirname "$_self")" && pwd)"
fi

if [ -z "$SRC" ]; then
  REF="${COST_XRAY_REF:-master}"
  if command -v git >/dev/null 2>&1; then
    if [ -d "$DEST/.git" ]; then
      echo "==> updating $DEST"
      git -C "$DEST" pull --ff-only
    else
      echo "==> cloning $REPO -> $DEST"
      mkdir -p "$(dirname "$DEST")"
      git clone --depth 1 "$REPO" "$DEST"
    fi
  else
    # no git: fetch a source tarball with curl, so git is not a hard dependency. tar strips the
    # archive's top-level <repo>-<ref>/ dir; a re-install replaces DEST (captured data lives
    # elsewhere, under ~/.cost-xray). Override the URL with COST_XRAY_TARBALL if needed.
    command -v curl >/dev/null 2>&1 || { echo "need git or curl to fetch the source" >&2; exit 1; }
    tarball="${COST_XRAY_TARBALL:-${REPO%.git}/archive/refs/heads/$REF.tar.gz}"
    echo "==> git not found; downloading $tarball -> $DEST"
    rm -rf "$DEST"; mkdir -p "$DEST"
    curl -fsSL "$tarball" | tar -xz -C "$DEST" --strip-components=1
  fi
  SRC="$DEST"
fi

cd "$SRC"

echo "==> setting up Python runtime + venv + dependencies (self-bootstrapping via uv)"
# uv is a single self-contained binary (no admin, no system Python needed). It provisions a
# matching Python >=3.10 itself, so the install never depends on the machine's Python being
# present or new enough -- the failure mode on stock macOS (system 3.9, a venv without pip).
UV="$(command -v uv 2>/dev/null || true)"
if [ -z "$UV" ]; then
  for c in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do [ -x "$c" ] && { UV="$c"; break; }; done || true
fi
if [ -z "$UV" ]; then
  echo "==> installing uv (self-contained, no admin)"
  curl -LsSf https://astral.sh/uv/install.sh | sh || true
  for c in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do [ -x "$c" ] && { UV="$c"; break; }; done || true
fi
[ -n "$UV" ] || { echo "uv install failed -- see https://astral.sh/uv" >&2; exit 1; }

# venv with a managed Python >=3.10 (reuses a matching system one if present, else downloads it).
"$UV" venv --python 3.12 .venv
"$UV" pip install --python .venv/bin/python -q -r requirements.txt
# the [tui] extra (textual) powers the mouse TUI behind `cx`; fall back gracefully if it fails
"$UV" pip install --python .venv/bin/python -q -e '.[tui]' 2>/dev/null \
  || "$UV" pip install --python .venv/bin/python -q -e . 2>/dev/null || true

echo "==> installing service + shell wrapper + skill"
exec ./run.sh install
