import json, os, re, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

HOME = os.path.expanduser("~")
HOOK = str(Path(__file__).resolve().parent / "review-record.py")  # the worktree copy
SHA = "ABCDEF0123456789abcdef0123456789ABCDEF01"  # 40 hex, mixed case
SHA_LOWER = SHA.lower()
TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def verdict_text(verdict="APPROVE", repo="Acme/Widget", pr=42, sha=SHA):
    return "Looks fine.\nREVIEW-VERDICT: %s %s#%d@%s" % (verdict, repo, pr, sha)


def assistant_text(text):
    return {"type": "assistant", "message": {"role": "assistant",
                                             "content": [{"type": "text", "text": text}]}}


class T(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.log = os.path.join(self.root, "review-record.log")
        self.prlog = os.path.join(self.root, "state", "pr-reviews.jsonl")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _run(self, raw):
        """Run the hook with raw stdin (str or bytes). Returns stdout text; asserts exit 0."""
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        e = dict(os.environ)
        e["REVIEW_RECORD_LOG"] = self.log
        e["PR_REVIEWS_LOG"] = self.prlog
        r = subprocess.run([sys.executable, "-I", HOOK], input=raw, capture_output=True, env=e)
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
        return r.stdout.decode("utf-8")

    def hook(self, **fields):
        p = {"hook_event_name": "SubagentStop", "agent_type": "pr-reviewer-2",
             "agent_id": "agent-1", "session_id": "sess-1", "stop_hook_active": False}
        p.update(fields)
        return self._run(json.dumps(p))

    def records(self):
        if not os.path.exists(self.prlog):
            return []
        with open(self.prlog, encoding="utf-8") as f:
            return [json.loads(x) for x in f.read().splitlines()]

    def log_lines(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as f:
            return [json.loads(x) for x in f.read().splitlines()]

    def transcript(self, entries):
        path = os.path.join(self.root, "agent.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
        return path

    # 1
    def test_01_approve_via_last_assistant_message(self):
        out = self.hook(last_assistant_message=verdict_text("APPROVE"))
        self.assertEqual(out, "")
        recs = self.records()
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual(rec["repo"], "acme/widget")
        self.assertIs(type(rec["pr"]), int)
        self.assertEqual(rec["pr"], 42)
        self.assertEqual(rec["sha"], SHA_LOWER)
        self.assertEqual(rec["verdict"], "APPROVE")
        self.assertEqual(rec["agent_type"], "pr-reviewer-2")
        self.assertEqual(rec["agent_id"], "agent-1")
        self.assertEqual(rec["session_id"], "sess-1")
        self.assertRegex(rec["ts"], TS)
        self.assertEqual(set(rec), {"ts", "repo", "pr", "sha", "verdict", "agent_type",
                                    "agent_id", "session_id"})

    # 2
    def test_02_approve_via_transcript_fallback(self):
        path = self.transcript([
            {"type": "user", "message": {"role": "user", "content": "review PR 42"}},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "gh pr view"}}]}},
            assistant_text(verdict_text("APPROVE")),
        ])
        out = self.hook(agent_transcript_path=path)
        self.assertEqual(out, "")
        recs = self.records()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["verdict"], "APPROVE")
        self.assertEqual(recs[0]["repo"], "acme/widget")
        self.assertEqual(recs[0]["sha"], SHA_LOWER)

    def test_02b_transcript_plain_string_content_counts_as_text(self):
        path = self.transcript([
            {"type": "assistant", "message": {"role": "assistant", "content": verdict_text("CHANGES")}},
        ])
        self.assertEqual(self.hook(agent_transcript_path=path), "")
        self.assertEqual(self.records()[0]["verdict"], "CHANGES")

    # 3
    def test_03_changes_recorded_as_changes(self):
        self.assertEqual(self.hook(last_assistant_message=verdict_text("CHANGES", pr=7)), "")
        recs = self.records()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["verdict"], "CHANGES")
        self.assertEqual(recs[0]["pr"], 7)

    # 4
    def test_04_non_reviewer_ignored(self):
        out = self.hook(agent_type="kreeva-osh", last_assistant_message=verdict_text("APPROVE"))
        self.assertEqual(out, "")
        self.assertEqual(self.records(), [])
        self.assertEqual(self.log_lines()[-1]["outcome"], "ignored")

    def test_04b_missing_agent_type_ignored(self):
        p = {"hook_event_name": "SubagentStop", "last_assistant_message": verdict_text("APPROVE")}
        self.assertEqual(self._run(json.dumps(p)), "")
        self.assertEqual(self.records(), [])

    # 5
    def test_05_opus_reviewer_and_pr_reviewer_both_accepted(self):
        self.assertEqual(self.hook(agent_type="opus-reviewer", last_assistant_message=verdict_text("APPROVE", pr=1)), "")
        self.assertEqual(self.hook(agent_type="pr-reviewer-2", last_assistant_message=verdict_text("APPROVE", pr=2)), "")
        recs = self.records()
        self.assertEqual([r["agent_type"] for r in recs], ["opus-reviewer", "pr-reviewer-2"])
        self.assertEqual([r["pr"] for r in recs], [1, 2])

    # 6
    def test_06_last_verdict_line_wins(self):
        text = verdict_text("APPROVE", pr=10) + "\nREVIEW-VERDICT: CHANGES acme/widget#11@" + SHA
        self.assertEqual(self.hook(last_assistant_message=text), "")
        recs = self.records()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["verdict"], "CHANGES")
        self.assertEqual(recs[0]["pr"], 11)

    # 7
    def test_07_short_sha_blocks(self):
        short = SHA[:39]
        out = self.hook(last_assistant_message=verdict_text("APPROVE", sha=short))
        decision = json.loads(out)
        self.assertEqual(decision["decision"], "block")
        self.assertIn("REVIEW-VERDICT: APPROVE|CHANGES", decision["reason"])
        self.assertEqual(self.records(), [])

    # 8
    def test_08_short_sha_with_stop_hook_active_is_silent(self):
        out = self.hook(stop_hook_active=True, last_assistant_message=verdict_text("APPROVE", sha=SHA[:39]))
        self.assertEqual(out, "")
        self.assertEqual(self.records(), [])

    # 9
    def test_09_abstain_records_nothing(self):
        out = self.hook(last_assistant_message="Could not fetch the diff.\nREVIEW-VERDICT: ABSTAIN gh auth failed")
        self.assertEqual(out, "")
        self.assertEqual(self.records(), [])
        self.assertEqual(self.log_lines()[-1]["outcome"], "abstain")

    def test_09b_abstain_after_approve_wins(self):
        text = verdict_text("APPROVE") + "\nREVIEW-VERDICT: ABSTAIN changed my mind"
        self.assertEqual(self.hook(last_assistant_message=text), "")
        self.assertEqual(self.records(), [])

    # 10
    def test_10_garbage_stdin_fails_open(self):
        for raw in [b"not json", b"", b"\xff\xfe\x00bad", b"[1, 2]"]:
            self.assertEqual(self._run(raw), "", repr(raw))
        self.assertEqual(self.records(), [])

    def test_10b_utf8_bom_payload_accepted(self):
        p = {"hook_event_name": "SubagentStop", "agent_type": "pr-reviewer-2",
             "last_assistant_message": verdict_text("APPROVE")}
        self.assertEqual(self._run(b"\xef\xbb\xbf" + json.dumps(p).encode("utf-8")), "")
        self.assertEqual(len(self.records()), 1)

    # 11
    def test_11_log_has_keys_not_message_text(self):
        marker = "SECRET-BODY-MARKER-4711"
        self.hook(last_assistant_message=marker + "\n" + verdict_text("APPROVE"))
        with open(self.log, encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn(marker, raw)
        self.assertNotIn(SHA, raw)
        line = self.log_lines()[-1]
        self.assertIn("last_assistant_message", line["keys"])
        self.assertEqual(line["keys"], sorted(line["keys"]))
        self.assertEqual(line["agent_type"], "pr-reviewer-2")
        self.assertEqual(line["hook_event_name"], "SubagentStop")
        self.assertEqual(line["outcome"], "recorded")
        self.assertRegex(line["ts"], TS)

    def test_11b_log_written_on_garbage_input(self):
        self._run(b"not json")
        line = self.log_lines()[-1]
        self.assertEqual(line["keys"], [])
        self.assertEqual(line["outcome"], "none")

    def test_11c_no_verdict_blocks_and_logs(self):
        out = self.hook(last_assistant_message="I looked at it, no verdict")
        self.assertEqual(json.loads(out)["decision"], "block")
        self.assertEqual(self.log_lines()[-1]["outcome"], "blocked")


if __name__ == "__main__":
    unittest.main()
