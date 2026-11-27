"""Session repo tracker: records which git repos a Claude Code session touches and
prefixes the session title with the dominant repo, e.g. "[my-app] feature-branch".

Events: PostToolUse, CwdChanged, UserPromptSubmit, SessionStart. PostToolUse counts only
the paths the tool names (post_tool_paths), so the launch cwd is not counted per tool
call; CwdChanged, UserPromptSubmit and SessionStart count the cwd they carry.

State per session: <STATE_DIR>/<safe_sid>.json (other scripts read it). STATE_DIR = env
SESSION_REPO_STATE_DIR, else ~/.claude/hooks/state/session-repo. Fields are only ever
added, never renamed or removed.

Only stdout output is a hookSpecificOutput sessionTitle JSON object. Nothing else is
printed, because stdout is injected into the model context. Fails open: any error is
swallowed and the script always exits 0.

Env: SESSION_REPO_TITLE=0 disables titling. SESSION_REPO_JOBS_DIR overrides the
background-job directory (default ~/.claude/jobs). Debug log goes to
<STATE_DIR>/../session-repo-debug.jsonl while it is under 100 KB.

Job names: the background-job daemon renames a job and stores the name as "name" in
<JOBS_DIR>/<first 8 chars of session_id>/state.json. When that name is set, it is the
base of the title and the "[repo] " prefix is put back on it, because the daemon's
rename drops the prefix. Without a job name the title rules are unchanged.
"""

import datetime
import fcntl
import json
import os
import re
import sys

DEFAULT_STATE_DIR = os.path.expanduser("~/.claude/hooks/state/session-repo")
DEFAULT_JOBS_DIR = os.path.expanduser("~/.claude/jobs")
DEBUG_MAX_BYTES = 100 * 1024
MAX_CANDIDATES = 30
TITLE_BASE_CHARS = 50
PROMPT_CHARS = 200
TRANSCRIPT_TAIL_BYTES = 4 * 1024 * 1024
TRANSCRIPT_FULL_MAX_BYTES = 20 * 1024 * 1024
TITLE_HISTORY_MAX = 10
EXCLUDED_BRANCHES = ("main", "master", "trunk")
HANDLED = ("PostToolUse", "CwdChanged", "UserPromptSubmit", "SessionStart")
TITLED = ("UserPromptSubmit", "SessionStart")

PATH_FIELDS = ("file_path", "path", "notebook_path", "cwd")
CD_RE = re.compile(r"""(?:^|[;&|(\s])cd\s+("[^"]*"|'[^']*'|[^\s;&|()<>]+)""")
GIT_C_RE = re.compile(r"""\bgit\s+-C\s+("[^"]*"|'[^']*'|[^\s;&|()<>]+)""")
ABS_RE = re.compile(r"""(?:^|[\s"'=:(])((?:~|/)[^\s"'`;|&<>()]+)""")
TITLE_PREFIX_RE = re.compile(r"^\[[^\]]+\]\s*")


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_sid(sid):
    return re.sub(r"[^A-Za-z0-9_-]", "_", sid)


def state_dir():
    return os.environ.get("SESSION_REPO_STATE_DIR") or DEFAULT_STATE_DIR


def jobs_dir():
    return os.environ.get("SESSION_REPO_JOBS_DIR") or DEFAULT_JOBS_DIR


def job_name(sid):
    """Name the background-job daemon gave this session (state.json "name"), or None.

    None for a missing or unreadable file, a non-string or empty name, or a system
    wrapper ("<..."). Never raises.
    """
    try:
        if not isinstance(sid, str) or not sid:
            return None
        path = os.path.join(jobs_dir(), safe_sid(sid[:8]), "state.json")
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return None
        name = data.get("name")
        if isinstance(name, str):
            name = name.strip()
            if name and not name.startswith("<"):
                return name
    except Exception:
        pass
    return None


# ---------------------------------------------------------------- repo resolution

