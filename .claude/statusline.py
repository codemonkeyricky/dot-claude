#!/usr/bin/env python3
"""Claude Code statusLine command.

Reads the session JSON on stdin and prints one status line:
  <model>  │  ⚙ <executors>  │  tasks ▶N ✓C/T  │  deploy <target> <sha> <status> <age>

Stdlib only. Must be fast and never crash: any failure degrades to the
model name (or an empty line) and exit 0.

Env overrides (for tests and odd setups):
  AGENT_BUDGET_STATE_DIR  default ~/.claude/hooks/state/agent-budget
  TASK_GATE_STATE_DIR     default ~/.claude/hooks/state/task-gate
  STATUSLINE_DEPLOY_LOG   deploy log path; else harness config key statusline.deploy_log
                          (CLAUDE_HARNESS_CONFIG, default ~/.claude/hooks/harness.local.json).
                          Neither set: the deploy segment is omitted.
  STATUSLINE_NOW          float epoch seconds used as "now"
"""
import json
import os
import sys
import time
from datetime import datetime, timezone

SEP = "  │  "
ACTIVE_WINDOW = 30 * 60      # executors idle longer than this are hidden
MAX_EXECUTORS = 4
WARN_IDLE = 300              # idle seconds before the ⚠ marker
DEPLOY_WINDOW = 24 * 3600
MAX_STATE_BYTES = 5 * 1024 * 1024
TAIL_BLOCK = 8192


def clean(text):
    """Collapse control whitespace so a segment can never break the single line."""
    return " ".join(str(text).split())


def fmt_age(seconds):
    s = int(max(0, seconds))
    if s < 60:
        return "%ds" % s
    if s < 3600:
        return "%dm" % (s // 60)
    return "%dh%02d" % (s // 3600, (s % 3600) // 60)


def state_path(env_name, default_dir, session_id):
    base = os.environ.get(env_name) or os.path.join(default_dir)
    return os.path.join(base, session_id + ".json")


def load_state(path):
    """Return the parsed JSON dict at path, or None if missing/corrupt/unsafe."""
    try:
        if os.path.getsize(path) > MAX_STATE_BYTES:
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def model_segment(data):
    model = data.get("model")
    if isinstance(model, dict):
        name = model.get("display_name") or model.get("id")
    else:
        name = model
    if not isinstance(name, str) or not name.strip():
        return ""
    return clean(name)


def executor_segment(session_id, now, home):
    path = state_path("AGENT_BUDGET_STATE_DIR",
                      os.path.join(home, ".claude", "hooks", "state", "agent-budget"),
                      session_id)
    state = load_state(path)
    if not state:
        return ""
    active = []
    for entry in state.values():
        if not isinstance(entry, dict):
            continue
        try:
            last_ts = float(entry.get("last_ts"))
        except (TypeError, ValueError):
            continue
        idle = now - last_ts
        if idle > ACTIVE_WINDOW:
            continue
        active.append((last_ts, idle, entry))
    if not active:
        return ""
    active.sort(key=lambda row: row[0], reverse=True)
    items = []
    for _, idle, entry in active[:MAX_EXECUTORS]:
        try:
            count = int(entry.get("count", 0))
        except (TypeError, ValueError):
            count = 0
        warn = "⚠" if idle > WARN_IDLE else ""
        items.append("%s %dc %s%s" % (clean(entry.get("agent_type", "agent")),
                                      count, warn, fmt_age(idle)))
    extra = len(active) - MAX_EXECUTORS
    line = ", ".join(items)
    if extra > 0:
        line += " +%d" % extra
    return "⚙ " + line


def task_segment(session_id, home):
    path = state_path("TASK_GATE_STATE_DIR",
                      os.path.join(home, ".claude", "hooks", "state", "task-gate"),
                      session_id)
    state = load_state(path)
    if not state:
        return ""
    total = done = running = 0
    for entry in state.values():
        if not isinstance(entry, dict):
            continue
        total += 1
        status = entry.get("status")
        if status == "completed":
            done += 1
        elif status == "in_progress":
            running += 1
    if total == 0:
        return ""
    return "tasks ▶%d ✓%d/%d" % (running, done, total)


def last_nonempty_line(path):
    """Return the last non-empty line of a file, reading only its tail."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        buf = b""
        while pos > 0:
            step = min(TAIL_BLOCK, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step) + buf
            stripped = buf.rstrip(b" \t\r\n")
            if not stripped:
                continue
            idx = stripped.rfind(b"\n")
            if idx != -1 or pos == 0:
                return stripped[idx + 1:].decode("utf-8", "replace").strip()
    return ""


def parse_ts(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return None


def deploy_log_path(home):
    """Deploy log path from env STATUSLINE_DEPLOY_LOG, else harness config statusline.deploy_log.
    None when neither is set (or the config is unreadable): the deploy segment is then omitted."""
    env = os.environ.get("STATUSLINE_DEPLOY_LOG")
    if env:
        return os.path.expanduser(env)
    cfg_path = (os.environ.get("CLAUDE_HARNESS_CONFIG")
                or os.path.join(home, ".claude", "hooks", "harness.local.json"))
    try:
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return None
    sl = cfg.get("statusline") if isinstance(cfg, dict) else None
    val = sl.get("deploy_log") if isinstance(sl, dict) else None
    return os.path.expanduser(val) if isinstance(val, str) and val else None


def deploy_segment(now, home):
    path = deploy_log_path(home)
    if not path or not os.path.isfile(path):
        return ""
    line = last_nonempty_line(path)
    if not line:
        return ""
    entry = json.loads(line)
    if not isinstance(entry, dict):
        return ""
    ts = parse_ts(entry.get("ts"))
    if ts is None or now - ts > DEPLOY_WINDOW:
        return ""
    target = clean(entry.get("target") or entry.get("project") or "deploy")
    sha = clean(entry.get("short") or str(entry.get("sha", ""))[:7])
    if entry.get("dryRun"):
        status = "dry-run"
    elif entry.get("verified") is True:
        status = "verified"
    elif entry.get("verified") is False:
        status = "unverified"
    else:
        status = clean(entry.get("status") or "")
    parts = ["deploy", target]
    if sha:
        parts.append(sha)
    if status:
        parts.append(status)
    parts.append(fmt_age(now - ts))
    return " ".join(parts)


def safe_session_id(data):
    sid = data.get("session_id")
    if not isinstance(sid, str) or not sid:
        return None
    if sid in (".", "..") or "/" in sid or "\\" in sid or "\x00" in sid:
        return None
    return sid


def build_line(data, now, home):
    segments = []
    builders = [
        lambda: model_segment(data),
    ]
    sid = safe_session_id(data)
    if sid:
        builders.append(lambda: executor_segment(sid, now, home))
        builders.append(lambda: task_segment(sid, home))
    builders.append(lambda: deploy_segment(now, home))
    for build in builders:
        try:
            seg = build()
        except Exception:
            seg = ""
        if seg:
            segments.append(clean(seg))
    return SEP.join(segments)


def main():
    line = ""
    data = {}
    try:
        raw = sys.stdin.read()
        parsed = json.loads(raw) if raw.strip() else {}
        data = parsed if isinstance(parsed, dict) else {}
        try:
            now = float(os.environ.get("STATUSLINE_NOW") or time.time())
        except ValueError:
            now = time.time()
        home = os.path.expanduser("~")
        line = build_line(data, now, home)
    except Exception:
        try:
            line = model_segment(data) if isinstance(data, dict) else ""
        except Exception:
            line = ""
    try:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception:
        code = 0
    sys.exit(code)
