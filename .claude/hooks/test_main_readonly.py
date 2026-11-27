import atexit, json, os, re, shutil, subprocess, sys, tempfile, unittest
HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main-readonly.py")
HOME = os.path.expanduser("~")

# Machine-local config fixtures. The hook reads CLAUDE_HARNESS_CONFIG at import time.
_CFG_DIR = tempfile.mkdtemp(prefix="mr-cfg-")
atexit.register(shutil.rmtree, _CFG_DIR, True)
CFG = os.path.join(_CFG_DIR, "harness.local.json")
with open(CFG, "w", encoding="utf-8") as _f:
    json.dump({"readonly_scripts": [{"pattern": r"^/home/test/proj/(?:\.claude/worktrees/[^/]+/)?scripts/status\.sh$",
                                     "flags": ["--json", "--expected"]}]}, _f)
EXEMPT_CFG = os.path.join(_CFG_DIR, "exempt.json")
with open(EXEMPT_CFG, "w", encoding="utf-8") as _f:
    json.dump({"exempt_env": {"HARNESS_TEST_ROLE": ["bot", "bot-*"]}}, _f)
NO_CFG = os.path.join(_CFG_DIR, "missing.json")  # never created
BAD_CFG = os.path.join(_CFG_DIR, "bad.json")
with open(BAD_CFG, "w", encoding="utf-8") as _f:
    _f.write("{not json")

def _env(env):
    e = dict(os.environ); e.pop("CLAUDE_MAIN_WRITE", None); e.pop("CLAUDE_JOB_DIR", None); e.pop("HARNESS_TEST_ROLE", None)
    e["CLAUDE_HARNESS_CONFIG"] = NO_CFG  # isolate from any live local config
    e.update(env or {})
    return e

def run(payload, env=None):
    r = subprocess.run([sys.executable, HOOK], input=payload if isinstance(payload, str) else json.dumps(payload),
                       capture_output=True, text=True, env=_env(env))
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] if r.stdout.strip() else "allow"

def reason(payload):
    r = subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                       capture_output=True, text=True, env=_env(None))
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]

