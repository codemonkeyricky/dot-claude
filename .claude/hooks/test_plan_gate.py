import hashlib, json, os, subprocess, sys, time

import pytest

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plan-gate.py")
CLEAR = ("CLAUDE_REQUIRE_TASK", "CLAUDE_MAIN_WRITE", "CLAUDE_PLAN_GATE", "HARNESS_ROLE")
SID = "sA"


def files_md(obj):
    """A ```files block that matches obj's writes: one flat leaf per written path, tagged with its task ids."""
    tasks = obj.get("tasks", []) if isinstance(obj, dict) else obj
    ids, raw = {}, {}
    for t in tasks:
        if isinstance(t, dict) and isinstance(t.get("id"), str):
            for w in t.get("writes") or []:
                if isinstance(w, str):
                    key = w.replace("\\", "/").lower()  # same normalisation as the hook's norm_path
                    while key.startswith("./"):
                        key = key[2:]
                    raw.setdefault(key, w)
                    ids.setdefault(key, []).append(t["id"])
    lines = ["%s  [%s]" % (raw[k], ", ".join(sorted(set(v)))) for k, v in ids.items()]
    return "```files\n" + "".join(line + "\n" for line in lines) + "```\n"


def plan_md(obj, files=True):
    text = "# Plan\n\nSome prose.\n\n```tasks\n" + json.dumps(obj) + "\n```\n"
    return text + ("\n" + files_md(obj) if files else "")


def task(tid, writes=(), needs=None, dispatch=None, subject=None):
    t = {"id": tid, "subject": subject or ("task " + tid), "writes": list(writes)}
    if needs is not None:
        t["needs"] = needs
    if dispatch is not None:
        t["dispatch"] = dispatch
    return t


def wide_plan():
    return {"tasks": [
        task("A", ["src/a.py"]),
        task("B", ["src/b.py"], needs={"A": "B uses A"}),
        task("C", ["docs/c.md"]),
    ]}


def chain_plan(serial=None):
    doc = {"tasks": [task("A", ["a"]), task("B", ["b"], needs={"A": "r"}),
                     task("C", ["c"], needs={"B": "r"}), task("D", ["d"], needs={"C": "r"})]}
    if serial is not None:
        doc["serial_reason"] = serial
    return doc


class Hook:
    def __init__(self, root):
        self.root = str(root)
        self.state = os.path.join(self.root, "state")
        self.plans = os.path.join(self.root, "plans")
        self.log = os.path.join(self.root, "plan-gate.log")
        self.harness = os.path.join(self.root, "harness.json")
        os.makedirs(self.plans, exist_ok=True)

    def run(self, payload=None, raw=None, env=None):
        e = {k: v for k, v in os.environ.items() if k not in CLEAR}
        e.update({"TASK_GATE_STATE_DIR": self.state, "PLAN_GATE_PLANS_DIR": self.plans,
                  "PLAN_GATE_LOG": self.log, "CLAUDE_HARNESS_CONFIG": self.harness})
        e.update(env or {})
        data = raw if raw is not None else json.dumps(payload)
        r = subprocess.run([sys.executable, "-I", HOOK], input=data.encode("utf-8"),
                           capture_output=True, env=e)
        assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
        return r.stdout.decode("utf-8")

    def exit_plan(self, plan=None, phase="PreToolUse", sid=SID, env=None, transcript=None):
        ti = {} if plan is None else {"plan": plan}
        p = {"session_id": sid, "hook_event_name": phase, "tool_name": "ExitPlanMode", "tool_input": ti}
        if transcript is not None:
            p["transcript_path"] = transcript
        return self.run(p, env=env)

    def tool(self, name, ti, sid=SID, env=None, agent_id=None):
        p = {"session_id": sid, "hook_event_name": "PreToolUse", "tool_name": name, "tool_input": ti,
             "tool_use_id": "toolu_x"}
        if agent_id:
            p["agent_id"] = agent_id
        return self.run(p, env=env)

    def write_state(self, entries, sid=SID):
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, sid + ".json"), "w", encoding="utf-8") as f:
            json.dump(entries, f)

    def plan_json(self, sid=SID):
        with open(os.path.join(self.state, sid + ".plan.json"), encoding="utf-8") as f:
            return json.load(f)

    def make_plan_json(self, plan_obj, sid=SID):
        """Run the real PostToolUse so plan.json is written the way the hook writes it."""
        out = self.exit_plan(plan_md(plan_obj), phase="PostToolUse", sid=sid)
        return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out.strip() else ""


