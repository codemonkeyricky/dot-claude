"""Unit tests for deploy-trigger.py and deploy-reconcile.py (pure parts only, no network).

Run from the repo's .claude/hooks directory: python3 -m unittest test_deploy_hooks -v
"""

import fcntl
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock

HERE = os.path.dirname(os.path.abspath(__file__))
TRIGGER_PATH = os.path.join(HERE, "deploy-trigger.py")


def _load(modname, filename):
    spec = importlib.util.spec_from_file_location(modname, os.path.join(HERE, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


trigger = _load("deploy_trigger", "deploy-trigger.py")
reconcile = _load("deploy_reconcile", "deploy-reconcile.py")


class TriggerMatchTests(unittest.TestCase):
    POSITIVE = [
        "git push",
        "git push origin x",
        "git -C /home/user/x push origin b",
        "git -c user.name=t push",
        "cd /home/user/x && git push",
        "GIT_TRACE=1 git push origin feat",
        "git --no-pager push --force-with-lease origin feat",
        "echo ok; git push origin b",
        "gh pr merge 12 --squash",
        "gh pr create --fill",
        "gh -R example-org/app pr close 7",
        "gh pr ready 9",
    ]
    NEGATIVE = [
        "git log --grep push",
        "gh pr view 3",
        "echo git push",  # quoted/echoed text is not a command: documented choice
        "ls",
        "git status",
        "git commit -m 'push later'",
        "gh pr list --state open",
        "git -C /x log --oneline push",
        "",
    ]

    def test_positive_commands_trigger(self):
        for cmd in self.POSITIVE:
            with self.subTest(cmd=cmd):
                self.assertTrue(trigger.is_deploy_trigger(cmd))

    def test_negative_commands_do_not_trigger(self):
        for cmd in self.NEGATIVE:
            with self.subTest(cmd=cmd):
                self.assertFalse(trigger.is_deploy_trigger(cmd))

    def test_unbalanced_quotes_fall_back_without_raising(self):
        self.assertTrue(trigger.is_deploy_trigger("git push origin 'broken"))
        self.assertFalse(trigger.is_deploy_trigger("ls 'broken"))


class PlanTests(unittest.TestCase):
    MASTER = "a" * 40
    OLD = "b" * 40
    HEAD = "c" * 40
    NEWHEAD = "d" * 40

    def base_state(self, **over):
        st = {"prod": {"sha": self.MASTER, "failed_sha": None}, "previews": {}}
        st.update(over)
        return st

    def test_new_master_plans_prod(self):
        st = self.base_state(prod={"sha": self.OLD})
        self.assertEqual(reconcile.plan(st, self.MASTER, []), [{"kind": "prod", "sha": self.MASTER}])

    def test_same_master_plans_nothing(self):
        self.assertEqual(reconcile.plan(self.base_state(), self.MASTER, []), [])

    def test_failed_master_is_not_retried(self):
        st = self.base_state(prod={"sha": self.OLD, "failed_sha": self.MASTER})
        self.assertEqual(reconcile.plan(st, self.MASTER, []), [])

    def test_failed_master_retries_on_new_sha(self):
        st = self.base_state(prod={"sha": self.OLD, "failed_sha": self.OLD})
        self.assertEqual(reconcile.plan(st, self.MASTER, []), [{"kind": "prod", "sha": self.MASTER}])

    def test_force_prod_plans_even_when_recorded(self):
        acts = reconcile.plan(self.base_state(), self.MASTER, [], force_prod=True)
        self.assertEqual(acts, [{"kind": "prod", "sha": self.MASTER}])

    def test_new_pr_head_plans_preview(self):
        st = self.base_state(previews={"5": {"sha": self.OLD}})
        prs = [{"number": 5, "headRefOid": self.NEWHEAD, "isCrossRepository": False}]
        self.assertEqual(reconcile.plan(st, self.MASTER, prs), [{"kind": "preview", "pr": 5, "sha": self.NEWHEAD}])

    def test_unseen_pr_plans_preview(self):
        prs = [{"number": 8, "headRefOid": self.HEAD, "isCrossRepository": False}]
        self.assertEqual(reconcile.plan(self.base_state(), self.MASTER, prs),
                         [{"kind": "preview", "pr": 8, "sha": self.HEAD}])

    def test_same_pr_head_plans_nothing(self):
        st = self.base_state(previews={"5": {"sha": self.HEAD}})
        prs = [{"number": 5, "headRefOid": self.HEAD, "isCrossRepository": False}]
        self.assertEqual(reconcile.plan(st, self.MASTER, prs), [])

    def test_failed_pr_head_is_not_retried(self):
        st = self.base_state(previews={"5": {"sha": self.OLD, "failed_sha": self.HEAD}})
        prs = [{"number": 5, "headRefOid": self.HEAD, "isCrossRepository": False}]
        self.assertEqual(reconcile.plan(st, self.MASTER, prs), [])

    def test_retry_failed_prod_sha_plans_prod(self):
        st = self.base_state(prod={"sha": self.OLD, "failed_sha": self.MASTER})
        self.assertEqual(reconcile.plan(st, self.MASTER, [], retry_failed=True),
                         [{"kind": "prod", "sha": self.MASTER}])

    def test_retry_failed_pr_head_plans_preview(self):
        st = self.base_state(previews={"5": {"sha": self.OLD, "failed_sha": self.HEAD}})
        prs = [{"number": 5, "headRefOid": self.HEAD, "isCrossRepository": False}]
        self.assertEqual(reconcile.plan(st, self.MASTER, prs, retry_failed=True),
                         [{"kind": "preview", "pr": 5, "sha": self.HEAD}])

    def test_failed_shas_plan_nothing_without_retry_flag(self):
        st = self.base_state(prod={"sha": self.OLD, "failed_sha": self.MASTER},
                             previews={"5": {"sha": self.OLD, "failed_sha": self.HEAD}})
        prs = [{"number": 5, "headRefOid": self.HEAD, "isCrossRepository": False}]
        self.assertEqual(reconcile.plan(st, self.MASTER, prs, retry_failed=False), [])
        self.assertEqual(reconcile.plan(st, self.MASTER, prs), [])

    def test_closed_pr_plans_cleanup(self):
        st = self.base_state(previews={"5": {"sha": self.HEAD}, "9": {"sha": self.OLD}})
        prs = [{"number": 9, "headRefOid": self.OLD, "isCrossRepository": False}]
        self.assertEqual(reconcile.plan(st, self.MASTER, prs), [{"kind": "cleanup", "pr": 5}])

    def test_cross_repo_pr_is_ignored(self):
        prs = [{"number": 4, "headRefOid": self.HEAD, "isCrossRepository": True}]
        self.assertEqual(reconcile.plan(self.base_state(), self.MASTER, prs), [])

    def test_unknown_pr_list_plans_no_previews_or_cleanups(self):
        st = self.base_state(previews={"5": {"sha": self.HEAD}})
        self.assertEqual(reconcile.plan(st, self.MASTER, None), [])

    def test_missing_state_plans_nothing(self):
        self.assertEqual(reconcile.plan(None, self.MASTER, [{"number": 1, "headRefOid": self.HEAD,
                                                             "isCrossRepository": False}]), [])

    def test_order_is_prod_then_previews_then_cleanups(self):
        st = self.base_state(prod={"sha": self.OLD}, previews={"7": {"sha": self.OLD}})
        prs = [{"number": 2, "headRefOid": self.HEAD, "isCrossRepository": False}]
        kinds = [a["kind"] for a in reconcile.plan(st, self.MASTER, prs)]
        self.assertEqual(kinds, ["prod", "preview", "cleanup"])


class UrlParseTests(unittest.TestCase):
    def test_json_deployment_url_without_scheme_gets_https(self):
        out = '{"deployment": {"url": "app-abc.vercel.app", "id": "dpl_1"}}'
        self.assertEqual(reconcile.parse_deploy_url(out), "https://app-abc.vercel.app")

    def test_json_with_preceding_text_line(self):
        out = 'Vercel CLI 63.1.0\n{"deployment": {"url": "https://x-y.vercel.app"}}\n'
        self.assertEqual(reconcile.parse_deploy_url(out), "https://x-y.vercel.app")

    def test_json_without_deployment_falls_back_to_text(self):
        out = '{"status": "ok"}\nProduction: https://app-q9.vercel.app [3s]\n'
        self.assertEqual(reconcile.parse_deploy_url(out), "https://app-q9.vercel.app")

    def test_plain_text_takes_last_vercel_app_url(self):
        out = ("Inspect: https://vercel.com/team/proj/abc\n"
               "Preview: https://app-pr-aaa.vercel.app [2s]\n"
               "Production: https://app-bbb.vercel.app [9s]\n")
        self.assertEqual(reconcile.parse_deploy_url(out), "https://app-bbb.vercel.app")

    def test_no_url_returns_none(self):
        self.assertIsNone(reconcile.parse_deploy_url("Error: nothing deployed"))
        self.assertIsNone(reconcile.parse_deploy_url(""))

    def test_bundle_from_html(self):
        html = '<script type="module" src="./assets/index-CV3Hno1W.js"></script>'
        self.assertEqual(reconcile.bundle_from_html(html), "index-CV3Hno1W.js")
        self.assertIsNone(reconcile.bundle_from_html("<html></html>"))


class LockModeTests(unittest.TestCase):
    def test_no_flags_tries_the_lock(self):
        self.assertEqual(reconcile.lock_mode(), "try")

    def test_force_prod_blocks(self):
        self.assertEqual(reconcile.lock_mode(force_prod=True), "block")

    def test_retry_failed_blocks(self):
        self.assertEqual(reconcile.lock_mode(retry_failed=True), "block")

    def test_dry_run_takes_no_lock(self):
        self.assertEqual(reconcile.lock_mode(force_prod=True, dry_run=True), "none")
        self.assertEqual(reconcile.lock_mode(retry_failed=True, dry_run=True), "none")
        self.assertEqual(reconcile.lock_mode(dry_run=True), "none")


class LockAcquireTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="lockwait-")
        self._old_state_dir = reconcile.STATE_DIR
        reconcile.STATE_DIR = self.dir
        self.entry = {"name": "t"}
        self.holder = os.open(os.path.join(self.dir, "t.lock"), os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(self.holder, fcntl.LOCK_EX)

    def tearDown(self):
        try:
            fcntl.flock(self.holder, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(self.holder)
        reconcile.STATE_DIR = self._old_state_dir
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_try_returns_none_while_held(self):
        self.assertIsNone(reconcile.acquire(self.entry))

    def test_block_waits_for_the_holder_then_acquires(self):
        released = threading.Timer(0.5, lambda: fcntl.flock(self.holder, fcntl.LOCK_UN))
        released.start()
        start = time.monotonic()
        fd = reconcile.acquire(self.entry, block=True)
        waited = time.monotonic() - start
        released.join()
        try:
            self.assertIsNotNone(fd)
            self.assertGreaterEqual(waited, 0.3)
        finally:
            if fd is not None:
                os.close(fd)


class HealthOkTests(unittest.TestCase):
    EXPECT = {"ok": True, "storage": "kv"}

    def test_matching_json_passes(self):
        self.assertTrue(reconcile.health_ok('{"ok": true, "storage": "kv", "extra": 1}', self.EXPECT))

    def test_mismatching_value_fails(self):
        self.assertFalse(reconcile.health_ok('{"ok": true, "storage": "fs"}', self.EXPECT))

    def test_missing_key_fails(self):
        self.assertFalse(reconcile.health_ok('{"ok": true}', self.EXPECT))

    def test_non_json_body_fails(self):
        self.assertFalse(reconcile.health_ok("<html>502 Bad Gateway</html>", self.EXPECT))
        self.assertFalse(reconcile.health_ok("", self.EXPECT))
        self.assertFalse(reconcile.health_ok(None, self.EXPECT))

    def test_json_array_fails_when_expect_is_set(self):
        self.assertFalse(reconcile.health_ok('[{"ok": true, "storage": "kv"}]', self.EXPECT))

    def test_bool_is_not_matched_by_one(self):
        self.assertFalse(reconcile.health_ok('{"ok": 1, "storage": "kv"}', self.EXPECT))

    def test_empty_expect_accepts_any_json(self):
        self.assertTrue(reconcile.health_ok('{"anything": 1}', {}))
        self.assertFalse(reconcile.health_ok("not json", {}))


class PollHealthTests(unittest.TestCase):
    HEALTH = {"path": "/api/health", "expect": {"ok": True, "storage": "kv"}}

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="health-")
        self._old_state_dir = reconcile.STATE_DIR
        reconcile.STATE_DIR = self.dir

    def tearDown(self):
        reconcile.STATE_DIR = self._old_state_dir
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_retries_until_the_body_matches(self):
        bodies = iter(["<html>warming</html>", '{"ok": true, "storage": "kv"}'])
        seen = []

        def fake_fetch(url):
            seen.append(url)
            return next(bodies)

        with unittest.mock.patch.object(reconcile, "fetch_text", fake_fetch), \
                unittest.mock.patch.object(reconcile.time, "sleep", lambda s: None):
            self.assertEqual(reconcile.poll_health("t", "https://h.example", self.HEALTH), (True, None))
        self.assertEqual(seen, ["https://h.example/api/health"] * 2)

    def test_all_bad_reports_body_tail(self):
        with unittest.mock.patch.object(reconcile, "fetch_text", lambda url: '{"ok": true, "storage": "fs"}'), \
                unittest.mock.patch.object(reconcile.time, "sleep", lambda s: None):
            ok, err = reconcile.poll_health("t", "https://h.example", self.HEALTH)
        self.assertFalse(ok)
        self.assertTrue(err.startswith("health check failed: "))
        self.assertIn('"storage": "fs"', err)

    def test_no_response_is_reported(self):
        with unittest.mock.patch.object(reconcile, "fetch_text", lambda url: None), \
                unittest.mock.patch.object(reconcile.time, "sleep", lambda s: None):
            ok, err = reconcile.poll_health("t", "https://h.example", self.HEALTH)
        self.assertEqual((ok, err), (False, "health check failed: no response"))


class StrategyTests(unittest.TestCase):
    def test_missing_strategy_defaults_to_remote(self):
        self.assertEqual(reconcile.strategy_of({"name": "x"}), "remote")

    def test_explicit_strategies(self):
        self.assertEqual(reconcile.strategy_of({"name": "x", "strategy": "remote"}), "remote")
        self.assertEqual(reconcile.strategy_of({"name": "x", "strategy": "prebuilt"}), "prebuilt")

    def test_unknown_strategy_raises(self):
        with self.assertRaises(reconcile.DeployError):
            reconcile.strategy_of({"name": "x", "strategy": "docker"})


class PrebuiltHelperTests(unittest.TestCase):
    ENTRY = {"name": "site",
             "build": {"command": ["node", "scripts/build.mjs", "{out}"],
                       "env": {"KV_PUBLIC_BASE": "https://kv.example", "npm_config_workspace_concurrency": "1"}}}

    def test_out_placeholder_is_substituted(self):
        self.assertEqual(reconcile.build_command(self.ENTRY, "/cache/root/out"),
                         ["node", "scripts/build.mjs", "/cache/root/out"])

    def test_build_command_is_required(self):
        with self.assertRaises(reconcile.DeployError):
            reconcile.build_command({"name": "x"}, "/o")

    def test_build_env_merges_config_and_pins_sha(self):
        env = reconcile.build_env(self.ENTRY, "a" * 40)
        self.assertEqual(env["KV_PUBLIC_BASE"], "https://kv.example")
        self.assertEqual(env["npm_config_workspace_concurrency"], "1")
        self.assertEqual(env["VERCEL_GIT_COMMIT_SHA"], "a" * 40)

    def test_prebuilt_prod_args(self):
        self.assertEqual(reconcile.prebuilt_deploy_args("S", True),
                         ["deploy", "--prebuilt", "--prod", "--archive=tgz", "--yes", "--scope", "S"])

    def test_prebuilt_preview_args_have_no_prod(self):
        self.assertEqual(reconcile.prebuilt_deploy_args("S", False),
                         ["deploy", "--prebuilt", "--archive=tgz", "--yes", "--scope", "S"])

    def test_remote_args_are_unchanged(self):
        sha = "b" * 40
        self.assertEqual(reconcile.remote_deploy_args("S", sha, True),
                         ["deploy", "--prod", "--yes", "--scope", "S", "--build-env", f"VERCEL_GIT_COMMIT_SHA={sha}"])
        self.assertEqual(reconcile.remote_deploy_args("S", sha, False),
                         ["deploy", "--yes", "--scope", "S", "--build-env", f"VERCEL_GIT_COMMIT_SHA={sha}"])


class DeploysLogGateTests(unittest.TestCase):
    def test_gate_only_opens_for_true(self):
        self.assertTrue(reconcile.wants_deploys_log({"deploys_log": True}))
        self.assertFalse(reconcile.wants_deploys_log({"deploys_log": False}))
        self.assertFalse(reconcile.wants_deploys_log({"deploys_log": "true"}))
        self.assertFalse(reconcile.wants_deploys_log({}))

    def _prod_deploy(self, entry):
        d = tempfile.mkdtemp(prefix="deploysgate-")
        names = ("STATE_DIR", "DEPLOYS_LOG", "EVENTS_LOG")
        saved = {n: getattr(reconcile, n) for n in names}
        reconcile.STATE_DIR = d
        reconcile.DEPLOYS_LOG = os.path.join(d, "deploys.jsonl")
        reconcile.EVENTS_LOG = os.path.join(d, "events.jsonl")
        ok = {"verified": True, "error": None, "url": "https://x.vercel.app", "expected": "index-a.js", "live": "index-a.js"}
        try:
            with unittest.mock.patch.object(reconcile, "deploy_version", lambda *a, **k: dict(ok)), \
                    unittest.mock.patch.object(reconcile, "commit_info", lambda e, s: ("abc1234", "subj")):
                st = {"prod": {}, "previews": {}}
                reconcile.deploy_prod(entry, "c" * 40, st)
            rows = []
            if os.path.exists(reconcile.DEPLOYS_LOG):
                with open(reconcile.DEPLOYS_LOG, encoding="utf-8") as fh:
                    rows = [json.loads(line) for line in fh if line.strip()]
            return rows, st
        finally:
            for n, v in saved.items():
                setattr(reconcile, n, v)
            shutil.rmtree(d, ignore_errors=True)

    def test_deploys_log_row_written_only_when_enabled(self):
        base = {"name": "t", "checkout": "/nowhere", "scope": "S", "prod": {"project": "p", "url": "https://x"}}
        rows, st = self._prod_deploy({**base, "deploys_log": True})
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["target"], rows[0]["verified"]), ("prod", True))
        self.assertEqual(st["prod"]["sha"], "c" * 40)
        rows, st = self._prod_deploy(dict(base))
        self.assertEqual(rows, [])
        self.assertEqual(st["prod"]["sha"], "c" * 40)


