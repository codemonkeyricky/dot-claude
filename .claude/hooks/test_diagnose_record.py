import json, os, re, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

HOOK = str(Path(__file__).resolve().parent / "diagnose-record.py")  # the worktree copy
TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def good_spec(verdict="DIAGNOSIS-VERDICT: SPEC"):
    return "\n".join([
        "## Root cause",
        "The parser drops the last token.",
        "",
        "## Fix spec",
        "Keep the final token in parse().",
        "",
        "## Checks",
        "- pytest -q tests/test_parser.py",
        "",
        verdict,
    ])


def assistant_text(text):
    return {"type": "assistant", "message": {"role": "assistant",
                                             "content": [{"type": "text", "text": text}]}}


def code_block(n):
    return "```python\n" + "\n".join("x = %d" % i for i in range(n)) + "\n```"


class T(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.log = os.path.join(self.root, "diagnose-record.log")
        self.diagnoses = os.path.join(self.root, "state", "diagnoses.jsonl")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _run(self, raw):
        """Run the hook with raw stdin (str or bytes). Returns stdout text; asserts exit 0."""
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        e = dict(os.environ)
        e["DIAGNOSE_RECORD_LOG"] = self.log
        e["DIAGNOSES_LOG"] = self.diagnoses
        r = subprocess.run([sys.executable, "-I", HOOK], input=raw, capture_output=True, env=e)
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
        return r.stdout.decode("utf-8")

    def hook(self, **fields):
        p = {"hook_event_name": "SubagentStop", "agent_type": "diagnoser-1",
             "agent_id": "agent-1", "session_id": "sess-1", "stop_hook_active": False}
        p.update(fields)
        return self._run(json.dumps(p))

    def records(self):
        if not os.path.exists(self.diagnoses):
            return []
        with open(self.diagnoses, encoding="utf-8") as f:
            return [json.loads(x) for x in f.read().splitlines()]

    def log_lines(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as f:
            return [json.loads(x) for x in f.read().splitlines()]

    def block_reason(self, out):
        decision = json.loads(out)
        self.assertEqual(decision["decision"], "block")
        return decision["reason"]

    # non-diagnoser is ignored, even with a bad report
    def test_01_non_diagnoser_ignored(self):
        out = self.hook(agent_type="pr-reviewer-2", last_assistant_message="no headings at all")
        self.assertEqual(out, "")
        self.assertEqual(self.records(), [])
        self.assertEqual(self.log_lines()[-1]["outcome"], "ignored")

    def test_01b_missing_agent_type_ignored(self):
        p = {"hook_event_name": "SubagentStop", "last_assistant_message": "no headings"}
        self.assertEqual(self._run(json.dumps(p)), "")
        self.assertEqual(self.records(), [])
        self.assertFalse(os.path.exists(self.log) and any(
            x["outcome"] != "ignored" for x in self.log_lines()))

    # good spec passes and logs one line
    def test_02_good_spec_passes_and_logs(self):
        out = self.hook(last_assistant_message=good_spec())
        self.assertEqual(out, "")
        recs = self.records()
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual(set(rec), {"kind", "ts", "session_id", "agent_id", "agent_type",
                                    "verdict", "chars"})
        self.assertEqual(rec["kind"], "diagnosis-spec")
        self.assertRegex(rec["ts"], TS)
        self.assertEqual(rec["session_id"], "sess-1")
        self.assertEqual(rec["agent_id"], "agent-1")
        self.assertEqual(rec["agent_type"], "diagnoser-1")
        self.assertEqual(rec["verdict"], "SPEC")
        self.assertEqual(rec["chars"], len(good_spec()))
        self.assertEqual(self.log_lines()[-1]["outcome"], "recorded")

    def test_02b_needs_user_verdict_logged(self):
        out = self.hook(last_assistant_message=good_spec("DIAGNOSIS-VERDICT: NEEDS-USER"))
        self.assertEqual(out, "")
        self.assertEqual(self.records()[0]["verdict"], "NEEDS-USER")

    def test_02c_transcript_fallback(self):
        path = os.path.join(self.root, "agent.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "message": {"role": "user", "content": "diagnose"}}) + "\n")
            f.write(json.dumps(assistant_text(good_spec())) + "\n")
        self.assertEqual(self.hook(agent_transcript_path=path), "")
        self.assertEqual(len(self.records()), 1)

    # missing heading blocks
    def test_03_missing_heading_blocks(self):
        text = good_spec().replace("## Checks", "## Check list")
        reason = self.block_reason(self.hook(last_assistant_message=text))
        self.assertIn("## Checks", reason)
        self.assertIn("## Root cause", reason)  # restates the required format
        self.assertIn("DIAGNOSIS-VERDICT: SPEC", reason)
        self.assertEqual(self.records(), [])
        self.assertEqual(self.log_lines()[-1]["outcome"], "blocked")

    def test_03b_heading_must_be_its_own_line(self):
        text = good_spec().replace("## Fix spec", "Text ## Fix spec")
        reason = self.block_reason(self.hook(last_assistant_message=text))
        self.assertIn("## Fix spec", reason)

    # bad last line blocks
    def test_04_bad_last_line_blocks(self):
        text = good_spec() + "\nThanks for reading."
        reason = self.block_reason(self.hook(last_assistant_message=text))
        self.assertIn("last non-empty line", reason)
        self.assertEqual(self.records(), [])

    def test_04b_verdict_not_last_blocks(self):
        text = good_spec() + "\nDIAGNOSIS-VERDICT: SPEC extra"
        self.block_reason(self.hook(last_assistant_message=text))
        self.assertEqual(self.records(), [])

    # > 8000 chars blocks
    def test_05_over_8000_chars_blocks(self):
        text = good_spec().replace("The parser drops the last token.",
                                   "The parser drops the last token. " + "x" * 8000)
        self.assertGreater(len(text), 8000)
        reason = self.block_reason(self.hook(last_assistant_message=text))
        self.assertIn("8000", reason)
        self.assertEqual(self.records(), [])

    def test_05b_exactly_8000_chars_passes(self):
        base = good_spec()
        text = base.replace("The parser drops the last token.",
                            "The parser drops the last token." + " " * (8000 - len(base)))
        self.assertEqual(len(text), 8000)
        self.assertEqual(self.hook(last_assistant_message=text), "")
        self.assertEqual(len(self.records()), 1)

    # 25-line code block blocks
    def test_06_long_code_block_blocks(self):
        text = good_spec().replace("Keep the final token in parse().",
                                   "Keep the final token in parse().\n" + code_block(25))
        reason = self.block_reason(self.hook(last_assistant_message=text))
        self.assertIn("25 lines", reason)
        self.assertEqual(self.records(), [])

    def test_06b_twenty_line_code_block_passes(self):
        text = good_spec().replace("Keep the final token in parse().",
                                   "Keep the final token in parse().\n" + code_block(20))
        self.assertEqual(self.hook(last_assistant_message=text), "")
        self.assertEqual(len(self.records()), 1)

    # every failed rule is listed
    def test_07_all_failures_listed(self):
        text = "## Root cause\nno other headings\n" + "x" * 8000 + "\n" + code_block(25) + "\nDone."
        reason = self.block_reason(self.hook(last_assistant_message=text))
        self.assertIn("## Fix spec", reason)
        self.assertIn("## Checks", reason)
        self.assertIn("last non-empty line", reason)
        self.assertIn("8000", reason)
        self.assertIn("25 lines", reason)

    # second stop after a block is allowed
    def test_08_second_stop_after_block_allowed(self):
        bad = good_spec().replace("## Checks", "## Check list")
        self.block_reason(self.hook(last_assistant_message=bad))
        self.assertEqual(self.hook(last_assistant_message=bad, stop_hook_active=True), "")
        self.assertEqual(self.records(), [])
        self.assertEqual(self.log_lines()[-1]["outcome"], "allowed")

    def test_09_garbage_stdin_fails_open(self):
        for raw in [b"not json", b"", b"\xff\xfe\x00bad", b"[1, 2]"]:
            self.assertEqual(self._run(raw), "", repr(raw))
        self.assertEqual(self.records(), [])
        self.assertEqual(self.log_lines()[-1]["outcome"], "none")

    def test_10_log_has_keys_not_message_text(self):
        marker = "SECRET-BODY-MARKER-4711"
        self.hook(last_assistant_message=good_spec().replace("parser", marker))
        with open(self.log, encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn(marker, raw)
        line = self.log_lines()[-1]
        self.assertIn("last_assistant_message", line["keys"])
        self.assertEqual(line["keys"], sorted(line["keys"]))


if __name__ == "__main__":
    unittest.main()
