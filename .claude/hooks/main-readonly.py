#!/usr/bin/env python3
"""PreToolUse guard: main agent plans + dispatches; subagents write.

Main agent == hook payload without `agent_id` (verified by probe).
Denies Bash/Monitor/PowerShell and file-mutating tools for the main agent, except
writes to a small set of allowed paths. Fails open on unparseable input.
Disable for the main session with CLAUDE_MAIN_WRITE=1 (this does not lift reviewer rules).
Reviewer and diagnoser subagents (payload `agent_id` set and `agent_type` matching REVIEWER_GLOBS:
opus-reviewer, pr-reviewer-*, diagnoser-*) get the same read-only rules as main, and may start a shell command with `cd <path> &&`
(or a plain `cd <path>`).
Reviewer writes are limited to temp roots (the job tmp dir and /tmp/claude-UID): no plans dirs, no memory dir.
Other subagents are unrestricted.
Machine-local settings come from one config file (not committed): the path in
CLAUDE_HARNESS_CONFIG, or ~/.claude/hooks/harness.local.json. See README.md, "Local config".
  readonly_scripts  extra read-only scripts (e.g. a project status check) are allowed:
                    [{"pattern": "<regex>", "flags": ["--json", ...]}]
  exempt_env        {"VAR": ["glob", ...]}: a session is exempt (this hook does not
                    restrict it) when some VAR is non-empty and matches one glob.
  main_allow_commands  ["glob", ...]: extra single simple commands allowed for the MAIN session only
                    (never reviewers). A glob (fnmatch.fnmatchcase) must match the command's words joined
                    by single spaces, quotes removed. BAD_CHARS, the '$'/'{' rule, the redirect rule and
                    BANNED_WORDS still apply, and a command with an unquoted pipe never matches.
A missing, unreadable or malformed file, or a malformed entry, means no scripts, no exemptions
and no extra commands. Globs use fnmatch.fnmatchcase, so matching is case-sensitive.

Shell rules (Bash): the command must pass a read-only allowlist. Quoting follows bash. Single quotes are
fully literal, so nothing inside them counts. Inside double quotes only '$' and backtick count (they expand);
';', '&', '|', '<', '>', '{' and '}' are literal there. Outside quotes a backslash makes the next character
literal, but '$', backtick and '{' are still refused. Newline and CR are refused everywhere. The command is
split at unquoted '&&', '||' and ';', and every non-empty part must pass the pipeline checks below. For the main session only, a part the
allowlist refuses may still pass when it is one simple command matching main_allow_commands, or runs a
script from a Claude job tmp dir (is_job_script_command: bash/sh/py/python/powershell with a path under
~/.claude/jobs/<id>/tmp/ or $CLAUDE_JOB_DIR/tmp/). A lone '&'
(background) is refused. The redirects 2>/dev/null, >/dev/null, 2>&1 and &>/dev/null are allowed when unquoted
and delimited, and are stripped before the check; any other '<' or '>' is refused. Shell comments are off
(a mid-word '#' is literal, as in bash). Each command's banned
options (SIMPLE_BAD_FLAGS) are matched option-aware: a long name is refused when any banned long
option starts with it (GNU prefixes), and a short cluster when it holds a banned letter. sed is
print/delete/substitute only, with no -i, -f, -s or extra scripts. git --output and --ext-diff
are refused on every subcommand. rg --pre and --hostname-bin are refused.

Write rules: a write is allowed only under an allowed root (cwd/.claude/plans, ~/.claude/plans,
the job tmp dir, /tmp/claude-UID, or the memory dir) when the lexical path and the realpath are
both under the root, and no root guard path is a symlink. ~/.claude/hooks, settings.json and
settings.local.json are never writable by the main session, whatever the roots say.
"""
import fnmatch, json, os, posixpath, random, re, shlex, sys, time

