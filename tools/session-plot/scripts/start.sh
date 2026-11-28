#!/usr/bin/env bash
# Start session-plot on 127.0.0.1:$PORT (default 4395), detached, then wait for /live to answer.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${PORT:-4395}"
LOG="${SESSION_PLOT_LOG:-$HOME/.local/state/session-plot/server.log}"
mkdir -p "$(dirname "$LOG")"

if ss -ltn | grep -q ":$PORT "; then
  echo "already listening on :$PORT"; ss -ltnp | grep ":$PORT " || true; exit 0
fi

cd "$DIR"
PORT="$PORT" setsid nohup npx tsx server.ts >>"$LOG" 2>&1 </dev/null &
echo "launched (shell pid $!), log $LOG"

for _ in $(seq 1 15); do
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/live" || true)
  if [ "$code" = "200" ]; then
    echo "OK: http://127.0.0.1:$PORT/live (HTTP 200)"
    ss -ltnp | grep ":$PORT " || true
    exit 0
  fi
  sleep 1
done
echo "FAILED: no HTTP 200 after 15 s; last log lines:"; tail -20 "$LOG"; exit 1
