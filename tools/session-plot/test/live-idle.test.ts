import { test, mock } from "node:test";
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { LiveIndex, type LiveUpdate } from "../src/live";

const PROJ = "proj-a";
const SID = "aaaaaaaa-0000-0000-0000-000000000003";
const MINUTE = 60_000;
const HOUR = 3_600_000;

// Quiet machine: one assistant message 10 minutes before "now", then no file changes at all.
// With a fake clock, the index's own idle timer must move the snapshot forward on its own.
test("with no file changes, two hours later the snapshot has moved on and the last-hour cost is zero", () => {
  const root = mkdtempSync(join(tmpdir(), "sp-live-idle-"));
  const dir = join(root, PROJ);
  mkdirSync(dir, { recursive: true });
  const t0 = Math.floor(Date.now() / MINUTE) * MINUTE;
  const msg = {
    type: "assistant",
    timestamp: new Date(t0 - 10 * MINUTE).toISOString(),
    uuid: "u-1",
    message: {
      id: "msg-1",
      model: "claude-opus-5-5",
      usage: { input_tokens: 1000, output_tokens: 10, cache_creation_input_tokens: 0, cache_read_input_tokens: 0 },
    },
  };
  writeFileSync(join(dir, `${SID}.jsonl`), JSON.stringify(msg) + "\n");

  mock.timers.enable({ apis: ["Date", "setInterval"], now: t0 });
  const idx = new LiveIndex(root);
  try {
    const first = idx.snapshot();
    assert.ok(first.totals.costLastHour > 0, "the message is inside the last hour at the start");

    const updates: LiveUpdate[] = [];
    idx.on("update", (u: LiveUpdate) => updates.push(u));
    mock.timers.tick(2 * HOUR);

    const later = idx.snapshot();
    assert.equal(later.now, t0 + 2 * HOUR, "snapshot now moved forward by two hours");
    assert.equal(later.totals.costLastHour, 0, "the message left the last hour");
    assert.ok(updates.length > 0, "subscribers received new snapshots without any file change");
    assert.ok(updates.every((u) => u.changed.length === 0), "idle snapshots report no changed session");
    assert.equal(updates[updates.length - 1].snapshot.now, later.now);
  } finally {
    mock.timers.reset();
    idx.close();
    rmSync(root, { recursive: true, force: true });
  }
});