HOME = os.path.expanduser("~")
WRITE_TOOLS = {"Write": "file_path", "Edit": "file_path",
               "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
SHELL_TOOLS = {"Bash", "Monitor", "PowerShell"}  # Monitor runs a free-form shell command too
EXECUTOR_SLUGS = ["qorvax-tel", "zyrene-vosk", "thal-ummir", "kreeva-osh", "vexlor-ain",
                  "nyxa-prell", "oomrak-zhee", "xilth-varo", "juun-ketrax", "plimm-oraxi"]


def hint():
    slug = random.choice(EXECUTOR_SLUGS)
    return ('Main agent is read-only: delegate this to a subagent, e.g. '
            'Agent(subagent_type="%s", isolation="worktree", prompt=<self-contained task + short report format>); '
            'the task must be in_progress with owner equal to that executor name (pool: %s). '
            'Main may only write plans (.claude/plans/), memory files, and job/scratch tmp dirs.'
            % (slug, ", ".join(EXECUTOR_SLUGS)))


def _real(path):
    """Canonical comparison form: symlinks/junctions resolved, case folded on Windows."""
    return os.path.normcase(os.path.realpath(path))


def _lexical(path):
    """Typed form: absolute and normalized, symlinks NOT resolved."""
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _under(p, root):
    return p == root or p.startswith(root + os.sep)


def _root_candidates(cwd, tmp_only=False):
    """[(root, guard)]: root is an unresolved directory; the root counts only when no guard
    path is a symlink. The cwd and HOME plans roots are guarded against symlinked .claude,
    .claude/plans and plans; the job tmp and scratchpad roots are guarded by their own path.
    tmp_only drops the plans roots (reviewers may write to temp roots only)."""
    base = cwd or os.getcwd()
    cwd_claude = os.path.join(base, ".claude")
    home_plans = os.path.join(HOME, ".claude", "plans")
    out = [] if tmp_only else [(os.path.join(cwd_claude, "plans"), [cwd_claude, os.path.join(cwd_claude, "plans")]),
                               (home_plans, [home_plans])]
    job = os.environ.get("CLAUDE_JOB_DIR")
    if job:
        job_tmp = os.path.join(job, "tmp")
        out.append((job_tmp, [job_tmp]))
    if hasattr(os, "getuid"):  # POSIX only: harness scratchpad
        scratch = "/tmp/claude-%d" % os.getuid()
        out.append((scratch, [scratch]))
    return out


def _protected(real):
    """True for paths the main session may never write, whatever the roots say (real is _real'd):
    anything under ~/.claude/hooks, and ~/.claude/settings.json / settings.local.json."""
    if _under(real, _real(os.path.join(HOME, ".claude", "hooks"))):
        return True
    return any(real == _real(os.path.join(HOME, ".claude", name))
               for name in ("settings.json", "settings.local.json"))


def _memory_rel(p, base):
    """Parts after <base>/<one segment>/memory/ when p has that layout with a non-empty file part, else None."""
    if not _under(p, base):
        return None
    parts = p[len(base) + 1:].split(os.sep)
    return parts if len(parts) >= 3 and parts[0] and parts[1] == "memory" and parts[2] else None


def _in_memory_dir(typed, real):
    """True for <HOME>/.claude/projects/<one segment>/memory/<something...>. The layout must hold
    for the typed path (lexical root) and for the realpath (resolved root), and the memory dir
    itself must not be a symlink."""
    proj_typed = _lexical(os.path.join(HOME, ".claude", "projects"))
    proj_real = _real(os.path.join(HOME, ".claude", "projects"))
    t_parts = _memory_rel(typed, proj_typed)
    if t_parts is None or _memory_rel(real, proj_real) is None:
        return False
    return not os.path.islink(os.path.join(proj_typed, t_parts[0], "memory"))


def path_allowed(path, cwd, tmp_only=False):
    """A write is allowed only when the target is under an allowed root AND all of these hold:
    (a) the lexical (normalized, unresolved) target is under the lexical root;
    (b) the realpath of the target is under the realpath of the root;
    (c) no guard path of that root is a symlink;
    and the target is not a protected path (see _protected), checked on its realpath.
    tmp_only (reviewers) allows only the temp roots: no plans dirs and no memory dir."""
    if not path or not isinstance(path, str):
        return False
    full = os.path.join(cwd or os.getcwd(), path)
    typed, real = _lexical(full), _real(full)
    if _protected(real):
        return False
    for root, guard in _root_candidates(cwd, tmp_only):
        if any(os.path.islink(g) for g in guard):
            continue
        if _under(typed, _lexical(root)) and _under(real, _real(root)):
            return True
    return False if tmp_only else _in_memory_dir(typed, real)


def deny(reason, tool, detail):
    try:
        log = os.path.join(HOME, ".claude", "hooks", "main-readonly.log")
        with open(log, "a", encoding="utf-8", errors="replace") as f:
            f.write("%s DENY %s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), tool, detail[:200]))
    except Exception:
        pass
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason}}))
    sys.exit(0)


# ---- read-only shell allowlist (pure; tested in test_main_readonly.py) ----

