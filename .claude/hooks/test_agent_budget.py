import json, os, shutil, subprocess, sys, tempfile, time, unittest

HOME = os.path.expanduser("~")
HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent-budget.py")
DAY = 86400


def reason(r):
    return json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]


class T(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.state = os.path.join(self.root, "state")
        self.log = os.path.join(self.root, "agent-budget.log")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _run(self, raw, env=None):
        e = dict(os.environ)
        for k in ("CLAUDE_AGENT_BUDGET", "AGENT_BUDGET_SOFT", "AGENT_BUDGET_HARD"):
            e.pop(k, None)
        e["CLAUDE_HARNESS_CONFIG"] = os.path.join(self.root, "no-harness.json")  # never created: isolate from live config
        e["AGENT_BUDGET_STATE_DIR"] = self.state
        e["AGENT_BUDGET_LOG"] = self.log
        e.update(env or {})
        r = subprocess.run([sys.executable, "-I", HOOK], input=raw, capture_output=True, text=True, env=e)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def pre(self, agent_id="a1", agent_type="kreeva-osh", tool="Bash", sid="sA", env=None, omit_agent_id=False):
        p = {"session_id": sid, "hook_event_name": "PreToolUse", "tool_name": tool,
             "tool_input": {"command": "ls"}, "tool_use_id": "toolu_x", "cwd": self.root,
             "agent_type": agent_type}
        if not omit_agent_id:
            p["agent_id"] = agent_id
        return self._run(json.dumps(p), env)

    def seed(self, agent_id, count, agent_type="kreeva-osh", sid="sA", warned=False):
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, sid + ".json"), "w", encoding="utf-8") as f:
            json.dump({agent_id: {"agent_type": agent_type, "count": count, "first_ts": 1.0,
                                  "last_ts": 1.0, "last_tool": "Read", "warned": warned}}, f)

    def state_of(self, sid="sA"):
        with open(os.path.join(self.state, sid + ".json"), encoding="utf-8") as f:
            return json.load(f)

    def log_text(self):
        if not os.path.exists(self.log):
            return ""
        with open(self.log, encoding="utf-8") as f:
            return f.read()

    # 1
    def test_01_main_payload_ignored_and_no_state_written(self):
        r = self.pre(omit_agent_id=True, env={"AGENT_BUDGET_SOFT": "1", "AGENT_BUDGET_HARD": "1"})
        self.assertEqual(r.stdout, "")
        self.assertFalse(os.path.exists(self.state))
        self.assertEqual(self.log_text(), "")

    # 2
    def test_02_counts_increment_per_agent_id(self):
        env = {"AGENT_BUDGET_SOFT": "5", "AGENT_BUDGET_HARD": "10"}
        self.pre("A", env=env)
        self.pre("B", agent_type="other", env=env)
        self.pre("A", tool="Read", env=env)
        st = self.state_of()
        self.assertEqual(st["A"]["count"], 2)
        self.assertEqual(st["B"]["count"], 1)
        self.assertEqual(st["A"]["agent_type"], "kreeva-osh")
        self.assertEqual(st["B"]["agent_type"], "other")
        self.assertEqual(st["A"]["last_tool"], "Read")
        self.assertIs(st["A"]["warned"], False)

    def test_02b_state_entry_has_exact_keys(self):
        self.pre("A", env={"AGENT_BUDGET_SOFT": "5", "AGENT_BUDGET_HARD": "10"})
        e = self.state_of()["A"]
        self.assertEqual(set(e), {"agent_type", "count", "first_ts", "last_ts", "last_tool", "warned"})
        self.assertGreater(e["last_ts"], 0)
        self.assertGreaterEqual(e["last_ts"], e["first_ts"])

    # 3
    def test_03_soft_deny_happens_exactly_once_at_soft(self):
        env = {"AGENT_BUDGET_SOFT": "2", "AGENT_BUDGET_HARD": "4"}
        r1 = self.pre(env=env)
        self.assertEqual(r1.stdout, "")
        r2 = self.pre(env=env)
        self.assertIn('"permissionDecision": "deny"', r2.stdout)
        self.assertEqual(json.loads(r2.stdout)["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertEqual(reason(r2), "Tool-call budget: 2 of 4 calls used. Wrap up now: commit what works, "
                                     "then send your final report (what is done, what is not, blockers). "
                                     "Further calls are allowed until 4.")
        self.assertIs(self.state_of()["a1"]["warned"], True)
        self.assertIn("DENY soft", self.log_text())
        self.assertIn("agent=a1", self.log_text())

    # 4
    def test_04_allowed_between_soft_and_hard(self):
        env = {"AGENT_BUDGET_SOFT": "2", "AGENT_BUDGET_HARD": "4"}
        self.pre(env=env)
        self.pre(env=env)  # soft deny
        self.assertEqual(self.pre(env=env).stdout, "")  # 3
        self.assertEqual(self.pre(env=env).stdout, "")  # 4
        self.assertEqual(self.state_of()["a1"]["count"], 4)

    # 5
    def test_05_denied_above_hard(self):
        env = {"AGENT_BUDGET_SOFT": "1", "AGENT_BUDGET_HARD": "2"}
        self.assertIn("Tool-call budget: 1 of 2", reason(self.pre(env=env)))  # soft, count 1
        self.assertEqual(self.pre(env=env).stdout, "")  # count 2, allowed
        r3 = self.pre(env=env)
        self.assertEqual(json.loads(r3.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(reason(r3), "Tool-call budget exhausted (2 calls). Stop and send your final report "
                                     "now; do not call more tools.")
        self.assertIn("DENY hard", self.log_text())
        r4 = self.pre(env=env)  # every call above hard is denied
        self.assertTrue(reason(r4).startswith("Tool-call budget exhausted"))
        self.assertEqual(self.state_of()["a1"]["count"], 4)

    # 6
    def test_06_reviewer_types_get_lower_limits(self):
        for agent_type in ("pr-reviewer-1", "opus-reviewer"):
            self.seed("R", 79, agent_type=agent_type, sid="s-" + agent_type)
            r = self.pre("R", agent_type=agent_type, sid="s-" + agent_type)
            self.assertEqual(reason(r), "Tool-call budget: 80 of 110 calls used. Wrap up now: commit what works, "
                                        "then send your final report (what is done, what is not, blockers). "
                                        "Further calls are allowed until 110.", agent_type)

    def test_06b_other_types_keep_default_limits(self):
        self.seed("G", 79, agent_type="general-purpose")
        self.assertEqual(self.pre("G", agent_type="general-purpose").stdout, "")  # 80: no warning
        self.seed("H", 149, agent_type="general-purpose", sid="sB")
        self.assertIn("Tool-call budget: 150 of 200", reason(self.pre("H", agent_type="general-purpose", sid="sB")))

    # 7
    def test_07_env_overrides_apply(self):
        env = {"AGENT_BUDGET_SOFT": "2", "AGENT_BUDGET_HARD": "3"}
        self.assertEqual(self.pre(env=env).stdout, "")
        self.assertIn("Tool-call budget: 2 of 3", reason(self.pre(env=env)))
        # env also beats the reviewer defaults
        r = self.pre("R", agent_type="pr-reviewer-1", sid="sR", env={"AGENT_BUDGET_SOFT": "1", "AGENT_BUDGET_HARD": "2"})
        self.assertIn("Tool-call budget: 1 of 2", reason(r))

    def test_07b_env_beats_harness_config(self):
        cfg = os.path.join(self.root, "h.json")
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump({"agent_budget": {"default": {"soft": 5, "hard": 6}}}, f)
        env = {"CLAUDE_HARNESS_CONFIG": cfg, "AGENT_BUDGET_SOFT": "2", "AGENT_BUDGET_HARD": "3"}
        self.pre(env=env)
        self.assertIn("Tool-call budget: 2 of 3", reason(self.pre(env=env)))

    # 8
    def test_08_harness_config_override(self):
        cfg = os.path.join(self.root, "h.json")
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump({"agent_budget": {"default": {"soft": 4, "hard": 6},
                                        "types": {"exec-*": {"soft": 2, "hard": 3}}}}, f)
        env = {"CLAUDE_HARNESS_CONFIG": cfg}
        self.assertEqual(self.pre("E", agent_type="exec-kreeva", env=env).stdout, "")
        self.assertIn("Tool-call budget: 2 of 3", reason(self.pre("E", agent_type="exec-kreeva", env=env)))
        for _ in range(3):
            self.assertEqual(self.pre("O", agent_type="other", env=env).stdout, "")
        self.assertIn("Tool-call budget: 4 of 6", reason(self.pre("O", agent_type="other", env=env)))

    def test_08b_harness_config_types_beat_builtin_reviewer_limits(self):
        cfg = os.path.join(self.root, "h.json")
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump({"agent_budget": {"types": {"pr-reviewer-*": {"soft": 3, "hard": 5}}}}, f)
        env = {"CLAUDE_HARNESS_CONFIG": cfg}
        self.pre("R", agent_type="pr-reviewer-2", env=env)
        self.pre("R", agent_type="pr-reviewer-2", env=env)
        self.assertIn("Tool-call budget: 3 of 5", reason(self.pre("R", agent_type="pr-reviewer-2", env=env)))

    def test_08c_malformed_harness_config_ignored(self):
        cfg = os.path.join(self.root, "h.json")
        with open(cfg, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.seed("G", 149, agent_type="general-purpose")
        self.assertIn("Tool-call budget: 150 of 200",
                      reason(self.pre("G", agent_type="general-purpose", env={"CLAUDE_HARNESS_CONFIG": cfg})))

    # 9
    def test_09_kill_switch_does_nothing(self):
        env = {"CLAUDE_AGENT_BUDGET": "0", "AGENT_BUDGET_SOFT": "1", "AGENT_BUDGET_HARD": "1"}
        self.assertEqual(self.pre(env=env).stdout, "")
        self.assertEqual(self.pre(env=env).stdout, "")
        self.assertFalse(os.path.exists(self.state))

    # 10
    def test_10_malformed_stdin_exits_zero_silently(self):
        for raw in ["not json", "", "[1,2]", "null"]:
            r = self._run(raw, env={"AGENT_BUDGET_SOFT": "1", "AGENT_BUDGET_HARD": "1"})
            self.assertEqual(r.stdout, "", repr(raw))
        self.assertFalse(os.path.exists(self.state))

    # 11
    def test_11_stale_state_swept_fresh_kept(self):
        os.makedirs(self.state, exist_ok=True)
        stale = os.path.join(self.state, "stale.json")
        with open(stale, "w", encoding="utf-8") as f:
            json.dump({}, f)
        old = time.time() - 20 * DAY
        os.utime(stale, (old, old))
        self.pre(sid="fresh")
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(os.path.join(self.state, "fresh.json")))

    # 12
    def test_12_unparseable_state_file_treated_as_empty(self):
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, "sA.json"), "w", encoding="utf-8") as f:
            f.write("garbage")
        self.assertEqual(self.pre(env={"AGENT_BUDGET_SOFT": "5", "AGENT_BUDGET_HARD": "9"}).stdout, "")
        self.assertEqual(self.state_of()["a1"]["count"], 1)


if __name__ == "__main__":
    unittest.main()
