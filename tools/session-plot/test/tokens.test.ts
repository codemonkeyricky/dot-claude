import { test } from "node:test";
import assert from "node:assert/strict";
import { isTokenKind, minuteTokenTotal, minuteTokens, tokensOf, type TokenKind } from "../src/tokens";
import type { LiveMinute } from "../src/types";

const counts = { in: 1, cacheWrite: 2, cacheRead: 4, out: 8 };
const minute: LiveMinute = {
  t: 0,
  byModel: {
    "model-a": { cost: 0, in: 1, cacheWrite: 2, cacheRead: 4, out: 8 },
    "model-b": { cost: 0, in: 10, cacheWrite: 0, cacheRead: 0, out: 0 },
  },
};

test("each kind sums the right counters", () => {
  assert.equal(tokensOf(counts, "all"), 15);
  assert.equal(tokensOf(counts, "output"), 8);
  assert.equal(tokensOf(counts, "input"), 3);
  assert.equal(tokensOf(counts, "cacheRead"), 4);
});

test("per-model values for a kind, a model with no activity in that kind is 0", () => {
  assert.deepEqual(minuteTokens(minute, "all"), { "model-a": 15, "model-b": 10 });
  assert.deepEqual(minuteTokens(minute, "output"), { "model-a": 8, "model-b": 0 });
  assert.deepEqual(minuteTokens(minute, "input"), { "model-a": 3, "model-b": 10 });
  assert.deepEqual(minuteTokens(minute, "cacheRead"), { "model-a": 4, "model-b": 0 });
  assert.equal(minuteTokenTotal(minute, "all"), 25);
});

test("an empty minute gives no models and a zero total for every kind", () => {
  const empty: LiveMinute = { t: 0, byModel: {} };
  for (const kind of ["all", "output", "input", "cacheRead"] as TokenKind[]) {
    assert.deepEqual(minuteTokens(empty, kind), {});
    assert.equal(minuteTokenTotal(empty, kind), 0);
  }
});

test("a model named __proto__ is kept as an ordinary key", () => {
  const odd: LiveMinute = {
    t: 0,
    // Computed key: a plain __proto__ key in a literal would set the prototype instead.
    byModel: { ["__proto__"]: { cost: 0, in: 2, cacheWrite: 0, cacheRead: 0, out: 1 } },
  };
  const v = minuteTokens(odd, "all");
  assert.deepEqual(Object.keys(v), ["__proto__"]);
  assert.equal(v["__proto__"], 3);
});

test("isTokenKind accepts only the four kinds", () => {
  assert.equal(isTokenKind("cacheRead"), true);
  assert.equal(isTokenKind("cache"), false);
  assert.equal(isTokenKind(null), false);
});
