import { test } from "node:test";
import assert from "node:assert/strict";
import { LatestRequest } from "../src/latest";

// The main page's session refresh captures the selection token when it starts. A user pick
// (start) made while it is in flight must make its late result stale.
test("a live refresh started before a user selection is stale when it lands", () => {
  const selection = new LatestRequest();
  const first = selection.start(); // user opens session A
  const refresh = selection.mark(); // live refresh for A begins
  assert.equal(selection.isCurrent(refresh), true);
  const second = selection.start(); // user picks B while the refresh is in flight
  assert.equal(selection.isCurrent(refresh), false, "refresh for A must be dropped");
  assert.equal(selection.isCurrent(second), true);
  assert.equal(selection.isCurrent(first), false);
});

test("a refresh with no selection in between still applies", () => {
  const selection = new LatestRequest();
  selection.start();
  const refresh = selection.mark();
  assert.equal(selection.isCurrent(refresh), true);
});