def reason(out):
    return json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"] if out.strip() else None


def decision(out):
    return json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out.strip() else "allow"


@pytest.fixture
def hook(tmp_path):
    return Hook(tmp_path)


def current_key(hook):
    try:
        return hook.plan_json()["key"]
    except (OSError, ValueError, KeyError):
        return "nokey"


def set_plan_field(hook, **fields):
    p = hook.plan_json()
    p.update(fields)
    with open(os.path.join(hook.state, SID + ".plan.json"), "w", encoding="utf-8") as f:
        json.dump(p, f)


def seeded(hook, extra=None, plan_ids=("A", "B", "C"), key=None):
    """Task state with one entry per plan id, B blockedBy A (matches wide_plan). Entries carry the plan key
    of the saved plan.json unless key is given."""
    key = key or current_key(hook)
    st = {"1": {"status": "pending", "owner": None, "subject": "a", "blockedBy": [],
                "metadata": {"plan_id": "A", "plan_key": key}},
          "2": {"status": "pending", "owner": None, "subject": "b", "blockedBy": ["1"],
                "metadata": {"plan_id": "B", "plan_key": key}},
          "3": {"status": "pending", "owner": None, "subject": "c", "blockedBy": [],
                "metadata": {"plan_id": "C", "plan_key": key}}}
    st = {k: v for k, v in st.items() if v["metadata"]["plan_id"] in plan_ids}
    st.update(extra or {})
    hook.write_state(st)
    return st


# ---- ExitPlanMode lint: denies --------------------------------------------

def test_01_no_tasks_block_denied(hook):
    out = hook.exit_plan("# Plan\n\nJust prose, no block.\n")
    assert decision(out) == "deny"
    assert "no ```tasks block" in reason(out)
    assert "```tasks" in reason(out)  # example block shown


def test_02_invalid_json_denied_with_parser_message(hook):
    out = hook.exit_plan("```tasks\n{\"tasks\": [}\n```\n")
    assert decision(out) == "deny"
    assert "JSON invalid" in reason(out)
    assert "Expecting" in reason(out) or "property" in reason(out) or "value" in reason(out)


def test_03_missing_id_denied(hook):
    out = hook.exit_plan(plan_md([{"subject": "x", "writes": []}]))
    assert decision(out) == "deny"
    assert "id must be a non-empty string" in reason(out)


def test_04_duplicate_id_denied(hook):
    out = hook.exit_plan(plan_md([task("A", ["a"]), task("A", ["b"])]))
    assert decision(out) == "deny"
    assert 'duplicate id "A"' in reason(out)


def test_05_needs_unknown_id_denied(hook):
    out = hook.exit_plan(plan_md([task("A", ["a"], needs={"Z": "why"})]))
    assert decision(out) == "deny"
    assert 'needs unknown id "Z"' in reason(out)


def test_06_needs_itself_denied(hook):
    out = hook.exit_plan(plan_md([task("A", ["a"], needs={"A": "why"})]))
    assert decision(out) == "deny"
    assert "needs itself" in reason(out)


def test_07_empty_needs_reason_denied(hook):
    out = hook.exit_plan(plan_md([task("A", ["a"]), task("B", ["b"], needs={"A": "  "})]))
    assert decision(out) == "deny"
    assert "non-empty reason" in reason(out)


def test_08_needs_cycle_named(hook):
    out = hook.exit_plan(plan_md([task("A", ["a"], needs={"B": "r"}), task("B", ["b"], needs={"A": "r"})]))
    assert decision(out) == "deny"
    assert "dependency cycle" in reason(out)
    assert "A -> B -> A" in reason(out)


