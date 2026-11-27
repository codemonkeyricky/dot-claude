#!/usr/bin/env python3
"""Agent budget: caps how many tool calls one subagent makes, so a runaway executor stops itself.

PreToolUse, matcher "*". Only subagent payloads (agent_id present) are counted; the main
session (no agent_id) is ignored. Each subagent call increments its count in
<state dir>/<session_id>.json, a dict keyed by agent_id:
  {"<agent_id>": {"agent_type": str, "count": int, "first_ts": float, "last_ts": float,
                  "last_tool": str, "warned": bool}}
Each call reads this file back, so keep its shape stable.

Limits per agent_type (soft and hard), resolved in this order, later layers winning:
  1. built-in: 150/200; pr-reviewer-* and opus-reviewer 80/110; diagnoser-* 40/60
  2. harness config key agent_budget (CLAUDE_HARNESS_CONFIG, default
     ~/.claude/hooks/harness.local.json): {"default": {"soft": N, "hard": M},
     "types": {"<glob>": {"soft": N, "hard": M}}}. The first matching glob in "types" wins,
     else "default" if present. A missing or invalid key keeps the value from layer 1.
  3. env AGENT_BUDGET_SOFT / AGENT_BUDGET_HARD, which override everything.

Decisions, with count = this call included:
  count > hard                    deny: "Tool-call budget exhausted (HARD calls)..."
  count >= soft and not warned    deny this call once: wrap-up message, warned=True;
                                  calls after that are allowed until hard
  otherwise                       allow, no output
The soft test is ">= soft" rather than "== soft" so a limit lowered mid-run still warns once.

Denials print {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
"permissionDecisionReason": ...}} on stdout with exit 0, and append one line to AGENT_BUDGET_LOG
(default ~/.claude/hooks/agent-budget.log).

Kill switch: CLAUDE_AGENT_BUDGET=0 does nothing at all. Any exception exits 0 silently (fail open).
State writes are load-modify-save under <session>.lock: O_EXCL create, retry every 50 ms up to
3 s, a lock older than 10 s is stale and removed. If the lock cannot be taken the write proceeds
unlocked (fail open, as task-gate does). State files untouched for 14 days are swept.
AGENT_BUDGET_STATE_DIR overrides the state dir (tests).
"""
import fnmatch, json, os, re, sys, tempfile, time

HOME = os.path.expanduser("~")
MAX_AGE = 14 * 86400
LOCK_WAIT = 3.0
LOCK_STALE = 10.0
LOCK_RETRY = 0.05
BUILTIN_DEFAULT = (150, 200)
BUILTIN_REVIEWER = (80, 110)
REVIEWER_GLOBS = ("pr-reviewer-*", "opus-reviewer")
BUILTIN_DIAGNOSER = (40, 60)
DIAGNOSER_GLOBS = ("diagnoser-*",)
STATE_DIR = (os.environ.get("AGENT_BUDGET_STATE_DIR")
             or os.path.join(HOME, ".claude", "hooks", "state", "agent-budget"))
LOG = (os.environ.get("AGENT_BUDGET_LOG")
       or os.path.join(HOME, ".claude", "hooks", "agent-budget.log"))
HARNESS_CONFIG = (os.environ.get("CLAUDE_HARNESS_CONFIG")
                  or os.path.join(HOME, ".claude", "hooks", "harness.local.json"))


# ---- limits --------------------------------------------------------------

