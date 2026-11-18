import { test } from "node:test";
import assert from "node:assert/strict";
import { appendFileSync, closeSync, mkdirSync, mkdtempSync, openSync, rmSync, statSync, unlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { LiveIndex } from "../src/live";

const SID = "aaaaaaaa-0000-0000-0000-000000000001";
const PROJ = "proj-a";

function usageLine(msgId: string, input: number, ts: string = new Date().toISOString()): string {
  return (
    JSON.stringify({
      type: "assistant",
      timestamp: ts,
      uuid: `uuid-${msgId}`,
      message: {
        id: msgId,
        model: "claude-opus-5-5",
        usage: { input_tokens: input, output_tokens: 10, cache_creation_input_tokens: 0, cache_read_input_tokens: 0 },
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
          model: "claude-opus-5-5",
          content: [{ type: "tool_use", id: "tu", name: "Probe", input: { timestamp: "2001-01-01T00:00:00.000Z" } }],
          usage: { input_tokens: 5, output_tokens: 1, cache_creation_input_tokens: 0, cache_read_input_tokens: 0 },
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
