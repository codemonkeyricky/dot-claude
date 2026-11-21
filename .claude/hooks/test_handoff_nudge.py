"""Tests for handoff-nudge.py (SessionStart, matcher "compact").

Each test runs the hook as a subprocess with HANDOFF_NUDGE_STATE_DIR pointed into a
temp directory.

Run: python3 -m pytest -q .claude/hooks/test_handoff_nudge.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "handoff-nudge.py")


class HandoffNudgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="handoff-nudge-test-")
        self.state_dir = os.path.join(self.tmp, "state")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_raw(self, stdin_bytes, extra_env=None):
        env = dict(os.environ)
        env["HANDOFF_NUDGE_STATE_DIR"] = self.state_dir
        env.pop("HANDOFF_NUDGE_AFTER", None)
        env.pop("CLAUDE_HANDOFF_NUDGE", None)
        if extra_env:
            env.update(extra_env)
        proc = subprocess.run(
            [sys.executable, "-I", HOOK],
            input=stdin_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        return proc

    def fire(self, sid="sess-1", source="compact", extra_env=None):
        payload = {"session_id": sid, "hook_event_name": "SessionStart", "source": source}
        return self.run_raw(json.dumps(payload).encode("utf-8"), extra_env)

    def state_file(self, sid="sess-1"):
        return os.path.join(self.state_dir, sid + ".json")

    def count(self, sid="sess-1"):
        with open(self.state_file(sid), "r", encoding="utf-8") as fh:
            return json.load(fh)["compactions"]

    def parse_output(self, proc):
        self.assertTrue(proc.stdout.strip(), "expected output")
        out = json.loads(proc.stdout.decode("utf-8"))
        return out["hookSpecificOutput"]

    def test_a_non_compact_sources_ignored(self):
        for source in ("startup", "resume", "clear"):
            proc = self.fire(source=source)
            self.assertEqual(proc.stdout, b"")
        self.assertFalse(os.path.exists(self.state_file()))

    def test_b_count_increments_on_each_compact(self):
        for expected in (1, 2, 3):
            self.fire()
            self.assertEqual(self.count(), expected)
        with open(self.state_file(), "r", encoding="utf-8") as fh:
            self.assertIsInstance(json.load(fh)["last_ts"], float)

    def test_c_no_output_at_one_and_two(self):
        self.assertEqual(self.fire().stdout, b"")
        self.assertEqual(self.fire().stdout, b"")

    def test_d_output_at_three_and_four_with_correct_n(self):
        self.fire()
        self.fire()
        hso = self.parse_output(self.fire())
        self.assertEqual(hso["hookEventName"], "SessionStart")
        self.assertIn("compacted 3 times", hso["additionalContext"])
        self.assertIn("write a handoff", hso["additionalContext"])
        hso = self.parse_output(self.fire())
        self.assertIn("compacted 4 times", hso["additionalContext"])

    def test_e_threshold_env(self):
        env = {"HANDOFF_NUDGE_AFTER": "1"}
        hso = self.parse_output(self.fire(extra_env=env))
        self.assertIn("compacted 1 times", hso["additionalContext"])
        self.fire(sid="sess-2")
        self.assertEqual(self.fire(sid="sess-2", extra_env={"HANDOFF_NUDGE_AFTER": "5"}).stdout, b"")

    def test_f_bad_threshold_env_falls_back_to_default(self):
        self.fire()
        self.fire()
        proc = self.fire(extra_env={"HANDOFF_NUDGE_AFTER": "many"})
        self.assertIn("compacted 3 times", self.parse_output(proc)["additionalContext"])

    def test_g_kill_switch(self):
        for _ in range(4):
            proc = self.fire(extra_env={"CLAUDE_HANDOFF_NUDGE": "0"})
            self.assertEqual(proc.stdout, b"")
        self.assertFalse(os.path.exists(self.state_file()))

    def test_h_bad_stdin_fails_open(self):
        proc = self.run_raw(b"this is not json {{{")
        self.assertEqual(proc.stdout, b"")
        self.assertFalse(os.path.exists(self.state_dir) and os.listdir(self.state_dir))

    def test_i_missing_session_id_ignored(self):
        proc = self.run_raw(json.dumps({"source": "compact"}).encode("utf-8"))
        self.assertEqual(proc.stdout, b"")

    def test_j_old_state_files_pruned(self):
        os.makedirs(self.state_dir)
        old = os.path.join(self.state_dir, "old.json")
        with open(old, "w", encoding="utf-8") as fh:
            json.dump({"compactions": 9, "last_ts": 0.0}, fh)
        past = time.time() - 15 * 24 * 3600
        os.utime(old, (past, past))
        self.fire()
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(self.state_file()))


if __name__ == "__main__":
    unittest.main()
