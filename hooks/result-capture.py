"""Stop hook: record each session's ``result:`` lines in a permanent JSONL log.

Background sessions end a deliverable with a line starting ``result:``. This hook
runs when the main agent finishes a response, takes the final assistant text
(``last_assistant_message`` from the payload, or else the last assistant entry
with text in the transcript tail), extracts every line matching
``^\\s*result:\\s*(.+?)\\s*$`` and appends each new one to a JSONL log as
``{"kind": "result", "ts", "session_id", "cwd", "text"}``.

Dedup: per-session state files (list of texts already recorded) live in the state
dir, named by a sanitized session id. State files older than 14 days are pruned.

Repo attribution: each record also carries ``"repo"`` (dominant repo name) and
``"repo_root"`` (its absolute root), read from that session's session-repo state
file (``<SESSION_REPO_STATE_DIR>/<safe sid>.json``, field ``dominant``). Both are
null when there is no state file or no dominant repo. A failed lookup never stops
the record being written.

Env overrides: RESULT_CAPTURE_LOG (log file), RESULT_CAPTURE_STATE_DIR (state dir),
SESSION_REPO_STATE_DIR (session-repo state dir).

Fails open: never prints to stdout, never blocks, always exits 0.
"""

import datetime
import json
import os
import re
import sys
import time

HOOK_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LOG = os.path.join(HOOK_DIR, "state", "completed-work.jsonl")
DEFAULT_STATE_DIR = os.path.join(HOOK_DIR, "state", "result-capture")
TAIL_BYTES = 2 * 1024 * 1024
STATE_TTL_SECONDS = 14 * 24 * 3600
RESULT_RE = re.compile(r"^\s*result:\s*(.+?)\s*$", re.MULTILINE)


def _strip_wrapping(text):
    """Remove markdown backticks or bold markers wrapping the whole result text."""
    t = text.strip()
    for wrap in ("**", "__", "`", "*", "_"):
        if len(t) > 2 * len(wrap) and t.startswith(wrap) and t.endswith(wrap):
            t = t[len(wrap):-len(wrap)].strip()
    return t


def _line_results(text):
    out = []
    for m in RESULT_RE.finditer(text):
        val = _strip_wrapping(m.group(1))
        if val:
            out.append(val)
    return out


def _text_of_entry(entry):
    msg = entry.get("message") if isinstance(entry, dict) else None
    content = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


def _final_assistant_text(transcript_path):
    with open(transcript_path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - TAIL_BYTES))
        raw = fh.read()
    lines = raw.decode("utf-8", errors="replace").splitlines()
    if size > TAIL_BYTES and lines:
        lines = lines[1:]  # first line may be cut mid-object
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        text = _text_of_entry(entry)
        if text.strip():
            return text
    return ""


def _safe_sid(sid):
    return re.sub(r"[^A-Za-z0-9_-]", "_", sid)


def _session_repo_for(sid):
    """Return (repo name, repo root) from the session-repo state file, or (None, None)."""
    try:
        state_dir = os.environ.get("SESSION_REPO_STATE_DIR") or os.path.expanduser(
            "~/.claude/hooks/state/session-repo")
        with open(os.path.join(state_dir, _safe_sid(sid) + ".json"), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        dom = data.get("dominant") if isinstance(data, dict) else None
        if isinstance(dom, dict):
            name = dom.get("name")
            root = dom.get("root")
            return (name if isinstance(name, str) else None,
                    root if isinstance(root, str) else None)
    except Exception:
        pass
    return (None, None)


def _load_state(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("recorded"), list):
            return data
    except Exception:
        pass
    return {"recorded": []}


def _prune_state(state_dir):
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
    payload = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
    if not isinstance(payload, dict):
        return
    sid = payload.get("session_id")
    if not isinstance(sid, str) or not sid:
        return
    cwd = payload.get("cwd")
    cwd = cwd if isinstance(cwd, str) else None

    text = payload.get("last_assistant_message")
    if not (isinstance(text, str) and text.strip()):
        tp = payload.get("transcript_path")
        if not isinstance(tp, str) or not os.path.isfile(tp):
            return
        text = _final_assistant_text(tp)
    results = _line_results(text or "")
    if not results:
        return

    state_dir = os.environ.get("RESULT_CAPTURE_STATE_DIR") or DEFAULT_STATE_DIR
    log_path = os.environ.get("RESULT_CAPTURE_LOG") or DEFAULT_LOG
    os.makedirs(state_dir, exist_ok=True)
    _prune_state(state_dir)

    state_path = os.path.join(state_dir, _safe_sid(sid) + ".json")
    state = _load_state(state_path)
    seen = set(state["recorded"])
    fresh = []
    for r in results:
        if r not in seen:
            seen.add(r)
            fresh.append(r)
    if not fresh:
        return

    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    repo, repo_root = _session_repo_for(sid)
    parent = os.path.dirname(os.path.abspath(log_path))
    os.makedirs(parent, exist_ok=True)
    with open(log_path, "a", encoding="utf-8", newline="\n") as fh:
        for r in fresh:
            rec = {"kind": "result", "ts": ts, "session_id": sid, "cwd": cwd, "text": r,
                   "repo": repo, "repo_root": repo_root}
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    state["recorded"] = list(seen)
    tmp = state_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False)
    os.replace(tmp, state_path)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
