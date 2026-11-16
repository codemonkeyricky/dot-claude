"""Parity between the two copies of the harness.local.json exempt_env loader.

main-readonly.py and task-gate.py each carry their own load_exempt_env() and env_exempt(),
because hooks run under `python3 -I` and cannot import a shared module. This test loads both
hook files from this repo and checks that, for every case below, they load the same mapping
and make the same exemption decision, and that both match the expected result.

Each case writes its own config file and loads both hooks fresh, since each hook reads
CLAUDE_HARNESS_CONFIG once, at import time.
"""
import importlib.util
import json
import os
import shutil
import tempfile
import unittest

HOOKS = os.path.dirname(os.path.abspath(__file__))
HOOK_FILES = {"main-readonly": "main-readonly.py", "task-gate": "task-gate.py"}
VAR = "HARNESS_PARITY_ROLE"
VALID = json.dumps({"exempt_env": {VAR: ["bot", "bot-*"]}})
MISSING = None  # no file is written for this case

# (name, config text or MISSING, value of VAR in the environment, expected loader result, expected exempt)
CASES = [
    ("valid config, glob match", VALID, "bot-minion", {VAR: ["bot", "bot-*"]}, True),
    ("valid config, exact match", VALID, "bot", {VAR: ["bot", "bot-*"]}, True),
    ("valid config, glob is case-sensitive", VALID, "Bot-minion", {VAR: ["bot", "bot-*"]}, False),
    ("valid config, no match", VALID, "human", {VAR: ["bot", "bot-*"]}, False),
    ("missing file", MISSING, "bot-minion", {}, False),
    ("invalid JSON", "{not json", "bot-minion", {}, False),
    ("top-level value is not an object", '["exempt_env"]', "bot", {}, False),
    ("exempt_env missing", "{}", "bot", {}, False),
    ("exempt_env is a list", json.dumps({"exempt_env": ["bot"]}), "bot", {}, False),
    ("pattern list holds a non-string", json.dumps({"exempt_env": {VAR: ["bot-*", 3]}}), "bot-1", {}, False),
    ("pattern is not a list", json.dumps({"exempt_env": {VAR: "bot*"}}), "bot-1", {}, False),
    ("empty var name is skipped", json.dumps({"exempt_env": {"": ["*"]}}), "x", {}, False),
    ("empty var value never exempts", json.dumps({"exempt_env": {VAR: ["*"]}}), "", {VAR: ["*"]}, False),
    ("broad glob matches any non-empty value", json.dumps({"exempt_env": {VAR: ["*"]}}), "anything",
     {VAR: ["*"]}, True),
]


def load_hook(name, filename):
    path = os.path.join(HOOKS, filename)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class HarnessConfigParityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="harness-parity-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._saved = {k: os.environ.get(k) for k in ("CLAUDE_HARNESS_CONFIG", VAR)}
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _load_both(self, case_no, config_text):
        path = os.path.join(self.tmp, "harness.local.json")
        if config_text is MISSING:
            path = os.path.join(self.tmp, "missing.json")  # never created
        else:
            with open(path, "w", encoding="utf-8") as f:
                f.write(config_text)
        os.environ["CLAUDE_HARNESS_CONFIG"] = path
        return {
            stem: load_hook("%s_case%d" % (stem.replace("-", "_"), case_no), filename)
            for stem, filename in HOOK_FILES.items()
        }

    def test_loaders_agree_on_every_case(self):
        for case_no, (name, config_text, value, expected_load, expected_exempt) in enumerate(CASES):
            with self.subTest(case=name):
                os.environ[VAR] = value
                mods = self._load_both(case_no, config_text)
                loads = {stem: m.load_exempt_env() for stem, m in mods.items()}
                exempts = {stem: m.env_exempt() for stem, m in mods.items()}
                self.assertEqual(loads["main-readonly"], loads["task-gate"],
                                 "load_exempt_env differs between the two hooks")
                self.assertEqual(exempts["main-readonly"], exempts["task-gate"],
                                 "env_exempt differs between the two hooks")
                self.assertEqual(loads["main-readonly"], expected_load)
                self.assertEqual(exempts["main-readonly"], expected_exempt)


if __name__ == "__main__":
    unittest.main()
