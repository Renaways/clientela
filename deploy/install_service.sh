#!/bin/bash
# Installs Clientela as a per-user launchd agent, so it starts on login and
# stays running at http://127.0.0.1:8000 without a terminal open.
#
# This script prepares and copies the plist but deliberately does NOT call
# `launchctl load` for you — review the generated plist, then run the
# printed command yourself when you're ready to activate it.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"
APP_PY="$PROJECT_DIR/app.py"
PLIST_LABEL="com.clientela.app"
DEST="$HOME/Library/LaunchAgents/$PLIST_LABEL.plist"

if [ ! -x "$PYTHON_BIN" ]; then
  echo "error: $PYTHON_BIN not found — create the venv first: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

mkdir -p "$PROJECT_DIR/data/logs"
mkdir -p "$HOME/Library/LaunchAgents"

sed \
  -e "s#__PYTHON_BIN__#$PYTHON_BIN#g" \
  -e "s#__APP_PY__#$APP_PY#g" \
  -e "s#__PROJECT_DIR__#$PROJECT_DIR#g" \
  "$SCRIPT_DIR/com.clientela.app.plist" > "$DEST"

echo "Wrote $DEST"
echo
echo "To activate it now, run:"
echo "  launchctl load $DEST"
echo
echo "To stop/remove it later:"
echo "  launchctl unload $DEST"