class T(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.outside = tempfile.mkdtemp()  # symlink target, outside any allowed root
        os.makedirs(self.d + "/.claude/plans")
        self.link = self.d + "/.claude/plans/evil"
        os.symlink(self.outside, self.link)
    def tearDown(self):
        try:
            os.unlink(self.link)
        except OSError:
            pass
        shutil.rmtree(self.d, ignore_errors=True)
        shutil.rmtree(self.outside, ignore_errors=True)
    def p(self, tool, **ti): return {"tool_name": tool, "tool_input": ti, "cwd": self.d}
    def test_reads_allowed(self):
        for t in ["Read", "Grep", "Glob", "Agent", "WebFetch", "TaskCreate", "mcp__x__y", "EnterWorktree"]:
            self.assertEqual(run(self.p(t, file_path="/etc/passwd")), "allow", t)
    def test_plan_write_allowed(self):
        self.assertEqual(run(self.p("Write", file_path=self.d + "/.claude/plans/x.md")), "allow")
        self.assertEqual(run(self.p("Write", file_path=HOME + "/.claude/plans/x.md")), "allow")
        self.assertEqual(run(self.p("Edit", file_path=".claude/plans/x.md")), "allow")
    def test_other_allowed_paths(self):
        self.assertEqual(run(self.p("Write", file_path=HOME + "/.claude/projects/-home-test/memory/a.md")), "allow")
        self.assertEqual(run(self.p("Write", file_path="/j/tmp/a"), {"CLAUDE_JOB_DIR": "/j"}), "allow")
    @unittest.skipUnless(hasattr(os, "getuid"), "os.getuid is absent on this platform")
    def test_tmp_claude_uid_allowed(self):
        self.assertEqual(run(self.p("Write", file_path="/tmp/claude-%d/s/a.html" % os.getuid())), "allow")
    def test_other_write_denied(self):
        for t, k in [("Write", "file_path"), ("Edit", "file_path"), ("MultiEdit", "file_path"), ("NotebookEdit", "notebook_path")]:
            self.assertEqual(run(self.p(t, **{k: self.d + "/src/a.ts"})), "deny", t)
    def test_escapes_denied(self):
        self.assertEqual(run(self.p("Write", file_path=self.d + "/.claude/plans/../../a")), "deny")
        self.assertEqual(run(self.p("Write", file_path=self.d + "/.claude/plans/evil/passwd")), "deny")
        self.assertEqual(run(self.p("Write", file_path=self.d + "/.claude/plansX/a")), "deny")
        self.assertEqual(run(self.p("Write", file_path=HOME + "/.claude/projects/x/notmemory/a")), "deny")
        self.assertEqual(run(self.p("Write", file_path="")), "deny")
    def test_bash_denied(self):
        for t in ["Bash", "PowerShell"]:
            self.assertEqual(run(self.p(t, command="rm x")), "deny")
    def test_subagent_allowed(self):
        for t, ti in [("Bash", {"command": "rm x"}), ("Write", {"file_path": "/x/a"})]:
            pl = self.p(t, **ti); pl["agent_id"] = "abc"; pl["agent_type"] = "general-purpose"
            self.assertEqual(run(pl), "allow")
    def test_exempt_env_roles(self):
        # exempt_env globs: "bot" exactly, and "bot-*" with a suffix, are exempt; near misses are not.
        ex = {"CLAUDE_HARNESS_CONFIG": EXEMPT_CFG}
        outside = self.d + "/src/a.ts"
        for role in ["bot", "bot-worker"]:
            env = dict(ex, HARNESS_TEST_ROLE=role)
            self.assertEqual(run(self.p("Bash", command="rm x"), env), "allow", role)
            self.assertEqual(run(self.p("Write", file_path=outside), env), "allow", role)
        for role in ["Bot-worker", "xbot-worker", "bots", ""]:
            env = dict(ex, HARNESS_TEST_ROLE=role)
            self.assertEqual(run(self.p("Bash", command="rm x"), env), "deny", repr(role))
            self.assertEqual(run(self.p("Write", file_path=outside), env), "deny", repr(role))
        self.assertEqual(run(self.p("Bash", command="rm x"), ex), "deny", "unset")
    def test_no_config_no_exemption(self):
        # With no config file (or a malformed one) nothing is exempt, even a matching value.
        self.assertEqual(run(self.p("Bash", command="rm x"), {"HARNESS_TEST_ROLE": "bot"}), "deny")
        self.assertEqual(run(self.p("Bash", command="rm x"), {"CLAUDE_HARNESS_CONFIG": BAD_CFG, "HARNESS_TEST_ROLE": "bot"}), "deny")
    def test_failopen_and_killswitch(self):
        self.assertEqual(run("not json"), "allow")
        self.assertEqual(run(""), "allow")
        self.assertEqual(run(self.p("Bash", command="rm x"), {"CLAUDE_MAIN_WRITE": "1"}), "allow")
    def test_deny_reason_names_new_pool(self):
        pool = ["qorvax-tel", "zyrene-vosk", "thal-ummir", "kreeva-osh", "vexlor-ain",
                "nyxa-prell", "oomrak-zhee", "xilth-varo", "juun-ketrax", "plimm-oraxi"]
        for pl in [self.p("Bash", command="rm x"), self.p("Write", file_path=self.d + "/src/a.ts")]:
            msg = reason(pl)
            named = [s for s in pool if 'subagent_type="%s"' % s in msg]
            self.assertEqual(len(named), 1, msg)
            self.assertIn("qorvax-tel", msg)
            self.assertIn("plimm-oraxi", msg)
            self.assertNotIn("pri" + "ya", msg)  # retired executor name


# Import the hook in-process with the test config active; the module reads it at import time.
import importlib.util
_prev_cfg = os.environ.get("CLAUDE_HARNESS_CONFIG")
os.environ["CLAUDE_HARNESS_CONFIG"] = CFG
try:
    _spec = importlib.util.spec_from_file_location("main_readonly", HOOK)
    mr = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(mr)
finally:
    if _prev_cfg is None:
        os.environ.pop("CLAUDE_HARNESS_CONFIG", None)
    else:
        os.environ["CLAUDE_HARNESS_CONFIG"] = _prev_cfg

ALLOWED = [
    "git -C /home/test/proj log --oneline -5",
    "git fetch origin",
    "git merge-base --is-ancestor a b",
    "curl -s https://example.com | grep -o 'assets/index-[^\"]*\\.js'",
    "cat /home/test/x.txt | head -40",
    "ss -ltnp",
    "git worktree list",
    "git branch -r --no-merged origin/master",
    "bash /home/test/proj/scripts/status.sh --json",
    "sed -n 1,20p file",
    "GIT_PAGER=cat git log -3",
    "/home/test/proj/.claude/worktrees/foo/scripts/status.sh --expected",
    "bash /home/test/proj/.claude/worktrees/foo/scripts/status.sh --json --expected",
    "git stash list", "git remote -v", "git branch --list 'feat*'", "git diff --stat HEAD~1",
    "curl -sSL --max-time 5 https://a.b/c", "find . -name '*.ts' | wc -l", "git status | head",
]
REJECTED = [
    "git push origin HEAD:master", "git branch -D x", "git worktree remove x", "git diff --output=/tmp/x",
    "curl -s -o x https://a", "curl -X POST https://a", "cat x > y", "ls; rm -rf /", "ls && rm x",
    "echo $(rm x)", "find . -delete", "sed -i s/a/b/ f", "rm x", "git log | xargs rm", "FOO=1 git log",
    "npx tsx a.ts", "npm publish", "tail -f log", "bash /tmp/evil.sh",
    "ls || rm x", "sleep 1 & rm x", "cat `rm x`", "ls\nrm x", "cat < x", "ls | tee f", "eval ls", "sudo ls",
    "git log --ext-diff", "git diff --output x", "git diff --out=x", "git fetch --upload-pack=evil o",
    "git fetch ext::sh", "git branch newname", "git branch -m a b", "git stash drop", "git remote add a b",
    "git reflog expire --all", "git config user.x y", "git -c core.pager=sh log", "curl https://a https://b",
    "curl -s file:///etc/passwd", "curl --config c https://a", "find . -exec rm {} +", "find . -fprint x",
    "sed -n 1w/tmp/x p", "sed -n '1e id' f", "sed 1p f", "awk '{print}' f", "bash status.sh", "bash -c ls",
    "bash /home/test/proj/scripts/status.sh --evil", "sort -o x y", "rg --pre sh x", "node a.js",
    "python3 a.py", "npm test", "date -s x", "uniq a b", "tail -F x", "", "ls |", "cat 'unterminated",
    "bash /home/test/proj/scripts/../../evil/scripts/status.sh",
]


class Allowlist(unittest.TestCase):
    def test_allowed(self):
        for c in ALLOWED:
            ok, why = mr.is_readonly_command(c)
            self.assertTrue(ok, "%r: %s" % (c, why))

    def test_rejected(self):
        for c in REJECTED:
            ok, why = mr.is_readonly_command(c)
            self.assertFalse(ok, c)
            self.assertTrue(why)

    def test_end_to_end_hook(self):
        d = tempfile.mkdtemp()
        try:
            pl = lambda c: {"tool_name": "Bash", "tool_input": {"command": c}, "cwd": d}
            self.assertEqual(run(pl("git -C /home/test/proj log --oneline -5")), "allow")
            self.assertEqual(run(pl("git push origin HEAD:master")), "deny")
            self.assertIn("Read-only commands", reason(pl("rm x")))
            self.assertEqual(run(pl("rm x"), {"CLAUDE_MAIN_WRITE": "1"}), "allow")
            self.assertEqual(run(dict(pl("git push"), agent_id="a1")), "allow")
            self.assertEqual(run({"tool_name": "PowerShell", "tool_input": {"command": "ls"}, "cwd": d}), "deny")
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_no_config_rejects_status_script(self):
        # Without a config the script path is not allowed; malformed config also must not crash the hook.
        d = tempfile.mkdtemp()
        try:
            pl = lambda c: {"tool_name": "Bash", "tool_input": {"command": c}, "cwd": d}
            cmd = "bash /home/test/proj/scripts/status.sh --json"
            self.assertEqual(run(pl(cmd), {"CLAUDE_HARNESS_CONFIG": NO_CFG}), "deny")
            self.assertEqual(run(pl(cmd), {"CLAUDE_HARNESS_CONFIG": BAD_CFG}), "deny")
            self.assertEqual(run(pl(cmd), {"CLAUDE_HARNESS_CONFIG": CFG}), "allow")
        finally:
            shutil.rmtree(d, ignore_errors=True)

class ScriptPathSafety(unittest.TestCase):
    """A configured script is matched on the typed path AND on its realpath (symlinks resolved)."""
    @classmethod
    def setUpClass(cls):
        r = cls.root = os.path.realpath(tempfile.mkdtemp(prefix="mr-scripts-"))
        for d in ["proj/scripts", "proj2", "evil/sub", "evil/scripts"]:
            os.makedirs(os.path.join(r, d))
        for f in ["proj/scripts/status.sh", "evil/scripts/status.sh"]:
            with open(os.path.join(r, f), "w", encoding="utf-8") as fh:
                fh.write("#!/bin/bash\necho ok\n")
        os.symlink(os.path.join(r, "evil", "sub"), os.path.join(r, "proj", "link"))
        os.symlink(os.path.join(r, "evil", "scripts"), os.path.join(r, "proj2", "scripts"))
        cls.cfg = os.path.join(r, "harness.local.json")
        with open(cls.cfg, "w", encoding="utf-8") as fh:
            json.dump({"readonly_scripts": [
                {"pattern": "^" + re.escape(r + "/proj/scripts/status.sh") + "$", "flags": ["--json"]},
                {"pattern": "^" + re.escape(r + "/proj2/scripts/status.sh") + "$", "flags": ["--json"]}]}, fh)
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)
    def pl(self, cmd):
        return {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": self.root}
    def env(self):
        return {"CLAUDE_HARNESS_CONFIG": self.cfg}
    def test_plain_absolute_allowed(self):
        self.assertEqual(run(self.pl("bash %s/proj/scripts/status.sh --json" % self.root), self.env()), "allow")
    def test_dotdot_through_symlink_denied(self):
        cmd = "bash %s/proj/link/../scripts/status.sh --json" % self.root
        self.assertEqual(run(self.pl(cmd), self.env()), "deny")
        self.assertIn("'..'", self._check("%s/proj/link/../scripts/status.sh" % self.root, ["--json"])[1])
    def test_dotdot_without_symlink_denied(self):
        self.assertEqual(run(self.pl("bash %s/proj/scripts/../scripts/status.sh --json" % self.root), self.env()), "deny")
    def test_symlinked_directory_realpath_denied(self):
        cmd = "bash %s/proj2/scripts/status.sh --json" % self.root
        self.assertEqual(run(self.pl(cmd), self.env()), "deny")
        self.assertIn("resolves to", self._check("%s/proj2/scripts/status.sh" % self.root, ["--json"])[1])
    def test_relative_path_denied(self):
        self.assertEqual(run(self.pl("bash scripts/status.sh --json"), self.env()), "deny")
        self.assertEqual(run(self.pl("bash proj/scripts/status.sh --json"), self.env()), "deny")
    def test_relative_path_refused_even_if_pattern_matches(self):
        saved = mr.SCRIPTS
        mr.SCRIPTS = [(re.compile(r"^scripts/status\.sh$"), frozenset(["--json"]))]
        try:
            ok, why = mr._script_check("scripts/status.sh", ["--json"])
        finally:
            mr.SCRIPTS = saved
        self.assertFalse(ok)
        self.assertIn("absolute", why)
    def test_unconfigured_flag_denied(self):
        self.assertEqual(run(self.pl("bash %s/proj/scripts/status.sh --evil" % self.root), self.env()), "deny")
    def _check(self, path, flags):
        """_script_check against this test's configured patterns (the hook's deny text is generic)."""
        with open(self.cfg, encoding="utf-8") as fh:
            entries = json.load(fh)["readonly_scripts"]
        saved = mr.SCRIPTS
        mr.SCRIPTS = [(re.compile(e["pattern"]), frozenset(e["flags"])) for e in entries]
        try:
            res = mr._script_check(path, flags)
        finally:
            mr.SCRIPTS = saved
        self.assertIsNotNone(res)
        return res


class ShellBypasses(unittest.TestCase):
    """Bash-syntax bypasses: mid-word '#' is literal, any unquoted '$' is refused, sort cannot write or run."""
    def setUp(self):
        self.d = tempfile.mkdtemp()
    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)
    def pl(self, cmd):
        return {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": self.d}
    def test_hash_is_literal_mid_word(self):
        cmd = "cat /etc/hostname#x | touch /tmp/pwn"
        self.assertFalse(mr.is_readonly_command(cmd)[0])
        self.assertEqual(run(self.pl(cmd)), "deny")
    def test_hash_inside_word_or_quotes_allowed(self):
        for c in ["grep '#include' f.c", "cat a#b"]:
            ok, why = mr.is_readonly_command(c)
            self.assertTrue(ok, "%r: %s" % (c, why))
    def test_unquoted_dollar_denied(self):
        for c in ["find . $IFS-exec rm {} +", "find . $IFS-delete", "git log $IFS--output=F",
                  "find . $'-delete'", "curl -s http://x/$HOME", "grep \\'$HOME\\' f", "sed -n 1,$p f"]:
            ok, why = mr.is_readonly_command(c)
            self.assertFalse(ok, c)
            self.assertEqual(run(self.pl(c)), "deny", c)
    def test_double_quoted_dollar_denied(self):
        for c in ['grep "a$" f', 'grep "x$HOME" f', 'grep "a\\$" f']:
            self.assertFalse(mr.is_readonly_command(c)[0], c)
    def test_single_quoted_dollar_allowed(self):
        for c in ["grep 'foo$' f", "sed -n '1,$p' f"]:
            ok, why = mr.is_readonly_command(c)
            self.assertTrue(ok, "%r: %s" % (c, why))
    def test_sort_write_or_exec_denied(self):
        for c in ["sort --compress-program=sh f", "sort --compress=sh f", "sort --compress-program sh f",
                  "sort -T /tmp f", "sort -T/tmp f", "sort -uT/tmp f", "sort --temporary-directory=/tmp f",
                  "sort --temp=/tmp f", "sort -o/tmp/x f", "sort -uo/tmp/x f", "sort --out=/tmp/x f",
                  "sort --output /tmp/x f"]:
            self.assertFalse(mr.is_readonly_command(c)[0], c)
        self.assertEqual(run(self.pl("sort --compress-program=sh f")), "deny")
        self.assertEqual(run(self.pl("sort -uT/tmp f")), "deny")
    def test_sort_plain_allowed(self):
        for c in ["sort -u f", "sort -n -k2 f", "sort -k2,2 -t, --reverse f"]:
            ok, why = mr.is_readonly_command(c)
            self.assertTrue(ok, "%r: %s" % (c, why))
        self.assertEqual(run(self.pl("sort -u f")), "allow")


class SymlinkedWriteRoots(unittest.TestCase):
    """Writes through symlinked roots, or into hooks/settings, are denied. HOME is a temp dir (via env)."""
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="mr-links-"))
        self.home = os.path.join(self.root, "home")
        self.hooks = os.path.join(self.home, ".claude", "hooks")
        os.makedirs(self.hooks)
        for path in [os.path.join(self.hooks, "harness.local.json"),
                     os.path.join(self.home, ".claude", "settings.json"),
                     os.path.join(self.home, ".claude", "settings.local.json")]:
            open(path, "w").close()
    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
    def env(self):
        return {"HOME": self.home}
    def write(self, path, cwd):
        return {"tool_name": "Write", "tool_input": {"file_path": path}, "cwd": cwd}
    def proj(self, name):
        d = os.path.join(self.root, name)
        os.makedirs(os.path.join(d, ".claude"))
        return d
    def test_real_plans_dir_allowed(self):
        p = self.proj("real")
        os.makedirs(p + "/.claude/plans")
        self.assertEqual(run(self.write(p + "/.claude/plans/x.md", p), self.env()), "allow")
        self.assertEqual(run(self.write(".claude/plans/x.md", p), self.env()), "allow")
    def test_symlinked_cwd_plans_denied(self):
        p = self.proj("linked")
        os.symlink(self.hooks, p + "/.claude/plans")
        self.assertEqual(run(self.write(p + "/.claude/plans/x.md", p), self.env()), "deny")
        self.assertEqual(run(self.write(p + "/.claude/plans/harness.local.json", p), self.env()), "deny")
    def test_symlinked_claude_dir_denied(self):
        p = self.proj("cdir")
        elsewhere = os.path.join(self.root, "elsewhere")
        os.makedirs(elsewhere + "/plans")
        os.rmdir(p + "/.claude")
        os.symlink(elsewhere, p + "/.claude")
        self.assertEqual(run(self.write(p + "/.claude/plans/x.md", p), self.env()), "deny")
    def test_plans_symlink_inside_root_into_hooks_denied(self):
        p = self.proj("inner")
        os.makedirs(p + "/.claude/plans")
        os.symlink(self.hooks, p + "/.claude/plans/evil")
        os.symlink(os.path.join(self.home, ".claude"), p + "/.claude/plans/evil2")
        self.assertEqual(run(self.write(p + "/.claude/plans/evil/harness.local.json", p), self.env()), "deny")
        self.assertEqual(run(self.write(p + "/.claude/plans/evil2/settings.json", p), self.env()), "deny")
        self.assertEqual(run(self.write(p + "/.claude/plans/evil2/settings.local.json", p), self.env()), "deny")
    def test_hooks_and_settings_denied_directly(self):
        p = self.proj("direct")
        os.makedirs(p + "/.claude/plans")
        for f in [self.hooks + "/harness.local.json", self.hooks + "/new.py",
                  self.home + "/.claude/settings.json", self.home + "/.claude/settings.local.json"]:
            self.assertEqual(run(self.write(f, p), self.env()), "deny", f)
    def test_home_plans_symlink_to_hooks_denied(self):
        os.symlink(self.hooks, self.home + "/.claude/plans")
        p = self.proj("hp")
        self.assertEqual(run(self.write(self.home + "/.claude/plans/harness.local.json", p), self.env()), "deny")
        self.assertEqual(run(self.write(self.home + "/.claude/plans/x.md", p), self.env()), "deny")
    def test_memory_dir_rules(self):
        p = self.proj("mem")
        os.makedirs(self.home + "/.claude/projects/-proj/memory")
        self.assertEqual(run(self.write(self.home + "/.claude/projects/-proj/memory/a.md", p), self.env()), "allow")
        os.makedirs(self.home + "/.claude/projects/other")
        os.symlink(self.hooks, self.home + "/.claude/projects/other/memory")
        self.assertEqual(run(self.write(self.home + "/.claude/projects/other/memory/harness.local.json", p), self.env()), "deny")
        self.assertEqual(run(self.write(self.home + "/.claude/projects/x/y/memory/a.md", p), self.env()), "deny")


