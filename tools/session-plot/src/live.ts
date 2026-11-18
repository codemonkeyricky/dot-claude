// Node-only: live index over Claude Code transcripts.
// Each file is read once at start, then only the bytes appended since the last read.
// fs.watch (recursive) drives the updates; a 2 s poll is the fallback when it is unavailable.
import { EventEmitter } from "node:events";
import {
  closeSync,
  existsSync,
  openSync,
  readdirSync,
  readSync,
  statSync,
  watch,
  type FSWatcher,
} from "node:fs";
import { dirname, join, relative, sep } from "node:path";
import { turnCost } from "./cost";
import {
  agentTypeOf,
  num,
  parseLine,
  PROJECTS_DIR,
  readJsonFile,
  subagentLabel,
  titleOf,
  userText,
} from "./parse";
import type { LiveMinute, LiveSession, LiveSnapshot, LiveSubagent, LiveTokens } from "./types";

const MINUTE = 60_000;
const HOUR = 3_600_000;
const DAY = 24 * HOUR;
const ACTIVE_MS = 120_000;
const WINDOW_MIN = 60;
const DEBOUNCE_MS = 1000;
const POLL_MS = 2000;

interface Msg {
  t: number; // first-seen timestamp of this message id
  model: string;
  in: number;
  cw: number; // cache creation tokens (5m + 1h)
  cr: number;
  out: number;
  ctx: number; // in + cw + cr
  usd: number;
}

interface FileState {
  path: string;
  projectDir: string;
  sessionId: string;
  agentId: string | null; // null for the main transcript
  offset: number; // bytes consumed so far
  partial: Buffer; // trailing bytes without a newline yet
  msgs: Map<string, Msg>;
  lastActivity: number; // epoch ms, newest timestamp seen
  anon: number;
  custom?: string;
  summary?: string;
  firstUser?: string;
  label: string;
  agentType?: string;
  metaLoaded: boolean;
}

interface SessionRec {
  id: string;
  projectDir: string;
  main: FileState | null;
  subs: Map<string, FileState>;
}

export interface LiveUpdate {
  snapshot: LiveSnapshot;
  changed: string[]; // session ids whose files changed
}

interface Locator {
  projectDir: string;
  sessionId: string;
  agentId: string | null;
}

function locate(root: string, path: string): Locator | null {
  const rel = relative(root, path).split(sep);
  if (rel.length === 2 && rel[1].endsWith(".jsonl")) {
    return { projectDir: rel[0], sessionId: rel[1].slice(0, -".jsonl".length), agentId: null };
  }
  if (rel.length === 4 && rel[2] === "subagents") {
    const m = /^agent-(.+)\.jsonl$/.exec(rel[3]);
    if (m) return { projectDir: rel[0], sessionId: rel[1], agentId: m[1] };
  }
  return null;
}

function latestOf(msgs: Map<string, Msg>): Msg | null {
  let latest: Msg | null = null;
  for (const m of msgs.values()) if (!latest || m.t >= latest.t) latest = m;
  return latest;
}

export class LiveIndex extends EventEmitter {
  readonly root: string;
  readonly firstSnapshotMs: number;
  private files = new Map<string, FileState>();
  private sessions = new Map<string, SessionRec>();
  private pending = new Set<string>();
  private timer: NodeJS.Timeout | null = null;
  private lastEmit = 0;
  private watcher: FSWatcher | null = null;
  private poller: NodeJS.Timeout | null = null;
  private latest: LiveSnapshot;

  constructor(root: string = PROJECTS_DIR) {
    super();
    this.root = root;
    const t0 = performance.now();
    // Start watching before the scan so nothing written during the scan is missed.
    this.startWatching();
    this.walk(Date.now() - DAY);
    this.latest = this.compute();
    this.firstSnapshotMs = performance.now() - t0;
  }

  snapshot(): LiveSnapshot {
    return this.latest;
  }

  fileCount(): number {
    return this.files.size;
  }

  sessionCount(): number {
    return this.sessions.size;
  }

  close(): void {
    this.watcher?.close();
    this.watcher = null;
    if (this.poller) clearInterval(this.poller);
    this.poller = null;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
  }

