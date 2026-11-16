#!/usr/bin/env python3
"""Task gate: the main agent tracks its subagent dispatches as a dependency DAG of tasks.

PostToolUse TaskCreate / TaskUpdate keep a per-session record in
<state dir>/<session_id>.json, one entry per task id:
  status      pending | in_progress | completed   (a deleted task is dropped)
  owner       executor name or null
  subject     text
  blockedBy   ids this task waits on (addBlockedBy; addBlocks adds the reverse edge)
  metadata    dict from TaskCreate / TaskUpdate; a null value deletes the key
  dispatched  True once an Agent/Task call was allowed for the owner
Entries written before these keys existed load with the defaults (blockedBy [],
metadata {}, dispatched False). Ids not in state (deleted, or never seen) count
as satisfied blockers.

Definitions:
  open blocker  an id in blockedBy whose entry exists in state and is not completed
  ready         pending, no owner, no open blocker, metadata.dispatch is not False
  undispatched  in_progress, owner set, dispatched is not True

Gates (PreToolUse, main agent only):
  TaskUpdate    denied when the call's addBlockedBy / addBlocks edges would create a
                cycle (self-edges included; all edges of one call are checked in order);
                when status=in_progress is set on a task with open blockers; when
                owner=X is set on an in_progress task while another task is already
                in_progress with owner X.
  Agent|Task    reviewer agents (opus-reviewer, pr-reviewer-*) are denied unless model is
                omitted, "opus" or starts with "claude-opus", and effort is not low or
                medium. This runs before the ownership check, so a denied call marks nothing
                dispatched.
                Otherwise allowed only if an in_progress task owned by the subagent has no open
                blocker. On allow, dispatched=True on all its in_progress tasks.

Nudges (PostToolUse, main agent only):
  TaskUpdate status=completed  additionalContext listing the ready tasks, newly
                               unblocked ones first (nothing if none are ready).
  Agent|Task                   additionalContext listing undispatched and ready tasks
                               (nothing if there are none).

Payload shapes, probed on Claude Code 2.1.319:
  PreToolUse Agent:       tool_input {description, prompt, subagent_type, run_in_background}
  PostToolUse TaskCreate: tool_input {subject, description, metadata?};
                          tool_response {"task": {"id": "1", "subject": ..., "description": ...}}
  PostToolUse TaskUpdate: tool_input {taskId, status?, owner?, subject?, metadata?,
                          addBlockedBy?, addBlocks?};
                          tool_response {success, taskId, updatedFields, statusChange}
  Main agent == payload without `agent_id`; session_id is present on every event.
  PostToolUse context is emitted as {"hookSpecificOutput": {"hookEventName":
  "PostToolUse", "additionalContext": text}} on stdout with exit 0.

Exemptions: CLAUDE_MAIN_WRITE=1, and sessions matched by exempt_env in the machine-local
harness config (same file and rules as main-readonly.py: ~/.claude/hooks/harness.local.json,
or the path in CLAUDE_HARNESS_CONFIG; see README.md, "Local config"). CLAUDE_REQUIRE_TASK=0
is a kill switch for the gates. Agent|Task calls whose subagent_type is in EXEMPT_TYPES
(read-only helper agents: web-fetch, claude-code-guide, Explore, Plan) are allowed
without a task. They all apply to the gates only; recording always runs. Subagent payloads (agent_id present) are never gated. Fails open on any exception or unparseable input.
Denials are logged to ~/.claude/hooks/task-gate.log.

State writes are load-modify-save under <state dir>/<session>.lock: O_EXCL create,
retry every 50 ms up to 3 s, a lock older than 10 s is stale and removed. If the lock
cannot be taken the write proceeds unlocked (fail open).
TASK_GATE_STATE_DIR and TASK_GATE_LOG override the state dir and the log (tests).

Completion log: every real transition to completed (a task not yet in state counts as
one) appends one JSON line to ~/.claude/hooks/state/completed-work.jsonl with kind, ts
(UTC), session_id, cwd, task_id, subject, owner, metadata, agent_id and agent_type. It
sits beside state/task-gate/, not inside it, so sweep() never deletes it and nothing in
this hook trims it; it is the permanent record. TASK_GATE_COMPLETION_LOG overrides the
path (tests). A failed write is ignored and the hook still fails open.
"""
import fnmatch, json, os, re, sys, tempfile, time

