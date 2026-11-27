#!/usr/bin/env python3
"""Dispatch stop: keep the main session from ending its turn while executor slots sit idle.

Stop hook. It reads the per-session task-gate state (written by task-gate.py; read only here) and
blocks the stop with a reason naming the ready tasks when the executor pool has free slots.

State: <STATE_DIR>/<safe session id>.json, where STATE_DIR is TASK_GATE_STATE_DIR or
~/.claude/hooks/state/task-gate. Keys starting with "_" are bookkeeping; every other key is a task
id mapping to {status, owner, subject, blockedBy, metadata, dispatch_pending_at?, dispatched_at?,
finished_at?}. A missing or unreadable state file means nothing to do.

Definitions:
  open blocker  an id in blockedBy whose entry exists in state and status != completed
  ready         status pending, owner falsy, no open blocker, metadata.dispatch is not False
  start(e)      the later of dispatched_at and dispatch_pending_at among those set; None if neither
  finished(e)   finished_at set and finished_at >= start(e)
  running(e)    status in_progress, owner in executors, start(e) set, not finished(e), and
                now - start(e) < window, where window is 120 when dispatch_pending_at is set and
                (dispatched_at unset or dispatch_pending_at > dispatched_at), else running_stale_sec
  R             number of running tasks
  free          executors owning no in_progress task
  P             pool size
  K             min(P, R + len(ready)) - R, capped at len(free), floor 0

Config: CLAUDE_HARNESS_CONFIG or ~/.claude/hooks/harness.local.json (missing or malformed means
defaults). Parsed exactly as task-gate.py parses it:
  executors          non-empty list of non-empty str (duplicates dropped); default: the ten executor names
  pool_size          integer above zero (bool and float are not accepted); env TASK_GATE_POOL_SIZE,
                     else this key, else 8
  running_stale_sec  finite number above zero (bool not accepted); env TASK_GATE_RUNNING_STALE_SEC,
                     else this key, else 3600
A value that does not convert or is not usable is skipped and the next source applies.

Behaviour (Stop event): prints {"decision": "block", "reason": ...} and exits 0 only when K > 0.
Silent (no output, exit 0) when: stdin is unparseable; any exception; the payload has an agent_id
(subagent); stop_hook_active is true (block at most once per stop, never loop); CLAUDE_DISPATCH_STOP=0,
CLAUDE_REQUIRE_TASK=0 or CLAUDE_MAIN_WRITE=1; env CLAUDE_LOCAL or LOCAL_DISPATCH is non-empty (local and
hybrid modes dispatch one executor at a time, so there is no pool to fill); an exempt_env match in the
harness config; K == 0.
Each block appends one line to DISPATCH_STOP_LOG (default ~/.claude/hooks/dispatch-stop.log).

Hooks run with python -I, so the helpers here are copies of task-gate.py's, not imports.
"""
import datetime
import fnmatch
import json
import math
import os
import re
import sys
import time

HOME = os.path.expanduser("~")
STATE_DIR = (os.environ.get("TASK_GATE_STATE_DIR")
             or os.path.join(HOME, ".claude", "hooks", "state", "task-gate"))
LOG = (os.environ.get("DISPATCH_STOP_LOG")
       or os.path.join(HOME, ".claude", "hooks", "dispatch-stop.log"))
HARNESS_CONFIG = (os.environ.get("CLAUDE_HARNESS_CONFIG")
                  or os.path.join(HOME, ".claude", "hooks", "harness.local.json"))
DEFAULT_EXECUTORS = ["qorvax-tel", "zyrene-vosk", "thal-ummir", "kreeva-osh", "vexlor-ain",
                     "nyxa-prell", "oomrak-zhee", "xilth-varo", "juun-ketrax", "plimm-oraxi"]
DEFAULT_POOL = 8
DEFAULT_STALE = 3600.0
PENDING_WINDOW = 120.0
SUBJECT_MAX = 80


