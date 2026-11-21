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

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _run(self, raw, env=None):
        e = dict(os.environ)
        e.pop("CLAUDE_MAIN_WRITE", None)
        e.pop("HARNESS_TEST_ROLE", None)
        e["CLAUDE_HARNESS_CONFIG"] = os.path.join(self.root, "no-harness.json")  # never created: isolate from live config
        e["TASK_GATE_STATE_DIR"] = self.state
        e["TASK_GATE_LOG"] = self.log
        e["TASK_GATE_COMPLETION_LOG"] = self.clog
        e.update(env or {})
        r = subprocess.run([sys.executable, "-I", HOOK], input=raw, capture_output=True, text=True, env=e)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def pre(self, subagent="general-purpose", sid="sA", tool="Agent", agent_id=None, env=None, omit_subagent=False, ti_extra=None, tuid="toolu_x"):
        ti = {"description": "d", "prompt": "p", "run_in_background": False}
        if not omit_subagent:
            ti["subagent_type"] = subagent
        ti.update(ti_extra or {})
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
        r = self.pre("general-purpose")
        out = json.loads(r.stdout)
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "PreToolUse")
        self.assertEqual(hso["permissionDecision"], "deny")
        self.assertIn('No in_progress task owned by "general-purpose"', hso["permissionDecisionReason"])
        self.assertIn("DENY", self.log_text())
        self.assertIn("subagent=general-purpose", self.log_text())

    # 2
    def test_02_pending_task_denied(self):
        self.create(1)
        self.assertEqual(decision(self.pre("general-purpose")), "deny")

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
        self.update(1, status="in_progress", owner="general-purpose")
        self.assertEqual(decision(self.pre(omit_subagent=True)), "allow")

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
    def test_09_subagent_payload_unrestricted(self):
        self.assertEqual(decision(self.pre("general-purpose", agent_id="agent-123")), "allow")

    # 10
    def _exempt_cfg(self):
        path = os.path.join(self.root, "exempt.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"exempt_env": {"HARNESS_TEST_ROLE": ["bot", "bot-*"]}}, f)
        return {"CLAUDE_HARNESS_CONFIG": path}

    def test_10_exempt_env_roles_exempt(self):
        cfg = self._exempt_cfg()
        for role in ["bot", "bot-minion"]:
            self.assertEqual(decision(self.pre("general-purpose", env=dict(cfg, HARNESS_TEST_ROLE=role))), "allow", role)

    def test_10b_other_roles_denied(self):
        cfg = self._exempt_cfg()
        for role in ["Bot-minion", "xbot-minion", "bots", ""]:
            self.assertEqual(decision(self.pre("general-purpose", env=dict(cfg, HARNESS_TEST_ROLE=role))), "deny", repr(role))
        self.assertEqual(decision(self.pre("general-purpose", env=cfg)), "deny", "unset")

    def test_10c_no_config_no_exemption(self):
        self.assertEqual(decision(self.pre("general-purpose", env={"HARNESS_TEST_ROLE": "bot"})), "deny")

    # 11
    def test_11_main_write_exempt(self):
        self.assertEqual(decision(self.pre("general-purpose", env={"CLAUDE_MAIN_WRITE": "1"})), "allow")

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
        self.assertEqual(decision(self.pre("general-purpose", tool="Task")), "deny")

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

    def post_agent(self, subagent="general-purpose", sid="sA"):
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
        self.assertEqual(self.pre("general-purpose", env={"CLAUDE_REQUIRE_TASK": "0"}).stdout, "")

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
                                    "owner", "metadata", "agent_id", "agent_type"})
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
        for model in ["sonnet", "haiku", "claude-sonnet-5", "opus-4", "claude-haiku-5-5"]:
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

    def test_35_reviewer_payload_from_subagent_not_gated(self):
        self.reviewer_setup()
        r = self.pre("pr-reviewer-1", agent_id="agent-7", ti_extra={"model": "haiku", "effort": "low"})
        self.assertEqual(r.stdout, "")

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


if __name__ == "__main__":
    unittest.main()
