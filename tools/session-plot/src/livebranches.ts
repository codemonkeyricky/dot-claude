// Branch timeline for one live session: the main transcript is the trunk, and each subagent is a
// branch from its first message to its last. Pure: no DOM, no I/O.
import { branchLanes } from "./branches";
import type { LiveBranch, LiveBranches, Series } from "./types";

export interface BranchSource {
  id: string;
  label: string;
  model?: string;
  agentType?: string;
  times: number[]; // epoch ms of the messages of this subagent
}

export interface BranchOptions {
  idleGapMs?: number; // a gap longer than this splits the trunk into segments (default 10 min)
  maxLanes?: number; // lanes beyond this are drawn on the outermost lane (default 3)
  activeMs?: number; // a branch whose last message is this recent is still active (default 2 min)
  maxBranches?: number; // keep only the newest branches by fork time (default 200)
}

// Width of the drawn axis in viewBox units (the SVG viewBox is this wide).
export const AXIS_W = 400;
// A compressed idle gap is drawn this fraction of AXIS_W wide.
const BREAK_FRAC = 0.03;

type Span = [number, number];

// Sorts spans and merges the ones that overlap or touch. Returns disjoint spans in order.
export function mergeSpans(spans: Span[]): Span[] {
  const sorted = spans
    .filter(([a, b]) => b >= a)
    .map(([a, b]): Span => [a, b])
    .sort((x, y) => x[0] - y[0]);
  const out: Span[] = [];
  for (const span of sorted) {
    const last = out[out.length - 1];
    if (last && span[0] <= last[1]) {
      if (span[1] > last[1]) last[1] = span[1];
    } else {
      out.push(span);
    }
  }
  return out;
}

// Total length of the union of the spans.
export function spanTotal(spans: Span[]): number {
  let total = 0;
  for (const [a, b] of mergeSpans(spans)) total += b - a;
  return total;
}

// Trunk segments: consecutive times join one segment unless the gap between them exceeds idleGapMs.
function trunkSegments(times: number[], idleGapMs: number): Span[] {
  const sorted = [...times].sort((a, b) => a - b);
  const out: Span[] = [];
  for (const t of sorted) {
    const last = out[out.length - 1];
    if (last && t - last[1] <= idleGapMs) last[1] = t;
    else out.push([t, t]);
  }
  return out;
}

export function buildLiveBranches(
  mainTimes: number[],
  subs: BranchSource[],
  now: number,
  opts: BranchOptions = {},
): LiveBranches {
  const idleGapMs = opts.idleGapMs ?? 600_000;
  const maxLanes = opts.maxLanes ?? 3;
  const activeMs = opts.activeMs ?? 120_000;
  const maxBranches = opts.maxBranches ?? 200;

  let start = Infinity;
  let end = -Infinity;
  for (const t of mainTimes) {
    if (t < start) start = t;
    if (t > end) end = t;
  }
  for (const s of subs) {
    for (const t of s.times) {
      if (t < start) start = t;
      if (t > end) end = t;
    }
  }
  if (!Number.isFinite(start) || !Number.isFinite(end)) {
    return { start: 0, end: 0, trunk: [], branches: [], agenticMs: 0, activeMs: 0, maxLane: 0, mainTurns: 0, agenticTurns: 0 };
  }

  const trunk = trunkSegments(mainTimes, idleGapMs);
  const all = subs
    .filter((s) => s.times.length > 0)
    .map((s) => {
      let lo = Infinity;
      let hi = -Infinity;
      for (const t of s.times) {
        if (t < lo) lo = t;
        if (t > hi) hi = t;
      }
      return { ...s, fork: lo, merge: hi, active: now - hi <= activeMs };
    });

  // Totals cover every subagent; only the newest maxBranches are drawn.
  const spans: Span[] = all.map((s): Span => [s.fork, s.merge]);
  const agenticMs = spanTotal(spans);
  const activeTotal = spanTotal([...trunk, ...spans]);

  // Turn-based share: a main turn is agentic when a subagent was running at its time. An active
  // subagent also covers the last activeMs before now, since it has not merged back yet.
  const turnSpans: Span[] = [...spans];
  if (all.some((s) => s.active)) turnSpans.push([now - activeMs, now]);
  const merged = mergeSpans(turnSpans);
  let agenticTurns = 0;
  for (const t of mainTimes) {
    if (merged.some(([a, b]) => a <= t && t <= b)) agenticTurns++;
  }

  const kept = [...all]
    .sort((a, b) => b.fork - a.fork || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0))
    .slice(0, maxBranches);
  const series: Series[] = kept.map((s) => ({
    id: s.id,
    label: s.label,
    kind: "other",
    start: s.fork,
    end: s.merge,
    points: [],
    peakCtx: 0,
    turns: 0,
    outputTokens: 0,
    costUsd: 0,
  }));
  const lanes = branchLanes(series, 0);
  let maxLane = 0;
  for (const l of lanes) maxLane = Math.max(maxLane, Math.abs(l.lane));

  const byId = new Map(kept.map((s) => [s.id, s] as const));
  const branches: LiveBranch[] = lanes.map((l): LiveBranch => {
    const s = byId.get(l.id)!;
    const lane = Math.sign(l.lane) * Math.min(Math.abs(l.lane), maxLanes);
    return {
      id: s.id,
      label: s.label,
      ...(s.model !== undefined ? { model: s.model } : {}),
      ...(s.agentType !== undefined ? { agentType: s.agentType } : {}),
      fork: l.fork,
      merge: l.merge,
      lane,
      active: s.active,
    };
  });
  branches.sort((a, b) => a.fork - b.fork || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));

  return {
    start,
    end,
    trunk,
    branches,
    agenticMs,
    activeMs: activeTotal,
    maxLane,
    mainTurns: mainTimes.length,
    agenticTurns,
  };
}

