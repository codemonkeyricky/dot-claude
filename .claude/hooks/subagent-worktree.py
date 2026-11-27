#!/usr/bin/env python3
"""PreToolUse guard: a subagent may change files only inside a linked git worktree.

Applies only to subagent calls (payload has `agent_id`). The main agent is
main-readonly.py's job; this file never restricts it.

Exempt (allowed everything): env CLAUDE_SUBAGENT_ANYWHERE=1, env CLAUDE_MAIN_WRITE=1,
a session matched by `exempt_env` in the local harness config (harness.local.json, the
same mapping main-readonly.py and task-gate.py read), and agent types Explore, Plan,
claude-code-guide, statusline-setup, opus-reviewer, or any starting with pr-reviewer- or diagnoser-.

Allowed location for a write target, a shell working directory or a file-operation path:
  - a scratch root: $CLAUDE_JOB_DIR/tmp, or tempfile.gettempdir();
  - inside a LINKED git worktree: walking up from the directory, the first `.git`
    entry is a file whose gitdir is <repo>/.git/worktrees/<name>. A `.git`
    directory (main checkout), a `.git` file pointing into .git/modules/
    (submodule), or no `.git` at all is NOT allowed.
Paths are compared after realpath + normcase, so junctions and symlinks cannot
lead out of a worktree. A path that does not exist yet resolves through its
nearest existing ancestor. On Windows, /c/rest, /cygdrive/c/rest and C:\rest name the
same path: the MSYS forms are read as <LETTER>:\rest wherever a path is resolved.

Write tools (Write, Edit, MultiEdit: file_path; NotebookEdit: notebook_path)
must target an allowed location.

Shell tools (Bash, PowerShell) are split into statements on &&, ||, ;, newline,
& and pipes. Before a statement is judged:
  - a simple literal assignment (NAME=value, NAME="value", NAME='value' in Bash;
    $name = 'value' or "value" in PowerShell) is recorded and is itself read-only;
    later statements have NAME, ${NAME} and quoted forms expanded. A reference to an
    unknown variable stays in the text, which makes a path through it unresolvable;
  - a Bash heredoc body (<<WORD, <<'WORD', <<"WORD", <<-WORD, up to the terminator line) is
    skipped: it is data, and the line that opens it is judged as usual. An unquoted body
    still runs $( ) and backticks, so such a line is judged with the header. <<< is not a heredoc;
  - leading control keywords (until, while, if, do, then, else, elif) are dropped
    and the rest is judged; done, fi, }, {, esac and `for NAME in` heads are read-only.
cd / Set-Location / sl / pushd move a tracked working directory (popd changes
nothing). Then the rules, in order:
  1. Every output target must be a null device or an allowed location: the targets
     of > >> 2> &> >| >&file, tee, curl -o/--output, and PowerShell Out-File,
     Set-Content, Add-Content (-Path, else the first positional argument).
  2. A file operation is judged by its path arguments alone, and the working
     directory does not matter. mkdir, touch, rm, rmdir, New-Item, Remove-Item, ni,
     ri, mv, Move-Item and move need every path allowed. cp, Copy-Item, copy and ln
     need only the destination (the last path, or -Destination) allowed. A cp, mv or
     ln given -t / --target-directory is unresolvable.
  3. A statement made only of read-only commands is allowed anywhere (cmd_read_only:
     sed without -i, bash `set` options, read-only git subcommands, gh except the
     commands that write local files, curl without -O/-d/-F/-T/--output-dir).
  4. Any other statement must run in an allowed directory; for `git -C <p> <mutating
     subcommand>` the directory checked is p instead.
Command substitution is never parsed: a file operation containing it is denied, and
any other command containing it needs an allowed directory. A path that depends on an
unknown variable, or on an unresolved directory, is denied.

Config allowlist: `subagent_allow_commands` in harness.local.json is a list of fnmatch
patterns (case-sensitive) matched against a statement's words joined by single spaces,
with quotes removed. A match lets that command run in any directory (e.g. a deploy
script). Redirects and output targets (rule 1) are still checked. File operations
(rule 2) are never allowlisted, so a pattern cannot exempt them. A statement containing
`$(` or a backtick never matches.

BEST-EFFORT: a mutating command that names its target by an absolute path outside
its working directory, and that the rules above do not recognise (e.g. `make
C:/main/x` run from a worktree), is judged by the working directory only and is not
caught.

Fails open (allows) on unparseable stdin and on any internal error.
Each denial is logged as one line to subagent-worktree.log beside this file, or in
$CLAUDE_HOOK_LOG_DIR when that is set (tests use it to keep the real log untouched).
"""
import fnmatch, json, os, re, sys, tempfile, time

