"""Tests for dispatch-stop.py (Stop hook). Each test runs the hook as a subprocess.

Run: C:/Python312/python.exe -m pytest .claude/hooks/test_dispatch_stop.py -q
"""

import json
import os
import subprocess
import sys
import time

import pytest

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dispatch-stop.py")
SID = "sess-1"
CLEARED = ("CLAUDE_DISPATCH_STOP", "CLAUDE_REQUIRE_TASK", "CLAUDE_MAIN_WRITE", "HARNESS_ROLE",
           "TASK_GATE_POOL_SIZE", "TASK_GATE_RUNNING_STALE_SEC", "CLAUDE_HARNESS_CONFIG",
           "TASK_GATE_STATE_DIR", "DISPATCH_STOP_LOG", "CLAUDE_LOCAL", "LOCAL_DISPATCH")


def task(status="pending", owner=None, subject="subj", blockedBy=None, metadata=None, **extra):
    e = {"status": status, "owner": owner, "subject": subject,
         "blockedBy": blockedBy or [], "metadata": metadata or {}}
    e.update(extra)
    return e


class Env:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.state_dir = tmp_path / "state"
        self.state_dir.mkdir()
        self.config = tmp_path / "harness.json"
        self.log = tmp_path / "dispatch-stop.log"

    def write_state(self, tasks, sid=SID):
        (self.state_dir / (sid + ".json")).write_text(json.dumps(tasks), encoding="utf-8")

    def write_config(self, data):
        self.config.write_text(json.dumps(data), encoding="utf-8")

    def run(self, payload=None, raw=None, extra=None):
        env = {k: v for k, v in os.environ.items() if k not in CLEARED}
        env["TASK_GATE_STATE_DIR"] = str(self.state_dir)
        env["CLAUDE_HARNESS_CONFIG"] = str(self.config)
        env["DISPATCH_STOP_LOG"] = str(self.log)
        env.update(extra or {})
        if raw is None:
            body = {"session_id": SID, "stop_hook_active": False, "transcript_path": "x"}
            body.update(payload or {})
            raw = json.dumps(body).encode("utf-8")
        proc = subprocess.run([sys.executable, "-I", HOOK], input=raw, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=env, timeout=30)
        assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
        return proc.stdout.decode("utf-8")


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def fresh(sec_ago=10):
    return time.time() - sec_ago


def blocked(out):
    if not out.strip():
        return None
    return json.loads(out)


def test_blocks_when_ready_and_free_slots(env):
    env.write_state({"1": task(subject="first task"), "2": task(subject="second task")})
    out = blocked(env.run())
    assert out["decision"] == "block"
    assert "#1 first task" in out["reason"]
    assert "#2 second task" in out["reason"]
    assert "2 free slot(s) (0/8 running)" in out["reason"]
    assert env.log.exists()


def test_no_block_when_pool_full(env):
    env.write_config({"pool_size": 2})
    env.write_state({
        "1": task("in_progress", "qorvax-tel", dispatched_at=fresh()),
        "2": task("in_progress", "zyrene-vosk", dispatched_at=fresh()),
        "3": task(subject="waiting"),
    })
    assert env.run() == ""


def test_k_limited_by_free_executors(env):
    env.write_config({"executors": ["alpha", "beta"]})
    env.write_state({
        "1": task("in_progress", "alpha", dispatched_at=fresh()),
        "2": task(subject="a"), "3": task(subject="b"), "4": task(subject="c"),
    })
    out = blocked(env.run())
    assert out["decision"] == "block"
    assert "1 free slot(s) (1/8 running)" in out["reason"]
    assert "(free: beta)" in out["reason"]
    assert "#2 a" in out["reason"]
    assert "#3" not in out["reason"]


def test_stale_running_not_counted(env):
    env.write_config({"pool_size": 1})
    env.write_state({
        "1": task("in_progress", "qorvax-tel", dispatched_at=fresh(7200)),
        "2": task(subject="waiting"),
    })
    out = blocked(env.run())
    assert out["decision"] == "block"
    assert "(0/1 running)" in out["reason"]


def test_fresh_running_counted(env):
    env.write_config({"pool_size": 1})
    env.write_state({
        "1": task("in_progress", "qorvax-tel", dispatched_at=fresh(60)),
        "2": task(subject="waiting"),
    })
    assert env.run() == ""