BANNED_WORDS = {"tee", "xargs", "eval", "exec", "source", "sudo"}
BAD_CHARS = ["\n", "\r"]  # refused everywhere, quotes or not
EXPANSION_CHARS = "$`{"  # refused outside single quotes (see _unquoted_hit)
REDIRECT_OK = re.compile(r"\d?>&1|&>/dev/null|\d?>/dev/null")  # the only redirects allowed (when unquoted)
SIMPLE_CMDS = {"cat", "head", "tail", "wc", "ls", "stat", "readlink", "realpath", "file", "du", "df",
               "ps", "ss", "pgrep", "date", "pwd", "whoami", "which", "jq", "grep", "rg", "sort", "uniq",
               "cut", "tr", "column", "basename", "dirname", "nproc", "free", "uptime"}
# per-command flags that write files, run programs or follow forever
SIMPLE_BAD_FLAGS = {
    "tail": {"-f", "-F", "--follow", "--retry"},
    "sort": {"-o", "--output", "-T", "--temporary-directory", "--compress-program"},
    "rg": {"--pre", "--pre-glob", "--hostname-bin"},
    "date": {"-s", "--set"},
    "file": {"-C", "--compile"},
    "ss": {"-K", "--kill"},
}
GIT_ALWAYS = {"fetch", "log", "status", "rev-parse", "merge-base", "show", "diff", "ls-remote", "cat-file",
              "for-each-ref", "rev-list", "describe", "shortlog", "blame", "ls-files", "ls-tree", "reflog"}
FIND_BAD = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fls"}
CURL_FLAGS = {"-s", "-S", "-sS", "-I", "-L", "-f", "-sL", "-sI", "--silent", "--head", "--location",
              "--fail", "--compressed"}
# Machine-local config (not committed), shared with task-gate.py:
#   {"readonly_scripts": [{"pattern": "<regex>", "flags": ["--json", "--expected"]}],
#    "exempt_env": {"CLAUDE_LOCAL": ["1"], "VAR": ["glob", ...]},
#    "main_allow_commands": ["glob", ...]}
# A script path must be absolute, with no '..' as typed. A single entry must fullmatch both its
# os.path.normpath and its os.path.realpath, and that entry's flags must permit every given flag.
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


def load_scripts():
    """Return [(compiled_regex, frozenset(flags))] from readonly_scripts. Never raises: a missing,
    unreadable or malformed file yields [], and a bad entry is skipped."""
    entries = _read_config().get("readonly_scripts", [])
    if not isinstance(entries, list):
        return []
    out = []
    for e in entries:
        try:
            pat, flags = e["pattern"], e.get("flags", [])
            if not isinstance(pat, str) or not isinstance(flags, list) or not all(isinstance(x, str) for x in flags):
                continue
            out.append((re.compile(pat), frozenset(flags)))
        except Exception:  # bad regex, non-dict entry, missing pattern
            continue
    return out


SCRIPTS = load_scripts()
ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
SED_ADDR = r"(?:\d+|\$|/(?:\\.|[^/\\])*/)"
SED_SCRIPT = re.compile(r"^\s*(?:" + SED_ADDR + r"(?:,\s*" + SED_ADDR + r")?)?\s*!?\s*(?:[pPd=q]|s/(?:\\.|[^/\\])*/(?:\\.|[^/\\])*/[gpI0-9]*)\s*$")


def _prefix_flag(arg, full, minlen):
    """True if arg (before '=') is an abbreviation of the long option `full` (git accepts prefixes)."""
    name = arg.split("=", 1)[0]
    return name.startswith("--") and len(name) >= minlen and full.startswith(name)


def _git_branch(args):
    value_flags = {"--contains", "--no-contains", "--merged", "--no-merged", "--points-at", "--sort", "--format"}
    ok_flags = {"-a", "-r", "-v", "-vv", "-l", "-i", "--list", "--all", "--remotes", "--verbose",
                "--show-current", "--color", "--no-color", "--column", "--no-column", "--no-abbrev",
                "--ignore-case", "--abbrev"} | value_flags
    list_mode = any(a in ("-l", "--list") for a in args)
    i = 0
    pos = []
    while i < len(args):
        a = args[i]
        if a == "--":
            pos += args[i + 1:]
            break
        if a.startswith("-"):
            name = a.split("=", 1)[0]
            if name not in ok_flags and not name.startswith("--abbrev"):
                return False, "git branch flag %s not allowed" % a
            if name in value_flags and "=" not in a and i + 1 < len(args) and not args[i + 1].startswith("-"):
                i += 1
        else:
            pos.append(a)
        i += 1
    if pos and not list_mode:
        return False, "git branch with a positional name (would create a branch)"
    return True, ""


