#!/usr/bin/env bash
# Start Subtle on Linux or macOS (after ./install.sh). The window opens; closing it keeps Subtle in the tray.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$ROOT/runtime/venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "Subtle isn't installed yet: run ./install.sh first."
  exit 1
fi
cd "$ROOT/server"
exec "$PY" supervisor.py "$@"
