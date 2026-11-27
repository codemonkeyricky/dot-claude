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
  rounds      [{model, effort, ts}] one per allowed executor dispatch that marked the task
  diagnoses_done  count of allowed diagnoses of the task (hook-owned; read by the diagnosis cap, never from TaskUpdate)
  dispatch_pending_at, dispatched_at, finished_at   optional epoch floats, present only when set (see Pool)
Entries written before these keys existed load with the defaults (blockedBy [],
metadata {}, dispatched False, rounds []). Ids not in state (deleted, or never seen) count
as satisfied blockers. The running-executor state is these three time fields on each task
(state file <TASK_GATE_STATE_DIR or ~/.claude/hooks/state/task-gate>/<session_id>.json).

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
  Agent|Task    from a subagent (agent_id present): always denied, whatever subagent_type or model, and
                whatever the kill switches say. Subagents report back to the main session instead.
  Agent|Task    (main agent) subagent_type must be an executor, a reviewer, a diagnoser, an EXEMPT_TYPES entry or
                listed in model_gate.allow_types; anything else (general-purpose, fork, claude, a missing
                subagent_type, an unknown name) is denied with a pointer to a Haiku executor. Logged as
                "model-gate=type".
  Agent|Task    reviewer agents (opus-reviewer, pr-reviewer-*) are denied unless model is
                omitted, "opus" (the default), or starts with "claude-opus" (Sonnet and Haiku are denied
                for reviewers), and effort is not low or
                medium. This runs before the ownership check, so a denied call marks nothing
                dispatched.
                Otherwise allowed only if an in_progress task owned by the subagent has no open
                blocker. On allow, dispatched=True on all its in_progress tasks.
                Auto-claim (one Agent call is enough): when no in_progress task owned by the
                subagent is unblocked and it owns no in_progress task at all, the task the call
                names in its description ('· #<n>' or 'task #<n>', case-insensitive; the prompt
                is never read) is claimed if ready
                (pending, no owner, no open blocker, metadata.dispatch not False): status
                in_progress, owner=subagent, dispatched=True, metadata.auto_claimed=True. The
                call is then allowed. A named task that is not ready keeps the denial, with a
                sentence saying why the auto-claim failed (e.g. "#7 is blocked by #3, #4").
  Model gate    (Agent|Task, main agent) an executor is a subagent whose agent file
                <agents dir>/<name>.md has frontmatter model: haiku (agents dir ~/.claude/agents, or
                TASK_GATE_AGENTS_DIR; the name must match [A-Za-z0-9_-]+, so no path is read outside it).
                model omitted, empty, "haiku" or "claude-haiku*" is allowed (then the ownership gate runs).
                Any other model is the escalation ladder: Haiku only unless model_gate.sonnet_fallback is true (default
                false), and then Sonnet is the only non-Haiku model. It needs proof for the task the call is for (the
                first unblocked in_progress task the executor owns, else its first owned task, else the task
                the description names): (1) an earlier allowed Haiku round in rounds, and the task is not
                completed; (2) metadata.escalate_reason, a non-empty string; (3) metadata.escalate_class,
                exactly one of scale, verify, debug, coupled; (4) the model is in model_gate.escalate_to
                (default ["sonnet"]; a name also matches "claude-<name>*"; opus is dropped from any local list).
                Opus is never allowed for executors, whatever the class or rounds: Sonnet is the only
                escalation. A refused call claims and
                marks nothing, logged as "model-gate=escalation". An allowed non-Haiku call clears
                escalate_reason and escalate_class on that task under the same lock that appends the round,
                so each reason buys one round. Every allowed executor call appends {model, effort, ts} to
                rounds on each task it marks dispatched (model is "haiku" when omitted). Reviewer calls are
                not recorded.
  Round cap     an executor call (Haiku or not) is refused when a task it would mark already has
                len(rounds) >= model_gate.max_rounds (default 3; a non-int or value below 1 is ignored)
                + metadata.allow_extra_rounds (a non-negative int, else 0). The refusal says to stop and
                ask the user. Reviewers and exempt types are not capped.
  Agent|Task    diagnoser agents (diagnoser-*, run on Opus) are denied unless model is omitted, "opus" or starts with
                "claude-opus" and effort is not low (DIAGNOSER_DENY). Then the ownership gate runs: the diagnose task is
                owned by the diagnoser (auto-claim included). Its metadata.diagnoses names the target task, which must
                exist (not be the diagnose task itself) and have a Haiku round, metadata.escalate_reason and a valid
                escalate_class, and not be completed. Caps from the config key diagnose_gate: max_per_task (default 2; the target's
                hook-owned diagnoses_done must be below it), max_per_session (default 4; the session's count of allowed
                diagnoses, state key _diagnoses) and one diagnoser running at once (no other diagnose task in_progress,
                dispatched and not finished; the same task is refused too). A cap refusal says to stop and ask the user. On allow, under the same
                lock: the target's escalate_reason and escalate_class are cleared, its diagnoses_done (mirrored to metadata.diagnosis_count) and the session
                count go up, and the diagnose task is marked launched as an executor's is. Diagnosers do not count
                toward the executor pool. Under CLAUDE_REQUIRE_TASK=0 (ungated mode) a diagnoser is denied: it has no
                proof to check.
  SendMessage   (PreToolUse, main agent) denied when `to` maps through _agents to a diagnoser-* type, or starts with
                "diagnoser" (any case): diagnosers are one-shot. The kill switches do not lift this.

Pool (executors run in parallel, up to a cap; executors are the names in the executors list below):
  executors     config key "executors" (non-empty list of strings), else DEFAULT_EXECUTORS (ten names)
  pool size     env TASK_GATE_POOL_SIZE, else config "pool_size", else 8
  stale         env TASK_GATE_RUNNING_STALE_SEC, else config "running_stale_sec", else 3600 s.
                A malformed value (not a positive finite number) is skipped; the next source applies.
  require_background  config "require_background" (bool, default true). While on, an executor's Agent call
                is refused when run_in_background is not exactly true and any task is ready (omitted counts
                as foreground). Env TASK_GATE_REQUIRE_BACKGROUND=0 turns it off, =1 on. It is always off
                when env LOCAL_DISPATCH is non-empty (hybrid mode dispatches one executor at a time).
  dispatch_pending_at  set on the executor's tasks when its Agent call is allowed (PreToolUse); the call's
                PostToolUse turns it into dispatched_at and removes it
  dispatched_at set on the owner's in_progress tasks by the executor's PostToolUse Agent: the launch time
                (dispatch_pending_at when set, else now); finished_at is removed only when it is earlier
  finished_at   set by SubagentStop (agent_type = owner) on in_progress tasks that have a start and no finish
  start(e)      the later of dispatched_at and dispatch_pending_at among those set; None if neither
  finished(e)   finished_at set and at or after start(e)
  running       in_progress, owner an executor, start(e) set, not finished(e), and younger than its window:
                120 s when dispatch_pending_at is set and newer than dispatched_at (or dispatched_at unset),
                else running_stale_sec
  free          executors that own no in_progress task
  Pool cap      an executor's Agent call (after the ownership and auto-claim logic) is refused when the running
                executors other than its own number at least the pool size ("Executor pool full"), or when the
                foreground rule above applies. A refused call saves nothing. On allow, the executor's tasks
                get dispatch_pending_at = now. Reviewers and other subagents are not counted or capped, and
                CLAUDE_REQUIRE_TASK=0 and CLAUDE_MAIN_WRITE=1 lift the pool cap (as they lift the DAG gates).
  deficit       min(pool, running + ready) - running, capped by the free executors; never negative

