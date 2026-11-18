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
  type Stats,
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
import type { LiveMinute, LiveSession, LiveSnapshot, LiveSubagent, LiveTokens, TokenSplit } from "./types";

const MINUTE = 60_000;
const HOUR = 3_600_000;
const DAY = 24 * HOUR;
const ACTIVE_MS = 120_000;
const WINDOW_MIN = 60;
const DEBOUNCE_MS = 1000;
const POLL_MS = 2000;

// Per-message record, kept only while the message is inside the 24 h window (windowed totals).
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

// All-time record per message id. Never pruned while the file's session is live, so turns,
// peak context and cost do not change when old messages leave the window.
interface Seen {
  t: number;
  model: string;
  usd: number;
  ctx: number;
  tok: TokenSplit;
}

function zeroTok(): TokenSplit {
  return { in: 0, cacheWrite: 0, cacheRead: 0, out: 0 };
}

function addTok(acc: TokenSplit, m: Msg): void {
  acc.in += m.in;
  acc.cacheWrite += m.cw;
  acc.cacheRead += m.cr;
  acc.out += m.out;
}

interface Last {
  t: number;
  model: string;
  ctx: number;
}

interface FileState {
  path: string;
  projectDir: string;
  sessionId: string;
  agentId: string | null; // null for the main transcript
  dev: number; // device and inode at the last read: a different pair means the file was replaced
  ino: number;
  mtime: number; // epoch ms at the last read
  offset: number; // bytes consumed so far
  partial: Buffer; // trailing bytes without a newline yet
  msgs: Map<string, Msg>; // windowed per-message records (pruned to the 24 h window)
  seen: Map<string, Seen>; // all-time per-message records (dedupe, turns, cost)
  totalUsd: number; // all-time cost of this file
  totalTok: TokenSplit; // all-time tokens of this file, same dedupe as totalUsd
  peakCtx: number; // all-time peak context
  last: Last | null; // newest non-synthetic assistant message
  lastActivity: number; // epoch ms, newest timestamp seen
  anon: number;
  custom?: string; // last custom-title seen
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

// True when the error means the file or its directory is no longer there.
function isGoneError(err: unknown): boolean {
  const code = (err as { code?: unknown } | null)?.code;
  return code === "ENOENT" || code === "ENOTDIR";
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

function sessionFiles(sess: SessionRec): FileState[] {
  const files: FileState[] = [];
  if (sess.main) files.push(sess.main);
  for (const f of sess.subs.values()) files.push(f);
  return files;
}

export class LiveIndex extends EventEmitter {
  readonly root: string;
  readonly firstSnapshotMs: number;
  private files = new Map<string, FileState>();
  private sessions = new Map<string, SessionRec>();
  // Paths already reported by warnOnce, so a persistent read error logs once.
  private warned = new Set<string>();
  private pending = new Set<string>();
  private timer: NodeJS.Timeout | null = null;
  private lastEmit = 0;
  private watcher: FSWatcher | null = null;
  private poller: NodeJS.Timeout | null = null;
  private latest: LiveSnapshot;

  constructor(root: string = PROJECTS_DIR) {
    super();
    // One SSE subscriber per open tab; the default limit of 10 would warn with more tabs.
    this.setMaxListeners(0);
    this.root = root;
    const t0 = performance.now();
    // Start watching before the scan so nothing written during the scan is missed.
    this.startWatching();
    this.walk(Date.now() - DAY);
    this.prune();
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

  // Re-scans the tree, prunes, and recomputes the snapshot now (no debounce, no event).
  refreshNow(): LiveSnapshot {
    this.walk(Date.now() - DAY);
    this.prune();
    this.latest = this.compute();
    return this.latest;
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
    this.prune();
    this.latest = this.compute();
    const update: LiveUpdate = { snapshot: this.latest, changed };
    this.emit("update", update);
  }

  // Only msgs (the per-message records behind the per-minute buckets) is bounded to the 24 h window:
  // entries older than the cutoff are dropped. The seen map, totals, peak and last message are
  // all-time per live session and are not pruned while the session is live, so their memory is
  // bounded per session, not per 24 h. A session is live while any of its files was written inside
  // the window, and all of its files are kept with it. A session whose files are all older than the
  // cutoff is dropped, and walk() does not re-add it because its files are out of window and untracked.
  private prune(): void {
    const cutoff = Date.now() - DAY;
    for (const st of this.files.values()) {
      for (const [key, m] of st.msgs) if (m.t < cutoff) st.msgs.delete(key);
    }
    for (const [key, sess] of this.sessions) {
      const files = sessionFiles(sess);
      if (files.some((f) => f.mtime >= cutoff)) continue;
      for (const f of files) this.files.delete(f.path);
      this.sessions.delete(key);
    }
  }

  // Drops the state of one file that was deleted or replaced under us, and its session once empty.
  private forget(st: FileState): void {
    this.files.delete(st.path);
    const key = `${st.projectDir}/${st.sessionId}`;
    const sess = this.sessions.get(key);
    if (!sess) return;
    if (st.agentId === null) {
      if (sess.main === st) sess.main = null;
    } else if (sess.subs.get(st.agentId) === st) {
      sess.subs.delete(st.agentId);
    }
    if (sess.main === null && sess.subs.size === 0) this.sessions.delete(key);
  }

  // Scans the projects tree. Files older than cutoff are skipped unless already tracked, or
  // unless the session has a recently changed subagent (then its main transcript is read too).
  // Returns the session ids whose state changed.
  private walk(cutoff: number): string[] {
    const changed = new Set<string>();
    // Every transcript the scan lists. A tracked file that is not listed any more was deleted.
    const listed = new Set<string>();
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
        continue; // not a directory, or removed during the scan
      }
      for (const name of entries) {
        if (!name.endsWith(".jsonl")) continue;
        const sid = name.slice(0, -".jsonl".length);
        let subRecent = false;
        const subDir = join(pdir, sid, "subagents");
        let subFiles: string[] = [];
        try {
          subFiles = readdirSync(subDir);
        } catch {
          // No subagents directory (or it went away during the scan).
        }
        for (const f of subFiles) {
          if (!/^agent-.+\.jsonl$/.test(f)) continue;
          const p = join(subDir, f);
          listed.add(p);
          const recent = this.mtimeOf(p) >= cutoff;
          if (recent) subRecent = true;
          if (recent || this.files.has(p)) {
            const got = this.ingestPath(p);
            if (got) changed.add(got);
          }
        }
        const mainPath = join(pdir, name);
        listed.add(mainPath);
        if (subRecent || this.mtimeOf(mainPath) >= cutoff || this.files.has(mainPath)) {
          const got = this.ingestPath(mainPath);
          if (got) changed.add(got);
        }
      }
    }
    // Forget tracked files the scan no longer lists (deleted, or their project directory went away).
    for (const [path, st] of [...this.files]) {
      if (listed.has(path)) continue;
      this.forget(st);
      changed.add(st.sessionId);
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
  // A file that is gone (or changes shape under the read) has its state dropped, not an error.
  private ingestPath(path: string): string | null {
    const loc = locate(this.root, path);
    if (!loc) return null;
    let stat: Stats;
    try {
      stat = statSync(path);
    } catch (err) {
      if (!isGoneError(err)) return this.warnOnce(path, err);
      const gone = this.files.get(path);
      if (!gone) return null;
      this.forget(gone);
      return gone.sessionId;
    }
    let st = this.files.get(path);
    if (!st) st = this.newFile(path, loc, stat);
    try {
      return this.readNew(st, stat) ? st.sessionId : null;
    } catch (err) {
      // The file is gone (ENOENT or ENOTDIR) between stat and read: forget it. A later walk or
      // watch event starts it over from the beginning if the file is back. Any other error keeps
      // the state and is reported once, so a transient read failure does not reset the file.
      if (!isGoneError(err)) return this.warnOnce(path, err);
      this.forget(st);
      return st.sessionId;
    }
  }

  // Logs a read failure for a path once per process and reports no state change.
  private warnOnce(path: string, err: unknown): null {
    if (!this.warned.has(path)) {
      this.warned.add(path);
      const msg = err instanceof Error ? err.message : String(err);
      console.warn(`session-plot: cannot read ${path}: ${msg}`);
    }
    return null;
  }

  private newFile(path: string, loc: Locator, stat: Stats): FileState {
    const st: FileState = {
      path,
      projectDir: loc.projectDir,
      sessionId: loc.sessionId,
      agentId: loc.agentId,
      dev: stat.dev,
      ino: stat.ino,
      mtime: stat.mtimeMs,
      offset: 0,
      partial: Buffer.alloc(0),
      msgs: new Map(),
      seen: new Map(),
      totalUsd: 0,
      totalTok: zeroTok(),
      peakCtx: 0,
      last: null,
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

  private readNew(st: FileState, stat: Stats): boolean {
    const size = stat.size;
    let changed = false;
    // A new inode (delete + recreate, or rename over) or a shorter file (truncate) means the
    // content we consumed is gone: drop what we derived from it and read from the start.
    const replaced = stat.ino !== st.ino || stat.dev !== st.dev;
    st.mtime = stat.mtimeMs;
    if (replaced || size < st.offset) {
      st.dev = stat.dev;
      st.ino = stat.ino;
      st.offset = 0;
      st.partial = Buffer.alloc(0);
      st.msgs.clear();
      st.seen.clear();
      st.totalUsd = 0;
      st.totalTok = zeroTok();
      st.peakCtx = 0;
      st.last = null;
      st.lastActivity = 0;
      st.anon = 0;
      st.custom = undefined;
      st.summary = undefined;
      st.firstUser = undefined;
      changed = true;
    }
    if (size === st.offset) return changed;

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
      return changed;
    }
    st.partial = Buffer.from(data.subarray(nl + 1));
    const text = data.subarray(0, nl).toString("utf8");
    for (const line of text.split("\n")) this.absorb(st, line);
    if (!st.metaLoaded && st.agentId !== null) this.loadMeta(st);
    return true;
  }

  private absorb(st: FileState, line: string): void {
    if (line.length === 0) return;
    const assistant = line.includes('"type":"assistant"');
    const wantsTitle =
      (st.firstUser === undefined && line.includes('"type":"user"')) ||
      line.includes('"custom-title"') ||
      (st.summary === undefined && line.includes('"summary"'));

    // Lines we parse take the top-level timestamp. Other lines fall back to a regex, which can
    // match a nested "timestamp" (e.g. inside tool_use input), so it is only used when unparsed.
    const o = assistant || wantsTitle ? parseLine(line) : null;
    let ts = NaN;
    if (o) {
      ts = typeof o.timestamp === "string" ? Date.parse(o.timestamp) : NaN;
    } else {
      const tsm = /"timestamp":"([^"]+)"/.exec(line);
      ts = tsm ? Date.parse(tsm[1]) : NaN;
    }
    if (Number.isFinite(ts) && ts > st.lastActivity) st.lastActivity = ts;

    if (!assistant && !wantsTitle) return;
    if (!o) return;
    // The last custom-title record wins: a rename appends a new one.
    if (o.type === "custom-title" && typeof o.customTitle === "string" && o.customTitle.trim()) {
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
    const prev = st.seen.get(key);
    const model = typeof msg.model === "string" && msg.model ? msg.model : (prev?.model ?? "");
    const t = prev?.t ?? ts;
    const input = num(usage.input_tokens);
    const cw = num(usage.cache_creation_input_tokens);
    const cr = num(usage.cache_read_input_tokens);
    const out = num(usage.output_tokens);
    const ctx = input + cw + cr;
    const usd = turnCost(model, usage).usd;

    // All-time summary: replaces this id's previous usage, so repeated lines count once.
    const tok: TokenSplit = { in: input, cacheWrite: cw, cacheRead: cr, out };
    const prevTok = prev?.tok ?? zeroTok();
    st.seen.set(key, { t, model, usd, ctx, tok });
    st.totalUsd += usd - (prev?.usd ?? 0);
    st.totalTok.in += tok.in - prevTok.in;
    st.totalTok.cacheWrite += tok.cacheWrite - prevTok.cacheWrite;
    st.totalTok.cacheRead += tok.cacheRead - prevTok.cacheRead;
    st.totalTok.out += tok.out - prevTok.out;
    if (ctx > st.peakCtx) st.peakCtx = ctx;
    // Synthetic placeholders (model "<...>") never become the session's latest message.
    if (!model.startsWith("<") && (st.last === null || t >= st.last.t)) st.last = { t, model, ctx };

    // Windowed record: only messages inside the 24 h window are kept per message.
    if (t >= Date.now() - DAY) st.msgs.set(key, { t, model, in: input, cw, cr, out, ctx, usd });
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
      const files = sessionFiles(sess);
      let lastActivity = 0;
      for (const f of files) lastActivity = Math.max(lastActivity, f.lastActivity);
      if (lastActivity < dayAgo) continue;

      let costToday = 0;
      let costLastHour = 0;
      const tokToday = zeroTok();
      const tokLastHour = zeroTok();
      for (const f of files) {
        for (const m of f.msgs.values()) {
          if (m.model.startsWith("<")) continue; // synthetic placeholders
          if (m.t >= midnight) {
            costToday += m.usd;
            totals.costToday += m.usd;
            addTok(tokToday, m);
          }
          if (m.t >= hourAgo) {
            costLastHour += m.usd;
            totals.costLastHour += m.usd;
            totals.outLastHour += m.out;
            addTok(tokLastHour, m);
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
        mainTurns = sess.main.seen.size;
        mainPeakCtx = sess.main.peakCtx;
        if (sess.main.last) {
          mainCtx = sess.main.last.ctx;
          mainModel = sess.main.last.model || undefined;
        }
      }

      const activeSubagents: LiveSubagent[] = [];
      for (const f of sess.subs.values()) {
        if (f.lastActivity < now - ACTIVE_MS || f.agentId === null) continue;
        activeSubagents.push({
          id: f.agentId,
          label: f.label,
          agentType: f.agentType,
          model: f.last?.model || undefined,
          ctx: f.last?.ctx ?? 0,
          lastActivity: f.lastActivity,
          costUsd: f.totalUsd,
          tokens: { ...f.totalTok },
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
        tokToday,
        tokLastHour,
        subagentCount: sess.subs.size,
        activeSubagents,
      });
    }

    sessions.sort((a, b) => b.lastActivity - a.lastActivity);
    return { now, windowMin: WINDOW_MIN, totals, perMinute, sessions };
  }
}
