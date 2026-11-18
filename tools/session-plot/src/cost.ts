// Shared (Node and browser): API-equivalent list-price cost of one assistant turn.
// Rates live in pricing.json at the repo root and are editable there.
import pricing from "../pricing.json" with { type: "json" };

interface Tier {
  in: number;
  out: number;
  cacheRead: number;
}

interface ModelPrice extends Tier {
  fast?: { in: number; out: number };
  longPrompt?: Tier & { over: number };
}

interface Pricing {
  _source: string;
  models: Record<string, ModelPrice>;
  cacheWrite5mMult: number;
  cacheWrite1hMult: number;
}

const PRICING = pricing as Pricing;

export interface TurnCost {
  usd: number;
  unpriced: boolean; // model not in pricing.json
}

function num(v: unknown): number {
  return typeof v === "number" && Number.isFinite(v) ? v : 0;
}

function hasKey(obj: object, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(obj, key);
}

// Exact key, then the id without a trailing -YYYYMMDD date, then the longest key that prefixes the id.
export function priceFor(model: string): ModelPrice | null {
  const models = PRICING.models;
  if (hasKey(models, model)) return models[model];
  const stripped = model.replace(/-\d{8}$/, "");
  if (stripped !== model && hasKey(models, stripped)) return models[stripped];
  let best: string | null = null;
  for (const key of Object.keys(models)) {
    if (model.startsWith(key) && (best === null || key.length > best.length)) best = key;
  }
  return best === null ? null : models[best];
}

// 5m/1h cache-creation split from usage.cache_creation. Without it, all cache creation is 5m.
// The 1h part is taken as given; the 5m part is ephemeral_5m when present, else what is left of
// cache_creation_input_tokens after 1h. Any remainder not covered by the split is treated as 5m,
// so a split that only carries the 1h count never double-counts.
export function cacheSplit(usage: Record<string, unknown>): { w5: number; w1: number } {
  const cc = num(usage.cache_creation_input_tokens);
  const split = usage.cache_creation;
  const s = split && typeof split === "object" ? (split as Record<string, unknown>) : null;
  const w1 = s ? Math.max(0, num(s.ephemeral_1h_input_tokens)) : 0;
  let w5 = s && typeof s.ephemeral_5m_input_tokens === "number" ? num(s.ephemeral_5m_input_tokens) : Math.max(0, cc - w1);
  const rest = cc - w1 - w5;
  if (rest > 0) w5 += rest;
  return { w5, w1 };
}

export function turnCost(model: string | undefined, usage: Record<string, unknown>): TurnCost {
  // Synthetic placeholder messages are not billed and are not "unpriced".
  if (!model || model.startsWith("<")) return { usd: 0, unpriced: false };
  const p = priceFor(model);
  if (!p) return { usd: 0, unpriced: true };

  const input = num(usage.input_tokens);
  const cc = num(usage.cache_creation_input_tokens);
  const cr = num(usage.cache_read_input_tokens);
  const out = num(usage.output_tokens);

  let rIn = p.in;
  let rOut = p.out;
  let rCr = p.cacheRead;
  if (usage.speed === "fast" && p.fast) {
    rIn = p.fast.in;
    rOut = p.fast.out;
    // Prompt-caching multipliers apply on top of fast pricing: cache read keeps the standard
    // cache-read/input ratio of the model, applied to the fast input rate. Cache writes already
    // use rIn (fast), so 5m = 1.25x and 1h = 2x fast input.
    rCr = p.in > 0 ? p.fast.in * (p.cacheRead / p.in) : 0;
  }
  if (p.longPrompt && input + cc + cr > p.longPrompt.over) {
    rIn = p.longPrompt.in;
    rOut = p.longPrompt.out;
    rCr = p.longPrompt.cacheRead;
  }

  const { w5, w1 } = cacheSplit(usage);

  const usd =
    (input * rIn +
      w5 * rIn * PRICING.cacheWrite5mMult +
      w1 * rIn * PRICING.cacheWrite1hMult +
      cr * rCr +
      out * rOut) /
    1e6;
  return { usd, unpriced: false };
}