Nudges (PostToolUse, main agent only):
  TaskUpdate status=completed  additionalContext listing the ready tasks, newly
                               unblocked ones first (nothing if none are ready).
  Agent|Task                   additionalContext listing undispatched and ready tasks
                               (nothing if there are none). When this call auto-claimed a
                               task, first a sentence "Auto-claimed task #id for <type> ..."
                               (once, from the marker the PreToolUse claim left).
                               Both nudges end with the pool line "Pool: R/P executors running; N ready; dispatch
                               K now: #a, #b (free: name1, name2)" when K > 0, else "Pool: R/P executors running;
                               N ready."
  SubagentStop (any agent)     agent_type's in_progress tasks with a start and no finish get finished_at = now.
                               No output; fails open.

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
is a kill switch for the task/DAG gates (ownership, round cap, escalation proof). Under it Agent calls still
pass the type gate and the reviewer rule, and only Haiku executors run (Sonnet and Opus are denied), and diagnosers are denied.
Agent|Task calls whose subagent_type is in EXEMPT_TYPES
(read-only helper agents: web-fetch, claude-code-guide, Explore) are allowed
without a task, only with a model omitted or haiku-family (any other model is denied). They all apply to the gates only; recording always runs. Agent|Task from a subagent (agent_id present) is always denied; other subagent payloads are never gated. Fails open on any exception or unparseable input.
Denials are logged to ~/.claude/hooks/task-gate.log (Agent denials tagged model-gate=type or
model-gate=escalation when the model gate refused them). Plan is no longer exempt: it runs on the
parent's model, so it is gated like any other non-executor type.

State writes are load-modify-save under <state dir>/<session>.lock: O_EXCL create,
retry every 50 ms up to 3 s, a lock older than 10 s is stale and removed. If the lock
cannot be taken the write proceeds unlocked (fail open).
TASK_GATE_STATE_DIR and TASK_GATE_LOG override the state dir and the log (tests).

