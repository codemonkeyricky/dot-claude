import json, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

HOME = os.path.expanduser("~")
HOOK = str(Path(__file__).resolve().parent / "ready-gate.py")  # the worktree copy
HEAD = "a1b2c3d4" * 5          # current head sha of PR 12 in the fake gh
OLD = "0f" * 20               # an older commit
FAKE_GH_SRC = '''import json, os, sys
argv_file = os.environ.get("FAKE_GH_ARGV")
if argv_file:
    with open(argv_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(sys.argv[1:]) + "\\n")
sys.stdout.write(os.environ.get("FAKE_GH_OUT", ""))
sys.stderr.write(os.environ.get("FAKE_GH_ERR", ""))
sys.exit(int(os.environ.get("FAKE_GH_RC") or 0))
'''


def view_json(pr, sha, repo="Owner/Repo"):
    return json.dumps({"number": pr, "headRefOid": sha,
                       "url": "https://github.com/%s/pull/%d" % (repo, pr)})


def decision(r):
    return json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] if r.stdout.strip() else "allow"


def reason(r):
    return json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]


class T(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.records = os.path.join(self.root, "pr-reviews.jsonl")
        self.log = os.path.join(self.root, "ready-gate.log")
        self.argv = os.path.join(self.root, "argv.jsonl")
        self.fake = os.path.join(self.root, "fake_gh.py")
        with open(self.fake, "w", encoding="utf-8") as f:
            f.write(FAKE_GH_SRC)
        self.gh_out = view_json(12, HEAD)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _run(self, raw, env=None):
        e = dict(os.environ)
        for k in ("CLAUDE_SKIP_READY_REVIEW", "READY_GATE_GH_CMD",
                  "FAKE_GH_OUT", "FAKE_GH_RC", "FAKE_GH_ERR", "FAKE_GH_ARGV"):
            e.pop(k, None)
        e["PR_REVIEWS_LOG"] = self.records
        e["READY_GATE_LOG"] = self.log
        e["READY_GATE_GH_CMD"] = json.dumps([sys.executable, self.fake])
        e["FAKE_GH_ARGV"] = self.argv
        e["FAKE_GH_OUT"] = self.gh_out
        e.update(env or {})
        r = subprocess.run([sys.executable, "-I", HOOK], input=raw, capture_output=True, text=True, env=e)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def call(self, tool, ti, env=None, agent_id=None):
        p = {"session_id": "sA", "hook_event_name": "PreToolUse", "tool_name": tool,
             "tool_input": ti, "tool_use_id": "toolu_x", "cwd": self.root}
        if agent_id:
            p["agent_id"] = agent_id
        return self._run(json.dumps(p), env)

    def bash(self, command, tool="Bash", env=None, agent_id=None):
        return self.call(tool, {"command": command}, env, agent_id)

    def rec(self, verdict, sha=HEAD, pr=12, repo="owner/repo"):
        return {"ts": "2026-11-15T10:00:00Z", "repo": repo, "pr": pr, "sha": sha,
                "verdict": verdict, "agent_type": "pr-reviewer-1", "agent_id": "a1", "session_id": "sR"}

    def write_records(self, *items):
        with open(self.records, "w", encoding="utf-8") as f:
            for it in items:
                f.write((it if isinstance(it, str) else json.dumps(it)) + "\n")

    def gh_calls(self):
        if not os.path.exists(self.argv):
            return []
        with open(self.argv, encoding="utf-8") as f:
            return [json.loads(x) for x in f.read().splitlines()]

    def log_text(self):
        if not os.path.exists(self.log):
            return ""
        with open(self.log, encoding="utf-8") as f:
            return f.read()

    # 1
    def test_01_approve_at_head_allows(self):
        self.write_records(self.rec("APPROVE"))
        r = self.bash("gh pr ready 12")
        self.assertEqual(decision(r), "allow")
        self.assertEqual(r.stdout, "")
        self.assertEqual(self.gh_calls()[0][:2], ["pr", "view"])

    # 2
    def test_02_no_record_denied_mentions_reviewer(self):
        self.write_records()
        r = self.bash("gh pr ready 12")
        self.assertEqual(decision(r), "deny")
        self.assertIn("pr-reviewer", reason(r))
        self.assertIn("owner/repo#12", reason(r))
        self.assertIn("latest verdict: none", reason(r))

    def test_02b_missing_records_file_denied(self):
        self.assertEqual(decision(self.bash("gh pr ready 12")), "deny")

    # 3
    def test_03_approve_for_older_sha_denied(self):
        self.write_records(self.rec("APPROVE", sha=OLD))
        self.assertEqual(decision(self.bash("gh pr ready 12")), "deny")

    # 4
    def test_04_last_record_in_file_order_wins(self):
        self.write_records(self.rec("APPROVE"), self.rec("CHANGES"))
        r = self.bash("gh pr ready 12")
        self.assertEqual(decision(r), "deny")
        self.assertIn("latest verdict: CHANGES", reason(r))
        self.write_records(self.rec("CHANGES"), self.rec("APPROVE"))
        self.assertEqual(decision(self.bash("gh pr ready 12")), "allow")

    # 5
    def test_05_undo_allowed_without_calling_gh(self):
        self.write_records()
        self.assertEqual(decision(self.bash("gh pr ready 12 --undo")), "allow")
        self.assertEqual(self.gh_calls(), [])

    # 6
    def test_06_repo_flag_matched_case_insensitively(self):
        self.write_records(self.rec("APPROVE", repo="owner/repo"))
        r = self.bash("gh pr ready -R Owner/Repo 12")
        self.assertEqual(decision(r), "allow")
        argv = self.gh_calls()[0]
        for tok in ("-R", "Owner/Repo", "12"):
            self.assertIn(tok, argv)
        self.assertEqual(decision(self.bash("gh pr ready --repo=Owner/Repo 12")), "allow")

    # 7
    def test_07_no_ref_passes_no_ref_to_gh(self):
        self.write_records(self.rec("APPROVE"))
        self.assertEqual(decision(self.bash("gh pr ready")), "allow")
        self.assertEqual(self.gh_calls()[0], ["pr", "view", "--json", "number,headRefOid,url"])

    # 8
    def test_08_chained_ready_gated_other_gh_and_git_allowed(self):
        self.gh_out = view_json(7, HEAD)
        self.write_records()
        r = self.bash("git push && gh pr ready 7")
        self.assertEqual(decision(r), "deny")
        self.assertIn("owner/repo#7", reason(r))
        self.assertEqual(self.bash("gh pr view 7").stdout, "")
        self.assertEqual(self.bash("git status").stdout, "")
        self.write_records(self.rec("APPROVE", pr=7))
        self.assertEqual(decision(self.bash("git push && gh pr ready 7")), "allow")

    # 9
    def test_09_subagent_payload_still_gated(self):
        self.write_records()
        self.assertEqual(decision(self.bash("gh pr ready 12", agent_id="agent-9")), "deny")

    # 10
    def test_10_powershell_gated(self):
        self.write_records()
        self.assertEqual(decision(self.bash("gh pr ready 12", tool="PowerShell")), "deny")

    # 11
    def test_11_gh_exit_1_denied_could_not_resolve(self):
        self.write_records(self.rec("APPROVE"))
        r = self.bash("gh pr ready 12", env={"FAKE_GH_RC": "1", "FAKE_GH_ERR": "no pull request found"})
        self.assertEqual(decision(r), "deny")
        self.assertIn("could not resolve", reason(r))
        self.assertIn("no pull request found", reason(r))

    def test_11b_unparseable_gh_output_denied(self):
        self.write_records(self.rec("APPROVE"))
        r = self.bash("gh pr ready 12", env={"FAKE_GH_OUT": "not json"})
        self.assertEqual(decision(r), "deny")
        self.assertIn("could not resolve", reason(r))

    # 12
    def test_12_pr_create_needs_draft(self):
        r = self.bash("gh pr create --title x")
        self.assertEqual(decision(r), "deny")
        self.assertIn("Create PRs as drafts", reason(r))
        self.assertEqual(self.bash("gh pr create --draft --title x").stdout, "")
        self.assertEqual(self.bash("gh pr create -d -t x").stdout, "")

    # 13
    def test_13_graphql_mark_ready_denied(self):
        cmd = ('gh api graphql -f query=\'mutation { markPullRequestReadyForReview'
               '(input:{pullRequestId:"X"}) { pullRequest { number } } }\'')
        r = self.bash(cmd)
        self.assertEqual(decision(r), "deny")
        self.assertIn("gh pr ready", reason(r))

    # 14
    def test_14_records_file_write_protected(self):
        r = self.call("Write", {"file_path": "C:/x/.claude/hooks/state/pr-reviews.jsonl", "content": "{}"})
        self.assertEqual(decision(r), "deny")
        self.assertIn("written only by the review-record hook", reason(r))
        self.assertEqual(decision(self.call("NotebookEdit", {"notebook_path": "C:/x/PR-Reviews.jsonl"})), "deny")
        self.assertEqual(decision(self.call("Edit", {"file_path": "C:/x/notes.md"})), "allow")
        self.assertEqual(self.call("Write", {"file_path": os.path.join(self.root, "notes.md"),
                                             "content": "x"}).stdout, "")

    # 15
    def test_15_bash_mention_of_records_denied(self):
        r = self.bash("echo {} >> ~/.claude/hooks/state/PR-REVIEWS.jsonl")
        self.assertEqual(decision(r), "deny")
        self.assertIn("Read tool", reason(r))

    # 16
    def test_16_skip_env_allows(self):
        self.write_records()
        self.assertEqual(self.bash("gh pr ready 12", env={"CLAUDE_SKIP_READY_REVIEW": "1"}).stdout, "")

    # 18
    def test_18_garbage_stdin_allows(self):
        for raw in ["not json", "", "[1,2]"]:
            self.assertEqual(self._run(raw).stdout, "", repr(raw))

    # 19
    def test_19_malformed_records_skipped(self):
        self.write_records(self.rec("APPROVE"), "garbage{", "[1]",
                           {"repo": "owner/repo", "pr": "12", "sha": HEAD, "verdict": "CHANGES"},
                           {"repo": "owner/repo", "pr": True, "sha": HEAD, "verdict": "CHANGES"})
        self.assertEqual(decision(self.bash("gh pr ready 12")), "allow")

    # 20
    def test_20_denial_logged_as_one_line(self):
        self.write_records()
        self.bash("gh pr ready 12")
        lines = self.log_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("DENY Bash", lines[0])

    # 21
    def test_21_broken_gh_cmd_env_fails_closed(self):
        self.write_records(self.rec("APPROVE"))
        r = self.bash("gh pr ready 12", env={"READY_GATE_GH_CMD": "not json"})
        self.assertEqual(decision(r), "deny")
        self.assertIn("ready-gate error", reason(r))

    # 22
    def test_22_gh_exe_and_newline_segments_gated(self):
        self.write_records()
        self.assertEqual(decision(self.bash("gh.exe pr ready 12")), "deny")
        self.assertEqual(decision(self.bash("echo hi\ngh pr ready 12")), "deny")
        self.assertEqual(self.bash("echo hi").stdout, "")

    # 23: gh as the program of a segment (after wrappers) is still gated
    def test_23_gh_program_with_wrappers_gated(self):
        self.write_records()
        for cmd in ["gh pr ready 12", "  gh pr ready 12", "GH_TOKEN=x gh pr ready 12",
                    "cd C:/repo && gh pr ready 12", "& gh pr ready 12",
                    '& "C:/Program Files/GitHub CLI/gh.exe" pr ready 12',
                    "gh.exe pr ready 12", "env GH_TOKEN=x command gh pr ready 12"]:
            r = self.bash(cmd)
            self.assertEqual(decision(r), "deny", cmd)
            self.assertIn("owner/repo#12", reason(r), cmd)
        self.assertEqual(decision(self.bash("gh pr create --title x")), "deny")

    # 24: text that merely contains gh pr ready / gh pr create is not gated
    def test_24_text_mentioning_gh_pr_ready_not_gated(self):
        self.write_records()
        for cmd in ['echo "then gh pr ready 12"',
                    'grep -n "gh pr ready" notes.md',
                    'git commit -m "docs: mention gh pr create"',
                    "cat > f.md <<'EOF'\nRun gh pr ready after review.\nEOF",
                    "python -c \"print('gh pr ready')\""]:
            r = self.bash(cmd)
            self.assertEqual(r.stdout, "", cmd)
        self.assertEqual(self.gh_calls(), [])

    # 25: review round cap (3 CHANGES verdicts per PR, any sha)
    def test_25_zero_changes_is_round_one(self):
        self.write_records()
        r = self.bash("gh pr ready 12")
        self.assertEqual(decision(r), "deny")
        self.assertIn("round 1 of 3", reason(r))

    def test_26_two_changes_different_shas_is_round_three(self):
        self.write_records(*[self.rec("CHANGES", sha="%040x" % i) for i in range(1, 3)])
        r = self.bash("gh pr ready 12")
        self.assertEqual(decision(r), "deny")
        self.assertIn("round 3 of 3", reason(r))

    def test_27_three_changes_capped(self):
        self.write_records(*[self.rec("CHANGES", sha="%040x" % i) for i in range(1, 4)])
        r = self.bash("gh pr ready 12")
        self.assertEqual(decision(r), "deny")
        self.assertIn("capped at 3 rounds", reason(r))
        self.assertIn("ask the user", reason(r))
        self.assertNotIn("round", reason(r).replace("rounds", ""))

    def test_28_three_changes_then_approve_for_head_allowed(self):
        self.write_records(*[self.rec("CHANGES", sha="%040x" % i) for i in range(1, 4)],
                           self.rec("APPROVE"))
        self.assertEqual(self.bash("gh pr ready 12").stdout, "")
        self.assertEqual(decision(self.bash("gh pr ready 12")), "allow")

    def test_29_changes_for_other_pr_not_counted(self):
        self.write_records(*[self.rec("CHANGES", pr=99) for _ in range(3)],
                           *[self.rec("CHANGES", pr=12, repo="owner/other") for _ in range(3)])
        r = self.bash("gh pr ready 12")
        self.assertEqual(decision(r), "deny")
        self.assertIn("round 1 of 3", reason(r))


if __name__ == "__main__":
    unittest.main()
