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
Skips inputs already carrying a name. Fails open. Disable: CLAUDE_AGENT_NAMES=0.
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
# General dispatches draw only from names not reserved for executors or reviewers.
POOL = [n for n in NAMES if n not in EXECUTORS.values() and n not in REVIEWERS.values()]
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
        else:
            name = next_name(p.get("session_id"))
    except Exception:
        return
    # Executors sign with their first name ("Qorvax here:"); reviewers with the full
    # label ("PR Reviewer 1 here:"), since "PR" alone is not a name.
    sign = name if isinstance(sub, str) and sub in REVIEWERS else name.split()[0]
    new = dict(ti)
    new["description"] = name + SEP + desc if desc else name
    new["prompt"] = ("You are %s, the engineer assigned to this task. Start your final report with \"%s here:\".\n\n"
                     % (name, sign)) + str(ti.get("prompt") or "")
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "permissionDecisionReason": "assigned to " + name,
        "updatedInput": new}}))


if __name__ == "__main__":
    main()