Completion log: every real transition to completed (a task not yet in state counts as
one) appends one JSON line to ~/.claude/hooks/state/completed-work.jsonl with kind, ts
(UTC), session_id, cwd, task_id, subject, owner, metadata, agent_id, agent_type and rounds
([{model, effort}] copied from the task's rounds, ts omitted; [] if none). It
sits beside state/task-gate/, not inside it, so sweep() never deletes it and nothing in
this hook trims it; it is the permanent record. TASK_GATE_COMPLETION_LOG overrides the
path (tests). A failed write is ignored and the hook still fails open.

Escalation log: each allowed non-Haiku executor dispatch appends one line to the same file,
{kind "escalation", ts, session_id, task_id, subagent, model, effort, escalate_reason, class}, where
class is the escalate_class the round was bought with.

Harness config: the model_gate key of the same local file holds allow_types (extra allowed
subagent types, default []), escalate_to (sonnet-family models an escalation may name, default ["sonnet"]; opus and others are refused)
and max_rounds (executor rounds per task before the cap, default 3). sonnet_fallback (default false) turns
Sonnet escalation on: while it is false, executors are Haiku-only and every non-Haiku model is denied, Opus always.
diagnose_gate holds max_per_task (default 2) and max_per_session (default 4), the diagnoser caps.
Diagnosis log: each allowed diagnoser claim appends one line to the same file, {kind "diagnosis", ts, session_id,
task_id (the diagnose task), target, escalate_reason, class}.
TASK_GATE_AGENTS_DIR overrides the agents dir (tests).
"""
import fnmatch, json, math, os, re, sys, tempfile, time

HOME = os.path.expanduser("~")
DISPATCH_TOOLS = {"Agent", "Task"}
GATED_TOOLS = DISPATCH_TOOLS | {"TaskUpdate"}
DEFAULT_SUBAGENT = "general-purpose"
EXEMPT_TYPES = {"web-fetch", "claude-code-guide", "Explore"}
ESCALATE_DEFAULT = ["sonnet"]
OPUS_DENY_TEXT = ("opus is never allowed for executors (Haiku first, Sonnet is the only escalation; "
                  "Opus is for the main session and reviewers)")
SUBAGENT_DENY = ("Subagents may not dispatch agents (prevents unreviewed Opus/Sonnet spend); "
                 "report back to the main session instead.")
ESCALATE_CLASSES = ("scale", "verify", "debug", "coupled")
MAX_ROUNDS_DEFAULT = 3
AGENT_NAME = re.compile(r"[A-Za-z0-9_-]+")
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


DEFAULT_EXECUTORS = ["qorvax-tel", "zyrene-vosk", "thal-ummir", "kreeva-osh", "vexlor-ain",
                     "nyxa-prell", "oomrak-zhee", "xilth-varo", "juun-ketrax", "plimm-oraxi"]
DEFAULT_POOL = 8
DEFAULT_STALE = 3600.0
PENDING_WINDOW = 120.0
TIME_FIELDS = ("dispatch_pending_at", "dispatched_at", "finished_at")


def load_config():
    """The harness config as a dict. Never raises: a missing, unreadable or non-object file yields {}."""
    try:
        with open(HARNESS_CONFIG, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def executors():
    """Executor names: config key "executors" when it is a non-empty list of strings, else DEFAULT_EXECUTORS."""
    v = load_config().get("executors")
    if isinstance(v, list) and v and all(isinstance(x, str) and x for x in v):
        return list(dict.fromkeys(v))
    return list(DEFAULT_EXECUTORS)


def _as_int(x):
    if isinstance(x, (bool, float)):
        raise ValueError(x)
    return int(x)


def _as_float(x):
    if isinstance(x, bool):
        raise ValueError(x)
    return float(x)


def setting(env_name, key, default, conv):
    """First usable value of: env var env_name, config key, default. A value that does not convert,
    or is not a finite number above zero, is skipped (the next source applies)."""
    for raw in (os.environ.get(env_name), load_config().get(key)):
        if raw in (None, ""):
            continue
        try:
            v = conv(raw)
        except (TypeError, ValueError):
            continue
        if v > 0 and math.isfinite(v):
            return v
    return default


def pool_size():
    return int(setting("TASK_GATE_POOL_SIZE", "pool_size", DEFAULT_POOL, _as_int))


def stale_sec():
    return setting("TASK_GATE_RUNNING_STALE_SEC", "running_stale_sec", DEFAULT_STALE, _as_float)


def require_background():
    """Whether an executor's foreground Agent call is refused while tasks are ready. Off whenever env
    LOCAL_DISPATCH is non-empty; env TASK_GATE_REQUIRE_BACKGROUND "0" or "1" wins; else config
    require_background (a bool, default true)."""
    if os.environ.get("LOCAL_DISPATCH"):
        return False
    env = os.environ.get("TASK_GATE_REQUIRE_BACKGROUND")
    if env == "0":
        return False
    if env == "1":
        return True
    v = load_config().get("require_background")
    return v if isinstance(v, bool) else True


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


def load_model_gate():
    """(allow_types, escalate_to) from the model_gate key of HARNESS_CONFIG. Never raises: a missing or
    malformed file gives ([], ["sonnet"]); a malformed entry inside a list is skipped."""
    allow, escalate = [], list(ESCALATE_DEFAULT)
    try:
        with open(HARNESS_CONFIG, encoding="utf-8") as f:
            data = json.load(f)
        raw = data.get("model_gate") if isinstance(data, dict) else None
        if isinstance(raw, dict):
            if isinstance(raw.get("allow_types"), list):
                allow = [x for x in raw["allow_types"] if isinstance(x, str) and x]
            if isinstance(raw.get("escalate_to"), list):
                escalate = [x for x in raw["escalate_to"]
                            if isinstance(x, str) and x and is_sonnet(x) and not is_opus(x)]
    except Exception:
        pass
    return allow, escalate


def load_max_rounds():
    """model_gate.max_rounds from HARNESS_CONFIG: the executor rounds a task may use before the cap (default 3).
    Never raises; a missing file, a non-int, a bool or a value below 1 gives the default."""
    try:
        with open(HARNESS_CONFIG, encoding="utf-8") as f:
            data = json.load(f)
        raw = data.get("model_gate") if isinstance(data, dict) else None
        v = raw.get("max_rounds") if isinstance(raw, dict) else None
    except Exception:
        return MAX_ROUNDS_DEFAULT
    if isinstance(v, int) and not isinstance(v, bool) and v >= 1:
        return v
    return MAX_ROUNDS_DEFAULT


def load_sonnet_fallback():
    """model_gate.sonnet_fallback from HARNESS_CONFIG: only a JSON true turns Sonnet escalation on. Never raises."""
    raw = load_config().get("model_gate")
    return isinstance(raw, dict) and raw.get("sonnet_fallback") is True


def load_diagnose_gate():
    """(max_per_task, max_per_session) from diagnose_gate in HARNESS_CONFIG (defaults 2 and 4). A non-int, a bool
    or a value below 1 gives the default. Never raises."""
    raw = load_config().get("diagnose_gate")
    raw = raw if isinstance(raw, dict) else {}
    out = []
    for key in ("max_per_task", "max_per_session"):
        v = raw.get(key)
        out.append(v if isinstance(v, int) and not isinstance(v, bool) and v >= 1 else DIAGNOSE_DEFAULT[key])
    return out[0], out[1]


def agents_dir():
    return os.environ.get("TASK_GATE_AGENTS_DIR") or os.path.join(HOME, ".claude", "agents")


def agent_model(subagent):
    """The lowercased model: value in the frontmatter of <agents dir>/<subagent>.md, or None. Only a plain
    name is looked up, so no path outside the agents dir is read. Never raises."""
    if not isinstance(subagent, str) or not AGENT_NAME.fullmatch(subagent):
        return None
    try:
        with open(os.path.join(agents_dir(), subagent + ".md"), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except Exception:
        return None
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*?)\s*$", line)
        if m and m.group(1) == "model":
            return m.group(2).strip("\"'").lower() or None
    return None


def model_in(model, names):
    """True when model is one of names; a name also matches 'claude-<name>...' (case-insensitive)."""
    if not isinstance(model, str):
        return False
    m = model.lower()
    return any(m == n.lower() or m.startswith("claude-" + n.lower()) for n in names)


def is_haiku(model):
    return model_in(model, ["haiku"])


def allowed_type(subagent, allow_types):
    """An executor (agent file with model: haiku) or an explicit model_gate.allow_types entry."""
    return is_haiku(agent_model(subagent)) or subagent in allow_types


def exempt_decision(subagent, raw, ts, sid):
    """None to allow an EXEMPT_TYPES dispatch: its model must be omitted or haiku-family. Else (reason, log_line)."""
    if raw in (None, "") or is_haiku(raw):
        return None
    return (EXEMPT_DENY % subagent[:100],
            "%s DENY Agent session=%s subagent=%s model-gate=exempt model=%s" % (
                ts, sid[:36], subagent[:100], str(raw)[:100]))


def allow_types_decision(subagent, raw, ts, sid):
    """None when the model fits the type. An executor (agent file with model: haiku) passes as before; a type
    reached only through model_gate.allow_types needs an explicit haiku-family model. Else (reason, log_line)."""
    if is_haiku(agent_model(subagent)) or is_haiku(raw):
        return None
    return (ALLOW_TYPES_DENY % subagent[:100],
            "%s DENY Agent session=%s subagent=%s model-gate=type model=%s" % (
                ts, sid[:36], subagent[:100], str(raw)[:100]))


TYPE_DENY = ('Subagent type "%s" is not allowed from the main session. Dispatch a Haiku executor instead: '
             'one whose agent file in ~/.claude/agents has model: haiku (qorvax-tel, zyrene-vosk, thal-ummir, '
             '...), with a task it owns. general-purpose, fork, claude and other types are not dispatchable.')
EXEMPT_DENY = "Exempt type %s must run on Haiku (model omitted or haiku)"
ALLOW_TYPES_DENY = ('Subagent type "%s" is listed in model_gate.allow_types, so it runs only with an explicit '
                    'model: haiku (omitted is not enough: it can inherit the parent model, e.g. Opus).')
ESCALATE_HINT = ('Next step after a Haiku round fails: split the task into smaller Haiku tasks; for verify-only '
                 'failures retry Haiku at effort high; for coupled or debug failures TaskUpdate metadata '
                 '{escalate_reason, escalate_class: scale|verify|debug|coupled}, dispatch a diagnose task (owner '
                 'diagnoser-1, metadata {diagnoses: "<failed task id>"}), then re-dispatch the failed task to Haiku '
                 'with the spec. Opus is never used by executors.')
HAIKU_ONLY_TEXT = ('Executors are Haiku-only: model_gate.sonnet_fallback is not true in the local harness config, '
                   'so no Sonnet escalation')
DIAGNOSE_DEFAULT = {"max_per_task": 2, "max_per_session": 4}
DIAGNOSER_DENY = ('Diagnoser agents run on Opus: dispatch %s with model omitted or "opus" and effort other than low '
                  '(got model=%r, effort=%r).')
DIAGNOSER_UNGATED = ('Diagnoser agents need the task gate for their proof, and CLAUDE_REQUIRE_TASK=0 or '
                     'CLAUDE_MAIN_WRITE=1 is set. Dispatch the diagnoser with the gate on.')
DIAGNOSER_SEND = ('Diagnosers are one-shot: dispatch a new diagnose task with the prior spec and the new failing output.')
DIAGNOSE_NO_TARGET = ('Diagnose task needs metadata.diagnoses naming the failed task id (TaskUpdate metadata '
                      '{diagnoses: "<id>"}).')


def is_opus(model):
    return isinstance(model, str) and "opus" in model.lower()


def is_sonnet(model):
    return isinstance(model, str) and "sonnet" in model.lower()


def escalation_problem(state, subagent, ti, model):
    """(problem, task id, escalate_reason, escalate_class). problem is None when the non-Haiku dispatch is proven:
    the task has an earlier allowed Haiku round and is not completed; metadata.escalate_reason is set;
    metadata.escalate_class is one of ESCALATE_CLASSES; model is in model_gate.escalate_to; and the model is
    never opus. The task is the first unblocked
    in_progress task the executor owns, else its first owned task, else the task the call names. Read-only."""
    _, escalate = load_model_gate()
    owned = owned_status(state, subagent)[1]
    tgt = next((k for k in owned if not open_among(state, state[k].get("blockedBy") or [])), None)
    if tgt is None:
        tgt = owned[0] if owned else task_ref(ti)
    e = state.get(tgt) if tgt is not None else None
    if not isinstance(e, dict):
        return ("Model %r denied: no in_progress task owned by %s or named in the description to escalate. %s"
                % (model, subagent, ESCALATE_HINT)), tgt, None, None
    rounds = [r for r in e.get("rounds") or [] if isinstance(r, dict)]
    md = e.get("metadata") or {}
    missing = []
    if e.get("status") == "completed":
        missing.append("#%s is already completed" % tgt)
    elif not any(is_haiku(r.get("model")) for r in rounds):
        missing.append("no earlier Haiku round on #%s (dispatch it with model omitted first)" % tgt)
    raw = md.get("escalate_reason")
    reason = raw.strip() if isinstance(raw, str) else ""
    if not reason:
        missing.append("no escalate_reason in #%s metadata" % tgt)
    cls = md.get("escalate_class")
    if not (isinstance(cls, str) and cls in ESCALATE_CLASSES):
        cls = None
        missing.append("no valid escalate_class in #%s metadata (one of %s)" % (tgt, ", ".join(ESCALATE_CLASSES)))
    if not model_in(model, escalate):
        missing.append("model %r is not in model_gate.escalate_to %s (local harness config)" % (model, escalate))
    if is_opus(model):
        missing.append(OPUS_DENY_TEXT)
    elif not is_sonnet(model):
        missing.append("model %r is not sonnet-family (an executor may escalate only to sonnet)" % model)
    if not load_sonnet_fallback():
        missing.insert(0, HAIKU_ONLY_TEXT)
    if missing:
        return ("Model %r denied for #%s: %s. %s" % (model, tgt, "; ".join(missing), ESCALATE_HINT)), tgt, None, None
    return None, tgt, reason, cls


def deny_reason(subagent, blocked=(), note=""):
    text = ('No in_progress task owned by "%s". Before dispatching, TaskCreate a task for this work '
            '(or reuse one) and TaskUpdate it to status="in_progress", owner="%s" (load '
            'TaskCreate/TaskUpdate via ToolSearch if deferred), then retry the Agent call.'
            % (subagent, subagent))
    if blocked:
        text += (" Its in_progress task(s) %s still have open blockers; finish those first "
                 "(or remove the edge)." % ", ".join("#" + t for t in blocked))
    if note:
        text += " " + note
    return text


REVIEWER_DENY = ('Reviewer agents run on Opus at high effort: dispatch %s with model omitted or "opus" '
                 'and effort omitted or high+ (got model=%r, effort=%r). Sonnet and Haiku are not allowed for '
                 'reviewers; the haiku/medium default in CLAUDE.md is for executors only.')


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


def is_diagnoser(subagent):
    return isinstance(subagent, str) and fnmatch.fnmatchcase(subagent, "diagnoser-*")


def diagnoser_ok(ti):
    """True when a diagnoser dispatch asks for Opus (or leaves the model unset) at an effort other than low."""
    model, effort = ti.get("model"), ti.get("effort")
    if model not in (None, ""):
        m = str(model).lower()
        if not (m == "opus" or m.startswith("claude-opus")):
            return False
    return str(effort).lower() != "low"


# ---- state helpers -------------------------------------------------------

def session_path(sid):
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", sid or "") or "unknown"
    return os.path.join(STATE_DIR, safe + ".json")


def str_list(v):
    """Non-empty string ids from a list, de-duplicated, order kept; anything else gives []."""
    if not isinstance(v, list):
        return []
    return list(dict.fromkeys(str(x) for x in v if x not in (None, "")))


def is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def norm(e):
    """Fill the fields that entries written before the DAG fields lack."""
    e.setdefault("status", "pending")
    e.setdefault("owner", None)
    e.setdefault("subject", None)
    e["blockedBy"] = str_list(e.get("blockedBy"))
    md = e.get("metadata")
    e["metadata"] = md if isinstance(md, dict) else {}
    e["dispatched"] = e.get("dispatched") is True
    rs = e.get("rounds")
    e["rounds"] = [r for r in rs if isinstance(r, dict)] if isinstance(rs, list) else []
    e["diagnoses_done"] = count_of(e.get("diagnoses_done"))
    for k in TIME_FIELDS:  # optional epoch floats; absent stays absent
        if not is_num(e.get(k)):
            e.pop(k, None)
    return e


def new_entry(subject=None):
    return {"status": "pending", "owner": None, "subject": subject,
            "blockedBy": [], "metadata": {}, "dispatched": False, "rounds": [], "diagnoses_done": 0}


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


def start_of(e):
    """start(e): the later of dispatched_at and dispatch_pending_at among those set; None if neither."""
    vals = [e[k] for k in ("dispatched_at", "dispatch_pending_at") if is_num(e.get(k))]
    return max(vals) if vals else None


def is_finished(e):
    """finished(e): finished_at set and at or after start(e)."""
    s = start_of(e)
    return s is not None and is_num(e.get("finished_at")) and e["finished_at"] >= s


def is_running(e, now, execs=None, stale=None):
    """True for an executor's in_progress task with a start that is not finished and is younger than its
    window: PENDING_WINDOW when dispatch_pending_at is set and (dispatched_at unset or dispatch_pending_at
    > dispatched_at), else stale."""
    execs = executors() if execs is None else execs
    stale = stale_sec() if stale is None else stale
    if e.get("status") != "in_progress" or e.get("owner") not in execs:
        return False
    s = start_of(e)
    if s is None or is_finished(e):
        return False
    d, p = e.get("dispatched_at"), e.get("dispatch_pending_at")
    pending_newer = is_num(p) and (not is_num(d) or p > d)
    return now - s < (PENDING_WINDOW if pending_newer else stale)


def running_ids(state, now, execs=None, stale=None, exclude=None):
    """Sorted ids of running tasks, leaving out those owned by exclude."""
    execs = executors() if execs is None else execs
    stale = stale_sec() if stale is None else stale
    return sorted((k for k, e in entries(state)
                   if e.get("owner") != exclude and is_running(e, now, execs, stale)), key=tkey)


def free_executors(state, execs=None):
    """Executors (in config order) that own no in_progress task."""
    execs = executors() if execs is None else execs
    return [x for x in execs
            if not any(e.get("status") == "in_progress" and e.get("owner") == x for _, e in entries(state))]


def deficit(state, now=None):
    """How many more executors to dispatch now: min(pool, running + ready) - running, capped by the free
    executors; never negative."""
    now = time.time() if now is None else now
    rc = len(running_ids(state, now))
    k = min(pool_size(), rc + len(ready_ids(state))) - rc
    return max(0, min(k, len(free_executors(state))))


def pool_line(state, now=None):
    """Pool status for nudges: running/pool, ready count, and when deficit > 0 the first K ready ids
    with the first K free executor names."""
    now = time.time() if now is None else now
    execs = executors()
    rdy = ready_ids(state)
    head = "Pool: %d/%d executors running; %d ready" % (len(running_ids(state, now, execs)), pool_size(), len(rdy))
    k = deficit(state, now)
    if k > 0:
        return "%s; dispatch %d now: %s (free: %s)" % (
            head, k, ", ".join("#" + t for t in rdy[:k]), ", ".join(free_executors(state, execs)[:k]))
    return head + "."


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


# Only these forms name a task, and only in the description: '· #<n>' (middle dot, optional spaces)
# or 'task #<n>' (case-insensitive). A '#<n>' elsewhere (e.g. "review PR #5") names nothing.
TASK_REF = re.compile(r"(?:\u00b7\s*|\btask )#(\d+)\b", re.IGNORECASE)


def task_ref(ti):
    """Id named by an Agent call's description, or None. The prompt is never read."""
    v = ti.get("description")
    m = TASK_REF.search(v) if isinstance(v, str) else None
    return m.group(1) if m else None


def is_stale_claim(e):
    """True for an in_progress auto-claim whose call never reached PostToolUse (still has its marker)."""
    return e.get("status") == "in_progress" and bool((e.get("metadata") or {}).get("auto_claim_pending"))


STUCK_HINT = ('; if that claim is stuck (its call was denied), reset it with TaskUpdate status pending, '
              'owner ""')


def claim_problem(state, tid):
    """Why #tid cannot be auto-claimed, or None when it is ready (the is_ready rules). An in_progress task
    is never taken here: a stale auto-claim (auto_claim_pending still set) is retaken only by its own
    owner, in dispatch_gate. Any other executor is told how to reset it."""
    e = state.get(tid)
    if not isinstance(e, dict):
        return "#%s is not in the task list" % tid
    if e.get("status") != "pending":
        hint = STUCK_HINT if e.get("status") == "in_progress" else ""
        return "#%s is %s%s" % (tid, e.get("status") or "unknown", hint)
    if e.get("owner"):
        return "#%s is owned by %s" % (tid, e["owner"])
    opn = open_among(state, e.get("blockedBy") or [])
    if opn:
        return "#%s is blocked by %s" % (tid, ", ".join("#" + b for b in opn))
    if (e.get("metadata") or {}).get("dispatch") is False:
        return "#%s has metadata dispatch false" % tid
    return None


def claim(state, tid, subagent, tool_use_id):
    """Auto-claim #tid for subagent (in memory; the caller saves). Leaves a pending marker
    (metadata.auto_claim_pending) keyed by the call's tool_use_id; PostToolUse clears it. Without a
    tool_use_id nothing can be correlated, so no marker is stored (no shared key)."""
    e = state[tid]
    e["status"] = "in_progress"
    e["owner"] = subagent
    e["dispatched"] = True
    e["metadata"]["auto_claimed"] = True
    e["metadata"].pop("auto_claim_pending", None)
    drop_autoclaim_records(state, tid)  # a retaken claim replaces its earlier call's record
    if tool_use_id:
        e["metadata"]["auto_claim_pending"] = tool_use_id
        state.setdefault("_autoclaims", {})[tool_use_id] = {"task": tid, "subagent": subagent}


def drop_autoclaim_records(state, tid):
    """Forget every pending auto-claim record for #tid (its call's PostToolUse must not announce it)."""
    claims = state.get("_autoclaims")
    if not isinstance(claims, dict):
        return
    for k in [k for k, v in claims.items() if isinstance(v, dict) and v.get("task") == tid]:
        claims.pop(k)
    if not claims:
        state.pop("_autoclaims", None)


def utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def allow_extra_rounds(md):
    """metadata.allow_extra_rounds as a non-negative int; anything else (non-int, bool, negative) is 0."""
    v = md.get("allow_extra_rounds")
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0


def cap_problem(state, tid, max_rounds):
    """None, or the refusal when task tid already has max_rounds + allow_extra_rounds executor rounds."""
    e = state.get(tid)
    if not isinstance(e, dict):
        return None
    rounds = e.get("rounds")
    k = len(rounds) if isinstance(rounds, list) else 0
    cap = max_rounds + allow_extra_rounds(e.get("metadata") or {})
    if k < cap:
        return None
    return ("Task #%s already used %d rounds (cap %d). Stop and ask the user; after their OK set "
            "metadata.allow_extra_rounds." % (tid, k, cap))


def clear_escalation(state, tid):
    """Drop escalate_reason and escalate_class from task tid's metadata: one reason buys one round."""
    e = state.get(tid) if tid is not None else None
    if isinstance(e, dict):
        md = dict(e.get("metadata") or {})
        md.pop("escalate_reason", None)
        md.pop("escalate_class", None)
        e["metadata"] = md


def capacity_problem(state, subagent, ti, now):
    """Why an executor's dispatch is refused for pool capacity, or for a foreground call while other tasks are
    ready (require_background); None when it may proceed. Read-only."""
    pool = pool_size()
    others = running_ids(state, now, exclude=subagent)
    if len(others) >= pool:
        return ("Executor pool full: %d of %d running (%s). Wait for a report, mark it completed, then dispatch."
                % (len(others), pool, ", ".join("#" + k for k in others)))
    if require_background() and ti.get("run_in_background") is not True:
        rdy = ready_ids(state)
        if rdy:
            return ("Dispatch executors with run_in_background: true while other tasks are ready: %s."
                    % ", ".join("#" + k for k in rdy))
    return None


def count_of(v):
    """v as a non-negative int; anything else (a bool included) is 0."""
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def diagnosis_problem(state, sid, subagent, marked, now):
    """(problem, record) for a diagnoser claim on the marked tasks. problem is None when the diagnosis is proven and
    within its caps; record is then the completion-log line. The diagnose task is the first marked task whose
    metadata has diagnoses, and that names the target. The target must exist (and not be the diagnose task) and have
    a Haiku round, metadata.escalate_reason and a valid escalate_class. Caps: the target's hook-owned diagnoses_done against
    max_per_task, the session's _diagnoses against max_per_session, no other diagnoser still running, and the diagnose
    task itself not running again. A completed target is denied. Read-only."""
    dtid = next((k for k in marked if isinstance(state.get(k), dict)
                 and "diagnoses" in (state[k].get("metadata") or {})), None)
    if dtid is None:
        return DIAGNOSE_NO_TARGET, None
    raw = state[dtid]["metadata"]["diagnoses"]
    tgt = str(raw).strip() if isinstance(raw, (str, int)) and not isinstance(raw, bool) else ""
    target = dict(entries(state)).get(tgt) if tgt != dtid else None
    if not isinstance(target, dict):
        return ("Diagnose task #%s: metadata.diagnoses %r names no existing task other than itself."
                % (dtid, raw)), None
    if target.get("status") == "completed":
        return ("Diagnosis of #%s denied: target already completed; nothing to diagnose." % tgt), None
    md = target.get("metadata") or {}
    missing = []
    if not any(is_haiku(r.get("model")) for r in rounds_of(target) if isinstance(r, dict)):
        missing.append("#%s has no Haiku round (a diagnosis follows a failed Haiku round)" % tgt)
    reason = md.get("escalate_reason")
    reason = reason.strip() if isinstance(reason, str) else ""
    if not reason:
        missing.append("#%s has no escalate_reason (set it with TaskUpdate metadata first)" % tgt)
    cls = md.get("escalate_class")
    if not (isinstance(cls, str) and cls in ESCALATE_CLASSES):
        cls = None
        missing.append("#%s has no valid escalate_class (one of %s)" % (tgt, ", ".join(ESCALATE_CLASSES)))
    if missing:
        return "Diagnosis of #%s denied: %s." % (tgt, "; ".join(missing)), None
    per_task, per_session = load_diagnose_gate()
    done_task, done_session = count_of(target.get("diagnoses_done")), count_of(state.get("_diagnoses"))
    if done_task >= per_task:
        return ("#%s already had %d diagnoses (cap %d). Stop and ask the user before another diagnosis."
                % (tgt, done_task, per_task)), None
    if done_session >= per_session:
        return ("This session already ran %d diagnoses (cap %d). Stop and ask the user before another diagnosis."
                % (done_session, per_session)), None
    running = sorted((k for k, x in entries(state) if is_diagnoser(x.get("owner"))
                      and is_running(x, now, execs=[x.get("owner")])), key=tkey)
    busy = [k for k in running if k != dtid]
    if busy:
        return ("Another diagnoser is still running on %s. One diagnoser at a time: wait for its report."
                % ", ".join("#" + k for k in busy)), None
    if dtid in running:
        return ("Diagnose task #%s is already running under a diagnoser. Wait for its report; "
                "no second run of the same task." % dtid), None
    return None, {"kind": "diagnosis", "ts": utc_now(), "session_id": sid, "task_id": dtid, "target": tgt,
                  "escalate_reason": reason, "class": cls}


def apply_diagnosis(state, rec):
    """Under the lock, for an allowed diagnosis: the target's escalation is spent (one reason buys one diagnosis),
    its diagnoses_done goes up (metadata.diagnosis_count mirrors it), and so does the session's _diagnoses count."""
    tgt = rec["target"]
    clear_escalation(state, tgt)
    e = state[tgt]
    e["diagnoses_done"] = count_of(e.get("diagnoses_done")) + 1
    md = e["metadata"]
    md["diagnosis_count"] = e["diagnoses_done"]  # informational mirror; the cap reads diagnoses_done
    state["_diagnoses"] = count_of(state.get("_diagnoses")) + 1


def dispatch_gate(sid, subagent, ti=None, tool_use_id=None, model=None, round_rec=None, diagnose=False):
    """Dict {ok, owned, note, cap, model_problem, escalation}. cap is the round-cap refusal text (None otherwise);
    the caller denies with it alone. model is the call's non-Haiku model (None when omitted
    or Haiku). It is checked first, under the lock, by escalation_problem, so a refused call claims and marks
    nothing. round_rec ({model, effort, ts}) is appended to each task an allowed call marks dispatched (None for
    reviewers). escalation is the completion-log record of an allowed non-Haiku call. Otherwise: on allow, the
    owner's in_progress tasks are marked dispatched. With no unblocked in_progress task of the owner, the task
    the call names is auto-claimed when ready; note says why the auto-claim failed."""
    ti = ti or {}
    out = {"ok": False, "owned": [], "note": "", "cap": None, "pool": None, "model_problem": None, "escalation": None,
           "diagnosis": None, "diag_problem": None}
    note = ""
    max_rounds = load_max_rounds()
    execs = executors()
    now = time.time()
    with Lock(sid):
        state = load(sid)
        tgt, reason, cls = None, None, None
        if model is not None:
            out["model_problem"], tgt, reason, cls = escalation_problem(state, subagent, ti, model)
            if out["model_problem"]:
                return out
        ok, owned = owned_status(state, subagent)
        out["owned"] = owned
        tid = task_ref(ti)
        own = state.get(tid) if tid is not None else None
        claimed = None
        if (tool_use_id and isinstance(own, dict) and is_stale_claim(own) and own.get("owner") == subagent
                and not open_among(state, own.get("blockedBy") or [])):
            # the same executor re-dispatching its own stuck auto-claim (its earlier call was denied or
            # never ran): the new call's marker replaces the old one
            claim(state, tid, subagent, tool_use_id)
            claimed, ok = tid, True
        elif not ok:
            if tid is not None:
                why = ("%s already owns in_progress %s" % (subagent, ", ".join("#" + k for k in owned))
                       if owned else claim_problem(state, tid))
                if why is None:
                    claim(state, tid, subagent, tool_use_id)
                    claimed, ok = tid, True
                else:
                    note = "Auto-claim of #%s failed: %s." % (tid, why)
        if ok:
            marked = list(owned) + ([claimed] if claimed and claimed not in owned else [])
            capped = None
            if round_rec:
                capped = next((p for p in (cap_problem(state, k, max_rounds) for k in marked) if p), None)
            pool_why = capacity_problem(state, subagent, ti, now) if (not capped and subagent in execs) else None
            diag_why, diag = (None, None)
            if diagnose:
                diag_why, diag = diagnosis_problem(state, sid, subagent, marked, now)
            if capped:
                ok, note = False, capped
                out["cap"] = capped
            elif pool_why:
                ok, note = False, pool_why
                out["pool"] = pool_why
            elif diag_why:
                ok, note = False, diag_why
                out["diag_problem"] = diag_why
            else:
                for k in marked:
                    state[k]["dispatched"] = True
                    if subagent in execs or diagnose:
                        state[k]["dispatch_pending_at"] = now
                    if round_rec:
                        state[k].setdefault("rounds", []).append(dict(round_rec))
                if model is not None and round_rec:
                    # built before clear_escalation: the log keeps the reason and class this round was bought with
                    out["escalation"] = {"kind": "escalation", "ts": round_rec.get("ts"), "session_id": sid,
                                         "task_id": tgt, "subagent": subagent, "model": round_rec.get("model"),
                                         "effort": round_rec.get("effort"), "escalate_reason": reason,
                                         "class": cls}
                    clear_escalation(state, tgt)
                if diag:
                    out["diagnosis"] = diag
                    apply_diagnosis(state, diag)
                save(sid, state)
    out["ok"], out["note"] = ok, note
    return out


def mark_launched(sid, subagent, now):
    """PostToolUse Agent for an executor: its in_progress tasks get dispatched_at = the launch time
    (dispatch_pending_at when set, else now); dispatch_pending_at is removed; finished_at is removed only
    when it is earlier than the launch (a SubagentStop that came first is kept)."""
    with Lock(sid):
        state = load(sid)
        changed = False
        for _, e in entries(state):
            if e.get("status") == "in_progress" and e.get("owner") == subagent:
                launch = e["dispatch_pending_at"] if is_num(e.get("dispatch_pending_at")) else now
                e["dispatched_at"] = launch
                e.pop("dispatch_pending_at", None)
                if is_num(e.get("finished_at")) and e["finished_at"] < launch:
                    e.pop("finished_at", None)
                changed = True
        if changed:
            save(sid, state)


def finish_agent(sid, agent_type, now):
    """SubagentStop: the in_progress tasks owned by agent_type that have a start and no finish get
    finished_at = now."""
    with Lock(sid):
        state = load(sid)
        changed = False
        for _, e in entries(state):
            if (e.get("owner") == agent_type and e.get("status") == "in_progress"
                    and start_of(e) is not None and not is_finished(e)):
                e["finished_at"] = now
                changed = True
        if changed:
            save(sid, state)


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
            % (done, ", ".join(fmt(state, t) for t in order))) + " " + pool_line(state)


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
            "agent_type": p.get("agent_type") or None,
            "rounds": [{"model": r.get("model"), "effort": r.get("effort")} for r in rounds_of(e)
                       if isinstance(r, dict)]}


