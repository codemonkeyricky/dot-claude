"""Unit tests for deploy-trigger.py and deploy-reconcile.py (pure parts only, no network).

Run from the repo's .claude/hooks directory: python3 -m unittest test_deploy_hooks -v
"""

import fcntl
import importlib.util
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import unittest.mock

HERE = os.path.dirname(os.path.abspath(__file__))


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
        self.assertEqual(reconcile.strategy_of(by["site"]), "prebuilt")
        self.assertNotIn("deploys_log", by["site"])
        self.assertEqual(by["site"]["health"], {"path": "/api/health", "expect": {"ok": True, "storage": "kv"}})


if __name__ == "__main__":
    unittest.main()
