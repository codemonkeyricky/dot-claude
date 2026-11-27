#!/usr/bin/env bash
# Find and stop Claude Code background processes so that new background jobs do not start
# with settings from another mode. Only the Claude Code daemon ("claude daemon run") carries
# sticky settings for new jobs, so by default the target set is the daemon tree: each daemon
# plus all of its descendants (bg-pty-host, bg-spare, background job sessions).
# Interactive sessions, the chrome native host and other claude processes are NOT listed unless --all.
#
# Always excluded: this script, every ancestor of this script (up to PID 1), and any process
# (or daemon subtree) whose args or environment contain CLAUDE_LOCAL_KEEP.
#
# Usage: claude-kill-bg.sh [--dry-run] [--yes] [--count] [--all] [--stale-for local|anthropic|hybrid]
#   (default)          list the daemon-tree targets and ask "kill these N processes? [y/N]"
#   --yes              skip the prompt and kill
#   --dry-run          only list (nothing is killed)
#   --count            print only the number of targets
#   --all              scope = every claude process (old behaviour), still excluding self/ancestors
#   --stale-for MODE   only daemons whose mode is not MODE, plus their subtrees. MODE is local, hybrid or
#                      anthropic. A daemon's mode (from ANTHROPIC_BASE_URL in /proc/<pid>/environ) is hybrid
#                      when it names 127.0.0.1:$HYBRID_PORT, local when it names the Ollama host, else anthropic.
#                      Unreadable environ counts as stale. Cannot be combined with --all.
# Warning: killing the Claude daemon ends any running background Claude jobs.
set -euo pipefail

DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck source=config.sh
source "$DIR/config.sh"

DRY=0
YES=0
COUNT_ONLY=0
ALL=0
STALE_SET=0
STALE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --yes) YES=1 ;;
    --count) COUNT_ONLY=1 ;;
    --all) ALL=1 ;;
    --stale-for)
      [[ $# -ge 2 ]] || { echo "--stale-for needs local, anthropic or hybrid" >&2; exit 2; }
      STALE_SET=1; STALE="$2"; shift ;;
    --stale-for=*) STALE_SET=1; STALE="${1#--stale-for=}" ;;
    -h|--help) awk 'NR == 1 { next } /^#/ { print; next } { exit }' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
if [[ $STALE_SET -eq 1 ]]; then
  case "$STALE" in
    local|anthropic|hybrid) ;;
    *) echo "--stale-for must be local, anthropic or hybrid" >&2; exit 2 ;;
  esac
  if [[ $ALL -eq 1 ]]; then echo "--stale-for and --all cannot be combined" >&2; exit 2; fi
fi

CLAUDE_PATH="$(command -v claude 2>/dev/null || echo "$HOME/.local/bin/claude")"
CLAUDE_REAL="$(readlink -f "$CLAUDE_PATH" 2>/dev/null || echo "$CLAUDE_PATH")"

# Ancestors of this script (self, parent, grandparent, ... up to PID 1).
ancestors=" $$ "
p=$$
while :; do
  pp="$(awk '/^PPid:/{print $2}' "/proc/$p/status" 2>/dev/null || true)"
  if [[ -z "$pp" || "$pp" -le 1 ]]; then break; fi
  ancestors+="$pp "
  p=$pp
done

