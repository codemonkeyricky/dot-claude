import { test } from "node:test";
import assert from "node:assert/strict";
import { AXIS_W, buildLiveBranches, compressAxis, mergeSpans, spanTotal, type BranchSource } from "../src/livebranches";

const MIN = 60_000;
const NOW = 1_000_000_000;

function sub(id: string, times: number[], extra: Partial<BranchSource> = {}): BranchSource {
  return { id, label: id, times, ...extra };
}

test("a gap exactly equal to idleGapMs (10 min by default) stays one trunk segment", () => {
  const b = buildLiveBranches([0, 10 * MIN, 20 * MIN], [], NOW);
  assert.deepEqual(b.trunk, [[0, 20 * MIN]]);
});

test("a gap longer than idleGapMs (10 min by default) splits the trunk", () => {
  const b = buildLiveBranches([0, 10 * MIN + 1, 20 * MIN + 2], [], NOW);
  // Both gaps (10 min + 1 ms, and 10 min + 1 ms) are longer than idleGapMs, so three segments.
  assert.deepEqual(b.trunk, [[0, 0], [10 * MIN + 1, 10 * MIN + 1], [20 * MIN + 2, 20 * MIN + 2]]);
});

test("a lone trunk time gives a [t,t] segment", () => {
  const b = buildLiveBranches([42], [], NOW);
  assert.deepEqual(b.trunk, [[42, 42]]);
  assert.equal(b.start, 42);
  assert.equal(b.end, 42);
});

test("agenticMs is the union of overlapping subagent intervals", () => {
  const subs = [sub("a", [0, 10]), sub("b", [5, 20]), sub("c", [30, 40])];
  const b = buildLiveBranches([], subs, NOW);
  assert.equal(b.agenticMs, 20 + 10);
});

test("activeMs is the union of trunk segments and subagent intervals", () => {
  // Trunk [0,10] and a subagent [5,20] union to [0,20]; a separate trunk [100,100] adds nothing.
  const b = buildLiveBranches([0, 10, 100], [sub("a", [5, 20])], NOW, { idleGapMs: 50 });
  assert.equal(b.activeMs, 20 + 0);
  // A trunk segment that the subagent does not cover adds its length.
  const c = buildLiveBranches([0, 30], [sub("a", [5, 10])], NOW, { idleGapMs: 100 });
  assert.equal(c.activeMs, 30);
  assert.equal(c.agenticMs, 5);
});

test("a main-only session has zero agentic time", () => {
  const b = buildLiveBranches([0, MIN, 2 * MIN], [], NOW);
  assert.equal(b.agenticMs, 0);
  assert.equal(b.activeMs, 2 * MIN);
  assert.deepEqual(b.branches, []);
});

test("lanes beyond maxLanes are clamped, and maxLane keeps the unclamped size", () => {
  // Seven overlapping subagents need lanes +1,-1,+2,-2,+3,-3,+4; the 7th is drawn on lane +3.
  const ids = ["a", "b", "c", "d", "e", "f", "g"];
  const subs = ids.map((id, i) => sub(id, [i, 100]));
  const b = buildLiveBranches([], subs, NOW, { maxLanes: 3 });
  assert.equal(b.maxLane, 4);
  assert.deepEqual(
    b.branches.map((x) => x.lane),
    [1, -1, 2, -2, 3, -3, 3],
  );
  assert.ok(b.branches.every((x) => Math.abs(x.lane) <= 3));
});

test("active is true only when the last message is within activeMs of now", () => {
  const subs = [sub("recent", [NOW - 60_000]), sub("old", [NOW - 300_000])];
  const b = buildLiveBranches([], subs, NOW, { activeMs: 120_000 });
  const active = Object.fromEntries(b.branches.map((x) => [x.id, x.active]));
  assert.deepEqual(active, { recent: true, old: false });
});

test("maxBranches keeps the branches with the latest fork", () => {
  const subs = [sub("s1", [10]), sub("s2", [20]), sub("s3", [30]), sub("s4", [40])];
  const b = buildLiveBranches([], subs, NOW, { maxBranches: 2 });
  assert.deepEqual(b.branches.map((x) => x.id), ["s3", "s4"]);
});

test("branches are sorted by fork, then id, and subagents without times are dropped", () => {
  const subs = [sub("z", [5]), sub("a", [5]), sub("none", []), sub("early", [1])];
  const b = buildLiveBranches([], subs, NOW);
  assert.deepEqual(b.branches.map((x) => x.id), ["early", "a", "z"]);
});