HOME = os.path.expanduser("~")
LOG = os.path.join(os.environ.get("CLAUDE_HOOK_LOG_DIR")
                   or os.path.dirname(os.path.abspath(__file__)), "subagent-worktree.log")
MSYS_DRIVE = re.compile(r"/(?:cygdrive/)?([A-Za-z])(/.*)?\Z", re.S)
WRITE_TOOLS = {"Write": "file_path", "Edit": "file_path",
               "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
SHELL_TOOLS = {"Bash", "PowerShell"}
EXEMPT_AGENT_TYPES = {"Explore", "Plan", "claude-code-guide",
                      "statusline-setup", "opus-reviewer"}
NAV_CMDS = {"cd", "set-location", "sl", "pushd", "popd"}
GENERIC_READ = {"ls", "dir", "cat", "type", "head", "tail", "wc", "grep", "egrep",
                "rg", "find", "pwd", "echo", "printf", "which", "where", "file",
                "stat", "du", "df", "tree", "sort", "uniq", "cut", "jq", "diff",
                "cmp", "basename", "dirname", "realpath", "readlink", "date", "env",
                "whoami", "hostname", "true"}
READ_EXTRA = {"sleep", "test", "[", "[[", "false", ":", "exit", "start-sleep"}
PS_READ = {"get-childitem", "gci", "get-content", "gc", "get-item", "gi",
           "test-path", "select-string", "sls", "get-process", "get-service",
           "measure-object", "select-object", "where-object", "sort-object",
           "format-table", "ft", "format-list", "fl", "convertfrom-json",
           "convertto-json", "resolve-path", "get-date", "get-command",
           "get-location", "write-output", "write-host", "out-string",
           "get-filehash", "get-itemproperty", "gp", "get-acl", "split-path", "join-path",
           "group-object", "compare-object", "gcm", "rvpa", "gps", "gsv"}
CONTROL_WORDS = {"until", "while", "if", "do", "then", "else", "elif"}
STRUCT_WORDS = {"done", "fi", "}", "{", "esac"}
FOR_HEAD = re.compile(r"for\s+[A-Za-z_]\w*\s+in(\s|$)", re.IGNORECASE)
BASH_ASSIGN = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", re.S)
PS_ASSIGN = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)", re.S)
VAR_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
GIT_READ = {"status", "log", "diff", "show", "rev-parse", "ls-files", "ls-tree",
            "cat-file", "blame", "grep", "describe", "shortlog", "reflog",
            "merge-base", "for-each-ref", "name-rev", "fetch", "rev-list",
            "show-ref", "count-objects", "check-ignore", "var"}
GIT_BRANCH_FLAGS = {"-a", "-r", "-v", "-vv", "--show-current"}
GIT_BRANCH_LIST = {"-l", "--list"}  # after these, bare words are name patterns, not branch names
GIT_BRANCH_FILTERS = {"--contains", "--merged", "--no-merged"}  # take an optional commit
GH_LOCAL_WRITES = {("repo", "clone"), ("pr", "checkout"), ("run", "download"),
                   ("release", "download"), ("gist", "clone"), ("extension", "install"),
                   ("auth", "login"), ("auth", "setup-git"), ("alias", "set"),
                   ("config", "set")}
FILE_OPS_ALL = {"mkdir", "touch", "rm", "rmdir", "new-item", "remove-item", "ni", "ri",
                "mv", "move-item", "move"}
FILE_OPS_DEST = {"cp", "copy-item", "copy", "ln"}
PS_WRITERS = {"out-file", "set-content", "add-content"}
PS_PATH_PARAMS = {"path", "literalpath", "filepath", "pspath"}
PS_DEST_PARAMS = {"destination"}
PS_VALUE_PARAMS = {"itemtype", "name", "value", "filter", "include", "exclude", "type",
                   "encoding", "variable", "erroraction", "ea", "warningaction", "wa"}
NULL_TARGETS = {"/dev/null", "nul", "$null"}
REASON = ("Subagents may only change files inside a git worktree (%s). "
          "Create one: git -C <repo> worktree add <repo>/.claude/worktrees/<name> -b <branch>, "
          "then write there and prefix shell commands with cd <worktree> &&. "
          "Scratch output may go in $CLAUDE_JOB_DIR/tmp or %%TEMP%%. "
          "To run a script that only writes to scratch, run it from there: cd $CLAUDE_JOB_DIR/tmp && … "
          "Read-only commands are allowed anywhere.")