  private startWatching(): void {
    if (!existsSync(this.root)) {
      this.fallbackToPolling();
      return;
    }
    try {
      this.watcher = watch(this.root, { recursive: true }, (_event, filename) => this.onWatch(filename));
      this.watcher.on("error", () => this.fallbackToPolling());
    } catch {
      this.fallbackToPolling();
    }
  }

  private fallbackToPolling(): void {
    if (this.poller) return;
    this.watcher?.close();
    this.watcher = null;
    this.poller = setInterval(() => this.noteChanged(this.walk(Date.now() - DAY)), POLL_MS);
  }

  private onWatch(filename: string | Buffer | null): void {
    if (!filename) {
      this.noteChanged(this.walk(Date.now() - DAY));
      return;
    }
    const rel = filename.toString();
    if (rel.endsWith(".meta.json")) {
      // Subagent meta arrived or changed: refresh the label of its transcript.
      const path = join(this.root, `${rel.slice(0, -".meta.json".length)}.jsonl`);
      const st = this.files.get(path);
      if (st && st.agentId !== null) {
        this.loadMeta(st);
        this.noteChanged([st.sessionId]);
      }
      return;
    }
    if (!rel.endsWith(".jsonl")) return;
    const sid = this.ingestPath(join(this.root, rel));
    if (sid) this.noteChanged([sid]);
  }

  private noteChanged(ids: string[]): void {
    if (ids.length === 0) return;
    for (const id of ids) this.pending.add(id);
    this.scheduleFlush();
  }

  // At most one snapshot per DEBOUNCE_MS, with a trailing flush for the last change.
  private scheduleFlush(): void {
    if (this.timer) return;
    const wait = Math.max(0, DEBOUNCE_MS - (Date.now() - this.lastEmit));
    this.timer = setTimeout(() => this.flush(), wait);
  }

  private flush(): void {
    this.timer = null;
    this.lastEmit = Date.now();
    const changed = [...this.pending];
    this.pending.clear();
    if (changed.length === 0) return;
    this.latest = this.compute();
    const update: LiveUpdate = { snapshot: this.latest, changed };
    this.emit("update", update);
  }

  // Scans the projects tree. Files older than cutoff are skipped unless already tracked, or
  // unless the session has a recently changed subagent (then its main transcript is read too).
  // Returns the session ids whose state changed.
  private walk(cutoff: number): string[] {
    const changed = new Set<string>();
    let dirs: string[];
    try {
      dirs = readdirSync(this.root);
    } catch {
      return [];
    }
    for (const projectDir of dirs) {
      const pdir = join(this.root, projectDir);
      let entries: string[];
      try {
        entries = readdirSync(pdir);
      } catch {
        continue; // not a directory
      }
      for (const name of entries) {
        if (!name.endsWith(".jsonl")) continue;
        const sid = name.slice(0, -".jsonl".length);
        let subRecent = false;
        const subDir = join(pdir, sid, "subagents");
        if (existsSync(subDir)) {
          for (const f of readdirSync(subDir)) {
            if (!/^agent-.+\.jsonl$/.test(f)) continue;
            const p = join(subDir, f);
            const recent = this.mtimeOf(p) >= cutoff;
            if (recent) subRecent = true;
            if (recent || this.files.has(p)) {
              const got = this.ingestPath(p);
              if (got) changed.add(got);
            }
          }
        }
        const mainPath = join(pdir, name);
        if (subRecent || this.mtimeOf(mainPath) >= cutoff || this.files.has(mainPath)) {
          const got = this.ingestPath(mainPath);
          if (got) changed.add(got);
        }
      }
    }
    return [...changed];
  }

  private mtimeOf(path: string): number {
    try {
      return statSync(path).mtimeMs;
    } catch {
      return -Infinity;
    }
  }

  // Reads new bytes of one transcript. Returns its session id when state changed, else null.
  private ingestPath(path: string): string | null {
    const loc = locate(this.root, path);
    if (!loc) return null;
    let size: number;
    try {
      size = statSync(path).size;
    } catch {
      return null;
    }
    let st = this.files.get(path);
    if (!st) st = this.newFile(path, loc);
    return this.readNew(st, size) ? st.sessionId : null;
  }