test("model and agentType are carried over when present", () => {
  const b = buildLiveBranches([], [sub("m", [1, 2], { model: "claude-x", agentType: "executor" })], NOW);
  assert.equal(b.branches[0].model, "claude-x");
  assert.equal(b.branches[0].agentType, "executor");
  assert.equal(b.branches[0].fork, 1);
  assert.equal(b.branches[0].merge, 2);
});

test("empty input gives zeros", () => {
  assert.deepEqual(buildLiveBranches([], [], NOW), {
    start: 0,
    end: 0,
    trunk: [],
    branches: [],
    agenticMs: 0,
    activeMs: 0,
    maxLane: 0,
    mainTurns: 0,
    agenticTurns: 0,
  });
});

test("start and end cover the main session and the subagents", () => {
  const b = buildLiveBranches([50, 60], [sub("a", [10, 90])], NOW);
  assert.equal(b.start, 10);
  assert.equal(b.end, 90);
});

test("mergeSpans joins touching and overlapping spans and spanTotal measures the union", () => {
  assert.deepEqual(mergeSpans([[5, 10], [0, 5], [20, 30]]), [[0, 10], [20, 30]]);
  assert.equal(spanTotal([[0, 10], [5, 12], [20, 25]]), 17);
  assert.equal(spanTotal([]), 0);
});

test("a main-only session has zero agentic turns", () => {
  const b = buildLiveBranches([0, MIN, 2 * MIN], [], NOW);
  assert.equal(b.mainTurns, 3);
  assert.equal(b.agenticTurns, 0);
});

test("main turns inside a subagent interval are agentic, the others are talk", () => {
  // Subagent [10, 20]: the turns at 10, 15 and 20 ran while it worked; 5 and 25 did not.
  const b = buildLiveBranches([5, 10, 15, 20, 25], [sub("a", [10, 20])], NOW);
  assert.equal(b.mainTurns, 5);
  assert.equal(b.agenticTurns, 3);
});

test("a main turn exactly at a subagent's fork or merge time counts as agentic", () => {
  const b = buildLiveBranches([100, 200], [sub("a", [100, 200])], NOW);
  assert.equal(b.agenticTurns, 2);
});

test("an active subagent covers the activeMs window before now", () => {
  // The subagent's last message is 30 s ago, so it is active and covers [NOW-120 s, NOW].
  const b = buildLiveBranches([NOW - 60_000, NOW - 400_000], [sub("a", [NOW - 30_000])], NOW, { activeMs: 120_000 });
  assert.equal(b.agenticTurns, 1);
});

test("compressAxis is monotonic and stays within [0, AXIS_W]", () => {
  const b = buildLiveBranches([0, MIN, 60 * MIN, 61 * MIN], [], NOW);
  const { x } = compressAxis(b, 61 * MIN);
  let prev = -Infinity;
  for (let t = -5 * MIN; t <= 70 * MIN; t += 0.5 * MIN) {
    const px = x(t);
    assert.ok(px >= 0 && px <= AXIS_W, `x(${t}) = ${px} is within the axis`);
    assert.ok(px >= prev, `x is non-decreasing at ${t}`);
    prev = px;
  }
});

test("a gap within breakMs stays linear: no break mark, and time maps proportionally", () => {
  const b = buildLiveBranches([0, 2 * MIN, 4 * MIN], [], NOW);
  const axis = compressAxis(b, 4 * MIN);
  assert.deepEqual(axis.breaks, []);
  assert.equal(axis.x(2 * MIN), AXIS_W / 2);
});

test("a gap longer than breakMs is drawn at a fixed width and gets one break mark", () => {
  // Trunk [0,1 min] and [60 min, 61 min]: the 59 min gap is compressed to 3% of the axis.
  const b = buildLiveBranches([0, MIN, 60 * MIN, 61 * MIN], [], NOW);
  const axis = compressAxis(b, 61 * MIN);
  assert.equal(axis.breaks.length, 1);
  const gapPx = axis.x(60 * MIN) - axis.x(MIN);
  assert.ok(Math.abs(gapPx - 0.03 * AXIS_W) < 1e-9, `gap is ${gapPx} px`);
  // The break mark sits in the middle of the gap.
  assert.ok(Math.abs(axis.breaks[0] - (axis.x(MIN) + gapPx / 2)) < 1e-9);
});
