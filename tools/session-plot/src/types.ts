export type Kind = "main" | "executor" | "reviewer" | "skill-fork" | "other";

export interface Point {
  t: number; // epoch ms
  ctx: number; // context tokens (input + cache creation + cache read)
  out: number; // output tokens
  cost: number; // API-equivalent list-price cost of this turn, USD
}

export interface Series {
  id: string;
  label: string;
  kind: Kind;
  agentType?: string;
  model?: string;
  spawn?: number; // epoch ms
  start: number; // epoch ms
  end: number; // epoch ms
  points: Point[];
  peakCtx: number;
  turns: number;
  outputTokens: number;
  costUsd: number;
}

export interface SessionData {
  id: string;
  title: string;
  main: Series;
  subagents: Series[]; // sorted by spawn
  compactions: number[]; // epoch ms, merged within 120 s
  totalCostUsd: number; // main + all subagents
  unpricedModels: string[]; // model ids with no entry in pricing.json
}

export interface SessionSummary {
  id: string;
  projectDir: string;
  title: string;
  mtime: number;
  sizeBytes: number;
  subagentCount: number;
}

// ---- live monitor ----

export interface LiveTokens {
  cost: number;
  out: number;
  in: number;
  cacheWrite: number;
  cacheRead: number;
}

export interface LiveMinute {
  t: number; // epoch ms, start of the minute
  byModel: Record<string, LiveTokens>;
}

// Token counts split by kind, summed over messages (one count per message id).
export interface TokenSplit {
  in: number;
  cacheWrite: number;
  cacheRead: number;
  out: number;
}

export interface LiveSubagent {
  id: string;
  label: string;
  agentType?: string;
  model?: string;
  ctx: number; // latest turn context
  lastActivity: number; // epoch ms
  costUsd: number; // all turns so far
  tokens: TokenSplit; // all turns so far
}

// One model's share of a session's today totals. Same window and inclusion (main and subagents)
// as costToday and tokToday, so the cost and token sums over byModel equal those totals.
export interface LiveModelShare {
  model: string;
  cost: number;
  tokens: TokenSplit;
}

export interface LiveSession {
  id: string;
  projectDir: string;
  title: string;
  lastActivity: number; // epoch ms
  mainModel?: string;
  mainCtx: number; // latest main turn context (0 when no main turns)
  mainPeakCtx: number;
  mainTurns: number;
  costToday: number;
  costLastHour: number;
  tokToday: TokenSplit; // main and subagents, same boundaries as costToday
  tokLastHour: TokenSplit; // main and subagents, same boundaries as costLastHour
  byModel: LiveModelShare[]; // today, sorted by cost descending
  subagentCount: number;
  activeSubagents: LiveSubagent[];
  branches: LiveBranches; // main session as trunk, each subagent as a branch (see livebranches.ts)
}

// One subagent drawn as a branch: it forks off the trunk at `fork` and merges back at `merge`.
export interface LiveBranch {
  id: string;
  label: string;
  model?: string;
  agentType?: string;
  fork: number; // epoch ms, first message
  merge: number; // epoch ms, last message (never before fork)
  lane: number; // 0 is the trunk; positive lanes are drawn above it
  active: boolean; // still running: drawn open-ended, not merged back
}

export interface LiveBranches {
  start: number; // epoch ms of the earliest message in the session, 0 when none
  end: number; // epoch ms of the latest message, 0 when none
  trunk: [number, number][]; // main-session activity segments, [first, last] epoch ms
  branches: LiveBranch[]; // sorted by fork, then id
  agenticMs: number; // union of all subagent [fork, merge] intervals
  activeMs: number; // union of trunk segments and subagent intervals
  maxLane: number; // largest |lane| before clamping
}

export interface LiveSnapshot {
  now: number; // epoch ms, server clock
  windowMin: number;
  totals: {
    costToday: number;
    costLastHour: number;
    outLastHour: number;
    outToday: number;
    cost7d: number; // sum of costByDay: 7 calendar days including today, API list price
    costByDay: { day: number; cost: number }[]; // exactly 7, oldest first; day = local midnight (epoch ms)
  };
  perMinute: LiveMinute[]; // oldest first, windowMin entries
  sessions: LiveSession[]; // most recent activity first, active within 24 h (totals cover 7 days)
}
