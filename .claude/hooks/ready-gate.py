#!/usr/bin/env python3
r"""Ready gate: a GitHub PR leaves draft only after an Opus review approved its head commit.

PreToolUse hook for Write, Edit, MultiEdit, NotebookEdit, Bash and PowerShell. It applies
to the main session AND to subagents (a payload with agent_id is gated too).

Rules (first denial wins):
  A  Records file protected. Write/Edit/MultiEdit (tool_input.file_path) or NotebookEdit
     (notebook_path) whose basename, lowercased, is pr-reviews.jsonl is denied. A Bash or
     PowerShell command containing "pr-reviews" (case-insensitive) is denied; the Read tool
     is the way to read the file.
  B  A Bash/PowerShell command is split into segments on newlines, ";", "&&", "||" and "|"
     (a rough split). For each segment, leading whitespace and wrappers (env assignments
     NAME=value, env, command, exec, time, sudo, &, !, (, {) are stripped; B1 and B2 fire
     only if the segment then starts with gh (gh, gh.exe, or a path ending in /gh or
     \gh, optionally quoted), then `pr`, then ready or create. Text that merely contains
     "gh pr ready" (echo, grep, a commit message, a heredoc body) is not gated:
     B1`gh pr ready` without --undo: the PR is resolved with `gh pr view` (ref = first
         non-flag token, repo from -R/--repo/--repo=). The LAST record in the records file
         for that repo, pr and head sha must have verdict APPROVE. Otherwise the call is
         denied with the instruction to dispatch a pr-reviewer-<n> subagent. The denial
         counts n = CHANGES records for that repo and PR (any sha; malformed lines skipped).
         While n < REVIEW_ROUND_CAP (3) it adds "This would be review round n+1 of 3."; at
         n >= 3 the reason says the review/fix loop is capped and to ask the user instead.
         A failed resolve is denied ("could not resolve the PR ...").
     B2  `gh pr create` without --draft or -d is denied.
     B3  markPullRequestReadyForReview anywhere in the command (for example through
         gh api graphql) is denied: use gh pr ready so the gate can check the PR.
  C  Fail closed for ready commands: an unexpected exception while evaluating a B1 segment
     denies with the exception text. Exceptions anywhere else allow.

Exemptions (the whole hook allows): env CLAUDE_SKIP_READY_REVIEW == "1". Unparseable
stdin allows.

Review record contract (written by a different hook): JSONL at env PR_REVIEWS_LOG (default
~/.claude/hooks/state/pr-reviews.jsonl), one object per line:
  {"ts", "repo" (lowercase "owner/repo"), "pr" (int), "sha" (lowercase 40-hex),
   "verdict" ("APPROVE" | "CHANGES"), "agent_type", "agent_id", "session_id"}
Malformed lines (not an object, or repo/pr/sha/verdict of the wrong type) are skipped.

Deny output: stdout {"hookSpecificOutput": {"hookEventName": "PreToolUse",
"permissionDecision": "deny", "permissionDecisionReason": <text>}} with exit 0. Each denial
is one line in env READY_GATE_LOG (default ~/.claude/hooks/ready-gate.log).

Environment:
  PR_REVIEWS_LOG       records file (tests)
  READY_GATE_LOG       denial log (tests)
  READY_GATE_GH_CMD    JSON list replacing ["gh"], e.g. ["python", "fake_gh.py"] (tests)
  CLAUDE_SKIP_READY_REVIEW   exemption above
"""
import json, os, re, shlex, subprocess, sys, time

HOME = os.path.expanduser("~")
RECORDS = (os.environ.get("PR_REVIEWS_LOG")
           or os.path.join(HOME, ".claude", "hooks", "state", "pr-reviews.jsonl"))
LOG = (os.environ.get("READY_GATE_LOG")
       or os.path.join(HOME, ".claude", "hooks", "ready-gate.log"))