def _git(args):
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] == "-C" and i + 1 < len(args) and not args[i + 1].startswith("-"):
            i += 2
        elif args[i] in ("--no-pager", "-P"):
            i += 1
        else:
            return False, "git global option %s not allowed" % args[i]
    if i >= len(args):
        return False, "git needs a subcommand"
    sub, rest = args[i], args[i + 1:]
    for a in rest:
        if "ext::" in a or a.startswith("--exec") or _prefix_flag(a, "--upload-pack", 4):
            return False, "git argument %s not allowed" % a
        # every subcommand: --output writes a file, --ext-diff runs a program (prefixes count)
        if _prefix_flag(a, "--output", 3) or _prefix_flag(a, "--ext-diff", 5):
            return False, "git %s %s not allowed" % (sub, a)
    if sub in GIT_ALWAYS:
        if sub == "reflog" and any(a in ("expire", "delete") for a in rest):
            return False, "git reflog expire/delete not allowed"
        return True, ""
    if sub == "worktree":
        if rest[:1] == ["list"] and all(a in ("--porcelain", "-v", "--verbose", "-z") for a in rest[1:]):
            return True, ""
        return False, "only git worktree list is allowed"
    if sub == "branch":
        return _git_branch(rest)
    if sub == "stash":
        return (True, "") if rest[:1] == ["list"] else (False, "only git stash list is allowed")
    if sub == "remote":
        return (True, "") if rest in ([], ["-v"]) else (False, "only git remote [-v] is allowed")
    return False, "git %s is not read-only" % sub


def _curl(args):
    urls = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--max-time", "-m"):
            if i + 1 >= len(args) or not re.fullmatch(r"\d+(\.\d+)?", args[i + 1]):
                return False, "curl %s needs a number" % a
            i += 2
            continue
        if re.fullmatch(r"--max-time=\d+(\.\d+)?", a) or a in CURL_FLAGS:
            pass
        elif re.fullmatch(r"-[sSILf]+", a):
            pass
        elif a.startswith("-"):
            return False, "curl flag %s not allowed" % a
        else:
            urls.append(a)
        i += 1
    if len(urls) != 1 or not re.match(r"^https?://", urls[0], re.I):
        return False, "curl needs exactly one http(s) URL"
    return True, ""


def _find(args):
    for a in args:
        if a in FIND_BAD or a.startswith("-fprint"):
            return False, "find %s not allowed" % a
    return True, ""


SED_FLAGS = {"-n", "-E", "-r", "--quiet", "--silent", "--regexp-extended"}


def _sed(args):
    """sed -n only. Every argument is checked: the only options are SED_FLAGS and -e/--expression,
    so -i, -f, -s, --in-place and the like are refused wherever they appear (before or after the
    script). There is exactly one script (the -e value, or else the first operand when there is no
    -e), and it must pass SED_SCRIPT. A second script, including a second -e, is refused."""
    if not args or args[0] != "-n":
        return False, "only sed -n is allowed"
    exprs, operands = [], []
    i = 1
    while i < len(args):
        a = args[i]
        if a in ("-e", "--expression"):
            if i + 1 >= len(args):
                return False, "sed %s needs a script" % a
            exprs.append(args[i + 1])
            i += 2
            continue
        if a in SED_FLAGS:
            pass
        elif a.startswith("-") and a != "-":
            return False, "sed flag %s not allowed (only -n, -E, -r, -e)" % a
        else:
            operands.append(a)
        i += 1
    if len(exprs) > 1:
        return False, "sed: only one script allowed"
    script = exprs[0] if exprs else (operands[0] if operands else None)
    if script is None or not SED_SCRIPT.match(script):
        return False, "sed script not in the simple print/delete/substitute form"
    return True, ""


def _bad_option(cmd, a):
    """True when option argument a of cmd is a banned flag (SIMPLE_BAD_FLAGS) or abbreviates one. GNU getopt
    takes unambiguous long-option prefixes, so a long name is refused when any banned long option
    starts with it (conservative). A single-dash cluster such as -Cz is refused when any letter in
    it is a banned short flag. Operands, '-' and '--' are never refused."""
    if not a.startswith("-") or a in ("-", "--"):
        return False
    banned = SIMPLE_BAD_FLAGS.get(cmd, ())
    if a.startswith("--"):
        name = a.split("=", 1)[0]
        return any(full.startswith(name) for full in banned if full.startswith("--"))
    letters = {full[1] for full in banned if not full.startswith("--")}
    return any(ch in letters for ch in a[1:])


def _simple(cmd, args):
    for a in args:
        if _bad_option(cmd, a):
            return False, "%s %s not allowed" % (cmd, a)
    if cmd == "uniq" and len([a for a in args if not a.startswith("-")]) > 1:
        return False, "uniq with an output file not allowed"
    return True, ""


