#!/usr/bin/env python3
"""PreToolUse hook on Agent: give each dispatched subagent a team-member name.

Executors (subagent_type in EXECUTORS, defined in ~/.claude/agents/<slug>.md)
always get their fixed roster name; their dispatch does not advance the
round-robin counter. Every other dispatch gets a name from POOL (NAMES[10:],
i.e. never a roster name), handed out round-robin per session (start offset
derived from the session id, counter kept under a file lock so parallel
dispatches get distinct names).

Rewrites the Agent input via updatedInput:
  description -> "<Name> · <description>"   (what the UI shows)
  prompt      -> one-line intro + original prompt (subagent signs its report)
  effort      -> "high" for a reviewer / "medium" for an executor, when the dispatch gives no
                 effort (absent, null or empty), and only while that pool actually runs on the
                 local model (local_active). An explicit effort is left as-is; so is the input
                 off-local or on a pool not routed local.
Skips inputs already carrying a name. Fails open. Disable naming: CLAUDE_AGENT_NAMES=0.
Disable the local-effort override: CLAUDE_LOCAL_EFFORT=0.
"""
import fcntl, hashlib, json, os, sys

HOME = os.path.expanduser("~")
AGENT_TOOLS = {"Agent", "Task"}
NAMES = [
    "Priya Sharma", "Arjun Iyer", "Ananya Reddy", "Rohan Mehta", "Kavya Nair",
    "Vikram Singh", "Sneha Patil", "Aditya Rao", "Meera Krishnan", "Rahul Gupta",
    "Divya Menon", "Karthik Subramanian", "Pooja Desai", "Siddharth Joshi",
    "Lakshmi Venkatesan", "Amit Banerjee", "Neha Kulkarni", "Suresh Pillai",
    "Ishita Chatterjee", "Manish Agarwal", "Deepika Hegde", "Harpreet Kaur",
    "Gaurav Saxena", "Anjali Bhatt", "Nikhil Shetty", "Shreya Ghosh",
    "Farhan Qureshi", "Joseph Mathew", "Tanvi Deshpande", "Varun Malhotra",
]
# Fixed names for the executor pool (~/.claude/agents/<slug>.md), keyed by subagent_type.
EXECUTORS = {
    "qorvax-tel": "Qorvax Tel", "zyrene-vosk": "Zyrene Vosk", "thal-ummir": "Thal Ummir",
    "kreeva-osh": "Kreeva Osh", "vexlor-ain": "Vexlor Ain", "nyxa-prell": "Nyxa Prell",
    "oomrak-zhee": "Oomrak Zhee", "xilth-varo": "Xilth Varo", "juun-ketrax": "Juun Ketrax",
    "plimm-oraxi": "Plimm Oraxi",
}
# Fixed names for the PR reviewer pool (~/.claude/agents/pr-reviewer-<n>.md), keyed by subagent_type.
REVIEWERS = {
    "pr-reviewer-1": "PR Reviewer 1", "pr-reviewer-2": "PR Reviewer 2", "pr-reviewer-3": "PR Reviewer 3",
}
# Fixed names for the diagnoser pool (diagnoser-*), keyed by subagent_type.
DIAGNOSERS = {"diagnoser-1": "Diagnoser 1"}
# General dispatches draw only from names not reserved for executors, reviewers or diagnosers.
POOL = [n for n in NAMES if n not in EXECUTORS.values() and n not in REVIEWERS.values()
        and n not in DIAGNOSERS.values()]
SEP = " · "


def state_dir():
    return os.environ.get("CLAUDE_AGENT_NAMES_STATE") or os.path.join(HOME, ".claude", "hooks", "state", "agent-names")


def next_name(session_id):
    sid = session_id if session_id and "/" not in session_id and not session_id.startswith(".") else "nosession"
    d = state_dir()
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, sid + ".count"), "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        raw = f.read().strip()
        n = int(raw) if raw.isdigit() else 0
        f.seek(0); f.truncate(); f.write(str(n + 1)); f.flush()
    offset = int(hashlib.sha1(sid.encode()).hexdigest(), 16) % len(POOL)
    return POOL[(offset + n) % len(POOL)]


def local_active():
    """(reviewer_local, executor_local): whether each pool runs on the local model.

    Mirrors local-mode.py: CLAUDE_LOCAL=1 (claude-local) and LOCAL_DISPATCH=all (claude-local-all)
    route every request, main and subagents, to the local model, so both pools are local.
    LOCAL_DISPATCH=1 or subagents routes only the Haiku executors local; the Opus reviewer still
    goes to Anthropic, so only the executor flag is set. Off-local is (False, False).
    """
    if os.environ.get("CLAUDE_LOCAL", "").strip() == "1":
        return True, True
    mode = os.environ.get("LOCAL_DISPATCH", "").strip().lower()
    if mode in ("1", "subagents"):
        return False, True
    if mode == "all":
        return True, True
    return False, False


def main():
    if os.environ.get("CLAUDE_AGENT_NAMES") == "0":
        return
    try:
        p = json.load(sys.stdin)
        tool = p.get("tool_name", "")
        ti = p.get("tool_input")
    except Exception:
        return
    if tool not in AGENT_TOOLS or not isinstance(ti, dict):
        return
    desc = str(ti.get("description") or "")
    if any(desc.startswith(n + SEP) for n in NAMES):
        return  # already named
    sub = ti.get("subagent_type")
    try:
        if isinstance(sub, str) and sub in EXECUTORS:
            name = EXECUTORS[sub]  # executor: fixed name, counter untouched
        elif isinstance(sub, str) and sub in REVIEWERS:
            name = REVIEWERS[sub]  # reviewer: fixed name, counter untouched
        elif isinstance(sub, str) and sub in DIAGNOSERS:
            name = DIAGNOSERS[sub]  # diagnoser: fixed name, counter untouched
        else:
            name = next_name(p.get("session_id"))
    except Exception:
        return
    # Executors sign with their first name ("Qorvax here:"); reviewers and diagnosers with the
    # full label ("PR Reviewer 1 here:", "Diagnoser 1 here:"), since "PR" or "Diagnoser" alone is not a name.
    sign = name if isinstance(sub, str) and (sub in REVIEWERS or sub in DIAGNOSERS) else name.split()[0]
    new = dict(ti)
    new["description"] = name + SEP + desc if desc else name
    new["prompt"] = ("You are %s, the engineer assigned to this task. Start your final report with \"%s here:\".\n\n"
                     % (name, sign)) + str(ti.get("prompt") or "")
    if os.environ.get("CLAUDE_LOCAL_EFFORT") != "0" and isinstance(sub, str) and not ti.get("effort"):
        reviewer_local, executor_local = local_active()
        if sub in REVIEWERS and reviewer_local:
            new["effort"] = "high"
        elif sub in EXECUTORS and executor_local:
            new["effort"] = "medium"
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "permissionDecisionReason": "assigned to " + name,
        "updatedInput": new}}))


if __name__ == "__main__":
    main()
