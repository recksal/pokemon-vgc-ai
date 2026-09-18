#!/usr/bin/env bash
set -euo pipefail

SHOWDOWN_REPO="${VGC_SHOWDOWN_REPO:-/workspaces/pokemon-showdown}"
SHOWDOWN_SHA="efe494857"

echo "==> Installing uv and project dependencies"
python -m pip install --disable-pip-version-check uv
uv sync --extra dev

if [ ! -d "$SHOWDOWN_REPO/.git" ]; then
  echo "==> Cloning pinned Pokemon Showdown checkout"
  git clone --filter=blob:none https://github.com/smogon/pokemon-showdown.git "$SHOWDOWN_REPO"
fi

echo "==> Pinning Pokemon Showdown to $SHOWDOWN_SHA"
git -C "$SHOWDOWN_REPO" fetch --depth=1 origin "$SHOWDOWN_SHA"
git -C "$SHOWDOWN_REPO" checkout --detach "$SHOWDOWN_SHA"

echo "==> Installing/building Pokemon Showdown"
(
  cd "$SHOWDOWN_REPO"
  npm ci
  npm run build
)

echo
echo "Codespace setup complete."
echo "Showdown repo: $SHOWDOWN_REPO"
echo "Run the readiness check with:"
echo "  .venv/bin/python offline/check_remote_analyzer_ready.py"
