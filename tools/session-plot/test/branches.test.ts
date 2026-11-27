import { test } from "node:test";
import assert from "node:assert/strict";
import { branchLanes } from "../src/branches";
import type { Series } from "../src/types";

function sub(id: string, start: number, end: number, spawn?: number): Series {
  return {
    id,
    label: id,
    kind: "executor",
    spawn,
    start,
    end,
    points: [],
    peakCtx: 0,
    turns: 0,
    outputTokens: 0,
    costUsd: 0,
  };
}

function laneOf(lanes: { id: string; lane: number }[], id: string): number | undefined {
  return lanes.find((l) => l.id === id)?.lane;
}

test("no subagents gives no branches", () => {
  assert.deepEqual(branchLanes([]), []);
});

test("one subagent goes on lane +1 (above the trunk)", () => {
  const lanes = branchLanes([sub("a", 100, 200, 100)]);
  assert.equal(lanes.length, 1);
  assert.equal(lanes[0].id, "a");
  assert.equal(lanes[0].lane, 1);
  assert.equal(lanes[0].fork, 100);
  assert.equal(lanes[0].merge, 200);
});

test("two overlapping subagents go on +1 and -1", () => {
  const lanes = branchLanes([sub("a", 0, 100, 0), sub("b", 50, 150, 50)]);
  assert.equal(laneOf(lanes, "a"), 1);
  assert.equal(laneOf(lanes, "b"), -1);
});

test("three overlapping subagents go on +1, -1 and +2", () => {
  const lanes = branchLanes([sub("a", 0, 100, 0), sub("b", 10, 110, 10), sub("c", 20, 120, 20)]);
  assert.equal(laneOf(lanes, "a"), 1);
  assert.equal(laneOf(lanes, "b"), -1);
  assert.equal(laneOf(lanes, "c"), 2);
});

test("a sequential subagent after the first merges reuses +1", () => {
  const lanes = branchLanes([sub("a", 0, 100, 0), sub("b", 50, 150, 50), sub("c", 200, 300, 200)]);
  assert.equal(laneOf(lanes, "a"), 1);
  assert.equal(laneOf(lanes, "b"), -1);
  assert.equal(laneOf(lanes, "c"), 1);
});

test("the gap keeps a lane busy until it has been free for the gap", () => {
  // a merges at 100. c forks at 105: free with no gap, but not with a gap of 10 (100 + 10 >= 105).
  const subs = [sub("a", 0, 100, 0), sub("b", 50, 150, 50), sub("c", 105, 300, 105)];
  assert.equal(laneOf(branchLanes(subs), "c"), 1);
  assert.equal(laneOf(branchLanes(subs, 10), "c"), 2);
});

test("ties on fork time are ordered by id, whatever the input order", () => {
  const forward = branchLanes([sub("a", 0, 100, 0), sub("b", 0, 100, 0)]);
  const reverse = branchLanes([sub("b", 0, 100, 0), sub("a", 0, 100, 0)]);
  assert.equal(laneOf(forward, "a"), 1);
  assert.equal(laneOf(forward, "b"), -1);
  assert.equal(laneOf(reverse, "a"), 1);
  assert.equal(laneOf(reverse, "b"), -1);
  assert.deepEqual(
    forward.map((l) => l.id),
    ["a", "b"],
  );
});

test("a missing spawn time uses the start time as the fork", () => {
  const lanes = branchLanes([sub("a", 500, 600)]);
  assert.equal(lanes[0].fork, 500);
  assert.equal(lanes[0].merge, 600);
  assert.equal(lanes[0].lane, 1);
});

test("an end before the fork gives merge = fork", () => {
  const lanes = branchLanes([sub("a", 100, 50, 100)]);
  assert.equal(lanes[0].fork, 100);
  assert.equal(lanes[0].merge, 100);
});