# Machine-local config (not committed), also read by main-readonly.py and task-gate.py. This hook
# reads only exempt_env and subagent_allow_commands from it:
#   {"exempt_env": {"VAR": ["glob", ...]}, "subagent_allow_commands": ["glob", ...]}
HARNESS_CONFIG = (os.environ.get("CLAUDE_HARNESS_CONFIG")
                  or os.path.join(HOME, ".claude", "hooks", "harness.local.json"))


def _read_config():
    """The parsed HARNESS_CONFIG object, or {} when missing, unreadable or not a JSON object."""
    try:
        with open(HARNESS_CONFIG, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def load_exempt_env():
    """{var: [glob, ...]} from exempt_env. Never raises: a missing, unreadable or malformed
    file yields {}, and a malformed entry is skipped."""
    raw = _read_config().get("exempt_env", {})
    if not isinstance(raw, dict):
        return {}
    out = {}
    for var, pats in raw.items():
        if isinstance(var, str) and var and isinstance(pats, list) and all(isinstance(p, str) for p in pats):
            out[var] = pats
    return out


def load_allow_commands():
    """fnmatch patterns from subagent_allow_commands. Never raises: missing/malformed -> []."""
    raw = _read_config().get("subagent_allow_commands", [])
    if not isinstance(raw, list):
        return []
    return [p for p in raw if isinstance(p, str) and p.strip()]


def env_exempt():
    """True when some exempt_env variable is non-empty and matches one of its globs."""
    for var, pats in load_exempt_env().items():
        val = os.environ.get(var, "")
        if val and any(fnmatch.fnmatchcase(val, p) for p in pats):
            return True
    return False


# ---------- location checks ----------

def _real(path):
    return os.path.normcase(os.path.realpath(path))


def msys_path(p):
    """On Windows, /c/rest, /cygdrive/c/rest become C:\\rest (a bare /c becomes C:\\). Any other
    path, and every path off Windows, is returned unchanged."""
    if os.name != "nt" or not isinstance(p, str):
        return p
    m = MSYS_DRIVE.match(p)
    if not m:
        return p
    rest = (m.group(2) or "").replace("/", "\\")
    return m.group(1).upper() + ":" + (rest or "\\")


def _scratch_roots(cwd):
    roots = [tempfile.gettempdir()]
    job = os.environ.get("CLAUDE_JOB_DIR")
    if job:
        roots.append(os.path.join(cwd, msys_path(job), "tmp"))
    return [_real(r) for r in roots if r]


def _gitfile_kind(g, d):
    """Kind of a `.git` FILE found in directory d: 'linked' or 'other'."""
    try:
        with open(g, encoding="utf-8", errors="replace") as f:
            line = f.readline().strip()
    except OSError:
        return "other"
    if not line.lower().startswith("gitdir:"):
        return "other"
    gd = _real(os.path.join(d, line[len("gitdir:"):].strip()))
    parts = gd.split(os.sep)
    if "worktrees" in parts:
        i = parts.index("worktrees")
        if i >= 1 and parts[i - 1] == ".git":
            return "linked"
    return "other"


def _git_kind(d):
    """Kind of the first `.git` entry walking up from existing directory d.
    'main' for a .git directory, 'linked' / 'other' for a .git file, None if none."""
    while True:
        g = os.path.join(d, ".git")
        if os.path.isdir(g):
            return "main"
        if os.path.isfile(g):
            return _gitfile_kind(g, d)
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def allowed_dir(path, cwd):
    """True if path (absolute or relative to cwd) is in a scratch root or a linked worktree."""
    real = _real(os.path.join(cwd, path))  # realpath is non-strict: a missing tail is kept as-is
    for root in _scratch_roots(cwd):
        if real == root or real.startswith(root + os.sep):
            return True
    d = real if os.path.isdir(real) else os.path.dirname(real)
    while not os.path.isdir(d) and os.path.dirname(d) != d:
        d = os.path.dirname(d)
    return _git_kind(d) == "linked"


# ---------- shell parsing ----------

def split_ops(cmd, shell):
    """Split on top-level &&, ||, ;, newline, |, and & (background / PS call).
    Returns [(text, op)] where op is the separator that follows text ('' for the last)."""
    out, buf, q, i, n = [], [], None, 0, len(cmd)
    while i < n:
        c = cmd[i]
        if q:
            buf.append(c)
            if q == '"' and c == "\\" and shell == "bash" and i + 1 < n:
                buf.append(cmd[i + 1])
                i += 2
                continue
            if c == q:
                q = None
            i += 1
            continue
        if c in "'\"":
            q = c
            buf.append(c)
            i += 1
            continue
        if c == "\\" and shell == "bash" and i + 1 < n:
            buf.append(c + cmd[i + 1])
            i += 2
            continue
        if cmd[i:i + 2] in ("&&", "||"):
            out.append(("".join(buf), cmd[i:i + 2]))
            buf = []
            i += 2
            continue
        if c in ";\n":
            out.append(("".join(buf), ";"))
            buf = []
            i += 1
            continue
        if c == "|":
            out.append(("".join(buf), "|"))
            buf = []
            i += 1
            continue
        if c == "&" and (i == 0 or cmd[i - 1] not in "<>") and cmd[i + 1:i + 2] != ">":
            out.append(("".join(buf), "&"))
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    out.append(("".join(buf), ""))
    return out


HEREDOC_WORD = re.compile(r"<<(-?)[ \t]*(?:'([^'\n]*)'|\"([^\"\n]*)\"|\\?([A-Za-z0-9_.\-]+))")


def heredoc_delims(line):
    """[(delimiter, strip_tabs, quoted)] for each <<WORD heredoc opened on line, outside quotes.
    <<< here-strings are not heredocs."""
    out, q, i, n = [], None, 0, len(line)
    while i < n:
        c = line[i]
        if q:
            if c == q:
                q = None
            i += 1
        elif c in "'\"":
            q = c
            i += 1
        elif c == "\\":
            i += 2
        elif line.startswith("<<<", i):
            i += 3
        elif line.startswith("<<", i):
            m = HEREDOC_WORD.match(line, i)
            if m:
                quoted = m.group(2) is not None or m.group(3) is not None
                delim = next(g for g in m.groups()[1:] if g is not None)
                out.append((delim, m.group(1) == "-", quoted))
                i = m.end()
            else:
                i += 2
        else:
            i += 1
    return out


def strip_heredocs(cmd, shell):
    """cmd with heredoc bodies and their terminator lines removed; the line that opens a heredoc
    stays and is judged as usual. A body is data, except that an UNQUOTED heredoc runs $( ) and
    backticks in it, so such a body line is appended to its header line for judgment.
    Bash only."""
    if shell != "bash" or "<<" not in cmd:
        return cmd
    out, pending = [], []  # pending: [delim, strip_tabs, quoted, index of the header line]
    for line in cmd.split("\n"):
        if pending:
            delim, strip, quoted, hdr = pending[0]
            if not quoted and ("$(" in line or "`" in line):
                out[hdr] += " " + line.strip()
            if (line.lstrip("\t") if strip else line) == delim:
                pending.pop(0)
            continue
        out.append(line)
        for delim, strip, quoted in heredoc_delims(line):
            pending.append([delim, strip, quoted, len(out) - 1])
    return "\n".join(out)


def statements(cmd, shell):
    """List of statements; each statement is a list of pipe-part strings."""
    stmts, cur = [], [""]
    for text, op in split_ops(cmd, shell):
        cur[-1] += text
        if op == "|":
            cur.append("")
        else:
            stmts.append(cur)
            cur = [""]
    return stmts


def words(s):
    """Whitespace split honouring quotes; quotes removed, backslashes kept."""
    out, buf, q, has = [], [], None, False
    for c in s:
        if q:
            if c == q:
                q = None
            else:
                buf.append(c)
        elif c in "'\"":
            q = c
            has = True
        elif c.isspace():
            if buf or has:
                out.append("".join(buf))
                buf, has = [], False
        else:
            buf.append(c)
    if buf or has:
        out.append("".join(buf))
    return out


def _first_word(s):
    """The first shell word of s with its quotes removed ('' when there is none)."""
    out, q = [], None
    for c in s:
        if q:
            if c == q:
                q = None
            else:
                out.append(c)
        elif c in "'\"":
            q = c
        elif c.isspace() or c in ";|&<>()":
            break
        else:
            out.append(c)
    return "".join(out)


def redirect_targets(s):
    """Targets of the unquoted > and >> redirections in s (>| and &> included). File
    descriptor duplications such as 2>&1 and >&- are not targets."""
    out, q, i = [], None, 0
    while i < len(s):
        c = s[i]
        if q:
            if c == q:
                q = None
            i += 1
            continue
        if c in "'\"":
            q = c
            i += 1
            continue
        if c == ">":
            j = i + 1
            if j < len(s) and s[j] == ">":
                j += 1
            if j < len(s) and s[j] == "|":
                j += 1
            rest = s[j:].lstrip()
            if rest.startswith("&"):
                if not re.match(r"&\s*(\d+|-)", rest):
                    out.append(_first_word(rest[1:]))  # >&file writes to a file
            else:
                out.append(_first_word(rest))
            i = j
            continue
        i += 1
    return out


def expand(p):
    p = msys_path(p)
    p = re.sub(r"\$\{?HOME\}?|\$env:USERPROFILE", lambda m: HOME, p, flags=re.I)
    if p.startswith("~"):
        p = HOME + p[1:]
    return p


def resolve_dir(p, base):
    """Absolute normalised path for p (relative to base), or None when it cannot be known:
    a relative path with no base, or any variable or command substitution left in it."""
    t = expand(p)
    if "$" in t or "`" in t:
        return None
    if os.path.isabs(t):
        return os.path.normpath(t)
    if base is None:
        return None
    return os.path.normpath(os.path.join(base, t))


def navigate(head, args, cur):
    if head == "popd":
        return cur
    # Skip options (cd -P, Set-Location -Path ...) but keep "-" (the previous directory).
    rest = [a for a in args if a == "-" or (not a.startswith("-") and a.lower() != "/d")]
    raw = rest[0] if rest else "~"
    if raw == "-":
        return None
    return resolve_dir(raw, cur)


def git_split(w):
    """For a git word list: (value of -C or None, lowercased subcommand, remaining args)."""
    i, cdir = 1, None
    while i < len(w):
        a = w[i]
        if a == "-C" and i + 1 < len(w):
            cdir = w[i + 1]
            i += 2
        elif a == "-c" and i + 1 < len(w):
            i += 2
        elif a in ("--no-pager", "-P", "--no-optional-locks"):
            i += 1
        else:
            break
    sub = w[i].lower() if i < len(w) else ""
    return cdir, sub, w[i + 1:]


def git_read_only(w):
    _, sub, rest = git_split(w)
    if sub in GIT_READ:
        return True
    if sub == "branch":
        # Listing only: a bare word is a branch name (creation) unless --list/-l came first.
        listing, k = False, 0
        while k < len(rest):
            a = rest[k]
            if a in GIT_BRANCH_LIST:
                listing = True
                k += 1
            elif a in GIT_BRANCH_FLAGS:
                k += 1
            elif a == "--format":
                k += 2
            elif a.startswith("--format=") or any(a.startswith(f + "=") for f in GIT_BRANCH_FILTERS):
                k += 1
            elif a in GIT_BRANCH_FILTERS:
                k += 2 if k + 1 < len(rest) and not rest[k + 1].startswith("-") else 1
            elif listing and not a.startswith("-"):
                k += 1  # a name pattern
            else:
                return False  # -d -D -m -M -c -C -u --set-upstream-to --unset-upstream -f, names
        return True
    if sub == "remote":
        return not rest or rest in (["-v"], ["--verbose"]) or rest[0].lower() in ("get-url", "show")
    if sub == "config":
        return any(a in ("--get", "--get-all", "--get-regexp", "--list", "-l") for a in rest)
    if sub == "worktree":
        return bool(rest) and rest[0] in ("list", "add", "prune", "remove")
    if sub == "stash":
        return bool(rest) and rest[0].lower() in ("list", "show")
    if sub == "tag":
        if not rest:
            return True
        flags = [a for a in rest if a.startswith("-")]
        return bool(flags) and all(f in ("-l", "--list") or f.startswith("-n") for f in flags)
    if sub == "symbolic-ref":
        if any(a in ("-d", "--delete", "-m") or a.startswith("--message") for a in rest):
            return False
        return len([a for a in rest if not a.startswith("-")]) == 1
    return False


def gh_writes_local(w):
    """True for the gh commands that write files into the working directory or home config."""
    a = [x.lower() for x in w[1:]]
    if len(a) >= 2 and (a[0], a[1]) in GH_LOCAL_WRITES:
        return True
    return (len(a) >= 2 and (a[0], a[1]) == ("repo", "fork")
            and any(x == "--clone" or x.startswith("--clone=") for x in a))


def curl_parse(w):
    """(read_only, output targets) for a curl word list. -O, --remote-name, -d, -F, -T and
    --output-dir make the call mutating; -o / --output name an output file that is checked
    as a target."""
    args = w[1:]
    ok, targets, i = True, [], 0
    while i < len(args):
        a = args[i]
        low = a.lower()
        nxt = args[i + 1] if i + 1 < len(args) else ""
        if low.startswith(("--data", "--form", "--upload-file", "--remote-name", "--output-dir")):
            ok = False
        elif low == "--output":
            targets.append(nxt)
            i += 1
        elif low.startswith("--output="):
            targets.append(a[len("--output="):])
        elif low.startswith("--output"):
            ok = False
        elif low.startswith("--request="):
            if a[len("--request="):].upper() != "GET":
                ok = False
        elif a == "--request":
            if nxt.upper() != "GET":
                ok = False
        elif a.startswith("-") and not a.startswith("--") and len(a) > 1:
            cluster = a[1:]
            for j, ch in enumerate(cluster):
                if ch == "o":
                    attached = cluster[j + 1:]
                    if attached:
                        targets.append(attached)
                    else:
                        targets.append(nxt)
                        i += 1
                    break
                if ch in "dFTO":
                    ok = False
                    break
                if ch == "X":
                    attached = cluster[j + 1:]
                    if (attached if attached else nxt).upper() != "GET":
                        ok = False
                    break
        i += 1
    return ok, targets


def sed_in_place(args):
    """True when a sed argument list edits files in place (-i, -Ei, -ni, -i.bak, --in-place)."""
    for a in args:
        if a == "--":
            break
        if a.startswith("--"):
            if a.startswith("--in-place"):
                return True
            continue
        if a.startswith("-") and len(a) > 1:
            for ch in a[1:]:
                if ch == "i":
                    return True
                if ch in "efl":  # takes the rest of the cluster (or the next word) as its argument
                    break
    return False


def set_options_only(args):
    """True when a bash `set` list only sets options (set -e, set -euo pipefail, set +x)."""
    i = 0
    while i < len(args):
        a = args[i]
        if len(a) < 2 or a[0] not in "-+":
            return False
        i += 2 if "o" in a[1:] else 1  # -o takes the option name as the next word
    return True


def _bash_positional(args):
    out, end = [], False
    for a in args:
        if not end and a == "--":
            end = True
            continue
        if not end and a.startswith("-") and a != "-":
            continue
        out.append(a)
    return out


def _t_flag(a):
    """True for cp/mv/ln -t and --target-directory (the destination is then not the last word)."""
    if a.startswith("--"):
        return a.lower().startswith("--target")
    return a.startswith("-") and "t" in a[1:]


def _ps_args(args):
    """(ordered path values, destination or None, named path values, positional values) of a
    PowerShell argument list. Switches are skipped; value parameters are not paths."""
    ordered, named, pos, dest = [], [], [], None
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if a.startswith("-") and len(a) > 1:
            name, val = a[1:], None
            if ":" in name:
                name, val = name.split(":", 1)
            key = name.lower()
            if key in PS_PATH_PARAMS or key in PS_DEST_PARAMS or key in PS_VALUE_PARAMS:
                if val is None:
                    val = args[i] if i < len(args) else ""
                    i += 1
                if key in PS_PATH_PARAMS:
                    named.append(val)
                    ordered.append(val)
                elif key in PS_DEST_PARAMS:
                    dest = val
            continue
        pos.append(a)
        ordered.append(a)
    return ordered, dest, named, pos


def has_subst(text, shell):
    return "$(" in text or (shell == "bash" and "`" in text)


def fileop_paths(w, p, shell):
    """The paths a file operation touches, or None when w is not a file operation.
    An empty string stands for a path that cannot be resolved."""
    name = w[0].lower()
    if name not in FILE_OPS_ALL and name not in FILE_OPS_DEST:
        return None
    if has_subst(p, shell):
        return [""]
    args = w[1:]
    if shell == "bash":
        if name in ("cp", "mv", "ln") and any(_t_flag(a) for a in args):
            return [""]
        ordered, dest = _bash_positional(args), None
    else:
        ordered, dest, _, _ = _ps_args(args)
    if name in FILE_OPS_DEST:
        if dest is not None:
            return [dest]
        return ordered[-1:] or ["."]
    out = ordered + ([dest] if dest is not None else [])
    return out or ["."]


def write_targets(w, shell):
    """Files a command writes to through its arguments: tee, curl -o, and PowerShell
    Out-File / Set-Content / Add-Content. An empty string stands for an unresolvable target."""
    if not w:
        return []
    name = w[0].lower()
    args = w[1:]
    if name == "curl":
        return curl_parse(w)[1]
    if name == "tee" and shell == "bash":
        return _bash_positional(args)
    if name == "tee" or (shell == "powershell" and name in PS_WRITERS):
        _, _, named, pos = _ps_args(args)
        if named:
            return named[:1]
        return pos[:1] or [""]
    return []


def parse_assignment(text, shell, env):
    """(NAME, value) when text is only one simple literal assignment, else None. Bash: NAME=word
    with no operators. PowerShell: $name = 'literal' or "literal". PowerShell names are lower-cased."""
    t = text.strip()
    if has_subst(t, shell):
        return None
    if shell == "bash":
        m = BASH_ASSIGN.fullmatch(t)
        if not m or re.search(r"[<>|&();`]", m.group(2)):
            return None
        ws = words(expand_vars(m.group(2), env, shell))
        if len(ws) > 1:
            return None
        return m.group(1), (ws[0] if ws else "")
    m = PS_ASSIGN.fullmatch(t)
    if not m:
        return None
    rest = m.group(2).strip()
    if len(rest) >= 2 and rest[0] == "'" and rest[-1] == "'" and "'" not in rest[1:-1]:
        value = rest[1:-1]
    elif len(rest) >= 2 and rest[0] == '"' and rest[-1] == '"' and '"' not in rest[1:-1]:
        value = expand_vars(rest[1:-1], env, shell)
    else:
        return None
    return m.group(1).lower(), value


def expand_vars(text, env, shell):
    """text with known NAME, ${NAME} and quoted references replaced by their values.
    Single-quoted text is literal; unknown references are left in place."""
    if "$" not in text:
        return text
    out, q, i, n = [], None, 0, len(text)
    while i < n:
        c = text[i]
        if q == "'":
            out.append(c)
            if c == "'":
                q = None
            i += 1
            continue
        if shell == "bash" and c == "\\" and i + 1 < n:
            out.append(text[i:i + 2])
            i += 2
            continue
        if c in "'\"":
            if q == c:
                q = None
            elif q is None:
                q = c
            out.append(c)
            i += 1
            continue
        if c == "$":
            m = VAR_REF.match(text, i)
            if m:
                key = m.group(1) or m.group(2)
                if shell != "bash":
                    key = key.lower()
                if key in env:
                    out.append(env[key])
                    i = m.end()
                    continue
        out.append(c)
        i += 1
    return "".join(out)


def strip_keywords(text, shell):
    """text without leading control keywords. A statement that is only done, fi, }, {, esac
    or a `for NAME in` head (with no command substitution) becomes ''."""
    t = text.strip()
    while True:
        if t.lower() in STRUCT_WORDS:
            return ""
        if FOR_HEAD.match(t) and not has_subst(t, shell):
            return ""
        m = re.match(r"([A-Za-z_]+)(\s+|$)", t)
        if not m or m.group(1).lower() not in CONTROL_WORDS:
            return t
        t = t[m.end():].lstrip()


def cmd_read_only(w, shell):
    name = w[0].lower()
    args = w[1:]
    low = [a.lower() for a in args]
    if name in GENERIC_READ or name in PS_READ or name in READ_EXTRA:
        if name == "find":
            return not any(a in ("-delete", "-exec", "-execdir", "-ok", "-okdir") for a in low)
        if name == "sort":
            return not any(a.startswith("-o") or a.startswith("--output") for a in low)
        if name == "date":
            return not any(a.startswith("-s") or a.startswith("--set") for a in low)
        if name == "env":
            return all("=" in a and not a.startswith("-") for a in args)
        return True
    if name == "tee":
        return True  # its file arguments are output targets, checked by write_targets
    if shell == "powershell" and name in PS_WRITERS:
        return True  # likewise
    if name == "git":
        return git_read_only(w)
    if name == "gh":
        return not gh_writes_local(w)
    if name == "curl":
        return curl_parse(w)[0]
    if name == "sed":
        return not sed_in_place(args)
    if name == "set":
        return shell == "bash" and set_options_only(args)
    return False


def _path_block(t, cur, cwd):
    """None if path t resolves into an allowed location (relative to cur), else the blocking
    path, or 'an unresolved path'."""
    d = resolve_dir(t, cur) if t else None
    if d is None:
        return "an unresolved path"
    return None if allowed_dir(d, cwd) else d


def _out_block(t, cur, cwd):
    """None if an output target may be written, else what blocked it. Null devices are allowed."""
    if t.lower() in NULL_TARGETS:
        return None
    return _path_block(t, cur, cwd)


def check_shell(cmd, shell, cwd):
    """None if the command may run here; otherwise the path or directory that blocked it."""
    allow = load_allow_commands()
    cur, env = cwd, {}
    for stmt in statements(strip_heredocs(cmd, shell), shell):
        parts = list(stmt)
        parts[0] = strip_keywords(parts[0], shell)
        if len(parts) == 1:
            asg = parse_assignment(parts[0], shell, env)
            if asg is not None:
                env[asg[0]] = asg[1]
                continue
        parts = [expand_vars(p, env, shell) for p in parts]
        for p in parts:
            w = words(p)
            for t in redirect_targets(p) + write_targets(w, shell):
                blk = _out_block(t, cur, cwd)
                if blk is not None:
                    return blk
        if len(parts) == 1:
            w = words(parts[0])
            if w and w[0].lower() in NAV_CMDS:
                cur = navigate(w[0].lower(), w[1:], cur)
                continue
        needs_cwd, target = False, cur
        for p in parts:
            w = words(p)
            if not w:
                continue
            paths = fileop_paths(w, p, shell)
            if paths is not None:
                for path in paths:
                    blk = _path_block(path, cur, cwd)
                    if blk is not None:
                        return blk
                continue
            # Placed after the file-operation block, which always continues, so an allowlisted
            # pattern can never exempt a file operation (rule 2).
            if allow and not has_subst(p, shell) and "`" not in p and any(
                    fnmatch.fnmatchcase(" ".join(w), pat) for pat in allow):
                continue
            if has_subst(p, shell) or not cmd_read_only(w, shell):
                needs_cwd = True
                if w[0].lower() == "git" and not git_read_only(w):
                    cdir = git_split(w)[0]
                    if cdir is not None:
                        target = resolve_dir(cdir, cur)
                        if target is None:
                            return "an unresolved directory"
        if needs_cwd:
            if target is None:
                return "an unresolved directory"
            if not allowed_dir(target, cwd):
                return target
    return None


# ---------- decision and output ----------

def decide(p):
    """None to allow, else (what was blocked, tool, detail)."""
    tool = str(p.get("tool_name") or "")
    ti = p.get("tool_input") or {}
    cwd = msys_path(p.get("cwd") or os.getcwd())
    if tool in WRITE_TOOLS:
        path = ti.get(WRITE_TOOLS[tool])
        if not isinstance(path, str) or not path:
            return ("a write with no target path", tool, repr(path))
        target = os.path.join(cwd, msys_path(path))
        if allowed_dir(target, cwd):
            return None
        return ("path %s is not in one" % target, tool, target)
    if tool in SHELL_TOOLS:
        cmd = ti.get("command")
        if not isinstance(cmd, str):
            return None
        blocked = check_shell(cmd, "bash" if tool == "Bash" else "powershell", cwd)
        if blocked is not None:
            return ("shell command in %s" % blocked, tool, cmd)
    return None


def deny(what, agent_type, tool, detail):
    try:
        line = detail[:200].replace("\r", " ").replace("\n", " ")
        with open(LOG, "a", encoding="utf-8", errors="replace") as f:
            f.write("%s %s %s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), agent_type, tool, line))
    except Exception:
        pass
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": REASON % what}}))
    sys.exit(0)


def main():
    try:
        p = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
    except Exception:
        return  # fail open
    if not isinstance(p, dict) or not p.get("agent_id"):
        return  # main agent: not this hook's job
    if os.environ.get("CLAUDE_SUBAGENT_ANYWHERE") == "1" or os.environ.get("CLAUDE_MAIN_WRITE") == "1":
        return
    if env_exempt():
        return  # session matched by exempt_env in the local harness config: not restricted
    agent_type = str(p.get("agent_type") or "")
    if agent_type in EXEMPT_AGENT_TYPES or agent_type.startswith(("pr-reviewer-", "diagnoser-")):
        return
    try:
        verdict = decide(p)
    except Exception:
        return  # fail open
    if verdict:
        what, tool, detail = verdict
        deny(what, agent_type, tool, detail)


if __name__ == "__main__":
    main()