def _script_check(path, flags):
    """None if path names no configured script; else (allowed, reason). A script is allowed only when:
    (a) path is absolute; (b) no component of path as typed is '..'; and (c) a single configured
    entry fullmatches BOTH os.path.normpath(path) and os.path.realpath(path) AND its flags permit
    every given flag. Requiring one entry to match both paths stops a symlink from making a typed
    name match one entry while the resolved file is governed by another entry's flags."""
    norm = os.path.normpath(path)
    if not any(rx.fullmatch(norm) for rx, _ in SCRIPTS):
        return None
    if not os.path.isabs(path):
        return False, "script path %s must be absolute" % path
    if ".." in path.split("/"):
        return False, "script path %s contains '..'" % path
    real = os.path.realpath(path)
    both = [allowed for rx, allowed in SCRIPTS if rx.fullmatch(norm) and rx.fullmatch(real)]
    if not both:
        return False, "script path %s resolves to %s, which no single configured entry matches" % (path, real)
    if any(all(f in allowed for f in flags) for allowed in both):
        return True, ""
    return False, "script flags not allowed"


def _segment(tokens):
    if not tokens:
        return False, "empty pipeline segment"
    if ENV_RE.match(tokens[0]):
        if tokens[0] != "GIT_PAGER=cat":
            return False, "env prefix %s not allowed" % tokens[0]
        tokens = tokens[1:]
        if not tokens:
            return False, "empty pipeline segment"
    cmd, args = tokens[0], tokens[1:]
    if cmd in ("bash", "sh"):
        if not args:
            return False, "%s needs a script path" % cmd
        res = _script_check(args[0], args[1:])
        if res and res[0]:
            return True, ""
        return False, "%s only runs configured read-only scripts" % cmd
    res = _script_check(cmd, args)
    if res is not None:
        return res
    if cmd == "git":
        return _git(args)
    if cmd == "curl":
        return _curl(args)
    if cmd == "find":
        return _find(args)
    if cmd == "sed":
        return _sed(args)
    if cmd in SIMPLE_CMDS:
        return _simple(cmd, args)
    return False, "command %s is not in the read-only allowlist" % cmd


DQ_LIVE = "$`"  # the only characters that still expand inside double quotes


def _unquoted_hit(cmd, chars):
    """True if a character in chars is live for bash. Outside double and single quotes a listed character
    is refused even when backslash-escaped. Inside double quotes only DQ_LIVE characters count, so '{' and
    the operators are literal there. Inside single quotes nothing counts. Outside quotes a backslash
    escapes the next character, so an escaped quote does not open a quote."""
    state = None  # None (unquoted), "'" (single), or '"' (double)
    i = 0
    while i < len(cmd):
        c = cmd[i]
        nxt = cmd[i + 1] if i + 1 < len(cmd) else ""
        if state == "'":
            if c == "'":
                state = None
        elif state == '"':
            if c in DQ_LIVE and c in chars:
                return True
            if c == "\\":
                if nxt and nxt in DQ_LIVE and nxt in chars:
                    return True
                i += 1  # skip the escaped character
            elif c == '"':
                state = None
        elif c == "\\":
            if nxt and nxt in chars:
                return True
            i += 1  # skip the escaped character
        elif c in chars:
            return True
        elif c in "'\"":
            state = c
        i += 1
    return False


def _live_indices(cmd):
    """Indices of characters bash reads as syntax: not inside single or double quotes, and not
    backslash-escaped outside quotes (an escaped character is an ordinary word character)."""
    out = []
    state = None
    i = 0
    while i < len(cmd):
        c = cmd[i]
        if state == "'":
            if c == "'":
                state = None
        elif state == '"':
            if c == '"':
                state = None
            elif c == "\\":
                i += 1
        elif c == "\\":
            i += 1
        elif c in "'\"":
            state = c
        else:
            out.append(i)
        i += 1
    return out


def _strip_redirects(cmd):
    """(cmd with allowed redirects blanked, "") or (None, reason). A redirect is allowed only when it is
    one of REDIRECT_OK, unquoted, not glued to a preceding < > &, and followed by whitespace, an operator
    or the end (so /dev/nullx is not taken for /dev/null). Any other live < or > is refused. Each allowed
    redirect is replaced by a space so that neighbouring words do not merge."""
    live = set(_live_indices(cmd))
    spans = []
    i = 0
    while i < len(cmd):
        if i in live:
            m = REDIRECT_OK.match(cmd, i)
            if (m and (m.end() == len(cmd) or cmd[m.end()] in " \t|;&")
                    and (i == 0 or cmd[i - 1] not in "<>&")):
                spans.append((i, m.end()))
                i = m.end()
                continue
            if cmd[i] in "<>":
                return None, "redirection not allowed (only 2>/dev/null, >/dev/null, 2>&1, &>/dev/null)"
        i += 1
    out, pos = [], 0
    for a, b in spans:
        out.append(cmd[pos:a])
        out.append(" ")
        pos = b
    out.append(cmd[pos:])
    return "".join(out), ""


