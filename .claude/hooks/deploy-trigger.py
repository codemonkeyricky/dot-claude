#!/usr/bin/env python3
"""Trigger for deploy-reconcile.py. Run as `python3 -I deploy-trigger.py`.

Events:
  PostToolUse (Bash): starts a reconcile pass when the command is `git push` or
      `gh pr create|merge|close|reopen|ready` (see is_deploy_trigger).
  SessionStart: always starts a reconcile pass (catches up on merges done on github.com).

The reconciler is spawned fully detached (new session, stdio to DEVNULL) and this hook returns at
once. It never fails the tool: every error is swallowed and the exit status is always 0.
Kill switch: DEPLOY_HOOKS=0 does nothing.

Matching is quoting-aware: the line is lexed with shlex, split on shell operators, and each simple
command's leading env assignments and wrappers (nohup, time, exec, command) are skipped. So
`cd x && git push`, `git -C /x push origin b` and `git -c k=v push` match, while `git log --grep
push`, `gh pr view 3` and `echo git push` do not. Known miss: a push nested in `bash -c '...'`
is not seen; the session-start pass or a manual run catches it.
"""

import json
import os
import re
import shlex
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RECONCILE = os.path.join(HERE, "deploy-reconcile.py")
CONFIG_PATH = os.path.join(HERE, "deploy-hooks.local.json")
STATE_DIR = os.path.expanduser("~/.local/state/deploy-hooks")
GH_PR_WRITES = {"create", "merge", "close", "reopen", "ready"}
WRAPPERS = {"nohup", "time", "exec", "command"}
ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
OPERATOR_CHARS = set(";&|()")
FALLBACK_SPLIT = re.compile(r"&&|\|\||[;|&()]")


def simple_commands(command):
    """Split a shell command line into simple commands (lists of words), quoting respected."""
    out = []
    for line in command.splitlines():
        try:
            lex = shlex.shlex(line, posix=True, punctuation_chars=True)
            lex.whitespace_split = True
            tokens = list(lex)
        except ValueError:  # unbalanced quotes: fall back to a plain split
            out.extend(s.split() for s in FALLBACK_SPLIT.split(line))
            continue
        current = []
        for tok in tokens:
            if tok and all(c in OPERATOR_CHARS for c in tok):
                if current:
                    out.append(current)
                current = []
            else:
                current.append(tok)
        if current:
            out.append(current)
    return [c for c in out if c]


def _strip_prefix(words):
    i = 0
    while i < len(words) and (ENV_ASSIGN.match(words[i]) or words[i] in WRAPPERS):
        i += 1
    return words[i:]


def is_git_push(words):
    if not words or words[0] != "git":
        return False
    i = 1
    while i < len(words):
        if words[i] in ("-C", "-c"):
            i += 2
        elif words[i].startswith("-"):
            i += 1
        else:
            break
    return i < len(words) and words[i] == "push"


def is_gh_pr_write(words):
    if not words or words[0] != "gh":
        return False
    i = 1
    while i < len(words) and words[i].startswith("-"):
        i += 2 if words[i] in ("-R", "--repo") else 1
    return i + 1 < len(words) and words[i] == "pr" and words[i + 1] in GH_PR_WRITES


def is_deploy_trigger(command):
    for words in simple_commands(command or ""):
        words = _strip_prefix(words)
        if is_git_push(words) or is_gh_pr_write(words):
            return True
    return False


def log_paths():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            names = [r["name"] for r in json.load(fh).get("repos") or []]
    except Exception:  # noqa: BLE001
        names = []
    return [os.path.join(STATE_DIR, n + ".log") for n in names] or [STATE_DIR]


def spawn():
    os.makedirs(STATE_DIR, exist_ok=True)
    subprocess.Popen(
        [sys.executable, "-I", RECONCILE],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
        cwd=STATE_DIR,
    )


def main():
    if os.environ.get("DEPLOY_HOOKS") == "0":
        return 0
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        event = data.get("hook_event_name") or ""
        if event == "PostToolUse":
            if data.get("tool_name") != "Bash":
                return 0
            command = (data.get("tool_input") or {}).get("command")
            if not isinstance(command, str) or not is_deploy_trigger(command):
                return 0
            spawn()
            where = ", ".join(log_paths())
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": f"deploy-hooks: reconcile started (log: {where})",
            }}))
        elif event == "SessionStart":
            spawn()
    except Exception:  # noqa: BLE001 - never fail the tool
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BaseException:  # noqa: BLE001
        sys.exit(0)
