# Shared settings for the local Ollama setup and the Claude router (hybrid-proxy.py).
# Sourced by the other scripts. Every value can be overridden from the environment.

LOCAL_LLM_MODEL="${LOCAL_LLM_MODEL:-qwen3.8:27b}"
LOCAL_LLM_CTX="${LOCAL_LLM_CTX:-163840}"
OLLAMA_HOST="${OLLAMA_HOST:-127.0.0.1:11434}"
LOCAL_LLM_COMPACT_WINDOW="${LOCAL_LLM_COMPACT_WINDOW:-130000}"
# hybrid-proxy.py listens here; claude-route points ANTHROPIC_BASE_URL at HYBRID_URL.
HYBRID_PORT="${HYBRID_PORT:-4000}"
HYBRID_URL="http://127.0.0.1:$HYBRID_PORT"
# Runtime state: ollama and hybrid-proxy logs and pids, mode marker, generated settings. Never written to the script directory.
LOCAL_LLM_STATE_DIR="${LOCAL_LLM_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/claude-local}"
mkdir -p "$LOCAL_LLM_STATE_DIR"

export LOCAL_LLM_MODEL LOCAL_LLM_CTX OLLAMA_HOST LOCAL_LLM_COMPACT_WINDOW HYBRID_PORT HYBRID_URL LOCAL_LLM_STATE_DIR

# Removes every x-local-dispatch line from ANTHROPIC_CUSTOM_HEADERS. Other header lines stay. The variable is
# unset when no header line is left.
strip_dispatch_header() {
  local current="${ANTHROPIC_CUSTOM_HEADERS:-}" line
  [[ -n "$current" ]] || return 0
  local -a kept=()
  while IFS= read -r line; do
    if [[ "${line,,}" =~ ^[[:space:]]*x-local-dispatch[[:space:]]*: ]]; then continue; fi
    kept+=("$line")
  done <<<"$current"
  if [[ ${#kept[@]} -eq 0 ]]; then
    unset ANTHROPIC_CUSTOM_HEADERS
  else
    local IFS=$'\n'
    export ANTHROPIC_CUSTOM_HEADERS="${kept[*]}"
  fi
}
