#!/usr/bin/env bash
# Install this repo's tracked Claude Code config (the tracked files under .claude/ in this repo)
# into ${CLAUDE_DIR:-$HOME/.claude}. Paths inside .claude/ map to the same relative paths in the
# target: .claude/hooks/x.py installs to <target>/hooks/x.py. Nothing outside .claude/ is installed.
# Copies tracked files and never deletes anything in the target directory.
# The repo's settings file is .claude/settings.harness.json (not settings.json, so Claude Code
# does not load it as project settings when run inside this repo). It is deep-merged into the
# target's settings.json, not copied: keys from the repo win, other keys
# already in the target are kept (dicts merge recursively; lists are replaced).
# The merge runs first in every mode, so a bad settings file stops the script before
# any file is copied. The merged file keeps the mode of the target settings.json it replaces.
# If the target settings.json is a symlink, the merged file is written to the real file it
# points to, so the symlink stays in place.
#
#   ./deploy.sh            copy tracked files and merge settings.harness.json into the target settings.json
#   ./deploy.sh --dry-run  list what would change, change nothing
#   ./deploy.sh --diff     show unified diffs target -> repo, change nothing
set -euo pipefail

# CLAUDE_DIR is relative to the caller's working directory, so resolve it to an absolute path
# before the cd calls below move us into the repo. A leading ~ or ~/ means $HOME (the shell does
# not expand a tilde that comes from a variable's value).
CLAUDE_DIR="${CLAUDE_DIR:-$HOME/.claude}"
case "$CLAUDE_DIR" in
  "~") CLAUDE_DIR="$HOME" ;;
  "~/"*) CLAUDE_DIR="$HOME/${CLAUDE_DIR#"~/"}" ;;
esac
case "$CLAUDE_DIR" in
  /*) ;;
  *) CLAUDE_DIR="$PWD/$CLAUDE_DIR" ;;
esac

cd "$(dirname "${BASH_SOURCE[0]}")"
# Everything below works relative to the harness directory in this repo.
cd .claude

mode=install
case "${1:-}" in
  "")        mode=install ;;
  --dry-run) mode=dry-run ;;
  --diff)    mode=diff ;;
  *) echo "usage: $0 [--dry-run | --diff]" >&2; exit 2 ;;
esac

# Tracked files that get deployed: git ls-files run inside .claude/ lists paths relative to it.
# settings.harness.json is merged separately, so it is excluded (and .gitignore, if one exists there).
# It is never copied as a plain file: it lands in the target only as the merged settings.json.
list_files() {
  git ls-files | grep -vxF -e .gitignore -e settings.harness.json || true
}

# Write the merged settings.json (target deep-merged with the repo settings.harness.json) to the
# file given as $1, and copy the mode of $2 onto it when $2 exists. $2 is the real destination (a
# symlink is resolved by the caller). A missing target is treated as {}. Invalid JSON in either
# file, or a top-level value that is not an object, exits nonzero with a message naming the file,
# and nothing is written.
merge_settings() {
  python3 -I - "$CLAUDE_DIR/settings.json" settings.harness.json "$1" "$2" <<'PY'
import json
import os
import stat
import sys


def merge(base, over):
    out = dict(base)
    for key, value in over.items():
        if isinstance(out.get(key), dict) and isinstance(value, dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = value
    return out


def load(path, label):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except ValueError as e:  # includes json.JSONDecodeError and UnicodeDecodeError
        sys.exit("deploy.sh: %s (%s) is not valid JSON: %s" % (label, path, e))
    if not isinstance(data, dict):
        sys.exit("deploy.sh: %s (%s) must contain a JSON object" % (label, path))
    return data


target, repo, out, dest = sys.argv[1:5]
existing = load(target, "target settings.json") if os.path.exists(target) else {}
incoming = load(repo, "repo settings.harness.json")
text = json.dumps(merge(existing, incoming), indent=2) + "\n"
with open(out, "w", encoding="utf-8") as f:
    f.write(text)
if os.path.exists(dest):
    os.chmod(out, stat.S_IMODE(os.stat(dest).st_mode))
PY
}

# The real file that settings.json is written to. A symlink is followed; anything else is
# used as is.
SETTINGS="$CLAUDE_DIR/settings.json"
dest="$SETTINGS"
if [ -L "$SETTINGS" ]; then
  dest=$(python3 -I -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$SETTINGS")
fi

# Merge first, into a temp file, in every mode. A failing merge exits here (set -e), before
# anything in the target directory is touched. The temp file sits beside the real destination,
# so the final mv is a rename on one filesystem. The trap removes the temp file on any exit.
if [ "$mode" = install ]; then
  mkdir -p "$CLAUDE_DIR"
  tmp=$(mktemp "$(dirname "$dest")/.settings.json.XXXXXX")
else
  tmp=$(mktemp)
fi
trap 'rm -f "$tmp"' EXIT
merge_settings "$tmp" "$dest"

case "$mode" in
  diff)
    list_files | while IFS= read -r f; do
      if [ -e "$CLAUDE_DIR/$f" ]; then
        diff -u "$CLAUDE_DIR/$f" "$f" || true
      else
        diff -u /dev/null "$f" || true
      fi
    done
    if [ -e "$SETTINGS" ]; then
      diff -u "$SETTINGS" "$tmp" || true
    else
      diff -u /dev/null "$tmp" || true
    fi
    ;;
  dry-run)
    list_files | rsync -a -n -i --files-from=- . "$CLAUDE_DIR/"
    if [ ! -e "$SETTINGS" ] || ! cmp -s "$SETTINGS" "$tmp"; then
      echo "settings.json (merge)"
    fi
    ;;
  install)
    list_files | rsync -a --files-from=- . "$CLAUDE_DIR/"
    mv "$tmp" "$dest"
    echo "installed tracked files into $CLAUDE_DIR and merged settings.json"
    ;;
esac