def rounds_of(e):
    rounds = e.get("rounds")
    return rounds if isinstance(rounds, list) else []


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
    if tid.startswith("_"):
        return None  # hook bookkeeping (_agents, _diagnoses), never a task
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
    return "; ".join(parts) + ". Dispatch them now in one message, one Agent call each. " + pool_line(state)


AUTOCLAIM_TEXT = ("Auto-claimed task #%s for %s (gate state only; the TaskList UI still shows it pending "
                  "— no TaskUpdate needed until completion).")


def pop_autoclaim(sid, tool_use_id):
    """PostToolUse for a call: clears its pending marker and removes its auto-claim record. Returns the
    record when the claim is still held by the subagent it was made for, else None."""
    if not tool_use_id:
        return None
    with Lock(sid):
        state = load(sid)
        claims = state.get("_autoclaims")
        if not isinstance(claims, dict) or tool_use_id not in claims:
            return None
        rec = claims.pop(tool_use_id)
        if not claims:
            state.pop("_autoclaims", None)
        e = state.get(rec.get("task")) if isinstance(rec, dict) else None
        held = False
        if isinstance(e, dict):
            meta = e.get("metadata") or {}
            if meta.get("auto_claim_pending") == tool_use_id:
                meta.pop("auto_claim_pending", None)
            held = e.get("owner") == rec.get("subagent")
        save(sid, state)
    return rec if isinstance(rec, dict) and held else None