def _split_chains(cmd):
    """(parts, "") split at unquoted '&&', '||' and ';' (blank parts dropped), or (None, reason) when an
    unquoted lone '&' (background) is present."""
    live = set(_live_indices(cmd))
    parts, start, i = [], 0, 0
    while i < len(cmd):
        if i in live:
            if cmd[i:i + 2] in ("&&", "||") and i + 1 in live:
                parts.append(cmd[start:i])
                i += 2
                start = i
                continue
            if cmd[i] == ";":
                parts.append(cmd[start:i])
                i += 1
                start = i
                continue
            if cmd[i] == "&":
                return None, "background & not allowed"
        i += 1
    parts.append(cmd[start:])
    return [p.strip() for p in parts if p.strip()], ""


def _pipe_stages(cmd):
    """Split at unquoted, unescaped '|' only (the same quote tracking as _split_chains). A quoted or
    backslash-escaped '|' stays inside its stage as an ordinary argument."""
    live = set(_live_indices(cmd))
    stages, start = [], 0
    for i, c in enumerate(cmd):
        if c == "|" and i in live:
            stages.append(cmd[start:i])
            start = i + 1
    stages.append(cmd[start:])
    return stages


def _pipeline(cmd):
    """Allowlist check for one command or pipeline (no unquoted &&, || or ;). Returns (allowed, reason)."""
    segs = []
    for stage in _pipe_stages(cmd):
        try:
            # comments off: bash treats a mid-word '#' as literal, shlex would drop the rest of the line
            lex = shlex.shlex(stage, posix=True)
            lex.whitespace_split = True
            lex.commenters = ""
            tokens = list(lex)
        except ValueError as e:
            return False, "unparseable command: %s" % e
        if any(t in BANNED_WORDS for t in tokens):
            return False, "forbidden word in command"
        segs.append(tokens)
    for seg in segs:
        ok, why = _segment(seg)
        if not ok:
            return False, why
    return True, ""


def is_readonly_command(cmd, allow_extra=False):
    """Strict allowlist for read-only shell commands. Returns (allowed, reason). Newlines are refused
    everywhere; expansions are refused outside single quotes; allowed redirects are blanked; the command
    is split at unquoted && || ; and every part must pass _pipeline. allow_extra (main session only, never
    reviewers) also lets a refused part pass via _extra_allowed (main_allow_commands or a job-tmp script)."""
    if not isinstance(cmd, str) or not cmd.strip():
        return False, "empty command"
    for bad in BAD_CHARS:
        if bad in cmd:
            return False, "newline or carriage return not allowed"
    if _unquoted_hit(cmd, EXPANSION_CHARS):
        return False, "shell expansion ($, backtick or {) not allowed outside single quotes"
    stripped, why = _strip_redirects(cmd)
    if stripped is None:
        return False, why
    parts, why = _split_chains(stripped)
    if parts is None:
        return False, why
    if not parts:
        return False, "empty command"
    for part in parts:
        ok, why = _pipeline(part)
        if not ok and allow_extra and _extra_allowed(part):
            ok, why = True, ""
        if not ok:
            return False, why
    return True, ""


def load_main_allow_commands():
    """fnmatch globs from main_allow_commands. Never raises: a missing, unreadable or malformed file,
    or a value that is not a list, yields []; non-string and blank entries are skipped."""
    raw = _read_config().get("main_allow_commands", [])
    if not isinstance(raw, list):
        return []
    return [p for p in raw if isinstance(p, str) and p.strip()]


def _pattern_allowed(tokens):
    """True when the words of one simple command, joined by single spaces, fullmatch a main_allow_commands glob."""
    line = " ".join(tokens)
    return any(fnmatch.fnmatchcase(line, p) for p in load_main_allow_commands())


JOB_SHELLS = {"bash", "sh", "py", "python", "python3", "powershell", "powershell.exe", "pwsh"}


def _slash(p):
    """The path with Windows separators folded to '/'."""
    return p.replace("\\", "/")


def _key(p):
    """Normalized comparison key: forward slashes, case-folded on Windows (case-insensitive file system)."""
    k = posixpath.normpath(_slash(p))
    return k.lower() if os.name == "nt" else k


def _real_key(p):
    """_key of the resolved path (symlinks and junctions followed)."""
    return _key(os.path.realpath(p))


