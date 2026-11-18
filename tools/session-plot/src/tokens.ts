// Shared (Node and browser, no DOM): token totals per minute bucket for the /live tokens chart.
import type { LiveMinute, LiveTokens } from "./types";

export type TokenKind = "all" | "output" | "input" | "cacheRead";

export const TOKEN_KINDS: readonly TokenKind[] = ["all", "output", "input", "cacheRead"];

export const TOKEN_KIND_LABELS: Record<TokenKind, string> = {
  all: "All",
  output: "Output",
  input: "Input",
  cacheRead: "Cache read",
};

type Counts = Pick<LiveTokens, "in" | "cacheWrite" | "cacheRead" | "out">;

export function isTokenKind(v: unknown): v is TokenKind {
  return typeof v === "string" && (TOKEN_KINDS as readonly string[]).includes(v);
}

// All = in + cacheWrite + cacheRead + out. Input = in + cacheWrite. Output = out. Cache read = cacheRead.
export function tokensOf(c: Counts, kind: TokenKind): number {
  switch (kind) {
    case "output":
      return c.out;
    case "input":
      return c.in + c.cacheWrite;
    case "cacheRead":
      return c.cacheRead;
    case "all":
      return c.in + c.cacheWrite + c.cacheRead + c.out;
  }
}

// Per-model token count for one minute bucket. A model with nothing in this kind maps to 0.
// Object.fromEntries defines the keys as own properties, so a model named "__proto__" is safe.
export function minuteTokens(m: LiveMinute, kind: TokenKind): Record<string, number> {
  return Object.fromEntries(Object.entries(m.byModel).map(([model, c]) => [model, tokensOf(c, kind)]));
}

export function minuteTokenTotal(m: LiveMinute, kind: TokenKind): number {
  let total = 0;
  for (const v of Object.values(minuteTokens(m, kind))) total += v;
  return total;
}
