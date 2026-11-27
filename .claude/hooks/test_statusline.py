"""Tests for ../statusline.py: the one-line status line (model and effort, the context bar,
prompt-cache state, cost and wall time), the auto-compact window, the age and duration
formatters, and stdin handling. Every run is hermetic: HOME and CLAUDE_CONFIG_DIR point
under pytest's tmp_path, never at real ~/.claude state."""
import importlib.util
import json
import os
import re
import subprocess
import sys

import pytest

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "statusline.py")
NOW = 1_000_000.0
SID = "11111111-2222-3333-4444-555555555555"
# SGR colour codes; stripped so assertions read the text only.
ANSI = re.compile(r"\x1b\[[0-9;]*m")
MARKER = "[status line: no input]"
FULL = "[Opus 5.5 · medium] · ctx [###############-----] 152k/200k · cache warm (1h) 59m left · $8.55 · 1h00m"


class Env:
    """One test's directories, all under tmp_path."""

    def __init__(self, base):
        self.home = str(base / "home")
        self.cfg = str(base / "claude")  # CLAUDE_CONFIG_DIR: statusline.json
        os.makedirs(self.home)
        os.makedirs(self.cfg)

    def base_env(self):
        return {"PATH": os.environ.get("PATH", ""), "HOME": self.home,
                "CLAUDE_CONFIG_DIR": self.cfg, "STATUSLINE_NOW": str(NOW)}


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def run_raw(env, payload, extra=None):
    """Run the script once with stdin `payload`; return raw stdout (colour included)."""
    e = env.base_env()
    for k, v in (extra or {}).items():  # a None value removes the variable
        if v is None:
            e.pop(k, None)
        else:
            e[k] = v
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    r = subprocess.run([sys.executable, SCRIPT], input=raw, capture_output=True,
                       encoding="utf-8", env=e, timeout=20)
    assert r.returncode == 0, r.stderr
    assert r.stderr == ""
    assert "Traceback" not in r.stdout
    assert r.stdout.endswith("\n")
    return r.stdout


def rows_of(raw):
    """The output as plain-text rows, colour stripped, trailing empty split dropped."""
    return [ANSI.sub("", line) for line in raw.split("\n")[:-1]]


def run(env, payload, extra=None):
    return rows_of(run_raw(env, payload, extra))


def payload(name="Opus", **fields):
    """A minimal session payload; keyword fields are added to it as they are."""
    p = {"session_id": SID, "cwd": "/tmp", "model": {"id": "claude-opus", "display_name": name}}
    p.update(fields)
    return p


def load_module(env, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", env.cfg)
    monkeypatch.setenv("HOME", env.home)
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    spec = importlib.util.spec_from_file_location("statusline_under_test", SCRIPT)
    sl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sl)
    return sl


def test_full_line_has_model_ctx_cache_cost_and_time(env):
    p = payload("Opus 5.5", effort={"level": "medium"},
                context_window={"used_percentage": 76, "total_input_tokens": 152000,
                                "context_window_size": 200000},
                prompt_cache={"warm": True, "ttl": "1h", "expires_at": NOW + 3540},
                cost={"total_cost_usd": 8.55, "total_duration_ms": 3_600_000})
    assert run(env, p) == [FULL]


def test_output_is_exactly_one_line(env):
    rows = run(env, payload())
    assert len(rows) == 1


def test_model_only_without_context_shows_ctx_n_a(env):
    assert run(env, payload()) == ["[Opus] · ctx n/a"]


def test_model_missing_shows_placeholder(env):
    assert run(env, {"session_id": SID}) == ["[?] · ctx n/a"]


def test_model_falls_back_to_id(env):
    rows = run(env, {"session_id": SID, "model": {"id": "m-1"}})
    assert rows[0].startswith("[m-1]")


def test_effort_renders_in_the_model_bracket(env):
    rows = run(env, payload("Opus", effort={"level": "high"}))
    assert rows[0].startswith("[Opus · high] ·")