HOME = os.path.expanduser("~")
DISPATCH_TOOLS = {"Agent", "Task"}
GATED_TOOLS = DISPATCH_TOOLS | {"TaskUpdate"}
DEFAULT_SUBAGENT = "general-purpose"
EXEMPT_TYPES = {"web-fetch", "claude-code-guide", "Explore", "Plan"}
MAX_AGE = 14 * 86400
LOCK_WAIT = 3.0
LOCK_STALE = 10.0
LOCK_RETRY = 0.05
STATE_DIR = (os.environ.get("TASK_GATE_STATE_DIR")
             or os.path.join(HOME, ".claude", "hooks", "state", "task-gate"))
LOG = (os.environ.get("TASK_GATE_LOG")
       or os.path.join(HOME, ".claude", "hooks", "task-gate.log"))
COMPLETION_LOG = (os.environ.get("TASK_GATE_COMPLETION_LOG")
                  or os.path.join(HOME, ".claude", "hooks", "state", "completed-work.jsonl"))
HARNESS_CONFIG = (os.environ.get("CLAUDE_HARNESS_CONFIG")
                  or os.path.join(HOME, ".claude", "hooks", "harness.local.json"))


def load_exempt_env():
    """{var: [glob, ...]} from the exempt_env key of HARNESS_CONFIG. Never raises: a missing,
    unreadable or malformed file yields {}, and a malformed entry is skipped."""
    try:
        with open(HARNESS_CONFIG, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    raw = data.get("exempt_env", {}) if isinstance(data, dict) else {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for var, pats in raw.items():
        if isinstance(var, str) and var and isinstance(pats, list) and all(isinstance(p, str) for p in pats):
            out[var] = pats
    return out


def env_exempt():
    """True when some exempt_env variable is non-empty and matches one of its globs."""
    for var, pats in load_exempt_env().items():
        val = os.environ.get(var, "")
        if val and any(fnmatch.fnmatchcase(val, p) for p in pats):
            return True
    return False


def deny_reason(subagent, blocked=()):
    text = ('No in_progress task owned by "%s". Before dispatching, TaskCreate a task for this work '
            '(or reuse one) and TaskUpdate it to status="in_progress", owner="%s" (load '
            'TaskCreate/TaskUpdate via ToolSearch if deferred), then retry the Agent call.'
            % (subagent, subagent))
    if blocked:
        text += (" Its in_progress task(s) %s still have open blockers; finish those first "
                 "(or remove the edge)." % ", ".join("#" + t for t in blocked))
    return text


REVIEWER_DENY = ('Reviewer agents run on Opus at high effort: dispatch %s with model omitted or "opus" '
                 'and effort omitted or high+ (got model=%r, effort=%r). The haiku/medium default in '
                 'CLAUDE.md is for executors only.')


def is_reviewer(subagent):
    return subagent == "opus-reviewer" or subagent.startswith("pr-reviewer-")


def reviewer_ok(ti):
    """True when a reviewer dispatch asks for Opus (or leaves the model unset) at high effort."""
    model, effort = ti.get("model"), ti.get("effort")
    if model not in (None, ""):
        m = str(model).lower()
        if not (m == "opus" or m.startswith("claude-opus")):
            return False
    return str(effort).lower() not in ("low", "medium")


# ---- state helpers -------------------------------------------------------

def session_path(sid):
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", sid or "") or "unknown"
    return os.path.join(STATE_DIR, safe + ".json")


def str_list(v):
    """Non-empty string ids from a list, de-duplicated, order kept; anything else gives []."""
    if not isinstance(v, list):
        return []
    return list(dict.fromkeys(str(x) for x in v if x not in (None, "")))


def norm(e):
    """Fill the fields that entries written before the DAG fields lack."""
    e.setdefault("status", "pending")
    e.setdefault("owner", None)
    e.setdefault("subject", None)
    e["blockedBy"] = str_list(e.get("blockedBy"))
    md = e.get("metadata")
    e["metadata"] = md if isinstance(md, dict) else {}
    e["dispatched"] = e.get("dispatched") is True
    return e


def new_entry(subject=None):
    return {"status": "pending", "owner": None, "subject": subject,
            "blockedBy": [], "metadata": {}, "dispatched": False}


def clean_meta(v):
    return {k: x for k, x in v.items() if x is not None} if isinstance(v, dict) else {}


def entries(state):
    # keys starting with "_" hold bookkeeping (e.g. "_agents"), never tasks
    return [(k, v) for k, v in state.items() if isinstance(v, dict) and not k.startswith("_")]


def load(sid):
    try:
        with open(session_path(sid), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    for _, e in entries(data):
        norm(e)
    return data


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


def task_id_from(resp):
    """Structured tool_response.task.id first, else the first '#<digits>' in the response text."""
    if isinstance(resp, dict):
        task = resp.get("task")
        if isinstance(task, dict) and task.get("id") not in (None, ""):
            return str(task["id"])
    text = resp if isinstance(resp, str) else json.dumps(resp, default=str)
    m = re.search(r"#(\d+)", text or "")
    return m.group(1) if m else None


def tkey(t):
    return (0, int(t), "") if t.isdigit() else (1, 0, t)


# ---- graph helpers -------------------------------------------------------

def open_among(state, ids):
    out = []
    for b in ids:
        e = state.get(b)
        if isinstance(e, dict) and e.get("status") != "completed":
            out.append(b)
    return out


def is_ready(state, tid):
    e = state[tid]
    meta = e.get("metadata") or {}
    return (e.get("status") == "pending" and not e.get("owner")
            and not open_among(state, e.get("blockedBy") or [])
            and meta.get("dispatch") is not False)


def ready_ids(state):
    return sorted((k for k, _ in entries(state) if is_ready(state, k)), key=tkey)


def undispatched_ids(state):
    return sorted((k for k, e in entries(state)
                   if e.get("status") == "in_progress" and e.get("owner") and e.get("dispatched") is not True),
                  key=tkey)


def fmt(state, tid, extra=""):
    return '#%s "%s"%s' % (tid, state[tid].get("subject") or "", extra)


def find_path(graph, start, goal):
    """BFS along blockedBy edges from start; the path [start, ..., goal] or None."""
    prev = {start: None}
    queue = [start]
    while queue:
        n = queue.pop(0)
        if n == goal:
            path = []
            while n is not None:
                path.append(n)
                n = prev[n]
            return list(reversed(path))
        for m in graph.get(n, []):
            if m not in prev:
                prev[m] = n
                queue.append(m)
    return None


def apply_edges(state, edges):
    """Apply (t, b) meaning 't blockedBy b' to a copy in order. Returns (error, graph)."""
    work = {k: list(e.get("blockedBy") or []) for k, e in entries(state)}
    for t, b in edges:
        path = find_path(work, b, t)
        if path is not None:
            cycle = " -> ".join("#" + n for n in [t] + path)
            return ("Edge #%s blockedBy #%s would create a cycle %s; the task list must stay a DAG."
                    % (t, b, cycle), None)
        lst = work.setdefault(t, [])
        if b not in lst:
            lst.append(b)
    return None, work


def owned_status(state, subagent):
    owned = sorted((k for k, e in entries(state)
                    if e.get("status") == "in_progress" and e.get("owner") == subagent), key=tkey)
    ok = any(not open_among(state, state[k].get("blockedBy") or []) for k in owned)
    return ok, owned


# ---- PreToolUse gates ----------------------------------------------------

def check_task_update(sid, ti):
    """Reason string to deny the TaskUpdate, or None. Read-only."""
    raw = ti.get("taskId")
    if raw in (None, ""):
        return None
    tid = str(raw)
    state = load(sid)
    cur = state.get(tid)
    cur_status = cur.get("status") if isinstance(cur, dict) else None
    status = ti.get("status")
    edges = ([(tid, b) for b in str_list(ti.get("addBlockedBy"))]
             + [(x, tid) for x in str_list(ti.get("addBlocks"))])
    err, work = apply_edges(state, edges)
    if err:
        return err
    if status == "in_progress":
        opn = open_among(state, work.get(tid, []))
        if opn:
            return ("Task #%s is blocked by open task(s) %s; finish those first (or remove the edge)."
                    % (tid, ", ".join("#" + b for b in opn)))
    owner = ti.get("owner")
    if owner and (status or cur_status) == "in_progress":
        for k, e in entries(state):
            if k != tid and e.get("status") == "in_progress" and e.get("owner") == owner:
                return ("Executor %s already owns in_progress task #%s; give parallel tasks distinct executor names."
                        % (owner, k))
    return None


def dispatch_gate(sid, subagent):
    """(allowed, owned in_progress ids). On allow, marks the owner's in_progress tasks dispatched."""
    with Lock(sid):
        state = load(sid)
        ok, owned = owned_status(state, subagent)
        if ok:
            for k in owned:
                state[k]["dispatched"] = True
            save(sid, state)
    return ok, owned


# ---- PostToolUse recording and nudges -----------------------------------

def post_create(sid, ti, resp):
    tid = task_id_from(resp)
    if not tid:
        return
    subject = ti.get("subject")
    if isinstance(resp, dict) and isinstance(resp.get("task"), dict):
        subject = resp["task"].get("subject") or subject
    with Lock(sid):
        state = load(sid)
        prev = state.get(tid)
        e = new_entry(subject)
        e["metadata"] = clean_meta(ti.get("metadata"))
        if isinstance(prev, dict):
            e["blockedBy"] = str_list(prev.get("blockedBy"))
        state[tid] = e
        save(sid, state)


def completion_text(state, done):
    ready = ready_ids(state)
    if not ready:
        return None
    newly = [t for t in ready if done in state[t].get("blockedBy", [])]
    order = newly + [t for t in ready if t not in newly]
    return ('Task #%s completed. Ready to dispatch now (no open blockers): %s. Dispatch ALL of them in '
            'parallel: next message = one TaskUpdate per task (status=in_progress, owner=<distinct executor '
            'name>), then the following message = one Agent call per task, all in that single message. '
            'Mark a task metadata {"dispatch": false} if it is yours or the user\'s, not an executor\'s.'
            % (done, ", ".join(fmt(state, t) for t in order)))


def completion_record(sid, tid, e, p):
    """One line of the completion log, built from the entry after the update."""
    p = p if isinstance(p, dict) else {}
    return {"kind": "task",
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "session_id": sid,
            "cwd": p.get("cwd") or None,
            "task_id": tid,
            "subject": e.get("subject"),
            "owner": e.get("owner"),
            "metadata": e.get("metadata") or {},
            "agent_id": p.get("agent_id") or None,
            "agent_type": p.get("agent_type") or None}


def log_completion(rec):
    """Append one record to the completion log. Never raises: a failed write must not break the hook."""
    try:
        os.makedirs(os.path.dirname(os.path.abspath(COMPLETION_LOG)), exist_ok=True)
        with open(COMPLETION_LOG, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def post_update(sid, ti, main_agent, p=None):
    raw = ti.get("taskId")
    if raw in (None, ""):
        return None
    tid = str(raw)
    status = ti.get("status")
    text = None
    record = None
    with Lock(sid):
        state = load(sid)
        if status == "deleted":
            state.pop(tid, None)
        else:
            e = state.get(tid)
            was_known = isinstance(e, dict)
            if not was_known:
                e = new_entry()
            old_status, old_owner = e.get("status"), e.get("owner")
            if status:
                e["status"] = str(status)
            if "owner" in ti:
                e["owner"] = ti["owner"] or None
            if ti.get("subject"):
                e["subject"] = str(ti["subject"])
            if ((status == "in_progress" and old_status != "in_progress")
                    or ("owner" in ti and e["owner"] != old_owner)):
                e["dispatched"] = False
            md = ti.get("metadata")
            if isinstance(md, dict):
                cur = dict(e.get("metadata") or {})
                for k, v in md.items():
                    if v is None:
                        cur.pop(k, None)
                    else:
                        cur[k] = v
                e["metadata"] = cur
            e["blockedBy"] = str_list((e.get("blockedBy") or []) + str_list(ti.get("addBlockedBy")))
            state[tid] = e
            if status == "completed" and (not was_known or old_status != "completed"):
                record = completion_record(sid, tid, e, p)
            for x in str_list(ti.get("addBlocks")):
                other = state.get(x)
                if not isinstance(other, dict):
                    other = new_entry()
                    state[x] = other
                other["blockedBy"] = str_list((other.get("blockedBy") or []) + [tid])
        if main_agent and status == "completed":
            text = completion_text(state, tid)
        save(sid, state)
        if record:
            log_completion(record)
    return text


def dispatch_nudge(sid):
    state = load(sid)
    und = undispatched_ids(state)
    rdy = ready_ids(state)
    if not und and not rdy:
        return None
    parts = []
    if und:
        parts.append("Still not dispatched: " + ", ".join(
            fmt(state, t, " (in_progress, owner %s, no Agent call yet)" % state[t].get("owner")) for t in und))
    if rdy:
        parts.append("ready: " + ", ".join(fmt(state, t) for t in rdy))
    return "; ".join(parts) + ". Dispatch them now in one message, one Agent call each."


def agent_id_from(resp):
    """agentId of an Agent/Task launch: tool_response dict key 'agentId' (also nested), else the
    'agentId: <hex>' line of the launch text (string, or content blocks with a text field)."""
    if isinstance(resp, dict):
        v = resp.get("agentId")
        if isinstance(v, str) and v:
            return v
        for x in resp.values():
            if isinstance(x, (dict, list)):
                r = agent_id_from(x)
                if r:
                    return r
        text = resp.get("text") if isinstance(resp.get("text"), str) else None
    elif isinstance(resp, list):
        for x in resp:
            r = agent_id_from(x)
            if r:
                return r
        return None
    else:
        text = resp if isinstance(resp, str) else None
    if text:
        m = re.search(r"agentId:\s*([A-Za-z0-9_-]+)", text)
        if m:
            return m.group(1)
    return None


def record_agent(sid, ti, resp):
    """PostToolUse Agent/Task: remember agentId and name -> subagent_type for SendMessage tracking."""
    sub = str(ti.get("subagent_type") or DEFAULT_SUBAGENT)
    keys = [k for k in (agent_id_from(resp), ti.get("name")) if isinstance(k, str) and k]
    if not keys:
        return
    with Lock(sid):
        state = load(sid)
        ag = state.get("_agents")
        if not isinstance(ag, dict):
            ag = {}
        for k in keys:
            ag[k] = sub
        state["_agents"] = ag
        save(sid, state)


def post_sendmessage(sid, ti):
    """SendMessage resumes an existing executor: mark its in_progress tasks dispatched.
    `to` is an agentId or a name (recorded at launch), or the executor name itself."""
    to = ti.get("to")
    if not isinstance(to, str) or not to:
        return
    with Lock(sid):
        state = load(sid)
        ag = state.get("_agents")
        sub = ag.get(to) if isinstance(ag, dict) else None
        if sub is None:
            sub = to  # fallback: `to` is the executor name
        changed = False
        for k, e in entries(state):
            if e.get("status") == "in_progress" and e.get("owner") == sub and e.get("dispatched") is not True:
                e["dispatched"] = True
                changed = True
        if changed:
            save(sid, state)


def post(sid, tool, ti, resp, main_agent, p=None):
    if tool == "SendMessage":
        if main_agent:
            post_sendmessage(sid, ti)
        return None
    if tool in DISPATCH_TOOLS and main_agent:
        record_agent(sid, ti, resp)
    if tool == "TaskCreate":
        post_create(sid, ti, resp)
        return None
    if tool == "TaskUpdate":
        return post_update(sid, ti, main_agent, p)
    if tool in DISPATCH_TOOLS and main_agent:
        return dispatch_nudge(sid)
    return None


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
        p = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
        if not isinstance(p, dict):
            return
        event = p.get("hook_event_name", "")
        tool = p.get("tool_name", "")
        ti = p.get("tool_input") if isinstance(p.get("tool_input"), dict) else {}
        sid = p.get("session_id") or ""
        main_agent = not p.get("agent_id")
        if event == "PostToolUse":
            text = post(sid, tool, ti, p.get("tool_response"), main_agent, p)
            sweep()
            if text:
                print(json.dumps({"hookSpecificOutput": {
                    "hookEventName": "PostToolUse", "additionalContext": text}}))
            return
        if event != "PreToolUse" or tool not in GATED_TOOLS:
            return
        if os.environ.get("CLAUDE_MAIN_WRITE") == "1":
            return
        if os.environ.get("CLAUDE_REQUIRE_TASK") == "0":
            return  # kill switch
        if env_exempt():
            return  # session matched by exempt_env in the local harness config: exempt
        if not main_agent:
            return  # subagent: unrestricted
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        if tool == "TaskUpdate":
            reason = check_task_update(sid, ti)
            if reason is None:
                return
            log_line = "%s DENY TaskUpdate session=%s reason=%s" % (ts, sid[:36], reason[:200])
        else:
            subagent = str(ti.get("subagent_type") or DEFAULT_SUBAGENT)
            if subagent in EXEMPT_TYPES:
                return
            if is_reviewer(subagent) and not reviewer_ok(ti):
                model, effort = ti.get("model"), ti.get("effort")
                reason = REVIEWER_DENY % (subagent, model, effort)
                log_line = "%s DENY Agent session=%s subagent=%s reviewer-model=%s effort=%s" % (
                    ts, sid[:36], subagent[:100], model, effort)
            else:
                ok, owned = dispatch_gate(sid, subagent)
                if ok:
                    return
                reason = deny_reason(subagent, owned)
                log_line = "%s DENY Agent session=%s subagent=%s" % (ts, sid[:36], subagent[:100])
    except Exception:
        return  # fail open
    emit_deny(reason, log_line)


if __name__ == "__main__":
    main()