def as_limit(v):
    """Positive int from a config or env value, else None."""
    if isinstance(v, bool):
        return None
    try:
        n = int(v.strip()) if isinstance(v, str) else int(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return n if n > 0 else None


def load_budget_cfg():
    """The agent_budget dict from HARNESS_CONFIG. Never raises: anything unreadable gives {}."""
    try:
        with open(HARNESS_CONFIG, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    raw = data.get("agent_budget") if isinstance(data, dict) else None
    return raw if isinstance(raw, dict) else {}


def pick(block, base):
    """(soft, hard) from a config block; a missing or invalid key keeps the base value."""
    soft, hard = base
    if isinstance(block, dict):
        soft = as_limit(block.get("soft")) or soft
        hard = as_limit(block.get("hard")) or hard
    return soft, hard


def limits(agent_type):
    """(soft, hard) for an agent type, through the three layers described in the docstring."""
    diagnoser = any(fnmatch.fnmatchcase(agent_type, g) for g in DIAGNOSER_GLOBS)
    reviewer = any(fnmatch.fnmatchcase(agent_type, g) for g in REVIEWER_GLOBS)
    if diagnoser:
        base = BUILTIN_DIAGNOSER
    elif reviewer:
        base = BUILTIN_REVIEWER
    else:
        base = BUILTIN_DEFAULT
    cfg = load_budget_cfg()
    types = cfg.get("types") if isinstance(cfg.get("types"), dict) else {}
    block = None
    for glob, b in types.items():
        if fnmatch.fnmatchcase(agent_type, glob):
            block = b
            break
    else:
        block = cfg.get("default")
    soft, hard = pick(block, base)
    soft = as_limit(os.environ.get("AGENT_BUDGET_SOFT")) or soft
    hard = as_limit(os.environ.get("AGENT_BUDGET_HARD")) or hard
    return soft, hard


# ---- state helpers -------------------------------------------------------

def session_path(sid):
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", sid or "") or "unknown"
    return os.path.join(STATE_DIR, safe + ".json")


def load(sid):
    try:
        with open(session_path(sid), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(sid, state):
    """Atomic write: temp file in the same directory, then os.replace."""
    os.makedirs(STATE_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=STATE_DIR, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, session_path(sid))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Lock:
    """Best-effort per-session lock. Never raises; proceeds unlocked if it cannot be taken."""

    def __init__(self, sid):
        self.path = session_path(sid)[:-len(".json")] + ".lock"
        self.held = False

    def __enter__(self):
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            deadline = time.time() + LOCK_WAIT
            while not self.held:
                try:
                    os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                    self.held = True
                except FileExistsError:
                    if time.time() >= deadline:
                        break
                    if not self._stale():
                        time.sleep(LOCK_RETRY)
        except OSError:
            pass
        return self

    def _stale(self):
        try:
            if time.time() - os.path.getmtime(self.path) > LOCK_STALE:
                os.unlink(self.path)
                return True
        except OSError:
            pass
        return False

    def __exit__(self, *exc):
        if self.held:
            try:
                os.unlink(self.path)
            except OSError:
                pass
        return False


def sweep():
    """Delete state files untouched for MAX_AGE. Opportunistic; never raises."""
    try:
        now = time.time()
        for name in os.listdir(STATE_DIR):
            path = os.path.join(STATE_DIR, name)
            if name.endswith(".json") and now - os.path.getmtime(path) > MAX_AGE:
                os.unlink(path)
    except OSError:
        pass


def num(v, default):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def bump(sid, agent_id, agent_type, tool, soft, hard, now):
    """Increment this agent's count under the lock and save. Returns (decision, count), where
    decision is None, "soft" or "hard"."""
    with Lock(sid):
        state = load(sid)
        prev = state.get(agent_id) if isinstance(state.get(agent_id), dict) else {}
        count = int(num(prev.get("count"), 0)) + 1
        warned = prev.get("warned") is True
        if count > hard:
            decision = "hard"
        elif count >= soft and not warned:
            decision = "soft"
            warned = True
        else:
            decision = None
        state[agent_id] = {"agent_type": agent_type, "count": count,
                           "first_ts": num(prev.get("first_ts"), now), "last_ts": now,
                           "last_tool": tool, "warned": warned}
        save(sid, state)
    return decision, count


def reason_for(decision, count, hard):
    if decision == "soft":
        return ("Tool-call budget: %d of %d calls used. Wrap up now: commit what works, then send your "
                "final report (what is done, what is not, blockers). Further calls are allowed until %d."
                % (count, hard, hard))
    return ("Tool-call budget exhausted (%d calls). Stop and send your final report now; do not call "
            "more tools." % hard)


def emit_deny(reason, log_line):
    try:
        with open(LOG, "a", encoding="utf-8", errors="replace") as f:
            f.write(log_line + "\n")
    except Exception:
        pass
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason}}))


def main():
    try:
        if os.environ.get("CLAUDE_AGENT_BUDGET") == "0":
            return  # kill switch
        p = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
        if not isinstance(p, dict):
            return
        if p.get("hook_event_name", "PreToolUse") != "PreToolUse":
            return
        agent_id = p.get("agent_id")
        if not agent_id:
            return  # main session: not budgeted
        agent_id = str(agent_id)
        sid = str(p.get("session_id") or "")
        tool = str(p.get("tool_name") or "")
        agent_type = str(p.get("agent_type") or "unknown")
        soft, hard = limits(agent_type)
        decision, count = bump(sid, agent_id, agent_type, tool, soft, hard, time.time())
        sweep()
        if decision is None:
            return
        reason = reason_for(decision, count, hard)
        log_line = "%s DENY %s session=%s agent=%s type=%s count=%d soft=%d hard=%d tool=%s" % (
            time.strftime("%Y-%m-%d %H:%M:%S"), decision, sid[:36], agent_id[:100],
            agent_type[:100], count, soft, hard, tool[:100])
    except Exception:
        return  # fail open
    emit_deny(reason, log_line)


if __name__ == "__main__":
    main()
