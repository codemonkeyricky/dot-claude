#!/usr/bin/env python3
"""Plan gate: lint the task block of a plan on ExitPlanMode and gate executor dispatch on the DAG it implies.

Plan text comes from, in order:
  (a) ExitPlanMode tool_input.plan, when a non-empty string;
  (b) the session transcript (payload transcript_path, JSONL, one JSON object per line): the LAST Write, Edit
      or MultiEdit tool_use whose input.file_path is a .md file directly or indirectly under PLANS_DIR; the file
      is read from disk (an unreadable file falls through to (c));
  (c) the newest *.md in PLANS_DIR by mtime.
A plan must contain exactly one fenced block whose info string is `tasks`. The block is JSON: either an array of
tasks, or an object {"serial_reason": str (optional), "tasks": [...]}. A task:
  id        non-empty string, unique
  subject   non-empty string
  writes    list of path/glob strings (may be [])
  needs     optional object {other_id: non-empty reason string}
  dispatch  optional bool, default true (false: the caller's or the user's own task, never an executor's)
The plan must contain exactly one ```files block whenever any task writes something: a directory tree of every file
the tasks write, laid out as in the project. When every task's writes is [], the block is optional, and if present it
may hold directory lines only (no file lines). Format:
  directory line  a name ending in "/" with no [ids] list; the name may hold several segments ("src/parser/") and
                  spaces; text after the name is a note and is ignored
  file line       <name>  [<ids>]  <note>; whitespace is REQUIRED before "[", and <ids> is a comma and/or space
                  separated list of task ids, never empty. The name may hold "/", spaces, brackets ("x[1].py"), or be
                  a glob ("*.sql"), or a directory written as one write entry ("docs/  [C]": a name ending in "/"
                  WITH an id list is a leaf)
  indentation     leading spaces, tree connectors (│, ├──, └──, |--, `--, each followed by a space) and a bare │ or
                  | followed by spaces. Depth is the column where the name starts; a line's parent is the nearest
                  earlier directory line with a smaller depth. A tab in the indentation is a problem, and so is a
                  line indented deeper than the file line just before it.
  top-level leaf  a write that cannot be drawn as nested lines (absolute, containing "//", or with a segment that
                  would not survive parsing) is one top-level file line holding its full path, e.g. "/abs/x.py  [A]"
The full path of a leaf is its ancestor directory names joined with its name (a leaf with no ancestors keeps its
name verbatim). Blank lines are ignored. Any other line shape is a problem naming the line number. Checks, comparing
normalised paths (norm_path):
  every task write must appear as a leaf    "files block is missing <path> (written by #A)"
  every leaf must equal some write          "files block lists <path>, which no task writes"
  a leaf's id set must equal exactly the tasks whose writes contain it
                                            "files block: <path> is tagged [X] but is written by [A, B]"
  no duplicate leaves
The checks run in lint() only when the tasks block parsed and has no cycle. Problems join the same deny list, and
the deny text prints the tree generated from the writes (render_files_tree).

Edges (B waits for A) are computed from the block itself, never from the task list:
  A is in B.needs, OR A appears earlier in the list than B and their writes overlap.
Two write entries overlap when, after normalising (backslash -> /, strip leading ./, lowercase,
trailing / removed), they are equal, one is a directory prefix of the other (prefix + "/"), or
either fnmatch-matches the other. The raw edge set is kept (no transitive reduction).

Plan key: first 10 hex chars of sha1(plan text + str(created)), where created is the PostToolUse time (unix
seconds) saved in plan.json. Task entries belong to a plan only through metadata.plan_key == the key (and
metadata.plan_id), so tasks of an older plan that reuse the same plan ids never satisfy a new plan.

Gates (main session only; a payload with agent_id is a subagent and is never gated):
  PreToolUse ExitPlanMode  denied when: no plan text; no ```tasks block; more than one; JSON invalid
                           (parser message included); schema errors (missing or duplicate id, needs naming
                           an unknown id or itself, empty reason, bad writes/dispatch/serial_reason); a
                           needs-induced dependency cycle (named); a dispatchable task with more than
                           plan_max_writes write entries ("split #X"); a pure chain: at least 4
                           dispatchable tasks and a critical path (longest chain, counting dispatchable tasks only;
                           paths still pass through dispatch:false tasks) equal to their number, with no
                           serial_reason; no ```files block, more than one, or a ```files tree that does not match the writes (see above).
                           Otherwise allowed (no output), and the resolved plan is recorded in
                           <state dir>/<safe session>.plan-pending.json = {path, source, sha1, at}, where source
                           is "input", "transcript" or "newest" and path is null for "input".
  PostToolUse ExitPlanMode deletes the pending record. If it recorded a path, that file is re-read and re-linted
                           (a newer plan file that appeared in between is ignored); otherwise the plan is
                           resolved again as above. On a valid plan it writes <state dir>/<safe session>.plan.json
                           atomically = {plan_file, created, key, serial_reason, tasks, edges: {id: [blocker ids]}}
                           and emits additionalContext: TaskCreate each task with metadata {"plan_key", "plan_id",
                           "writes"} (plus "dispatch": false where applicable), TaskUpdate addBlockedBy per edge
                           (plan ids, "D <- A, B"), then dispatch the level-0 tasks in ONE message, one Agent call
                           each, description ending '· #<id>'. Dependency levels and the critical path length are
                           listed too, then a "Files changed:" tree generated from the writes.
  PreToolUse Agent|Task    when plan.json exists, the plan is active (see Retire) and the subagent type is gated
                           (not web-fetch, claude-code-guide, Explore, Plan, opus-reviewer, pr-reviewer-*, diagnoser-*): denied
                           when a plan task has no live task entry (metadata.plan_key and plan_id), when a plan
                           edge B <- A is missing from the entry's blockedBy, or when a plan task's blockedBy
                           names another plan task that is not a plan edge. Plan tasks with no entry are tolerated
                           once the plan is sealed. When the check passes with every plan task present and exactly
                           the plan's edges, plan.json is rewritten with "sealed": true (atomic). Once sealed,
                           deleting plan tasks does not deny dispatch; the edge checks still apply to the plan
                           tasks that exist.
  PreToolUse TaskUpdate    when plan.json exists and the plan is active: each addBlockedBy / addBlocks edge whose
                           two endpoint entries both carry metadata.plan_key (matching the plan) and
                           metadata.plan_id must be a plan edge, else denied.
Retire: a plan is inactive, and every Agent|Task and TaskUpdate check is skipped, when any state entry (deleted ones
included) has metadata.plan_abandon equal to the plan's key, or when now - created > plan_ttl_sec (harness config,
default 86400; a non-numeric or non-positive value uses the default). The "missing tasks" and "extra edges"
deny messages say how to abandon a plan: TaskCreate a task with subject 'abandon plan <key>' and metadata
{"plan_abandon": "<key>", "dispatch": false}.
Anything else: no output.

Task state is written by task-gate.py and only read here: <state dir>/<safe session>.json, entries keyed
by task id (keys starting with "_" are bookkeeping; status "deleted" entries are ignored for plan ids).
The read is done under task-gate's lock protocol (StateLock is a copy of task-gate.py's Lock, same lock file
<state dir>/<safe session>.lock: O_EXCL create, retry every 50 ms up to 3 s, a lock older than 10 s is stale and
removed, and the read proceeds unlocked when the lock cannot be taken). The lock is held only for the read.

Exempt (hook does nothing): CLAUDE_REQUIRE_TASK=0, CLAUDE_MAIN_WRITE=1, CLAUDE_PLAN_GATE=0, sessions
matched by exempt_env in the harness config (same rules as task-gate.py). Unparseable stdin or any
exception allows silently. Deny output goes to stdout as
{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": text}}
with exit 0; each deny appends one line to the log.

Config (harness config): plan_max_writes (int, default 8), plan_ttl_sec (number, default 86400),
plan_require_files (bool, default true; false skips every ```files check).
Env overrides (tests): TASK_GATE_STATE_DIR, PLAN_GATE_PLANS_DIR, PLAN_GATE_LOG, CLAUDE_HARNESS_CONFIG.
"""
import fnmatch, hashlib, json, os, re, sys, tempfile, time