def load_config():
    """Harness config dict; {} when missing, unreadable or not an object."""
    try:
        with open(HARNESS_CONFIG, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def load_exempt_env(config):
    """{var: [glob, ...]} from the exempt_env key; a malformed entry is skipped."""
    raw = config.get("exempt_env", {})
    if not isinstance(raw, dict):
        return {}
    out = {}
    for var, pats in raw.items():
        if isinstance(var, str) and var and isinstance(pats, list) and all(isinstance(p, str) for p in pats):
            out[var] = pats
    return out


def env_exempt(config):
    """True when some exempt_env variable is non-empty and matches one of its globs."""
    for var, pats in load_exempt_env(config).items():
        val = os.environ.get(var, "")
        if val and any(fnmatch.fnmatchcase(val, p) for p in pats):
            return True
    return False


def session_path(sid):
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", sid or "") or "unknown"
    return os.path.join(STATE_DIR, safe + ".json")


def str_list(v):
    if not isinstance(v, list):
        return []
    return list(dict.fromkeys(str(x) for x in v if x not in (None, "")))


def load_state(sid):
    """Task entries of the session, normalised; {} when the file is missing or unreadable."""
    try:
        with open(session_path(sid), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    tasks = {}
    for k, e in data.items():
        if k.startswith("_") or not isinstance(e, dict):
            continue
        e.setdefault("status", "pending")
        e.setdefault("owner", None)
        e.setdefault("subject", None)
        e["blockedBy"] = str_list(e.get("blockedBy"))
        md = e.get("metadata")
        e["metadata"] = md if isinstance(md, dict) else {}
        tasks[k] = e
    return tasks


def tkey(t):
    return (0, int(t), "") if t.isdigit() else (1, 0, t)


def open_among(state, ids):
    return [b for b in ids
            if isinstance(state.get(b), dict) and state[b].get("status") != "completed"]


def is_ready(state, tid):
    e = state[tid]
    return (e.get("status") == "pending" and not e.get("owner")
            and not open_among(state, e.get("blockedBy") or [])
            and e["metadata"].get("dispatch") is not False)


def is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def start_of(e):
    """start(e): the later of dispatched_at and dispatch_pending_at among those set; None if neither."""
    vals = [e[k] for k in ("dispatched_at", "dispatch_pending_at") if is_num(e.get(k))]
    return max(vals) if vals else None


def is_finished(e):
    """finished(e): finished_at set and at or after start(e)."""
    s = start_of(e)
    return s is not None and is_num(e.get("finished_at")) and e["finished_at"] >= s


def is_running(e, now, executors, stale):
    if e.get("status") != "in_progress" or e.get("owner") not in executors:
        return False
    s = start_of(e)
    if s is None or is_finished(e):
        return False
    d, p = e.get("dispatched_at"), e.get("dispatch_pending_at")
    pending_newer = is_num(p) and (not is_num(d) or p > d)
    return now - s < (PENDING_WINDOW if pending_newer else stale)


def _as_int(x):
    if isinstance(x, (bool, float)):
        raise ValueError(x)
    return int(x)


def _as_float(x):
    if isinstance(x, bool):
        raise ValueError(x)
    return float(x)


def setting(env_name, key, default, conv, config):
    """First usable value of: env var env_name, config key, default. A value that does not convert,
    or is not a finite number above zero, is skipped (the next source applies). Same as task-gate.py."""
    for raw in (os.environ.get(env_name), config.get(key)):
        if raw in (None, ""):
            continue
        try:
            v = conv(raw)
        except (TypeError, ValueError):
            continue
        if v > 0 and math.isfinite(v):
            return v
    return default


def pool_size(config):
    return int(setting("TASK_GATE_POOL_SIZE", "pool_size", DEFAULT_POOL, _as_int, config))


def stale_sec(config):
    return setting("TASK_GATE_RUNNING_STALE_SEC", "running_stale_sec", DEFAULT_STALE, _as_float, config)


def executors_from(config):
    """Executor names: config key "executors" when it is a non-empty list of non-empty strings, else
    DEFAULT_EXECUTORS. Same as task-gate.py."""
    v = config.get("executors")
    if isinstance(v, list) and v and all(isinstance(x, str) and x for x in v):
        return list(dict.fromkeys(v))
    return list(DEFAULT_EXECUTORS)


def compute(state, executors, pool, stale, now):
    """(K, R, ready ids sorted, free executor names)."""
    ready = sorted((k for k in state if is_ready(state, k)), key=tkey)
    running = sum(1 for e in state.values() if is_running(e, now, executors, stale))
    busy = {e.get("owner") for e in state.values() if e.get("status") == "in_progress"}
    free = [n for n in executors if n not in busy]
    k = min(pool, running + len(ready)) - running
    k = max(0, min(k, len(free)))
    return k, running, ready, free


def reason_text(state, k, running, pool, ready, free):
    items = "; ".join("#%s %s" % (tid, (state[tid].get("subject") or "")[:SUBJECT_MAX])
                      for tid in ready[:k])
    return ("Executor pool has %d free slot(s) (%d/%d running) and ready tasks are waiting: %s. "
            "Dispatch them now in ONE message, one Agent call each to a free executor "
            "(free: %s), description ending · #<id>. If a task is not executor work, "
            "TaskUpdate it with metadata {\"dispatch\": false}."
            % (k, running, pool, items, ", ".join(free)))


def log_block(sid, k, running, pool, ready, free):
    rec = {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(), "session_id": sid,
           "k": k, "running": running, "pool": pool, "ready": ready[:k], "free": free}
    try:
        os.makedirs(os.path.dirname(LOG) or ".", exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def main():
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except Exception:
        return
    if not isinstance(payload, dict):
        return
    if payload.get("agent_id"):
        return
    if payload.get("stop_hook_active") is True:
        return
    if (os.environ.get("CLAUDE_DISPATCH_STOP") == "0" or os.environ.get("CLAUDE_REQUIRE_TASK") == "0"
            or os.environ.get("CLAUDE_MAIN_WRITE") == "1"):
        return
    if os.environ.get("CLAUDE_LOCAL") or os.environ.get("LOCAL_DISPATCH"):
        return
    config = load_config()
    if env_exempt(config):
        return
    sid = payload.get("session_id")
    sid = sid if isinstance(sid, str) else ""
    state = load_state(sid)
    if not state:
        return
    executors = executors_from(config)
    pool = pool_size(config)
    stale = stale_sec(config)
    now = time.time()
    k, running, ready, free = compute(state, executors, pool, stale, now)
    if k <= 0:
        return
    log_block(sid, k, running, pool, ready, free)
    print(json.dumps({"decision": "block",
                      "reason": reason_text(state, k, running, pool, ready, free)}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