def test_09_split_dispatchable_task_denied(hook):
    out = hook.exit_plan(plan_md([task("X", ["f%d" % i for i in range(9)])]))
    assert decision(out) == "deny"
    assert "split #X" in reason(out)


def test_10_pure_chain_of_four_denied(hook):
    out = hook.exit_plan(plan_md(chain_plan()))
    assert decision(out) == "deny"
    assert "pure chain of 4" in reason(out)
    assert "serial_reason" in reason(out)


def test_11_no_plan_text_denied(hook):
    out = hook.exit_plan(None)
    assert decision(out) == "deny"
    assert "no plan text" in reason(out)


def test_12_two_tasks_blocks_denied(hook):
    text = plan_md(wide_plan()) + plan_md(wide_plan())
    out = hook.exit_plan(text)
    assert decision(out) == "deny"
    assert "2 ```tasks blocks" in reason(out)


def test_13_plan_max_writes_from_harness_config(hook):
    with open(hook.harness, "w", encoding="utf-8") as f:
        json.dump({"plan_max_writes": 2}, f)
    out = hook.exit_plan(plan_md([task("X", ["a", "b", "c"])]))
    assert decision(out) == "deny"
    assert "split #X" in reason(out) and "limit is 2" in reason(out)


def test_14_newest_plan_file_used_when_no_plan_text(hook):
    good = os.path.join(hook.plans, "good.md")
    bad = os.path.join(hook.plans, "bad.md")
    with open(good, "w", encoding="utf-8") as f:
        f.write(plan_md(wide_plan()))
    with open(bad, "w", encoding="utf-8") as f:
        f.write("no block here")
    os.utime(good, (1000, 1000))
    os.utime(bad, (2000, 2000))  # bad is newer
    assert decision(hook.exit_plan(None)) == "deny"
    os.utime(bad, (500, 500))  # good is now newer
    assert hook.exit_plan(None) == ""


# ---- ExitPlanMode lint: allows and exemptions -----------------------------

def test_15_valid_wide_plan_allowed(hook):
    assert hook.exit_plan(plan_md(wide_plan())) == ""


def test_16_chain_with_serial_reason_allowed(hook):
    assert hook.exit_plan(plan_md(chain_plan(serial="each step rewrites the same generated file"))) == ""


def test_39_dispatch_false_fanin_does_not_inflate_chain(hook):
    # dispatchable tasks A, B(needs A), C, D (A, C, D independent, so the width is 3 and the chain is 2);
    # E and F are the caller's own tasks (dispatch false) and must not count toward the chain
    out = hook.exit_plan(plan_md({"tasks": [
        task("A", ["a"]), task("B", ["b"], needs={"A": "r"}), task("C", ["c"]), task("D", ["d"]),
        task("E", ["e"], needs={"A": "r", "B": "r", "C": "r", "D": "r"}, dispatch=False),
        task("F", ["f"], needs={"E": "r"}, dispatch=False)]}))
    assert out == ""


def test_40_chain_through_dispatch_false_middle_task_still_denied(hook):
    # A -> X(dispatch false) -> B -> C -> D: four dispatchable tasks on one path
    out = hook.exit_plan(plan_md({"tasks": [
        task("A", ["a"]), task("X", ["x"], needs={"A": "r"}, dispatch=False),
        task("B", ["b"], needs={"X": "r"}), task("C", ["c"], needs={"B": "r"}),
        task("D", ["d"], needs={"C": "r"})]}))
    assert decision(out) == "deny"
    assert "pure chain of 4 dispatchable tasks" in reason(out)


def test_17_split_rule_ignores_non_dispatchable_task(hook):
    out = hook.exit_plan(plan_md([task("X", ["f%d" % i for i in range(9)], dispatch=False), task("Y", ["y"])]))
    assert out == ""


def test_18_subagent_payload_never_gated(hook):
    out = hook.run({"session_id": SID, "agent_id": "ag1", "hook_event_name": "PreToolUse",
                    "tool_name": "ExitPlanMode", "tool_input": {"plan": "no block"}})
    assert out == ""