def dispatch_post(sid, p):
    """PostToolUse Agent|Task: the auto-claim note for this call, then the undispatched/ready nudge."""
    claim_rec = pop_autoclaim(sid, (p or {}).get("tool_use_id"))
    parts = []
    if claim_rec:
        parts.append(AUTOCLAIM_TEXT % (claim_rec.get("task"), claim_rec.get("subagent")))
    nudge = dispatch_nudge(sid)
    if nudge:
        parts.append(nudge)
    return " ".join(parts) or None


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
        sub = str(ti.get("subagent_type") or DEFAULT_SUBAGENT)
        if sub in executors() or is_diagnoser(sub):
            mark_launched(sid, sub, time.time())
        return dispatch_post(sid, p)
    return None


def agent_gate(sid, ti, tool_use_id, ts):
    """Main-session Agent|Task decision: None to allow, else (reason, log_line). Order: exempt types; reviewers
    (model/effort rule, then ownership); otherwise the type gate, the model gate, then ownership. A refused
    call marks nothing."""
    subagent = str(ti.get("subagent_type") or DEFAULT_SUBAGENT)
    if subagent in EXEMPT_TYPES:
        return exempt_decision(subagent, ti.get("model"), ts, sid)
    if is_reviewer(subagent):
        if not reviewer_ok(ti):
            model, effort = ti.get("model"), ti.get("effort")
            return (REVIEWER_DENY % (subagent, model, effort),
                    "%s DENY Agent session=%s subagent=%s reviewer-model=%s effort=%s" % (
                        ts, sid[:36], subagent[:100], model, effort))
        res = dispatch_gate(sid, subagent, ti, tool_use_id)
    elif is_diagnoser(subagent):
        if not diagnoser_ok(ti):
            model, effort = ti.get("model"), ti.get("effort")
            return (DIAGNOSER_DENY % (subagent, model, effort),
                    "%s DENY Agent session=%s subagent=%s diagnoser-model=%s effort=%s" % (
                        ts, sid[:36], subagent[:100], model, effort))
        res = dispatch_gate(sid, subagent, ti, tool_use_id, diagnose=True)
        if res["diag_problem"]:
            return (res["diag_problem"], "%s DENY Agent session=%s subagent=%s diagnosis" % (
                ts, sid[:36], subagent[:100]))
        if res["diagnosis"]:
            log_completion(res["diagnosis"])
    else:
        allow_types, _ = load_model_gate()
        if not allowed_type(subagent, allow_types):
            return (TYPE_DENY % subagent[:100],
                    "%s DENY Agent session=%s subagent=%s model-gate=type" % (ts, sid[:36], subagent[:100]))
        bad = allow_types_decision(subagent, ti.get("model"), ts, sid)
        if bad:
            return bad
        raw = ti.get("model")
        omitted = raw in (None, "")
        resolved = "haiku" if omitted else str(raw).lower()
        requested = None if omitted or is_haiku(resolved) else resolved
        rnd = {"model": resolved, "effort": ti.get("effort") or None, "ts": utc_now()}
        res = dispatch_gate(sid, subagent, ti, tool_use_id, requested, rnd)
        if res["model_problem"]:
            return (res["model_problem"],
                    "%s DENY Agent session=%s subagent=%s model-gate=escalation model=%s" % (
                        ts, sid[:36], subagent[:100], resolved[:100]))
        if res["escalation"]:
            log_completion(res["escalation"])
    if res["ok"]:
        return None
    if res["pool"]:
        # the pool refusal stands alone too: no ownership prefix, no blocker suffix
        return (res["pool"], "%s DENY Agent session=%s subagent=%s capacity=%s" % (
            ts, sid[:36], subagent[:100], res["pool"][:200]))
    if res["cap"]:
        # the cap refusal stands alone: no ownership prefix, no blocker suffix
        return (res["cap"], "%s DENY Agent session=%s subagent=%s" % (ts, sid[:36], subagent[:100]))
    return (deny_reason(subagent, res["owned"], res["note"]),
            "%s DENY Agent session=%s subagent=%s" % (ts, sid[:36], subagent[:100]))


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