class ConfigTests(unittest.TestCase):
    def test_config_parses_and_entries_are_well_formed(self):
        example = os.path.join(HERE, "deploy-hooks.example.json")
        with unittest.mock.patch.object(reconcile, "CONFIG_PATH", example):
            cfg = reconcile.load_config()
        names = [r["name"] for r in cfg["repos"]]
        self.assertEqual(names, ["app", "site"])
        by = {r["name"]: r for r in cfg["repos"]}
        self.assertEqual(reconcile.strategy_of(by["app"]), "remote")
        self.assertIs(by["app"].get("deploys_log"), True)
        self.assertEqual(by["app"]["branch"], "main")
        self.assertEqual(cfg["deploys_log_path"], "~/logs/deploy-hooks/deploys.jsonl")
        self.assertEqual(reconcile.strategy_of(by["site"]), "prebuilt")
        self.assertNotIn("deploys_log", by["site"])
        self.assertEqual(by["site"]["health"], {"path": "/api/health", "expect": {"ok": True, "storage": "kv"}})


class DeploysLogPathTests(unittest.TestCase):
    """Top-level deploys_log_path redirects prod deploy rows; HOME is a temp dir so ~ is safe."""

    NAMES = ("CONFIG_PATH", "STATE_DIR", "DEPLOYS_LOG", "EVENTS_LOG")

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="deploys-home-")
        self._saved = {n: getattr(reconcile, n) for n in self.NAMES}
        self._saved_home = os.environ.get("HOME")
        os.environ["HOME"] = self.home
        reconcile.STATE_DIR = os.path.join(self.home, "state")
        reconcile.EVENTS_LOG = os.path.join(reconcile.STATE_DIR, "events.jsonl")
        self.cfg_path = os.path.join(self.home, "deploy-hooks.local.json")

    def tearDown(self):
        for n, v in self._saved.items():
            setattr(reconcile, n, v)
        if self._saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._saved_home
        shutil.rmtree(self.home, ignore_errors=True)

    def _write_config(self, extra):
        repo = {"name": "app", "gh": "example-org/app", "checkout": "/home/user/app", "scope": "my-team",
                "deploys_log": True, "prod": {"project": "app", "url": "https://app-abc.vercel.app"},
                "preview": {"project": "app", "alias": "app-pr-{n}.vercel.app"}}
        with open(self.cfg_path, "w", encoding="utf-8") as fh:
            json.dump({**extra, "repos": [repo]}, fh)
        reconcile.CONFIG_PATH = self.cfg_path

    def _main_then_prod_deploy(self):
        # --repo with no match returns 2 right after the config is applied, before any repo runs.
        with unittest.mock.patch("sys.stderr", io.StringIO()):
            rc = reconcile.main(["--repo", "absent"])
        entry = reconcile.load_config()["repos"][0]
        ok = {"verified": True, "error": None, "url": "https://x.vercel.app", "expected": "index-a.js", "live": "index-a.js"}
        with unittest.mock.patch.object(reconcile, "deploy_version", lambda *a, **k: dict(ok)), \
                unittest.mock.patch.object(reconcile, "commit_info", lambda e, s: ("abc1234", "subj")):
            reconcile.deploy_prod(entry, "c" * 40, {"prod": {}, "previews": {}})
        return rc

    def test_top_level_key_redirects_prod_rows_with_tilde_expansion(self):
        self._write_config({"deploys_log_path": "~/logs/deploy-hooks/deploys.jsonl"})
        self.assertEqual(self._main_then_prod_deploy(), 2)
        redirected = os.path.join(self.home, "logs", "deploy-hooks", "deploys.jsonl")
        self.assertEqual(reconcile.DEPLOYS_LOG, redirected)
        with open(redirected, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["target"], rows[0]["verified"]), ("prod", True))
        self.assertFalse(os.path.exists(os.path.join(reconcile.STATE_DIR, "deploys.jsonl")))

    def test_default_deploys_log_path_when_key_absent(self):
        # Only main runs here: once DEPLOYS_LOG is the real default path, no deploy may append to it.
        self._write_config({})
        with unittest.mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(reconcile.main(["--repo", "absent"]), 2)
        self.assertEqual(reconcile.DEPLOYS_LOG, reconcile.DEFAULT_DEPLOYS_LOG)

    def test_resolve_expands_tilde_and_defaults(self):
        self.assertEqual(reconcile.resolve_deploys_log_path({"deploys_log_path": "~/x/d.jsonl"}),
                         os.path.join(self.home, "x", "d.jsonl"))
        self.assertEqual(reconcile.resolve_deploys_log_path({}), reconcile.DEFAULT_DEPLOYS_LOG)


class BranchTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="branch-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _git_init(self, name):
        path = os.path.join(self.dir, name)
        os.makedirs(path)
        subprocess.run(["git", "init", "-q", path], check=True, capture_output=True)
        return path

    def test_explicit_branch_key_wins_without_git(self):
        with unittest.mock.patch.object(reconcile, "run", side_effect=AssertionError("git must not run")):
            self.assertEqual(reconcile.branch_of({"branch": "trunk", "checkout": "/nonexistent/x"}), "trunk")

    def test_detects_default_branch_from_origin_head(self):
        checkout = self._git_init("detect")
        subprocess.run(["git", "-C", checkout, "symbolic-ref", "refs/remotes/origin/HEAD",
                        "refs/remotes/origin/develop"], check=True, capture_output=True)
        self.assertEqual(reconcile.branch_of({"checkout": checkout}), "develop")

    def test_falls_back_to_master_without_origin_head(self):
        self.assertEqual(reconcile.branch_of({"checkout": self._git_init("nohead")}), "master")

    def test_falls_back_to_master_when_checkout_missing(self):
        self.assertEqual(reconcile.branch_of({"checkout": os.path.join(self.dir, "missing")}), "master")


class GatherPrListTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gather-")
        self._saved_state = reconcile.STATE_DIR
        reconcile.STATE_DIR = os.path.join(self.dir, "state")
        self.entry = {"name": "t", "gh": "example-org/app", "checkout": "/home/user/app", "branch": "main"}

    def tearDown(self):
        reconcile.STATE_DIR = self._saved_state
        shutil.rmtree(self.dir, ignore_errors=True)

    def _gather(self, prs_json):
        calls = {"git": [], "sh": []}

        def fake_git(entry, args, timeout=None):
            calls["git"].append(args)
            return ("a" * 40 + "\n") if args[0] == "rev-parse" else ""

        def fake_sh(name, args, cwd, timeout, env=None):
            calls["sh"].append(args)
            return prs_json

        with unittest.mock.patch.object(reconcile, "git", fake_git), \
                unittest.mock.patch.object(reconcile, "sh", fake_sh):
            return reconcile.gather(self.entry), calls

    def test_gather_fetches_configured_branch(self):
        (master, _prs, _trunc), calls = self._gather("[]")
        self.assertIn(["rev-parse", "origin/main"], calls["git"])
        self.assertEqual(master, "a" * 40)

    def test_pr_list_limit_is_1000(self):
        _result, calls = self._gather("[]")
        gh = [a for a in calls["sh"] if a[0] == "gh"][0]
        self.assertEqual(gh[gh.index("--limit") + 1], "1000")

    def test_full_page_is_truncated_and_plans_no_cleanup(self):
        prs = [{"number": i, "headRefOid": "b" * 40, "isCrossRepository": False} for i in range(1, 1001)]
        (master, open_prs, truncated), _calls = self._gather(json.dumps(prs))
        self.assertTrue(truncated)
        st = {"prod": {"sha": master}, "previews": {"5000": {"sha": "c" * 40}}}
        self.assertIn("cleanup", [a["kind"] for a in reconcile.plan(st, master, open_prs, cleanup=True)])
        self.assertNotIn("cleanup", [a["kind"] for a in reconcile.plan(st, master, open_prs, cleanup=False)])

    def test_short_list_is_not_truncated(self):
        prs = [{"number": 3, "headRefOid": "b" * 40, "isCrossRepository": False}]
        (_master, open_prs, truncated), _calls = self._gather(json.dumps(prs))
        self.assertFalse(truncated)
        self.assertEqual(len(open_prs), 1)