@pytest.mark.parametrize("env", [
    {"CLAUDE_REQUIRE_TASK": "0"},
    {"CLAUDE_MAIN_WRITE": "1"},
    {"CLAUDE_PLAN_GATE": "0"},
])
def test_19_kill_switches_exempt(hook, env):
    assert hook.exit_plan("no block", env=env) == ""


def test_20_exempt_env_in_harness_config_exempts(hook):
    with open(hook.harness, "w", encoding="utf-8") as f:
        json.dump({"exempt_env": {"PLAN_TEST_ROLE": ["bulk*"]}}, f)
    assert hook.exit_plan("no block", env={"PLAN_TEST_ROLE": "bulk-1"}) == ""
    assert decision(hook.exit_plan("no block", env={"PLAN_TEST_ROLE": "other"})) == "deny"


def test_21_unparseable_stdin_allowed(hook):
    assert hook.run(raw="{not json") == ""


def test_22_tool_input_plan_takes_precedence_over_plans_dir(hook):
    with open(os.path.join(hook.plans, "stale.md"), "w", encoding="utf-8") as f:
        f.write("no block in this file")
    assert hook.exit_plan(plan_md(wide_plan())) == ""


# ---- edges -----------------------------------------------------------------

def edges_of(hook, obj):
    hook.make_plan_json(obj)
    return hook.plan_json()["edges"]


def test_23_prefix_and_normalised_overlap_make_edges(hook):
    e = edges_of(hook, {"tasks": [task("A", ["src"]), task("B", ["./SRC/x.py"]),
                                   task("C", [".\\Src\\Util.py"]), task("D", ["src/util.py"])]})
    assert e["B"] == ["A"]  # ./SRC/x.py is under src
    assert e["C"] == ["A"]  # .\Src\Util.py is under src; not equal to src/x.py
    assert e["D"] == ["A", "C"]  # src prefix; src/util.py equals C after normalising


def test_24_glob_overlap_and_no_overlap(hook):
    e = edges_of(hook, {"tasks": [task("A", ["src/*.py"]), task("B", ["src/util.py"]),
                                   task("C", ["docs/x.md"])]})
    assert e == {"A": [], "B": ["A"], "C": []}


def test_25_needs_edge_without_overlap(hook):
    e = edges_of(hook, {"tasks": [task("P", ["p"], needs={"Q": "r"}), task("Q", ["q"]),
                                   task("R", ["r"])]})
    assert e == {"P": ["Q"], "Q": [], "R": []}


def test_26_post_writes_plan_json_and_context_names_level_zero(hook):
    ctx = hook.make_plan_json(wide_plan())
    saved = hook.plan_json()
    assert saved["serial_reason"] is None
    assert saved["created"] > 0
    assert [t["id"] for t in saved["tasks"]] == ["A", "B", "C"]
    assert saved["edges"] == {"A": [], "B": ["A"], "C": []}
    assert "level 0: A, C" in ctx
    assert "B <- A" in ctx
    assert "'· #<task id>'" in ctx


def test_27_post_invalid_plan_writes_nothing(hook):
    out = hook.exit_plan("no block", phase="PostToolUse")
    assert out == ""
    assert not os.path.exists(os.path.join(hook.state, SID + ".plan.json"))


# ---- Agent|Task dispatch ---------------------------------------------------

def dispatch(hook, sub="juun-ketrax", sid=SID):
    return hook.tool("Agent", {"subagent_type": sub, "description": "work · #1", "prompt": "p"}, sid=sid)


def test_28_dispatch_denied_when_plan_tasks_have_no_entries(hook):
    hook.make_plan_json(wide_plan())
    out = dispatch(hook)
    assert decision(out) == "deny"
    assert "TaskCreate tasks for plan ids: A, B, C" in reason(out)


def test_29_dispatch_denied_on_missing_edge(hook):
    hook.make_plan_json(wide_plan())
    seeded(hook)
    st = json.load(open(os.path.join(hook.state, SID + ".json")))
    st["2"]["blockedBy"] = []
    hook.write_state(st)
    out = dispatch(hook)
    assert decision(out) == "deny"
    assert "missing edges: B <- A" in reason(out)


