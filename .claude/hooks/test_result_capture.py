"""Unit tests for result-capture.py (Stop hook).

Each test runs the hook as a subprocess with RESULT_CAPTURE_LOG and
RESULT_CAPTURE_STATE_DIR pointed into a temp directory.

Run: cd hooks && python3 -m unittest test_result_capture -v
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "result-capture.py")


class ResultCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="result-capture-test-")
        self.log = os.path.join(self.tmp, "log", "completed-work.jsonl")
        self.state_dir = os.path.join(self.tmp, "state")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_hook(self, stdin_bytes):
        env = dict(os.environ)
        env["RESULT_CAPTURE_LOG"] = self.log
        env["RESULT_CAPTURE_STATE_DIR"] = self.state_dir
        proc = subprocess.run(
            [sys.executable, "-I", HOOK],
            input=stdin_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertEqual(proc.stdout, b"", "hook must not write to stdout")
        return proc

    def run_payload(self, payload):
        return self.run_hook(json.dumps(payload).encode("utf-8"))

    def read_records(self):
        with open(self.log, "r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_a_result_line_recorded(self):
        self.run_payload({
            "session_id": "sess-a",
            "cwd": "/work/a",
            "last_assistant_message": "intro\nresult: Shipped X\n",
        })
        records = self.read_records()
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec["kind"], "result")
        self.assertEqual(rec["text"], "Shipped X")
        self.assertEqual(rec["session_id"], "sess-a")
        self.assertEqual(rec["cwd"], "/work/a")
        self.assertIn("ts", rec)

    def test_b_same_payload_twice_is_deduped(self):
        payload = {
            "session_id": "sess-b",
            "cwd": "/work/b",
            "last_assistant_message": "intro\nresult: Shipped X\n",
        }
        self.run_payload(payload)
        self.run_payload(payload)
        records = self.read_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["text"], "Shipped X")

    def test_c_bold_wrapping_stripped(self):
        self.run_payload({
            "session_id": "sess-c",
            "cwd": "/work/c",
            "last_assistant_message": "result: **Bold thing**",
        })
        records = self.read_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["text"], "Bold thing")

    def test_d_no_result_line_creates_no_log(self):
        self.run_payload({
            "session_id": "sess-d",
            "cwd": "/work/d",
            "last_assistant_message": "just a normal reply with no marker",
        })
        self.assertFalse(os.path.exists(self.log))

    def test_e_transcript_fallback(self):
        transcript = os.path.join(self.tmp, "transcript.jsonl")
        entries = [
            {"type": "user", "message": {"content": [{"type": "text", "text": "do it"}]}},
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "result: From transcript"},
            ]}},
        ]
        with open(transcript, "w", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(json.dumps(entry) + "\n")
        self.run_payload({
            "session_id": "sess-e",
            "cwd": "/work/e",
            "transcript_path": transcript,
        })
        records = self.read_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["text"], "From transcript")
        self.assertEqual(records[0]["session_id"], "sess-e")

    def test_f_invalid_json_stdin_fails_open(self):
        proc = self.run_hook(b"this is not json {{{")
        self.assertEqual(proc.stdout, b"")
        self.assertFalse(os.path.exists(self.log))


if __name__ == "__main__":
    unittest.main()
