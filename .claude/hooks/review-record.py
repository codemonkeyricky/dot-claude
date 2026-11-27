#!/usr/bin/env python3
"""Review record: remember the PR review verdict an Opus reviewer subagent prints.

SubagentStop hook. Only a subagent whose agent_type is "opus-reviewer" or starts with
"pr-reviewer-" is acted on; every other payload is ignored.

The reviewer's final text is payload["last_assistant_message"] when that is a non-empty
string. Otherwise it is read from the JSONL at payload["agent_transcript_path"]: the last
entry with type "assistant" whose message.content has at least one {"type": "text"} block,
its text blocks joined with "\\n". A plain string message.content counts as its text.

The LAST line of that text (re.MULTILINE) matching either of these decides, whichever
kind appears last:
  ^REVIEW-VERDICT:\\s+(APPROVE|CHANGES)\\s+([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\\d+)@([0-9a-fA-F]{40})\\s*$
  ^REVIEW-VERDICT:\\s+ABSTAIN\\b

  APPROVE / CHANGES  one JSON line is appended to the PR reviews log:
                     {"ts": "<UTC %Y-%m-%dT%H:%M:%SZ>", "repo": "<owner/repo, lowercased>",
                      "pr": <int>, "sha": "<40 hex, lowercased>", "verdict": "APPROVE"|"CHANGES",
                      "agent_type": ..., "agent_id": ..., "session_id": ...}
                     Nothing is written to stdout.
  ABSTAIN            nothing is recorded and nothing is written to stdout.
  no verdict line    unless stop_hook_active is True, one JSON object is printed:
                     {"decision": "block", "reason": "..."}
                     so the reviewer ends its report with a verdict line. With
                     stop_hook_active True nothing is printed (no loop).

Diagnostics: every run appends one JSON line to the hook log: ts, hook_event_name,
agent_type, keys (sorted top-level key names of the payload, never values) and outcome:
  ignored    not a reviewer
  recorded   APPROVE or CHANGES written to the PR reviews log
  blocked    block decision printed
  abstain    ABSTAIN verdict, nothing recorded
  none       nothing done: no verdict with stop_hook_active, or a failure

Fails open: unparseable stdin or any exception gives exit 0 and no stdout. The exit code
is always 0.

Env overrides (tests): REVIEW_RECORD_LOG (hook log, default
~/.claude/hooks/review-record.log) and PR_REVIEWS_LOG (PR reviews log, default
~/.claude/hooks/state/pr-reviews.jsonl).
"""
import json, os, re, sys, time

HOME = os.path.expanduser("~")
REVIEWER_TYPES = ("opus-reviewer",)
REVIEWER_PREFIX = "pr-reviewer-"
LOG = (os.environ.get("REVIEW_RECORD_LOG")
       or os.path.join(HOME, ".claude", "hooks", "review-record.log"))
PR_REVIEWS = (os.environ.get("PR_REVIEWS_LOG")
              or os.path.join(HOME, ".claude", "hooks", "state", "pr-reviews.jsonl"))
VERDICT_RE = re.compile(
    r"^REVIEW-VERDICT:\s+(APPROVE|CHANGES)\s+([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)@([0-9a-fA-F]{40})\s*$",
    re.MULTILINE)
ABSTAIN_RE = re.compile(r"^REVIEW-VERDICT:\s+ABSTAIN\b", re.MULTILINE)
BLOCK_REASON = ("End your report with the verdict line as its last line: REVIEW-VERDICT: "
                "APPROVE|CHANGES owner/repo#N@<40-char head sha>, or REVIEW-VERDICT: ABSTAIN "
                "<reason> if you could not review.")


def now_utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def short(v):
    return None if v is None else str(v)[:100]


def is_reviewer(agent_type):
    return agent_type in REVIEWER_TYPES or agent_type.startswith(REVIEWER_PREFIX)


def text_of_entry(entry):
    """The text of an assistant transcript entry, or None when it has no text."""
    if not isinstance(entry, dict) or entry.get("type") != "assistant":
        return None
    msg = entry.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return content or None
    if isinstance(content, list):
        texts = [b["text"] for b in content
                 if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)]
        if texts:
            return "\n".join(texts)
    return None


def final_text(p):
    """last_assistant_message, else the last assistant text in the transcript, else ''."""
    lam = p.get("last_assistant_message")
    if isinstance(lam, str) and lam:
        return lam
    path = p.get("agent_transcript_path")
    if not isinstance(path, str) or not path:
        return ""
    last = ""
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            text = text_of_entry(entry)
            if text is not None:
                last = text
    return last


def find_verdict(text):
    """(kind, match) for the last verdict line: kind is APPROVE, CHANGES or ABSTAIN. None if none."""
    best = None
    for m in VERDICT_RE.finditer(text):
        best = (m.start(), m.group(1), m)
    for m in ABSTAIN_RE.finditer(text):
        if best is None or m.start() > best[0]:
            best = (m.start(), "ABSTAIN", m)
    if best is None:
        return None
    return best[1], best[2]


def record_verdict(p, verdict, m, agent_type):
    rec = {"ts": now_utc(), "repo": m.group(2).lower(), "pr": int(m.group(3)),
           "sha": m.group(4).lower(), "verdict": verdict, "agent_type": agent_type,
           "agent_id": p.get("agent_id"), "session_id": p.get("session_id")}
    os.makedirs(os.path.dirname(os.path.abspath(PR_REVIEWS)), exist_ok=True)
    with open(PR_REVIEWS, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def handle(p):
    """(outcome, stdout object or None) for one payload."""
    agent_type = p.get("agent_type")
    agent_type = agent_type if isinstance(agent_type, str) else ""
    if not is_reviewer(agent_type):
        return "ignored", None
    found = find_verdict(final_text(p))
    if found is None:
        if p.get("stop_hook_active") is True:
            return "none", None
        return "blocked", {"decision": "block", "reason": BLOCK_REASON}
    kind, m = found
    if kind == "ABSTAIN":
        return "abstain", None
    record_verdict(p, kind, m, agent_type)
    return "recorded", None


def log_run(p, outcome):
    """Append one diagnostic line. Never raises."""
    try:
        d = p if isinstance(p, dict) else {}
        rec = {"ts": now_utc(), "hook_event_name": short(d.get("hook_event_name")),
               "agent_type": short(d.get("agent_type")), "keys": sorted(d), "outcome": outcome}
        os.makedirs(os.path.dirname(os.path.abspath(LOG)), exist_ok=True)
        with open(LOG, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def read_payload():
    try:
        p = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
    except Exception:
        return None
    return p if isinstance(p, dict) else None


def main():
    p = read_payload()
    outcome, out = "none", None
    if p is not None:
        try:
            outcome, out = handle(p)
        except Exception:
            outcome, out = "none", None
    log_run(p, outcome)
    if out is not None:
        print(json.dumps(out))


if __name__ == "__main__":
    main()