class ScriptEntryFlags(unittest.TestCase):
    """A configured entry must fullmatch both the typed path and the realpath, and permit the flags."""
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="mr-entry-"))
        os.makedirs(os.path.join(self.root, "proj"))
        os.makedirs(os.path.join(self.root, "evil", "sub"))
        os.symlink(os.path.join(self.root, "evil", "sub"), os.path.join(self.root, "proj", "linkdir"))
        self.typed = os.path.join(self.root, "proj", "linkdir", "status.sh")
        self.real = os.path.join(self.root, "evil", "sub", "status.sh")
        self.saved = mr.SCRIPTS
    def tearDown(self):
        mr.SCRIPTS = self.saved
        shutil.rmtree(self.root, ignore_errors=True)
    def test_entries_split_between_typed_and_real_denied(self):
        # entry A matches only the typed path (flags --json); entry B matches only the realpath (flags --evil)
        mr.SCRIPTS = [(re.compile("^" + re.escape(self.typed) + "$"), frozenset(["--json"])),
                      (re.compile("^" + re.escape(self.real) + "$"), frozenset(["--evil"]))]
        self.assertFalse(mr._script_check(self.typed, ["--evil"])[0])
        self.assertFalse(mr.is_readonly_command("bash %s --evil" % self.typed)[0])
        self.assertFalse(mr.is_readonly_command("bash %s --json" % self.typed)[0])
    def test_single_entry_matching_both_permits_its_flags(self):
        both = re.compile("^" + re.escape(self.root) + r"/(?:proj/linkdir|evil/sub)/status\.sh$")
        mr.SCRIPTS = [(both, frozenset(["--json"]))]
        self.assertTrue(mr.is_readonly_command("bash %s --json" % self.typed)[0])
        self.assertFalse(mr.is_readonly_command("bash %s --evil" % self.typed)[0])


