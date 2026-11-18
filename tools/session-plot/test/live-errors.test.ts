import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { LiveIndex } from "../src/live";

const PROJ = "proj-a";
const SID = "aaaaaaaa-0000-0000-0000-000000000009";

// A directory named like a transcript: stat succeeds, but reading it fails with EISDIR, which is
// neither ENOENT nor ENOTDIR, so the index must not treat it as a deleted file.
test("a read error other than ENOENT/ENOTDIR warns once and reports no change", () => {
  const root = mkdtempSync(join(tmpdir(), "sp-live-err-"));
  const dir = join(root, PROJ);
  mkdirSync(join(dir, `${SID}.jsonl`), { recursive: true });
  const warnings: string[] = [];
  const realWarn = console.warn;
  console.warn = (...args: unknown[]) => {
    warnings.push(args.map(String).join(" "));
  };
  const idx = new LiveIndex(root);
  try {
    assert.doesNotThrow(() => idx.refreshNow());
    assert.doesNotThrow(() => idx.refreshNow());
    const matching = warnings.filter((w) => w.includes(`${SID}.jsonl`));
    assert.equal(matching.length, 1, `expected one warning, got ${JSON.stringify(warnings)}`);
    assert.match(matching[0], /cannot read/);
  } finally {
    console.warn = realWarn;
    idx.close();
    rmSync(root, { recursive: true, force: true });
  }
});
