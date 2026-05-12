#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$DIR/playlist_cover_helper_web.py"
HOST="127.0.0.1"
PORT="8765"
URL="http://${HOST}:${PORT}/"

if pgrep -f "$SCRIPT" >/dev/null 2>&1; then
  echo "CoverFix is already running at ${URL}"
  exit 0
fi

if lsof -iTCP:"${PORT}" -sTCP:LISTEN -n -P >/dev/null 2>&1; then
  echo "Port ${PORT} is already in use by another process."
  echo "Release the port first, then run ./run.sh again."
  lsof -iTCP:"${PORT}" -sTCP:LISTEN -n -P
  exit 1
fi

exec python3 "$SCRIPT"