# Process table: parent, args, is-claude, children.
declare -A PPID_OF=() ARGS_OF=() IS_CLAUDE=() IS_DAEMON=() CHILDREN=()
ALL_PIDS=()
for d in /proc/[0-9]*; do
  pid="${d#/proc/}"
  [[ "$pid" =~ ^[0-9]+$ ]] || continue
  args="$(tr '\0' ' ' 2>/dev/null <"$d/cmdline" || true)"
  [[ -n "$args" ]] || continue
  argv=()
  mapfile -d '' -t argv <"$d/cmdline" 2>/dev/null || true  # NUL-split argv, so only real argv words match
  exe="$(readlink "$d/exe" 2>/dev/null || true)"
  pp=0
  while read -r k v _; do
    if [[ "$k" == "PPid:" ]]; then pp="$v"; break; fi
  done 2>/dev/null <"$d/status" || true
  c=0
  if [[ "$exe" == "$CLAUDE_REAL" || "$exe" == "$HOME/.local/share/claude/versions/"* ]]; then
    c=1
  elif [[ -n "$exe" && "$(basename "$exe")" == node* && "$args" == *claude* ]]; then
    c=1
  fi
  PPID_OF[$pid]="$pp"
  ARGS_OF[$pid]="$args"
  IS_CLAUDE[$pid]="$c"
  # Daemon root: argv[1] is "daemon" (claude binary), or node running a cli.js with "daemon" as argv[2].
  isd=0
  if [[ "${argv[1]:-}" == daemon ]]; then
    isd=1
  elif [[ "$(basename -- "${argv[0]:-}")" == node* && ( "${argv[1]:-}" == *claude-code/cli.js || "${argv[1]:-}" == *claude/cli.js ) && "${argv[2]:-}" == daemon ]]; then
    isd=1
  fi
  IS_DAEMON[$pid]="$isd"
  CHILDREN[$pp]+="$pid "
  ALL_PIDS+=("$pid")
done

is_ancestor() { [[ "$ancestors" == *" $1 "* ]]; }

is_keep() { # CLAUDE_LOCAL_KEEP in args or environment
  [[ "${ARGS_OF[$1]:-}" == *CLAUDE_LOCAL_KEEP* ]] && return 0
  local env
  env="$(tr '\0' '\n' 2>/dev/null <"/proc/$1/environ" || true)"
  grep -q '^CLAUDE_LOCAL_KEEP=' <<<"$env"
}