def test_finished_running_not_counted(env):
    env.write_config({"pool_size": 1})
    env.write_state({
        "1": task("in_progress", "qorvax-tel", dispatched_at=fresh(60), finished_at=fresh(5)),
        "2": task(subject="waiting"),
    })
    out = blocked(env.run())
    assert out["decision"] == "block"


def test_dispatch_pending_counted(env):
    env.write_config({"pool_size": 1})
    env.write_state({
        "1": task("in_progress", "qorvax-tel", dispatch_pending_at=fresh(10)),
        "2": task(subject="waiting"),
    })
    assert env.run() == ""


def test_stale_dispatch_pending_not_counted(env):
    env.write_config({"pool_size": 1})
    env.write_state({
        "1": task("in_progress", "qorvax-tel", dispatch_pending_at=fresh(600)),
        "2": task(subject="waiting"),
    })
    out = blocked(env.run())
    assert out["decision"] == "block"
    assert "(0/1 running)" in out["reason"]


def test_blocked_by_open_blocker_not_ready(env):
    env.write_state({"1": task(subject="root"), "2": task(subject="child", blockedBy=["1"])})
    out = blocked(env.run())
    assert "#1 root" in out["reason"]
    assert "#2" not in out["reason"]


def test_completed_blocker_makes_task_ready(env):
    env.write_state({"1": task("completed", subject="done"),
                     "2": task(subject="child", blockedBy=["1"])})
    out = blocked(env.run())
    assert "#2 child" in out["reason"]
    assert "#1" not in out["reason"]


def test_dispatch_false_ignored(env):
    env.write_state({"1": task(subject="not executor work", metadata={"dispatch": False})})
    assert env.run() == ""


def test_stop_hook_active_silent(env):
    env.write_state({"1": task(subject="ready")})
    assert env.run({"stop_hook_active": True}) == ""


def test_subagent_payload_silent(env):
    env.write_state({"1": task(subject="ready")})
    assert env.run({"agent_id": "agent-7"}) == ""


@pytest.mark.parametrize("var,val", [
    ("CLAUDE_DISPATCH_STOP", "0"),
    ("CLAUDE_REQUIRE_TASK", "0"),
    ("CLAUDE_MAIN_WRITE", "1"),
])
def test_kill_switches_silent(env, var, val):
    env.write_state({"1": task(subject="ready")})
    assert env.run(extra={var: val}) == ""


@pytest.mark.parametrize("var", ["CLAUDE_LOCAL", "LOCAL_DISPATCH"])
def test_local_mode_silent(env, var):
    env.write_state({"1": task(subject="ready")})
    assert env.run(extra={var: "1"}) == ""


def test_local_mode_blocks_without_flag(env):
    env.write_state({"1": task(subject="ready")})
    assert blocked(env.run())["decision"] == "block"


def test_exempt_env_match_silent(env):
    env.write_config({"exempt_env": {"HARNESS_ROLE": ["bot-*"]}})
    env.write_state({"1": task(subject="ready")})
    assert env.run(extra={"HARNESS_ROLE": "bot-3"}) == ""
    out = blocked(env.run(extra={"HARNESS_ROLE": "human"}))
    assert out["decision"] == "block"


def test_missing_state_file_silent(env):
    assert env.run() == ""


def test_unparseable_stdin_silent(env):
    env.write_state({"1": task(subject="ready")})
    assert env.run(raw=b"this is not json {{{") == ""


def test_pool_size_from_config(env):
    env.write_config({"pool_size": 1})
    env.write_state({"1": task("in_progress", "qorvax-tel", dispatched_at=fresh()),
                     "2": task(subject="waiting")})
    assert env.run() == ""
    env.write_config({"pool_size": 4})
    out = blocked(env.run())
    assert out["decision"] == "block"
    assert "(1/4 running)" in out["reason"]


def test_pool_size_from_env_overrides_config(env):
    env.write_config({"pool_size": 1})
    env.write_state({"1": task("in_progress", "qorvax-tel", dispatched_at=fresh()),
                     "2": task(subject="waiting")})
    out = blocked(env.run(extra={"TASK_GATE_POOL_SIZE": "4"}))
    assert "(1/4 running)" in out["reason"]


