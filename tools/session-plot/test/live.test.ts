import { test } from "node:test";
import assert from "node:assert/strict";
import {
  appendFileSync,
  closeSync,
  mkdirSync,
  mkdtempSync,
  openSync,
  rmSync,
  statSync,
  unlinkSync,
  utimesSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { LiveIndex } from "../src/live";
import { turnCost } from "../src/cost";

const SID = "aaaaaaaa-0000-0000-0000-000000000001";
const PROJ = "proj-a";
const DAY = 24 * 3_600_000;
const MODEL = "claude-opus-5-5";

function usageOf(input: number) {
  return { input_tokens: input, output_tokens: 10, cache_creation_input_tokens: 0, cache_read_input_tokens: 0 };
}

function usageLine(msgId: string, input: number, ts: string = new Date().toISOString()): string {
  return (
    JSON.stringify({
      type: "assistant",
      timestamp: ts,
      uuid: `uuid-${msgId}`,
      message: {
        id: msgId,
        model: MODEL,
        usage: usageOf(input),
      },
    }) + "\n"
  );
}

function userLine(text: string, ts: string = new Date().toISOString()): string {
  return JSON.stringify({ type: "user", timestamp: ts, message: { role: "user", content: text } }) + "\n";
}

function customTitleLine(title: string): string {
  return JSON.stringify({ type: "custom-title", customTitle: title, sessionId: SID }) + "\n";
}

// A temp projects root with one main transcript for SID. The caller removes `root` when done.
function fixture(): { root: string; file: string; dir: string } {
  const root = mkdtempSync(join(tmpdir(), "sp-live-"));
  const dir = join(root, PROJ);
  mkdirSync(dir, { recursive: true });
  return { root, dir, file: join(dir, `${SID}.jsonl`) };
}

function sessionOf(idx: LiveIndex) {
  const snap = idx.refreshNow();
  const s = snap.sessions.find((x) => x.id === SID);
  assert.ok(s, "session should be in the snapshot");
  return s;
}

test("a partial last line is not consumed until it is completed", () => {
  const { root, file } = fixture();
  const idx = new LiveIndex(root);
  try {
    const l1 = usageLine("m1", 100);
    const l2 = usageLine("m2", 200);
    writeFileSync(file, l1 + l2.slice(0, 25));
    assert.equal(sessionOf(idx).mainTurns, 1);

    appendFileSync(file, l2.slice(25));
    assert.equal(sessionOf(idx).mainTurns, 2);
  } finally {
    idx.close();
    rmSync(root, { recursive: true, force: true });
  }
});

test("a duplicate message.id is counted once, with the last usage", () => {
  const { root, file } = fixture();
  const idx = new LiveIndex(root);
  try {
    writeFileSync(file, usageLine("dup", 100) + usageLine("dup", 300));
    const s = sessionOf(idx);
    assert.equal(s.mainTurns, 1);
    assert.equal(s.mainCtx, 300);
  } finally {
    idx.close();
    rmSync(root, { recursive: true, force: true });
  }
});

test("truncation (size smaller than the consumed offset) resets the file", () => {
  const { root, file } = fixture();
  const idx = new LiveIndex(root);
  try {
    writeFileSync(file, usageLine("a", 100) + usageLine("b", 200));
    assert.equal(sessionOf(idx).mainTurns, 2);

    writeFileSync(file, usageLine("c", 50)); // same inode, shorter file
    const s = sessionOf(idx);
    assert.equal(s.mainTurns, 1);
    assert.equal(s.mainCtx, 50);
  } finally {
    idx.close();
    rmSync(root, { recursive: true, force: true });
  }
});

test("rewrite by delete and recreate (new inode) at equal size drops stale messages", () => {
  const { root, file } = fixture();
  const idx = new LiveIndex(root);
  // Keeps the old inode allocated, so the recreated file gets a different inode number.
  let keep: number | null = null;
  try {
    writeFileSync(file, usageLine("old", 100));
    const before = sessionOf(idx);
    assert.equal(before.mainTurns, 1);
    const oldSize = statSync(file).size;
    const oldIno = statSync(file).ino;

    keep = openSync(file, "r");
    unlinkSync(file);
    writeFileSync(file, usageLine("new", 999)); // same length as the old line
    assert.equal(statSync(file).size, oldSize);
    assert.notEqual(statSync(file).ino, oldIno);

    const s = sessionOf(idx);
    assert.equal(s.mainTurns, 1, "the stale message must be gone");
    assert.equal(s.mainCtx, 999);
  } finally {
    if (keep !== null) closeSync(keep);
    idx.close();
    rmSync(root, { recursive: true, force: true });
  }
});

test("a later custom-title replaces the earlier one", () => {
  const { root, file } = fixture();
  const idx = new LiveIndex(root);
  try {
    writeFileSync(file, userLine("first prompt") + customTitleLine("First"));
    assert.equal(sessionOf(idx).title, "First");

    appendFileSync(file, customTitleLine("Second"));
    assert.equal(sessionOf(idx).title, "Second");
  } finally {
    idx.close();
    rmSync(root, { recursive: true, force: true });
  }
});

test("timestamp comes from the parsed line, not from a nested timestamp in tool input", () => {
  const { root, file } = fixture();
  const idx = new LiveIndex(root);
  try {
    const now = new Date().toISOString();
    // The nested "timestamp" appears first in the raw text; the top-level one is recent.
    const line =
      JSON.stringify({
        type: "assistant",
        message: {
          id: "nested",
          model: MODEL,
          content: [{ type: "tool_use", id: "tu", name: "Probe", input: { timestamp: "2001-01-01T00:00:00.000Z" } }],
          usage: usageOf(5),
        },
        timestamp: now,
      }) + "\n";
    writeFileSync(file, line);
    const s = sessionOf(idx);
    assert.equal(s.lastActivity, Date.parse(now));
  } finally {
    idx.close();
    rmSync(root, { recursive: true, force: true });
  }
});

test("pruning per-message records leaves the all-time turns, peak and subagent cost unchanged", () => {
  const { root, dir, file } = fixture();
  const subDir = join(dir, SID, "subagents");
  mkdirSync(subDir, { recursive: true });
  const subFile = join(subDir, "agent-sub2.jsonl");
  const oldTs = new Date(Date.now() - 2 * DAY).toISOString();
  writeFileSync(file, usageLine("old-main", 1000, oldTs) + usageLine("new-main", 5000));
  writeFileSync(subFile, usageLine("old-sub", 300, oldTs) + usageLine("new-sub", 700));
  const idx = new LiveIndex(root);
  try {
    const expectedSub = turnCost(MODEL, usageOf(300)).usd + turnCost(MODEL, usageOf(700)).usd;

    const s1 = sessionOf(idx);
    assert.equal(s1.mainTurns, 2);
    assert.equal(s1.mainPeakCtx, 5000);
    assert.equal(s1.activeSubagents.length, 1);
    assert.ok(Math.abs(s1.activeSubagents[0].costUsd - expectedSub) < 1e-12);
    // The windowed store holds only the recent main message; the old one is still in the summary.
    assert.equal((idx as any).files.get(file).msgs.size, 1);

    appendFileSync(file, usageLine("later-main", 3000));
    const s2 = sessionOf(idx);
    assert.equal(s2.mainTurns, 3);
    assert.equal(s2.mainPeakCtx, 5000);
    assert.ok(Math.abs(s2.activeSubagents[0].costUsd - expectedSub) < 1e-12);
  } finally {
    idx.close();
    rmSync(root, { recursive: true, force: true });
  }
});

test("an idle main transcript is kept while a subagent is active, and a poll does not re-read it", () => {
  const { root, dir, file } = fixture();
  const subDir = join(dir, SID, "subagents");
  mkdirSync(subDir, { recursive: true });
  const subFile = join(subDir, "agent-sub3.jsonl");
  const old = new Date(Date.now() - 2 * DAY);
  writeFileSync(file, customTitleLine("Idle main") + usageLine("main1", 4000, old.toISOString()));
  utimesSync(file, old, old);
  writeFileSync(subFile, usageLine("sub1", 700));
  const idx = new LiveIndex(root);
  try {
    const s1 = sessionOf(idx);
    assert.equal(s1.title, "Idle main");
    assert.equal(s1.mainTurns, 1);
    assert.equal(s1.mainCtx, 4000);
    assert.equal(s1.mainModel, MODEL);
    assert.equal(s1.activeSubagents.length, 1);

    const mainState = (idx as any).files.get(file);
    assert.ok(mainState, "the main transcript stays tracked while its session is live");
    const offset = mainState.offset;
    // A poll with nothing new reports no changed sessions and does not re-read the main file.
    assert.deepEqual((idx as any).walk(Date.now() - DAY), []);
    assert.equal(mainState.offset, offset);

    const s2 = sessionOf(idx);
    assert.equal(s2.title, "Idle main");
    assert.equal(s2.mainTurns, 1);
    assert.equal(s2.mainCtx, 4000);
    assert.equal(s2.mainModel, MODEL);
  } finally {
    idx.close();
    rmSync(root, { recursive: true, force: true });
  }
});

test("a session whose files are all older than 24 h is dropped and not re-added by a poll", () => {
  const { root, file } = fixture();
  writeFileSync(file, usageLine("m1", 100));
  const idx = new LiveIndex(root);
  try {
    assert.equal(idx.sessionCount(), 1);
    const old = new Date(Date.now() - 2 * DAY);
    utimesSync(file, old, old);
    idx.refreshNow();
    assert.equal(idx.sessionCount(), 0);
    assert.equal(idx.fileCount(), 0);

    idx.refreshNow();
    assert.equal(idx.sessionCount(), 0);
    assert.equal(idx.fileCount(), 0);
  } finally {
    idx.close();
    rmSync(root, { recursive: true, force: true });
  }
});

test("deleting a tracked transcript between polls does not throw and forgets it", () => {
  const { root, file } = fixture();
  writeFileSync(file, usageLine("m1", 100));
  const idx = new LiveIndex(root);
  try {
    assert.equal(idx.fileCount(), 1);
    unlinkSync(file);
    const snap = idx.refreshNow();
    assert.equal(idx.fileCount(), 0);
    assert.equal(idx.sessionCount(), 0);
    assert.equal(snap.sessions.length, 0);
  } finally {
    idx.close();
    rmSync(root, { recursive: true, force: true });
  }
});

test("a synthetic <...> placeholder is not the session's latest model or context", () => {
  const { root, file } = fixture();
  const idx = new LiveIndex(root);
  try {
    const later = new Date(Date.now() + 1000).toISOString();
    const synthetic =
      JSON.stringify({
        type: "assistant",
        timestamp: later,
        uuid: "uuid-synth",
        message: { id: "synth", model: "<synthetic>", usage: usageOf(0) },
      }) + "\n";
    writeFileSync(file, usageLine("real", 1234) + synthetic);
    const s = sessionOf(idx);
    assert.equal(s.mainModel, MODEL);
    assert.equal(s.mainCtx, 1234);
  } finally {
    idx.close();
    rmSync(root, { recursive: true, force: true });
  }
});