def test_control_chars_in_model_name_cannot_break_line(env):
    rows = run(env, payload(name="Op\nus"))
    assert len(rows) == 1
    assert rows[0].startswith("[Op us]")


def test_auto_compact_window_rescales_the_ctx_bar(env):
    # 51,398 tokens of a 1M window read 17% of 300k when the auto-compact window is 300k.
    p = payload(context_window={"used_percentage": 5, "total_input_tokens": 51398,
                                "context_window_size": 1_000_000})
    rows = run(env, p, {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "300000"})
    assert rows == ["[Opus] · ctx [" + "#" * 3 + "-" * 17 + "] 51k/300k"]


def test_cache_cold_shows_cause_and_rebuild_price(env):
    p = payload(model={"id": "claude-opus-5-5", "display_name": "Opus 5.5"},
                prompt_cache={"warm": False, "caching_observed": True, "ttl": "5m",
                              "recache_tokens_if_cold": 100000,
                              "last_miss_cause": {"causes": ["system_prompt_changed"]}})
    assert run(env, p)[0].endswith("cache cold (system prompt changed) $0.50 to rebuild")


def test_cache_lapsed_past_its_expiry(env):
    p = payload(prompt_cache={"warm": True, "ttl": "5m", "expires_at": NOW - 10})
    assert run(env, p)[0].endswith("· cache lapsed")


def test_cache_warm_near_expiry_prices_the_rebuild(env):
    p = payload(model={"id": "claude-opus-5-5", "display_name": "Opus 5.5"},
                prompt_cache={"warm": True, "ttl": "1h", "expires_at": NOW + 120,
                              "recache_tokens_if_cold": 100000})
    assert run(env, p)[0].endswith("cache warm (1h) 2m left $0.80 if it lapses")


def test_cost_and_time_are_omitted_when_absent(env):
    assert run(env, payload(cost={})) == ["[Opus] · ctx n/a"]


def test_ctx_colour_tiers(env, monkeypatch):
    sl = load_module(env, monkeypatch)
    cfg = dict(sl.DEFAULTS)
    assert sl.ctx_color(None, 50, cfg) == sl.GREEN
    assert sl.ctx_color(None, 80, cfg) == sl.YELLOW
    assert sl.ctx_color(None, 95, cfg) == sl.RED
    assert sl.ctx_color(250_000, 50, cfg) == sl.YELLOW  # the token tier can only raise the colour
    assert sl.ctx_color(350_000, 50, cfg) == sl.RED


def test_age_and_duration_formatting(env, monkeypatch):
    sl = load_module(env, monkeypatch)
    assert sl.fmt_age(None) == "?"
    assert sl.fmt_age(12) == "12s"
    assert sl.fmt_age(60) == "60s"
    assert sl.fmt_age(90) == "1m"
    assert sl.fmt_age(3540) == "59m"
    assert sl.fmt_age(5400) == "1h"
    assert sl.fmt_age(172_800) == "2d"
    assert sl.fmt_duration(3_600_000) == "1h00m"
    assert sl.fmt_duration(90_000) == "1m"


def test_garbage_stdin_prints_no_input_marker(env):
    for raw in ["not json", "", "{not json"]:
        assert run(env, raw) == [MARKER], raw


def test_non_object_json_renders_without_crash(env):
    for raw in ["[1,2,3]", "null", "{\"session_id\": 5}"]:
        rows = run(env, raw)
        assert rows, raw
        assert "[?]" in rows[0], raw
        assert not any(MARKER in r for r in rows), raw


def test_colour_is_emitted_by_default_and_disabled_by_config(env):
    raw = run_raw(env, payload(context_window={"used_percentage": 5}))
    assert "\x1b[" in raw
    with open(os.path.join(env.cfg, "statusline.json"), "w") as f:
        json.dump({"color": False}, f)
    raw = run_raw(env, payload(context_window={"used_percentage": 5}))
    assert "\x1b[" not in raw
    assert rows_of(raw) == ["[Opus] · ctx [#-------------------]"]
