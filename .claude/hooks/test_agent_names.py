import json, os, subprocess, sys, tempfile, unittest
from concurrent.futures import ThreadPoolExecutor
HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent-names.py")
sys.path.insert(0, os.path.dirname(HOOK))
_mod = __import__("importlib").import_module("agent-names")
NAMES, POOL, EXECUTORS = _mod.NAMES, _mod.POOL, _mod.EXECUTORS
ROSTER = set(EXECUTORS.values())
SID = "11111111-2222-3333-4444-555555555555"


class T(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp()

    def run_hook(self, payload, env=None, **kw):
        e = dict(os.environ); e.pop("CLAUDE_AGENT_NAMES", None)
        for k in ("CLAUDE_LOCAL", "LOCAL_DISPATCH", "CLAUDE_ROUTER", "CLAUDE_LOCAL_EFFORT"):
            e.pop(k, None)  # deterministic: local-mode flags are set explicitly per test below
        e["CLAUDE_AGENT_NAMES_STATE"] = self.state
        e.update(env or {}); e.update(kw)
        r = subprocess.run([HOOK], input=payload if isinstance(payload, str) else json.dumps(payload),
                           capture_output=True, text=True, env=e)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)["hookSpecificOutput"] if r.stdout.strip() else None

    def p(self, tool="Agent", sid=SID, **ti):
        ti.setdefault("description", "Install hook"); ti.setdefault("prompt", "Do the thing.")
        return {"tool_name": tool, "tool_input": ti, "session_id": sid}

    def test_rewrites_description_and_prompt(self):
        o = self.run_hook(self.p(subagent_type="general-purpose", isolation="worktree"))
        u = o["updatedInput"]
        name, rest = u["description"].split(" · ", 1)
        self.assertIn(name, POOL); self.assertEqual(rest, "Install hook")
        self.assertTrue(u["prompt"].startswith("You are %s," % name))
        self.assertTrue(u["prompt"].endswith("Do the thing."))
        self.assertEqual(u["subagent_type"], "general-purpose"); self.assertEqual(u["isolation"], "worktree")
        self.assertEqual(o["permissionDecision"], "allow")

    def test_round_robin_distinct(self):
        got = [self.run_hook(self.p())["updatedInput"]["description"].split(" · ")[0] for _ in range(len(POOL))]
        self.assertEqual(sorted(got), sorted(POOL))
        again = self.run_hook(self.p())["updatedInput"]["description"].split(" · ")[0]
        self.assertEqual(again, got[0])

    def test_parallel_distinct(self):
        with ThreadPoolExecutor(8) as ex:
            got = list(ex.map(lambda _: self.run_hook(self.p())["updatedInput"]["description"], range(8)))
        self.assertEqual(len(set(got)), 8)

    def test_sessions_independent(self):
        a = self.run_hook(self.p(sid="aaaa"))["updatedInput"]["description"]
        self.run_hook(self.p(sid="bbbb"))
        self.assertNotEqual(a, self.run_hook(self.p(sid="aaaa"))["updatedInput"]["description"])
        self.assertEqual(sorted(os.listdir(self.state)), ["aaaa.count", "bbbb.count"])

    def test_already_named_skipped(self):
        self.assertIsNone(self.run_hook(self.p(description=NAMES[3] + " · x")))

    def test_other_tools_and_failopen(self):
        for t in ["Read", "Bash", "TaskCreate"]:
            self.assertIsNone(self.run_hook(self.p(tool=t)))
        self.assertIsNone(self.run_hook("not json"))
        self.assertIsNone(self.run_hook(""))
        self.assertIsNone(self.run_hook(self.p(), {"CLAUDE_AGENT_NAMES": "0"}))

    def test_bad_session_id_safe(self):
        self.assertIsNotNone(self.run_hook(self.p(sid="../../etc")))
        self.assertIsNotNone(self.run_hook(self.p(sid=None)))
        self.assertEqual(os.listdir(self.state), ["nosession.count"])

    def test_executor_gets_fixed_name(self):
        o = self.run_hook(self.p(subagent_type="qorvax-tel", description="Fix lint"))
        u = o["updatedInput"]
        self.assertEqual(u["description"], "Qorvax Tel · Fix lint")
        self.assertTrue(u["prompt"].startswith("You are Qorvax Tel,"))
        self.assertIn('"Qorvax here:"', u["prompt"])
        self.assertTrue(u["prompt"].endswith("Do the thing."))
        self.assertEqual(u["subagent_type"], "qorvax-tel")

    def test_executor_does_not_consume_counter(self):
        fresh = tempfile.mkdtemp()
        baseline = self.run_hook(self.p(), {"CLAUDE_AGENT_NAMES_STATE": fresh})["updatedInput"]["description"]
        for _ in range(3):
            self.run_hook(self.p(subagent_type="zyrene-vosk"))
        after = self.run_hook(self.p())["updatedInput"]["description"]
        self.assertEqual(after, baseline)

    def test_reviewer_gets_fixed_name(self):
        o = self.run_hook(self.p(subagent_type="pr-reviewer-2", description="Review PR 40",
                                 model="opus", effort="high"))
        u = o["updatedInput"]
        self.assertEqual(u["description"], "PR Reviewer 2 · Review PR 40")
        self.assertTrue(u["prompt"].startswith("You are PR Reviewer 2,"))
        self.assertIn('"PR Reviewer 2 here:"', u["prompt"])
        self.assertTrue(u["prompt"].endswith("Do the thing."))
        self.assertEqual(u["subagent_type"], "pr-reviewer-2")
        self.assertEqual(u["model"], "opus"); self.assertEqual(u["effort"], "high")

    def test_reviewer_does_not_consume_counter(self):
        fresh = tempfile.mkdtemp()
        baseline = self.run_hook(self.p(), {"CLAUDE_AGENT_NAMES_STATE": fresh})["updatedInput"]["description"]
        for n in (1, 2, 3):
            self.run_hook(self.p(subagent_type="pr-reviewer-%d" % n))
        after = self.run_hook(self.p())["updatedInput"]["description"]
        self.assertEqual(after, baseline)

    def test_diagnoser_gets_fixed_name(self):
        self.assertNotIn("Diagnoser 1", POOL)
        u = self.run_hook(self.p(subagent_type="diagnoser-1", description="Diagnose build"))["updatedInput"]
        self.assertEqual(u["description"], "Diagnoser 1 · Diagnose build")
        self.assertTrue(u["prompt"].startswith("You are Diagnoser 1,"))
        self.assertIn('"Diagnoser 1 here:"', u["prompt"])
        self.assertEqual(u["subagent_type"], "diagnoser-1")

    def test_diagnoser_does_not_consume_counter(self):
        fresh = tempfile.mkdtemp()
        baseline = self.run_hook(self.p(), {"CLAUDE_AGENT_NAMES_STATE": fresh})["updatedInput"]["description"]
        for _ in range(3):
            self.run_hook(self.p(subagent_type="diagnoser-1"))
        after = self.run_hook(self.p())["updatedInput"]["description"]
        self.assertEqual(after, baseline)

    def test_general_never_gets_reviewer_name(self):
        got = [self.run_hook(self.p(subagent_type="general-purpose"))["updatedInput"]["description"].split(" · ")[0]
               for _ in range(25)]
        self.assertEqual(set(got) & set(_mod.REVIEWERS.values()), set())

    def test_general_never_gets_roster_name(self):
        got = [self.run_hook(self.p(subagent_type="general-purpose"))["updatedInput"]["description"].split(" · ")[0]
               for _ in range(25)]
        self.assertEqual(set(got) & ROSTER, set())
        self.assertTrue(set(got) <= set(POOL))

    # local-mode effort override: reviewer -> high, executor -> medium, only where that pool runs local
    def test_reviewer_high_in_local_mode(self):
        u = self.run_hook(self.p(subagent_type="pr-reviewer-1"), CLAUDE_LOCAL="1")["updatedInput"]
        self.assertEqual(u["effort"], "high")
        self.assertEqual(u["description"], "PR Reviewer 1 · Install hook")

    def test_executor_medium_in_local_mode(self):
        u = self.run_hook(self.p(subagent_type="qorvax-tel"), CLAUDE_LOCAL="1")["updatedInput"]
        self.assertEqual(u["effort"], "medium")
        self.assertEqual(u["description"], "Qorvax Tel · Install hook")

    def test_local_dispatch_all_routes_both_pools_local(self):
        self.assertEqual(self.run_hook(self.p(subagent_type="pr-reviewer-2"), LOCAL_DISPATCH="all")["updatedInput"]["effort"], "high")
        self.assertEqual(self.run_hook(self.p(subagent_type="zyrene-vosk"), LOCAL_DISPATCH="all")["updatedInput"]["effort"], "medium")

    def test_subagents_mode_executor_local_reviewer_left(self):
        # LOCAL_DISPATCH=subagents: only the Haiku executor runs local; the Opus reviewer stays on Anthropic
        self.assertNotIn("effort", self.run_hook(self.p(subagent_type="pr-reviewer-3"), LOCAL_DISPATCH="subagents")["updatedInput"])
        self.assertEqual(self.run_hook(self.p(subagent_type="thal-ummir"), LOCAL_DISPATCH="subagents")["updatedInput"]["effort"], "medium")

    def test_off_local_reviewer_left_alone(self):
        self.assertNotIn("effort", self.run_hook(self.p(subagent_type="pr-reviewer-1"))["updatedInput"])

    def test_off_local_preserves_input_effort(self):
        u = self.run_hook(self.p(subagent_type="pr-reviewer-1", effort="high"))["updatedInput"]
        self.assertEqual(u["effort"], "high")

    def test_hybrid_executor_explicit_effort_kept(self):
        # LOCAL_DISPATCH=1: the executor runs local, but an explicit effort wins over the pin
        u = self.run_hook(self.p(subagent_type="thal-ummir", effort="high"), LOCAL_DISPATCH="1")["updatedInput"]
        self.assertEqual(u["effort"], "high")

    def test_local_reviewer_explicit_effort_kept(self):
        u = self.run_hook(self.p(subagent_type="pr-reviewer-1", effort="low"), CLAUDE_LOCAL="1")["updatedInput"]
        self.assertEqual(u["effort"], "low")

    def test_local_executor_no_effort_gets_medium(self):
        u = self.run_hook(self.p(subagent_type="thal-ummir"), LOCAL_DISPATCH="1")["updatedInput"]
        self.assertEqual(u["effort"], "medium")

    def test_empty_effort_gets_pinned(self):
        u = self.run_hook(self.p(subagent_type="thal-ummir", effort=""), LOCAL_DISPATCH="1")["updatedInput"]
        self.assertEqual(u["effort"], "medium")

    def test_local_effort_kill_switch(self):
        u = self.run_hook(self.p(subagent_type="pr-reviewer-1"), CLAUDE_LOCAL="1", CLAUDE_LOCAL_EFFORT="0")["updatedInput"]
        self.assertNotIn("effort", u)

    def test_general_purpose_effort_untouched_in_local(self):
        self.assertNotIn("effort", self.run_hook(self.p(subagent_type="general-purpose"), CLAUDE_LOCAL="1")["updatedInput"])


if __name__ == "__main__":
    unittest.main()