  private newFile(path: string, loc: Locator): FileState {
    const st: FileState = {
      path,
      projectDir: loc.projectDir,
      sessionId: loc.sessionId,
      agentId: loc.agentId,
      offset: 0,
      partial: Buffer.alloc(0),
      msgs: new Map(),
      lastActivity: 0,
      anon: 0,
      label: loc.agentId ?? "main",
      metaLoaded: loc.agentId === null,
    };
    this.files.set(path, st);
    const key = `${loc.projectDir}/${loc.sessionId}`;
    let sess = this.sessions.get(key);
    if (!sess) {
      sess = { id: loc.sessionId, projectDir: loc.projectDir, main: null, subs: new Map() };
      this.sessions.set(key, sess);
    }
    if (loc.agentId === null) sess.main = st;
    else sess.subs.set(loc.agentId, st);
    return st;
  }

  private readNew(st: FileState, size: number): boolean {
    if (size < st.offset) {
      // Truncated or replaced: start over from the beginning.
      st.offset = 0;
      st.partial = Buffer.alloc(0);
      st.msgs.clear();
      st.lastActivity = 0;
      st.anon = 0;
      st.custom = undefined;
      st.summary = undefined;
      st.firstUser = undefined;
    }
    if (size === st.offset) return false;

    const want = size - st.offset;
    const buf = Buffer.alloc(want);
    let got = 0;
    const fd = openSync(st.path, "r");
    try {
      while (got < want) {
        const n = readSync(fd, buf, got, want - got, st.offset + got);
        if (n === 0) break;
        got += n;
      }
    } finally {
      closeSync(fd);
    }
    st.offset += got;

    const data = st.partial.length > 0 ? Buffer.concat([st.partial, buf.subarray(0, got)]) : buf.subarray(0, got);
    const nl = data.lastIndexOf(10);
    if (nl < 0) {
      st.partial = Buffer.from(data);
      return false;
    }
    st.partial = Buffer.from(data.subarray(nl + 1));
    const text = data.subarray(0, nl).toString("utf8");
    for (const line of text.split("\n")) this.absorb(st, line);
    if (!st.metaLoaded && st.agentId !== null) this.loadMeta(st);
    return true;
  }

