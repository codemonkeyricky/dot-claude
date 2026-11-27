"""SessionStart hook (matcher "compact"): nudge after repeated or rapid compactions.

Two signals, both counted per session:

- Count: each compaction loses detail and later turns re-read a large context. From
  the Nth compaction (env ``HANDOFF_NUDGE_AFTER``, default 3) the model is told to
  write a handoff and continue in a fresh session.
- Rapid: if the previous compaction was less than ``HANDOFF_NUDGE_RAPID_MIN`` minutes
  ago (default 20; 0 disables), main is probably pulling large tool output (files,
  logs, test output, sweeps) into its own context. From the 2nd compaction on the
  model is told to send big reads to Explore or an executor.

Input (stdin JSON): ``session_id``, ``hook_event_name``, ``source``. Only
``source == "compact"`` is counted; other sources exit silently.

State: ``<HANDOFF_NUDGE_STATE_DIR or ~/.claude/hooks/state/handoff-nudge>/<safe sid>.json``
holding ``{"compactions": N, "last_ts": float}``. The previous ``last_ts`` is read
before it is overwritten, to decide the rapid signal. State files older than 14 days
are pruned opportunistically.

Output: a ``hookSpecificOutput.additionalContext`` message when the count threshold is
reached or the rapid signal fires with count >= 2. Otherwise nothing is printed.
Bad or empty env values fall back to the defaults.

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
DEFAULT_RAPID_MIN = 20
STATE_TTL_SECONDS = 14 * 24 * 3600


def _safe_sid(sid):
    return re.sub(r"[^A-Za-z0-9_-]", "_", sid)


def _threshold():
    raw = os.environ.get("HANDOFF_NUDGE_AFTER")
    try:
        return int(raw) if raw not in (None, "") else DEFAULT_AFTER
    except ValueError:
        return DEFAULT_AFTER


def _rapid_min():
    raw = os.environ.get("HANDOFF_NUDGE_RAPID_MIN")
    try:
        return float(raw) if raw not in (None, "") else DEFAULT_RAPID_MIN
    except ValueError:
        return DEFAULT_RAPID_MIN


def _load_state(path):
    """Return ``(count, last_ts)``; ``last_ts`` is a float or None. Fails safe to (0, None)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("compactions"), int):
            last = data.get("last_ts")
            last_ts = None
            if isinstance(last, (int, float)) and not isinstance(last, bool):
                last_ts = float(last)
            return data["compactions"], last_ts
    except Exception:
        pass
    return 0, None


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
    prev_count, last_ts = _load_state(state_path)
    count = prev_count + 1
    now = time.time()
    rapid_min = _rapid_min()
    rapid = last_ts is not None and rapid_min > 0 and (now - last_ts) < rapid_min * 60

    tmp = state_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"compactions": count, "last_ts": now}, fh)
    os.replace(tmp, state_path)

    threshold = _threshold()
    if not (count >= threshold or (rapid and count >= 2)):
        return

    parts = [f"This session has been compacted {count} times."]
    if rapid:
        minutes = max(1, int((now - last_ts) // 60))
        parts.append(
            f"The last compaction was {minutes} min ago: main is likely pulling large tool "
            "output (files, logs, test output, sweeps) into its own context. Dispatch big "
            "reads to Explore or an executor and keep only the findings here."
        )
    if count >= threshold:
        parts.append(
            "Each compaction loses detail and later turns re-read a large context. At the "
            "next natural break, write a handoff (state, branch/PR, next steps) to memory or "
            "a HANDOFF file and suggest the user continue in a fresh session."
        )
    text = " ".join(parts)
    out = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}
    sys.stdout.write(json.dumps(out) + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