class LockRaceTests(unittest.TestCase):
    """Queued-rerun interleavings on real flock files in a temp STATE_DIR, with run_pass mocked."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="race-")
        self._saved_state = reconcile.STATE_DIR
        reconcile.STATE_DIR = self.dir
        self.entry = {"name": "t"}
        self.rerun = os.path.join(self.dir, "t.rerun")
        self.lockpath = os.path.join(self.dir, "t.lock")
        self.passes = 0

    def tearDown(self):
        reconcile.STATE_DIR = self._saved_state
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_trigger_refused_while_pass_runs_is_rerun_after_release(self):
        started, proceed = threading.Event(), threading.Event()

        def fake_run_pass(entry, force_prod, retry_failed=False):
            self.passes += 1
            if self.passes == 1:
                started.set()
                proceed.wait(10)

        with unittest.mock.patch.object(reconcile, "run_pass", fake_run_pass), \
                unittest.mock.patch.object(reconcile.time, "sleep", lambda s: None):
            runner = threading.Thread(target=reconcile.locked_pass, args=(self.entry, False))
            runner.start()
            self.assertTrue(started.wait(10))
            reconcile.locked_pass(self.entry, False)  # refused by the running pass: queues a rerun
            self.assertTrue(os.path.exists(self.rerun))
            proceed.set()
            runner.join(10)
        self.assertFalse(runner.is_alive())
        self.assertEqual(self.passes, 2)
        self.assertFalse(os.path.exists(self.rerun))

    def test_refused_trigger_with_held_lock_only_leaves_the_marker(self):
        holder = os.open(self.lockpath, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(holder, fcntl.LOCK_EX)
        try:
            with unittest.mock.patch.object(reconcile, "run_pass", lambda *a, **k: self._count()), \
                    unittest.mock.patch.object(reconcile.time, "sleep", lambda s: None):
                reconcile.locked_pass(self.entry, False)
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            os.close(holder)
        self.assertEqual(self.passes, 0)
        self.assertTrue(os.path.exists(self.rerun))

    def _count(self):
        self.passes += 1

    def test_reruns_are_bounded(self):
        def fake_run_pass(entry, force_prod, retry_failed=False):
            self.passes += 1
            reconcile.touch(self.rerun)  # a trigger lands during every pass

        with unittest.mock.patch.object(reconcile, "run_pass", fake_run_pass), \
                unittest.mock.patch.object(reconcile.time, "sleep", lambda s: None):
            reconcile.locked_pass(self.entry, False)
        self.assertEqual(self.passes, 1 + reconcile.MAX_EXTRA_PASSES)
        self.assertTrue(os.path.exists(self.rerun))

    def test_lost_relock_hands_the_pass_to_the_new_holder(self):
        real_flock = fcntl.flock
        grabbed = []

        def flock(fd, op):
            real_flock(fd, op)
            if op == fcntl.LOCK_UN and not grabbed:
                # Another process takes the lock right after the release; our re-take must fail.
                other = os.open(self.lockpath, os.O_CREAT | os.O_RDWR, 0o644)
                real_flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                grabbed.append(other)

        def fake_run_pass(entry, force_prod, retry_failed=False):
            self.passes += 1
            reconcile.touch(self.rerun)

        try:
            with unittest.mock.patch.object(reconcile.fcntl, "flock", flock), \
                    unittest.mock.patch.object(reconcile, "run_pass", fake_run_pass), \
                    unittest.mock.patch.object(reconcile.time, "sleep", lambda s: None):
                reconcile.locked_pass(self.entry, False)
        finally:
            for fd in grabbed:
                real_flock(fd, fcntl.LOCK_UN)
                os.close(fd)
        self.assertEqual(self.passes, 1)
        self.assertTrue(os.path.exists(self.rerun))


class GuardBaselineTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="guard-")
        self.pre = os.path.join(self.dir, "preexisting")
        self.new = os.path.join(self.dir, "appears")
        open(self.pre, "w").close()
        self._saved = {n: getattr(reconcile, n) for n in ("GUARD_PATHS", "GUARD_BASELINE", "STATE_DIR")}
        reconcile.GUARD_PATHS = (self.pre, self.new)
        reconcile.STATE_DIR = os.path.join(self.dir, "state")

    def tearDown(self):
        for n, v in self._saved.items():
            setattr(reconcile, n, v)
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_preexisting_guard_path_is_a_warning_not_fatal(self):
        reconcile.record_guard_baseline("t")
        self.assertIsNone(reconcile.guard_home())
        with open(os.path.join(reconcile.STATE_DIR, "t.log"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("warning", text)
        self.assertIn(self.pre, text)

    def test_guard_path_appearing_during_the_pass_is_fatal(self):
        reconcile.record_guard_baseline("t")
        open(self.new, "w").close()
        err = reconcile.guard_home()
        self.assertIsNotNone(err)
        self.assertTrue(err.startswith("FATAL"))
        self.assertIn(self.new, err)
        self.assertNotIn(self.pre, err)

    def test_no_guard_path_means_no_error(self):
        os.unlink(self.pre)
        reconcile.record_guard_baseline("t")
        self.assertIsNone(reconcile.guard_home())


class PruneSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="prune-")
        self.cache = os.path.join(self.dir, "cache")
        os.makedirs(self.cache)
        self._saved_cache = reconcile.CACHE_DIR
        reconcile.CACHE_DIR = self.cache

    def tearDown(self):
        reconcile.CACHE_DIR = self._saved_cache
        shutil.rmtree(self.dir, ignore_errors=True)

    def _snap(self, prefix, mtime):
        path = tempfile.mkdtemp(prefix=prefix, dir=self.cache)
        os.utime(path, (mtime, mtime))
        return path

    def test_keeps_only_the_newest_failed_snapshot_of_the_repo(self):
        old_pr = self._snap("app-pr-", 1000)
        old_prod = self._snap("app-prod-", 2000)
        newest = self._snap("app-pr-", 3000)
        other = self._snap("site-pr-", 500)
        removed = reconcile.prune_failed_snapshots("app")
        self.assertEqual(sorted(removed), sorted([old_pr, old_prod]))
        self.assertTrue(os.path.isdir(newest))
        self.assertTrue(os.path.isdir(other))
        self.assertFalse(os.path.exists(old_pr))
        self.assertFalse(os.path.exists(old_prod))

    def test_symlinks_out_of_the_cache_are_never_followed(self):
        outside = os.path.join(self.dir, "outside")
        os.makedirs(outside)
        link = os.path.join(self.cache, "app-pr-evil")
        os.symlink(outside, link)
        self._snap("app-pr-", 1000)
        self._snap("app-pr-", 3000)
        removed = reconcile.prune_failed_snapshots("app")
        self.assertEqual(len(removed), 1)
        self.assertTrue(os.path.isdir(outside))
        self.assertTrue(os.path.islink(link))


class RemoteBuildEnvTests(unittest.TestCase):
    def test_remote_local_build_gets_build_env_and_pinned_sha(self):
        root = tempfile.mkdtemp(prefix="remote-")
        self.addCleanup(shutil.rmtree, root, True)
        os.makedirs(os.path.join(root, "snap"))
        nm = os.path.join(root, "nm")
        os.makedirs(nm)
        entry = {"name": "t", "scope": "my-team", "node_modules": [nm],
                 "build": {"env": {"KV_PUBLIC_BASE": "https://kv.example"}}}
        sha = "e" * 40
        seen = {}

        def fake_sh(name, args, cwd, timeout, env=None):
            seen["env"] = env
            os.makedirs(os.path.join(cwd, "dist", "assets"))
            with open(os.path.join(cwd, "dist", "assets", "index-abc123.js"), "w", encoding="utf-8") as fh:
                fh.write("x")
            return ""

        def fake_vercel(name, args, cwd, timeout):
            return '{"deployment": {"url": "t-abc.vercel.app"}}'

        res = {"expected": None, "url": None}
        with unittest.mock.patch.object(reconcile, "sh", fake_sh), \
                unittest.mock.patch.object(reconcile, "vercel", fake_vercel):
            reconcile.build_remote(entry, root, sha, False, "proj", res)
        self.assertEqual(seen["env"]["KV_PUBLIC_BASE"], "https://kv.example")
        self.assertEqual(seen["env"]["VERCEL_GIT_COMMIT_SHA"], sha)
        self.assertEqual(res["expected"], "index-abc123.js")
        self.assertEqual(res["url"], "https://t-abc.vercel.app")


class TriggerSafetyTests(unittest.TestCase):
    PUSH = json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Bash",
                       "tool_input": {"command": "git push origin main"}})
    SESSION = json.dumps({"hook_event_name": "SessionStart"})

    def test_kill_switch_spawns_nothing_and_exits_zero(self):
        with unittest.mock.patch.dict(os.environ, {"DEPLOY_HOOKS": "0"}), \
                unittest.mock.patch.object(trigger, "spawn") as spawn, \
                unittest.mock.patch.object(trigger.subprocess, "Popen") as popen, \
                unittest.mock.patch("sys.stdin", io.StringIO(self.PUSH)):
            rc = trigger.main()
        self.assertEqual(rc, 0)
        spawn.assert_not_called()
        popen.assert_not_called()

    def test_kill_switch_as_a_real_process_exits_zero_silently(self):
        env = dict(os.environ, DEPLOY_HOOKS="0")
        p = subprocess.run([sys.executable, "-I", TRIGGER_PATH], input=self.PUSH, env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout, "")

    def test_malformed_stdin_exits_zero_without_spawning(self):
        with unittest.mock.patch.dict(os.environ, {"DEPLOY_HOOKS": "1"}), \
                unittest.mock.patch.object(trigger, "spawn") as spawn, \
                unittest.mock.patch("sys.stdin", io.StringIO("{not json")):
            rc = trigger.main()
        self.assertEqual(rc, 0)
        spawn.assert_not_called()

    def test_malformed_stdin_as_a_real_process_exits_zero(self):
        # Malformed JSON fails before any spawn, so this real run starts no reconcile pass.
        env = {k: v for k, v in os.environ.items() if k != "DEPLOY_HOOKS"}
        p = subprocess.run([sys.executable, "-I", TRIGGER_PATH], input="{not json", env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0)

    def test_spawn_exception_on_push_still_exits_zero(self):
        with unittest.mock.patch.dict(os.environ, {"DEPLOY_HOOKS": "1"}), \
                unittest.mock.patch.object(trigger, "spawn", side_effect=OSError("boom")), \
                unittest.mock.patch("sys.stdin", io.StringIO(self.PUSH)), \
                unittest.mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(trigger.main(), 0)

    def test_spawn_exception_on_session_start_still_exits_zero(self):
        with unittest.mock.patch.dict(os.environ, {"DEPLOY_HOOKS": "1"}), \
                unittest.mock.patch.object(trigger, "spawn", side_effect=OSError("boom")), \
                unittest.mock.patch("sys.stdin", io.StringIO(self.SESSION)):
            self.assertEqual(trigger.main(), 0)


if __name__ == "__main__":
    unittest.main()