def test_30_dispatch_denied_on_extra_edge(hook):
    hook.make_plan_json(wide_plan())
    seeded(hook)
    st = json.load(open(os.path.join(hook.state, SID + ".json")))
    st["3"]["blockedBy"] = ["1"]  # C blocked by A, which the plan does not say
    hook.write_state(st)
    out = dispatch(hook)
    assert decision(out) == "deny"
    assert "extra edges: C <- A" in reason(out)


def test_31_dispatch_allowed_when_task_list_matches_plan(hook):
    hook.make_plan_json(wide_plan())
    seeded(hook)
    assert dispatch(hook) == ""


def test_32_gated_types_only(hook):
    hook.make_plan_json(wide_plan())
    assert dispatch(hook, sub="Explore") == ""
    assert dispatch(hook, sub="pr-reviewer-1") == ""
    assert dispatch(hook, sub="diagnoser-1") == ""
    assert decision(dispatch(hook, sub="general-purpose")) == "deny"


# ---- TaskUpdate edge checks ------------------------------------------------

def test_33_task_update_adds_edge_not_in_plan_denied(hook):
    hook.make_plan_json(wide_plan())
    seeded(hook)
    out = hook.tool("TaskUpdate", {"taskId": "3", "addBlockedBy": ["1"]})
    assert decision(out) == "deny"
    assert "edge 3 <- 1 is not in the plan's computed DAG" in reason(out)


def test_34_task_update_addblocks_reverse_direction_denied(hook):
    hook.make_plan_json(wide_plan())
    seeded(hook)
    out = hook.tool("TaskUpdate", {"taskId": "1", "addBlocks": ["3"]})  # 3 <- 1
    assert decision(out) == "deny"


def test_35_task_update_plan_edge_allowed(hook):
    hook.make_plan_json(wide_plan())
    seeded(hook)
    assert hook.tool("TaskUpdate", {"taskId": "2", "addBlockedBy": ["1"]}) == ""
    assert hook.tool("TaskUpdate", {"taskId": "1", "addBlocks": ["2"]}) == ""


def test_36_task_update_without_plan_ids_allowed(hook):
    hook.make_plan_json(wide_plan())
    hook.write_state({"7": {"status": "pending", "owner": None, "subject": "s", "blockedBy": [],
                            "metadata": {}},
                      "8": {"status": "pending", "owner": None, "subject": "t", "blockedBy": [],
                            "metadata": {}}})
    assert hook.tool("TaskUpdate", {"taskId": "8", "addBlockedBy": ["7"]}) == ""


def test_37_no_plan_json_means_no_edge_or_dispatch_checks(hook):
    seeded(hook)
    assert hook.tool("TaskUpdate", {"taskId": "3", "addBlockedBy": ["1"]}) == ""
    assert dispatch(hook) == ""


def test_38_subagent_dispatch_never_gated(hook):
    hook.make_plan_json(wide_plan())
    out = hook.tool("Agent", {"subagent_type": "juun-ketrax", "description": "d"}, agent_id="ag9")
    assert out == ""


# ---- plan key, seal, retire, plan source, locked state read ----------------

def test_41_plan_key_is_sha1_prefix_of_text_and_created(hook):
    ctx = hook.make_plan_json(wide_plan())
    saved = hook.plan_json()
    expect = hashlib.sha1((plan_md(wide_plan()) + str(saved["created"])).encode("utf-8")).hexdigest()[:10]
    assert saved["key"] == expect
    assert '"plan_key": "%s"' % expect in ctx


def test_42_old_plan_tasks_with_same_plan_id_do_not_match(hook):
    hook.make_plan_json(wide_plan())
    seeded(hook, key="OLDPLAN000")
    out = dispatch(hook)
    assert decision(out) == "deny"
    assert "TaskCreate tasks for plan ids: A, B, C" in reason(out)


def test_43_seal_then_delete_task_allows_dispatch(hook):
    hook.make_plan_json(wide_plan())
    seeded(hook)
    assert dispatch(hook) == ""
    assert hook.plan_json()["sealed"] is True
    st = json.load(open(os.path.join(hook.state, SID + ".json")))
    st["3"]["status"] = "deleted"
    hook.write_state(st)
    assert dispatch(hook) == ""


