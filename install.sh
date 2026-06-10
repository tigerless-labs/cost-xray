#!/usr/bin/env bash
set -euo pipefail

REPO="${COST_XRAY_REPO:-https://github.com/tigerless-labs/cost-xray.git}"
DEST="${COST_XRAY_HOME:-$HOME/.local/share/cost-xray}"

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

"$UV" venv --python 3.12 .venv
"$UV" pip install --python .venv/bin/python -q -r requirements.txt
"$UV" pip install --python .venv/bin/python -q -e '.[tui]' 2>/dev/null \
  || "$UV" pip install --python .venv/bin/python -q -e . 2>/dev/null || true

echo "==> installing service + shell wrapper + skill"
exec ./run.sh install
