#!/usr/bin/env bash
# Stop the ollama server started by ollama-up.sh (pid from ollama.pid in the state dir), then remove the pid file.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=config.sh
source "$DIR/config.sh"

PIDFILE="$LOCAL_LLM_STATE_DIR/ollama.pid"

# True when pid is an ollama server: comm is ollama, or argv[0] is ollama and the command line has `serve`.
is_ollama_pid() {
  local p="$1" comm a
  local -a argv=()
  comm="$(cat "/proc/$p/comm" 2>/dev/null || true)"
  if [[ "$comm" == ollama ]]; then return 0; fi
  mapfile -d '' -t argv <"/proc/$p/cmdline" 2>/dev/null || return 1
  [[ "$(basename -- "${argv[0]:-}")" == ollama ]] || return 1
  for a in ${argv[@]+"${argv[@]}"}; do
    if [[ "$a" == serve ]]; then return 0; fi
  done
  return 1
}

if [[ ! -f "$PIDFILE" ]]; then
  echo "no pid file at $PIDFILE; nothing to stop"
  exit 0
fi

pid="$(cat "$PIDFILE")"
if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
  if ! [[ "$pid" =~ ^[0-9]+$ ]] || ! is_ollama_pid "$pid"; then
    echo "stale pid file, not killing (pid $pid is not ollama)"
    rm -f "$PIDFILE"
    exit 0
  fi
  kill "$pid"
  for _ in $(seq 1 10); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
  echo "stopped ollama (pid $pid)"
else
  echo "pid ${pid:-?} not running"
fi
rm -f "$PIDFILE"
