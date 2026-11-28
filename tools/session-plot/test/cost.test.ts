import { test } from "node:test";
import assert from "node:assert/strict";
import pricing from "../pricing.json" with { type: "json" };
import { cacheSplit, fmtOutPerUsd, outPerUsd, priceFor, turnCost } from "../src/cost";

test("outPerUsd divides output tokens by list-price dollars", () => {
  const v = outPerUsd(764805, 16.97);
  assert.ok(v !== null && Math.abs(v - 45068.06) < 0.01, String(v));
  assert.equal(outPerUsd(100, 0), null);
  assert.equal(outPerUsd(0, 1), 0);
  assert.equal(outPerUsd(100, -1), null);
  assert.equal(outPerUsd(Number.NaN, 1), null);
});

test("fmtOutPerUsd formats with thousands separators and dash for null", () => {
  assert.equal(fmtOutPerUsd(null), "—");
  assert.equal(fmtOutPerUsd(45072.4), "45,072");
});

type Rates = {
  in: number;
  out: number;
  cacheRead: number;
  fast?: { in: number; out: number };
  longPrompt?: { over: number; in: number; out: number; cacheRead: number };
};
const models = pricing.models as Record<string, Rates>;
const OPUS = "claude-opus-5-5";
const HAIKU = "claude-haiku-5-5";
const opus = models[OPUS];
const haiku = models[HAIKU];
const M = 1e6;
const near = (a: number, b: number): void => assert.ok(Math.abs(a - b) < 1e-12, `${a} != ${b}`);

test("standard turn: input, output, cache read, 5m and 1h cache writes", () => {
  const usage = {
    input_tokens: 1000,
    output_tokens: 500,
    cache_read_input_tokens: 200000,
    cache_creation_input_tokens: 3000,
    cache_creation: { ephemeral_5m_input_tokens: 1000, ephemeral_1h_input_tokens: 2000 },
  };
  const expected =
    (1000 * opus.in +
      1000 * opus.in * pricing.cacheWrite5mMult +
      2000 * opus.in * pricing.cacheWrite1hMult +
      200000 * opus.cacheRead +
      500 * opus.out) /
    M;
  const got = turnCost(OPUS, usage);
  near(got.usd, expected);
  assert.equal(got.unpriced, false);
});

test("fast turn: cache read uses the fast input rate times the standard cache-read/input ratio", () => {
  assert.ok(opus.fast, "pricing.json must give opus a fast tier for this test");
  const fastIn = opus.fast.in;
  const ratio = opus.cacheRead / opus.in;
  const usage = { speed: "fast", input_tokens: 0, output_tokens: 0, cache_read_input_tokens: 200000 };
  const fastCost = turnCost(OPUS, usage).usd;
  near(fastCost, (200000 * fastIn * ratio) / M);
  // With the rates in pricing.json the fast cache read is 2x the standard one when fast input is 2x.
  const standardCost = turnCost(OPUS, { ...usage, speed: undefined }).usd;
  near(fastCost, standardCost * (fastIn / opus.in));
});

test("fast turn: cache writes are 1.25x (5m) and 2x (1h) of the fast input rate", () => {
  const fastIn = opus.fast!.in;
  const usage = {
    speed: "fast",
    cache_creation_input_tokens: 3000,
    cache_creation: { ephemeral_5m_input_tokens: 1000, ephemeral_1h_input_tokens: 2000 },
  };
  const expected =
    (1000 * fastIn * pricing.cacheWrite5mMult + 2000 * fastIn * pricing.cacheWrite1hMult) / M;
  near(turnCost(OPUS, usage).usd, expected);
});

test("fast turn: all four terms are consistent with the fast rates", () => {
  const fast = opus.fast!;
  const usage = {
    speed: "fast",
    input_tokens: 10,
    output_tokens: 20,
    cache_read_input_tokens: 30,
    cache_creation_input_tokens: 40,
    cache_creation: { ephemeral_5m_input_tokens: 40 },
  };
  const cacheReadRate = fast.in * (opus.cacheRead / opus.in);
  const expected =
    (10 * fast.in + 40 * fast.in * pricing.cacheWrite5mMult + 30 * cacheReadRate + 20 * fast.out) / M;
  near(turnCost(OPUS, usage).usd, expected);
});

test("split with only ephemeral_1h present does not double-count", () => {
  const usage = {
    cache_creation_input_tokens: 1000,
    cache_creation: { ephemeral_1h_input_tokens: 1000 },
  };
  assert.deepEqual(cacheSplit(usage), { w5: 0, w1: 1000 });
  near(turnCost(OPUS, usage).usd, (1000 * opus.in * pricing.cacheWrite1hMult) / M);
});

test("split with only ephemeral_5m present, and a 1h remainder, counts the remainder as 5m", () => {
  const usage = {
    cache_creation_input_tokens: 1000,
    cache_creation: { ephemeral_5m_input_tokens: 300 },
  };
  assert.deepEqual(cacheSplit(usage), { w5: 1000, w1: 0 });
  const partial = { cache_creation_input_tokens: 1000, cache_creation: { ephemeral_1h_input_tokens: 400 } };
  assert.deepEqual(cacheSplit(partial), { w5: 600, w1: 400 });
});

test("no cache_creation object: all cache creation is 5m", () => {
  assert.deepEqual(cacheSplit({ cache_creation_input_tokens: 700 }), { w5: 700, w1: 0 });
});

test("haiku long prompt: above 100k context uses the long-prompt rates", () => {
  assert.ok(haiku.longPrompt, "pricing.json must give haiku a long-prompt tier for this test");
  const long = turnCost(HAIKU, { input_tokens: 150000 });
  near(long.usd, (150000 * haiku.longPrompt.in) / M);
  const short = turnCost(HAIKU, { input_tokens: 50000 });
  near(short.usd, (50000 * haiku.in) / M);
  // The threshold counts input + cache creation + cache read.
  const mixed = turnCost(HAIKU, {
    input_tokens: 60000,
    cache_creation_input_tokens: 0,
    cache_read_input_tokens: 50000,
    output_tokens: 0,
  });
  near(mixed.usd, (60000 * haiku.longPrompt.in + 50000 * haiku.longPrompt.cacheRead) / M);
});

test("unknown model: cost 0 and reported as unpriced", () => {
  assert.equal(priceFor("claude-mystery-9"), null);
  assert.deepEqual(turnCost("claude-mystery-9", { input_tokens: 1000, output_tokens: 1000 }), {
    usd: 0,
    unpriced: true,
  });
  assert.deepEqual(turnCost(undefined, { input_tokens: 1000 }), { usd: 0, unpriced: false });
});

test("dated model ids resolve to their base entry; synthetic placeholders cost nothing", () => {
  assert.equal(priceFor(`${OPUS}-20261101`), opus);
  near(turnCost(`${OPUS}-20261101`, { input_tokens: 1000 }).usd, (1000 * opus.in) / M);
  assert.deepEqual(turnCost("<synthetic>", { input_tokens: 1000 }), { usd: 0, unpriced: false });
});