def test_44_delete_before_seal_still_denied(hook):
    hook.make_plan_json(wide_plan())
    seeded(hook)
    st = json.load(open(os.path.join(hook.state, SID + ".json")))
    st["3"]["status"] = "deleted"
    hook.write_state(st)
    out = dispatch(hook)
    assert decision(out) == "deny"
    assert "C" in reason(out)


def test_45_abandon_entry_disables_gating(hook):
    hook.make_plan_json(wide_plan())
    key = hook.plan_json()["key"]
    st = seeded(hook)
    st["3"]["blockedBy"] = ["1"]  # extra edge C <- A: denied without the abandon entry
    st["9"] = {"status": "pending", "owner": None, "subject": "abandon plan " + key, "blockedBy": [],
               "metadata": {"plan_abandon": key, "dispatch": False}}
    hook.write_state(st)
    assert dispatch(hook) == ""
    assert hook.tool("TaskUpdate", {"taskId": "3", "addBlockedBy": ["2"]}) == ""  # 3 <- 2 is not a plan edge


def test_46_ttl_expiry_disables_gating(hook):
    hook.make_plan_json(wide_plan())
    assert decision(dispatch(hook)) == "deny"
    set_plan_field(hook, created=int(time.time()) - 100000)  # older than the default 86400 s
    assert dispatch(hook) == ""


def test_47_ttl_config_override_and_malformed_value(hook):
    hook.make_plan_json(wide_plan())
    set_plan_field(hook, created=int(time.time()) - 100000)
    with open(hook.harness, "w", encoding="utf-8") as f:
        json.dump({"plan_ttl_sec": 200000}, f)
    assert decision(dispatch(hook)) == "deny"  # still inside the configured TTL
    with open(hook.harness, "w", encoding="utf-8") as f:
        json.dump({"plan_ttl_sec": "soon"}, f)  # malformed: default 86400 applies, plan expired
    assert dispatch(hook) == ""


def test_48_abandon_text_in_missing_tasks_deny(hook):
    hook.make_plan_json(wide_plan())
    key = hook.plan_json()["key"]
    out = dispatch(hook)
    assert decision(out) == "deny"
    assert "abandon plan %s" % key in reason(out)
    assert '{"plan_abandon": "%s", "dispatch": false}' % key in reason(out)


def test_49_extra_edge_message_says_delete_and_recreate(hook):
    hook.make_plan_json(wide_plan())
    st = seeded(hook)
    st["3"]["blockedBy"] = ["1"]
    hook.write_state(st)
    out = dispatch(hook)
    assert decision(out) == "deny"
    assert "extra edges: C <- A" in reason(out)
    assert 'status "deleted"' in reason(out)
    assert "recreate" in reason(out)


def write_plan_file(hook, name, obj, mtime=None):
    p = os.path.join(hook.plans, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(plan_md(obj))
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def write_transcript(hook, *paths):
    """A JSONL transcript: one Write to each path (in order), then an unrelated Bash tool_use."""
    p = os.path.join(hook.root, "transcript.jsonl")
    lines = [json.dumps({"type": "user", "message": {"role": "user", "content": "plan it"}})]
    for fp in paths:
        lines.append(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "writing"},
            {"type": "tool_use", "name": "Write", "input": {"file_path": fp, "content": "x"}}]}}))
    lines.append(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}}))
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return p


def pending_of(hook, sid=SID):
    with open(os.path.join(hook.state, sid + ".plan-pending.json"), encoding="utf-8") as f:
        return json.load(f)


def test_50_transcript_write_beats_newer_unrelated_plan(hook):
    mine = write_plan_file(hook, "mine.md", wide_plan(), mtime=1000)
    other = os.path.join(hook.plans, "other.md")  # newer and unrelated: no ```tasks block, so denied if chosen
    with open(other, "w", encoding="utf-8") as f:
        f.write("not a plan at all")
    os.utime(other, (2000, 2000))
    tp = write_transcript(hook, mine)
    assert hook.exit_plan(None, transcript=tp) == ""
    assert pending_of(hook)["source"] == "transcript"
    assert pending_of(hook)["path"] == mine


