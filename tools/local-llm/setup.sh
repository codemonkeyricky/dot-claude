#!/usr/bin/env bash
# One-time (and idempotent) setup for claude-local:
# checks ollama, starts the server, verifies the model is pulled, warms it, and links claude-local into ~/.local/bin.
# Never pulls a model automatically. If the default tag qwen3-coder:30b is missing but qwen3-coder:latest is
# pulled, it creates the 30b tag from it with `ollama cp` (no extra disk: the weights are shared).
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=config.sh
source "$DIR/config.sh"

OLLAMA_BIN="${OLLAMA_BIN:-$(command -v ollama || echo /usr/local/bin/ollama)}"

model_present() { # $1 = name:tag, checked against the global $list
  printf '%s\n' "$list" | awk -v m="$1" 'NR>1 && $1==m {found=1} END {exit !found}'
}

echo "== ollama binary"
if [[ ! -x "$OLLAMA_BIN" ]]; then
  echo "FAIL: ollama not found (looked for $OLLAMA_BIN). Install ollama first." >&2
  exit 1
fi
echo "ok: $OLLAMA_BIN ($("$OLLAMA_BIN" --version 2>/dev/null | tail -n 1))"

echo "== server"
"$DIR/ollama-up.sh"

echo "== model: $LOCAL_LLM_MODEL"
want="$LOCAL_LLM_MODEL"
if [[ "$want" != *:* ]]; then want="$want:latest"; fi
list="$("$OLLAMA_BIN" list)"
if ! model_present "$want" && [[ "$want" == "qwen3-coder:30b" ]] && model_present "qwen3-coder:latest"; then
  echo "note: $want is not pulled, but qwen3-coder:latest is. Creating $want from it with ollama cp (no extra disk, the weights are shared)."
  "$OLLAMA_BIN" cp qwen3-coder:latest "$want"
  list="$("$OLLAMA_BIN" list)"
fi
if ! model_present "$want"; then
  echo "FAIL: model '$LOCAL_LLM_MODEL' is not pulled. Not pulling automatically." >&2
  echo "Free disk on home filesystem:" >&2
  df -h ~ >&2
  echo "To pull it (needs free disk for the download), run:" >&2
  echo "  $OLLAMA_BIN pull $LOCAL_LLM_MODEL" >&2
  exit 1
fi
echo "ok: $want is pulled"

echo "== warm model"
"$OLLAMA_BIN" run "$LOCAL_LLM_MODEL" "hi" >/dev/null
echo "ok: warmed $LOCAL_LLM_MODEL"

echo "== symlink"
mkdir -p "$HOME/.local/bin"
ln -sfn "$DIR/claude-local" "$HOME/.local/bin/claude-local"
echo "ok: $HOME/.local/bin/claude-local -> $DIR/claude-local"
ln -sfn "$DIR/claude-anthropic" "$HOME/.local/bin/claude-anthropic"
echo "ok: $HOME/.local/bin/claude-anthropic -> $DIR/claude-anthropic"

case ":$PATH:" in
  *":$HOME/.local/bin:"*) echo "ok: ~/.local/bin is on PATH" ;;
  *)
    echo "WARN: ~/.local/bin is not on PATH. Add this line to ~/.bashrc:"
    echo '  export PATH="$HOME/.local/bin:$PATH"'
    ;;
esac

echo "setup complete. Run: claude-local -p \"...\""
