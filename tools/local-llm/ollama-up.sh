#!/usr/bin/env bash
# Start a detached `ollama serve` with the local-LLM settings, unless one is already answering.
# Usage: ollama-up.sh [--quiet]
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=config.sh
source "$DIR/config.sh"

QUIET=0
if [[ "${1:-}" == "--quiet" ]]; then QUIET=1; fi
say() { if [[ $QUIET -eq 0 ]]; then echo "$@"; fi; }

LOG="$LOCAL_LLM_STATE_DIR/ollama.log"
PIDFILE="$LOCAL_LLM_STATE_DIR/ollama.pid"
OLLAMA_BIN="${OLLAMA_BIN:-$(command -v ollama || echo /usr/local/bin/ollama)}"

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

if curl -sf "http://$OLLAMA_HOST/api/version" >/dev/null 2>&1; then
  # Drop a pid file that no longer names a live ollama server.
  if [[ -f "$PIDFILE" ]]; then
    p="$(cat "$PIDFILE" 2>/dev/null || true)"
    if ! { [[ "$p" =~ ^[0-9]+$ ]] && is_ollama_pid "$p"; }; then rm -f "$PIDFILE"; fi
  fi
  say "already running (env set at its start applies)"
  exit 0
fi

if [[ ! -x "$OLLAMA_BIN" ]]; then
  echo "ollama binary not found at $OLLAMA_BIN" >&2
  exit 1
fi

env OLLAMA_HOST="$OLLAMA_HOST" \
    OLLAMA_CONTEXT_LENGTH="$LOCAL_LLM_CTX" \
    OLLAMA_FLASH_ATTENTION=1 \
    OLLAMA_KV_CACHE_TYPE=q8_0 \
    OLLAMA_NUM_PARALLEL=1 \
    OLLAMA_KEEP_ALIVE=30m \
    OLLAMA_MAX_LOADED_MODELS=1 \
    setsid nohup "$OLLAMA_BIN" serve >"$LOG" 2>&1 </dev/null &
echo $! > "$PIDFILE"

for _ in $(seq 1 30); do
  if curl -sf "http://$OLLAMA_HOST/api/version" >/dev/null 2>&1; then
    say "ollama up on $OLLAMA_HOST (pid $(cat "$PIDFILE"))"
    exit 0
  fi
  if ! kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    break
  fi
  sleep 1
done

echo "ollama did not come up on $OLLAMA_HOST within 30 s. Log tail ($LOG):" >&2
tail -n 30 "$LOG" >&2 || true
exit 1