def test_51_post_uses_pre_recorded_path_not_a_newer_plan(hook):
    mine = write_plan_file(hook, "mine.md", wide_plan(), mtime=1000)
    tp = write_transcript(hook, mine)
    assert hook.exit_plan(None, transcript=tp) == ""
    write_plan_file(hook, "newer.md", {"tasks": [task("Z", ["z"])]}, mtime=5000)  # appears in between
    hook.exit_plan(None, phase="PostToolUse")  # no transcript, no tool_input.plan
    saved = hook.plan_json()
    assert saved["plan_file"] == mine
    assert [t["id"] for t in saved["tasks"]] == ["A", "B", "C"]
    assert not os.path.exists(os.path.join(hook.state, SID + ".plan-pending.json"))


def test_52_missing_transcript_falls_back_to_newest(hook):
    write_plan_file(hook, "good.md", wide_plan(), mtime=1000)
    missing = os.path.join(hook.root, "no-such-transcript.jsonl")
    assert hook.exit_plan(None, transcript=missing) == ""
    assert pending_of(hook)["source"] == "newest"


def test_53_fresh_lock_file_read_completes_after_wait(hook):
    hook.make_plan_json(wide_plan())
    seeded(hook)
    lock = os.path.join(hook.state, SID + ".lock")
    open(lock, "w").close()  # held by someone else, mtime now: not stale
    t0 = time.time()
    out = dispatch(hook)
    assert out == ""
    assert time.time() - t0 >= 2.5  # waited for the lock before reading unlocked
    assert os.path.exists(lock)  # not ours, so left in place


def test_54_stale_lock_is_removed_and_read_is_fast(hook):
    hook.make_plan_json(wide_plan())
    seeded(hook)
    lock = os.path.join(hook.state, SID + ".lock")
    open(lock, "w").close()
    old = time.time() - 60
    os.utime(lock, (old, old))
    t0 = time.time()
    assert dispatch(hook) == ""
    assert time.time() - t0 < 2.0
    assert not os.path.exists(lock)  # taken over as stale, released after the read


# ---- ```files block ---------------------------------------------------------

def plan_with(obj, files_body):
    return "# Plan\n\n```tasks\n" + json.dumps(obj) + "\n```\n\n```files\n" + files_body + "\n```\n"


