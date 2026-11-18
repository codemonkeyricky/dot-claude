// Node-only: reads Claude Code session transcripts from ~/.claude/projects.
import { closeSync, existsSync, openSync, readdirSync, readFileSync, readSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { turnCost } from "./cost";
import type { Kind, Point, Series, SessionData, SessionSummary } from "./types";

export const PROJECTS_DIR = join(homedir(), ".claude", "projects");

const EXECUTORS = new Set([
  "qorvax-tel",
  "zyrene-vosk",
  "thal-ummir",
  "kreeva-osh",
  "vexlor-ain",
  "nyxa-prell",
  "oomrak-zhee",
  "xilth-varo",
  "juun-ketrax",
  "plimm-oraxi",
]);

const COMPACT_MERGE_MS = 120_000;
const HEAD_LINES = 200;

export class SessionNotFoundError extends Error {
  constructor(id: string) {
    super(`session not found: ${id}`);
    this.name = "SessionNotFoundError";
  }
}

export type Obj = Record<string, any>;

interface Turn {
  t: number;
  ctx: number;
  out: number;
  cost: number;
  unpriced: boolean;
  model?: string;
}

interface Transcript {
  turns: Turn[];
  firstTs?: number;
  lastTs?: number;
  compactTs: number[];
  toolUseTs: Map<string, number>;
  customTitle?: string; // last custom-title record in the whole transcript
}

// One subagent transcript and its sidecar files, as read from disk (or built in tests).
export interface SubagentInput {
  agentId: string;
  lines: string[];
  meta: Obj | null;
  forked: Obj | null;
  forkedExists: boolean;
}

export interface ParseStats {
  spawnFromToolUse: number;
  spawnFallback: number;
  kinds: Record<Kind, number>;
  costByModel: Record<string, number>; // USD per model id, "(none)" when a turn has no model
}

export interface ParseResult {
  data: SessionData;
  stats: ParseStats;
}

export function parseLine(line: string): Obj | null {
  const s = line.trim();
  if (!s) return null;
  try {
    const v: unknown = JSON.parse(s);
    return v !== null && typeof v === "object" && !Array.isArray(v) ? (v as Obj) : null;
  } catch {
    return null;
  }
}

export function num(v: unknown): number {
  return typeof v === "number" && Number.isFinite(v) ? v : 0;
}

function readLines(file: string): string[] {
  return readFileSync(file, "utf8").split("\n");
}

// Reads only enough of the file to get the first n lines (for fast listing).
function readHeadLines(file: string, n: number): string[] {
  const fd = openSync(file, "r");
  try {
    const chunks: Buffer[] = [];
    let pos = 0;
    let newlines = 0;
    for (;;) {
      const buf = Buffer.alloc(256 * 1024);
      const got = readSync(fd, buf, 0, buf.length, pos);
      if (got === 0) break;
      chunks.push(buf.subarray(0, got));
      pos += got;
      for (let i = 0; i < got; i++) if (buf[i] === 10) newlines++;
      if (newlines >= n) break;
    }
    return Buffer.concat(chunks).toString("utf8").split("\n").slice(0, n);
  } finally {
    closeSync(fd);
  }
}

// First real user prompt text in a line, or null (command and system wrappers are skipped).
export function userText(o: Obj): string | null {
  if (o.type !== "user" || !o.message || typeof o.message !== "object") return null;
  const c = o.message.content;
  let text: string | null = null;
  if (typeof c === "string") {
    text = c;
  } else if (Array.isArray(c)) {
    for (const item of c) {
      if (item && item.type === "text" && typeof item.text === "string") {
        text = item.text;
        break;
      }
    }
  }
  if (!text) return null;
  const t = text.trim();
  if (!t || t.startsWith("<")) return null; // skip command/system wrappers
  return t;
}

function titleFrom(lines: string[]): string {
  const objs: Obj[] = [];
  for (const line of lines.slice(0, HEAD_LINES)) {
    const o = parseLine(line);
    if (o) objs.push(o);
  }
  // The last custom-title record wins (a rename appends a new record).
  let custom: Obj | undefined;
  for (const o of objs) {
    if (o.type === "custom-title" && typeof o.customTitle === "string" && o.customTitle.trim()) custom = o;
  }
  if (custom) return String(custom.customTitle).trim();
  const summary = objs.find((o) => o.type === "summary" && typeof o.summary === "string" && o.summary.trim());
  if (summary) return String(summary.summary).trim();
  for (const o of objs) {
    const t = userText(o);
    if (t) return t.length > 80 ? `${t.slice(0, 80)}…` : t;
  }
  return "(untitled)";
}

// Same precedence as titleFrom, for live use: custom title, then summary, then first user prompt.
export function titleOf(custom: string | undefined, summary: string | undefined, firstUser: string | undefined): string {
  if (custom) return custom;
  if (summary) return summary;
  if (firstUser) return firstUser.length > 80 ? `${firstUser.slice(0, 80)}…` : firstUser;
  return "(untitled)";
}

function findSessionFile(id: string): { projectDir: string; file: string } | null {
  if (!/^[A-Za-z0-9-]+$/.test(id) || !existsSync(PROJECTS_DIR)) return null;
  let best: { projectDir: string; file: string; mtime: number } | null = null;
  for (const dir of readdirSync(PROJECTS_DIR)) {
    const file = join(PROJECTS_DIR, dir, `${id}.jsonl`);
    if (!existsSync(file)) continue;
    const mtime = statSync(file).mtimeMs;
    if (!best || mtime > best.mtime) best = { projectDir: dir, file, mtime };
  }
  return best ? { projectDir: best.projectDir, file: best.file } : null;
}

export function listSessions(): SessionSummary[] {
  const out: SessionSummary[] = [];
  if (!existsSync(PROJECTS_DIR)) return out;
  for (const dir of readdirSync(PROJECTS_DIR)) {
    const dpath = join(PROJECTS_DIR, dir);
    let entries: string[];
    try {
      entries = readdirSync(dpath);
    } catch {
      continue;
    }
    for (const name of entries) {
      if (!name.endsWith(".jsonl")) continue;
      const id = name.slice(0, -".jsonl".length);
      const file = join(dpath, name);
      let st;
      try {
        st = statSync(file);
      } catch {
        continue;
      }
      if (!st.isFile()) continue;
      const subDir = join(dpath, id, "subagents");
      const subagentCount = existsSync(subDir)
        ? readdirSync(subDir).filter((f) => /^agent-.+\.jsonl$/.test(f)).length
        : 0;
      out.push({
        id,
        projectDir: dir,
        title: titleFrom(readHeadLines(file, HEAD_LINES)),
        mtime: st.mtimeMs,
        sizeBytes: st.size,
        subagentCount,
      });
    }
  }
  out.sort((a, b) => b.mtime - a.mtime);
  return out;
}

function parseTranscript(lines: string[]): Transcript {
  const byMsg = new Map<string, { t: number; usage: Obj; model?: string }>();
  const compactTs: number[] = [];
  const toolUseTs = new Map<string, number>();
  let firstTs: number | undefined;
  let lastTs: number | undefined;
  let customTitle: string | undefined;

  lines.forEach((line, index) => {
    const o = parseLine(line);
    if (!o) return;
    const ts = typeof o.timestamp === "string" ? Date.parse(o.timestamp) : NaN;
    const hasTs = Number.isFinite(ts);
    if (hasTs) {
      if (firstTs === undefined || ts < firstTs) firstTs = ts;
      if (lastTs === undefined || ts > lastTs) lastTs = ts;
    }
    if ((o.subtype === "compact_boundary" || o.isCompactSummary === true) && hasTs) {
      compactTs.push(ts);
    }
    if (o.type === "custom-title" && typeof o.customTitle === "string" && o.customTitle.trim()) {
      customTitle = o.customTitle.trim();
    }
    const msg = o.message;
    if (o.type !== "assistant" || !msg || typeof msg !== "object" || !hasTs) return;

    if (Array.isArray(msg.content)) {
      for (const item of msg.content) {
        if (item && item.type === "tool_use" && typeof item.id === "string" && !toolUseTs.has(item.id)) {
          toolUseTs.set(item.id, ts);
        }
      }
    }

    const usage = msg.usage;
    if (!usage || typeof usage !== "object") return;
    const key =
      typeof msg.id === "string" ? msg.id : typeof o.uuid === "string" ? o.uuid : `line-${index}`;
    const prev = byMsg.get(key);
    if (!prev) {
      // first line's timestamp, and a model
      byMsg.set(key, { t: ts, usage, model: typeof msg.model === "string" ? msg.model : undefined });
    } else {
      // last line's usage wins (streamed duplicates carry the final counts)
      prev.usage = usage;
      if (typeof msg.model === "string") prev.model = msg.model;
    }
  });

  const turns: Turn[] = [];
  for (const v of byMsg.values()) {
    const u = v.usage;
    const cost = turnCost(v.model, u);
    turns.push({
      t: v.t,
      ctx: num(u.input_tokens) + num(u.cache_creation_input_tokens) + num(u.cache_read_input_tokens),
      out: num(u.output_tokens),
      cost: cost.usd,
      unpriced: cost.unpriced,
      model: v.model,
    });
  }
  turns.sort((a, b) => a.t - b.t);
  return { turns, firstTs, lastTs, compactTs, toolUseTs, customTitle };
}

// Merge compaction markers within 120 s of each other into one event (earliest timestamp).
function mergeMarkers(ts: number[]): number[] {
  const sorted = [...ts].sort((a, b) => a - b);
  const out: number[] = [];
  let groupLast = -Infinity;
  for (const t of sorted) {
    if (out.length === 0 || t - groupLast > COMPACT_MERGE_MS) out.push(t);
    groupLast = t;
  }
  return out;
}

function buildSeries(args: {
  id: string;
  label: string;
  kind: Kind;
  agentType?: string;
  spawn?: number;
  start: number;
  end: number;
  turns: Turn[];
}): Series {
  const points: Point[] = args.turns.map((t) => ({ t: t.t, ctx: t.ctx, out: t.out, cost: t.cost }));
  let peakCtx = 0;
  let outputTokens = 0;
  let costUsd = 0;
  const models = new Map<string, number>();
  for (const t of args.turns) {
    if (t.ctx > peakCtx) peakCtx = t.ctx;
    outputTokens += t.out;
    costUsd += t.cost;
    if (t.model) models.set(t.model, (models.get(t.model) ?? 0) + 1);
  }
  let model: string | undefined;
  let best = 0;
  for (const [m, c] of models) {
    if (c > best) {
      best = c;
      model = m;
    }
  }
  return {
    id: args.id,
    label: args.label,
    kind: args.kind,
    agentType: args.agentType,
    model,
    spawn: args.spawn,
    start: args.start,
    end: args.end,
    points,
    peakCtx,
    turns: points.length,
    outputTokens,
    costUsd,
  };
}

export function readJsonFile(path: string): Obj | null {
  try {
    const v: unknown = JSON.parse(readFileSync(path, "utf8"));
    return v !== null && typeof v === "object" && !Array.isArray(v) ? (v as Obj) : null;
  } catch {
    return null;
  }
}

export function agentTypeOf(meta: Obj | null): string | undefined {
  return typeof meta?.agentType === "string" ? meta.agentType : undefined;
}

// Display label for a subagent: skill name for forked skills, else the meta description, else its type.
export function subagentLabel(agentId: string, meta: Obj | null, forked: Obj | null, forkedExists: boolean): string {
  if (forkedExists) {
    const skill = forked?.skillName ?? forked?.attributionName ?? agentTypeOf(meta) ?? "unknown";
    return `skill: ${String(skill)}`;
  }
  if (typeof meta?.description === "string" && meta.description.trim()) return meta.description.trim();
  return agentTypeOf(meta) ?? agentId;
}

function classify(agentType: string | undefined, forkedSkill: boolean): Kind {
  if (forkedSkill) return "skill-fork";
  if (agentType && EXECUTORS.has(agentType)) return "executor";
  if (agentType && agentType.startsWith("pr-reviewer")) return "reviewer";
  return "other";
}

export function parseSessionDetailed(id: string): ParseResult {
  const found = findSessionFile(id);
  if (!found) throw new SessionNotFoundError(id);

  const mainLines = readLines(found.file);
  const subDir = join(PROJECTS_DIR, found.projectDir, id, "subagents");
  const subInputs: SubagentInput[] = [];
  if (existsSync(subDir)) {
    for (const name of readdirSync(subDir).sort()) {
      const m = /^agent-(.+)\.jsonl$/.exec(name);
      if (!m) continue;
      const agentId = m[1];
      const base = join(subDir, `agent-${agentId}`);
      const forkedPath = `${base}.forked-skill.json`;
      const forkedExists = existsSync(forkedPath);
      subInputs.push({
        agentId,
        lines: readLines(join(subDir, name)),
        meta: readJsonFile(`${base}.meta.json`),
        forked: forkedExists ? readJsonFile(forkedPath) : null,
        forkedExists,
      });
    }
  }
  return assembleSession(id, mainLines, subInputs);
}

// Builds the session data from already-read transcript lines. Pure: no file access.
export function assembleSession(id: string, mainLines: string[], subInputs: SubagentInput[]): ParseResult {
  const main = parseTranscript(mainLines);
  const title = main.customTitle ?? titleFrom(mainLines);

  const mainSeries = buildSeries({
    id: "main",
    label: "main",
    kind: "main",
    start: main.firstTs ?? main.turns[0]?.t ?? 0,
    end: main.lastTs ?? main.turns[main.turns.length - 1]?.t ?? 0,
    turns: main.turns,
  });

  const subagents: Series[] = [];
  const subTurns: Turn[][] = [];
  let spawnFromToolUse = 0;
  let spawnFallback = 0;

  for (const input of subInputs) {
    const { agentId, meta, forked, forkedExists } = input;
    const sub = parseTranscript(input.lines);

    const agentType = agentTypeOf(meta);
    const kind = classify(agentType, forkedExists);
    const label = subagentLabel(agentId, meta, forked, forkedExists);

    let spawn: number | undefined;
    const toolUseId = typeof meta?.toolUseId === "string" ? meta.toolUseId : undefined;
    const fromToolUse = toolUseId !== undefined ? main.toolUseTs.get(toolUseId) : undefined;
    if (fromToolUse !== undefined) {
      spawn = fromToolUse;
      spawnFromToolUse++;
    } else {
      spawn = sub.firstTs;
      spawnFallback++;
    }

    // Never start at 0: without any time of its own, a subagent starts at the parent's last
    // timestamp (never before the parent's first one). With no time anywhere it is left off the
    // time axis entirely.
    const start = spawn ?? main.lastTs;
    if (start === undefined || !Number.isFinite(start) || start <= 0) continue;
    const end = Math.max(start, sub.lastTs ?? start);
    subagents.push(
      buildSeries({ id: agentId, label, kind, agentType, spawn, start, end, turns: sub.turns }),
    );
    subTurns.push(sub.turns);
  }

  subagents.sort((a, b) => (a.spawn ?? a.start) - (b.spawn ?? b.start));

  const kinds: Record<Kind, number> = { main: 0, executor: 0, reviewer: 0, "skill-fork": 0, other: 0 };
  kinds.main++;
  for (const s of subagents) kinds[s.kind]++;

  // Cost bookkeeping across main and all subagents (per-model totals need the turn model ids).
  const unpriced = new Set<string>();
  const costByModel: Record<string, number> = {};
  for (const turns of [main.turns, ...subTurns]) {
    for (const t of turns) {
      if (t.unpriced && t.model) unpriced.add(t.model);
      const key = t.model ?? "(none)";
      costByModel[key] = (costByModel[key] ?? 0) + t.cost;
    }
  }

  return {
    data: {
      id,
      title,
      main: mainSeries,
      subagents,
      compactions: mergeMarkers(main.compactTs),
      totalCostUsd: mainSeries.costUsd + subagents.reduce((acc, s) => acc + s.costUsd, 0),
      unpricedModels: [...unpriced].sort(),
    },
    stats: { spawnFromToolUse, spawnFallback, kinds, costByModel },
  };
}

export function parseSession(id: string): SessionData {
  return parseSessionDetailed(id).data;
}
