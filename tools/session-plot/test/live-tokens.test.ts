import { test } from "node:test";
import assert from "node:assert/strict";
import { appendFileSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { LiveIndex } from "../src/live";
import type { TokenSplit } from "../src/types";

const SID = "aaaaaaaa-0000-0000-0000-000000000011";
const PROJ = "proj-tok";
const DAY = 24 * 3_600_000;
const MODEL = "claude-opus-5-5";

interface Counts {
  in: number;
  cw: number;
  cr: number;
  out: number;
}

// One assistant line with explicit usage. The same msgId on a later line replaces the earlier usage.
function line(msgId: string, c: Counts, ts: string = new Date().toISOString()): string {
  return (
    JSON.stringify({
      type: "assistant",
      timestamp: ts,
      uuid: `uuid-${msgId}-${Math.random()}`,
      message: {
        id: msgId,
        model: MODEL,
        usage: {
          input_tokens: c.in,
          cache_creation_input_tokens: c.cw,
          cache_read_input_tokens: c.cr,
          output_tokens: c.out,
        },
      },
    }) + "\n"
  );
}

function fixture(): { root: string; dir: string; file: string; subDir: string } {
  const root = mkdtempSync(join(tmpdir(), "sp-live-tok-"));
  const dir = join(root, PROJ);
  const subDir = join(dir, SID, "subagents");
  mkdirSync(subDir, { recursive: true });
  return { root, dir, file: join(dir, `${SID}.jsonl`), subDir };
}

function sessionOf(idx: LiveIndex) {
  const snap = idx.refreshNow();
  const s = snap.sessions.find((x) => x.id === SID);
  assert.ok(s, "session should be in the snapshot");
  return s;
}

const sum = (a: TokenSplit, b: TokenSplit): TokenSplit => ({
  in: a.in + b.in,
  cacheWrite: a.cacheWrite + b.cacheWrite,
  cacheRead: a.cacheRead + b.cacheRead,
  out: a.out + b.out,
});

test("session token totals add up across the main transcript and its subagents", () => {
  const { root, file, subDir } = fixture();
  const main = [
    { id: "m1", c: { in: 100, cw: 20, cr: 300, out: 5 } },
    { id: "m2", c: { in: 50, cw: 0, cr: 0, out: 7 } },
  ];
  const sub = { id: "s1", c: { in: 10, cw: 1, cr: 2, out: 3 } };
  writeFileSync(file, main.map((m) => line(m.id, m.c)).join(""));
  writeFileSync(join(subDir, "agent-sub1.jsonl"), line(sub.id, sub.c));
  const idx = new LiveIndex(root);
  try {
    const s = sessionOf(idx);
    const expectedMain: TokenSplit = { in: 150, cacheWrite: 20, cacheRead: 300, out: 12 };
    const expectedSub: TokenSplit = { in: 10, cacheWrite: 1, cacheRead: 2, out: 3 };
    assert.deepEqual(s.activeSubagents[0].tokens, expectedSub);
    assert.deepEqual(s.tokToday, sum(expectedMain, expectedSub));
    assert.deepEqual(s.tokLastHour, sum(expectedMain, expectedSub));
  } finally {
    idx.close();
    rmSync(root, { recursive: true, force: true });
  }
});

test("a repeated message id is counted once, with its last usage", () => {
  const { root, file } = fixture();
  writeFileSync(file, line("dup", { in: 100, cw: 0, cr: 0, out: 5 }) + line("dup", { in: 100, cw: 0, cr: 0, out: 50 }));
  const idx = new LiveIndex(root);
  try {
    const s = sessionOf(idx);
    assert.deepEqual(s.tokToday, { in: 100, cacheWrite: 0, cacheRead: 0, out: 50 });
    assert.deepEqual(s.tokLastHour, { in: 100, cacheWrite: 0, cacheRead: 0, out: 50 });
  } finally {
    idx.close();
    rmSync(root, { recursive: true, force: true });
  }
});

test("pruning old messages leaves the all-time subagent tokens unchanged", () => {
  const { root, file, subDir } = fixture();
  const subFile = join(subDir, "agent-sub2.jsonl");
  const oldTs = new Date(Date.now() - 2 * DAY).toISOString();
  writeFileSync(file, line("main-new", { in: 5, cw: 0, cr: 0, out: 1 }));
  writeFileSync(
    subFile,
    line("sub-old", { in: 300, cw: 30, cr: 3000, out: 40 }, oldTs) + line("sub-new", { in: 700, cw: 7, cr: 70, out: 4 }),
  );
  const idx = new LiveIndex(root);
  try {
    const allTimeSub: TokenSplit = { in: 1000, cacheWrite: 37, cacheRead: 3070, out: 44 };
    const s1 = sessionOf(idx);
    assert.deepEqual(s1.activeSubagents[0].tokens, allTimeSub);
    // The windowed store keeps only the recent subagent message, so today's and the hour's
    // session totals exclude the old one.
    assert.deepEqual(s1.tokToday, { in: 705, cacheWrite: 7, cacheRead: 70, out: 4 + 1 });

    appendFileSync(file, line("main-later", { in: 9, cw: 0, cr: 0, out: 2 }));
    const s2 = sessionOf(idx);
    assert.deepEqual(s2.activeSubagents[0].tokens, allTimeSub);
  } finally {
    idx.close();
    rmSync(root, { recursive: true, force: true });
  }
});

// One assistant line for a given model. Messages of different models in one session are split by byModel.
function modelLine(model: string, msgId: string, c: Counts): string {
  return (
    JSON.stringify({
      type: "assistant",
      timestamp: new Date().toISOString(),
      uuid: `uuid-${msgId}-${Math.random()}`,
      message: {
        id: msgId,
        model,
        usage: { input_tokens: c.in, cache_creation_input_tokens: c.cw, cache_read_input_tokens: c.cr, output_tokens: c.out },
      },
    }) + "\n"
  );
}

test("byModel splits today's cost and tokens by model, and its sums equal the session totals", () => {
  const { root, file, subDir } = fixture();
  writeFileSync(
    file,
    modelLine("claude-opus-5-5", "b1", { in: 1000, cw: 0, cr: 5000, out: 300 }) +
      modelLine("claude-haiku-5-5", "s1", { in: 10, cw: 2, cr: 0, out: 4 }) +
      modelLine("claude-opus-5-5", "b2", { in: 500, cw: 100, cr: 0, out: 50 }),
  );
  writeFileSync(
    join(subDir, "agent-sub-mix.jsonl"),
    modelLine("claude-sonnet-5-5", "t1", { in: 7, cw: 0, cr: 9, out: 1 }) + modelLine("claude-haiku-5-5", "s2", { in: 3, cw: 0, cr: 0, out: 2 }),
  );
  const idx = new LiveIndex(root);
  try {
    const s = sessionOf(idx);
    assert.deepEqual(s.byModel.map((m) => m.model).sort(), ["claude-haiku-5-5", "claude-opus-5-5", "claude-sonnet-5-5"]);
    assert.ok(s.costToday > 0, "priced models, so the cost check is not trivially zero");
    for (let i = 1; i < s.byModel.length; i++) assert.ok(s.byModel[i - 1].cost >= s.byModel[i].cost, "sorted by cost descending");
    const costSum = s.byModel.reduce((acc, m) => acc + m.cost, 0);
    assert.ok(Math.abs(costSum - s.costToday) < 1e-9, `cost ${costSum} should equal costToday ${s.costToday}`);
    const tokSum = s.byModel.reduce((acc, m) => sum(acc, m.tokens), zeroSplit());
    assert.deepEqual(tokSum, s.tokToday);
    const big = s.byModel.find((m) => m.model === "claude-opus-5-5")!;
    assert.deepEqual(big.tokens, { in: 1500, cacheWrite: 100, cacheRead: 5000, out: 350 });
  } finally {
    idx.close();
    rmSync(root, { recursive: true, force: true });
  }
});

function zeroSplit(): TokenSplit {
  return { in: 0, cacheWrite: 0, cacheRead: 0, out: 0 };
}
