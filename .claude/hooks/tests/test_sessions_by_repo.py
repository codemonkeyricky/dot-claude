"""Unit tests for sessions-by-repo.py and the repo lookup in result-capture.py.

Every test uses temp dirs and env overrides (SESSION_REPO_STATE_DIR,
RESULT_CAPTURE_LOG, RESULT_CAPTURE_STATE_DIR); nothing touches the real state.

Run: python3 -I -m unittest discover -s tests -p 'test_sessions_by_repo.py' -v
"""

import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HOOKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORT = os.path.join(HOOKS, "sessions-by-repo.py")
RESULT_HOOK = os.path.join(HOOKS, "result-capture.py")

UTC = datetime.timezone.utc


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def now_minus(days):
    return datetime.datetime.now(UTC) - datetime.timedelta(days=days)


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sessions-by-repo-test-")
        self.state = os.path.join(self.tmp, "session-repo")
        self.log = os.path.join(self.tmp, "completed-work.jsonl")
        os.makedirs(self.state)
        self.env = dict(os.environ)
        self.env["SESSION_REPO_STATE_DIR"] = self.state
        self.env["RESULT_CAPTURE_LOG"] = self.log

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_state(self, sid, updated, repos=None, dominant_root=None, title=None,
                    first_prompt=None):
        repos = repos or {}
        dominant = None
        if dominant_root is not None:
            dominant = {"root": dominant_root, "name": repos[dominant_root]["name"]}
        data = {
            "session_id": sid,
            "transcript_path": None,
            "launch_cwd": None,
            "first_prompt": first_prompt,
            "repos": repos,
            "dominant": dominant,
            "title": {"base": None, "applied": title, "applied_ts": None},
            "updated_ts": iso(updated),
        }
        with open(os.path.join(self.state, sid + ".json"), "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def write_results(self, recs):
        with open(self.log, "w", encoding="utf-8") as fh:
            for r in recs:
                fh.write(json.dumps(r) + "\n")

    def run_report(self, *args):
        proc = subprocess.run(
            [sys.executable, "-I", REPORT] + list(args),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertNotIn(b"Traceback", proc.stderr)
        return proc.stdout.decode("utf-8"), proc.stderr.decode("utf-8")

    def seed_standard(self):
        repo_a = "/home/test/deploy/app"
        repo_b = "/home/test/dev/api"
        self.write_state(
            "aaaa1111-a-new", now_minus(0.1),
            repos={repo_a: {"name": "app", "count": 9, "first_ts": iso(now_minus(1)),
                         "last_ts": iso(now_minus(0.1))},
                   repo_b: {"name": "api", "count": 1, "first_ts": iso(now_minus(1)),
                         "last_ts": iso(now_minus(1))}},
            dominant_root=repo_a, title="fix login redirect")
        self.write_state(
            "bbbb2222-b", now_minus(2),
            repos={repo_b: {"name": "api", "count": 4, "first_ts": iso(now_minus(3)),
                         "last_ts": iso(now_minus(2))}},
            dominant_root=repo_b, first_prompt="  fix the   retry\nbackoff bug  ")
        self.write_state(
            "cccc3333-none", now_minus(5), repos={}, dominant_root=None,
            first_prompt=None)
        self.write_state(
            "dddd4444-old", now_minus(40),
            repos={repo_a: {"name": "app", "count": 2, "first_ts": iso(now_minus(41)),
                         "last_ts": iso(now_minus(40))}},
            dominant_root=repo_a, title="ancient")
        self.write_results([
            {"kind": "result", "ts": iso(now_minus(0.5)), "session_id": "aaaa1111-a-new",
             "cwd": repo_a, "text": "first repo_a result"},
            {"kind": "result", "ts": iso(now_minus(0.4)), "session_id": "aaaa1111-a-new",
             "cwd": repo_a, "text": "second repo_a result"},
            {"kind": "result", "ts": iso(now_minus(0.3)), "session_id": "aaaa1111-a-new",
             "cwd": repo_a, "text": "third repo_a result"},
            {"kind": "task", "ts": iso(now_minus(0.2)), "session_id": "aaaa1111-a-new",
             "text": "not a result"},
            {"kind": "result", "ts": iso(now_minus(2)), "session_id": "bbbb2222-b",
             "cwd": repo_b, "text": "api result"},
        ])

    def test_text_report_groups_by_repo(self):
        self.seed_standard()
        out, _ = self.run_report("--days", "0")
        self.assertIn("app  /home/test/deploy/app  (2 sessions)", out)
        self.assertIn("api  /home/test/dev/api  (1 session)", out)
        self.assertIn("(no repo)  (1 session)", out)
        # session line: local time, [repo] title, sid8, share, also:
        self.assertIn("[app] fix login redirect  aaaa1111  90%  also: api", out)
        self.assertIn("[api] fix the retry backoff bug  bbbb2222  100%", out)
        # newest first: app bucket (0.1 days ago) before api (2 days) before no-repo (5)
        self.assertLess(out.index("app  /home"), out.index("api  /home"))
        self.assertLess(out.index("api  /home"), out.index("(no repo)"))
        # latest 2 results, newest first, under the session
        self.assertIn("      result: third repo_a result\n      result: second repo_a result", out)
        self.assertNotIn("first repo_a result", out)
        self.assertIn("      result: api result", out)

    def test_repo_filter_exact_name(self):
        self.seed_standard()
        out, _ = self.run_report("--days", "0", "--repo", "api")
        self.assertIn("api  /home/test/dev/api", out)
        self.assertNotIn("app  /home", out)
        self.assertNotIn("(no repo)", out)

    def test_repo_filter_root_substring(self):
        self.seed_standard()
        out, _ = self.run_report("--days", "0", "--repo", "deploy/app")
        self.assertIn("app  /home/test/deploy/app", out)
        self.assertNotIn("api  /home", out)

    def test_days_filter_excludes_old_session(self):
        self.seed_standard()
        out, _ = self.run_report("--days", "14")
        self.assertNotIn("ancient", out)
        self.assertIn("fix login redirect", out)
        out_all, _ = self.run_report("--days", "0")
        self.assertIn("ancient", out_all)

    def test_json_shape(self):
        self.seed_standard()
        out, _ = self.run_report("--days", "0", "--json")
        buckets = json.loads(out)
        self.assertIsInstance(buckets, list)
        self.assertEqual([b["name"] for b in buckets],
                         ["app", "api", "(no repo)"])
        repo_a = buckets[0]
        self.assertEqual(repo_a["root"], "/home/test/deploy/app")
        self.assertEqual(set(repo_a.keys()), {"name", "root", "sessions"})
        s = repo_a["sessions"][0]
        self.assertEqual(set(s.keys()), {"session_id", "title", "updated_ts", "share",
                                         "other_repos", "results"})
        self.assertEqual(s["session_id"], "aaaa1111-a-new")
        self.assertEqual(s["title"], "fix login redirect")
        self.assertEqual(s["share"], 90)
        self.assertEqual(s["other_repos"], ["api"])
        self.assertEqual(s["results"], ["third repo_a result", "second repo_a result"])
        nr = buckets[2]
        self.assertIsNone(nr["root"])
        self.assertEqual(nr["sessions"][0]["title"], "(untitled)")
        self.assertIsNone(nr["sessions"][0]["share"])

    def test_json_empty(self):
        out, _ = self.run_report("--json")
        self.assertEqual(json.loads(out), [])

    def test_empty_text_says_no_sessions(self):
        out, _ = self.run_report("--days", "0")
        self.assertEqual(out.strip(), "No sessions found.")

    def test_missing_state_dir_is_not_an_error(self):
        self.env["SESSION_REPO_STATE_DIR"] = os.path.join(self.tmp, "does-not-exist")
        out, _ = self.run_report("--days", "0")
        self.assertIn("No sessions found.", out)

    def test_corrupt_state_file_skipped_with_warning(self):
        self.seed_standard()
        with open(os.path.join(self.state, "eeee5555-bad.json"), "w") as fh:
            fh.write("{not json")
        with open(os.path.join(self.state, "ffff6666.lock"), "w") as fh:
            fh.write("")
        out, err = self.run_report("--days", "0", "--json")
        self.assertIn("eeee5555-bad.json", err)
        self.assertIn("skipping corrupt", err)
        self.assertEqual(len(json.loads(out)), 3)

    def test_results_joined_by_session_id(self):
        self.write_state("gggg7777-x", now_minus(0.1),
                         repos={"/r/x": {"name": "x", "count": 1, "first_ts": iso(now_minus(1)),
                                         "last_ts": iso(now_minus(0.1))}},
                         dominant_root="/r/x", title="t")
        self.write_state("hhhh8888-y", now_minus(0.2),
                         repos={"/r/y": {"name": "y", "count": 1, "first_ts": iso(now_minus(1)),
                                         "last_ts": iso(now_minus(0.2))}},
                         dominant_root="/r/y", title="u")
        self.write_results([
            {"kind": "result", "ts": iso(now_minus(0.1)), "session_id": "hhhh8888-y",
             "text": "only for y"},
            {"kind": "result", "ts": iso(now_minus(0.1)), "session_id": "unknown-sid",
             "text": "orphan"},
        ])
        with open(self.log, "a", encoding="utf-8") as fh:
            fh.write("this is not json\n")
        out, _ = self.run_report("--days", "0", "--json")
        by_sid = {s["session_id"]: s for b in json.loads(out) for s in b["sessions"]}
        self.assertEqual(by_sid["hhhh8888-y"]["results"], ["only for y"])
        self.assertEqual(by_sid["gggg7777-x"]["results"], [])

    def test_result_text_truncated_to_120(self):
        self.write_state("iiii9999", now_minus(0.1),
                         repos={"/r/z": {"name": "z", "count": 1, "first_ts": iso(now_minus(1)),
                                         "last_ts": iso(now_minus(0.1))}},
                         dominant_root="/r/z", title="long")
        self.write_results([{"kind": "result", "ts": iso(now_minus(0.1)),
                             "session_id": "iiii9999", "text": "x" * 300}])
        out, _ = self.run_report("--days", "0")
        line = [l for l in out.splitlines() if "result:" in l][0]
        self.assertEqual(line.split("result: ", 1)[1], "x" * 120 + "…")


class ResultCaptureRepoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="result-capture-repo-test-")
        self.log = os.path.join(self.tmp, "log", "completed-work.jsonl")
        self.cap_state = os.path.join(self.tmp, "cap-state")
        self.repo_state = os.path.join(self.tmp, "session-repo")
        os.makedirs(self.repo_state)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_hook(self, payload):
        env = dict(os.environ)
        env["RESULT_CAPTURE_LOG"] = self.log
        env["RESULT_CAPTURE_STATE_DIR"] = self.cap_state
        env["SESSION_REPO_STATE_DIR"] = self.repo_state
        proc = subprocess.run(
            [sys.executable, "-I", RESULT_HOOK],
            input=json.dumps(payload).encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertEqual(proc.stdout, b"")

    def read_records(self):
        with open(self.log, "r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_repo_set_from_state_file(self):
        with open(os.path.join(self.repo_state, "sess-repo.json"), "w", encoding="utf-8") as fh:
            json.dump({"session_id": "sess-repo", "dominant": {"root": "/work/repo_a",
                                                              "name": "app"}}, fh)
        self.run_hook({"session_id": "sess-repo", "cwd": "/work/repo_a",
                       "last_assistant_message": "done\nresult: X"})
        rec = self.read_records()[0]
        self.assertEqual(rec["kind"], "result")
        self.assertEqual(rec["text"], "X")
        self.assertEqual(rec["repo"], "app")
        self.assertEqual(rec["repo_root"], "/work/repo_a")

    def test_repo_null_when_no_state_file(self):
        self.run_hook({"session_id": "sess-none", "cwd": "/work/none",
                       "last_assistant_message": "done\nresult: Y"})
        rec = self.read_records()[0]
        self.assertEqual(rec["text"], "Y")
        self.assertIsNone(rec["repo"])
        self.assertIsNone(rec["repo_root"])

    def test_repo_null_when_state_file_corrupt(self):
        with open(os.path.join(self.repo_state, "sess-bad.json"), "w") as fh:
            fh.write("{broken")
        self.run_hook({"session_id": "sess-bad", "cwd": "/w",
                       "last_assistant_message": "result: Z"})
        rec = self.read_records()[0]
        self.assertEqual(rec["text"], "Z")
        self.assertIsNone(rec["repo"])

    def test_sanitized_sid_matches_state_file(self):
        with open(os.path.join(self.repo_state, "a_b_c.json"), "w", encoding="utf-8") as fh:
            json.dump({"dominant": {"root": "/r", "name": "r"}}, fh)
        self.run_hook({"session_id": "a/b:c", "cwd": "/w",
                       "last_assistant_message": "result: W"})
        rec = self.read_records()[0]
        self.assertEqual(rec["repo"], "r")


if __name__ == "__main__":
    unittest.main()
