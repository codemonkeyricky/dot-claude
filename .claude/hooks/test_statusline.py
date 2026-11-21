import calendar, json, os, subprocess, sys, tempfile, time, unittest
SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "statusline.py")
SEP = "  │  "
NOW = 1_000_000.0
EPOCH = float(calendar.timegm(time.strptime("2026-11-20T04:29:55Z", "%Y-%m-%dT%H:%M:%SZ")))
SID = "11111111-2222-3333-4444-555555555555"


class T(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.budget = tempfile.mkdtemp()
        self.tasks = tempfile.mkdtemp()
        self.deploy_log = os.path.join(tempfile.mkdtemp(), "deploys.jsonl")

    def run_sl(self, payload, env=None):
        e = {"PATH": os.environ.get("PATH", ""), "HOME": self.home,
             "AGENT_BUDGET_STATE_DIR": self.budget, "TASK_GATE_STATE_DIR": self.tasks,
             "STATUSLINE_DEPLOY_LOG": self.deploy_log, "STATUSLINE_NOW": str(NOW)}
        e.update(env or {})
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        r = subprocess.run([sys.executable, SCRIPT], input=raw, capture_output=True,
                           text=True, env=e, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        lines = r.stdout.split("\n")
        self.assertEqual(lines[-1], "")
        self.assertEqual(len(lines), 2, r.stdout)
        return lines[0]

    def payload(self, sid=SID, name="Opus"):
        return {"session_id": sid, "cwd": "/tmp", "model": {"id": "claude-opus", "display_name": name}}

    def write_state(self, d, name, obj):
        with open(os.path.join(d, SID + ".json"), "w") as f:
            if isinstance(obj, str):
                f.write(obj)
            else:
                json.dump(obj, f)

    def write_deploy(self, *entries):
        with open(self.deploy_log, "w") as f:
            for e in entries:
                f.write((e if isinstance(e, str) else json.dumps(e)) + "\n")

    def test_model_only_without_state(self):
        self.assertEqual(self.run_sl(self.payload()), "Opus")

    def test_model_falls_back_to_id_and_empty_when_missing(self):
        self.assertEqual(self.run_sl({"session_id": SID, "model": {"id": "m-1"}}), "m-1")
        self.assertEqual(self.run_sl({"session_id": SID}), "")

    def test_executor_format_order_idle_and_warning(self):
        self.write_state(self.budget, SID, {
            "a": {"agent_type": "qorvax-tel", "count": 3, "first_ts": 1, "last_ts": NOW - 12, "last_tool": "Bash", "warned": False},
            "b": {"agent_type": "zyrene-vosk", "count": 1, "first_ts": 1, "last_ts": NOW - 240, "last_tool": "Read", "warned": False},
            "c": {"agent_type": "thal-ummir", "count": 7, "first_ts": 1, "last_ts": NOW - 400, "last_tool": "Edit", "warned": True},
        })
        out = self.run_sl(self.payload())
        self.assertEqual(out, SEP.join(["Opus",
            "⚙ qorvax-tel 3c 12s, zyrene-vosk 1c 4m, thal-ummir 7c ⚠6m"]))

    def test_executor_30_minute_cutoff_and_hour_format(self):
        self.write_state(self.budget, SID, {
            "old": {"agent_type": "gone", "count": 1, "last_ts": NOW - 1801},
            "edge": {"agent_type": "edge", "count": 2, "last_ts": NOW - 1800},
            "hr": {"agent_type": "hour", "count": 4, "last_ts": NOW - 3900},
        })
        out = self.run_sl(self.payload())
        self.assertEqual(out, SEP.join(["Opus", "⚙ edge 2c ⚠30m"]))
        self.assertNotIn("gone", out)
        self.assertNotIn("hour", out)

    def test_age_formatting(self):
        sys.path.insert(0, os.path.dirname(SCRIPT))
        import importlib
        sl = importlib.import_module("statusline")
        self.assertEqual(sl.fmt_age(12), "12s")
        self.assertEqual(sl.fmt_age(59), "59s")
        self.assertEqual(sl.fmt_age(60), "1m")
        self.assertEqual(sl.fmt_age(1800), "30m")
        self.assertEqual(sl.fmt_age(3900), "1h05")
        self.assertEqual(sl.fmt_age(-5), "0s")

    def test_executor_overflow_shows_plus_n(self):
        state = {}
        for i in range(6):
            state["k%d" % i] = {"agent_type": "t%d" % i, "count": 1, "last_ts": NOW - 10 * (i + 1)}
        self.write_state(self.budget, SID, state)
        out = self.run_sl(self.payload())
        self.assertEqual(out, SEP.join(["Opus", "⚙ t0 1c 10s, t1 1c 20s, t2 1c 30s, t3 1c 40s +2"]))

    def test_executor_hidden_when_all_stale_or_corrupt_entries_skipped(self):
        self.write_state(self.budget, SID, {
            "old": {"agent_type": "x", "count": 1, "last_ts": NOW - 5000},
            "bad": {"agent_type": "y", "count": 1, "last_ts": "nope"},
            "junk": "not-a-dict",
        })
        self.assertEqual(self.run_sl(self.payload()), "Opus")

    def test_task_counts(self):
        self.write_state(self.tasks, SID, {
            "1": {"status": "completed", "owner": "a", "subject": "s"},
            "2": {"status": "completed", "owner": "b", "subject": "s"},
            "3": {"status": "in_progress", "owner": "c", "subject": "s"},
            "4": {"status": "pending", "owner": "", "subject": "s"},
        })
        self.assertEqual(self.run_sl(self.payload()), SEP.join(["Opus", "tasks ▶1 ✓2/4"]))

    def test_empty_task_state_has_no_segment(self):
        self.write_state(self.tasks, SID, {})
        self.assertEqual(self.run_sl(self.payload()), "Opus")

    def test_deploy_segment_from_real_shaped_line(self):
        self.write_deploy({"by": "deploy-hook", "dryRun": False, "expected": "index-BM_F2e48.js",
                           "live": "index-BM_F2e48.js", "sha": "82cb9fcc93dbabe0963ff4bceffbe474e19dc525",
                           "short": "82cb9fc", "smoke": None, "smokeQuery": None,
                           "subject": "Remove geared tail (#109)", "target": "prod",
                           "ts": "2026-11-20T04:29:55Z", "verified": True})
        ts_epoch = EPOCH  # the line's ts; age is driven by STATUSLINE_NOW below
        out = self.run_sl(self.payload(), env={"STATUSLINE_NOW": str(ts_epoch + 3600)})
        self.assertEqual(out, SEP.join(["Opus", "deploy prod 82cb9fc verified 1h00"]))

    def test_deploy_older_than_24h_or_missing_is_skipped(self):
        self.write_deploy({"target": "prod", "short": "abc1234", "verified": True,
                           "ts": "2026-11-20T04:29:55Z"})
        out = self.run_sl(self.payload(), env={"STATUSLINE_NOW": str(EPOCH + 25 * 3600)})
        self.assertEqual(out, "Opus")
        out = self.run_sl(self.payload(), env={"STATUSLINE_DEPLOY_LOG": os.path.join(self.home, "none.jsonl")})
        self.assertEqual(out, "Opus")

    def test_deploy_tail_read_handles_large_file_and_trailing_blanks(self):
        filler = [{"target": "staging", "short": "f%06d" % i, "verified": True,
                   "ts": EPOCH - 10000 - i, "subject": "x" * 200} for i in range(400)]
        last = {"target": "prod", "short": "1234567", "dryRun": True, "ts": EPOCH}
        self.write_deploy(*filler, last)
        with open(self.deploy_log, "a") as f:
            f.write("\n\n   \n")
        out = self.run_sl(self.payload(), env={"STATUSLINE_NOW": str(EPOCH + 60)})
        self.assertEqual(out, SEP.join(["Opus", "deploy prod 1234567 dry-run 1m"]))

    def test_garbage_stdin_exits_zero(self):
        for raw in ["not json", "[1,2,3]", "", "null", "{\"session_id\": 5}"]:
            out = self.run_sl(raw)
            self.assertEqual(out, "")

    def test_corrupt_state_file_still_prints_model(self):
        self.write_state(self.budget, SID, "{not valid json")
        self.write_state(self.tasks, SID, "[]")
        self.assertEqual(self.run_sl(self.payload()), "Opus")

    def test_unsafe_session_id_never_reads_state(self):
        self.write_state(self.budget, SID, {"a": {"agent_type": "leak", "count": 1, "last_ts": NOW}})
        out = self.run_sl(self.payload(sid="../" + os.path.basename(self.budget)))
        self.assertEqual(out, "Opus")
        self.assertNotIn("leak", out)

    def test_control_chars_cannot_break_line(self):
        self.write_state(self.budget, SID, {"a": {"agent_type": "bad\nname", "count": 1, "last_ts": NOW}})
        out = self.run_sl(self.payload(name="Op\nus"))
        self.assertEqual(out, SEP.join(["Op us", "⚙ bad name 1c 0s"]))


if __name__ == "__main__":
    unittest.main()
