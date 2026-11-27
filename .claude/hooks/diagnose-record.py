#!/usr/bin/env python3
"""Diagnose record: enforce the output format of a diagnoser subagent and log its spec.

SubagentStop hook. Only a subagent whose agent_type starts with "diagnoser-" is acted on;
every other payload is ignored (exit 0, no output).

The diagnoser's final text is payload["last_assistant_message"] when that is a non-empty
string. Otherwise it is read from the JSONL at payload["agent_transcript_path"]: the last
entry with type "assistant" whose message.content has at least one {"type": "text"} block,
its text blocks joined with "\\n". A plain string message.content counts as its text.

The text must satisfy every rule:
  headings   each of "## Root cause", "## Fix spec", "## Checks" is on a line of its own
  verdict    the last non-empty line is exactly DIAGNOSIS-VERDICT: SPEC or
             DIAGNOSIS-VERDICT: NEEDS-USER
  length     at most 8000 characters
  code       no fenced code block (``` ... ```) longer than 20 lines

  all rules pass   one JSON line is appended to the diagnoses log:
                   {"kind": "diagnosis-spec", "ts": "<UTC %Y-%m-%dT%H:%M:%SZ>",
                    "session_id": ..., "agent_id": ..., "agent_type": ...,
                    "verdict": "SPEC"|"NEEDS-USER", "chars": <int>}
                   Nothing is written to stdout.
  any rule fails   unless stop_hook_active is True, one JSON object is printed:
                   {"decision": "block", "reason": "..."}
                   whose reason lists every failed rule and restates the required format,
                   so the diagnoser rewrites its report. With stop_hook_active True
                   nothing is printed: the stop is allowed (no loop).

Diagnostics: every run appends one JSON line to the hook log: ts, hook_event_name,
agent_type, keys (sorted top-level key names of the payload, never values) and outcome:
  ignored    not a diagnoser
  recorded   every rule passed; the spec line was written to the diagnoses log
  blocked    block decision printed
  allowed    a rule failed but stop_hook_active is True, so the stop is let through
             (the failed rule names are logged under "failed")
  none       nothing done: a failure with stop_hook_active, or an error

Fails open: unparseable stdin or any exception gives exit 0 and no stdout. The exit code
is always 0.

Env overrides (tests): DIAGNOSE_RECORD_LOG (hook log, default
~/.claude/hooks/diagnose-record.log) and DIAGNOSES_LOG (diagnoses log, default
~/.claude/hooks/state/diagnoses.jsonl).
"""
import json, os, sys, time

HOME = os.path.expanduser("~")
DIAGNOSER_PREFIX = "diagnoser-"
LOG = (os.environ.get("DIAGNOSE_RECORD_LOG")
       or os.path.join(HOME, ".claude", "hooks", "diagnose-record.log"))
DIAGNOSES = (os.environ.get("DIAGNOSES_LOG")
             or os.path.join(HOME, ".claude", "hooks", "state", "diagnoses.jsonl"))
HEADINGS = ("## Root cause", "## Fix spec", "## Checks")
VERDICTS = ("DIAGNOSIS-VERDICT: SPEC", "DIAGNOSIS-VERDICT: NEEDS-USER")
MAX_CHARS = 8000
MAX_CODE_LINES = 20
FORMAT_TEXT = ("Required format: the headings '## Root cause', '## Fix spec' and '## Checks', each "
               "on its own line; total length at most 8000 characters; no fenced code block longer "
               "than 20 lines; and the last line exactly 'DIAGNOSIS-VERDICT: SPEC' or "
               "'DIAGNOSIS-VERDICT: NEEDS-USER'.")


def now_utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def short(v):
    return None if v is None else str(v)[:100]


def is_diagnoser(agent_type):
    return agent_type.startswith(DIAGNOSER_PREFIX)


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


def longest_code_block(lines):
    """Length in lines of the longest fenced code block (body lines only; unclosed counts to the end)."""
    longest = 0
    in_fence = False
    count = 0
    for line in lines:
        s = line.strip()
        if not in_fence:
            if s.startswith("```"):
                in_fence, count = True, 0
        elif s == "```":
            longest = max(longest, count)
            in_fence = False
        else:
            count += 1
    if in_fence:
        longest = max(longest, count)
    return longest


def check_rules(text):
    """List of failed rule descriptions. Empty when every rule passes."""
    failed = []
    lines = text.splitlines()
    stripped = [ln.strip() for ln in lines]
    for heading in HEADINGS:
        if heading not in stripped:
            failed.append("missing heading line '%s'" % heading)
    nonempty = [ln for ln in stripped if ln]
    if not nonempty or nonempty[-1] not in VERDICTS:
        last = nonempty[-1] if nonempty else ""
        failed.append("last non-empty line must be exactly 'DIAGNOSIS-VERDICT: SPEC' or "
                      "'DIAGNOSIS-VERDICT: NEEDS-USER' (got '%s')" % last[:100])
    if len(text) > MAX_CHARS:
        failed.append("text is %d characters, over the %d limit" % (len(text), MAX_CHARS))
    longest = longest_code_block(lines)
    if longest > MAX_CODE_LINES:
        failed.append("a fenced code block is %d lines, over the %d-line limit"
                      % (longest, MAX_CODE_LINES))
    return failed


def verdict_of(text):
    nonempty = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return nonempty[-1].split(":", 1)[1].strip()


def record_spec(p, verdict, chars):
    rec = {"kind": "diagnosis-spec", "ts": now_utc(), "session_id": p.get("session_id"),
           "agent_id": p.get("agent_id"), "agent_type": p.get("agent_type"),
           "verdict": verdict, "chars": chars}
    os.makedirs(os.path.dirname(os.path.abspath(DIAGNOSES)), exist_ok=True)
    with open(DIAGNOSES, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def block_reason(failed):
    return ("Your diagnosis report breaks the format. Failed rules: " + "; ".join(failed)
            + ". " + FORMAT_TEXT)


def handle(p):
    """(outcome, extra, stdout object or None) for one payload."""
    agent_type = p.get("agent_type")
    agent_type = agent_type if isinstance(agent_type, str) else ""
    if not is_diagnoser(agent_type):
        return "ignored", None, None
    text = final_text(p)
    failed = check_rules(text)
    if failed:
        if p.get("stop_hook_active") is True:
            return "allowed", failed, None
        return "blocked", failed, {"decision": "block", "reason": block_reason(failed)}
    record_spec(p, verdict_of(text), len(text))
    return "recorded", None, None


def log_run(p, outcome, extra=None):
    """Append one diagnostic line. Never raises."""
    try:
        d = p if isinstance(p, dict) else {}
        rec = {"ts": now_utc(), "hook_event_name": short(d.get("hook_event_name")),
               "agent_type": short(d.get("agent_type")), "keys": sorted(d), "outcome": outcome}
        if extra:
            rec["failed"] = extra
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
    outcome, extra, out = "none", None, None
    if p is not None:
        try:
            outcome, extra, out = handle(p)
        except Exception:
            outcome, extra, out = "none", None, None
    log_run(p, outcome, extra)
    if out is not None:
        print(json.dumps(out))


if __name__ == "__main__":
    main()