mode_of() { # prints hybrid, local, anthropic, or unreadable
  local env base host
  if ! env="$(tr '\0' '\n' 2>/dev/null <"/proc/$1/environ")" || [[ -z "$env" ]]; then
    echo unreadable; return
  fi
  base="$(grep '^ANTHROPIC_BASE_URL=' <<<"$env" | head -n1 | cut -d= -f2- || true)"
  host="${OLLAMA_HOST:-}"
  case "$base" in
    "http://127.0.0.1:$HYBRID_PORT"|"http://127.0.0.1:$HYBRID_PORT"/*|"http://localhost:$HYBRID_PORT"|"http://localhost:$HYBRID_PORT"/*) echo hybrid; return ;;
    *127.0.0.1:11434*|*localhost:11434*) echo local; return ;;
  esac
  if [[ -n "$host" && "$base" == *"$host"* ]]; then echo local; else echo anthropic; fi
}

# Daemons (roots of the scope). Their mode is reported; with --stale-for only stale ones are roots.
roots=()
daemon_lines=()
for pid in ${ALL_PIDS[@]+"${ALL_PIDS[@]}"}; do
  [[ "${IS_CLAUDE[$pid]}" == 1 && "${IS_DAEMON[$pid]:-0}" == 1 ]] || continue
  m="$(mode_of "$pid")"
  stale=no
  if [[ "$m" != "anthropic" && "$m" != "local" ]]; then stale=yes; fi
  if [[ $STALE_SET -eq 1 ]]; then
    [[ "$m" == "$STALE" ]] && stale=no || stale=yes
    [[ "$stale" == yes ]] || continue
  fi
  roots+=("$pid")
  daemon_lines+=("daemon pid $pid: mode $m")
done

raw=()
queue=("${roots[@]+"${roots[@]}"}")
declare -A SEEN=()
i=0
while (( i < ${#queue[@]} )); do
  pid="${queue[$i]}"; i=$((i + 1))
  [[ -z "${SEEN[$pid]:-}" ]] || continue
  SEEN[$pid]=1
  if ! is_ancestor "$pid" && is_keep "$pid"; then continue; fi  # keep-marked subtree is left alone
  raw+=("$pid")
  for ch in ${CHILDREN[$pid]:-}; do queue+=("$ch"); done
done
if [[ $ALL -eq 1 ]]; then  # --all: also every other claude process (interactive, chrome host, ...)
  for pid in ${ALL_PIDS[@]+"${ALL_PIDS[@]}"}; do
    [[ "${IS_CLAUDE[$pid]}" == 1 && -z "${SEEN[$pid]:-}" ]] && raw+=("$pid")
  done
fi

targets=()
target_etime=()
target_args=()
excluded_ancestor=0
excluded_keep=0
declare -A IN_T=()
for pid in ${raw[@]+"${raw[@]}"}; do
  if is_ancestor "$pid"; then excluded_ancestor=$((excluded_ancestor + 1)); continue; fi
  if is_keep "$pid"; then excluded_keep=$((excluded_keep + 1)); continue; fi
  targets+=("$pid")
  IN_T[$pid]=1
  target_etime+=("$(ps -o etime= -p "$pid" 2>/dev/null | tr -d ' ' || echo '?')")
  target_args+=("${ARGS_OF[$pid]:0:160}")
done
n=${#targets[@]}

# Claude processes outside the scope (interactive sessions, chrome native host, ...), left alone.
left=0
for pid in ${ALL_PIDS[@]+"${ALL_PIDS[@]}"}; do
  [[ "${IS_CLAUDE[$pid]}" == 1 ]] || continue
  is_ancestor "$pid" && continue
  [[ -n "${IN_T[$pid]:-}" ]] && continue
  left=$((left + 1))
done

if [[ $COUNT_ONLY -eq 1 ]]; then
  echo "$n"
  exit 0
fi

if [[ $ALL -eq 1 ]]; then
  scope="all claude processes"
elif [[ $STALE_SET -eq 1 ]]; then
  scope="daemon tree, daemons not in $STALE mode"
else
  scope="daemon tree"
fi
for line in "${daemon_lines[@]+"${daemon_lines[@]}"}"; do echo "  $line"; done
echo "Claude Code processes to stop: $n (scope: $scope; excluded: $excluded_ancestor ancestors/self, $excluded_keep CLAUDE_LOCAL_KEEP, $left claude processes outside scope left alone)"
for ((i = 0; i < ${#targets[@]}; i++)); do
  printf '  pid %-7s etime %-12s %s\n' "${targets[$i]}" "${target_etime[$i]}" "${target_args[$i]}"
done

if [[ $n -eq 0 ]]; then
  echo "nothing to stop"
  exit 0
fi

if [[ $DRY -eq 1 ]]; then
  echo "dry run: nothing killed"
  exit 0
fi

if [[ $YES -eq 0 ]]; then
  if [[ ! -t 0 ]]; then
    echo "no terminal and no --yes: not killing" >&2
    exit 1
  fi
  read -r -p "kill these $n processes? [y/N] " ans
  case "$ans" in
    y|Y|yes|YES) ;;
    *) echo "not killing"; exit 1 ;;
  esac
fi

for pid in ${targets[@]+"${targets[@]}"}; do
  kill -TERM "$pid" 2>/dev/null || true
done

for _ in $(seq 1 5); do
  alive=0
  for pid in ${targets[@]+"${targets[@]}"}; do
    if kill -0 "$pid" 2>/dev/null; then alive=1; break; fi
  done
  [[ $alive -eq 0 ]] && break
  sleep 1
done

survivors=0
for pid in ${targets[@]+"${targets[@]}"}; do
  if kill -0 "$pid" 2>/dev/null; then
    kill -KILL "$pid" 2>/dev/null || true
    survivors=$((survivors + 1))
  fi
done
echo "stopped $n processes (SIGKILL needed for $survivors)"