HOME = os.path.expanduser("~")
STATE_DIR = (os.environ.get("TASK_GATE_STATE_DIR")
             or os.path.join(HOME, ".claude", "hooks", "state", "task-gate"))
PLANS_DIR = os.environ.get("PLAN_GATE_PLANS_DIR") or os.path.join(HOME, ".claude", "plans")
LOG = os.environ.get("PLAN_GATE_LOG") or os.path.join(HOME, ".claude", "hooks", "plan-gate.log")
HARNESS_CONFIG = (os.environ.get("CLAUDE_HARNESS_CONFIG")
                  or os.path.join(HOME, ".claude", "hooks", "harness.local.json"))
DISPATCH_TOOLS = {"Agent", "Task"}
DEFAULT_SUBAGENT = "general-purpose"
EXEMPT_TYPES = {"web-fetch", "claude-code-guide", "Explore", "Plan", "opus-reviewer"}
DEFAULT_MAX_WRITES = 8
DEFAULT_PLAN_TTL = 86400
CHAIN_MIN = 4
PLAN_WRITE_TOOLS = {"Write", "Edit", "MultiEdit"}
LOCK_WAIT = 3.0
LOCK_STALE = 10.0
LOCK_RETRY = 0.05

EXAMPLE = '''```tasks
{"tasks": [
  {"id": "A", "subject": "Add the parser", "writes": ["src/parser.py"]},
  {"id": "B", "subject": "Parser tests", "writes": ["tests/test_parser.py"],
   "needs": {"A": "the tests import the parser module"}},
  {"id": "C", "subject": "Document the parser", "writes": ["docs/parser.md"]}
]}
```

```files
src/
  parser.py          [A]  new
tests/
  test_parser.py     [B]  new
docs/
  parser.md          [C]  edit
```'''


# ---- config and exemptions -------------------------------------------------

def load_exempt_env():
    """{var: [glob, ...]} from the exempt_env key of HARNESS_CONFIG. Never raises."""
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
    for var, pats in load_exempt_env().items():
        val = os.environ.get(var, "")
        if val and any(fnmatch.fnmatchcase(val, p) for p in pats):
            return True
    return False


def exempt():
    if os.environ.get("CLAUDE_REQUIRE_TASK") == "0":
        return True
    if os.environ.get("CLAUDE_MAIN_WRITE") == "1":
        return True
    if os.environ.get("CLAUDE_PLAN_GATE") == "0":
        return True
    return env_exempt()