RECORDS_NAME = "pr-reviews.jsonl"
SHELL_TOOLS = {"Bash", "PowerShell"}
WRITE_TOOLS = {"Write": "file_path", "Edit": "file_path",
               "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
# gh as the program of a segment: bare, .exe, or a quoted/unquoted path ending in /gh or \gh.
GH_PROG = r"""(?:"(?:[^"]*[\\/])?gh(?:\.exe)?"|'(?:[^']*[\\/])?gh(?:\.exe)?'|(?:[^\s"']*[\\/])?gh(?:\.exe)?)"""
READY_RE = re.compile(GH_PROG + r"\s+pr\s+ready\b", re.I)
CREATE_RE = re.compile(GH_PROG + r"\s+pr\s+create\b", re.I)
# One leading wrapper: an env assignment, env/command/exec/time/sudo, the PowerShell call
# operator &, or ! ( {. Applied repeatedly by strip_wrappers.
WRAP_RE = re.compile(r"(?:[A-Za-z_]\w*=\S*|env|command|exec|time|sudo|&)\s+|[!({]\s*", re.I)
MARK_RE = re.compile(r"markPullRequestReadyForReview", re.I)
SPLIT_RE = re.compile(r"\r?\n|;|&&|\|\|?")
URL_RE = re.compile(r"/([^/]+)/([^/]+)/pull/(\d+)")
SHA_RE = re.compile(r"[0-9a-f]{40}")
GH_TIMEOUT = 15
REVIEW_ROUND_CAP = 3

RECORDS_REASON = ("pr-reviews.jsonl is written only by the review-record hook. "
                  "To read it, use the Read tool, not the shell.")
CREATE_REASON = ("Create PRs as drafts (gh pr create --draft); a PR leaves draft only through "
                 "gh pr ready after an Opus review.")
MARK_REASON = "Use gh pr ready <n> so the review gate can check the PR."


class ResolveError(Exception):
    """The PR could not be resolved; the message is the deny reason."""


def gh_cmd():
    raw = os.environ.get("READY_GATE_GH_CMD")
    return json.loads(raw) if raw else ["gh"]


def tokenize(text):
    try:
        return shlex.split(text, posix=True)
    except ValueError:
        return text.split()


def squash(text):
    return " ".join(str(text).split())[:200]


def fail_text(detail):
    return "could not resolve the PR to check its review: " + squash(detail)


def resolve(ref, repo, cwd):
    """(owner/repo lowercase, pr int, sha lowercase, url) from gh pr view. Raises ResolveError."""
    cmd = gh_cmd() + ["pr", "view"]
    if ref:
        cmd.append(ref)
    if repo:
        cmd += ["-R", repo]
    cmd += ["--json", "number,headRefOid,url"]
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=GH_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise ResolveError(fail_text("gh pr view timed out after %d s" % GH_TIMEOUT))
    except OSError as e:
        raise ResolveError(fail_text(e))
    if r.returncode != 0:
        raise ResolveError(fail_text(r.stderr or r.stdout or "gh exited %d" % r.returncode))
    try:
        data = json.loads(r.stdout)
        m = URL_RE.search(data["url"])
        if not m:
            raise ValueError("no /owner/repo/pull/<n> in the url")
        sha = str(data["headRefOid"]).lower()
        if not SHA_RE.fullmatch(sha):
            raise ValueError("headRefOid is not a 40-hex sha")
        pr = int(data["number"])
    except (ValueError, KeyError, TypeError) as e:
        raise ResolveError(fail_text(e))
    repo_name = ("%s/%s" % (m.group(1), m.group(2))).lower()
    return repo_name, pr, sha, data["url"]


def valid_record(o):
    pr = o.get("pr")
    return (isinstance(o.get("repo"), str) and isinstance(pr, int) and not isinstance(pr, bool)
            and isinstance(o.get("sha"), str) and isinstance(o.get("verdict"), str))


def latest_verdict(repo, pr, sha):
    """Verdict of the last valid record for repo/pr/sha in file order, or None."""
    try:
        f = open(RECORDS, encoding="utf-8", errors="replace")
    except OSError:
        return None
    latest = None
    with f:
        for line in f:
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if not isinstance(o, dict) or not valid_record(o):
                continue
            if o["repo"].lower() == repo and o["pr"] == pr and o["sha"].lower() == sha:
                latest = o["verdict"]
    return latest


def changes_count(repo, pr):
    """Number of valid CHANGES records for repo/pr, any sha."""
    try:
        f = open(RECORDS, encoding="utf-8", errors="replace")
    except OSError:
        return 0
    n = 0
    with f:
        for line in f:
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if not isinstance(o, dict) or not valid_record(o):
                continue
            if o["repo"].lower() == repo and o["pr"] == pr and o["verdict"] == "CHANGES":
                n += 1
    return n


def ready_reason(rest, cwd):
    """Reason to deny `gh pr ready` whose arguments are `rest`, or None to allow."""
    tokens = tokenize(rest)
    if "--undo" in tokens:
        return None
    repo = ref = None
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in ("-R", "--repo") and i + 1 < len(tokens):
            repo = tokens[i + 1]
            i += 2
            continue
        if t.startswith("--repo="):
            repo = t.split("=", 1)[1]
        elif not t.startswith("-") and ref is None:
            ref = t
        i += 1
    name, pr, sha, url = resolve(ref, repo, cwd)
    verdict = latest_verdict(name, pr, sha)
    if verdict == "APPROVE":
        return None
    pr_name = "%s#%d" % (name, pr)
    n = changes_count(name, pr)
    if n >= REVIEW_ROUND_CAP:
        return ('PR %s has had %d CHANGES verdicts; the review/fix loop is capped at %d rounds. '
                'Do not dispatch another fix or review: stop and ask the user how to proceed '
                '(merge as is, take over by hand, or allow more rounds). If you are a subagent, '
                'stop and report this to the main session.' % (pr_name, n, REVIEW_ROUND_CAP))
    reason = ('PR %s head %s has no Opus APPROVE for this commit (latest verdict: %s). Before marking '
              'it ready: TaskCreate "Opus review %s", set it in_progress with owner pr-reviewer-<1-3>, '
              'and dispatch Agent(subagent_type="pr-reviewer-<n>", model omitted or "opus", '
              'prompt="Review %s at %s; checkout %s"). A push after the review needs a new review. '
              'If you are a subagent, stop and report this to the main session.'
              % (pr_name, sha[:12], verdict or "none", pr_name, url, sha, cwd or os.getcwd()))
    return reason + (" This would be review round %d of %d." % (n + 1, REVIEW_ROUND_CAP))


def strip_wrappers(seg):
    """The segment with leading whitespace and leading wrappers removed."""
    s = seg.lstrip()
    while True:
        m = WRAP_RE.match(s)
        if not m:
            return s
        s = s[m.end():].lstrip()


def segment_reason(seg, cwd):
    s = strip_wrappers(seg)
    m = READY_RE.match(s)
    if m:
        try:
            return ready_reason(s[m.end():], cwd)
        except ResolveError as e:
            return str(e)
        except Exception as e:  # rule C: fail closed
            return "ready-gate error: %s: %s" % (type(e).__name__, e)
    if CREATE_RE.match(s) and not {"--draft", "-d"} & set(tokenize(s)):
        return CREATE_REASON
    return None


def decide(tool, ti, cwd):
    """Reason string to deny the call, or None to allow."""
    if tool in WRITE_TOOLS:
        path = ti.get(WRITE_TOOLS[tool])
        if isinstance(path, str) and re.split(r"[\\/]", path)[-1].lower() == RECORDS_NAME:
            return RECORDS_REASON
        return None
    if tool not in SHELL_TOOLS:
        return None
    cmd = ti.get("command") if isinstance(ti.get("command"), str) else ""
    if "pr-reviews" in cmd.lower():
        return RECORDS_REASON
    for seg in SPLIT_RE.split(cmd):
        reason = segment_reason(seg, cwd)
        if reason:
            return reason
    if MARK_RE.search(cmd):
        return MARK_REASON
    return None


def log_denial(tool, sid, reason):
    try:
        line = "%s DENY %s session=%s reason=%s" % (
            time.strftime("%Y-%m-%d %H:%M:%S"), tool, sid[:36], squash(reason)[:200])
        with open(LOG, "a", encoding="utf-8", errors="replace") as f:
            f.write(line + "\n")
    except Exception:
        pass


def main():
    if os.environ.get("CLAUDE_SKIP_READY_REVIEW") == "1":
        return
    try:
        p = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
        if not isinstance(p, dict):
            return
        tool = p.get("tool_name", "")
        ti = p.get("tool_input") if isinstance(p.get("tool_input"), dict) else {}
        cwd = p.get("cwd") if isinstance(p.get("cwd"), str) else None
        if cwd and not os.path.isdir(cwd):
            cwd = None
        sid = str(p.get("session_id") or "")
        reason = decide(tool, ti, cwd)
    except Exception:
        return  # fail open
    if reason is None:
        return
    log_denial(tool, sid, reason)
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason}}))


if __name__ == "__main__":
    main()
