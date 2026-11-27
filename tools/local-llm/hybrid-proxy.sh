#!/usr/bin/env bash
# Start, stop or inspect the router (hybrid-proxy.py) used by claude-route and claude-hybrid.
# Usage: hybrid-proxy.sh up|down|status [--quiet]
#   up      start the router unless one already answers with the expected local_model; waits up to 10 s.
#           With the systemd user unit installed (see hybrid-proxy.service), the unit is started instead.
#   down    stop the router whose pid is in the state dir, only if that process is hybrid-proxy.py
#   status  print running / not running and the health JSON (exit 1 when not running)
# Log and pid files live in LOCAL_LLM_STATE_DIR (one log line per request; never headers or bodies).
# HYBRID_UNIT names the systemd unit (default hybrid-proxy); set it empty to ignore systemd.
set -euo pipefail

DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck source=config.sh
source "$DIR/config.sh"

PY="$DIR/hybrid-proxy.py"
PIDFILE="$LOCAL_LLM_STATE_DIR/hybrid-proxy.pid"
LOG="$LOCAL_LLM_STATE_DIR/hybrid-proxy.log"
UNIT="${HYBRID_UNIT-hybrid-proxy}"

ACTION="${1:-}"
QUIET=0
for a in "${@:2}"; do
  case "$a" in
    --quiet) QUIET=1 ;;
    *) echo "unknown argument: $a" >&2; exit 2 ;;
  esac
done
say() { if [[ $QUIET -eq 0 ]]; then echo "$@"; fi; }

unit_installed() {
  [[ -n "$UNIT" && -f "$HOME/.config/systemd/user/$UNIT.service" ]] && command -v systemctl >/dev/null 2>&1
}

health() { curl -sf -m 2 "$HYBRID_URL/_hybrid/health"; }
# Something answers the health endpoint (any model).
answering() { health >/dev/null 2>&1; }
# The router answers ok AND reports the local model this config expects.
healthy() {
  local body
  body="$(health 2>/dev/null | tr -d ' \n')" || return 1
  [[ "$body" == *'"ok":true'* && "$body" == *"\"local_model\":\"$LOCAL_LLM_MODEL\""* ]]
}
wait_healthy() {
  local i
  for i in $(seq 1 20); do
    if healthy; then return 0; fi
    sleep 0.5
  done
  return 1
}

# True when pid is a live process whose argv is exactly: python3 -I <this repo's hybrid-proxy.py>.
is_proxy_pid() {
  [[ "$1" =~ ^[0-9]+$ ]] || return 1
  local -a argv=()
  mapfile -d '' -t argv <"/proc/$1/cmdline" 2>/dev/null || return 1
  [[ ${#argv[@]} -eq 3 && "${argv[0]##*/}" == python3* && "${argv[1]}" == "-I" && "${argv[2]}" == "$PY" ]]
}

read_pid() {
  if [[ -f "$PIDFILE" ]]; then cat "$PIDFILE" 2>/dev/null || true; fi
}

up() {
  if answering; then
    if healthy; then say "hybrid proxy already answering on $HYBRID_URL"; return 0; fi
    echo "something on $HYBRID_URL answers but does not report local_model $LOCAL_LLM_MODEL. Stop it or set LOCAL_LLM_MODEL to match." >&2
    exit 1
  fi
  if unit_installed; then
    if ! systemctl --user start "$UNIT"; then echo "systemctl --user start $UNIT failed" >&2; exit 1; fi
    if wait_healthy; then say "hybrid proxy up on $HYBRID_URL (systemd unit $UNIT)"; return 0; fi
    echo "unit $UNIT started but $HYBRID_URL did not report local_model $LOCAL_LLM_MODEL within 10 s. Log tail ($LOG):" >&2
    tail -n 30 "$LOG" >&2 || true
    exit 1
  fi
  if [[ ! -f "$PY" ]]; then echo "missing $PY" >&2; exit 1; fi
  local pid i
  setsid nohup python3 -I "$PY" >>"$LOG" 2>&1 </dev/null &
  pid=$!
  echo "$pid" >"$PIDFILE"
  for i in $(seq 1 20); do
    if healthy; then say "hybrid proxy up on $HYBRID_URL (pid $pid)"; return 0; fi
    if ! kill -0 "$pid" 2>/dev/null; then break; fi
    sleep 0.5
  done
  echo "hybrid proxy did not report local_model $LOCAL_LLM_MODEL on $HYBRID_URL within 10 s. Log tail ($LOG):" >&2
  tail -n 30 "$LOG" >&2 || true
  kill "$pid" 2>/dev/null || true
  rm -f "$PIDFILE"
  exit 1
}

down() {
  if unit_installed; then
    systemctl --user stop "$UNIT" || true
    say "hybrid proxy unit $UNIT stopped"
    return 0
  fi
  if [[ ! -f "$PIDFILE" ]]; then
    say "no pid file at $PIDFILE; nothing to stop"
    return 0
  fi
  local pid
  pid="$(read_pid)"
  if is_proxy_pid "$pid"; then
    kill -TERM "$pid" 2>/dev/null || true
    for _ in $(seq 1 10); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.5
    done
    if kill -0 "$pid" 2>/dev/null; then kill -KILL "$pid" 2>/dev/null || true; fi
    rm -f "$PIDFILE"
    say "hybrid proxy stopped (pid $pid)"
  else
    rm -f "$PIDFILE"
    say "pid file was stale (pid ${pid:-none} is not hybrid-proxy.py); removed"
  fi
}

status() {
  local pid state
  if unit_installed; then
    local active
    active="$(systemctl --user is-active "$UNIT" 2>/dev/null || true)"
    echo "hybrid proxy: unit $UNIT is $active"
    if answering; then health; echo; fi
    [[ "$active" == "active" ]] && return 0
    return 1
  fi
  pid="$(read_pid)"
  if is_proxy_pid "$pid"; then
    state="running (pid $pid)"
  elif answering; then
    state="running (pid unknown; answering on $HYBRID_URL)"
  else
    state="not running"
  fi
  echo "hybrid proxy: $state"
  if answering; then health; echo; fi
  [[ "$state" == "not running" ]] && return 1
  return 0
}

case "$ACTION" in
  up) up ;;
  down) down ;;
  status) status ;;
  *) echo "usage: hybrid-proxy.sh up|down|status [--quiet]" >&2; exit 2 ;;
esac
