"""Tests for hooks/session-repo.py (this repo copy). Run with:
python3 -I -m unittest discover -s hooks/tests -p 'test_session_repo.py' -v
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "session-repo.py"))


def _git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t",
         "-c", "commit.gpgsign=false", *args],
        cwd=cwd, check=True, capture_output=True,
    )


class SessionRepoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="session-repo-test-")
        self.state_dir = os.path.join(self.tmp, "state", "session-repo")
        self.repo_a = os.path.join(self.tmp, "repo-a")
        self.repo_b = os.path.join(self.tmp, "repo-b")
        self.wt_a = os.path.join(self.tmp, "wt-a")
        self.plain = os.path.join(self.tmp, "plain")
        for d in (self.repo_a, self.repo_b, self.plain):
            os.makedirs(d)
        _git(self.repo_a, "init", "-q", "-b", "master")
        with open(os.path.join(self.repo_a, "a.txt"), "w") as fh:
            fh.write("a\n")
        _git(self.repo_a, "add", "a.txt")
        _git(self.repo_a, "commit", "-q", "-m", "init")
        _git(self.repo_a, "worktree", "add", "-q", "-b", "wt-branch", self.wt_a)
        _git(self.repo_b, "init", "-q", "-b", "master")
        _git(self.repo_b, "commit", "-q", "--allow-empty", "-m", "init")
        with open(os.path.join(self.plain, "notes.txt"), "w") as fh:
            fh.write("n\n")
        self.name_a = "repo-a"
        self.real_a = os.path.realpath(self.repo_a)
        self.real_b = os.path.realpath(self.repo_b)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # helpers
    def run_hook(self, payload=None, raw=None, extra_env=None):
        env = dict(os.environ)
        env["SESSION_REPO_STATE_DIR"] = self.state_dir
        env.pop("SESSION_REPO_TITLE", None)
        if extra_env:
            env.update(extra_env)
        data = raw if raw is not None else json.dumps(payload).encode("utf-8")
        return subprocess.run([sys.executable, "-I", SCRIPT], input=data,
                              capture_output=True, env=env, timeout=30)

    def ev(self, event, sid="sess-1", **kw):
        p = {"hook_event_name": event, "session_id": sid,
             "transcript_path": "/x/t.jsonl", "cwd": self.plain}
        p.update(kw)
        return p

    def load(self, sid="sess-1"):
        path = os.path.join(self.state_dir, sid + ".json")
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def repos(self, sid="sess-1"):
        st = self.load(sid)
        return st["repos"] if st else {}

    def title_of(self, proc):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout, "expected title output")
        return json.loads(proc.stdout)["hookSpecificOutput"]

    # 1
    def test_read_counts_repo(self):
        p = self.run_hook(self.ev("PostToolUse", tool_name="Read",
                                  tool_input={"file_path": os.path.join(self.repo_a, "a.txt")}))
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout, b"")
        self.assertEqual(self.repos()[self.real_a]["count"], 1)
        self.assertEqual(self.repos()[self.real_a]["name"], self.name_a)

    # 2
    def test_bash_cd_counts_target(self):
        p = self.run_hook(self.ev("PostToolUse", tool_name="Bash",
                                  tool_input={"command": f"cd {self.repo_b} && ls"}))
        self.assertEqual(p.returncode, 0)
        self.assertIn(self.real_b, self.repos())
        self.assertEqual(self.repos()[self.real_b]["count"], 1)

    # 3
    def test_worktree_edit_counts_main_root(self):
        p = self.run_hook(self.ev("PostToolUse", tool_name="Edit",
                                  tool_input={"file_path": os.path.join(self.wt_a, "a.txt")}))
        self.assertEqual(p.returncode, 0)
        repos = self.repos()
        self.assertIn(self.real_a, repos)
        self.assertNotIn(os.path.realpath(self.wt_a), repos)

    # 4
    def test_non_repo_path_counts_nothing(self):
        self.run_hook(self.ev("PostToolUse", tool_name="Read",
                              tool_input={"file_path": os.path.join(self.plain, "notes.txt")}))
        self.assertEqual(self.repos(), {})

    # 5
    def test_missing_file_inside_repo_counts_repo(self):
        self.run_hook(self.ev("PostToolUse", tool_name="Write",
                              tool_input={"file_path": os.path.join(self.repo_a, "missing", "new.txt")}))
        self.assertEqual(self.repos()[self.real_a]["count"], 1)

    # 6
    def test_prompt_becomes_title(self):
        p = self.run_hook(self.ev("UserPromptSubmit", cwd=self.repo_a,
                                  prompt="fix the login redirect loop on logout"))
        out = self.title_of(p)
        self.assertEqual(out["hookEventName"], "UserPromptSubmit")
        self.assertEqual(out["sessionTitle"], f"[{self.name_a}] fix the login redirect loop on logout")

    def test_prompt_title_truncated_at_50(self):
        prompt = "refactor the payment retry queue so failed charges back off correctly"
        p = self.run_hook(self.ev("UserPromptSubmit", cwd=self.repo_a, prompt=prompt))
        expected = prompt[:50].rstrip()
        self.assertEqual(self.title_of(p)["sessionTitle"], f"[{self.name_a}] {expected}")

    # 7
    def test_existing_title_kept_with_prefix(self):
        p = self.run_hook(self.ev("SessionStart", cwd=self.repo_a, session_title="my thing"))
        self.assertEqual(self.title_of(p)["sessionTitle"], f"[{self.name_a}] my thing")

    # 8
    def test_other_repo_prefix_replaced(self):
        p = self.run_hook(self.ev("SessionStart", cwd=self.repo_a, session_title="[repo-b] my thing"))
        self.assertEqual(self.title_of(p)["sessionTitle"], f"[{self.name_a}] my thing")

    # 9
    def test_correct_title_no_output(self):
        p = self.run_hook(self.ev("SessionStart", cwd=self.repo_a, session_title=f"[{self.name_a}] my thing"))
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout, b"")

    # 10
    def test_title_disabled_by_env(self):
        p = self.run_hook(self.ev("SessionStart", cwd=self.repo_a, session_title="my thing"),
                          extra_env={"SESSION_REPO_TITLE": "0"})
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout, b"")

    # 11
    def test_garbage_stdin_fails_open(self):
        for raw in (b"not json {", b"", b"[1, 2]"):
            p = self.run_hook(raw=raw)
            self.assertEqual(p.returncode, 0, raw)
            self.assertEqual(p.stdout, b"", raw)

    # 12
    def test_post_tool_use_never_prints(self):
        p = self.run_hook(self.ev("PostToolUse", cwd=self.repo_a, tool_name="Read",
                                  tool_input={"file_path": os.path.join(self.repo_a, "a.txt")}))
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout, b"")

    # cwd change counts the new directory only
    def test_cwd_changed_counts_new_cwd(self):
        p = self.run_hook(self.ev("CwdChanged", old_cwd=self.plain, new_cwd=self.repo_b))
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout, b"")
        self.assertEqual(self.repos()[self.real_b]["count"], 1)
        self.assertNotIn(self.real_a, self.repos())

    # 13
    def test_perf_fifty_post_tool_use_calls(self):
        payload = self.ev("PostToolUse", cwd=self.repo_a, tool_name="Read",
                          tool_input={"file_path": os.path.join(self.repo_a, "a.txt")})
        times = []
        for _ in range(50):
            t0 = time.perf_counter()
            p = self.run_hook(payload)
            times.append(time.perf_counter() - t0)
            self.assertEqual(p.returncode, 0)
        avg_ms = sum(times) / len(times) * 1000
        self.assertLess(avg_ms, 100, f"average {avg_ms:.1f} ms")
        self.assertEqual(self.repos()[self.real_a]["count"], 50)

    # --- notification wrappers and AI titles

    def _transcript(self, lines):
        path = os.path.join(self.tmp, "transcript.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            for obj in lines:
                fh.write(json.dumps(obj) + "\n")
        return path

    def test_task_notification_not_stored_as_first_prompt(self):
        note = "<task-notification>\nbackground job finished\n</task-notification>"
        self.run_hook(self.ev("UserPromptSubmit", cwd=self.repo_a, prompt=note))
        self.assertIsNone(self.load().get("first_prompt"))
        self.run_hook(self.ev("UserPromptSubmit", cwd=self.repo_a,
                              prompt="  fix the login tab  "))
        self.assertEqual(self.load()["first_prompt"], "fix the login tab")

    def test_ai_title_from_transcript(self):
        tp = self._transcript([{"type": "user", "message": "hi"},
                               {"type": "ai-title", "aiTitle": "foo-bar"}])
        p = self.run_hook(self.ev("SessionStart", cwd=self.repo_a, transcript_path=tp))
        self.assertEqual(self.title_of(p)["sessionTitle"], f"[{self.name_a}] foo-bar")

    def test_custom_title_beats_later_ai_title(self):
        tp = self._transcript([{"type": "custom-title", "customTitle": "[repo-b] mine"},
                               {"type": "ai-title", "aiTitle": "foo-bar"}])
        p = self.run_hook(self.ev("SessionStart", cwd=self.repo_a, transcript_path=tp))
        self.assertEqual(self.title_of(p)["sessionTitle"], f"[{self.name_a}] mine")

    def test_bad_transcript_lines_are_skipped(self):
        path = os.path.join(self.tmp, "bad.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json\n\n[1,2]\n")
            fh.write(json.dumps({"type": "ai-title", "aiTitle": "ok-title"}) + "\n")
        p = self.run_hook(self.ev("SessionStart", cwd=self.repo_a, transcript_path=path))
        self.assertEqual(self.title_of(p)["sessionTitle"], f"[{self.name_a}] ok-title")

    def test_no_base_anywhere_emits_nothing(self):
        p = self.run_hook(self.ev("SessionStart", cwd=self.repo_a,
                                  transcript_path=os.path.join(self.tmp, "absent.jsonl")))
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout, b"")

    def test_applied_title_not_rewritten_when_current_empty(self):
        tp = self._transcript([{"type": "ai-title", "aiTitle": "foo"}])
        p1 = self.run_hook(self.ev("SessionStart", cwd=self.repo_a, transcript_path=tp))
        self.assertEqual(self.title_of(p1)["sessionTitle"], f"[{self.name_a}] foo")
        p2 = self.run_hook(self.ev("UserPromptSubmit", cwd=self.repo_a, prompt="next"))
        self.assertEqual(p2.returncode, 0)
        self.assertEqual(p2.stdout, b"")

    # --- git branch as title base

    def _checkout(self, *args):
        _git(self.repo_a, "checkout", "-q", *args)

    def test_empty_current_retitles_on_branch_switch(self):
        self._checkout("-b", "feature-x")
        p1 = self.run_hook(self.ev("SessionStart", cwd=self.repo_a, session_title=""))
        self.assertEqual(self.title_of(p1)["sessionTitle"], f"[{self.name_a}] feature-x")
        self._checkout("-b", "feature-y")
        time.sleep(1.1)  # branch timestamps are whole seconds
        p2 = self.run_hook(self.ev("UserPromptSubmit", cwd=self.repo_a, prompt="next",
                                   session_title=""))
        self.assertEqual(self.title_of(p2)["sessionTitle"], f"[{self.name_a}] feature-y")

    def test_ai_title_then_new_branch_retitles_on_empty_current(self):
        tp = self._transcript([{"type": "ai-title", "aiTitle": "foo"}])
        p1 = self.run_hook(self.ev("SessionStart", cwd=self.repo_a, transcript_path=tp,
                                   session_title=""))
        self.assertEqual(self.title_of(p1)["sessionTitle"], f"[{self.name_a}] foo")
        self._checkout("-b", "feature-z")
        p2 = self.run_hook(self.ev("UserPromptSubmit", cwd=self.repo_a, prompt="go",
                                   session_title=""))
        self.assertEqual(self.title_of(p2)["sessionTitle"], f"[{self.name_a}] feature-z")

    def test_same_branch_again_with_empty_current_emits_nothing(self):
        self._checkout("-b", "feature-x")
        self.run_hook(self.ev("SessionStart", cwd=self.repo_a, session_title=""))
        p2 = self.run_hook(self.ev("UserPromptSubmit", cwd=self.repo_a, prompt="again",
                                   session_title=""))
        self.assertEqual(p2.returncode, 0)
        self.assertEqual(p2.stdout, b"")

    def test_feature_branch_is_title_base(self):
        self._checkout("-b", "feature-x")
        p = self.run_hook(self.ev("SessionStart", cwd=self.repo_a))
        self.assertEqual(self.title_of(p)["sessionTitle"], f"[{self.name_a}] feature-x")
        self.assertEqual(self.load()["repos"][self.real_a]["branches"]["feature-x"]["count"], 1)

    def test_branch_switch_renames_hook_title(self):
        self._checkout("-b", "feature-x")
        p1 = self.run_hook(self.ev("SessionStart", cwd=self.repo_a))
        self.assertEqual(self.title_of(p1)["sessionTitle"], f"[{self.name_a}] feature-x")
        self._checkout("-b", "feature-y")
        time.sleep(1.1)  # branch timestamps are whole seconds; make feature-y strictly newer
        p2 = self.run_hook(self.ev("UserPromptSubmit", cwd=self.repo_a, prompt="next",
                                   session_title=f"[{self.name_a}] feature-x"))
        self.assertEqual(self.title_of(p2)["sessionTitle"], f"[{self.name_a}] feature-y")

    def test_user_title_beats_branch(self):
        self._checkout("-b", "feature-x")
        p = self.run_hook(self.ev("SessionStart", cwd=self.repo_a, session_title="my thing"))
        self.assertEqual(self.title_of(p)["sessionTitle"], f"[{self.name_a}] my thing")

    def test_master_only_uses_ai_title(self):
        tp = self._transcript([{"type": "ai-title", "aiTitle": "foo"}])
        p = self.run_hook(self.ev("SessionStart", cwd=self.repo_a, transcript_path=tp))
        self.assertEqual(self.title_of(p)["sessionTitle"], f"[{self.name_a}] foo")
        self.assertEqual(self.load()["title"]["fallback"], "foo")

    def test_detached_head_falls_back_to_prompt(self):
        self._checkout("--detach")
        p = self.run_hook(self.ev("UserPromptSubmit", cwd=self.repo_a, prompt="fix login"))
        self.assertEqual(self.title_of(p)["sessionTitle"], f"[{self.name_a}] fix login")
        self.assertIsNone(self.load()["repos"][self.real_a].get("branches", {}).get("feature-x"))

    def test_worktree_branch_beats_later_master(self):
        self.run_hook(self.ev("PostToolUse", tool_name="Edit",
                              tool_input={"file_path": os.path.join(self.wt_a, "a.txt")}))
        self.run_hook(self.ev("PostToolUse", tool_name="Read",
                              tool_input={"file_path": os.path.join(self.repo_a, "a.txt")}))
        p = self.run_hook(self.ev("UserPromptSubmit", cwd=self.repo_a, prompt="go"))
        self.assertEqual(self.title_of(p)["sessionTitle"], f"[{self.name_a}] wt-branch")
        branches = self.load()["repos"][self.real_a]["branches"]
        self.assertEqual(branches["wt-branch"]["count"], 1)
        self.assertEqual(branches["master"]["count"], 2)

    # --- healing states written by the pre-fix version

    def test_old_state_with_wrapper_title_heals_from_ai_title(self):
        os.makedirs(self.state_dir, exist_ok=True)
        old = {
            "session_id": "sess-1",
            "transcript_path": None,
            "launch_cwd": self.plain,
            "first_prompt": "<task-notification> x",
            "repos": {self.real_a: {"name": self.name_a, "count": 3,
                                    "first_ts": "2026-11-15T00:00:00Z",
                                    "last_ts": "2026-11-15T00:00:00Z"}},
            "dominant": {"root": self.real_a, "name": self.name_a},
            "title": {"base": "<task-notification> x",
                      "applied": f"[{self.name_a}] <task-notification> x",
                      "applied_ts": "2026-11-15T00:00:00Z"},
            "updated_ts": "2026-11-15T00:00:00Z",
        }
        with open(os.path.join(self.state_dir, "sess-1.json"), "w", encoding="utf-8") as fh:
            json.dump(old, fh)
        tp = self._transcript([
            {"type": "ai-title", "aiTitle": "real-name"},
            {"type": "custom-title", "customTitle": f"[{self.name_a}] <task-notification> x"},
        ])
        p = self.run_hook(self.ev("UserPromptSubmit", cwd=self.repo_a, prompt="next",
                                  session_title="", transcript_path=tp))
        self.assertEqual(self.title_of(p)["sessionTitle"], f"[{self.name_a}] real-name")

    def test_user_custom_title_beats_ai_title(self):
        tp = self._transcript([
            {"type": "ai-title", "aiTitle": "real-name"},
            {"type": "custom-title", "customTitle": "my name"},
        ])
        p = self.run_hook(self.ev("SessionStart", cwd=self.repo_a, session_title="",
                                  transcript_path=tp))
        self.assertEqual(self.title_of(p)["sessionTitle"], f"[{self.name_a}] my name")


if __name__ == "__main__":
    unittest.main()