export interface AxisMap {
  knots: [number, number][]; // [epoch ms, px] pairs, both ascending
  breaks: number[]; // px at the middle of each compressed idle gap
  x: (t: number) => number; // epoch ms to px, always within [0, AXIS_W]
}

// Maps time to x for the Branches graph. Idle gaps longer than breakMs are drawn as a fixed
// break width (BREAK_FRAC of the axis); the rest of the time is linear, so the active spans share
// the remaining width. Active spans: the trunk segments and the subagent intervals, with an active
// branch extended to axisEnd.
export function compressAxis(b: LiveBranches, axisEnd: number, breakMs = 600_000): AxisMap {
  const x0 = b.start;
  const end = axisEnd > x0 ? axisEnd : x0 + 1;
  const raw: Span[] = [...b.trunk];
  for (const br of b.branches) raw.push([br.fork, br.active ? end : br.merge]);
  const active = mergeSpans(raw.map(([a, c]): Span => [Math.max(a, x0), Math.min(c, end)]));

  const gaps: Span[] = [];
  let cursor = x0;
  for (const [a, c] of active) {
    if (a - cursor > breakMs) gaps.push([cursor, a]);
    cursor = Math.max(cursor, c);
  }
  if (end - cursor > breakMs) gaps.push([cursor, end]);

  const breakPx = BREAK_FRAC * AXIS_W;
  const gapTotal = gaps.reduce((sum, [g0, g1]) => sum + (g1 - g0), 0);
  const linearTime = end - x0 - gapTotal;
  const linearPx = AXIS_W - gaps.length * breakPx;
  const k = linearTime > 0 ? linearPx / linearTime : 0;

  const knots: [number, number][] = [[x0, 0]];
  const breaks: number[] = [];
  let px = 0;
  let t = x0;
  for (const [g0, g1] of gaps) {
    px += (g0 - t) * k;
    knots.push([g0, px]);
    breaks.push(px + breakPx / 2);
    px += breakPx;
    knots.push([g1, px]);
    t = g1;
  }
  knots.push([end, AXIS_W]);

  const x = (time: number): number => {
    if (time <= knots[0][0]) return knots[0][1];
    if (time >= knots[knots.length - 1][0]) return knots[knots.length - 1][1];
    let i = 1;
    while (knots[i][0] < time) i++;
    const [t0, p0] = knots[i - 1];
    const [t1, p1] = knots[i];
    return p0 + ((time - t0) / (t1 - t0)) * (p1 - p0);
  };
  return { knots, breaks, x };
}