def _resolve_gitfile(gitfile, d):
    """For a .git file, return (root, HEAD path) of the worktree or submodule, or (None, None)."""
    with open(gitfile, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read(4096)
    m = re.search(r"^gitdir:\s*(.+?)\s*$", text, re.M)
    if not m:
        return None, None
    gd = m.group(1)
    if not os.path.isabs(gd):
        gd = os.path.join(d, gd)
    gd = os.path.realpath(gd)
    head = os.path.join(gd, "HEAD")
    idx = gd.find("/worktrees/")
    if idx >= 0:
        common = gd[:idx]
        if os.path.basename(common) == ".git":
            return os.path.realpath(os.path.dirname(common)), head
        return os.path.realpath(common), head
    # Submodule: the directory holding the .git file is the root.
    return os.path.realpath(d), head


def find_repo(path):
    """Return (root realpath, HEAD file path) for the git checkout containing path.

    (None, None) when there is no repo. No subprocess.
    """
    try:
        p = os.path.realpath(os.path.expanduser(path))
        while not os.path.exists(p):
            parent = os.path.dirname(p)
            if parent == p:
                return None, None
            p = parent
        if os.path.isfile(p):
            p = os.path.dirname(p)
        d = p
        while True:
            g = os.path.join(d, ".git")
            if os.path.isdir(g):
                return os.path.realpath(d), os.path.join(g, "HEAD")
            if os.path.isfile(g):
                return _resolve_gitfile(g, d)
            parent = os.path.dirname(d)
            if parent == d:
                return None, None
            d = parent
    except Exception:
        return None, None


def find_root(path):
    """Return the git root containing path, or None."""
    return find_repo(path)[0]


def read_branch(head_path):
    """Branch name from a HEAD file ("ref: refs/heads/X" -> X), None when detached."""
    try:
        with open(head_path, "r", encoding="utf-8", errors="replace") as fh:
            line = fh.readline().strip()
    except Exception:
        return None
    if line.startswith("ref:"):
        ref = line[4:].strip()
        if ref.startswith("refs/heads/"):
            return ref[len("refs/heads/"):] or None
    return None


def _resolve(tok, base):
    tok = tok.strip()
    if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'":
        tok = tok[1:-1]
    if not tok or tok == "-":
        return None
    if tok.startswith(("/", "~")) or not base:
        return tok
    return os.path.join(base, tok)


def explicit_paths(payload):
    """Paths a tool names in its tool_input, resolved to absolute where possible.

    Covers PATH_FIELDS (tool_input.cwd included), `cd` targets, `git -C` targets and
    absolute paths in a Bash command. Relative paths resolve against the payload cwd.
    The payload cwd itself is NOT included: the caller decides whether it counts.
    """
    cwd = payload.get("cwd")
    base = cwd if isinstance(cwd, str) else ""
    out = []
    ti = payload.get("tool_input")
    if isinstance(ti, dict):
        for key in PATH_FIELDS:
            v = ti.get(key)
            if isinstance(v, str) and v:
                out.append(_resolve(v, base))
        cmd = ti.get("command")
        if isinstance(cmd, str):
            for rx in (CD_RE, GIT_C_RE):
                for m in rx.finditer(cmd):
                    out.append(_resolve(m.group(1), base))
            for m in ABS_RE.finditer(cmd):
                out.append(m.group(1))
    out = [p for p in out if p]
    return out[:MAX_CANDIDATES]


def post_tool_paths(payload):
    """Paths one PostToolUse counts. Explicit paths only, so the launch cwd never gets
    a count just because a tool ran there:
      - explicit paths in tool_input (file paths, cd / git -C targets, absolute paths);
      - a Bash call that names no path counts the cwd (the shell ran there);
      - any other tool with no explicit path counts nothing (TaskUpdate, Agent, ToolSearch).
    """
    paths = explicit_paths(payload)
    cwd = payload.get("cwd")
    if not paths and payload.get("tool_name") == "Bash" and isinstance(cwd, str) and cwd:
        paths = [cwd]
    return paths[:MAX_CANDIDATES]


def roots_for(paths):
    """[(root, HEAD path)] once per root. The first worktree touched for a root wins."""
    seen = {}
    for p in paths:
        root, head = find_repo(p)
        if root and root not in seen:
            seen[root] = head
    return list(seen.items())


# ---------------------------------------------------------------- state

def _blank_title():
    return {"base": None, "applied": None, "applied_ts": None, "history": [], "fallback": None}


def _blank_state(sid):
    return {
        "session_id": sid,
        "transcript_path": None,
        "launch_cwd": None,
        "first_prompt": None,
        "repos": {},
        "dominant": None,
        "title": _blank_title(),
        "updated_ts": now_iso(),
    }


def _load_state(path, sid):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            st = _blank_state(sid)
            st.update(data)
            if not isinstance(st.get("repos"), dict):
                st["repos"] = {}
            title = _blank_title()
            if isinstance(st.get("title"), dict):
                title.update(st["title"])
            if not isinstance(title.get("history"), list):
                title["history"] = []
            st["title"] = title
            return st
    except Exception:
        pass
    return _blank_state(sid)


def _pick_branch(rec):
    """Latest-touched branch of a repo, skipping main/master/trunk. Ties: higher count."""
    branches = rec.get("branches")
    if not isinstance(branches, dict):
        return None
    best, best_key = None, None
    for name, b in branches.items():
        if name in EXCLUDED_BRANCHES or not isinstance(b, dict):
            continue
        key = (b.get("last_ts") or "", int(b.get("count", 0) or 0))
        if best_key is None or key > best_key:
            best, best_key = name, key
    return best


def _recompute_dominant(state):
    best_key, best = None, None
    for root, rec in state["repos"].items():
        if not isinstance(rec, dict):
            continue
        key = (int(rec.get("count", 0) or 0), rec.get("last_ts") or "", root)
        if best_key is None or key > best_key:
            best_key = key
            best = {"root": root,
                    "name": rec.get("name") or os.path.basename(root) or root,
                    "branch": _pick_branch(rec)}
    state["dominant"] = best


def update_state(sid, mutate):
    """Run mutate(state) -> bool under an exclusive flock; write the file if it returns True.

    Returns the state as it stood after mutate. The lock covers the whole
    read-modify-write because parallel subagents share one session file.
    """
    sd = state_dir()
    os.makedirs(sd, exist_ok=True)
    path = os.path.join(sd, safe_sid(sid) + ".json")
    with open(path + ".lock", "a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            state = _load_state(path, sid)
            if not mutate(state):
                return state
            _recompute_dominant(state)
            state["updated_ts"] = now_iso()
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
            return state
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


# ---------------------------------------------------------------- titler

def _clean_title(value):
    """Strip the "[x] " prefix. None when empty, not a string, or a system wrapper ("<...")."""
    if not isinstance(value, str):
        return None
    v = TITLE_PREFIX_RE.sub("", value.strip(), count=1).strip()
    if not v or v.startswith("<"):
        return None
    return v


def _is_wrapper(value):
    return isinstance(value, str) and value.strip().startswith("<")


def _scan_title_lines(lines, ours=frozenset()):
    """Latest user custom-title not in ours, else latest ai-title. Wrappers ("<...") skipped."""
    custom = ai = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        kind = obj.get("type")
        if kind == "custom-title":
            v = _clean_title(obj.get("customTitle"))
            if v and v not in ours:
                custom = v
        elif kind == "ai-title":
            v = _clean_title(obj.get("aiTitle"))
            if v:
                ai = v
    return custom if custom is not None else ai


def _transcript_title(path, ours=frozenset()):
    """Latest session title in a transcript JSONL, or None. Reads the last 4 MB first,
    then the whole file if that is under 20 MB and nothing was found."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            start = max(0, size - TRANSCRIPT_TAIL_BYTES)
            fh.seek(start)
            raw = fh.read()
        lines = raw.decode("utf-8", errors="replace").splitlines()
        if start > 0 and lines:
            lines = lines[1:]  # first line may be cut mid-object
        value = _scan_title_lines(lines, ours)
        if value is None and start > 0 and size < TRANSCRIPT_FULL_MAX_BYTES:
            with open(path, "rb") as fh:
                value = _scan_title_lines(fh.read().decode("utf-8", errors="replace").splitlines(), ours)
        return value
    except Exception:
        return None


def _record_title(state, title, history, base, new, now, event):
    """Store the applied title in title state and return the hook JSON line."""
    title.update({"base": base, "applied": new, "applied_ts": now,
                  "history": (history + [new])[-TITLE_HISTORY_MAX:]})
    state["title"] = title
    return json.dumps({"hookSpecificOutput": {"hookEventName": event, "sessionTitle": new}})


def _title_output(state, event, payload, now, transcript):
    """Return the JSON line to print (and update title state), or None.

    Job name: when the background-job daemon has set a job name (job_name), that name
    is the base, prefixed with the repo. No output when the result equals the job name or
    the current session title; it is re-sent on each titled event until the job name carries
    it. Otherwise the rules below are skipped.

    Base rules (no job name):
      - user-named: payload session_title is non-empty, not one we applied (not in
        title.history), and has a non-empty base after the prefix strip -> that base.
      - otherwise (including an empty session_title, which live payloads always send):
        branch, else title.fallback, else transcript title, else first prompt.
        Transcript and prompt results are stored in title.fallback (sticky).
    Skip: no output when new == cur, or when new == applied and cur is "".
    """
    if os.environ.get("SESSION_REPO_TITLE") == "0":
        return None
    dom = state.get("dominant")
    if not dom:
        return None
    title = dict(state.get("title") or _blank_title())
    history = title.get("history") if isinstance(title.get("history"), list) else []
    applied = title.get("applied")
    # Titles this hook wrote, raw and prefix-stripped. Old states lack history.
    ours_set = set()
    for t in [applied] + list(history):
        if isinstance(t, str) and t:
            ours_set.add(t)
            c = _clean_title(t)
            if c:
                ours_set.add(c)
    cur = payload.get("session_title")
    cur = cur if isinstance(cur, str) else ""
    jname = job_name(state.get("session_id") or payload.get("session_id"))
    job_base = _clean_title(jname) if jname else None
    if job_base:
        new = f"[{dom['name']}] {job_base}"
        if new in (jname, cur):
            return None
        # Keep history free of repeats: a re-sent title is not appended again.
        hist = history[:-1] if history and history[-1] == new else history
        return _record_title(state, title, hist, job_base, new, now, event)
    ours = cur == "" or cur in history or (bool(applied) and cur == applied)
    base_cur = TITLE_PREFIX_RE.sub("", cur, count=1)

    if cur and not ours and base_cur and not _is_wrapper(base_cur):
        base = base_cur
    else:
        base = dom.get("branch") or ""
        if not base and not _is_wrapper(title.get("fallback")):
            base = title.get("fallback") or ""
        if not base and transcript:
            base = _transcript_title(transcript, frozenset(ours_set)) or ""
            if base:
                title["fallback"] = base
        if not base:
            prompt = state.get("first_prompt")
            if _is_wrapper(prompt):
                prompt = None
            base = prompt[:TITLE_BASE_CHARS].rstrip() if prompt else ""
            if base:
                title["fallback"] = base
    state["title"] = title  # keeps any fallback change even when nothing is emitted
    if not base:
        return None  # never emit a bare "[repo]"

    new = f"[{dom['name']}] {base}".strip()
    if new == cur:
        return None
    if new == title.get("applied") and (cur == "" or (ours and cur == new)):
        return None  # already applied; host has no title of its own right now
    return _record_title(state, title, history, base, new, now, event)


# ---------------------------------------------------------------- handlers

def handle(payload):
    event = payload.get("hook_event_name")
    sid = payload.get("session_id")
    if not isinstance(sid, str) or not sid or event not in HANDLED:
        return ""

    cwd_raw = payload.get("cwd")
    cwd = cwd_raw if isinstance(cwd_raw, str) and cwd_raw else None
    transcript = payload.get("transcript_path")
    transcript = transcript if isinstance(transcript, str) and transcript else None

    if event == "PostToolUse":
        paths = post_tool_paths(payload)
    elif event == "CwdChanged":
        new_cwd = payload.get("new_cwd")
        if not (isinstance(new_cwd, str) and new_cwd):
            new_cwd = cwd
        paths = [new_cwd] if new_cwd else []
    else:  # UserPromptSubmit, SessionStart: count the payload cwd
        paths = [cwd] if cwd else []

    roots = roots_for(paths)  # filesystem only, done outside the lock
    now = now_iso()
    out = {"text": ""}

    def mutate(state):
        changed = False
        if state.get("transcript_path") is None and transcript:
            state["transcript_path"] = transcript
            changed = True
        if state.get("launch_cwd") is None and cwd:
            state["launch_cwd"] = cwd
            changed = True
        for root, head in roots:
            rec = state["repos"].get(root)
            if not isinstance(rec, dict):
                rec = {"name": os.path.basename(root) or root, "count": 0,
                       "first_ts": now, "last_ts": now}
                state["repos"][root] = rec
            rec["count"] = int(rec.get("count", 0) or 0) + 1
            rec["last_ts"] = now
            branch = read_branch(head) if head else None
            if branch:
                branches = rec.get("branches")
                if not isinstance(branches, dict):
                    branches = {}
                    rec["branches"] = branches
                b = branches.get(branch) if isinstance(branches.get(branch), dict) else {"count": 0}
                branches[branch] = {"count": int(b.get("count", 0) or 0) + 1, "last_ts": now}
            changed = True
        if event == "UserPromptSubmit" and not state.get("first_prompt"):
            prompt = payload.get("prompt")
            stripped = prompt.strip() if isinstance(prompt, str) else ""
            # System wrappers (<task-notification>, <system-reminder>, <command-name>...)
            # start with "<" and are not real user prompts.
            if stripped and not stripped.startswith("<"):
                state["first_prompt"] = " ".join(stripped.split())[:PROMPT_CHARS]
                changed = True
        if event in TITLED:
            _recompute_dominant(state)
            before = json.dumps(state.get("title"), sort_keys=True)
            tpath = transcript or state.get("transcript_path")
            line = _title_output(state, event, payload, now, tpath)
            if line:
                out["text"] = line
            if line or json.dumps(state.get("title"), sort_keys=True) != before:
                changed = True
        return changed

    update_state(sid, mutate)
    return out["text"]


# ---------------------------------------------------------------- debug log

def _debug_log(payload):
    try:
        base = os.path.dirname(os.path.normpath(state_dir()))
        path = os.path.join(base, "session-repo-debug.jsonl")
        if os.path.exists(path) and os.path.getsize(path) >= DEBUG_MAX_BYTES:
            return
        rec = {
            "ts": now_iso(),
            "hook_event_name": payload.get("hook_event_name"),
            "session_id": payload.get("session_id"),
            "agent_id": payload.get("agent_id"),
            "agent_type": payload.get("agent_type"),
            "tool_name": payload.get("tool_name"),
            "payload_keys": sorted(str(k) for k in payload.keys()),
            "session_title": payload.get("session_title"),
        }
        os.makedirs(base, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def main():
    payload = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
    if not isinstance(payload, dict):
        return ""
    _debug_log(payload)
    return handle(payload)


if __name__ == "__main__":
    text = ""
    try:
        text = main() or ""
    except Exception:
        text = ""
    if text:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
    sys.exit(0)