def ungated_agent_decision(ti, ts, sid):
    """CLAUDE_REQUIRE_TASK=0 path for Agent calls from the main session: no DAG ownership gate, round cap or
    task-based escalation proof. The type gate and the reviewer rule still apply, and only Haiku executors run
    (Sonnet and Opus are denied). None to allow, else (reason, log_line)."""
    subagent = str(ti.get("subagent_type") or DEFAULT_SUBAGENT)
    if subagent in EXEMPT_TYPES:
        return exempt_decision(subagent, ti.get("model"), ts, sid)
    if is_diagnoser(subagent):
        return (DIAGNOSER_UNGATED, "%s DENY Agent session=%s subagent=%s diagnoser-ungated" % (
            ts, sid[:36], subagent[:100]))
    if is_reviewer(subagent):
        if reviewer_ok(ti):
            return None
        model, effort = ti.get("model"), ti.get("effort")
        return (REVIEWER_DENY % (subagent, model, effort),
                "%s DENY Agent session=%s subagent=%s reviewer-model=%s effort=%s" % (
                    ts, sid[:36], subagent[:100], model, effort))
    allow_types, _ = load_model_gate()
    if not allowed_type(subagent, allow_types):
        return (TYPE_DENY % subagent[:100],
                "%s DENY Agent session=%s subagent=%s model-gate=type" % (ts, sid[:36], subagent[:100]))
    bad = allow_types_decision(subagent, ti.get("model"), ts, sid)
    if bad:
        return bad
    raw = ti.get("model")
    if raw in (None, "") or is_haiku(raw):
        return None
    why = OPUS_DENY_TEXT if is_opus(raw) else "without the task gate only Haiku executors run"
    return ("Model %r denied with CLAUDE_REQUIRE_TASK=0: %s." % (raw, why),
            "%s DENY Agent session=%s subagent=%s model-gate=escalation model=%s" % (
                ts, sid[:36], subagent[:100], str(raw)[:100]))


