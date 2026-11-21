"""SessionStart hook (matcher "compact"): nudge toward a handoff after repeated compactions.

Each compaction re-reads a large context on every later turn and loses detail, so
after enough compactions in one session the model is told to consider writing a
handoff and continuing in a fresh session.

Input (stdin JSON): ``session_id``, ``hook_event_name``, ``source``. Only
``source == "compact"`` is counted; other sources exit silently.

State: ``<HANDOFF_NUDGE_STATE_DIR or ~/.claude/hooks/state/handoff-nudge>/<safe sid>.json``
holding ``{"compactions": N, "last_ts": float}``. State files older than 14 days are
pruned opportunistically.

Threshold: env ``HANDOFF_NUDGE_AFTER`` (default 3). When N >= threshold the hook prints
a ``hookSpecificOutput.additionalContext`` message. Below threshold it prints nothing.

Kill switch: ``CLAUDE_HANDOFF_NUDGE=0``. Fails open: any error or bad JSON exits 0
silently.
"""

import json
import os
import re
import sys
import time

DEFAULT_STATE_DIR = os.path.expanduser("~/.claude/hooks/state/handoff-nudge")
DEFAULT_AFTER = 3
STATE_TTL_SECONDS = 14 * 24 * 3600


def _safe_sid(sid):
    return re.sub(r"[^A-Za-z0-9_-]", "_", sid)


def _threshold():
    raw = os.environ.get("HANDOFF_NUDGE_AFTER")
    try:
        return int(raw) if raw not in (None, "") else DEFAULT_AFTER
    except ValueError:
        return DEFAULT_AFTER


def _load_count(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("compactions"), int):
            return data["compactions"]
    except Exception:
        pass
    return 0


def _prune(state_dir):
    cutoff = time.time() - STATE_TTL_SECONDS
    try:
        for name in os.listdir(state_dir):
            p = os.path.join(state_dir, name)
            try:
                if name.endswith(".json") and os.path.getmtime(p) < cutoff:
                    os.remove(p)
            except Exception:
                pass
    except Exception:
        pass


def main():
    if os.environ.get("CLAUDE_HANDOFF_NUDGE") == "0":
        return
    payload = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
    if not isinstance(payload, dict):
        return
    if payload.get("source") != "compact":
        return
    sid = payload.get("session_id")
    if not isinstance(sid, str) or not sid:
        return

    state_dir = os.environ.get("HANDOFF_NUDGE_STATE_DIR") or DEFAULT_STATE_DIR
    os.makedirs(state_dir, exist_ok=True)
    _prune(state_dir)

    state_path = os.path.join(state_dir, _safe_sid(sid) + ".json")
    count = _load_count(state_path) + 1
    tmp = state_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"compactions": count, "last_ts": time.time()}, fh)
    os.replace(tmp, state_path)

    if count >= _threshold():
        text = (
            f"This session has been compacted {count} times; each compaction loses detail "
            "and re-reads cost a ~150k-token context per turn. At the next natural break, "
            "write a handoff (state, branch/PR, next steps) to memory or a HANDOFF file "
            "and suggest the user continue in a fresh session."
        )
        out = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}
        sys.stdout.write(json.dumps(out) + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
