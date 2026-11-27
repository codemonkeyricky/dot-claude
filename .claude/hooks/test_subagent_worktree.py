import json, os, shutil, subprocess, sys, tempfile, unittest
HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "subagent-worktree.py")
STRIP = ("CLAUDE_JOB_DIR", "CLAUDE_SUBAGENT_ANYWHERE", "CLAUDE_MAIN_WRITE", "CLAUDE_HARNESS_CONFIG",
         "HARNESS_TEST_ROLE", "CLAUDE_HOOK_LOG_DIR")
REAL_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "subagent-worktree.log")
WRITE_KEYS = [("Write", "file_path"), ("Edit", "file_path"), ("MultiEdit", "file_path"), ("NotebookEdit", "notebook_path")]


def fwd(p):
    return p.replace("\\", "/")


def _rm(p):
    def fix(func, path, exc):
        try:
            os.chmod(path, 0o700)
            func(path)
        except OSError:
            pass
    shutil.rmtree(p, onerror=fix)


class T(unittest.TestCase):
    def setUp(self):
        # Repos live under self.base; the hook is pointed at self.tmpenv as TEMP,
        # which is NOT an ancestor of the repos.
        self.base = tempfile.mkdtemp(prefix="sw-base-")
        self.tmpenv = tempfile.mkdtemp(prefix="sw-tmp-")
        self.main = os.path.join(self.base, "main")
        self.wt = os.path.join(self.base, "wt")
        self.plain = os.path.join(self.base, "plain")
        self.job = os.path.join(self.base, "job")
        self.subrepo = os.path.join(self.base, "subrepo")
        self.logdir = tempfile.mkdtemp(prefix="sw-log-")  # deny log goes here, not beside the hook
        os.makedirs(self.main)
        os.makedirs(self.plain)
        os.makedirs(self.job)
        gitc = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
        subprocess.run(["git", "init", "-q", self.main], check=True, capture_output=True)
        with open(os.path.join(self.main, "a"), "w") as f:
            f.write("x\n")
        subprocess.run(gitc + ["-C", self.main, "add", "-A"], check=True, capture_output=True)
        subprocess.run(gitc + ["-C", self.main, "commit", "-q", "-m", "init"], check=True, capture_output=True)
        subprocess.run(["git", "-C", self.main, "worktree", "add", "-q", self.wt, "-b", "wtbranch"],
                       check=True, capture_output=True)
        # Submodule-style checkout: .git is a FILE pointing into .git/modules/.
        os.makedirs(self.subrepo)
        with open(os.path.join(self.subrepo, ".git"), "w") as f:
            f.write("gitdir: %s/.git/modules/x\n" % fwd(self.main))

    def tearDown(self):
        _rm(self.base)
        _rm(self.tmpenv)
        _rm(self.logdir)

    def run_hook(self, payload, env=None):
        e = dict(os.environ)
        for k in STRIP:
            e.pop(k, None)
        e.update({"TEMP": self.tmpenv, "TMP": self.tmpenv, "TMPDIR": self.tmpenv,
                  "CLAUDE_HOOK_LOG_DIR": self.logdir})
        e.update(env or {})
        e.setdefault("CLAUDE_HARNESS_CONFIG", os.path.join(self.base, "no-harness-config.json"))
        r = subprocess.run([sys.executable, HOOK],
                           input=payload if isinstance(payload, str) else json.dumps(payload),
                           capture_output=True, text=True, env=e)
        self.assertEqual(r.returncode, 0, r.stderr)
        if not r.stdout.strip():
            return "allow", ""
        out = json.loads(r.stdout)["hookSpecificOutput"]
        return out["permissionDecision"], out["permissionDecisionReason"]

    def decide(self, payload, env=None):
        return self.run_hook(payload, env)[0]

    def sub(self, tool, cwd, agent_type="general-purpose", **ti):
        return {"tool_name": tool, "tool_input": ti, "cwd": cwd, "session_id": "s1",
                "hook_event_name": "PreToolUse", "agent_id": "a1", "agent_type": agent_type}

    def sh(self, cmd, cwd, tool="Bash", agent_type="general-purpose"):
        return self.sub(tool, cwd, agent_type, command=cmd)

    def w(self, tool, path, cwd):
        key = "notebook_path" if tool == "NotebookEdit" else "file_path"
        return self.sub(tool, cwd, **{key: path})

    # ----- main agent and fail-open -----

    def test_main_agent_unrestricted(self):
        self.assertEqual(self.decide({"tool_name": "Bash", "tool_input": {"command": "rm a"}, "cwd": self.main}), "allow")
        self.assertEqual(self.decide({"tool_name": "Write", "tool_input": {"file_path": self.main + "/a"}, "cwd": self.main}), "allow")

    def test_fail_open(self):
        self.assertEqual(self.decide("not json"), "allow")
        self.assertEqual(self.decide(""), "allow")

    # ----- write tools -----

    def test_write_in_worktree_allowed(self):
        for tool, _ in WRITE_KEYS:
            ext = "n.ipynb" if tool == "NotebookEdit" else "a.txt"
            self.assertEqual(self.decide(self.w(tool, os.path.join(self.wt, ext), self.wt)), "allow", tool)
        self.assertEqual(self.decide(self.w("Write", os.path.join(self.wt, "deep", "new", "f.txt"), self.wt)), "allow")

    def test_write_in_main_or_plain_denied(self):
        for tool, _ in WRITE_KEYS:
            ext = "n.ipynb" if tool == "NotebookEdit" else "a.txt"
            self.assertEqual(self.decide(self.w(tool, os.path.join(self.main, ext), self.main)), "deny", tool)
            self.assertEqual(self.decide(self.w(tool, os.path.join(self.plain, ext), self.plain)), "deny", tool)

    def test_sibling_worktree_outside_main_dir_allowed(self):
        # Layout used by this user: worktrees live in <repo>-wt/<name>, beside the main checkout.
        sib = os.path.join(self.base, "main-wt", "sib")
        os.makedirs(os.path.dirname(sib))
        subprocess.run(["git", "-C", self.main, "worktree", "add", "-q", sib, "-b", "sibbranch"],
                       check=True, capture_output=True)
        self.assertFalse(os.path.commonpath([os.path.abspath(sib), os.path.abspath(self.main)]) == os.path.abspath(self.main))
        self.assertEqual(self.decide(self.w("Write", os.path.join(sib, "a.txt"), sib)), "allow")
        self.assertEqual(self.decide(self.w("Edit", os.path.join(sib, "a.txt"), sib)), "allow")
        self.assertEqual(self.decide(self.sh("git status", sib)), "allow")
        self.assertEqual(self.decide(self.w("Write", os.path.join(self.main, "a.txt"), self.main)), "deny")

    def test_write_in_submodule_denied(self):
        self.assertEqual(self.decide(self.w("Write", os.path.join(self.subrepo, "a"), self.subrepo)), "deny")
        self.assertEqual(self.decide(self.sh("rm a", self.subrepo)), "deny")

    def test_write_scratch_allowed(self):
        env = {"CLAUDE_JOB_DIR": self.job}
        self.assertEqual(self.decide(self.w("Write", os.path.join(self.job, "tmp", "x"), self.plain), env), "allow")
        self.assertEqual(self.decide(self.w("Write", os.path.join(self.tmpenv, "x"), self.plain)), "allow")
        self.assertEqual(self.decide(self.w("Write", os.path.join(self.job, "tmp", "x"), self.plain)), "deny")

    def test_write_relative_path(self):
        self.assertEqual(self.decide(self.w("Edit", "sub/x.txt", self.wt)), "allow")
        self.assertEqual(self.decide(self.w("Edit", "sub/x.txt", self.main)), "deny")

    def test_write_dotdot_escape_denied(self):
        self.assertEqual(self.decide(self.w("Write", self.wt + "/../main/a.txt", self.wt)), "deny")
        self.assertEqual(self.decide(self.w("Write", self.wt + "/../plain/x", self.wt)), "deny")

    # ----- shell tools, cwd = main checkout -----

    def test_bash_main_readonly_allowed(self):
        for cmd in ["git status", "ls -la", "gh pr view 1", "git log | head",
                    "git worktree add ../x -b y", "git status 2>/dev/null", "ls > /dev/null",
                    "git diff 2>&1 | head -5"]:
            self.assertEqual(self.decide(self.sh(cmd, self.main)), "allow", cmd)

    def test_bash_main_mutating_denied(self):
        for cmd in ["rm a", "echo x > a", "git commit -m x", "python s.py", "sed -i s/a/b/ f",
                    "find . -delete", "ls $(rm x)"]:
            self.assertEqual(self.decide(self.sh(cmd, self.main)), "deny", cmd)

    def test_bash_cd_and_git_c(self):
        wt, main = fwd(self.wt), fwd(self.main)
        self.assertEqual(self.decide(self.sh("cd %s && git commit -m x" % wt, self.main)), "allow")
        self.assertEqual(self.decide(self.sh("cd %s && cd %s && rm a" % (wt, main), self.main)), "deny")
        self.assertEqual(self.decide(self.sh("git -C %s commit -m x" % wt, self.main)), "allow")
        self.assertEqual(self.decide(self.sh("git -C %s commit -m x" % main, self.main)), "deny")
        self.assertEqual(self.decide(self.sh("git -C %s commit -m x" % main, self.wt)), "deny")

    def test_bash_cwd_worktree(self):
        self.assertEqual(self.decide(self.sh("npm test", self.wt)), "allow")
        self.assertEqual(self.decide(self.sh("rm a", self.wt)), "allow")

    # ----- PowerShell -----

    def test_powershell(self):
        ps = lambda cmd, cwd: self.sh(cmd, cwd, tool="PowerShell")
        wt = fwd(self.wt)
        self.assertEqual(self.decide(ps("Get-ChildItem", self.main)), "allow")
        self.assertEqual(self.decide(ps("Set-Location %s; Remove-Item a" % wt, self.main)), "allow")
        self.assertEqual(self.decide(ps("Remove-Item a", self.main)), "deny")
        self.assertEqual(self.decide(ps("Set-Content a x", self.main)), "deny")

    # ----- exemptions -----

    def test_exempt_agent_types(self):
        for at in ["Explore", "opus-reviewer", "pr-reviewer-2", "diagnoser-1"]:
            self.assertEqual(self.decide(self.sh("rm a", self.main, agent_type=at)), "allow", at)

    def test_exempt_env_switches(self):
        self.assertEqual(self.decide(self.sh("rm a", self.main), {"CLAUDE_SUBAGENT_ANYWHERE": "1"}), "allow")
        self.assertEqual(self.decide(self.sh("rm a", self.main), {"CLAUDE_MAIN_WRITE": "1"}), "allow")

    def _config(self, text):
        path = os.path.join(self.base, "harness.local.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_exempt_env_config_glob(self):
        cfg = self._config(json.dumps({"exempt_env": {"HARNESS_TEST_ROLE": ["bot-*"]}}))
        env = {"CLAUDE_HARNESS_CONFIG": cfg}
        self.assertEqual(self.decide(self.sh("rm a", self.main), dict(env, HARNESS_TEST_ROLE="bot-x")), "allow")
        self.assertEqual(self.decide(self.sh("rm a", self.main), dict(env, HARNESS_TEST_ROLE="Bot-x")), "deny")
        self.assertEqual(self.decide(self.sh("rm a", self.main), dict(env, HARNESS_TEST_ROLE="")), "deny")

    def test_exempt_env_missing_config_denies(self):
        cfg = os.path.join(self.base, "missing.json")
        env = {"CLAUDE_HARNESS_CONFIG": cfg, "HARNESS_TEST_ROLE": "bot-x"}
        self.assertEqual(self.decide(self.sh("rm a", self.main), env), "deny")

    # ----- deny reason and log -----

    def test_deny_reason_is_actionable(self):
        dec, reason = self.run_hook(self.sh("rm a", self.main))
        self.assertEqual(dec, "deny")
        self.assertIn("worktree add", reason)
        self.assertIn("shell command in", reason)
        dec, reason = self.run_hook(self.w("Write", os.path.join(self.main, "a"), self.main))
        self.assertEqual(dec, "deny")
        self.assertIn("worktree add", reason)
        self.assertIn("cd $CLAUDE_JOB_DIR/tmp", reason)

    # ----- regression: harmless commands allowed, mutating ones still denied -----

    def _scr(self):
        return fwd(os.path.join(self.job, "tmp"))

    def test_allow_read_only_remote_and_scratch_commands(self):
        env = {"CLAUDE_JOB_DIR": self.job}
        m, wt, scr = fwd(self.main), fwd(self.wt), self._scr()
        cases = [
            "sed -n 2,5p %s/f" % m,
            "git -C %s stash list" % m,
            "git -C %s symbolic-ref refs/remotes/origin/HEAD" % m,
            "cd %s && git fetch origin && git symbolic-ref refs/remotes/origin/HEAD; git rev-parse origin/master; git worktree list" % m,
            "gh pr ready 161 --repo o/r",
            "gh pr create --draft --title x --body y",
            'curl -s -o /dev/null -w "%{http_code}" http://x',
            "until grep -q x f 2>/dev/null; do sleep 5; done",
            "git -C %s worktree remove %s" % (m, wt),
            'W="%s"; cd "$W" && rm a' % wt,
            "mkdir -p %s/x" % scr,
            "cp %s/a %s/a" % (m, scr),
            "cp %s/a %s/a" % (m, wt),
            "git -C %s log --oneline > %s/log.txt" % (m, scr),
            "set -euo pipefail; ls",
        ]
        for cmd in cases:
            self.assertEqual(self.decide(self.sh(cmd, self.plain), env), "allow", cmd)
        ps = "$wt = '%s'; Set-Location $wt; Remove-Item a" % wt
        self.assertEqual(self.decide(self.sh(ps, self.plain, tool="PowerShell"), env), "allow", ps)

    def test_deny_mutating_and_unresolvable_commands(self):
        env = {"CLAUDE_JOB_DIR": self.job}
        m, wt, scr = fwd(self.main), fwd(self.wt), self._scr()
        cases = [
            "sed -i s/a/b/ %s/f" % m,
            "sed -Ei s/a/b/ %s/f" % m,
            "git -C %s stash" % m,
            "git -C %s symbolic-ref HEAD refs/heads/x" % m,
            "cd %s && gh pr checkout 5" % m,
            "curl -o %s/f http://x" % m,
            'W="%s"; cd "$W" && rm a' % m,
            "rm %s/a" % m,
            "cp %s/a %s/a" % (wt, m),
            "mv %s/a %s/a" % (m, scr),
            "cd $UNKNOWN && rm a",
            "python s.py > %s/out.json" % scr,
            "until rm x; do sleep 1; done",
            "git -C %s log > %s/log.txt" % (m, m),
        ]
        for cmd in cases:
            self.assertEqual(self.decide(self.sh(cmd, self.plain), env), "deny", cmd)

    def _allow_cfg(self, value):
        path = os.path.join(self.base, "allow-harness.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"subagent_allow_commands": value}, f)
        return {"CLAUDE_HARNESS_CONFIG": path}

    def test_allowlist_permits_matching_commands_anywhere(self):
        env = self._allow_cfg(["python* */portal_deploy.py *", "gh pr edit *"])
        p = fwd(self.plain)
        allowed = [
            "python C:/x/portal_deploy.py deploy --confirm",
            "cd C:/notaworktree && python3 tools/portal_deploy.py build",
            "gh pr edit 12 --body-file C:/tmp/b.md",
        ]
        for cmd in allowed:
            self.assertEqual(self.decide(self.sh(cmd, p), env), "allow", cmd)

    def test_allowlist_keeps_other_and_guarded_commands_denied(self):
        env = self._allow_cfg(["python* */portal_deploy.py *", "gh pr edit *"])
        p = fwd(self.plain)
        denied = [
            "python C:/x/other.py",
            "python C:/x/portal_deploy.py deploy > C:/notaworktree/out.txt",
            "python $(echo C:/x/portal_deploy.py) deploy",
        ]
        for cmd in denied:
            self.assertEqual(self.decide(self.sh(cmd, p), env), "deny", cmd)

    def test_allowlist_never_exempts_file_operations(self):
        env = self._allow_cfg(["rm *", "cp *", "mv *"])
        p = fwd(self.plain)
        for cmd in ["rm C:/notaworktree/f", "cp a C:/notaworktree/f", "mv a C:/notaworktree/f"]:
            self.assertEqual(self.decide(self.sh(cmd, p), env), "deny", cmd)

    def test_allowlist_missing_or_malformed_is_unchanged(self):
        p = fwd(self.plain)
        cmd = "python C:/x/portal_deploy.py deploy --confirm"
        self.assertEqual(self.decide(self.sh(cmd, p)), "deny")
        self.assertEqual(self.decide(self.sh(cmd, p), self._allow_cfg("notalist")), "deny")

    # ----- fix 1: git branch listing is read-only, branch creation and edits are not -----

    def test_git_branch_listing_allowed_anywhere(self):
        cases = [
            "git branch", "git branch --list", "git branch --list feat*",
            "git branch -l feat*", "git branch --list feat* other*",
            "git branch --list --format=%(refname:short) feat*",
            "git branch -a", "git branch -r", "git branch -v", "git branch -vv",
            "git branch --show-current", "git branch --contains abc123",
            "git branch --merged main", "git branch --no-merged", "git branch --format=%(refname)",
            "git branch -a --list feat*",
        ]
        for cmd in cases:
            self.assertEqual(self.decide(self.sh(cmd, self.plain)), "allow", cmd)

    def test_git_branch_mutating_still_denied(self):
        cases = [
            "git branch newb", "git branch newb HEAD~1", "git branch -d old", "git branch -D old",
            "git branch -m old new", "git branch -M a b", "git branch -c a b", "git branch -C a b",
            "git branch -u origin/x", "git branch --set-upstream-to=origin/x",
            "git branch --unset-upstream", "git branch -f x HEAD", "git branch -a newb",
            "git branch --format x newb", "git branch --list x -d y", "git branch --list x newb -f",
        ]
        for cmd in cases:
            self.assertEqual(self.decide(self.sh(cmd, self.plain)), "deny", cmd)

    # ----- fix 2: MSYS /c/... paths on Windows (skipped elsewhere) -----

    def _msys(self, p):
        drive, rest = os.path.splitdrive(os.path.abspath(p))
        return "/%s%s" % (drive[0].lower(), fwd(rest))

    def test_msys_cd_into_worktree_allowed(self):
        if os.name != "nt":
            self.skipTest("MSYS /<drive>/ path mapping is Windows-only")
        wt = self._msys(self.wt)
        self.assertEqual(self.decide(self.sh("cd %s && touch x" % wt, self.plain)), "allow")
        self.assertEqual(self.decide(self.sh("cd /cygdrive%s && touch x" % wt, self.plain)), "allow")

    def test_msys_cd_into_non_worktree_denied(self):
        if os.name != "nt":
            self.skipTest("MSYS /<drive>/ path mapping is Windows-only")
        plain = self._msys(self.plain)
        self.assertEqual(self.decide(self.sh("cd %s && touch x" % plain, self.wt)), "deny")
        self.assertEqual(self.decide(self.sh("cd %s && rm a" % self._msys(self.main), self.plain)), "deny")

    def test_msys_write_tool_and_redirect(self):
        if os.name != "nt":
            self.skipTest("MSYS /<drive>/ path mapping is Windows-only")
        self.assertEqual(self.decide(self.w("Write", self._msys(self.wt) + "/n.txt", self.wt)), "allow")
        self.assertEqual(self.decide(self.w("Write", self._msys(self.main) + "/n.txt", self.wt)), "deny")
        self.assertEqual(self.decide(self.sh("echo x > %s/n.txt" % self._msys(self.plain), self.wt)), "deny")

    # ----- fix 3: read-only PowerShell cmdlets -----

    def test_powershell_read_only_cmdlets_allowed(self):
        ps = lambda cmd: self.sh(cmd, self.plain, tool="PowerShell")
        cases = ["Get-FileHash a", "Get-ItemProperty a", "gp a", "Get-Acl a", "Split-Path a -Parent",
                 "Join-Path a b", "Group-Object x", "Compare-Object a b", "Resolve-Path a",
                 "Measure-Object", "Select-Object -First 1", "Get-Command git", "gcm git",
                 "ConvertFrom-Json", "Out-String", "Get-Date", "gps", "gsv", "Get-Process"]
        for cmd in cases:
            self.assertEqual(self.decide(ps(cmd)), "allow", cmd)

    def test_powershell_writers_still_denied(self):
        ps = lambda cmd: self.sh(cmd, self.plain, tool="PowerShell")
        cases = ["Out-File a", "Set-Content a x", "Add-Content a x", "New-Item a",
                 "Remove-Item a", "Copy-Item a b", "Move-Item a b", "ForEach-Object { rm $_ }",
                 "Invoke-Expression 'rm a'", "Start-Process rm"]
        for cmd in cases:
            self.assertEqual(self.decide(ps(cmd)), "deny", cmd)

    # ----- fix 4: CLAUDE_HOOK_LOG_DIR redirects the deny log -----

    def test_deny_log_goes_to_override_dir(self):
        real_before = os.path.getsize(REAL_LOG) if os.path.exists(REAL_LOG) else None
        self.assertEqual(self.decide(self.sh("rm a", self.main)), "deny")
        log = os.path.join(self.logdir, "subagent-worktree.log")
        with open(log, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("general-purpose Bash rm a", text)
        real_after = os.path.getsize(REAL_LOG) if os.path.exists(REAL_LOG) else None
        self.assertEqual(real_before, real_after)

    # ----- heredoc bodies are data; the line that opens them is judged -----

    def test_heredoc_commit_message_in_worktree_allowed(self):
        msg = ("git commit -F - <<'EOF'\n"
               "subagent-worktree: example\n"
               "- redirect > example and X:\\rest and a literal\n"
               "rm -rf /\n"
               "EOF")
        self.assertEqual(self.decide(self.sh(msg, self.wt)), "allow")
        # same message with no worktree cwd: the git command itself is judged by its directory
        self.assertEqual(self.decide(self.sh(msg, self.plain)), "deny")

    def test_heredoc_header_still_judged(self):
        outside = fwd(os.path.join(self.plain, "outside.txt"))  # absolute, outside any worktree
        cmd = "cat > %s <<'EOF'\nbody\nEOF" % outside
        self.assertEqual(self.decide(self.sh(cmd, self.plain)), "deny")
        self.assertEqual(self.decide(self.sh(cmd, self.wt)), "deny")
        self.assertEqual(self.decide(self.sh("cat > %s/out.txt <<'EOF'\nbody\nEOF" % fwd(self.wt), self.plain)), "allow")

    def test_heredoc_dash_tab_terminator(self):
        cmd = "git commit -F - <<-EOF\n\tmsg > X:\\rest\n\trm -rf /\n\tEOF"
        self.assertEqual(self.decide(self.sh(cmd, self.wt)), "allow")
        # a line after the terminator is still a command and is judged
        self.assertEqual(self.decide(self.sh("cat <<-EOF\n\tbody\n\tEOF\nrm a", self.plain)), "deny")
        self.assertEqual(self.decide(self.sh("cat <<-EOF\n\tbody\n\tEOF\nrm a", self.wt)), "allow")

    def test_heredoc_then_command_judged(self):
        self.assertEqual(self.decide(self.sh("cat <<'EOF'\nbody\nEOF\nrm a", self.plain)), "deny")
        self.assertEqual(self.decide(self.sh("cat <<'EOF'\nbody\nEOF\nrm a", self.wt)), "allow")

    def test_here_string_is_not_heredoc(self):
        outside = "C:/Users/test/outside.txt"
        self.assertEqual(self.decide(self.sh("cat <<< hi > %s" % outside, self.plain)), "deny")
        # no body to skip: the next line is a command
        self.assertEqual(self.decide(self.sh("cat <<< hi\nrm a", self.plain)), "deny")

    def test_unquoted_heredoc_substitution_still_judged(self):
        self.assertEqual(self.decide(self.sh("cat <<EOF\n$(rm a)\nEOF", self.plain)), "deny")
        self.assertEqual(self.decide(self.sh("cat <<EOF\n$(rm a)\nEOF", self.wt)), "allow")
        self.assertEqual(self.decide(self.sh("cat <<EOF\n`rm a`\nEOF", self.plain)), "deny")
        # quoted delimiter: $( ) is literal text, so the body is inert
        self.assertEqual(self.decide(self.sh("cat <<'EOF'\n$(rm a)\nEOF", self.plain)), "allow")


if __name__ == "__main__":
    unittest.main()