def test_stale_sec_from_env(env):
    env.write_state({"1": task("in_progress", "qorvax-tel", dispatched_at=fresh(120)),
                     "2": task(subject="waiting")})
    env.write_config({"pool_size": 1})
    assert env.run(extra={"TASK_GATE_RUNNING_STALE_SEC": "60"}) != ""
    assert env.run(extra={"TASK_GATE_RUNNING_STALE_SEC": "600"}) == ""


def test_no_ready_tasks_silent(env):
    env.write_state({"1": task("completed", subject="done")})
    assert env.run() == ""


def test_subject_truncated_to_80(env):
    env.write_state({"1": task(subject="x" * 200)})
    out = blocked(env.run())
    assert "#1 " + "x" * 80 + ";" in out["reason"] or "#1 " + "x" * 80 + "." in out["reason"]
    assert "x" * 81 not in out["reason"]


def test_numeric_ids_sorted(env):
    env.write_state({"10": task(subject="ten"), "2": task(subject="two")})
    out = blocked(env.run())
    assert out["reason"].index("#2 two") < out["reason"].index("#10 ten")


def test_pool_size_zero_falls_back_to_default(env):
    env.write_config({"pool_size": 0})
    env.write_state({"1": task(subject="ready")})
    out = blocked(env.run())
    assert out["decision"] == "block"
    assert "(0/8 running)" in out["reason"]


def test_empty_executors_list_falls_back_to_defaults(env):
    env.write_config({"executors": []})
    env.write_state({"1": task(subject="ready")})
    out = blocked(env.run())
    assert out["decision"] == "block"
    assert "(free: qorvax-tel, zyrene-vosk, thal-ummir" in out["reason"]


@pytest.mark.parametrize("cfg", [
    {"executors": ["alpha", 3]},
    {"executors": ["alpha", ""]},
    {"executors": "alpha"},
    {"pool_size": True},
    {"pool_size": 2.5},
    {"pool_size": -2},
    {"running_stale_sec": True},
    {"running_stale_sec": 0},
    {"running_stale_sec": -5},
])
def test_malformed_config_values_fall_back_like_task_gate(env, cfg):
    env.write_config(cfg)
    env.write_state({"1": task(subject="ready")})
    out = blocked(env.run())
    assert out["decision"] == "block"
    assert "(0/8 running)" in out["reason"]
    assert "(free: qorvax-tel, zyrene-vosk, thal-ummir" in out["reason"]


def test_string_pool_size_from_config_is_read(env):
    env.write_config({"pool_size": "3"})
    env.write_state({"1": task(subject="ready")})
    out = blocked(env.run())
    assert "(0/3 running)" in out["reason"]


def test_foreground_finish_after_dispatch_not_running(env):
    # Pre -> SubagentStop -> PostToolUse: dispatched_at at the launch, finished_at after it
    env.write_config({"pool_size": 1})
    env.write_state({"1": task("in_progress", "qorvax-tel", dispatched_at=fresh(30), finished_at=fresh(20)),
                     "2": task(subject="waiting")})
    out = blocked(env.run())
    assert out["decision"] == "block"
    assert "(0/1 running)" in out["reason"]


def test_redispatch_pending_newer_than_old_finish_running(env):
    env.write_config({"pool_size": 1})
    env.write_state({"1": task("in_progress", "qorvax-tel", dispatched_at=fresh(600), finished_at=fresh(500),
                               dispatch_pending_at=fresh(5)),
                     "2": task(subject="waiting")})
    assert env.run() == ""


def test_pending_newer_than_dispatch_uses_120_window(env):
    env.write_config({"pool_size": 1})
    env.write_state({"1": task("in_progress", "qorvax-tel", dispatched_at=fresh(300), dispatch_pending_at=fresh(150)),
                     "2": task(subject="waiting")})
    out = blocked(env.run())
    assert out["decision"] == "block"
    assert "(0/1 running)" in out["reason"]
    env.write_state({"1": task("in_progress", "qorvax-tel", dispatched_at=fresh(300), dispatch_pending_at=fresh(60)),
                     "2": task(subject="waiting")})
    assert env.run() == ""


def test_pending_older_than_dispatch_uses_stale_window(env):
    env.write_config({"pool_size": 1})
    env.write_state({"1": task("in_progress", "qorvax-tel", dispatched_at=fresh(300), dispatch_pending_at=fresh(400)),
                     "2": task(subject="waiting")})
    assert env.run() == ""