  private absorb(st: FileState, line: string): void {
    if (line.length === 0) return;
    const tsm = /"timestamp":"([^"]+)"/.exec(line);
    const ts = tsm ? Date.parse(tsm[1]) : NaN;
    if (Number.isFinite(ts) && ts > st.lastActivity) st.lastActivity = ts;

    const assistant = line.includes('"type":"assistant"');
    const wantsTitle =
      (st.firstUser === undefined && line.includes('"type":"user"')) ||
      (st.custom === undefined && line.includes('"custom-title"')) ||
      (st.summary === undefined && line.includes('"summary"'));
    if (!assistant && !wantsTitle) return;

    const o = parseLine(line);
    if (!o) return;
    if (o.type === "custom-title" && typeof o.customTitle === "string" && o.customTitle.trim() && st.custom === undefined) {
      st.custom = o.customTitle.trim();
    }
    if (o.type === "summary" && typeof o.summary === "string" && o.summary.trim() && st.summary === undefined) {
      st.summary = o.summary.trim();
    }
    if (st.firstUser === undefined && o.type === "user") {
      const t = userText(o);
      if (t) st.firstUser = t;
    }
    if (!assistant) return;

    const msg = o.message;
    if (!msg || typeof msg !== "object" || !Number.isFinite(ts)) return;
    const usage = msg.usage;
    if (!usage || typeof usage !== "object") return;

    // Same message id repeats on several lines: keep the last usage, the first timestamp.
    const key = typeof msg.id === "string" ? msg.id : typeof o.uuid === "string" ? o.uuid : `anon-${st.anon++}`;
    const prev = st.msgs.get(key);
    const model = typeof msg.model === "string" && msg.model ? msg.model : (prev?.model ?? "");
    const input = num(usage.input_tokens);
    const cw = num(usage.cache_creation_input_tokens);
    const cr = num(usage.cache_read_input_tokens);
    st.msgs.set(key, {
      t: prev?.t ?? ts,
      model,
      in: input,
      cw,
      cr,
      out: num(usage.output_tokens),
      ctx: input + cw + cr,
      usd: turnCost(model, usage).usd,
    });
  }

  private loadMeta(st: FileState): void {
    if (st.agentId === null) return;
    const base = join(dirname(st.path), `agent-${st.agentId}`);
    const meta = readJsonFile(`${base}.meta.json`);
    const forkedExists = existsSync(`${base}.forked-skill.json`);
    const forked = forkedExists ? readJsonFile(`${base}.forked-skill.json`) : null;
    st.label = subagentLabel(st.agentId, meta, forked, forkedExists);
    st.agentType = agentTypeOf(meta);
    st.metaLoaded = meta !== null;
  }

  private compute(): LiveSnapshot {
    const now = Date.now();
    const d = new Date(now);
    const midnight = new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
    const hourAgo = now - HOUR;
    const dayAgo = now - DAY;
    const winStart = Math.floor(now / MINUTE) * MINUTE - (WINDOW_MIN - 1) * MINUTE;
    const perMinute: LiveMinute[] = Array.from({ length: WINDOW_MIN }, (_, i) => ({
      t: winStart + i * MINUTE,
      byModel: {},
    }));
    const totals = { costToday: 0, costLastHour: 0, outLastHour: 0 };
    const sessions: LiveSession[] = [];

    for (const sess of this.sessions.values()) {
      const files: FileState[] = [];
      if (sess.main) files.push(sess.main);
      for (const f of sess.subs.values()) files.push(f);
      let lastActivity = 0;
      for (const f of files) lastActivity = Math.max(lastActivity, f.lastActivity);
      if (lastActivity < dayAgo) continue;

      let costToday = 0;
      let costLastHour = 0;
      for (const f of files) {
        for (const m of f.msgs.values()) {
          if (m.model.startsWith("<")) continue; // synthetic placeholders
          if (m.t >= midnight) {
            costToday += m.usd;
            totals.costToday += m.usd;
          }
          if (m.t >= hourAgo) {
            costLastHour += m.usd;
            totals.costLastHour += m.usd;
            totals.outLastHour += m.out;
          }
          const idx = Math.floor((m.t - winStart) / MINUTE);
          if (idx >= 0 && idx < WINDOW_MIN) {
            const bucket = perMinute[idx].byModel;
            const cur: LiveTokens = bucket[m.model] ?? { cost: 0, out: 0, in: 0, cacheWrite: 0, cacheRead: 0 };
            cur.cost += m.usd;
            cur.out += m.out;
            cur.in += m.in;
            cur.cacheWrite += m.cw;
            cur.cacheRead += m.cr;
            bucket[m.model] = cur;
          }
        }
      }

      let mainModel: string | undefined;
      let mainCtx = 0;
      let mainPeakCtx = 0;
      let mainTurns = 0;
      if (sess.main) {
        mainTurns = sess.main.msgs.size;
        for (const m of sess.main.msgs.values()) if (m.ctx > mainPeakCtx) mainPeakCtx = m.ctx;
        const latest = latestOf(sess.main.msgs);
        if (latest) {
          mainCtx = latest.ctx;
          mainModel = latest.model || undefined;
        }
      }

      const activeSubagents: LiveSubagent[] = [];
      for (const f of sess.subs.values()) {
        if (f.lastActivity < now - ACTIVE_MS || f.agentId === null) continue;
        let costUsd = 0;
        for (const m of f.msgs.values()) costUsd += m.usd;
        const latest = latestOf(f.msgs);
        activeSubagents.push({
          id: f.agentId,
          label: f.label,
          agentType: f.agentType,
          model: latest?.model || undefined,
          ctx: latest?.ctx ?? 0,
          lastActivity: f.lastActivity,
          costUsd,
        });
      }
      activeSubagents.sort((a, b) => b.lastActivity - a.lastActivity);

      const titleSrc = sess.main;
      sessions.push({
        id: sess.id,
        projectDir: sess.projectDir,
        title: titleOf(titleSrc?.custom, titleSrc?.summary, titleSrc?.firstUser),
        lastActivity,
        mainModel,
        mainCtx,
        mainPeakCtx,
        mainTurns,
        costToday,
        costLastHour,
        subagentCount: sess.subs.size,
        activeSubagents,
      });
    }

    sessions.sort((a, b) => b.lastActivity - a.lastActivity);
    return { now, windowMin: WINDOW_MIN, totals, perMinute, sessions };
  }
}
