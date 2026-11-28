import { test } from "node:test";
import assert from "node:assert/strict";
import { stableOrder } from "../src/order";

const s = (id: string, lastActivity: number) => ({ id, lastActivity });

test("with no previous order, sessions are listed most recent first", () => {
  const got = stableOrder([], [s("a", 100), s("c", 300), s("b", 200)]);
  assert.deepEqual(got, ["c", "b", "a"]);
});

test("an existing order is kept even when a lower session becomes the most recent", () => {
  const prev = ["a", "b", "c"];
  const got = stableOrder(prev, [s("a", 100), s("b", 200), s("c", 900)]);
  assert.deepEqual(got, ["a", "b", "c"]);
});

test("a new session goes on top of the kept order", () => {
  const got = stableOrder(["a", "b"], [s("a", 100), s("b", 200), s("n", 50)]);
  assert.deepEqual(got, ["n", "a", "b"]);
});

test("a session that is no longer present is dropped", () => {
  const got = stableOrder(["a", "gone", "b"], [s("b", 200), s("a", 100)]);
  assert.deepEqual(got, ["a", "b"]);
});

test("two new sessions are ordered by lastActivity, most recent first, above the kept ones", () => {
  const got = stableOrder(["a"], [s("a", 100), s("x", 300), s("y", 500)]);
  assert.deepEqual(got, ["y", "x", "a"]);
});