def load_gate():
    import importlib.util
    spec = importlib.util.spec_from_file_location("plan_gate_under_test", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_55_nested_tree_passes(hook):
    body = "src/\n  a.py  [A]  new\n  b.py  [B]  edit\ndocs/\n  c.md  [C]"
    assert hook.exit_plan(plan_with(wide_plan(), body)) == ""


def test_56_box_drawing_tree_passes(hook):
    body = "src/\n├── a.py  [A]  new\n|-- b.py  [B]\ndocs/\n`-- c.md  [C]"
    assert hook.exit_plan(plan_with(wide_plan(), body)) == ""


def test_57_multi_segment_dir_and_directory_leaf_pass(hook):
    obj = {"tasks": [task("A", ["src/parser/x.py"]), task("C", ["docs/"])]}
    body = "src/parser/\n  x.py  [A]\ndocs/  [C]  whole folder"
    assert hook.exit_plan(plan_with(obj, body)) == ""


def test_58_glob_leaf_passes(hook):
    obj = {"tasks": [task("A", ["db/*.sql"]), task("B", ["db/schema.md"])]}
    body = "db/\n  *.sql  [A]\n  schema.md  [B]"
    assert hook.exit_plan(plan_with(obj, body)) == ""


def test_59_missing_files_block_denied(hook):
    out = hook.exit_plan("# Plan\n\n```tasks\n" + json.dumps(wide_plan()) + "\n```\n")
    assert decision(out) == "deny"
    assert "no ```files block in the plan" in reason(out)


def test_60_two_files_blocks_denied(hook):
    out = hook.exit_plan(plan_md(wide_plan()) + files_md(wide_plan()))
    assert decision(out) == "deny"
    assert "the plan has 2 ```files blocks; keep exactly one" in reason(out)


def test_61_missing_file_denied(hook):
    out = hook.exit_plan(plan_with(wide_plan(), "src/a.py  [A]\nsrc/b.py  [B]"))
    assert decision(out) == "deny"
    assert "files block is missing docs/c.md (written by #C)" in reason(out)


def test_62_extra_file_denied(hook):
    out = hook.exit_plan(plan_with(wide_plan(), "src/a.py  [A]\nsrc/b.py  [B]\ndocs/c.md  [C]\nx.txt  [A]"))
    assert decision(out) == "deny"
    assert "files block lists x.txt, which no task writes" in reason(out)


def test_63_wrong_ids_denied(hook):
    out = hook.exit_plan(plan_with(wide_plan(), "src/a.py  [A, B]\nsrc/b.py  [B]\ndocs/c.md  [C, Z]"))
    assert decision(out) == "deny"
    assert "files block: src/a.py is tagged [A, B] but is written by [A]" in reason(out)
    assert "files block: docs/c.md is tagged [C, Z] but is written by [C]" in reason(out)


def test_64_duplicate_leaf_denied(hook):
    out = hook.exit_plan(plan_with(wide_plan(), "src/a.py  [A]\nsrc/a.py  [A]\nsrc/b.py  [B]\ndocs/c.md  [C]"))
    assert decision(out) == "deny"
    assert "files block lists src/a.py more than once" in reason(out)


def test_65_bad_line_shape_denied_with_line_number(hook):
    out = hook.exit_plan(plan_with(wide_plan(), "src/a.py\nsrc/b.py  [B]\ndocs/c.md  [C]"))
    assert decision(out) == "deny"
    assert "files block line 1 is neither a directory" in reason(out)


def test_66_no_writes_passes_without_files_block(hook):
    obj = {"tasks": [task("A"), task("B")]}
    assert hook.exit_plan(plan_md(obj, files=False)) == ""


def test_67_no_writes_files_block_with_file_lines_denied(hook):
    obj = {"tasks": [task("A"), task("B")]}
    out = hook.exit_plan(plan_with(obj, "x.py  [A]"))
    assert decision(out) == "deny"
    assert "must have no file lines" in reason(out)


def test_68_plan_require_files_false_skips_checks(hook):
    with open(hook.harness, "w", encoding="utf-8") as f:
        json.dump({"plan_require_files": False}, f)
    assert hook.exit_plan(plan_md(wide_plan(), files=False)) == ""
    assert hook.exit_plan(plan_with(wide_plan(), "src/a.py  [A]")) == ""


def test_69_deny_text_prints_expected_files_block(hook):
    r = reason(hook.exit_plan(plan_with(wide_plan(), "src/a.py  [A]\nsrc/b.py  [B]")))
    assert "files block is missing docs/c.md (written by #C)" in r
    assert "Expected ```files block (generated from writes; paste it and add notes):" in r
    assert "```files\ndocs/\n  c.md  [C]\nsrc/\n  a.py  [A]\n  b.py  [B]\n```" in r


def test_70_render_files_tree_round_trips(hook):
    mod = load_gate()
    tasks = [task("A", ["src/parser/x.py", "src/parser/y.py"]), task("B", ["src/z.py"]),
             task("C", ["docs/"]), task("D", ["src/parser/*.sql"]), task("E", ["src/parser/x.py"])]
    leaves, problems = mod.parse_files_tree(mod.render_files_tree(tasks))
    assert problems == []
    assert mod.tree_problems(tasks, leaves) == []
    assert sorted((lf["path"], tuple(sorted(lf["ids"]))) for lf in leaves) == [
        ("docs/", ("C",)), ("src/parser/*.sql", ("D",)), ("src/parser/x.py", ("A", "E")),
        ("src/parser/y.py", ("A",)), ("src/z.py", ("B",))]


def test_71_post_context_lists_files_changed(hook):
    ctx = hook.make_plan_json(wide_plan())
    assert "Files changed:" in ctx
    assert "a.py  [A]" in ctx and "c.md  [C]" in ctx


def test_72_no_plan_text_message_shows_files_block(hook):
    out = hook.exit_plan(None)
    assert "```files" in reason(out)