def harness_value(key):
    try:
        with open(HARNESS_CONFIG, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    return data.get(key) if isinstance(data, dict) else None


def is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def plan_max_writes():
    v = harness_value("plan_max_writes")
    if isinstance(v, int) and not isinstance(v, bool) and v >= 1:
        return v
    return DEFAULT_MAX_WRITES


def plan_ttl():
    v = harness_value("plan_ttl_sec")
    if is_num(v) and v > 0:
        return float(v)
    return float(DEFAULT_PLAN_TTL)


def plan_require_files():
    v = harness_value("plan_require_files")
    return v if isinstance(v, bool) else True


def gated_type(sub):
    return sub not in EXEMPT_TYPES and not sub.startswith(("pr-reviewer-", "diagnoser-"))


# ---- plan text and parsing -------------------------------------------------

def read_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (OSError, ValueError):
        return None


def newest_plan():
    """Path of the newest *.md in PLANS_DIR by mtime, or None."""
    try:
        names = [n for n in os.listdir(PLANS_DIR) if n.endswith(".md")]
    except OSError:
        return None
    best, best_t = None, None
    for n in names:
        path = os.path.join(PLANS_DIR, n)
        try:
            t = os.path.getmtime(path)
        except OSError:
            continue
        if best_t is None or t > best_t:
            best, best_t = path, t
    return best


def under_plans(p):
    """True when p is a .md file directly or indirectly under PLANS_DIR."""
    try:
        base = os.path.normcase(os.path.abspath(PLANS_DIR))
        cand = os.path.normcase(os.path.abspath(os.path.expanduser(p)))
        return cand.lower().endswith(".md") and cand != base and os.path.commonpath([base, cand]) == base
    except Exception:
        return False


def transcript_plan(tp):
    """(text, path) of the last Write/Edit/MultiEdit of a plans-dir .md in the transcript, else (None, None)."""
    if not isinstance(tp, str) or not tp:
        return None, None
    last = None
    try:
        with open(tp, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                msg = obj.get("message") if isinstance(obj, dict) else None
                content = msg.get("content") if isinstance(msg, dict) else None
                if not isinstance(content, list):
                    continue
                for b in content:
                    if not (isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in PLAN_WRITE_TOOLS):
                        continue
                    inp = b.get("input")
                    fp = inp.get("file_path") if isinstance(inp, dict) else None
                    if isinstance(fp, str) and under_plans(fp):
                        last = fp
    except OSError:
        return None, None
    if last is None:
        return None, None
    text = read_text(last)
    return (text, last) if text is not None else (None, None)


def resolve_plan(ti, tp):
    """(text, path, source) from tool_input.plan, the transcript, or the newest plan file; (None, None, None)."""
    v = ti.get("plan")
    if isinstance(v, str) and v.strip():
        return v, None, "input"
    text, path = transcript_plan(tp)
    if text is not None:
        return text, path, "transcript"
    path = newest_plan()
    if path is None:
        return None, None, None
    text = read_text(path)
    if text is None:
        return None, None, None
    return text, path, "newest"


def sha1_hex(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def plan_key(text, created):
    return sha1_hex(text + str(created))[:10]


def fenced_blocks(text, info="tasks"):
    """(bodies of ```<info> fences, error or None). The info string must be exactly info (tasks or files)."""
    bodies, cur = [], None
    for line in text.splitlines():
        if cur is None:
            if re.match(r"^\s*```\s*" + re.escape(info) + r"\s*$", line):
                cur = []
        elif line.strip() == "```":
            bodies.append("\n".join(cur))
            cur = None
        else:
            cur.append(line)
    if cur is not None:
        return bodies, "unterminated ```%s block (no closing ```)" % info
    return bodies, None


def validate_tasks(raw):
    """(normalised tasks, problems). Needs targets are checked after all ids are known."""
    problems, items = [], []
    for i, t in enumerate(raw, 1):
        if not isinstance(t, dict):
            problems.append("task #%d must be a JSON object" % i)
            continue
        tid = t.get("id")
        if not isinstance(tid, str) or not tid.strip():
            problems.append("task #%d: id must be a non-empty string" % i)
            continue
        if re.search(r"[\s,\[\]]", tid):
            problems.append('task "%s": id must not contain spaces, commas or square brackets (the ```files block '
                            "tags leaves with ids)" % tid)
            continue
        label = 'task "%s"' % tid
        if any(x["id"] == tid for x in items):
            problems.append('duplicate id "%s"' % tid)
        subj = t.get("subject")
        if not isinstance(subj, str) or not subj.strip():
            problems.append("%s: subject must be a non-empty string" % label)
        writes = t.get("writes")
        if not isinstance(writes, list) or not all(isinstance(w, str) and w.strip() for w in writes):
            problems.append('%s: writes must be a list of non-empty path strings (use [] for none)' % label)
            writes = []
        else:
            bad = [w for w in writes if not write_renderable(w)]
            if bad:
                problems.append('%s: write %s cannot be written in a ```files block (no leading or trailing spaces, '
                                'tabs or line breaks, and no leading "| " or "├── " style connector); rename it'
                                % (label, json.dumps(bad[0], ensure_ascii=False)))
        needs = t.get("needs")
        if needs is None:
            needs = {}
        if not isinstance(needs, dict):
            problems.append('%s: needs must be an object {"other_id": "reason"}' % label)
            needs = {}
        for k, r in needs.items():
            if not isinstance(r, str) or not r.strip():
                problems.append('%s: needs["%s"] needs a non-empty reason string' % (label, k))
        dispatch = t.get("dispatch", True)
        if not isinstance(dispatch, bool):
            problems.append("%s: dispatch must be true or false" % label)
            dispatch = True
        items.append({"id": tid, "subject": subj, "writes": writes,
                      "needs": {str(k): r for k, r in needs.items()}, "dispatch": dispatch})
    ids = {x["id"] for x in items}
    for x in items:
        for k in x["needs"]:
            if k == x["id"]:
                problems.append('task "%s": needs itself' % x["id"])
            elif k not in ids:
                problems.append('task "%s": needs unknown id "%s"' % (x["id"], k))
    return items, problems


def parse_plan(text):
    """(doc {serial_reason, tasks}, None) or (None, problems)."""
    bodies, err = fenced_blocks(text)
    if err:
        return None, [err]
    if not bodies:
        return None, ["no ```tasks block in the plan"]
    if len(bodies) > 1:
        return None, ["the plan has %d ```tasks blocks; keep exactly one" % len(bodies)]
    try:
        data = json.loads(bodies[0])
    except ValueError as e:
        return None, ["JSON invalid in the ```tasks block: %s" % e]
    serial = None
    if isinstance(data, list):
        raw = data
    elif isinstance(data, dict):
        raw = data.get("tasks")
        if not isinstance(raw, list):
            return None, ['"tasks" must be an array in the ```tasks object']
        if "serial_reason" in data:
            serial = data["serial_reason"]
            if not isinstance(serial, str) or not serial.strip():
                return None, ['"serial_reason" must be a non-empty string when present']
    else:
        return None, ['the ```tasks block must be a JSON array, or an object {"serial_reason": ..., "tasks": [...]}']
    items, problems = validate_tasks(raw)
    if problems:
        return None, problems
    return {"serial_reason": serial, "tasks": items}, None


# ---- files block -----------------------------------------------------------

LEAF_LINE = re.compile(r"^(.*\S)\s+\[([^\[\]]*)\]\s*(.*)$")
DIR_LINE = re.compile(r"^(.*?/)(?:\s.*)?$")
TREE_CONNECTORS = ("├── ", "└── ", "|-- ", "`-- ")


def name_start(line):
    """Column where a tree line's name starts: after leading spaces, tree connectors (each with its space) and
    bare vertical rules (│ or | followed by a space). None when a tab sits in the indentation."""
    i = 0
    while i < len(line):
        c = line[i]
        if c == " ":
            i += 1
        elif c == "\t":
            return None
        elif line.startswith(TREE_CONNECTORS, i):
            i += 4
        elif c in "│|" and line[i + 1:i + 2] == " ":
            i += 1
        else:
            break
    return i


def read_tree_line(line):
    """(depth, kind, name, ids, problem) of one non-blank line with no trailing spaces. kind is "leaf" (name [ids];
    the name keeps any "/") or "dir" (name ending in "/"). problem is None, or "tab", "empty" or "shape"."""
    depth = name_start(line)
    if depth is None:
        return None, None, None, None, "tab"
    rest = line[depth:]
    leaf = LEAF_LINE.match(rest)
    if leaf:
        ids = set(x for x in re.split(r"[,\s]+", leaf.group(2)) if x)
        if not ids:
            return depth, None, None, None, "empty"
        return depth, "leaf", leaf.group(1), ids, None
    d = DIR_LINE.match(rest)
    if d:
        return depth, "dir", d.group(1), None, None
    return depth, None, None, None, "shape"


def parse_files_tree(body):
    """(leaves, problems) of a ```files body. A leaf is {"path", "ids": set, "line"}; its path is the leaf name joined
    to its ancestor directory names (a leaf with no ancestors keeps its name verbatim). Blank lines are skipped."""
    leaves, problems, stack = [], [], []  # stack: (depth, directory path ending in "/"), directories only
    file_depth = None  # depth of the previous line when that line was a valid file line
    for n, raw in enumerate(body.splitlines(), 1):
        line = raw.rstrip()
        if not line.strip():
            continue
        depth, kind, name, ids, problem = read_tree_line(line)
        this_file = None
        if problem == "tab":
            problems.append("files block line %d: use spaces, not tabs, in the ```files block" % n)
        elif problem == "empty":
            problems.append("files block line %d has an empty id list: %s" % (n, line.strip()))
        elif problem:
            problems.append("files block line %d is neither a directory (name ending in /) nor a file line "
                            "(name  [ids]  note): %s" % (n, line.strip()))
        elif file_depth is not None and depth > file_depth:
            problems.append("files block line %d is indented under a file line: %s" % (n, line.strip()))
        else:
            while stack and stack[-1][0] >= depth:
                stack.pop()
            parent = stack[-1][1] if stack else ""
            if kind == "leaf":
                leaves.append({"path": parent + name, "ids": ids, "line": n})
                this_file = depth
            else:
                stack.append((depth, parent + name))
        file_depth = this_file
    return leaves, problems


def tree_problems(tasks, leaves):
    """Problems comparing the leaves of a ```files tree with the tasks' writes (see the module docstring)."""
    writers, first = {}, {}
    for t in tasks:
        for w in t["writes"]:
            n = norm_path(w)
            writers.setdefault(n, set()).add(t["id"])
            first.setdefault(n, w)
    counts = {}
    for lf in leaves:
        n = norm_path(lf["path"])
        counts[n] = counts.get(n, 0) + 1
    problems = []
    for n, ids in writers.items():
        if n not in counts:
            problems.append("files block is missing %s (written by %s)"
                            % (first[n], ", ".join("#" + x for x in sorted(ids))))
    for lf in leaves:
        n = norm_path(lf["path"])
        if n not in writers:
            problems.append("files block lists %s, which no task writes" % lf["path"])
        elif lf["ids"] != writers[n]:
            problems.append("files block: %s is tagged [%s] but is written by [%s]"
                            % (lf["path"], ", ".join(sorted(lf["ids"])), ", ".join(sorted(writers[n]))))
        if counts[n] > 1:
            problems.append("files block lists %s more than once" % lf["path"])
    out = []
    for p in problems:
        if p not in out:
            out.append(p)
    return out


def files_problems(text, tasks):
    """Problems with the plan's ```files block against the tasks' writes ([] when it is fine). With no writes at all,
    a block of directory lines (or an empty one) passes; only file lines are a problem."""
    bodies, err = fenced_blocks(text, "files")
    if err:
        return [err]
    any_writes = any(t["writes"] for t in tasks)
    if not bodies:
        return ["no ```files block in the plan"] if any_writes else []
    if len(bodies) > 1:
        return ["the plan has %d ```files blocks; keep exactly one" % len(bodies)]
    leaves, problems = parse_files_tree(bodies[0])
    if not any_writes:
        if leaves:
            return ["no task writes a file, so the ```files block must have no file lines"]
        return problems
    return problems + tree_problems(tasks, leaves)


def tree_segments(w):
    """(segments, is_dir) when write w can be drawn as a nested tree leaf: its normalised path has no leading "/" and
    no "//", and every segment survives parsing both as a directory line and as a leaf name. Else None."""
    p = w.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    is_dir = p.endswith("/")
    segs = (p[:-1] if is_dir else p).split("/")
    if p.startswith("/") or "//" in p or not all(segs):
        return None
    for i, seg in enumerate(segs):
        if read_tree_line(seg + "/")[1:3] != ("dir", seg + "/"):
            return None
        if i == len(segs) - 1:
            name = seg + "/" if is_dir else seg
            if read_tree_line(name + "  [A]")[1:3] != ("leaf", name):
                return None
    return segs, is_dir


def write_renderable(w):
    """True when write w can appear in a ```files block: as a nested tree leaf, or else as one top-level leaf line
    whose name is w itself (absolute paths, "//", segments that would not survive parsing)."""
    if w.splitlines() != [w]:
        return False
    if tree_segments(w) is not None:
        return True
    return read_tree_line(w + "  [A]")[1:3] == ("leaf", w)


def render_files_tree(tasks):
    """The ```files block generated from the tasks' writes. Writes are grouped by norm_path. A group becomes a leaf in
    the nested tree (directories merged, sorted by name, two spaces per level) when one of its spellings can be drawn
    there; otherwise it is one top-level leaf line holding the written path verbatim. Each leaf is tagged with its
    sorted task ids. parse_files_tree reads the result back to the same leaves."""
    groups = {}
    for t in tasks:
        for w in t["writes"]:
            g = groups.setdefault(norm_path(w), {"ids": set(), "spellings": []})
            g["ids"].add(t["id"])
            g["spellings"].append(w)
    root, top = {}, []
    for key in sorted(groups):
        g = groups[key]
        drawn = [(w, tree_segments(w)) for w in g["spellings"]]
        drawn = [d for d in drawn if d[1] is not None]
        if not drawn:
            top.append("%s  [%s]" % (g["spellings"][0], ", ".join(sorted(g["ids"]))))
            continue
        segs, is_dir = drawn[0][1]
        level = root
        for i, seg in enumerate(segs):
            node = level.setdefault(seg.lower(), {"name": seg, "ids": set(), "dir": False, "kids": {}})
            if i == len(segs) - 1:
                node["ids"] |= g["ids"]
                node["dir"] = is_dir
            level = node["kids"]
    lines = []

    def emit(level, depth):
        for key in sorted(level):
            node = level[key]
            pad = "  " * depth
            if node["ids"]:
                lines.append("%s%s%s  [%s]" % (pad, node["name"], "/" if node["dir"] else "",
                                               ", ".join(sorted(node["ids"]))))
            if node["kids"]:
                lines.append("%s%s/" % (pad, node["name"]))
                emit(node["kids"], depth + 1)

    emit(root, 0)
    return "\n".join(lines + top)


# ---- graph -----------------------------------------------------------------

def norm_path(p):
    s = p.replace("\\", "/").lower()
    while s.startswith("./"):
        s = s[2:]
    return s.rstrip("/")


def paths_overlap(a, b):
    na, nb = norm_path(a), norm_path(b)
    if na == nb:
        return True
    if nb.startswith(na + "/") or na.startswith(nb + "/"):
        return True
    return fnmatch.fnmatchcase(na, nb) or fnmatch.fnmatchcase(nb, na)


def writes_overlap(wa, wb):
    return any(paths_overlap(x, y) for x in wa for y in wb)


def build_edges(tasks):
    """{id: set of ids it waits on}: needs, plus earlier tasks with overlapping writes."""
    blockers = {t["id"]: set(t["needs"]) for t in tasks}
    for j, b in enumerate(tasks):
        for a in tasks[:j]:
            if writes_overlap(a["writes"], b["writes"]):
                blockers[b["id"]].add(a["id"])
    return blockers


def find_cycle(blockers):
    """A waits-on cycle as [n0, n1, ..., n0] (each waits on the next), or None."""
    color, stack = {}, []

    def visit(n):
        color[n] = 1
        stack.append(n)
        for m in sorted(blockers.get(n, ())):
            if color.get(m) == 1:
                return stack[stack.index(m):] + [m]
            if m not in color:
                c = visit(m)
                if c:
                    return c
        stack.pop()
        color[n] = 2
        return None

    for n in sorted(blockers):
        if n not in color:
            c = visit(n)
            if c:
                return c
    return None


def depths(blockers, weight=None):
    """{id: weight sum along the longest blocker chain ending at id} (weight defaults to 1 per node).
    Only call on an acyclic graph."""
    w = weight or {}
    memo = {}

    def d(n):
        if n not in memo:
            memo[n] = w.get(n, 1) + max([d(m) for m in blockers.get(n, ())] or [0])
        return memo[n]

    return {n: d(n) for n in blockers}


def lint(text):
    """(doc, blockers, problems). doc is None on a parse or schema failure; blockers None on a cycle."""
    doc, problems = parse_plan(text)
    if doc is None:
        return None, None, problems
    tasks = doc["tasks"]
    blockers = build_edges(tasks)
    cyc = find_cycle(blockers)
    if cyc:
        return doc, None, ["dependency cycle: " + " -> ".join(cyc)
                           + " (each waits on the next); remove a needs entry or the overlapping writes"]
    limit = plan_max_writes()
    dispatch = [t["id"] for t in tasks if t["dispatch"]]
    problems = []
    for t in tasks:
        if t["dispatch"] and len(t["writes"]) > limit:
            problems.append("split #%s: %d write entries, the limit is %d (plan_max_writes); split the task so "
                            "each one writes at most %d paths" % (t["id"], len(t["writes"]), limit, limit))
    weight = {t["id"]: int(t["dispatch"]) for t in tasks}
    crit = max(depths(blockers, weight).values(), default=0)
    if len(dispatch) >= CHAIN_MIN and crit == len(dispatch) and doc["serial_reason"] is None:
        problems.append("the plan is a pure chain of %d dispatchable tasks (dispatchable critical path %d) with no "
                        "serial_reason; make independent tasks free of needs and overlapping writes, or add "
                        '"serial_reason" explaining why the work must be serial' % (len(dispatch), crit))
    if plan_require_files():
        problems += files_problems(text, tasks)
    return doc, blockers, problems


def plan_reason(problems, expected=None):
    text = ("ExitPlanMode blocked: the plan's ```tasks / ```files blocks need fixing before you leave plan mode:\n- "
            + "\n- ".join(problems)
            + '\nFix: keep exactly one ```tasks fenced block holding a JSON array of tasks, or an object '
              '{"serial_reason": "...", "tasks": [...]}. Each task has id, subject, writes (list, may be []), '
              'optional needs {"other_id": "reason"} and optional dispatch (bool). Keep exactly one ```files fenced '
              'block: a directory tree of every file the tasks write, each file line tagged with its task ids, '
              'matching the writes exactly. Example:\n' + EXAMPLE)
    if expected is not None:
        text += ("\nExpected ```files block (generated from writes; paste it and add notes):\n```files\n"
                 + expected + "\n```")
    return text


NO_PLAN = ("ExitPlanMode blocked: there is no plan text (tool_input.plan is empty, the transcript names no plan "
           "file, and PLANS_DIR has no *.md plan). Write the plan with exactly one ```tasks block and one ```files block, then exit plan "
           "mode. Example:\n" + EXAMPLE)


# ---- plan.json, pending record and task state --------------------------------

def safe_sid(sid):
    return re.sub(r"[^A-Za-z0-9_-]", "_", sid or "") or "unknown"


def plan_path(sid):
    return os.path.join(STATE_DIR, safe_sid(sid) + ".plan.json")


def pending_path(sid):
    return os.path.join(STATE_DIR, safe_sid(sid) + ".plan-pending.json")


def task_state_path(sid):
    return os.path.join(STATE_DIR, safe_sid(sid) + ".json")


def write_json_atomic(path, payload):
    """Atomic write: temp file in the same directory, then os.replace."""
    os.makedirs(STATE_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=STATE_DIR, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_plan(sid, payload):
    write_json_atomic(plan_path(sid), payload)


def record_pending(sid, path, source, text):
    try:
        write_json_atomic(pending_path(sid), {"path": path, "source": source, "sha1": sha1_hex(text),
                                              "at": int(time.time())})
    except Exception:
        pass


def read_pending(sid):
    try:
        with open(pending_path(sid), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def clear_pending(sid):
    try:
        os.unlink(pending_path(sid))
    except OSError:
        pass


def load_plan(sid):
    try:
        with open(plan_path(sid), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        return None
    if not isinstance(data.get("edges"), dict):
        return None
    if not isinstance(data.get("key"), str) or not data["key"]:
        return None
    return data


def plan_edges(plan):
    """Set of (B, A) pairs meaning B waits on A."""
    return {(b, a) for b, lst in plan["edges"].items() if isinstance(lst, list) for a in lst}


class StateLock:
    """Copy of task-gate.py's Lock (same lock file and protocol). Never raises; proceeds unlocked if it cannot be taken."""

    def __init__(self, sid):
        self.path = task_state_path(sid)[:-len(".json")] + ".lock"
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


def read_state(sid):
    with StateLock(sid):
        try:
            with open(task_state_path(sid), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
    return data if isinstance(data, dict) else {}


def live_entries(state):
    return [(k, v) for k, v in state.items()
            if isinstance(v, dict) and not k.startswith("_") and v.get("status") != "deleted"]


def meta_of(e):
    md = e.get("metadata")
    return md if isinstance(md, dict) else {}


def blocked_by(e):
    v = e.get("blockedBy")
    return [str(x) for x in v if x not in (None, "")] if isinstance(v, list) else []


def str_list(v):
    if not isinstance(v, list):
        return []
    return [str(x) for x in v if x not in (None, "")]


def plan_index(state, key):
    """{plan id: task id} from live entries whose metadata.plan_key == key and metadata.plan_id is set
    (first in list order wins)."""
    out = {}
    for k, e in live_entries(state):
        md = meta_of(e)
        pid = md.get("plan_id")
        if isinstance(pid, str) and pid and md.get("plan_key") == key and pid not in out:
            out[pid] = k
    return out


def plan_inactive(plan, state):
    """True when the plan is abandoned (a state entry, deleted ones included, has plan_abandon == key) or expired."""
    key = plan["key"]
    for k, e in state.items():
        if isinstance(e, dict) and not k.startswith("_") and meta_of(e).get("plan_abandon") == key:
            return True
    created = plan.get("created")
    return is_num(created) and time.time() - created > plan_ttl()


def abandon_text(key):
    return ("If this plan is abandoned, TaskCreate a task with subject 'abandon plan %s' and metadata "
            '{"plan_abandon": "%s", "dispatch": false}.' % (key, key))


def dispatch_problem(plan, state, sealed=False):
    key = plan["key"]
    idx = plan_index(state, key)
    pids = [t["id"] for t in plan["tasks"]]
    missing = [p for p in pids if p not in idx]
    if missing and not sealed:
        return ('TaskCreate tasks for plan ids: %s with metadata {"plan_key": "%s", "plan_id": "<id>", '
                '"writes": [...]} before dispatching. %s' % (", ".join(missing), key, abandon_text(key)))
    edges = plan_edges(plan)
    bad = [("%s <- %s" % (b, a)) for b, a in sorted(edges)
           if b in idx and a in idx and idx[a] not in blocked_by(state[idx[b]])]
    if bad:
        return ("missing edges: %s; TaskUpdate addBlockedBy on each task so the entries match the plan"
                % "; ".join(bad))
    rev = {v: k for k, v in idx.items()}
    extra = []
    for pid in pids:
        if pid not in idx:
            continue
        for x in blocked_by(state[idx[pid]]):
            other = rev.get(x)
            if other is not None and (pid, other) not in edges:
                extra.append("%s <- %s" % (pid, other))
    if extra:
        return ("extra edges: %s; TaskUpdate cannot remove blockers, so delete the task (TaskUpdate status "
                '"deleted") and recreate it with the plan metadata and only the plan\'s edges, or abandon the '
                "plan. %s" % ("; ".join(extra), abandon_text(key)))
    return None


def update_problem(plan, state, ti):
    tid = ti.get("taskId")
    if tid in (None, ""):
        return None
    tid = str(tid)
    pairs = ([(tid, x) for x in str_list(ti.get("addBlockedBy"))]
             + [(x, tid) for x in str_list(ti.get("addBlocks"))])
    edges = plan_edges(plan)
    rev = {}
    for k, e in live_entries(state):
        md = meta_of(e)
        pid = md.get("plan_id")
        if isinstance(pid, str) and pid and md.get("plan_key") == plan["key"]:
            rev.setdefault(k, pid)
    for b_task, a_task in pairs:
        pb, pa = rev.get(b_task), rev.get(a_task)
        if pb is None or pa is None:
            continue
        if (pb, pa) not in edges:
            return ("edge %s <- %s is not in the plan's computed DAG; edit the plan's needs/writes instead"
                    % (b_task, a_task))
    return None


def write_plan(sid, doc, blockers, path, created, key):
    try:
        save_plan(sid, {
            "plan_file": path,
            "created": created,
            "key": key,
            "serial_reason": doc["serial_reason"],
            "tasks": doc["tasks"],
            "edges": {t: sorted(blockers[t]) for t in blockers},
        })
    except Exception:
        pass


def seal_plan(sid, plan):
    try:
        save_plan(sid, dict(plan, sealed=True))
    except Exception:
        pass


def context_text(doc, blockers, key):
    tasks = doc["tasks"]
    depth = depths(blockers)
    levels = {}
    for t in tasks:
        levels.setdefault(depth[t["id"]] - 1, []).append(t["id"])
    lvl = "; ".join("level %d: %s" % (n, ", ".join(ids)) for n, ids in sorted(levels.items()))
    edge_lines = ["%s <- %s" % (t["id"], ", ".join(sorted(blockers[t["id"]])))
                  for t in tasks if blockers[t["id"]]]
    edges = "; ".join(edge_lines) if edge_lines else "none (all tasks are independent)"
    now = [t["id"] for t in tasks if depth[t["id"]] == 1 and t["dispatch"]]
    now_txt = ", ".join(now) if now else "none"
    dispatchable = sum(1 for t in tasks if t["dispatch"])
    dag = ("Plan DAG recorded (plan key %s): %d tasks (%d dispatchable), critical path length %d (counting all "
            "tasks). Dependency levels (level 0 = no blockers; tasks with dispatch false are not dispatched): %s. "
            'Steps, in order: (1) TaskCreate each task with metadata {"plan_key": "%s", "plan_id": "<id>", '
            '"writes": [...]} (dispatch:false tasks also get "dispatch": false); (2) TaskUpdate addBlockedBy for '
            "each edge, listed as plan ids: %s; (3) dispatch every task with no blockers in ONE message, one Agent "
            "call each, description ending '· #<task id>': now %s."
            % (key, len(tasks), dispatchable, max(depth.values(), default=0), lvl, key, edges, now_txt))
    return dag + "\n\nFiles changed:\n" + (render_files_tree(tasks) or "(none)")


# ---- gates -----------------------------------------------------------------

def emit(event, text):
    if event == "PreToolUse":
        out = {"hookEventName": event, "permissionDecision": "deny", "permissionDecisionReason": text}
    else:
        out = {"hookEventName": event, "additionalContext": text}
    print(json.dumps({"hookSpecificOutput": out}))


def log_deny(kind, sid, reason):
    try:
        with open(LOG, "a", encoding="utf-8", errors="replace") as f:
            f.write("%s DENY %s session=%s reason=%s\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"), kind, sid[:36], reason.replace("\n", " | ")[:200]))
    except Exception:
        pass


def exit_check(ti, tp):
    """(reason or None, text, path, source) for a PreToolUse ExitPlanMode."""
    text, path, source = resolve_plan(ti, tp)
    if text is None:
        return NO_PLAN, None, None, None
    doc, blockers, problems = lint(text)
    if not problems:
        return None, text, path, source
    expected = None
    if doc is not None and blockers is not None and plan_require_files() and files_problems(text, doc["tasks"]):
        expected = render_files_tree(doc["tasks"])
    return plan_reason(problems, expected), text, path, source


def post_exit(sid, ti, tp):
    pend = read_pending(sid)
    clear_pending(sid)
    text = path = None
    rec = pend.get("path") if pend else None
    if isinstance(rec, str) and rec:
        text, path = read_text(rec), rec
    if text is None:
        text, path, _ = resolve_plan(ti, tp)
    if text is None:
        return
    doc, blockers, problems = lint(text)
    if problems or doc is None:
        return
    created = int(time.time())
    key = plan_key(text, created)
    write_plan(sid, doc, blockers, path, created, key)
    emit("PostToolUse", context_text(doc, blockers, key))


def dispatch_reason(sid, ti):
    sub = str(ti.get("subagent_type") or DEFAULT_SUBAGENT)
    if not gated_type(sub):
        return None
    plan = load_plan(sid)
    if plan is None:
        return None
    state = read_state(sid)
    if plan_inactive(plan, state):
        return None
    sealed = plan.get("sealed") is True
    problem = dispatch_problem(plan, state, sealed)
    if problem is None:
        if not sealed:
            seal_plan(sid, plan)
        return None
    return ("Executor dispatch blocked: the task list does not match the plan's computed DAG. %s. Fix the task "
            "list (TaskCreate / TaskUpdate addBlockedBy) to match the plan, then retry the Agent call." % problem)


def update_reason(sid, ti):
    plan = load_plan(sid)
    if plan is None:
        return None
    state = read_state(sid)
    if plan_inactive(plan, state):
        return None
    problem = update_problem(plan, state, ti)
    if problem is None:
        return None
    return problem + "."


def main():
    try:
        p = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
        if not isinstance(p, dict):
            return
        if p.get("agent_id") or exempt():
            return
        event = p.get("hook_event_name", "")
        tool = p.get("tool_name", "")
        ti = p.get("tool_input") if isinstance(p.get("tool_input"), dict) else {}
        sid = p.get("session_id") or ""
        tp = p.get("transcript_path")
        if tool == "ExitPlanMode":
            if event == "PreToolUse":
                reason, text, path, source = exit_check(ti, tp)
                if reason is None:
                    record_pending(sid, path, source, text)
                    return
                kind = "ExitPlanMode"
            elif event == "PostToolUse":
                post_exit(sid, ti, tp)
                return
            else:
                return
        elif event != "PreToolUse":
            return
        elif tool in DISPATCH_TOOLS:
            reason = dispatch_reason(sid, ti)
            if reason is None:
                return
            kind = "Agent"
        elif tool == "TaskUpdate":
            reason = update_reason(sid, ti)
            if reason is None:
                return
            kind = "TaskUpdate"
        else:
            return
    except Exception:
        return  # fail open
    log_deny(kind, sid, reason)
    emit("PreToolUse", reason)


if __name__ == "__main__":
    main()