def sendmessage_decision(sid, ti, ts):
    """PreToolUse SendMessage from the main session: None to allow, else (reason, log_line). `to` is denied when it
    maps through the recorded _agents to a diagnoser-* type, or starts with "diagnoser" (any case)."""
    to = ti.get("to")
    if not isinstance(to, str) or not to:
        return None
    ag = load(sid).get("_agents")
    sub = ag.get(to) if isinstance(ag, dict) else None
    if is_diagnoser(sub) or to.lower().startswith("diagnoser"):
        return (DIAGNOSER_SEND, "%s DENY SendMessage session=%s to=%s model-gate=diagnoser" % (
            ts, sid[:36], to[:100]))
    return None


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
        if event == "SubagentStop":
            agent_type = p.get("agent_type")
            if isinstance(agent_type, str) and agent_type:
                finish_agent(sid, agent_type, time.time())
            return
        if event != "PreToolUse" or (tool not in GATED_TOOLS and tool != "SendMessage"):
            return
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        if tool == "SendMessage":
            # a diagnoser is one-shot whatever the kill switches or exempt_env say; subagent messages are not gated
            decision = sendmessage_decision(sid, ti, ts) if main_agent else None
            if decision:
                emit_deny(decision[0], decision[1])
            return
        if tool in DISPATCH_TOOLS and not main_agent:
            # exempt nothing: neither a kill switch nor an exempt_env session lets a subagent dispatch
            sub = str(ti.get("subagent_type") or DEFAULT_SUBAGENT)
            emit_deny(SUBAGENT_DENY, "%s DENY Agent session=%s subagent=%s subagent-dispatch" % (
                ts, sid[:36], sub[:100]))
            return
        if env_exempt():
            return  # session matched by exempt_env in the local harness config: exempt (local models, no gate)
        if not main_agent:
            return  # subagent TaskUpdate: unrestricted
        # CLAUDE_MAIN_WRITE lifts only the DAG gate (like the kill switch); the type and model gates still run
        main_write = os.environ.get("CLAUDE_MAIN_WRITE") == "1"
        killed = os.environ.get("CLAUDE_REQUIRE_TASK") == "0"
        if tool == "TaskUpdate":
            if killed or main_write:
                return  # kill switch or CLAUDE_MAIN_WRITE: the DAG ownership gate is off
            reason = check_task_update(sid, ti)
            if reason is None:
                return
            log_line = "%s DENY TaskUpdate session=%s reason=%s" % (ts, sid[:36], reason[:200])
        else:
            decision = ungated_agent_decision(ti, ts, sid) if (killed or main_write) else agent_gate(
                sid, ti, p.get("tool_use_id"), ts)
            if decision is None:
                return
            reason, log_line = decision
    except Exception:
        return  # fail open
    emit_deny(reason, log_line)


if __name__ == "__main__":
    main()