def _under_job_dir(p, base):
    """True when the key p is base/<one segment>/tmp/<file...>; base is a key with no trailing slash."""
    if not p.startswith(base + "/"):
        return False
    parts = p[len(base) + 1:].split("/")
    return len(parts) >= 3 and parts[0] != "" and parts[1] == "tmp" and parts[2] != ""


def _under_tree(p, base):
    """True when the key p lies strictly under the directory key base."""
    return p.startswith(base + "/")


def _job_tmp_script(path):
    """True when path (as typed) is an absolute script under <HOME>/.claude/jobs/<one segment>/tmp/ or
    under $CLAUDE_JOB_DIR/tmp/. Both the typed form (normalized, unresolved) and the realpath must
    qualify. Relative paths, '..' components (either separator) and glob characters are refused. Off
    Windows a backslash is refused too: the path is the word bash runs, and bash treats '\\' as an escape."""
    if not (path.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", path)):
        return False
    if os.name != "nt" and "\\" in path:
        return False
    if ".." in re.split(r"[\\/]", path) or re.search(r"[*?\[]", path):
        return False
    typed, real = _key(path), _real_key(path)
    jobs = os.path.join(HOME, ".claude", "jobs")
    if _under_job_dir(typed, _key(jobs)) and _under_job_dir(real, _real_key(jobs)):
        return True
    job = os.environ.get("CLAUDE_JOB_DIR")
    if job:
        tmp = os.path.join(job, "tmp")
        if _under_tree(typed, _key(tmp)) and _under_tree(real, _real_key(tmp)):
            return True
    return False


def _words_literal(cmd):
    """Words of cmd as the script path bash will receive, or None when unparseable. Off Windows these are
    posix-shlex words (quotes removed, backslash escapes applied), the same words bash passes on, so the
    '..' and job-tmp checks see what runs. On Windows quotes are stripped and backslashes are kept literal,
    since they are path separators there."""
    posix = os.name != "nt"
    try:
        lex = shlex.shlex(cmd, posix=posix)
        lex.whitespace_split = True
        lex.commenters = ""
        words = list(lex)
    except ValueError:
        return None
    if posix:
        return words
    return [w[1:-1] if len(w) >= 2 and w[0] == w[-1] and w[0] in "'\"" else w for w in words]


def is_job_script_command(cmd):
    """True when cmd is one simple command that runs a script from a Claude job tmp dir:
      powershell | powershell.exe | pwsh  [-NoProfile] [-NonInteractive] [-ExecutionPolicy <word>] -File <path> [args...]
      bash | sh  <path> [args...]
      py  [-2 | -3 | -3.12 | -3-64 ...]  [-I | -u] <path> [args...]
      python | python3  [-I | -u] <path> [args...]
    The name is matched case-insensitively. The path must pass _job_tmp_script. Arguments after the
    path are not checked. The caller has already run the global checks and checked that cmd is one command."""
    words = _words_literal(cmd)
    if not words:
        return False
    name, args = words[0].lower(), words[1:]
    if name not in JOB_SHELLS:
        return False
    if name in ("powershell", "powershell.exe", "pwsh"):
        i = 0
        while i < len(args):
            opt = args[i].lower()
            if opt in ("-noprofile", "-noninteractive"):
                i += 1
            elif opt == "-executionpolicy" and i + 1 < len(args) and not args[i + 1].startswith("-"):
                i += 2
            else:
                break
        if i + 1 >= len(args) or args[i].lower() != "-file":
            return False
        path = args[i + 1]
    elif name in ("python", "python3"):
        i = 0
        while i < len(args) and args[i] in ("-I", "-u"):
            i += 1
        if i >= len(args):
            return False
        path = args[i]
    elif name == "py":
        i = 0
        while i < len(args) and re.match(r"^-[23](\.\d+)?(-(32|64))?$", args[i]):
            i += 1
        while i < len(args) and args[i] in ("-I", "-u"):
            i += 1
        if i >= len(args):
            return False
        path = args[i]
    else:
        if not args:
            return False
        path = args[0]
    return _job_tmp_script(path)


def _extra_allowed(cmd):
    """True when one command part that the built-in allowlist refused is allowed for the main session: it is
    one simple command (no unquoted pipe) that matches main_allow_commands, or runs a job-tmp script
    (is_job_script_command). BANNED_WORDS still refuse it. The caller has run the global checks."""
    if len(_pipe_stages(cmd)) != 1:
        return False
    try:
        lex = shlex.shlex(cmd, posix=True)
        lex.whitespace_split = True
        lex.commenters = ""
        tokens = list(lex)
    except ValueError:
        return False
    if not tokens or any(t in BANNED_WORDS for t in tokens):
        return False
    return _pattern_allowed(tokens) or is_job_script_command(cmd)


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


def env_exempt():
    """True when some exempt_env variable is non-empty and matches one of its globs."""
    for var, pats in load_exempt_env().items():
        val = os.environ.get(var, "")
        if val and any(fnmatch.fnmatchcase(val, p) for p in pats):
            return True
    return False


# Subagents whose payload agent_type matches one of these globs are reviewers: they get the main
# agent's read-only rules (plus `cd <path> &&`). Same globs as REVIEWER_GLOBS in agent-budget.py.
REVIEWER_GLOBS = ("pr-reviewer-*", "opus-reviewer", "diagnoser-*")
REVIEWER_READ_ONLY = "Reviewer and diagnoser agents are read-only: report findings, never edit/commit/push."
# A leading `cd <path>` (plain, or followed by `&&`). The path is one bare word or one quoted
# word; quotes, $, backticks, braces and shell operators are excluded so the match stays simple.
# Reviewers only: plain `gh pr view|diff|checks` with bare words and options (no quotes, pipes or redirects).
REVIEWER_GH = re.compile(r"gh pr (?:view|diff|checks)(?: [A-Za-z0-9_./:#@=,-]+)*")
_CD_PREFIX = re.compile(r"""\s*cd\s+('[^']*'|"[^"$`]*"|[^\s;&|<>$`{}'"\\]+)\s*(?:&&(.*))?\Z""", re.S)


def is_reviewer(agent_type):
    """True when a subagent's agent_type names a PR reviewer or diagnoser ('opus-reviewer', 'pr-reviewer-*', 'diagnoser-*')."""
    return isinstance(agent_type, str) and any(fnmatch.fnmatchcase(agent_type, g) for g in REVIEWER_GLOBS)


def strip_reviewer_cd(cmd):
    """Reviewers only: drop a leading `cd <path> &&`. Returns the rest of the command, or None for a
    plain `cd <path>`. Anything else (or a newline anywhere) is returned unchanged, so the normal
    read-only check still refuses it. A bare `cd <path> &&` leaves blank text, which is refused."""
    if not isinstance(cmd, str) or any(bad in cmd for bad in BAD_CHARS):
        return cmd
    m = _CD_PREFIX.match(cmd)
    if not m:
        return cmd
    if m.group(2) is None:
        return None
    return m.group(2).strip() or " "


def main():
    try:
        p = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
        tool = p.get("tool_name", "")
        ti = p.get("tool_input") or {}
        cwd = p.get("cwd")
    except Exception:
        return  # fail open
    reviewer = bool(p.get("agent_id")) and is_reviewer(p.get("agent_type"))
    if not reviewer:
        if os.environ.get("CLAUDE_MAIN_WRITE") == "1":
            return  # kill switch: main session only; reviewers stay read-only
        if env_exempt():
            return  # session matched by exempt_env in the local harness config: not restricted
        if p.get("agent_id"):
            return  # subagent (not a reviewer): unrestricted
    if tool in SHELL_TOOLS:
        why = ""
        if tool in ("Bash", "Monitor"):  # same read-only check for the free-form command of both
            try:
                cmd = ti.get("command", "")
                if reviewer:
                    cmd = strip_reviewer_cd(cmd)
                    if cmd is None:
                        return  # plain `cd <path>`: changes nothing on disk
                    if REVIEWER_GH.fullmatch(cmd):
                        return  # read-only PR views for the review itself
                ok, why = is_readonly_command(cmd, allow_extra=not reviewer)
            except Exception:
                ok, why = False, "internal error"
            if ok:
                return
        if reviewer:
            deny("Shell is disabled for reviewer agents. " + REVIEWER_READ_ONLY + " "
                 "Read-only commands (git diff/log/show, gh pr view, cat/grep/ls, cd <path> && ...) are allowed."
                 + (" Rejected because: " + why if why else ""), tool, str(ti.get("command", "")))
        deny("Shell is disabled for the main agent (no creative edits via shell). "
             "Read-only commands (git log/status/diff…, curl -s URL, cat/grep/ls, scripts listed in the local harness config) are allowed. "
             + hint() + (" Rejected because: " + why if why else ""), tool, str(ti.get("command", "")))
    if tool in WRITE_TOOLS:
        path = ti.get(WRITE_TOOLS[tool], "")
        if not path_allowed(path, cwd, tmp_only=reviewer):
            if reviewer:
                deny("Write to %s blocked. %s" % (path, REVIEWER_READ_ONLY), tool, str(path))
            deny("Write to %s blocked for the main agent. %s" % (path, hint()), tool, str(path))


if __name__ == "__main__":
    main()