class ReviewBypasses(unittest.TestCase):
    """Bypasses closed in review pass 3: brace expansion, sed -i after the script, rg exec flags,
    git --output/--ext-diff on every subcommand, option-aware bad flags, empty arguments, and
    symlinked write roots (job tmp dir and /tmp/claude-UID)."""
    def setUp(self):
        self.d = tempfile.mkdtemp()
    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)
    def pl(self, cmd):
        return {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": self.d}
    def assertDenied(self, cmd):
        self.assertFalse(mr.is_readonly_command(cmd)[0], cmd)
        self.assertEqual(run(self.pl(cmd)), "deny", cmd)
    def assertAllowed(self, cmd):
        ok, why = mr.is_readonly_command(cmd)
        self.assertTrue(ok, "%r: %s" % (cmd, why))
        self.assertEqual(run(self.pl(cmd)), "allow", cmd)
    def test_brace_expansion_denied(self):
        # bash expands these into separate flags; the hook must see the braces and refuse.
        for c in ["sort {-o,/tmp/x} f", "find D {-delete,-print}", "find . {-fprint,F}",
                  "git log {--output=F,--oneline}", "sort {--compress-program=X,-S1}"]:
            self.assertDenied(c)
    def test_quoted_brace_allowed(self):
        # '{' inside double or single quotes is literal for bash, so it is allowed
        for c in ["grep '{' f", "grep -c '{' f", "find . -name '{}' -print", 'grep "{" f', 'grep "a { b } c" f']:
            self.assertAllowed(c)
    def test_sed_in_place_denied(self):
        for c in ["sed -n s/a/b/p -i F", "sed -n p F -i", "sed -ni p F", "sed --in-pl p F", "sed -f s.sed F",
                  "sed -n -i p F", "sed -n s/a/b/p -i.bak F", "sed -n --in-place=.bak p F",
                  "sed -n -s p F", "sed -n p -s F", "sed -n -e 1p -e q F", "sed -n -e s/a/b/p -i F"]:
            self.assertDenied(c)
    def test_sed_read_only_allowed(self):
        for c in ["sed -n 1,5p F", "sed -n '/x/p' F", "sed -n -e 1,5p F", "sed -n p F", "sed -n -E 's/a/b/p' F"]:
            self.assertAllowed(c)
    def test_rg_exec_flags_denied(self):
        for c in ["rg --hostname-bin=sh x", "rg --hostname-bin sh x", "rg --hostn=sh x",
                  "rg --pr=sh x", "rg --pre=sh x", "rg --pre sh x"]:
            self.assertDenied(c)
    def test_rg_plain_allowed(self):
        for c in ["rg -n foo .", "rg --pretty -n foo ."]:
            self.assertAllowed(c)
    def test_git_output_denied_on_every_subcommand(self):
        for c in ["git reflog --output=F", "git rev-list --output=F HEAD", "git shortlog --output=F",
                  "git shortlog --outp=F", "git reflog --output F", "git rev-list --outp F HEAD",
                  "git status --output=F", "git blame --output=F f"]:
            self.assertDenied(c)
    def test_git_wrapper_and_pager_flags_denied(self):
        for c in ["git -c core.pager=sh log", "git --exec-path=/x log", "git -p log",
                  "git --paginate log", "git log --ext-diff"]:
            self.assertDenied(c)
    def test_git_output_names_not_over_blocked(self):
        for c in ["git log --oneline -3", "git log --output-indicator-new=x"]:
            self.assertAllowed(c)
    def test_option_aware_bad_flags_denied(self):
        for c in ["file --comp -m M", "file -Cz", "tail --fol f", "date --se=x", "ss --kil",
                  "tail -n5f f", "date -s x", "sort --out=/tmp/x f"]:
            self.assertDenied(c)
    def test_option_aware_plain_allowed(self):
        for c in ["tail -n 5 f", "date +%F", "file f", "ss -ltnp", "tail -5 f", "sort -k2,2 -t, f"]:
            self.assertAllowed(c)
    def test_empty_argument_allowed(self):
        self.assertAllowed('grep -c "" f')
        self.assertAllowed("grep -c '' f")
    def test_job_tmp_symlink_denied_real_dir_allowed(self):
        other = tempfile.mkdtemp()
        job = tempfile.mkdtemp()
        real_job = tempfile.mkdtemp()
        try:
            os.symlink(other, os.path.join(job, "tmp"))
            wr = lambda j: {"tool_name": "Write", "tool_input": {"file_path": j + "/tmp/x.txt"}, "cwd": self.d}
            self.assertEqual(run(wr(job), {"CLAUDE_JOB_DIR": job}), "deny")
            os.makedirs(os.path.join(real_job, "tmp"))
            self.assertEqual(run(wr(real_job), {"CLAUDE_JOB_DIR": real_job}), "allow")
        finally:
            for x in (other, job, real_job):
                shutil.rmtree(x, ignore_errors=True)
    def test_scratch_root_symlink_denied_real_dir_allowed(self):
        from unittest import mock
        fake = 900000 + os.getpid() % 50000  # fake uid: the /tmp/claude-<uid> root is this test's own
        root = "/tmp/claude-%d" % fake
        self.assertFalse(os.path.lexists(root))
        outside = tempfile.mkdtemp()
        try:
            with mock.patch("os.getuid", return_value=fake):
                os.symlink(outside, root)
                try:
                    self.assertFalse(mr.path_allowed(root + "/x.txt", self.d))
                finally:
                    os.unlink(root)
                os.makedirs(root)
                try:
                    self.assertTrue(mr.path_allowed(root + "/x.txt", self.d))
                finally:
                    os.rmdir(root)
        finally:
            shutil.rmtree(outside, ignore_errors=True)


class ShellRules(unittest.TestCase):
    """Quote-aware shell rules: single quotes are fully literal, double quotes only let $ and backtick
    through, newlines are refused, chains split at unquoted && || ;, a lone & is refused, and only the
    four harmless redirects are allowed. Deny output must carry the reason."""
    def setUp(self):
        self.d = tempfile.mkdtemp()
    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)
    def pl(self, cmd):
        return {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": self.d}
    def assertAllowed(self, cmd):
        ok, why = mr.is_readonly_command(cmd)
        self.assertTrue(ok, "%r: %s" % (cmd, why))
        self.assertEqual(run(self.pl(cmd)), "allow", cmd)
    def assertDenied(self, cmd):
        self.assertFalse(mr.is_readonly_command(cmd)[0], cmd)
        self.assertEqual(run(self.pl(cmd)), "deny", cmd)
    def test_single_quoted_operators_allowed(self):
        for c in ["jq '.a | select(.x>30)' f.json", "grep 'a|b' f", "grep 'a;b' f", "grep 'a&&b' f",
                  "grep 'x > y' f", "grep 'a b' f", "grep 'x<y' f"]:
            self.assertAllowed(c)
    def test_double_quoted_operators_literal(self):
        for c in ['grep "a|b" f', 'grep "a;b" f', 'grep "a&&b" f', 'grep "a||b" f', 'grep "x > y" f',
                  'grep "x < y" f', 'grep "a & b" f']:
            self.assertAllowed(c)
    def test_quoted_redirect_names_not_refused(self):
        self.assertAllowed('grep "2>/dev/null" f')
        self.assertAllowed("grep '>/dev/null' f")
    def test_allowed_redirects(self):
        for c in ["cat f 2>/dev/null", "cat f >/dev/null", "cat f 2>&1", "cat f &>/dev/null",
                  "cat f 2>/dev/null | head -3", "git status 2>/dev/null && git log -1",
                  "git status 2>/dev/null; git log -1", "git status 2>&1 | head -2"]:
            self.assertAllowed(c)
    def test_other_redirects_denied(self):
        for c in ["cat x > y", "cat f 2>/dev/nullx", "cat f 2>/tmp/x", "cat f 2>>/dev/null",
                  "cat f > /dev/null", "cat f 2>&12", "cat < x", "cat f >&2", "cat f &>x"]:
            self.assertDenied(c)
    def test_newline_denied_even_quoted(self):
        for c in ["ls\nrm x", "grep 'a\nb' f", 'grep "a\nb" f', "git status\r"]:
            self.assertDenied(c)
    def test_expansion_denied_outside_single_quotes(self):
        for c in ['grep "a$" f', 'grep "x$HOME" f', 'grep "`id`" f', "cat `rm x`", "echo $(rm x)",
                  "grep ${x} f", "grep $HOME f"]:
            self.assertDenied(c)
    def test_chains_split_and_checked(self):
        for c in ["git status && git log -1", "git status || git log -1", "git status; git log -1",
                  "git status ;git log -1", "git status ; git log -1 ; git branch --list"]:
            self.assertAllowed(c)
        for c in ["ls || rm x", "ls && rm x", "ls; rm -rf /", "git status && git push origin HEAD:master",
                  "git status || curl -X POST https://a"]:
            self.assertDenied(c)
    def test_quoted_pipe_is_an_argument_not_a_pipe(self):
        for c in ['find . "|" cat -delete', "find . '|' cat -delete", 'git log "|" cat --output=x',
                  'find . "|" cat -delete | head -1']:
            self.assertDenied(c)
        self.assertAllowed('grep "a|b" file')
        self.assertAllowed("grep 'a|b' file")
        self.assertAllowed("cat file | head")
        self.assertAllowed("cat file \\| head")
    def test_empty_pipe_stage_denied(self):
        for c in ["cat f |", "| cat f", "cat f | | head"]:
            self.assertDenied(c)
    def test_lone_background_ampersand_denied(self):
        for c in ["sleep 1 & rm x", "git status &", "git status & git log -1"]:
            self.assertDenied(c)
    def test_deny_output_carries_reason(self):
        cases = [("cat x > y", "redirection"), ("ls\nrm x", "newline"), ("echo $(rm x)", "expansion"),
                 ("sleep 1 & rm x", "background"), ("rm x", "")]
        for cmd, needle in cases:
            msg = reason(self.pl(cmd))
            self.assertIn("Read-only commands", msg, cmd)
            self.assertIn("Rejected because:", msg, cmd)
            if needle:
                self.assertIn(needle, msg, cmd)


class TestReviewerReadOnly(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)
    def p(self, tool, agent_type=None, **ti):
        pl = {"tool_name": tool, "tool_input": ti, "cwd": self.d}
        if agent_type is not None:
            pl["agent_id"] = "a1"
            pl["agent_type"] = agent_type
        return pl
    def test_reviewer_denied_writes_and_mutating_shell(self):
        for t in ["pr-reviewer-2", "opus-reviewer"]:
            self.assertEqual(run(self.p("Write", t, file_path=self.d + "/f.txt")), "deny", t)
            self.assertEqual(run(self.p("Edit", t, file_path=self.d + "/f.txt")), "deny", t)
            for c in ["echo x > f", "git commit -m x", "git push"]:
                self.assertEqual(run(self.p("Bash", t, command=c)), "deny", (t, c))
    def test_reviewer_kill_switch_does_not_lift_rules(self):
        self.assertEqual(run(self.p("Bash", "pr-reviewer-1", command="git push"), {"CLAUDE_MAIN_WRITE": "1"}), "deny")
    def test_reviewer_allowed_readonly_and_cd_prefix(self):
        for c in ["git diff", "gh pr view 1", "cd /tmp && git log -1", "cd /tmp", "cd '/tmp/x y' && git diff"]:
            self.assertEqual(run(self.p("Bash", "pr-reviewer-2", command=c)), "allow", c)
    def test_diagnoser_read_only_rules(self):
        self.assertEqual(run(self.p("Write", "diagnoser-1", file_path=self.d + "/f.txt")), "deny")
        self.assertEqual(run(self.p("Bash", "diagnoser-1", command="grep -n foo file.txt")), "allow")
        self.assertEqual(run(self.p("Bash", "diagnoser-1", command="python3 -c 'print(1)'")), "deny")
    def test_reviewer_cd_does_not_allow_mutation(self):
        for c in ["cd /tmp && rm x", "cd /tmp; git log -1", "cd /tmp && echo x > f", "cd /tmp &&", "cd $HOME && git log",
                  "gh pr merge 1", "gh pr ready 1", "gh pr view 1 > f"]:
            self.assertEqual(run(self.p("Bash", "pr-reviewer-2", command=c)), "deny", c)
    def test_reviewer_denial_message(self):
        msg = reason(self.p("Write", "pr-reviewer-2", file_path=self.d + "/f.txt"))
        self.assertIn("Reviewer and diagnoser agents are read-only: report findings, never edit/commit/push.", msg)
        self.assertNotIn("subagent_type", msg)
    def test_executor_still_unrestricted(self):
        self.assertEqual(run(self.p("Write", "qorvax-tel", file_path="/etc/zz-not-allowed")), "allow")
        self.assertEqual(run(self.p("Bash", "qorvax-tel", command="rm -rf /tmp/zz-nothing")), "allow")
    def test_main_still_denies_cd(self):
        self.assertEqual(run({"tool_name": "Bash", "tool_input": {"command": "cd /tmp"}, "cwd": self.d}), "deny")
    def test_reviewer_cd_gh_views_allowed(self):
        for c in ["cd /x && gh pr view 3 --json url,headRefOid", "cd /x && gh pr diff 3"]:
            self.assertEqual(run(self.p("Bash", "pr-reviewer-2", command=c)), "allow", c)
    def test_reviewer_cd_gh_redirect_and_merge_denied(self):
        for c in ["cd /x && gh pr diff 3 > f", "cd /x && gh pr merge 3"]:
            self.assertEqual(run(self.p("Bash", "pr-reviewer-2", command=c)), "deny", c)
    def test_reviewer_pytest_and_unittest_denied(self):
        for c in ["cd /x && python3 -m pytest -q .claude/hooks", "cd /x && python3 -m unittest discover -s /tmp/claude-1000",
                  "cd /x && python3 -m pytest --junitxml=/home/u/.bashrc", "cd /x && python3 -m pytest -p evilmod"]:
            self.assertEqual(run(self.p("Bash", "pr-reviewer-2", command=c)), "deny", c)
    def test_monitor_without_command_key_does_not_crash(self):
        for pl in [{"tool_name": "Monitor", "tool_input": {"timeout_ms": 1000}, "cwd": self.d},
                   {"tool_name": "Monitor", "tool_input": {}, "cwd": self.d}]:
            self.assertIn(run(pl), ("allow", "deny"), pl)
    def test_reviewer_pytest_redirect_denied(self):
        for c in ["python3 -m pytest -q > out", "python3 -m pytest -q | tail", "python3 -m pytest -q; rm x"]:
            self.assertEqual(run(self.p("Bash", "pr-reviewer-2", command=c)), "deny", c)
    def test_main_pytest_unchanged(self):
        self.assertEqual(run({"tool_name": "Bash", "tool_input": {"command": "python3 -m pytest -q"}, "cwd": self.d}), "deny")
    def test_reviewer_write_to_memory_denied(self):
        self.assertEqual(run(self.p("Write", "pr-reviewer-2", file_path=HOME + "/.claude/projects/x/memory/MEMORY.md")), "deny")
    def test_reviewer_write_to_plans_denied(self):
        self.assertEqual(run(self.p("Write", "pr-reviewer-2", file_path=HOME + "/.claude/plans/p.md")), "deny")
        self.assertEqual(run(self.p("Write", "pr-reviewer-2", file_path=self.d + "/.claude/plans/p.md")), "deny")
    def test_reviewer_write_to_job_tmp_allowed(self):
        self.assertEqual(run(self.p("Write", "pr-reviewer-2", file_path="/j/tmp/a"), {"CLAUDE_JOB_DIR": "/j"}), "allow")
    def test_main_write_to_plans_still_allowed(self):
        self.assertEqual(run({"tool_name": "Write", "tool_input": {"file_path": HOME + "/.claude/plans/p.md"}, "cwd": self.d}), "allow")
    def test_monitor_git_push_denied_main_and_reviewer(self):
        self.assertEqual(run({"tool_name": "Monitor", "tool_input": {"command": "git push origin HEAD"}, "cwd": self.d}), "deny")
        self.assertEqual(run(self.p("Monitor", "pr-reviewer-2", command="git push origin HEAD")), "deny")
    def test_monitor_git_log_allowed_main(self):
        self.assertEqual(run({"tool_name": "Monitor", "tool_input": {"command": "git log -1"}, "cwd": self.d}), "allow")


class MainAllowCommandsAndJobScripts(unittest.TestCase):
    """main_allow_commands globs and scripts run from a Claude job tmp dir. Both apply to the main session
    only: a reviewer payload is never allowed them. Config and HOME are set through the hook's env."""
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.home = tempfile.mkdtemp()
        self.cfg_dir = tempfile.mkdtemp()
    def tearDown(self):
        for x in (self.d, self.home, self.cfg_dir):
            shutil.rmtree(x, ignore_errors=True)
    def pl(self, cmd, agent_type=None):
        p = {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": self.d}
        if agent_type is not None:
            p["agent_id"] = "a1"
            p["agent_type"] = agent_type
        return p
    def env(self, text=None, **extra):
        """Hook env: the config holding text (raw), or no config when text is None; HOME is self.home."""
        if text is None:
            e = {"CLAUDE_HARNESS_CONFIG": NO_CFG}
        else:
            path = os.path.join(self.cfg_dir, "harness.local.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            e = {"CLAUDE_HARNESS_CONFIG": path}
        e["HOME"] = self.home
        e.update(extra)
        return e
    def ok(self, cmd, text=None, agent_type=None, **extra):
        return run(self.pl(cmd, agent_type), self.env(text, **extra)) == "allow"
    def cfg_allow(self, globs):
        return json.dumps({"main_allow_commands": globs})

    def test_pattern_allows_matching_simple_command(self):
        cfg = self.cfg_allow(["make -C * build"])
        self.assertTrue(self.ok("make -C /x build", cfg))
        self.assertTrue(self.ok("make -C '/x y' build", cfg))
        self.assertFalse(self.ok("make -C /x install", cfg))
        self.assertFalse(self.ok("make -C /x build extra", cfg))

    def test_pattern_refuses_chains_pipes_and_redirects(self):
        cfg = self.cfg_allow(["make -C * build"])
        for c in ["make -C /x build; rm -rf /", "make -C /x build | tee f", "make -C /x build $(id)",
                  "make -C /x build || rm x", "make -C /x build > f", "make -C /x build && rm x"]:
            self.assertFalse(self.ok(c, cfg), c)

    def test_banned_words_refused_even_when_pattern_matches(self):
        cfg = self.cfg_allow(["*"])
        for c in ["tee f", "ls xargs", "make eval", "make sudo x", "make exec", "make source f"]:
            self.assertFalse(self.ok(c, cfg), c)

    def test_pattern_does_not_bypass_global_checks(self):
        cfg = self.cfg_allow(["*"])
        for c in ["make > f", "make 2>/tmp/x", "make $HOME", "make ${X}", "make `id`", "make\nrm x",
                  "make | cat", "make 2>/dev/null | cat", "make 2>&1 > f"]:
            self.assertFalse(self.ok(c, cfg), repr(c))
        self.assertTrue(self.ok("make build 2>/dev/null", cfg))

    def test_missing_or_malformed_config_gives_no_patterns(self):
        self.assertFalse(self.ok("make build", None))
        for text in ['{not json', json.dumps({"main_allow_commands": "make *"}),
                     json.dumps({"main_allow_commands": {"a": "make *"}}), json.dumps(["main_allow_commands"]),
                     json.dumps({"main_allow_commands": [3, None, ["make *"]]})]:
            self.assertFalse(self.ok("make build", text), text)

    def test_non_string_entries_skipped(self):
        cfg = self.cfg_allow([3, "make build", None, "  "])
        self.assertTrue(self.ok("make build", cfg))
        self.assertFalse(self.ok("make install", cfg))

    def test_reviewer_not_allowed_pattern_command(self):
        cfg = self.cfg_allow(["make build", "*"])
        self.assertTrue(self.ok("make build", cfg))
        self.assertEqual(run(self.pl("make build", "pr-reviewer-1"), self.env(cfg)), "deny")
        self.assertEqual(run(self.pl("make build", "opus-reviewer"), self.env(cfg)), "deny")

    def test_job_tmp_script_rules(self):
        jobs = os.path.join(self.home, ".claude", "jobs")
        script = os.path.join(jobs, "3273eb82", "tmp", "deploy-micro-manager.ps1")
        cases_ok = ["powershell -NoProfile -ExecutionPolicy Bypass -File " + script,
                    "PowerShell.exe -NonInteractive -executionpolicy bypass -File " + script + " arg1",
                    "pwsh -File " + script, "bash '%s'" % script, "sh '%s' x y" % script,
                    "python3 -I '%s'" % script, "python -u '%s'" % script, "py '%s'" % script,
                    "powershell -File " + script]
        for c in cases_ok:
            self.assertTrue(self.ok(c), c)
        cases_bad = ["powershell -Command " + script, "powershell -EncodedCommand x",
                     "powershell -NoProfile -File " + os.path.join(jobs, "3273eb82", "deploy.ps1"),
                     "powershell -ExecutionPolicy -File " + script,
                     "bash /tmp/evil.sh", "bash tmp/a.sh",
                     "powershell -File " + os.path.join(jobs, "3273eb82", "tmp", "..", "tmp", "a.ps1"),
                     "powershell -File " + os.path.join(jobs, "x", "tmp2", "a.ps1"),
                     "powershell -File " + os.path.join(jobs, "a", "b", "tmp", "a.ps1"),
                     "powershell -File " + os.path.join(jobs, "3273eb82", "tmp", "*.ps1"),
                     "python -c 'x'", "python3 a.py",
                     "powershell -File %s | cat" % script, "powershell -File %s; rm x" % script,
                     "powershell -File %s $(id)" % script, "powershell -File %s tee" % script]
        for c in cases_bad:
            self.assertFalse(self.ok(c), c)

    def test_job_dir_env_tmp_allowed(self):
        job = os.path.join(self.home, "work", "job1")
        self.assertTrue(self.ok("bash '%s'" % os.path.join(job, "tmp", "a.sh"), CLAUDE_JOB_DIR=job))
        self.assertFalse(self.ok("bash '%s'" % os.path.join(job, "a.sh"), CLAUDE_JOB_DIR=job))

    def test_py_launcher_version_flags(self):
        script = os.path.join(self.home, ".claude", "jobs", "3273eb82", "tmp", "s.py")
        self.assertTrue(self.ok("py -3 '%s'" % script))
        self.assertTrue(self.ok("py -3.12 -I '%s'" % script))
        self.assertFalse(self.ok("py -3"))
        self.assertFalse(self.ok("py -3 C:/Windows/x.py"))

    def test_job_script_refused_for_reviewers(self):
        script = os.path.join(self.home, ".claude", "jobs", "3273eb82", "tmp", "s.sh")
        self.assertTrue(self.ok("bash '%s'" % script))
        self.assertEqual(run(self.pl("bash '%s'" % script, "pr-reviewer-2"), self.env()), "deny")
        self.assertEqual(run(self.pl("bash '%s'" % script, "opus-reviewer"), self.env()), "deny")

    def test_job_tmp_backslash_traversal_refused(self):
        # bash drops an unquoted backslash, so "\.\./" runs as "../" although the typed word has no '..'
        jobs = os.path.join(self.home, ".claude", "jobs")
        exact = "bash " + jobs + r"/x/tmp/\.\./\.\./\.\./\.\./\.\./\.\./tmp/claude-1000/evil.sh"
        mid = "bash " + jobs + r"/x/tmp/ok/\.\./\.\./\.\./\.\./\.\./\.\./tmp/evil.sh"
        quoted_dq = 'bash "' + jobs + r'/x/tmp/\\.\\./\\.\\./\\.\\./\\.\\./\\.\\./\\.\\./tmp/evil.sh"'
        quoted_sq = "bash '" + jobs + r"/x/tmp/a\b.sh'"
        quote_smuggle = "bash " + jobs + "/x/tmp/" + "/".join(["'..'"] * 6) + "/tmp/evil.sh"
        for c in (exact, mid, quoted_dq, quoted_sq, quote_smuggle):
            self.assertFalse(self.ok(c), c)
            self.assertEqual(run(self.pl(c), self.env()), "deny", c)

    def test_normal_job_tmp_script_still_passes(self):
        jobs = os.path.join(self.home, ".claude", "jobs")
        self.assertTrue(self.ok("bash " + jobs + "/x/tmp/a.sh"))
        self.assertTrue(self.ok("bash '%s'" % os.path.join(jobs, "x", "tmp", "a b.sh")))


if __name__ == "__main__":
    unittest.main()
