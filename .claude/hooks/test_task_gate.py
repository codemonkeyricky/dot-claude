import json, os, shutil, subprocess, sys, tempfile, time, unittest

HOME = os.path.expanduser("~")
HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "task-gate.py")
DAY = 86400


def decision(r):
    return json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] if r.stdout.strip() else "allow"


class T(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.state = os.path.join(self.root, "state")
        self.log = os.path.join(self.root, "task-gate.log")
        self.clog = os.path.join(self.root, "completed-work.jsonl")
        self.agents = os.path.join(self.root, "agents")
        os.makedirs(self.agents)
        for name, model in [("X", "haiku"), ("Y", "haiku"), ("qorvax-tel", "haiku"), ("zyrene-vosk", "haiku"),
                            ("kreeva-osh", "haiku"), ("opus-custom", "opus"),
                            ("thal-ummir", "haiku"), ("alpha-x", "haiku"), ("beta-y", "haiku")]:
            with open(os.path.join(self.agents, name + ".md"), "w", encoding="utf-8") as f:
                f.write("---\nname: %s\nmodel: %s\n---\nbody\n" % (name, model))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _run(self, raw, env=None):
        e = dict(os.environ)
        e.pop("CLAUDE_MAIN_WRITE", None)
        e.pop("HARNESS_TEST_ROLE", None)
        for var in ("TASK_GATE_POOL_SIZE", "TASK_GATE_RUNNING_STALE_SEC", "TASK_GATE_REQUIRE_BACKGROUND",
                    "LOCAL_DISPATCH"):
            e.pop(var, None)
        e["CLAUDE_HARNESS_CONFIG"] = os.path.join(self.root, "no-harness.json")  # never created: isolate from live config
        e["TASK_GATE_STATE_DIR"] = self.state
        e["TASK_GATE_LOG"] = self.log
        e["TASK_GATE_COMPLETION_LOG"] = self.clog
        e["TASK_GATE_AGENTS_DIR"] = self.agents
        e.update(env or {})
        r = subprocess.run([sys.executable, "-I", HOOK], input=raw, capture_output=True, text=True, env=e)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def pre(self, subagent="X", sid="sA", tool="Agent", agent_id=None, env=None, omit_subagent=False, ti_extra=None, tuid="toolu_x", omit_bg=False):
        ti = {"description": "d", "prompt": "p", "run_in_background": False}
        if not omit_subagent:
            ti["subagent_type"] = subagent
        ti.update(ti_extra or {})
        if omit_bg:  # the run_in_background key left out entirely (counts as foreground)
            ti.pop("run_in_background", None)
        p = {"session_id": sid, "hook_event_name": "PreToolUse", "tool_name": tool,
             "tool_input": ti, "tool_use_id": tuid, "cwd": self.root}
        if agent_id:
            p["agent_id"] = agent_id
        return self._run(json.dumps(p), env)

    def post(self, tool, ti, resp, sid="sA", env=None, extra=None):
        p = {"session_id": sid, "hook_event_name": "PostToolUse", "tool_name": tool,
             "tool_input": ti, "tool_response": resp, "tool_use_id": "toolu_y", "cwd": self.root}
        p.update(extra or {})
        return self._run(json.dumps(p), env)

    def create(self, tid, sid="sA"):
        return self.post("TaskCreate", {"subject": "probe", "description": "d"},
                         {"task": {"id": str(tid), "subject": "probe", "description": "d"}}, sid)

    def update(self, tid, sid="sA", **fields):
        ti = {"taskId": str(tid)}
        ti.update(fields)
        return self.post("TaskUpdate", ti, {"success": True, "taskId": str(tid),
                                            "updatedFields": list(fields), "statusChange": {}}, sid)

    def clog_lines(self):
        if not os.path.exists(self.clog):
            return []
        with open(self.clog, encoding="utf-8") as f:
            return [json.loads(x) for x in f.read().splitlines()]

    def log_text(self):
        if not os.path.exists(self.log):
            return ""
        with open(self.log, encoding="utf-8") as f:
            return f.read()

    # 1
    def test_01_no_tasks_denied_with_shape_and_log(self):
        r = self.pre("X")
        out = json.loads(r.stdout)
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "PreToolUse")
        self.assertEqual(hso["permissionDecision"], "deny")
        self.assertIn('No in_progress task owned by "X"', hso["permissionDecisionReason"])
        self.assertIn("DENY", self.log_text())
        self.assertIn("subagent=X", self.log_text())

    # 2
    def test_02_pending_task_denied(self):
        self.create(1)
        self.assertEqual(decision(self.pre("X")), "deny")

    # 3
    def test_03_in_progress_owner_allowed_only_for_owner(self):
        self.create(1)
        self.update(1, status="in_progress", owner="X")
        self.assertEqual(decision(self.pre("X")), "allow")
        self.assertEqual(self.pre("X").stdout, "")
        self.assertEqual(decision(self.pre("Y")), "deny")

    # 4
    def test_04_completed_owner_denied(self):
        self.create(1)
        self.update(1, status="in_progress", owner="X")
        self.update(1, status="completed")
        self.assertEqual(decision(self.pre("X")), "deny")

    # 5
    def test_05_deleted_owner_denied(self):
        self.create(1)
        self.update(1, status="in_progress", owner="X")
        self.update(1, status="deleted")
        self.assertEqual(decision(self.pre("X")), "deny")

    # 6
    def test_06_unknown_taskid_update_allowed(self):
        self.update(9, status="in_progress", owner="X")
        self.assertEqual(decision(self.pre("X")), "allow")

    # 7
    def test_07_missing_subagent_type_defaults_to_general_purpose(self):
        self.create(1)
        self.update(1, status="in_progress", owner="X")
        r = self.pre(omit_subagent=True)
        self.assertEqual(decision(r), "deny")
        self.assertIn("is not allowed from the main session", json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"])

    def test_07b_missing_subagent_type_denied_for_other_owner(self):
        self.create(1)
        self.update(1, status="in_progress", owner="X")
        self.assertEqual(decision(self.pre(omit_subagent=True)), "deny")

    # 8
    def test_08_sessions_are_isolated(self):
        self.create(1, sid="sA")
        self.update(1, sid="sA", status="in_progress", owner="X")
        self.assertEqual(decision(self.pre("X", sid="sA")), "allow")
        self.assertEqual(decision(self.pre("X", sid="sB")), "deny")

    # 9
    def test_09_subagent_agent_dispatch_denied(self):
        self.assertEqual(decision(self.pre("X", agent_id="agent-123")), "deny")

    # 10
    def _exempt_cfg(self):
        path = os.path.join(self.root, "exempt.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"exempt_env": {"HARNESS_TEST_ROLE": ["bot", "bot-*"]}}, f)
        return {"CLAUDE_HARNESS_CONFIG": path}

    def test_10_exempt_env_roles_exempt(self):
        cfg = self._exempt_cfg()
        for role in ["bot", "bot-worker"]:
            self.assertEqual(decision(self.pre("X", env=dict(cfg, HARNESS_TEST_ROLE=role))), "allow", role)

    def test_10b_other_roles_denied(self):
        cfg = self._exempt_cfg()
        for role in ["Bot-worker", "xbot-worker", "bots", ""]:
            self.assertEqual(decision(self.pre("X", env=dict(cfg, HARNESS_TEST_ROLE=role))), "deny", repr(role))
        self.assertEqual(decision(self.pre("X", env=cfg)), "deny", "unset")

    def test_10c_no_config_no_exemption(self):
        self.assertEqual(decision(self.pre("X", env={"HARNESS_TEST_ROLE": "bot"})), "deny")

    # 11
    def test_11_main_write_exempt(self):
        # CLAUDE_MAIN_WRITE lifts the DAG gate only: a haiku executor passes, the type gate still denies general-purpose
        mw = {"CLAUDE_MAIN_WRITE": "1"}
        self.assertEqual(decision(self.pre("X", env=mw)), "allow")
        self.assertEqual(decision(self.pre("kreeva-osh", env=mw, ti_extra={"model": "haiku"})), "allow")
        self.assertEqual(decision(self.pre("kreeva-osh", env=mw, ti_extra={"model": "opus"})), "deny")
        r = self.pre("general-purpose", env=mw)
        self.assertEqual(decision(r), "deny")
        self.assertIn("is not allowed from the main session", json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"])

    # 11b
    def test_11b_main_write_opus_and_sonnet_executor_denied(self):
        mw = {"CLAUDE_MAIN_WRITE": "1"}
        for model in ["opus", "opus[1m]", "us.anthropic.claude-opus-5-5", "sonnet"]:
            self.assertEqual(decision(self.pre("kreeva-osh", env=mw, ti_extra={"model": model})), "deny", model)
        self.assertIn("model-gate=escalation", self.log_text())

    # 12
    def test_12_garbage_and_empty_stdin_fail_open(self):
        for raw in ["not json", ""]:
            r = self._run(raw)
            self.assertEqual(r.stdout, "", repr(raw))

    # 13
    def test_13_text_fallback_task_id(self):
        self.post("TaskCreate", {"subject": "probe", "description": "d"}, "Task #3 created successfully")
        self.update(3, status="in_progress", owner="X")
        self.assertEqual(decision(self.pre("X")), "allow")

    # 14
    def test_14_task_tool_gated(self):
        self.assertEqual(decision(self.pre("X", tool="Task")), "deny")

    # 15
    def test_15_stale_state_removed_fresh_kept(self):
        os.makedirs(self.state, exist_ok=True)
        stale = os.path.join(self.state, "stale.json")
        with open(stale, "w", encoding="utf-8") as f:
            json.dump({"1": {"status": "pending", "owner": None, "subject": "old"}}, f)
        old = time.time() - 20 * DAY
        os.utime(stale, (old, old))
        self.create(1, sid="fresh")
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(os.path.join(self.state, "fresh.json")))

    # 16
    def test_16_no_tmp_leftovers(self):
        self.create(1)
        for i in range(2, 8):
            self.update(1, status="in_progress", owner="X%d" % i)
        leftovers = [n for n in os.listdir(self.state) if n.startswith(".tmp-")]
        self.assertEqual(leftovers, [])
        self.assertTrue(os.path.exists(os.path.join(self.state, "sA.json")))

    # ---- DAG helpers ----
    def pre_update(self, tid, sid="sA", agent_id=None, env=None, **fields):
        ti = {"taskId": str(tid)}
        ti.update(fields)
        p = {"session_id": sid, "hook_event_name": "PreToolUse", "tool_name": "TaskUpdate",
             "tool_input": ti, "tool_use_id": "toolu_w", "cwd": self.root}
        if agent_id:
            p["agent_id"] = agent_id
        return self._run(json.dumps(p), env)

    def post_agent(self, subagent="X", sid="sA"):
        return self.post("Agent", {"description": "d", "prompt": "p", "subagent_type": subagent},
                         {}, sid)

    def ctx(self, r):
        return json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"] if r.stdout.strip() else ""

    def sA_state(self, sid="sA"):
        with open(os.path.join(self.state, sid + ".json"), encoding="utf-8") as f:
            return json.load(f)

    # 17
    def test_17_cycle_edges_denied_and_addblocks_direction_checked(self):
        self.create(1)
        self.create(2)
        self.assertEqual(decision(self.pre_update(2, addBlockedBy=["1"])), "allow")
        self.update(2, addBlockedBy=["1"])
        self.assertEqual(self.sA_state()["2"]["blockedBy"], ["1"])
        r = self.pre_update(1, addBlockedBy=["2"])
        self.assertEqual(decision(r), "deny")
        self.assertIn("Edge #1 blockedBy #2 would create a cycle #1 -> #2 -> #1", json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertEqual(decision(self.pre_update(1, addBlockedBy=["1"])), "deny")
        # addBlocks on 2 means "1 blockedBy 2": a cycle
        self.assertEqual(decision(self.pre_update(2, addBlocks=["1"])), "deny")
        # addBlocks on 1 means "2 blockedBy 1": already the case, no cycle
        self.assertEqual(decision(self.pre_update(1, addBlocks=["2"])), "allow")
        # subagent payloads are never gated
        self.assertEqual(decision(self.pre_update(1, agent_id="agent-9", addBlockedBy=["2"])), "allow")
        # a denied call is not recorded
        self.assertEqual(self.sA_state().get("1", {}).get("blockedBy", []), [])

    # 18
    def test_18_in_progress_on_blocked_task_denied_until_blocker_completed(self):
        self.create(1)
        self.create(2)
        self.update(2, addBlockedBy=["1"])
        r = self.pre_update(2, status="in_progress", owner="X")
        self.assertEqual(decision(r), "deny")
        self.assertIn("Task #2 is blocked by open task(s) #1", json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"])
        self.update(1, status="completed")
        self.assertEqual(decision(self.pre_update(2, status="in_progress", owner="X")), "allow")

    # 19
    def test_19_completion_nudge_lists_newly_unblocked_and_skips_dispatch_false(self):
        for i in (1, 2, 3):
            self.create(i)
        self.update(2, addBlockedBy=["1"])
        self.update(3, addBlockedBy=["1"])
        text = self.ctx(self.update(1, status="completed"))
        self.assertIn("Task #1 completed", text)
        self.assertIn('#2 "probe"', text)
        self.assertIn('#3 "probe"', text)
        # second session: task 3 opts out of dispatch
        self.create(1, sid="sD")
        self.create(2, sid="sD")
        self.create(3, sid="sD")
        self.update(2, sid="sD", addBlockedBy=["1"])
        self.update(3, sid="sD", addBlockedBy=["1"])
        self.update(3, sid="sD", metadata={"dispatch": False})
        text = self.ctx(self.update(1, sid="sD", status="completed"))
        self.assertIn('#2 "probe"', text)
        self.assertNotIn('#3 "probe"', text)

    # 20
    def test_20_duplicate_owner_denied_same_task_allowed(self):
        self.create(1)
        self.create(2)
        self.update(1, status="in_progress", owner="X")
        self.assertEqual(decision(self.pre_update(2, status="in_progress", owner="X")), "deny")
        self.assertEqual(decision(self.pre_update(1, status="in_progress", owner="X")), "allow")
        self.assertEqual(decision(self.pre_update(2, status="in_progress", owner="Y")), "allow")

    # 21
    def test_21_agent_nudge_lists_undispatched_and_ready_then_goes_quiet(self):
        self.create(1)
        self.create(2)
        self.update(1, status="in_progress", owner="X")
        text = self.ctx(self.post_agent("X"))
        self.assertIn('Still not dispatched: #1 "probe" (in_progress, owner X, no Agent call yet)', text)
        self.assertIn("ready: #2", text)
        self.update(2, metadata={"dispatch": False})
        self.assertEqual(decision(self.pre("X")), "allow")
        self.assertEqual(self.post_agent("X").stdout, "")

    # 22
    def test_22_malformed_payloads_fail_open(self):
        for raw in ["[1,2]", "null", '{"hook_event_name":"PreToolUse","tool_name":"TaskUpdate","tool_input":"oops","session_id":"sA"}']:
            r = self._run(raw)
            self.assertEqual(r.stdout, "", raw)

    # 23
    def test_23_old_format_state_still_works(self):
        os.makedirs(self.state, exist_ok=True)
        old = {"1": {"status": "pending", "owner": None, "subject": "a"},
               "2": {"status": "in_progress", "owner": "X", "subject": "b"}}
        with open(os.path.join(self.state, "sA.json"), "w", encoding="utf-8") as f:
            json.dump(old, f)
        self.assertEqual(decision(self.pre("X")), "allow")
        self.assertIs(self.sA_state()["2"]["dispatched"], True)
        self.assertEqual(decision(self.pre_update(1, addBlockedBy=["2"])), "allow")
        self.update(1, addBlockedBy=["2"])
        self.assertEqual(self.sA_state()["1"]["blockedBy"], ["2"])
        self.assertIn("#1", self.ctx(self.update(2, status="completed")))

    # 24
    def test_24_stale_lock_removed_and_no_lock_left_behind(self):
        os.makedirs(self.state, exist_ok=True)
        lock = os.path.join(self.state, "sA.lock")
        open(lock, "w").close()
        old = time.time() - 30
        os.utime(lock, (old, old))
        self.create(1)
        self.assertEqual(self.sA_state()["1"]["status"], "pending")
        self.assertFalse(os.path.exists(lock))

    # 25
    def test_25_exempt_subagent_types_allowed_without_task(self):
        for sub in ["web-fetch", "Explore"]:
            r = self.pre(sub)
            self.assertEqual(decision(r), "allow", sub)
            self.assertEqual(r.stdout, "", sub)

    # 26
    def test_26_require_task_kill_switch_allows_agent(self):
        self.assertEqual(self.pre("X", env={"CLAUDE_REQUIRE_TASK": "0"}).stdout, "")

    # 27
    def test_27_require_task_kill_switch_still_records(self):
        self.post("TaskCreate", {"subject": "probe", "description": "d"},
                  {"task": {"id": "1", "subject": "probe", "description": "d"}},
                  env={"CLAUDE_REQUIRE_TASK": "0"})
        self.assertEqual(self.sA_state()["1"]["status"], "pending")
        self.assertEqual(self.sA_state()["1"]["subject"], "probe")


    # ---- completion log ----
    def test_28_completion_log_one_line_per_real_transition(self):
        self.post("TaskCreate", {"subject": "Do X", "description": "d"},
                  {"task": {"id": "1", "subject": "Do X", "description": "d"}})
        self.update(1, status="in_progress", owner="bob")
        self.assertEqual(self.clog_lines(), [])
        self.update(1, status="completed")
        lines = self.clog_lines()
        self.assertEqual(len(lines), 1)
        rec = lines[0]
        self.assertEqual(set(rec), {"kind", "ts", "session_id", "cwd", "task_id", "subject",
                                    "owner", "metadata", "agent_id", "agent_type", "rounds"})
        self.assertEqual(rec["rounds"], [])
        self.assertEqual(rec["kind"], "task")
        self.assertRegex(rec["ts"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(rec["session_id"], "sA")
        self.assertEqual(rec["cwd"], self.root)
        self.assertEqual(rec["task_id"], "1")
        self.assertEqual(rec["subject"], "Do X")
        self.assertEqual(rec["owner"], "bob")
        self.assertEqual(rec["metadata"], {})
        self.assertIsNone(rec["agent_id"])
        self.assertIsNone(rec["agent_type"])
        # already completed: no new line
        self.update(1, status="completed")
        self.assertEqual(len(self.clog_lines()), 1)
        # the raw file is newline-terminated utf-8 JSON lines
        with open(self.clog, "rb") as f:
            raw = f.read()
        self.assertTrue(raw.endswith(b"\n") and not raw.endswith(b"\r\n"))

    def test_29_untracked_task_completed_counts_as_transition(self):
        self.update(7, status="completed")
        lines = self.clog_lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["task_id"], "7")
        self.assertIsNone(lines[0]["subject"])
        self.assertIsNone(lines[0]["owner"])

    def test_30_subagent_completion_records_agent_fields(self):
        self.create(1)
        self.post("TaskUpdate", {"taskId": "1", "status": "completed"},
                  {"success": True, "taskId": "1", "updatedFields": ["status"], "statusChange": {}},
                  extra={"agent_id": "a1", "agent_type": "x"})
        lines = self.clog_lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["agent_id"], "a1")
        self.assertEqual(lines[0]["agent_type"], "x")

    def test_31_unwritable_completion_log_fails_open_state_still_updates(self):
        blocker = os.path.join(self.root, "blocker")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("I am a file")
        bad = os.path.join(blocker, "completed-work.jsonl")
        self.create(1)
        r = self.post("TaskUpdate", {"taskId": "1", "status": "completed"},
                      {"success": True, "taskId": "1", "updatedFields": ["status"], "statusChange": {}},
                      env={"TASK_GATE_COMPLETION_LOG": bad})
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.sA_state()["1"]["status"], "completed")
        self.assertFalse(os.path.exists(self.clog))

    def test_32_completion_nudge_still_printed_with_log_enabled(self):
        self.create(1)
        self.create(2)
        text = self.ctx(self.update(1, status="completed"))
        self.assertIn("Task #1 completed", text)
        self.assertIn('#2 "probe"', text)
        self.assertEqual(len(self.clog_lines()), 1)




class SendMessageTracking(T):
    AID = "ad131687c5a508974"

    def launch(self, sub="X", name=None, resp=None):
        ti = {"description": "d", "prompt": "p", "subagent_type": sub}
        if name:
            ti["name"] = name
        resp = resp if resp is not None else {"isAsync": True, "status": "async_launched", "agentId": self.AID}
        return self.post("Agent", ti, resp)

    def send(self, to, sid="sA"):
        return self.post("SendMessage", {"to": to, "message": "m", "summary": "s"}, {"success": True}, sid)

    def state_of(self, sid="sA"):
        with open(os.path.join(self.state, sid + ".json"), encoding="utf-8") as f:
            return json.load(f)

    def setup_undispatched(self):
        """Task 1 dispatched to X, done; task 2 now in_progress for X (resume case) -> nag."""
        self.create(1)
        self.update(1, status="in_progress", owner="X")
        self.pre("X")
        self.launch()
        self.update(1, status="completed")
        self.create(2)
        self.update(2, status="in_progress", owner="X")

    def test_agent_dispatch_recorded(self):
        self.create(1)
        self.update(1, status="in_progress", owner="X")
        self.pre("X")
        self.launch(name="nm")
        self.assertEqual(self.state_of()["_agents"], {self.AID: "X", "nm": "X"})

    def test_agent_id_in_text_response(self):
        self.launch(resp=[{"type": "text", "text": "Async agent launched.\nagentId: %s (internal)" % self.AID}])
        self.assertEqual(self.state_of()["_agents"], {self.AID: "X"})

    def test_sendmessage_marks_dispatched_and_nag_disappears(self):
        self.setup_undispatched()
        self.assertIn("Still not dispatched: #2", self.launch().stdout)
        self.send(self.AID)
        self.assertTrue(self.state_of()["2"]["dispatched"])
        self.assertNotIn("Still not dispatched", self.launch().stdout)

    def test_sendmessage_by_name(self):
        self.create(1)
        self.update(1, status="in_progress", owner="X")
        self.launch(name="nm")
        self.update(1, status="in_progress", owner="X")  # no reset: same status
        self.update(1, owner="Y")
        self.update(1, owner="X")
        self.assertFalse(self.state_of()["1"]["dispatched"])
        self.send("nm")
        self.assertTrue(self.state_of()["1"]["dispatched"])

    def test_sendmessage_executor_name_fallback(self):
        self.create(1)
        self.update(1, status="in_progress", owner="X")
        self.send("X")
        self.assertTrue(self.state_of()["1"]["dispatched"])

    def test_sendmessage_unknown_target_changes_nothing(self):
        self.setup_undispatched()
        before = self.state_of()
        self.send("zzz-unknown")
        self.assertEqual(self.state_of(), before)
        self.assertFalse(self.state_of()["2"]["dispatched"])

    def test_sendmessage_other_executor_untouched(self):
        self.setup_undispatched()
        self.create(3)
        self.update(3, status="in_progress", owner="Z")
        self.send(self.AID)
        st = self.state_of()
        self.assertTrue(st["2"]["dispatched"])
        self.assertFalse(st["3"]["dispatched"])


    # ---- reviewer model/effort gate ----
    def reviewer_setup(self, owner="pr-reviewer-1", sid="sA"):
        self.create(1, sid=sid)
        self.update(1, sid=sid, status="in_progress", owner=owner)

    def test_30_reviewer_non_opus_model_denied_and_not_marked_dispatched(self):
        self.reviewer_setup()
        r = self.pre("pr-reviewer-1", ti_extra={"model": "haiku"})
        self.assertEqual(decision(r), "deny")
        reason = json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("Opus", reason)
        self.assertIn("got model='haiku'", reason)
        self.assertIn("DENY Agent", self.log_text())
        self.assertIn("subagent=pr-reviewer-1 reviewer-model=haiku effort=None", self.log_text())
        # the denial marked nothing dispatched, so the owner is still listed as undispatched
        self.assertIs(self.sA_state()["1"]["dispatched"], False)
        self.assertIn("Still not dispatched: #1", self.ctx(self.post_agent("pr-reviewer-1")))

    def test_31_reviewer_model_rules(self):
        self.reviewer_setup()
        for model in [None, "", "opus", "Opus", "claude-opus-5-5"]:
            extra = {} if model is None else {"model": model}
            self.assertEqual(decision(self.pre("pr-reviewer-1", ti_extra=extra)), "allow", repr(model))
        for model in ["sonnet", "Sonnet", "claude-sonnet-5", "haiku", "claude-haiku-5-5", "opus-4"]:
            r = self.pre("pr-reviewer-1", ti_extra={"model": model})
            self.assertEqual(decision(r), "deny", model)
            self.assertIn("Opus", json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"])

    def test_32_reviewer_effort_rules(self):
        self.reviewer_setup()
        for effort in ["medium", "low", "Low"]:
            self.assertEqual(decision(self.pre("pr-reviewer-1", ti_extra={"effort": effort})), "deny", effort)
        for effort in ["high", "xhigh", "max"]:
            self.assertEqual(decision(self.pre("pr-reviewer-1", ti_extra={"effort": effort})), "allow", effort)
        self.assertEqual(decision(self.pre("pr-reviewer-1")), "allow")

    def test_33_opus_reviewer_with_haiku_denied(self):
        self.reviewer_setup(owner="opus-reviewer")
        self.assertEqual(decision(self.pre("opus-reviewer", ti_extra={"model": "haiku"})), "deny")
        self.assertEqual(decision(self.pre("opus-reviewer", ti_extra={"model": "opus"})), "allow")

    def test_34_executor_haiku_medium_unchanged(self):
        self.reviewer_setup(owner="kreeva-osh")
        self.assertEqual(decision(self.pre("kreeva-osh", ti_extra={"model": "haiku", "effort": "medium"})), "allow")
        self.assertEqual(decision(self.pre("kreeva-osh", ti_extra={"model": "haiku"})), "allow")

    def test_35_reviewer_dispatch_from_subagent_denied(self):
        self.reviewer_setup()
        r = self.pre("pr-reviewer-1", agent_id="agent-7", ti_extra={"model": "haiku", "effort": "low"})
        self.assertEqual(decision(r), "deny")
        self.assertIn("Subagents may not dispatch agents",
                      json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"])

    def test_36_reviewer_rule_runs_only_after_ownership_gate_passes_otherwise(self):
        # no in_progress task: the existing "No in_progress task" denial still applies to an allowed model
        r = self.pre("pr-reviewer-2")
        self.assertIn('No in_progress task owned by "pr-reviewer-2"',
                      json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"])

    # ---- auto-claim: one Agent call claims the ready task it names ----
    def agent_post(self, ti, tool_use_id, sid="sA"):
        return self.post("Agent", dict({"prompt": "p", "subagent_type": "X"}, **ti), {}, sid,
                         extra={"tool_use_id": tool_use_id})

    def test_40_autoclaim_from_description(self):
        self.create(3)
        self.assertEqual(self.pre("X", ti_extra={"description": "Qorvax · #3 fix"}).stdout, "")
        e = self.sA_state()["3"]
        self.assertEqual((e["status"], e["owner"], e["dispatched"]), ("in_progress", "X", True))
        self.assertIs(e["metadata"]["auto_claimed"], True)

    def test_41_prompt_is_never_read_for_the_task_ref(self):
        self.create(7)
        r = self.pre("X", ti_extra={"description": "no ref", "prompt": "work on task #7 now"})
        self.assertEqual(decision(r), "deny")
        self.assertEqual(self.sA_state()["7"]["status"], "pending")

    def test_41b_only_middle_dot_or_task_forms_claim(self):
        self.create(5)
        self.create(10)
        self.assertEqual(decision(self.pre("X", ti_extra={"description": "review PR #5"})), "deny")
        self.assertEqual(self.sA_state()["5"]["status"], "pending")
        self.assertEqual(self.pre("X", ti_extra={"description": "Qorvax Tel \u00b7 #10 fixes"}).stdout, "")
        self.assertEqual(self.sA_state()["10"]["owner"], "X")

    def test_41c_task_form_is_case_insensitive(self):
        self.create(10)
        self.assertEqual(self.pre("X", ti_extra={"description": "Task #10 fixes"}).stdout, "")
        self.assertEqual(self.sA_state()["10"]["owner"], "X")

    def test_42_autoclaim_denied_with_reason_when_blocked(self):
        self.create(1)
        self.create(2)
        self.create(3)
        self.update(3, addBlockedBy=["1", "2"])
        r = self.pre("X", ti_extra={"description": "task #3"})
        self.assertEqual(decision(r), "deny")
        reason = json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("Auto-claim of #3 failed: #3 is blocked by #1, #2.", reason)
        e = self.sA_state()["3"]
        self.assertEqual((e["status"], e["owner"]), ("pending", None))

    def test_43_autoclaim_denied_when_owned_by_someone_else(self):
        self.create(1)
        self.update(1, owner="Y")
        r = self.pre("X", ti_extra={"description": "task #1"})
        self.assertEqual(decision(r), "deny")
        self.assertIn("Auto-claim of #1 failed: #1 is owned by Y.",
                      json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertEqual(self.sA_state()["1"]["owner"], "Y")

    def test_44_autoclaim_denied_when_dispatch_false(self):
        self.create(1)
        self.update(1, metadata={"dispatch": False})
        r = self.pre("X", ti_extra={"description": "task #1"})
        self.assertEqual(decision(r), "deny")
        self.assertIn("Auto-claim of #1 failed: #1 has metadata dispatch false.",
                      json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertEqual(self.sA_state()["1"]["status"], "pending")

    def test_45_no_double_claim_when_executor_already_in_progress(self):
        self.create(1)
        self.create(2)
        self.update(1, status="in_progress", owner="X")
        self.assertEqual(self.pre("X", ti_extra={"description": "task #2"}).stdout, "")
        self.assertEqual((self.sA_state()["2"]["status"], self.sA_state()["2"]["owner"]), ("pending", None))
        self.assertEqual((self.sA_state()["1"]["status"], self.sA_state()["1"]["owner"]), ("in_progress", "X"))
        self.assertNotIn("auto_claimed", self.sA_state()["1"]["metadata"])

    def test_46_exempt_type_unaffected_by_autoclaim(self):
        self.create(3)
        r = self.pre("Explore", ti_extra={"description": "task #3"})
        self.assertEqual(r.stdout, "")
        self.assertEqual(self.sA_state()["3"]["status"], "pending")

    def test_47_completion_after_autoclaim_logs_and_nudges(self):
        self.create(1)
        self.create(3)
        self.assertEqual(self.pre("X", ti_extra={"description": "task #1"}).stdout, "")
        text = self.ctx(self.agent_post({"description": "task #1"}, "toolu_x"))
        self.assertIn("Auto-claimed task #1 for X (gate state only; the TaskList UI still shows it pending "
                      "— no TaskUpdate needed until completion).", text)
        self.assertIn("ready: #3", text)
        self.assertNotIn("_autoclaims", self.sA_state())
        # the marker is used once: the same call id again announces nothing
        self.assertNotIn("Auto-claimed", self.ctx(self.agent_post({"description": "task #1"}, "toolu_x")))
        text = self.ctx(self.update(1, status="completed"))
        self.assertIn("Task #1 completed", text)
        self.assertIn('#3 "probe"', text)
        lines = self.clog_lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual((lines[0]["task_id"], lines[0]["owner"]), ("1", "X"))
        self.assertEqual(self.sA_state()["1"]["status"], "completed")
        self.assertIs(self.sA_state()["1"]["metadata"]["auto_claimed"], True)
        # a repeat completion is not a transition: no second completion-log line
        self.update(1, status="completed")
        self.assertEqual(len(self.clog_lines()), 1)

    # ---- stale auto-claims: only the same executor may retake one ----
    def test_48_live_claim_not_taken_over_by_other_executor(self):
        self.create(3)
        self.assertEqual(self.pre("qorvax-tel", ti_extra={"description": "Qorvax · #3 fix"},
                                  tuid="toolu_A").stdout, "")
        r = self.pre("zyrene-vosk", ti_extra={"description": "Zyrene · #3 fix"}, tuid="toolu_B")
        self.assertEqual(decision(r), "deny")
        self.assertIn("TaskUpdate", json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"])
        e = self.sA_state()["3"]
        self.assertEqual((e["status"], e["owner"]), ("in_progress", "qorvax-tel"))
        self.assertEqual(e["metadata"]["auto_claim_pending"], "toolu_A")

    def test_49_stale_claim_retaken_by_same_executor_and_marker_replaced(self):
        self.create(3)
        self.assertEqual(self.pre("qorvax-tel", ti_extra={"description": "task #3"}, tuid="toolu_A").stdout, "")
        self.assertEqual(self.pre("qorvax-tel", ti_extra={"description": "task #3"}, tuid="toolu_B").stdout, "")
        state = self.sA_state()
        self.assertEqual((state["3"]["status"], state["3"]["owner"]), ("in_progress", "qorvax-tel"))
        self.assertEqual(state["3"]["metadata"]["auto_claim_pending"], "toolu_B")
        self.assertEqual(list(state["_autoclaims"]), ["toolu_B"])
        self.agent_post({"description": "task #3"}, "toolu_B")
        self.assertNotIn("auto_claim_pending", self.sA_state()["3"]["metadata"])
        self.assertNotIn("_autoclaims", self.sA_state())

    def test_50_after_post_other_executor_still_denied(self):
        self.create(3)
        self.pre("qorvax-tel", ti_extra={"description": "task #3"}, tuid="toolu_A")
        self.agent_post({"description": "task #3"}, "toolu_A")
        self.assertNotIn("auto_claim_pending", self.sA_state()["3"]["metadata"])
        r = self.pre("zyrene-vosk", ti_extra={"description": "task #3"}, tuid="toolu_B")
        self.assertEqual(decision(r), "deny")
        self.assertEqual(self.sA_state()["3"]["owner"], "qorvax-tel")


class ModelGate(unittest.TestCase):
    """Model gate for Agent|Task from the main session: executors run on Haiku; a non-Haiku model needs
    proven escalation (a prior Haiku round, metadata.escalate_reason, and a model in model_gate.escalate_to).
    Borrows T's harness helpers without inheriting its test methods (no duplicate runs)."""
    setUp = T.setUp
    tearDown = T.tearDown
    _run = T._run
    pre = T.pre
    post = T.post
    create = T.create
    update = T.update
    clog_lines = T.clog_lines
    log_text = T.log_text
    sA_state = T.sA_state

    def write_cfg(self, obj, name="harness.json"):
        path = os.path.join(self.root, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        return {"CLAUDE_HARNESS_CONFIG": path}

    def sf(self):
        """Harness config that turns Sonnet escalation on (model_gate.sonnet_fallback)."""
        return self.write_cfg({"model_gate": {"sonnet_fallback": True}}, name="sonnet.json")

    def mg(self, model=None, sub="X", task=1, sid="sA", effort=None, env=None):
        ti = {"description": "task #%d" % task} if task else {}
        if model is not None:
            ti["model"] = model
        if effort is not None:
            ti["effort"] = effort
        return self.pre(sub, sid=sid, ti_extra=ti, env=env)

    def reason(self, r):
        return json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]

    def rounds(self, tid="1", sid="sA"):
        return self.sA_state(sid)[tid]["rounds"]

    def test_mg01_executor_model_omitted_allowed_and_round_recorded(self):
        self.create(1)
        self.assertEqual(decision(self.mg()), "allow")
        st = self.sA_state()["1"]
        self.assertEqual(st["owner"], "X")
        self.assertTrue(st["dispatched"])
        self.assertEqual([r["model"] for r in st["rounds"]], ["haiku"])
        self.assertIsNone(st["rounds"][0]["effort"])
        self.assertTrue(st["rounds"][0]["ts"].endswith("Z"))

    def test_mg02_executor_haiku_models_allowed(self):
        self.create(1)
        self.assertEqual(decision(self.mg(model="haiku")), "allow")
        self.create(1, sid="sB")
        self.assertEqual(decision(self.mg(model="claude-haiku-5-5", sid="sB")), "allow")
        self.assertEqual(self.rounds("1")[0]["model"], "haiku")
        self.assertEqual(self.rounds("1", sid="sB")[0]["model"], "claude-haiku-5-5")

    def test_mg03_opus_first_round_denied_and_nothing_claimed(self):
        self.create(1)
        r = self.mg(model="opus")
        self.assertEqual(decision(r), "deny")
        self.assertIn("no earlier Haiku round", self.reason(r))
        self.assertIn("opus is never allowed for executors", self.reason(r))
        self.assertIn("model-gate=escalation", self.log_text())
        st = self.sA_state()["1"]
        self.assertEqual(st["status"], "pending")
        self.assertFalse(st["dispatched"])
        self.assertEqual(st["rounds"], [])
        self.assertEqual(self.clog_lines(), [])

    def test_mg04_opus_with_prior_round_but_no_reason_denied(self):
        self.create(1)
        self.assertEqual(decision(self.mg()), "allow")
        r = self.mg(model="opus")
        self.assertEqual(decision(r), "deny")
        self.assertIn("no escalate_reason", self.reason(r))
        self.assertEqual(len(self.rounds()), 1)
        self.assertEqual(self.clog_lines(), [])

    def test_mg05_escalation_with_prior_round_and_reason_allowed_and_logged(self):
        self.create(1)
        self.assertEqual(decision(self.mg()), "allow")
        self.update(1, metadata={"escalate_reason": "acceptance check 3 failed: tests red", "escalate_class": "coupled"})
        self.assertEqual(decision(self.mg(model="sonnet", env=self.sf(), effort="high")), "allow")
        self.assertEqual([r["model"] for r in self.rounds()], ["haiku", "sonnet"])
        self.assertEqual(self.rounds()[1]["effort"], "high")
        lines = self.clog_lines()
        self.assertEqual(len(lines), 1)
        rec = lines[0]
        self.assertEqual(rec["kind"], "escalation")
        self.assertEqual(rec["task_id"], "1")
        self.assertEqual(rec["subagent"], "X")
        self.assertEqual(rec["model"], "sonnet")
        self.assertEqual(rec["effort"], "high")
        self.assertEqual(rec["escalate_reason"], "acceptance check 3 failed: tests red")
        self.assertEqual(rec["class"], "coupled")
        self.assertEqual(rec["session_id"], "sA")

    def test_mg06_escalate_to_default_is_sonnet_and_opus(self):
        self.create(1)
        self.assertEqual(decision(self.mg()), "allow")
        self.update(1, metadata={"escalate_reason": "check failed", "escalate_class": "scale"})
        cfg = self.write_cfg({"model_gate": {"escalate_to": ["opus"], "sonnet_fallback": True}})
        r = self.mg(model="sonnet", env=cfg)
        self.assertEqual(decision(r), "deny")
        self.assertIn("not in model_gate.escalate_to", self.reason(r))
        self.assertEqual(self.clog_lines(), [])
        self.assertEqual(len(self.rounds()), 1)
        self.assertEqual(decision(self.mg(model="sonnet", env=self.sf())), "allow")
        self.assertEqual([r["model"] for r in self.rounds()], ["haiku", "sonnet"])

    def test_mg07_non_executor_types_denied_from_main(self):
        self.create(1)
        for sub in ["general-purpose", "fork", "claude", "statusline-setup", "Plan", "no-such-agent"]:
            r = self.pre(sub, ti_extra={"description": "task #1"})
            self.assertEqual(decision(r), "deny", sub)
            self.assertIn("is not allowed from the main session", self.reason(r), sub)
        r = self.pre(omit_subagent=True)
        self.assertEqual(decision(r), "deny")
        self.assertIn("is not allowed", self.reason(r))
        self.assertIn("model-gate=type", self.log_text())
        self.assertEqual(self.sA_state()["1"]["status"], "pending")

    def test_mg08_explore_and_helper_types_still_exempt(self):
        for sub in ["Explore", "web-fetch", "claude-code-guide"]:
            self.assertEqual(decision(self.pre(sub)), "allow", sub)

    def test_mg09_custom_agent_needs_allow_types(self):
        self.create(1)
        r = self.mg(sub="opus-custom")
        self.assertEqual(decision(r), "deny")
        self.assertIn("is not allowed from the main session", self.reason(r))
        cfg = self.write_cfg({"model_gate": {"allow_types": ["opus-custom"]}})
        self.assertEqual(decision(self.mg(sub="opus-custom", env=cfg)), "deny")  # omitted is not enough
        self.assertEqual(decision(self.mg(model="haiku", sub="opus-custom", env=cfg)), "allow")
        self.assertEqual(self.rounds()[0]["model"], "haiku")

    def test_mg10_unsafe_names_never_read(self):
        with open(os.path.join(self.root, "evil.md"), "w", encoding="utf-8") as f:
            f.write("---\nmodel: haiku\n---\n")
        self.create(1)
        for sub in ["../evil", "a/b", ".hidden"]:
            r = self.pre(sub, ti_extra={"description": "task #1"})
            self.assertEqual(decision(r), "deny", sub)
            self.assertIn("is not allowed", self.reason(r), sub)

    def test_mg11_old_state_without_rounds_loads(self):
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, "sA.json"), "w", encoding="utf-8") as f:
            json.dump({"1": {"subject": "old", "status": "in_progress", "owner": "X"}}, f)
        self.assertEqual(decision(self.mg(task=None)), "allow")
        self.assertEqual([r["model"] for r in self.rounds()], ["haiku"])

    def test_mg12_completed_task_cannot_be_escalated(self):
        self.create(1)
        self.assertEqual(decision(self.mg()), "allow")
        self.update(1, status="completed")
        self.update(1, metadata={"escalate_reason": "late"})
        r = self.mg(model="opus")
        self.assertEqual(decision(r), "deny")
        self.assertIn("already completed", self.reason(r))

    def test_mg13_sonnet_escalation_allowed_with_reason_and_class(self):
        self.create(1)
        self.assertEqual(decision(self.mg()), "allow")
        self.update(1, metadata={"escalate_reason": "check 2 failed: pixdiff", "escalate_class": "verify"})
        self.assertEqual(decision(self.mg(model="sonnet", env=self.sf())), "allow")
        self.assertEqual([r["model"] for r in self.rounds()], ["haiku", "sonnet"])
        self.assertEqual(self.clog_lines()[0]["kind"], "escalation")

    def test_mg14_escalation_without_class_denied(self):
        self.create(1)
        self.assertEqual(decision(self.mg()), "allow")
        self.update(1, metadata={"escalate_reason": "check failed"})
        r = self.mg(model="sonnet", env=self.sf())
        self.assertEqual(decision(r), "deny")
        self.assertIn("no valid escalate_class", self.reason(r))
        self.assertIn("scale, verify, debug, coupled", self.reason(r))
        self.assertIn("model-gate=escalation", self.log_text())
        self.assertEqual(len(self.rounds()), 1)
        self.assertEqual(self.clog_lines(), [])

    def test_mg15_escalation_with_invalid_class_denied_case_sensitive(self):
        self.create(1)
        self.assertEqual(decision(self.mg()), "allow")
        for bad in ["Debug", "urgent", ""]:
            self.update(1, metadata={"escalate_reason": "check failed", "escalate_class": bad})
            r = self.mg(model="sonnet", env=self.sf())
            self.assertEqual(decision(r), "deny", bad)
            self.assertIn("no valid escalate_class", self.reason(r), bad)
        self.assertEqual(len(self.rounds()), 1)

    def test_mg16_opus_denied_with_debug_class_and_no_sonnet_round(self):
        self.create(1)
        self.assertEqual(decision(self.mg()), "allow")
        self.update(1, metadata={"escalate_reason": "check failed", "escalate_class": "debug"})
        r = self.mg(model="opus")
        self.assertEqual(decision(r), "deny")
        self.assertIn("opus is never allowed for executors", self.reason(r))
        self.assertIn("Sonnet is the only escalation", self.reason(r))
        self.assertEqual(len(self.rounds()), 1)
        self.assertEqual(self.clog_lines(), [])

    def test_mg17_opus_denied_even_with_coupled_and_prior_haiku_and_sonnet_rounds(self):
        self.create(1)
        self.assertEqual(decision(self.mg()), "allow")
        self.update(1, metadata={"escalate_reason": "check 3 failed", "escalate_class": "scale"})
        self.assertEqual(decision(self.mg(model="sonnet", env=self.sf())), "allow")
        self.update(1, metadata={"escalate_reason": "hinge clash in cover geometry", "escalate_class": "coupled"})
        for model in ["opus", "claude-opus-5-5", "Opus"]:
            r = self.mg(model=model)
            self.assertEqual(decision(r), "deny", model)
            self.assertIn("opus is never allowed for executors", self.reason(r))
        self.assertEqual([r["model"] for r in self.rounds()], ["haiku", "sonnet"])
        self.assertEqual(self.sA_state()["1"]["metadata"]["escalate_class"], "coupled")
        self.assertEqual(len(self.clog_lines()), 1)

    def test_mg18_opus_denied_after_sonnet_round_with_class_debug(self):
        self.create(1)
        self.assertEqual(decision(self.mg()), "allow")
        self.update(1, metadata={"escalate_reason": "check 3 failed", "escalate_class": "scale"})
        self.assertEqual(decision(self.mg(model="sonnet", env=self.sf())), "allow")
        self.update(1, metadata={"escalate_reason": "still red after sonnet", "escalate_class": "debug"})
        r = self.mg(model="opus")
        self.assertEqual(decision(r), "deny")
        self.assertIn("opus is never allowed for executors", self.reason(r))
        self.assertEqual([r["model"] for r in self.rounds()], ["haiku", "sonnet"])

    def test_mg19_reason_and_class_cleared_after_allowed_round(self):
        self.create(1)
        self.assertEqual(decision(self.mg()), "allow")
        self.update(1, metadata={"escalate_reason": "check 3 failed", "escalate_class": "verify"})
        self.assertEqual(decision(self.mg(model="sonnet", env=self.sf())), "allow")
        md = self.sA_state()["1"]["metadata"]
        self.assertNotIn("escalate_reason", md)
        self.assertNotIn("escalate_class", md)
        r = self.mg(model="sonnet", env=self.sf())
        self.assertEqual(decision(r), "deny")
        self.assertIn("no escalate_reason", self.reason(r))
        self.assertIn("no valid escalate_class", self.reason(r))
        self.assertEqual(len(self.rounds()), 2)
        self.update(1, metadata={"escalate_reason": "fresh reason", "escalate_class": "debug"})
        self.assertEqual(decision(self.mg(model="sonnet", env=self.sf())), "allow")
        self.assertEqual(len(self.rounds()), 3)

    def test_mg20_round_cap_denies_fourth_round_until_allow_extra_rounds(self):
        self.create(1)
        for _ in range(3):
            self.assertEqual(decision(self.mg()), "allow")
        self.assertEqual(len(self.rounds()), 3)
        r = self.mg()
        self.assertEqual(decision(r), "deny")
        self.assertIn("Task #1 already used 3 rounds (cap 3)", self.reason(r))
        self.assertIn("after their OK set metadata.allow_extra_rounds", self.reason(r))
        self.assertEqual(len(self.rounds()), 3)
        self.update(1, metadata={"allow_extra_rounds": "1"})
        self.assertEqual(decision(self.mg()), "deny")
        self.update(1, metadata={"allow_extra_rounds": 1})
        self.assertEqual(decision(self.mg()), "allow")
        self.assertEqual(len(self.rounds()), 4)

    def test_mg21_reviewer_dispatch_unaffected_by_round_cap(self):
        self.create(1)
        self.update(1, status="in_progress", owner="X")
        for _ in range(3):
            self.assertEqual(decision(self.mg()), "allow")
        self.assertEqual(decision(self.mg()), "deny")
        self.update(1, owner="pr-reviewer-1")
        self.assertEqual(decision(self.pre("pr-reviewer-1", ti_extra={"model": "opus"})), "allow")
        self.assertEqual(len(self.rounds()), 3)

    def test_mg26_opus_denied_even_when_local_config_lists_opus(self):
        self.create(1)
        self.assertEqual(decision(self.mg()), "allow")
        self.update(1, metadata={"escalate_reason": "hinge clash", "escalate_class": "coupled"})
        cfg = self.write_cfg({"model_gate": {"escalate_to": ["opus", "sonnet"], "sonnet_fallback": True}})
        r = self.mg(model="opus", env=cfg)
        self.assertEqual(decision(r), "deny")
        self.assertIn("opus is never allowed for executors", self.reason(r))
        self.assertEqual(len(self.rounds()), 1)
        self.assertEqual(self.clog_lines(), [])
        self.assertEqual(decision(self.mg(model="sonnet", env=cfg)), "allow")
        self.assertEqual([r["model"] for r in self.rounds()], ["haiku", "sonnet"])

    def test_mg27_reviewer_sonnet_denied_omitted_and_opus_allowed(self):
        self.create(1)
        self.update(1, status="in_progress", owner="pr-reviewer-1")
        self.assertEqual(decision(self.pre("pr-reviewer-1", ti_extra={"model": "sonnet"})), "deny")
        self.assertEqual(decision(self.pre("pr-reviewer-1")), "allow")
        self.assertEqual(decision(self.pre("pr-reviewer-1", ti_extra={"model": "opus"})), "allow")

    def test_mg28_subagent_agent_dispatch_denied_with_and_without_model(self):
        for sub in ["kreeva-osh", "opus-reviewer", "pr-reviewer-1", "general-purpose"]:
            for ti_extra in [{}, {"model": "haiku"}, {"model": "sonnet"}, {"model": "opus"}]:
                r = self.pre(sub, agent_id="agent-9", ti_extra=ti_extra)
                self.assertEqual(decision(r), "deny", (sub, ti_extra))
                self.assertIn("Subagents may not dispatch agents", self.reason(r))
        self.assertIn("subagent-dispatch", self.log_text())

    def test_mg29_subagent_dispatch_denied_under_kill_switches(self):
        for env in [{"CLAUDE_REQUIRE_TASK": "0"}, {"CLAUDE_MAIN_WRITE": "1"}]:
            r = self.pre("kreeva-osh", agent_id="agent-9", ti_extra={"model": "haiku"}, env=env)
            self.assertEqual(decision(r), "deny", env)
            self.assertIn("Subagents may not dispatch agents", self.reason(r))

    def test_mg30_opus_executor_denied_with_require_task_off(self):
        off = {"CLAUDE_REQUIRE_TASK": "0"}
        for model in ["opus", "claude-opus-5-5", "Opus"]:
            r = self.pre("kreeva-osh", ti_extra={"model": model}, env=off)
            self.assertEqual(decision(r), "deny", model)
            self.assertIn("opus is never allowed for executors", self.reason(r))
        self.assertIn("model-gate=escalation", self.log_text())

    def test_mg31_haiku_executor_allowed_with_require_task_off(self):
        off = {"CLAUDE_REQUIRE_TASK": "0"}
        for ti_extra in [{}, {"model": "haiku"}, {"model": "claude-haiku-5-5"}]:
            r = self.pre("kreeva-osh", ti_extra=ti_extra, env=off)
            self.assertEqual(decision(r), "allow", ti_extra)
            self.assertEqual(r.stdout, "")
        self.assertEqual(decision(self.pre("kreeva-osh", ti_extra={"model": "sonnet"}, env=off)), "deny")

    def test_mg25_cap_refusal_denies_with_cap_text_only(self):
        self.create(1)
        self.update(1, status="in_progress", owner="X")
        for _ in range(3):
            self.assertEqual(decision(self.mg()), "allow")
        r = self.mg()
        self.assertEqual(decision(r), "deny")
        reason = self.reason(r)
        self.assertIn("Task #1 already used 3 rounds (cap 3). Stop and ask the user", reason)
        self.assertNotIn("No in_progress task owned", reason)
        self.assertNotIn("open blockers", reason)
        # guard against over-fixing: an ordinary no-owned-task denial keeps its ownership prefix
        r2 = self.pre("Y")
        self.assertEqual(decision(r2), "deny")
        self.assertIn("No in_progress task owned by \"Y\"", self.reason(r2))

    def test_mg22_escalation_log_record_carries_class(self):
        self.create(1)
        self.assertEqual(decision(self.mg()), "allow")
        self.update(1, metadata={"escalate_reason": "check 2 failed", "escalate_class": "scale"})
        self.assertEqual(decision(self.mg(model="sonnet", env=self.sf(), effort="medium")), "allow")
        lines = self.clog_lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["kind"], "escalation")
        self.assertEqual(lines[0]["class"], "scale")
        self.assertEqual(lines[0]["escalate_reason"], "check 2 failed")
        self.assertEqual(lines[0]["model"], "sonnet")

    def test_mg23_task_completion_log_line_carries_rounds(self):
        self.create(1)
        self.assertEqual(decision(self.mg(effort="medium")), "allow")
        self.update(1, metadata={"escalate_reason": "check 2 failed", "escalate_class": "verify"})
        self.assertEqual(decision(self.mg(model="sonnet", env=self.sf(), effort="high")), "allow")
        self.update(1, status="completed")
        task_lines = [x for x in self.clog_lines() if x["kind"] == "task"]
        self.assertEqual(len(task_lines), 1)
        self.assertEqual(task_lines[0]["rounds"], [{"model": "haiku", "effort": "medium"},
                                                   {"model": "sonnet", "effort": "high"}])
        self.assertNotIn("ts", task_lines[0]["rounds"][0])

    def test_mg24_max_rounds_from_config_and_invalid_values_default_to_three(self):
        cfg = self.write_cfg({"model_gate": {"max_rounds": 2}})
        self.create(1)
        self.assertEqual(decision(self.mg(env=cfg)), "allow")
        self.assertEqual(decision(self.mg(env=cfg)), "allow")
        r = self.mg(env=cfg)
        self.assertEqual(decision(r), "deny")
        self.assertIn("(cap 2)", self.reason(r))
        cfg0 = self.write_cfg({"model_gate": {"max_rounds": 0}}, name="zero.json")
        self.create(2, sid="sB")
        for _ in range(3):
            self.assertEqual(decision(self.mg(task=2, sid="sB", env=cfg0)), "allow")
        r = self.mg(task=2, sid="sB", env=cfg0)
        self.assertEqual(decision(r), "deny")
        self.assertIn("(cap 3)", self.reason(r))


    def test_mg32_exempt_types_haiku_or_omitted_only(self):
        self.assertEqual(decision(self.pre("Explore")), "allow")
        self.assertEqual(decision(self.pre("Explore", ti_extra={"model": "haiku"})), "allow")
        r = self.pre("Explore", ti_extra={"model": "opus"})
        self.assertEqual(decision(r), "deny")
        self.assertIn("Exempt type Explore must run on Haiku (model omitted or haiku)", self.reason(r))
        self.assertEqual(decision(self.pre("web-fetch", ti_extra={"model": "sonnet"})), "deny")
        self.assertEqual(decision(self.pre("claude-code-guide", ti_extra={"model": "opus[1m]"})), "deny")
        self.assertIn("model-gate=exempt", self.log_text())

    def test_mg33_escalation_opus_variants_denied_even_if_local_config_lists_them(self):
        self.create(1)
        self.assertEqual(decision(self.mg()), "allow")
        self.update(1, metadata={"escalate_reason": "hinge clash", "escalate_class": "coupled"})
        cfg = self.write_cfg({"model_gate": {"escalate_to": ["opus[1m]", "us.anthropic.claude-opus-5-5", "sonnet"]}})
        for model in ["opus[1m]", "us.anthropic.claude-opus-5-5", "Opus"]:
            r = self.mg(model=model, env=cfg)
            self.assertEqual(decision(r), "deny", model)
            self.assertIn("opus is never allowed for executors", self.reason(r), model)
        self.assertEqual(len(self.rounds()), 1)
        self.assertEqual(self.clog_lines(), [])

    def test_mg34_escalation_fable_and_other_non_sonnet_models_denied(self):
        self.create(1)
        self.assertEqual(decision(self.mg()), "allow")
        self.update(1, metadata={"escalate_reason": "check failed", "escalate_class": "debug"})
        for model in ["fable", "fable-5", "some-unknown-model"]:
            r = self.mg(model=model)
            self.assertEqual(decision(r), "deny", model)
            self.assertIn("not sonnet-family", self.reason(r), model)
        cfg = self.write_cfg({"model_gate": {"escalate_to": ["fable"]}})
        r = self.mg(model="fable", env=cfg)
        self.assertEqual(decision(r), "deny")
        self.assertIn("not sonnet-family", self.reason(r))
        self.assertEqual(len(self.rounds()), 1)
        self.assertEqual(self.clog_lines(), [])

    def test_mg35_allow_types_need_explicit_haiku(self):
        cfg = self.write_cfg({"model_gate": {"allow_types": ["Plan"]}})
        r = self.mg(sub="Plan", env=cfg)  # model omitted: a Plan agent inherits the parent model (Opus)
        self.assertEqual(decision(r), "deny")
        self.assertIn("model_gate.allow_types", self.reason(r))
        self.assertIn("model-gate=type", self.log_text())
        self.create(1)
        self.assertEqual(decision(self.mg(model="sonnet", sub="Plan", env=cfg)), "deny")
        self.assertEqual(decision(self.mg(model="haiku", sub="Plan", env=cfg)), "allow")
        self.assertEqual(self.rounds()[0]["model"], "haiku")


class Pool(unittest.TestCase):
    """Executor pool (ported from the gist fork): cap, running tracking, foreground refusal, timestamps,
    SubagentStop, pool line, config and env settings, and the switchable background rule. Borrows T's
    harness helpers without inheriting T's tests."""
    setUp = T.setUp
    tearDown = T.tearDown
    _run = T._run
    post = T.post
    create = T.create
    update = T.update
    log_text = T.log_text

    def pre(self, subagent="X", sid="sA", env=None, ti_extra=None, omit_bg=False, **kw):
        """Agent PreToolUse; run_in_background is true unless the test sets it (executors run in the background)."""
        extra = dict(ti_extra or {})
        if not omit_bg:
            extra.setdefault("run_in_background", True)
        return T.pre(self, subagent, sid=sid, env=env, ti_extra=extra, omit_bg=omit_bg, **kw)

    def ctx(self, r):
        return json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"] if r.stdout.strip() else ""

    def sA_state(self, sid="sA"):
        with open(os.path.join(self.state, sid + ".json"), encoding="utf-8") as f:
            return json.load(f)

    def reason(self, r):
        return json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]

    def pool(self, n):
        return {"TASK_GATE_POOL_SIZE": str(n)}

    def write_cfg(self, name, data):
        path = os.path.join(self.root, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return {"CLAUDE_HARNESS_CONFIG": path}

    def launch(self, owner, env=None):
        """The executor's Agent PostToolUse."""
        return self.post("Agent", {"description": "d", "prompt": "p", "subagent_type": owner}, {}, env=env)

    def start(self, tid, owner, env=None):
        """Owner claims #tid (TaskUpdate), its Agent call is allowed (PreToolUse) and the call posts."""
        self.update(tid, status="in_progress", owner=owner)
        self.assertEqual(self.pre(owner, env=env).stdout, "")
        self.launch(owner, env)

    def complete(self, tid, env=None):
        return self.post("TaskUpdate", {"taskId": str(tid), "status": "completed"},
                         {"success": True, "taskId": str(tid), "updatedFields": ["status"], "statusChange": {}},
                         env=env)

    def stop(self, agent_type, sid="sA"):
        return self._run(json.dumps({"session_id": sid, "hook_event_name": "SubagentStop",
                                     "agent_type": agent_type, "agent_id": "a1"}))

    def edit(self, fn, sid="sA"):
        path = os.path.join(self.state, sid + ".json")
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
        fn(st)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(st, f)

    def raw_state(self):
        with open(os.path.join(self.state, "sA.json"), "rb") as f:
            return f.read()

    def test_pool_full_denied_without_touching_state(self):
        env = self.pool(2)
        for i in (1, 2, 3):
            self.create(i)
        self.start(1, "qorvax-tel", env)
        self.start(2, "zyrene-vosk", env)
        self.update(3, status="in_progress", owner="thal-ummir")
        before = self.raw_state()
        r = self.pre("thal-ummir", env=env)
        self.assertEqual(decision(r), "deny")
        self.assertIn("Executor pool full: 2 of 2 running (#1, #2). Wait for a report, mark it completed, "
                      "then dispatch.", self.reason(r))
        self.assertIn("capacity=Executor pool full", self.log_text())
        self.assertEqual(self.raw_state(), before)
        # completing #1 frees a slot
        self.complete(1)
        self.assertEqual(self.pre("thal-ummir", env=env).stdout, "")

    def test_stale_running_not_counted(self):
        env = self.pool(1)
        self.create(1)
        self.create(2)
        self.start(1, "qorvax-tel", env)
        self.update(2, status="in_progress", owner="zyrene-vosk")
        self.assertEqual(decision(self.pre("zyrene-vosk", env=env)), "deny")  # fresh: running
        self.edit(lambda st: st["1"].update(dispatched_at=time.time() - 7200))  # past the 3600 s default
        self.assertEqual(self.pre("zyrene-vosk", env=env).stdout, "")

    def test_pending_counted_within_120s_only(self):
        env = self.pool(1)
        self.create(1)
        self.create(2)
        self.update(1, status="in_progress", owner="qorvax-tel")
        self.assertEqual(self.pre("qorvax-tel", env=env).stdout, "")  # allowed; no PostToolUse yet
        self.update(2, status="in_progress", owner="zyrene-vosk")
        self.assertEqual(decision(self.pre("zyrene-vosk", env=env)), "deny")
        self.edit(lambda st: st["1"].update(dispatch_pending_at=time.time() - 200))
        self.assertEqual(self.pre("zyrene-vosk", env=env).stdout, "")

    def test_reviewers_not_counted_or_capped(self):
        env = self.pool(1)
        for i in (1, 2, 3):
            self.create(i)
        self.start(1, "qorvax-tel", env)  # pool of 1 is full for executors
        self.update(2, status="in_progress", owner="pr-reviewer-1")
        # #3 is ready, so a foreground executor call would be refused; a reviewer is not capped
        self.assertEqual(self.pre("pr-reviewer-1", env=env, ti_extra={"run_in_background": False}).stdout, "")
        self.assertNotIn("dispatch_pending_at", self.sA_state()["2"])

    def test_foreground_refused_only_while_other_tasks_ready(self):
        self.create(1)
        self.create(2)
        self.update(1, status="in_progress", owner="qorvax-tel")
        before = self.raw_state()
        fg = {"run_in_background": False}
        r = self.pre("qorvax-tel", ti_extra=fg)
        self.assertEqual(decision(r), "deny")
        self.assertIn("Dispatch executors with run_in_background: true while other tasks are ready: #2.",
                      self.reason(r))
        self.assertEqual(self.raw_state(), before)
        self.assertEqual(self.pre("qorvax-tel", ti_extra={"run_in_background": True}).stdout, "")
        # the key omitted counts as foreground: denied while #2 is ready
        r = self.pre("qorvax-tel", omit_bg=True)
        self.assertEqual(decision(r), "deny")
        self.update(2, metadata={"dispatch": False})  # no task is ready any more
        self.assertEqual(self.pre("qorvax-tel", ti_extra=fg).stdout, "")
        self.assertEqual(self.pre("qorvax-tel", omit_bg=True).stdout, "")  # omitted and no ready task: allowed

    def test_post_sets_dispatched_at_and_clears_finished_and_pending(self):
        self.create(1)
        self.create(2)
        self.update(1, status="in_progress", owner="qorvax-tel")
        self.update(2, status="in_progress", owner="pr-reviewer-1")
        self.assertEqual(self.pre("qorvax-tel").stdout, "")
        self.edit(lambda st: st["1"].update(finished_at=1.0))
        self.launch("qorvax-tel")
        e = self.sA_state()["1"]
        self.assertIsInstance(e["dispatched_at"], float)
        self.assertNotIn("finished_at", e)
        self.assertNotIn("dispatch_pending_at", e)
        self.launch("pr-reviewer-1")  # not an executor: no dispatched_at
        self.assertNotIn("dispatched_at", self.sA_state()["2"])

    def test_subagent_stop_sets_finished_at_for_owner_only_once(self):
        self.create(1)
        self.create(2)
        self.start(1, "qorvax-tel")
        self.start(2, "zyrene-vosk")
        self.assertEqual(self.stop("qorvax-tel").stdout, "")
        st = self.sA_state()
        self.assertIsInstance(st["1"]["finished_at"], float)
        self.assertNotIn("finished_at", st["2"])
        first = st["1"]["finished_at"]
        self.stop("qorvax-tel")
        self.assertEqual(self.sA_state()["1"]["finished_at"], first)
        self.assertEqual(self.stop("nobody").stdout, "")  # no matching entries: nothing, no error

    def test_subagent_stop_frees_slot(self):
        env = self.pool(1)
        self.create(1)
        self.create(2)
        self.start(1, "qorvax-tel", env)
        self.update(2, status="in_progress", owner="zyrene-vosk")
        self.assertEqual(decision(self.pre("zyrene-vosk", env=env)), "deny")  # the one slot is running
        self.stop("qorvax-tel")
        self.assertEqual(self.pre("zyrene-vosk", env=env).stdout, "")  # SubagentStop freed it

    def test_foreground_order_leaves_task_not_running(self):
        # Pre (allowed, foreground) -> SubagentStop -> PostToolUse Agent: the finished run is not running
        env = self.pool(1)
        self.create(1)
        self.update(1, status="in_progress", owner="qorvax-tel")
        self.assertEqual(self.pre("qorvax-tel", env=env, ti_extra={"run_in_background": False}).stdout, "")
        self.stop("qorvax-tel")
        self.launch("qorvax-tel", env)
        e = self.sA_state()["1"]
        self.assertIsInstance(e["dispatched_at"], float)
        self.assertIsInstance(e["finished_at"], float)  # kept: SubagentStop came before the Post
        self.assertGreaterEqual(e["finished_at"], e["dispatched_at"])
        self.assertNotIn("dispatch_pending_at", e)
        # pool of 1 has a free slot: #1 is not running (#2 is created only now, so the foreground call was allowed)
        self.create(2)
        self.update(2, status="in_progress", owner="zyrene-vosk")
        self.assertEqual(self.pre("zyrene-vosk", env=env).stdout, "")

    def test_background_order_running_until_stop(self):
        # Pre -> PostToolUse Agent -> SubagentStop: running until the stop, not after
        env = self.pool(1)
        self.create(1)
        self.create(2)
        self.update(1, status="in_progress", owner="qorvax-tel")
        self.assertEqual(self.pre("qorvax-tel", env=env).stdout, "")
        self.launch("qorvax-tel", env)
        self.update(2, status="in_progress", owner="zyrene-vosk")
        self.assertEqual(decision(self.pre("zyrene-vosk", env=env)), "deny")  # running before the stop
        self.stop("qorvax-tel")
        self.assertEqual(self.pre("zyrene-vosk", env=env).stdout, "")  # not running after the stop

    def test_redispatch_after_finish_counts_running_at_once(self):
        env = self.pool(1)
        self.create(1)
        self.create(2)
        self.start(1, "qorvax-tel", env)
        self.stop("qorvax-tel")  # first run finished
        self.update(2, status="in_progress", owner="zyrene-vosk")
        self.assertEqual(self.pre("qorvax-tel", env=env).stdout, "")  # re-dispatch of #1 is allowed
        st = self.sA_state()["1"]
        self.assertIsInstance(st["finished_at"], float)  # old finish still stored
        self.assertIsInstance(st["dispatch_pending_at"], float)
        # the new pending launch is newer than the old finish: #1 counts as running right after Pre
        self.assertEqual(decision(self.pre("zyrene-vosk", env=env)), "deny")

    def test_window_120_for_pending_newer_than_dispatch_else_stale(self):
        env = self.pool(1)
        self.create(1)
        self.create(2)
        self.start(1, "qorvax-tel", env)
        self.update(2, status="in_progress", owner="zyrene-vosk")
        now = time.time()
        # re-dispatch pending 60 s old, newer than dispatched_at 300 s old: inside the 120 s window
        self.edit(lambda st: st["1"].update(dispatched_at=now - 300, dispatch_pending_at=now - 60))
        self.assertEqual(decision(self.pre("zyrene-vosk", env=env)), "deny")
        # pending 150 s old: past the 120 s window, not running
        self.edit(lambda st: st["1"].update(dispatch_pending_at=now - 150))
        self.assertEqual(self.pre("zyrene-vosk", env=env).stdout, "")
        # pending older than dispatched_at: the window is running_stale_sec (default 3600 s)
        self.edit(lambda st: st["1"].update(dispatched_at=now - 300, dispatch_pending_at=now - 400))
        self.assertEqual(decision(self.pre("zyrene-vosk", env=env)), "deny")

    def test_pool_line_names_deficit_and_free_executors(self):
        env = self.pool(3)
        for i in (1, 2, 3, 4):
            self.create(i)
        self.start(1, "qorvax-tel", env)
        # R=1, N=3, P=3: K = min(3, 1 + 3) - 1 = 2; free are the executors other than qorvax-tel
        text = self.ctx(self.launch("qorvax-tel", env))
        self.assertIn("Pool: 1/3 executors running; 3 ready; dispatch 2 now: #2, #3 "
                      "(free: zyrene-vosk, thal-ummir)", text)
        # completing #1: R=0, K = 3 with the first three executors free
        text = self.ctx(self.complete(1, env))
        self.assertIn("Pool: 0/3 executors running; 3 ready; dispatch 3 now: #2, #3, #4 "
                      "(free: qorvax-tel, zyrene-vosk, thal-ummir)", text)

    def test_pool_line_without_deficit(self):
        env = self.pool(1)
        self.create(1)
        self.create(2)
        self.start(1, "qorvax-tel", env)
        self.assertIn("Pool: 1/1 executors running; 1 ready.", self.ctx(self.launch("qorvax-tel", env)))

    def test_executors_and_pool_size_read_from_config(self):
        cenv = self.write_cfg("pool.json", {"executors": ["alpha-x", "beta-y"], "pool_size": 1})
        self.create(1)
        self.create(2)
        self.create(3)
        self.start(1, "alpha-x", cenv)
        self.update(2, status="in_progress", owner="beta-y")
        r = self.pre("beta-y", env=cenv)
        self.assertEqual(decision(r), "deny")
        self.assertIn("Executor pool full: 1 of 1 running (#1).", self.reason(r))
        # qorvax-tel is not in this config's executor list: a plain subagent, not capped
        self.update(3, status="in_progress", owner="qorvax-tel")
        self.assertEqual(self.pre("qorvax-tel", env=cenv).stdout, "")

    def test_malformed_settings_fall_back(self):
        bad = self.write_cfg("bad.json", {"executors": "nope", "pool_size": "lots", "running_stale_sec": -5})
        self.create(1)
        self.create(2)
        self.start(1, "qorvax-tel", bad)
        # defaults: pool 8, executors include qorvax-tel, stale 3600; K = min(8, 2) - 1 = 1
        self.assertIn("Pool: 1/8 executors running; 1 ready; dispatch 1 now: #2 (free: zyrene-vosk)",
                      self.ctx(self.launch("qorvax-tel", bad)))
        # a malformed env value is skipped and the config value applies
        mid = self.write_cfg("mid.json", {"pool_size": 3})
        self.assertIn("Pool: 1/3 executors running; 1 ready; dispatch 1 now: #2 (free: zyrene-vosk)",
                      self.ctx(self.launch("qorvax-tel", dict(mid, TASK_GATE_POOL_SIZE="x"))))

    def test_require_background_false_in_config_allows_foreground(self):
        cfg = self.write_cfg("nobg.json", {"require_background": False})
        self.create(1)
        self.create(2)
        self.update(1, status="in_progress", owner="qorvax-tel")
        self.assertEqual(self.pre("qorvax-tel", env=cfg, ti_extra={"run_in_background": False}).stdout, "")

    def test_require_background_true_in_config_refuses_foreground(self):
        cfg = self.write_cfg("bg.json", {"require_background": True})
        self.create(1)
        self.create(2)
        self.update(1, status="in_progress", owner="qorvax-tel")
        r = self.pre("qorvax-tel", env=cfg, ti_extra={"run_in_background": False})
        self.assertEqual(decision(r), "deny")
        self.assertIn("Dispatch executors with run_in_background: true while other tasks are ready: #2.",
                      self.reason(r))

    def test_env_require_background_zero_allows_foreground(self):
        env = {"TASK_GATE_REQUIRE_BACKGROUND": "0"}
        self.create(1)
        self.create(2)
        self.update(1, status="in_progress", owner="qorvax-tel")
        self.assertEqual(self.pre("qorvax-tel", env=env, ti_extra={"run_in_background": False}).stdout, "")
        self.assertEqual(self.pre("qorvax-tel", env=env, omit_bg=True).stdout, "")

    def test_local_dispatch_allows_foreground_while_ready(self):
        env = dict(self.write_cfg("lbg.json", {"require_background": True}), LOCAL_DISPATCH="1")
        self.create(1)
        self.create(2)
        self.update(1, status="in_progress", owner="qorvax-tel")
        self.assertEqual(self.pre("qorvax-tel", env=env, ti_extra={"run_in_background": False}).stdout, "")
        self.assertEqual(self.pre("qorvax-tel", env=env, omit_bg=True).stdout, "")

    def test_local_dispatch_still_enforces_pool_cap(self):
        env = dict(self.pool(1), LOCAL_DISPATCH="1")
        self.create(1)
        self.create(2)
        self.start(1, "qorvax-tel", env)
        self.update(2, status="in_progress", owner="zyrene-vosk")
        r = self.pre("zyrene-vosk", env=env, ti_extra={"run_in_background": False})
        self.assertEqual(decision(r), "deny")
        self.assertIn("Executor pool full: 1 of 1 running (#1).", self.reason(r))


class DiagnoserGate(unittest.TestCase):
    """Diagnoser gate (Agent from the main session, subagent_type diagnoser-*): an Opus diagnosis of a task whose
    Haiku round failed. Proven by a Haiku round, metadata.escalate_reason and escalate_class on the target, and a
    diagnose task whose metadata.diagnoses names the target. Caps and one-at-a-time run under the lock.
    Borrows T's harness helpers without inheriting its test methods (no duplicate runs)."""
    setUp = T.setUp
    tearDown = T.tearDown
    _run = T._run
    pre = T.pre
    post = T.post
    create = T.create
    update = T.update
    clog_lines = T.clog_lines
    log_text = T.log_text
    sA_state = T.sA_state

    def write_cfg(self, obj, name="harness.json"):
        path = os.path.join(self.root, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        return {"CLAUDE_HARNESS_CONFIG": path}

    def sf(self):
        return self.write_cfg({"model_gate": {"sonnet_fallback": True}}, name="sonnet.json")

    def reason(self, r):
        return json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]

    def target(self, tid=1, sid="sA", haiku=True, reason="check failed", cls="scale", ex="qorvax-tel"):
        """Task tid, with a Haiku executor round (when haiku) and the given escalation metadata."""
        self.create(tid, sid=sid)
        if haiku:
            r = self.pre(ex, sid=sid, ti_extra={"description": "task #%d" % tid, "run_in_background": True})
            self.assertEqual(decision(r), "allow", r.stdout)
        md = {}
        if reason is not None:
            md["escalate_reason"] = reason
        if cls is not None:
            md["escalate_class"] = cls
        if md:
            self.update(tid, sid=sid, metadata=md)

    def escalate(self, tid=1, reason="again", cls="debug", sid="sA"):
        self.update(tid, sid=sid, metadata={"escalate_reason": reason, "escalate_class": cls})

    def make_diag(self, tid=2, target=1, sid="sA", md=None):
        self.create(tid, sid=sid)
        self.update(tid, sid=sid, metadata=({"diagnoses": str(target)} if md is None else md))

    def dispatch_diag(self, sub="diagnoser-1", tid=2, model="opus", effort="high", sid="sA", env=None):
        ti = {"description": "task #%d" % tid, "run_in_background": True}
        if model is not None:
            ti["model"] = model
        if effort is not None:
            ti["effort"] = effort
        return self.pre(sub, sid=sid, ti_extra=ti, env=env)

    def complete(self, tid, sid="sA"):
        self.update(tid, sid=sid, status="completed")

    def test_dg01_allowed_with_proof_clears_escalation_and_logs(self):
        self.target(1)
        self.make_diag(2, 1)
        r = self.dispatch_diag()
        self.assertEqual(decision(r), "allow", r.stdout)
        st = self.sA_state()
        self.assertEqual(st["2"]["owner"], "diagnoser-1")
        self.assertTrue(st["2"]["dispatched"])
        self.assertFalse(st["1"]["metadata"].get("escalate_reason"))
        self.assertFalse(st["1"]["metadata"].get("escalate_class"))
        self.assertEqual(st["1"]["metadata"]["diagnosis_count"], 1)
        self.assertEqual(st["_diagnoses"], 1)
        lines = [x for x in self.clog_lines() if x.get("kind") == "diagnosis"]
        self.assertEqual(len(lines), 1)
        self.assertEqual((lines[0]["task_id"], lines[0]["target"], lines[0]["class"]), ("2", "1", "scale"))

    def test_dg02_denied_without_haiku_round(self):
        self.target(1, haiku=False)
        self.make_diag(2, 1)
        r = self.dispatch_diag()
        self.assertEqual(decision(r), "deny")
        self.assertIn("no Haiku round", self.reason(r))

    def test_dg03_denied_without_escalate_reason(self):
        self.target(1, reason=None)
        self.make_diag(2, 1)
        r = self.dispatch_diag()
        self.assertEqual(decision(r), "deny")
        self.assertIn("no escalate_reason", self.reason(r))

    def test_dg04_denied_with_bad_escalate_class(self):
        self.target(1, cls="bogus")
        self.make_diag(2, 1)
        r = self.dispatch_diag()
        self.assertEqual(decision(r), "deny")
        self.assertIn("no valid escalate_class", self.reason(r))

    def test_dg05_denied_without_diagnoses_metadata(self):
        self.target(1)
        self.create(2)
        r = self.dispatch_diag()
        self.assertEqual(decision(r), "deny")
        self.assertIn("metadata.diagnoses", self.reason(r))

    def test_dg06_denied_sonnet_model(self):
        self.target(1)
        self.make_diag(2, 1)
        r = self.dispatch_diag(model="sonnet")
        self.assertEqual(decision(r), "deny")
        self.assertIn("Diagnoser agents run on Opus", self.reason(r))

    def test_dg07_denied_effort_low(self):
        self.target(1)
        self.make_diag(2, 1)
        r = self.dispatch_diag(effort="low")
        self.assertEqual(decision(r), "deny")
        self.assertIn("Diagnoser agents run on Opus", self.reason(r))

    def test_dg08_model_omitted_at_high_effort_allowed(self):
        self.target(1)
        self.make_diag(2, 1)
        self.assertEqual(decision(self.dispatch_diag(model=None)), "allow")

    def test_dg09_per_task_cap_is_two(self):
        self.target(1)
        self.make_diag(2, 1)
        self.assertEqual(decision(self.dispatch_diag(tid=2)), "allow")
        self.complete(2)
        self.escalate(1)
        self.make_diag(3, 1)
        self.assertEqual(decision(self.dispatch_diag(tid=3)), "allow")
        self.complete(3)
        self.escalate(1)
        self.make_diag(4, 1)
        r = self.dispatch_diag(tid=4)
        self.assertEqual(decision(r), "deny")
        self.assertIn("already had 2 diagnoses (cap 2)", self.reason(r))

    def test_dg10_per_session_cap_from_config(self):
        cfg = self.write_cfg({"diagnose_gate": {"max_per_session": 1}})
        self.target(1)
        self.make_diag(2, 1)
        self.assertEqual(decision(self.dispatch_diag(tid=2, env=cfg)), "allow")
        self.complete(2)
        self.escalate(1)
        self.make_diag(3, 1)
        r = self.dispatch_diag(tid=3, env=cfg)
        self.assertEqual(decision(r), "deny")
        self.assertIn("cap 1", self.reason(r))

    def test_dg11_second_diagnoser_denied_while_one_runs(self):
        self.target(1)
        self.target(3, ex="zyrene-vosk")
        self.make_diag(2, 1)
        self.assertEqual(decision(self.dispatch_diag(sub="diagnoser-1", tid=2)), "allow")
        self.make_diag(4, 3)
        r = self.dispatch_diag(sub="diagnoser-2", tid=4)
        self.assertEqual(decision(r), "deny")
        self.assertIn("Another diagnoser is still running", self.reason(r))

    def test_dg12_ungated_mode_denies_diagnoser(self):
        r = self.pre("diagnoser-1", ti_extra={"model": "opus", "effort": "high"},
                     env={"CLAUDE_REQUIRE_TASK": "0"})
        self.assertEqual(decision(r), "deny")
        self.assertIn("CLAUDE_REQUIRE_TASK=0", self.reason(r))

    def test_dg13_sendmessage_to_diagnoser_denied(self):
        r = self.pre("X", tool="SendMessage", ti_extra={"to": "diagnoser-1", "message": "more"})
        self.assertEqual(decision(r), "deny")
        self.assertIn("one-shot", self.reason(r))

    def test_dg14_sendmessage_to_executor_allowed(self):
        r = self.pre("X", tool="SendMessage", ti_extra={"to": "qorvax-tel", "message": "hi"})
        self.assertEqual(decision(r), "allow")

    def test_dg15_sonnet_denied_by_default(self):
        self.target(1)
        r = self.pre("qorvax-tel", ti_extra={"description": "task #1", "model": "sonnet", "effort": "medium",
                                             "run_in_background": True})
        self.assertEqual(decision(r), "deny")
        self.assertIn("Haiku-only", self.reason(r))

    def test_dg16_sonnet_allowed_with_fallback_and_proof(self):
        self.target(1)
        self.escalate(1)
        r = self.pre("qorvax-tel", env=self.sf(), ti_extra={"description": "task #1", "model": "sonnet",
                                                             "effort": "medium", "run_in_background": True})
        self.assertEqual(decision(r), "allow", r.stdout)

    def test_dg17_opus_denied_with_fallback(self):
        self.target(1)
        r = self.pre("qorvax-tel", env=self.sf(), ti_extra={"description": "task #1", "model": "opus",
                                                             "effort": "medium", "run_in_background": True})
        self.assertEqual(decision(r), "deny")


    def test_dg18_hook_owned_cap_ignores_metadata_reset(self):
        self.target(1)
        self.make_diag(2, 1)
        self.assertEqual(decision(self.dispatch_diag(tid=2)), "allow")
        self.complete(2)
        self.escalate(1)
        self.make_diag(3, 1)
        self.assertEqual(decision(self.dispatch_diag(tid=3)), "allow")
        self.complete(3)
        self.assertEqual(self.sA_state()["1"]["diagnoses_done"], 2)
        self.update(1, metadata={"diagnosis_count": None, "escalate_reason": "again", "escalate_class": "debug"})
        self.make_diag(4, 1)
        r = self.dispatch_diag(tid=4)
        self.assertEqual(decision(r), "deny")
        self.assertIn("already had 2 diagnoses (cap 2)", self.reason(r))

    def test_dg19_same_task_second_dispatch_denied_while_running(self):
        self.target(1)
        self.make_diag(2, 1)
        self.assertEqual(decision(self.dispatch_diag(tid=2)), "allow")
        self.escalate(1)
        r = self.dispatch_diag(tid=2)
        self.assertEqual(decision(r), "deny")
        self.assertIn("already running under a diagnoser", self.reason(r))

    def test_dg20_completed_target_denied(self):
        self.target(1)
        self.complete(1)
        self.make_diag(2, 1)
        r = self.dispatch_diag()
        self.assertEqual(decision(r), "deny")
        self.assertIn("already completed", self.reason(r))

    def test_dg21_underscore_task_update_leaves_session_counter(self):
        self.target(1)
        self.make_diag(2, 1)
        self.assertEqual(decision(self.dispatch_diag()), "allow")
        self.update("_diagnoses", metadata={"x": "y"})
        self.update("_diagnoses", status="completed")
        self.assertEqual(self.sA_state()["_diagnoses"], 1)


if __name__ == "__main__":
    unittest.main()
