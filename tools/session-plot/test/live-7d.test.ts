import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { LiveIndex } from "../src/live";
import { turnCost } from "../src/cost";

// 7-day cost: one message per calendar day for nine days (today back to eight days ago, at local
// noon), plus one message three days ago in a second session. All messages cost the same, so the
// expected figures are exact multiples of one message's cost.
const PROJ = "proj-a";
const SID = "aaaaaaaa-0000-0000-0000-000000000011";
const SID_OLD = "aaaaaaaa-0000-0000-0000-000000000012";
const MODEL = "claude-opus-5-5";
const USAGE = { input_tokens: 100_000, output_tokens: 10, cache_creation_input_tokens: 0, cache_read_input_tokens: 0 };
const PER_MSG = turnCost(MODEL, USAGE).usd;
const EPS = 1e-9;

// Local noon, k calendar days before today.
function noonDaysAgo(k: number): Date {
  const now = new Date();
  return new Date(now.getFullYear(), now.getMonth(), now.getDate() - k, 12, 0, 0);
}

function messageLine(msgId: string, when: Date): string {
  return (
    JSON.stringify({
      type: "assistant",
      timestamp: when.toISOString(),
      uuid: `uuid-${msgId}`,
      message: { id: msgId, model: MODEL, usage: USAGE },
    }) + "\n"
  );
}

function setup(): { root: string; dir: string } {
  const root = mkdtempSync(join(tmpdir(), "sp-live-7d-"));
  const dir = join(root, PROJ);
  mkdirSync(dir, { recursive: true });
  let lines = "";
  for (let k = 0; k <= 8; k++) lines += messageLine(`day-${k}`, noonDaysAgo(k));
  writeFileSync(join(dir, `${SID}.jsonl`), lines);
  writeFileSync(join(dir, `${SID_OLD}.jsonl`), messageLine("old-3", noonDaysAgo(3)));
  return { root, dir };
}

test("cost7d covers seven calendar days and excludes messages from days 8 and 9 back", () => {
  const { root } = setup();
  const idx = new LiveIndex(root);
  try {
    const { totals } = idx.refreshNow();
    // Days 0..6 hold one message each, day 3 also holds the session-two message; days 7 and 8 are out.
    assert.ok(Math.abs(totals.cost7d - 8 * PER_MSG) < EPS, `cost7d ${totals.cost7d} should be 8 messages`);
  } finally {
    idx.close();
    rmSync(root, { recursive: true, force: true });
  }
});

test("costByDay has seven entries, oldest first, that sum to cost7d", () => {
  const { root } = setup();
  const idx = new LiveIndex(root);
  try {
    const { totals } = idx.refreshNow();
    assert.equal(totals.costByDay.length, 7);
    const sum = totals.costByDay.reduce((a, b) => a + b.cost, 0);
    assert.ok(Math.abs(sum - totals.cost7d) < EPS, "costByDay sums to cost7d");
    for (let i = 0; i < 7; i++) {
      const want = noonDaysAgo(6 - i);
      const midnight = new Date(want.getFullYear(), want.getMonth(), want.getDate()).getTime();
      assert.equal(totals.costByDay[i].day, midnight, `entry ${i} is local midnight of its day`);
      const expected = i === 3 ? 2 * PER_MSG : PER_MSG;
      assert.ok(Math.abs(totals.costByDay[i].cost - expected) < EPS, `entry ${i} cost ${totals.costByDay[i].cost}`);
    }
  } finally {
    idx.close();
    rmSync(root, { recursive: true, force: true });
  }
});

test("the last costByDay entry is today's cost", () => {
  const { root } = setup();
  const idx = new LiveIndex(root);
  try {
    const { totals } = idx.refreshNow();
    assert.ok(Math.abs(totals.costByDay[6].cost - totals.costToday) < EPS, "last entry equals costToday");
    assert.ok(Math.abs(totals.costToday - PER_MSG) < EPS, "one message today");
  } finally {
    idx.close();
    rmSync(root, { recursive: true, force: true });
  }
});

test("a session active only three days ago counts toward cost7d but is not in the 24 h session list", () => {
  const { root } = setup();
  const idx = new LiveIndex(root);
  try {
    const snap = idx.refreshNow();
    assert.ok(snap.sessions.some((s) => s.id === SID), "the session active today is listed");
    assert.ok(!snap.sessions.some((s) => s.id === SID_OLD), "the three-day-old session is not listed");
    assert.ok(snap.totals.cost7d >= 2 * PER_MSG, "its message is in cost7d");
  } finally {
    idx.close();
    rmSync(root, { recursive: true, force: true });
  }
});
