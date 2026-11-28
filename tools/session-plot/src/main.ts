// Browser client. No external libraries; the chart is hand-built SVG.
import { branchLanes } from "./branches";
import { LatestRequest } from "./latest";
import type { Kind, Series, SessionData, SessionSummary } from "./types";
import { fmtOutPerUsd, outPerUsd } from "./cost";

declare global {
  interface Window {
    __SESSION_DATA__?: SessionData;
  }
}

const KINDS: Kind[] = ["main", "executor", "reviewer", "skill-fork", "other"];
const KIND_VAR: Record<Kind, string> = {
  main: "var(--main)",
  executor: "var(--executor)",
  reviewer: "var(--reviewer)",
  "skill-fork": "var(--skill)",
  other: "var(--other)",
};

const LEFT = 56; // y-axis labels
const RIGHT = 14;
const PLOT_TOP = 24;
const ROW_H = 14;
const LABEL_CHAR_W = 5.8;
const LANE_H = 14; // Branches panel: height of one lane
const BRANCH_PAD = 4; // Branches panel: padding under the lowest lane
const BRANCH_GAP_MS = 2000; // a lane is reused only after this long past the previous merge
const COST_H = 140; // cumulative cost panel plot height
const REFRESH_MS = 3000; // live: at most one session refetch per 3 s
const USD_STEPS = [0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000];

const ESC: Record<string, string> = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

let current: SessionData | null = null;
let chartWidth = 0;
let hoverSid: string | null = null;
const seriesById = new Map<string, Series>();

function $<T extends HTMLElement>(id: string): T {
  return document.getElementById(id) as T;
}

function esc(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ESC[c] ?? c);
}

function pad(n: number): string {
  return String(n).padStart(2, "0");
}

function hhmm(t: number): string {
  const d = new Date(t);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function hhmmss(t: number): string {
  return `${hhmm(t)}:${pad(new Date(t).getSeconds())}`;
}

function dur(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const r = s % 60;
  if (m < 60) return `${m}m ${pad(r)}s`;
  return `${Math.floor(m / 60)}h ${pad(m % 60)}m`;
}

function kTok(n: number): string {
  return (n / 1000).toFixed(1);
}

function usd(n: number): string {
  return `$${n.toFixed(2)}`;
}

function shorten(s: string, max: number): string {
  return s.length > max ? `${s.slice(0, max - 1)}…` : s;
}

// Text after the first "·" when present (e.g. "Thal Ummir · Merge PR #67" -> "Merge PR #67").
function shortLabel(label: string): string {
  const i = label.indexOf("·");
  const s = (i >= 0 ? label.slice(i + 1) : label).trim() || label.trim();
  return shorten(s, 40);
}

function niceStep(span: number, maxTicks: number, steps: number[]): number {
  for (const s of steps) if (span / s <= maxTicks) return s;
  return steps[steps.length - 1];
}

const TICK_STEPS_MIN = [5, 10, 15, 30, 60, 120, 180, 240, 360, 720, 1440, 2880, 4320, 10080, 20160, 43200];
const MAX_TICKS = 24;

// Local-time ticks on whole minutes, on a grid of multiples of the step counted from local
// midnight. The step is chosen so there are about 12 ticks and never more than MAX_TICKS.
function timeTicks(tmin: number, tmax: number): number[] {
  if (!Number.isFinite(tmin) || !Number.isFinite(tmax) || tmax < tmin) return [];
  const stepMs = niceStep((tmax - tmin) / 60000, 12, TICK_STEPS_MIN) * 60000;
  const origin = new Date(tmin);
  origin.setHours(0, 0, 0, 0);
  const grid0 = origin.getTime();
  const ticks: number[] = [];
  // Hard cap: the loop ends at tmax or after MAX_TICKS ticks, whatever the range.
  for (let t = grid0 + Math.ceil((tmin - grid0) / stepMs) * stepMs; t <= tmax && ticks.length < MAX_TICKS; t += stepMs) {
    ticks.push(t);
  }
  return ticks;
}

function mainCtxAt(points: Series["points"], t: number): number | null {
  if (points.length === 0) return null;
  let v = points[0].ctx;
  for (const p of points) {
    if (p.t <= t) v = p.ctx;
    else break;
  }
  return v;
}

interface CumPt {
  t: number;
  main: number;
  subs: number;
  total: number;
}

// Running totals in time order: main, all subagents, and both together. Starts at 0 at tmin.
function cumulativeCost(d: SessionData, tmin: number): CumPt[] {
  const events: { t: number; c: number; sub: boolean }[] = [];
  for (const p of d.main.points) events.push({ t: p.t, c: p.cost, sub: false });
  for (const s of d.subagents) for (const p of s.points) events.push({ t: p.t, c: p.cost, sub: true });
  events.sort((a, b) => a.t - b.t);
  let m = 0;
  let s = 0;
  const out: CumPt[] = [{ t: tmin, main: 0, subs: 0, total: 0 }];
  for (const e of events) {
    if (e.sub) s += e.c;
    else m += e.c;
    out.push({ t: e.t, main: m, subs: s, total: m + s });
  }
  return out;
}

function renderChart(d: SessionData): void {
  const box = $<HTMLElement>("chart");
  chartWidth = box.clientWidth;
  const W = Math.max(280, Math.round(chartWidth || 343));
  const plotW = W - LEFT - RIGHT;
  const subs = d.subagents;
  const all: Series[] = [d.main, ...subs];

  // Only finite, positive times define the axis: a missing time must not pull it to 1970.
  const isTime = (t: number): boolean => Number.isFinite(t) && t > 0;
  const starts = all.map((s) => s.start).filter(isTime);
  if (starts.length === 0) {
    box.innerHTML = "";
    return;
  }
  const tmin = Math.min(...starts);
  let tmax = Math.max(tmin, ...all.map((s) => s.end).filter(isTime));
  if (tmax - tmin < 60000) tmax = tmin + 60000;
  const X = (t: number): number => LEFT + ((t - tmin) / (tmax - tmin)) * plotW;

  const maxCtx = Math.max(0, ...all.map((s) => s.peakCtx));
  const yStep = niceStep(maxCtx * 1.08, 6, [5e3, 1e4, 2e4, 2.5e4, 5e4, 1e5, 2e5, 5e5]);
  const yMax = Math.max(yStep, Math.ceil((maxCtx * 1.08) / yStep) * yStep);

  const plotH = Math.round(Math.min(300, Math.max(180, W * 0.6)));
  const plotBottom = PLOT_TOP + plotH;
  const Y = (c: number): number => plotBottom - (c / yMax) * plotH;

  const forksTitleY = plotBottom + 34;
  const forksTop = forksTitleY + 8;
  const rows = 1 + subs.length;
  const forksBottom = forksTop + rows * ROW_H;

  // Branches panel: trunk = main, each subagent a branch from its fork to its merge. Lane 0 is the
  // trunk; positive lanes are drawn above it and negative lanes below, so the trunk sits in the middle.
  const lanes = branchLanes(subs, BRANCH_GAP_MS);
  const laneById = new Map(lanes.map((l) => [l.id, l]));
  let maxLane = 0;
  let minLane = 0;
  for (const l of lanes) {
    maxLane = Math.max(maxLane, l.lane);
    minLane = Math.min(minLane, l.lane);
  }
  // Fourth panel: cumulative cost, same x axis, directly below Forks.
  const costTitleY = forksBottom + 34;
  const costTop = costTitleY + 8;
  const costBottom = costTop + COST_H;

  // Fifth panel: Branches, below cost.
  const brTitleY = costBottom + 34;
  const brTop = brTitleY + 8;
  const brBottom = brTop + (maxLane - minLane + 1) * LANE_H + BRANCH_PAD;
  const yLane = (lane: number): number => brTop + (maxLane - lane) * LANE_H + LANE_H / 2;
  const H = brBottom + 24;

  const out: string[] = [];
  out.push(
    `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="Context size, subagent forks and cumulative cost over time">`,
  );

  // Top panel: context size
  out.push(`<text class="ptitle" x="${LEFT}" y="14">Context size</text>`);
  const nY = Math.round(yMax / yStep);
  for (let i = 0; i <= nY; i++) {
    const v = i * yStep;
    const y = Y(v);
    out.push(`<line class="grid" x1="${LEFT}" x2="${W - RIGHT}" y1="${y}" y2="${y}"/>`);
    out.push(
      `<text class="tick" x="${LEFT - 6}" y="${(y + 4).toFixed(1)}" text-anchor="end">${v === 0 ? "0" : `${Math.round(v / 1000)}k`}</text>`,
    );
  }

  const ticks = timeTicks(tmin, tmax);
  for (const t of ticks) {
    const x = X(t).toFixed(1);
    out.push(`<line class="grid" x1="${x}" x2="${x}" y1="${PLOT_TOP}" y2="${brBottom}"/>`);
    out.push(`<text class="tick" x="${x}" y="${plotBottom + 14}" text-anchor="middle">${hhmm(t)}</text>`);
    out.push(`<text class="tick" x="${x}" y="${forksBottom + 14}" text-anchor="middle">${hhmm(t)}</text>`);
    out.push(`<text class="tick" x="${x}" y="${brBottom + 14}" text-anchor="middle">${hhmm(t)}</text>`);
    out.push(`<text class="tick" x="${x}" y="${costBottom + 14}" text-anchor="middle">${hhmm(t)}</text>`);
  }

  let lastTagX = -Infinity;
  for (const c of d.compactions) {
    if (c < tmin || c > tmax) continue;
    const cx = X(c);
    out.push(`<line class="compact" x1="${cx.toFixed(1)}" x2="${cx.toFixed(1)}" y1="${PLOT_TOP}" y2="${brBottom}"/>`);
    if (cx - lastTagX >= 52) {
      out.push(`<text class="ctag" x="${(cx + 3).toFixed(1)}" y="${PLOT_TOP + 11}">compact</text>`);
      lastTagX = cx;
    }
  }

  // Connectors from the main line at spawn time to the subagent's first point
  for (const s of subs) {
    if (s.spawn === undefined || s.points.length === 0) continue;
    const mc = mainCtxAt(d.main.points, s.spawn);
    if (mc === null) continue;
    const x = X(s.spawn).toFixed(1);
    out.push(
      `<line data-sid="${esc(s.id)}" class="conn" x1="${x}" x2="${x}" y1="${Y(mc).toFixed(1)}" y2="${Y(s.points[0].ctx).toFixed(1)}" style="stroke:${KIND_VAR[s.kind]}"/>`,
    );
  }

  // Polylines are never filled (CSS sets fill:none); single-point series become a dot.
  const poly = (s: Series, cls: string, color: string, width: number): string => {
    if (s.points.length === 1) {
      const p = s.points[0];
      return `<circle data-sid="${esc(s.id)}" class="${cls}" cx="${X(p.t).toFixed(1)}" cy="${Y(p.ctx).toFixed(1)}" r="${cls === "main" ? 3 : 2}" style="fill:${color}"/>`;
    }
    const pts = s.points.map((p) => `${X(p.t).toFixed(1)},${Y(p.ctx).toFixed(1)}`).join(" ");
    return `<polyline data-sid="${esc(s.id)}" class="${cls}" points="${pts}" style="stroke:${color};stroke-width:${width}"/>`;
  };

  for (const s of subs) out.push(poly(s, "sub", KIND_VAR[s.kind], 1.25));
  out.push(poly(d.main, "main", KIND_VAR.main, 2.5));

  // Wide transparent hit lines on top so thin series are easy to hover
  for (const s of subs) if (s.points.length > 1) out.push(poly(s, "hit", "transparent", 10));
  if (d.main.points.length > 1) out.push(poly(d.main, "hit", "transparent", 12));

  // Middle panel: forks
  out.push(`<text class="ptitle" x="${LEFT}" y="${forksTitleY}">Forks</text>`);
  all.forEach((s, r) => {
    const y = forksTop + r * ROW_H;
    const bx0 = X(s.spawn ?? s.start);
    const bx1 = Math.max(bx0 + 2, X(s.end));
    const w = (bx1 - bx0).toFixed(1);
    out.push(
      `<rect data-sid="${esc(s.id)}" class="bar" x="${bx0.toFixed(1)}" y="${y + 2}" width="${w}" height="10" rx="2" style="fill:${KIND_VAR[s.kind]}"><title>${esc(s.label)}</title></rect>`,
    );
    const label = shortLabel(s.label);
    const tw = label.length * LABEL_CHAR_W;
    const sid = esc(s.id);
    const ty = (y + 10.5).toFixed(1);
    if (bx0 - LEFT >= tw + 8) {
      out.push(
        `<text data-sid="${sid}" class="lbl" x="${(bx0 - 4).toFixed(1)}" y="${ty}" text-anchor="end">${esc(label)}</text>`,
      );
    } else if (W - RIGHT - bx1 >= tw + 8) {
      out.push(
        `<text data-sid="${sid}" class="lbl" x="${(bx1 + 4).toFixed(1)}" y="${ty}" text-anchor="start">${esc(label)}</text>`,
      );
    } else {
      const inside = bx1 - bx0 >= tw + 6;
      out.push(
        `<text data-sid="${sid}" class="lbl${inside ? " in" : ""}" x="${(bx0 + 3).toFixed(1)}" y="${ty}" text-anchor="start">${esc(label)}</text>`,
      );
    }
  });

  // Branches panel drawing. Wide transparent hit paths go last so they sit on top, as the context
  // panel's hit lines do.
  out.push(`<text class="ptitle" x="${LEFT}" y="${brTitleY}">Branches</text>`);
  const yT = yLane(0);
  const mainId = esc(d.main.id);
  const hits: string[] = [];
  const mainX0 = X(d.main.start);
  const mainX1 = Math.max(mainX0 + 2, X(d.main.end));
  out.push(
    `<line data-sid="${mainId}" class="trunk" x1="${mainX0.toFixed(1)}" x2="${mainX1.toFixed(1)}" y1="${yT.toFixed(1)}" y2="${yT.toFixed(1)}" style="stroke:${KIND_VAR.main}"><title>${esc(d.main.label)}</title></line>`,
  );
  hits.push(
    `<line data-sid="${mainId}" class="hit" x1="${mainX0.toFixed(1)}" x2="${mainX1.toFixed(1)}" y1="${yT.toFixed(1)}" y2="${yT.toFixed(1)}"/>`,
  );
  for (const s of subs) {
    const lane = laneById.get(s.id);
    if (!lane) continue;
    const sid = esc(s.id);
    const color = KIND_VAR[s.kind];
    const yL = yLane(lane.lane);
    const x0 = X(lane.fork);
    const x1 = Math.max(x0 + 2, X(lane.merge));
    // Out from the trunk over about 28 px (less when short), along the lane, and back to the trunk.
    const dx = Math.min(28, (x1 - x0) / 2);
    const f = (n: number): string => n.toFixed(1);
    const path =
      `M${f(x0)},${f(yT)} C${f(x0 + dx / 2)},${f(yT)} ${f(x0 + dx / 2)},${f(yL)} ${f(x0 + dx)},${f(yL)} ` +
      `L${f(x1 - dx)},${f(yL)} ` +
      `C${f(x1 - dx / 2)},${f(yL)} ${f(x1 - dx / 2)},${f(yT)} ${f(x1)},${f(yT)}`;
    out.push(
      `<path data-sid="${sid}" class="br" d="${path}" style="stroke:${color}"><title>${esc(s.label)}</title></path>`,
    );
    hits.push(`<path data-sid="${sid}" class="hit" d="${path}"/>`);
  }
  out.push(...hits);

  // Bottom panel: cumulative cost (API list price)
  const cum = cumulativeCost(d, tmin);
  const last = cum[cum.length - 1] ?? { t: tmin, main: 0, subs: 0, total: 0 };
  const cStep = niceStep(last.total * 1.08, 4, USD_STEPS);
  const cMax = Math.max(cStep, Math.ceil((last.total * 1.08) / cStep) * cStep);
  const YC = (v: number): number => costBottom - (v / cMax) * COST_H;
  out.push(`<text class="ptitle" x="${LEFT}" y="${costTitleY}">Cumulative cost (API list price)</text>`);
  const nC = Math.round(cMax / cStep);
  const cDec = Number.isInteger(cStep) ? 0 : 2; // 0.5 steps need two decimals; whole steps need none
  for (let i = 0; i <= nC; i++) {
    const v = i * cStep;
    const y = YC(v);
    out.push(`<line class="grid" x1="${LEFT}" x2="${W - RIGHT}" y1="${y.toFixed(1)}" y2="${y.toFixed(1)}"/>`);
    out.push(
      `<text class="tick" x="${LEFT - 6}" y="${(y + 4).toFixed(1)}" text-anchor="end">$${v.toFixed(cDec)}</text>`,
    );
  }
  const costLine = (key: "main" | "subs" | "total", color: string, dash: string): string => {
    if (cum.length < 2) return "";
    const pts = cum.map((p) => `${X(p.t).toFixed(1)},${YC(p[key]).toFixed(1)}`).join(" ");
    return `<polyline class="costline" points="${pts}" style="stroke:${color}"${dash ? ` stroke-dasharray="${dash}"` : ""}/>`;
  };
  out.push(costLine("subs", KIND_VAR.executor, ""));
  out.push(costLine("main", KIND_VAR.main, ""));
  out.push(costLine("total", "var(--fg)", "4 3"));
  // Final values, drawn in the empty top-left of the plot (the curves only rise).
  const legend: [string, string, number][] = [
    ["main", KIND_VAR.main, last.main],
    ["subagents", KIND_VAR.executor, last.subs],
    ["total", "var(--fg)", last.total],
  ];
  legend.forEach(([name, color, v], i) => {
    out.push(
      `<text class="ctag" x="${LEFT + 8}" y="${(costTop + 12 + i * 12).toFixed(1)}" style="fill:${color}">${name} ${usd(v)}</text>`,
    );
  });

  out.push("</svg>");
  box.innerHTML = out.join("");
}

function renderLegend(): void {
  $<HTMLElement>("legend").innerHTML = KINDS.map(
    (k) => `<span><i class="sw" style="background:${KIND_VAR[k]}"></i>${k}</span>`,
  ).join("");
}

function renderTable(d: SessionData): void {
  const rows = [d.main, ...d.subagents];
  const head =
    `<tr><th>Label</th><th>Kind</th><th>Model</th><th>Spawn</th><th>Duration</th>` +
    `<th class="n">Turns</th><th class="n">Peak ctx (k)</th><th class="n">Output (k)</th><th class="n">Cost $</th><th class="n">Out/$</th></tr>`;
  const body = rows
    .map(
      (s) =>
        `<tr data-sid="${esc(s.id)}">` +
        `<td>${esc(s.label)}</td>` +
        `<td><span class="sw" style="background:${KIND_VAR[s.kind]}"></span> ${s.kind}</td>` +
        `<td>${esc(s.model ?? "—")}</td>` +
        `<td class="mono">${s.spawn !== undefined ? hhmmss(s.spawn) : "—"}</td>` +
        `<td class="mono">${dur(s.end - s.start)}</td>` +
        `<td class="n">${s.turns}</td>` +
        `<td class="n">${kTok(s.peakCtx)}</td>` +
        `<td class="n">${kTok(s.outputTokens)}</td>` +
        `<td class="n">${s.costUsd.toFixed(2)}</td>` +
        `<td class="n">${fmtOutPerUsd(outPerUsd(s.outputTokens, s.costUsd))}</td>` +
        `</tr>`,
    )
    .join("");
  $<HTMLElement>("table").innerHTML = `<table><thead>${head}</thead><tbody>${body}</tbody></table>`;
}

function tipHtml(s: Series): string {
  const lines = [`<strong>${esc(s.label)}</strong>`, `kind: ${s.kind}`];
  if (s.agentType) lines.push(`agentType: ${esc(s.agentType)}`);
  lines.push(`model: ${esc(s.model ?? "—")}`);
  if (s.spawn !== undefined) lines.push(`spawn: ${hhmmss(s.spawn)}`);
  lines.push(`${hhmmss(s.start)} – ${hhmmss(s.end)}`);
  lines.push(`duration: ${dur(s.end - s.start)}`);
  lines.push(`turns: ${s.turns} · peak ctx: ${kTok(s.peakCtx)}k · output: ${kTok(s.outputTokens)}k`);
  lines.push(`cost: ${usd(s.costUsd)} (API list price)`);
  lines.push(`out/$: ${fmtOutPerUsd(outPerUsd(s.outputTokens, s.costUsd))}`);
  return lines.join("<br>");
}

function setHighlight(sid: string | null): void {
  hoverSid = sid;
  document.querySelectorAll<Element>("#chart [data-sid]").forEach((el) => {
    el.classList.toggle("dim", sid !== null && el.getAttribute("data-sid") !== sid);
  });
  document.querySelectorAll<Element>("#table tr[data-sid]").forEach((tr) => {
    tr.classList.toggle("hi", sid !== null && tr.getAttribute("data-sid") === sid);
  });
}

function placeTip(x: number, y: number): void {
  const tip = $<HTMLElement>("tip");
  const gap = 12;
  const w = tip.offsetWidth;
  const h = tip.offsetHeight;
  let left = x + gap;
  if (left + w > window.innerWidth - 4) left = Math.max(4, x - w - gap);
  let top = y + gap;
  if (top + h > window.innerHeight - 4) top = Math.max(4, y - h - gap);
  tip.style.left = `${left}px`;
  tip.style.top = `${top}px`;
}

function hideTip(): void {
  setHighlight(null);
  $<HTMLElement>("tip").hidden = true;
}

function bindHover(root: HTMLElement): void {
  const tip = $<HTMLElement>("tip");
  root.addEventListener("mouseover", (e: MouseEvent) => {
    const el = (e.target as Element).closest?.("[data-sid]");
    const s = el ? seriesById.get(el.getAttribute("data-sid") ?? "") : undefined;
    if (!s) {
      hideTip();
      return;
    }
    if (hoverSid !== s.id) {
      setHighlight(s.id);
      tip.innerHTML = tipHtml(s);
    }
    tip.hidden = false;
    placeTip(e.clientX, e.clientY);
  });
  root.addEventListener("mousemove", (e: MouseEvent) => {
    if (!tip.hidden) placeTip(e.clientX, e.clientY);
  });
  root.addEventListener("mouseleave", () => hideTip());
}

// After a re-render (live refresh), keep the hovered series highlighted and its tooltip current.
function restoreHover(): void {
  if (hoverSid === null) return;
  const s = seriesById.get(hoverSid);
  if (!s) {
    hideTip();
    return;
  }
  setHighlight(s.id);
  $<HTMLElement>("tip").innerHTML = tipHtml(s);
}

function renderSession(d: SessionData): void {
  current = d;
  seriesById.clear();
  for (const s of [d.main, ...d.subagents]) seriesById.set(s.id, s);
  $<HTMLElement>("subtitle").textContent = `${d.title} · ${d.id}`;
  renderLegend();
  renderChart(d);
  renderTable(d);
  const totalOut = d.main.outputTokens + d.subagents.reduce((a, s) => a + s.outputTokens, 0);
  const unpriced =d.unpricedModels.length > 0 ? ` · unpriced: ${d.unpricedModels.join(", ")}` : "";
  setStatus(
    `${d.subagents.length} subagents · ${d.compactions.length} compactions · ${d.main.turns} main turns · ` +
      `${usd(d.totalCostUsd)} total (API list price) · ${fmtOutPerUsd(outPerUsd(totalOut, d.totalCostUsd))} out/$${unpriced}`,
  );
  restoreHover();
}

function setStatus(msg: string): void {
  $<HTMLElement>("status").textContent = msg;
}

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return (await res.json()) as T;
}

// Each selection gets a token; a response for an earlier selection is discarded when it arrives late.
const selection = new LatestRequest();

async function selectSession(id: string): Promise<void> {
  const token = selection.start();
  try {
    const u = new URL(location.href);
    u.searchParams.set("session", id);
    history.replaceState(null, "", u);
  } catch {
    // URL bar update is cosmetic
  }
  setStatus("Loading…");
  try {
    const d = await getJson<SessionData>(`/api/session/${encodeURIComponent(id)}`);
    if (selection.isCurrent(token)) renderSession(d);
  } catch (e) {
    if (selection.isCurrent(token)) setStatus(`Failed to load ${id}: ${(e as Error).message}`);
  }
}

// ---- live mode (?live=1): refetch the open session when the server reports a change to it ----

let lastRefresh = 0;
let refreshTimer: ReturnType<typeof setTimeout> | null = null;

async function refreshCurrent(): Promise<void> {
  if (!current) return;
  const id = current.id;
  // A user selection made while this refresh is in flight makes its result stale.
  const token = selection.mark();
  lastRefresh = Date.now();
  try {
    const d = await getJson<SessionData>(`/api/session/${encodeURIComponent(id)}`);
    if (selection.isCurrent(token) && current && current.id === id) renderSession(d);
  } catch {
    // keep the last good render; the next change event retries
  }
}

function scheduleRefresh(): void {
  if (refreshTimer !== null) return;
  const wait = Math.max(0, REFRESH_MS - (Date.now() - lastRefresh));
  refreshTimer = setTimeout(() => {
    refreshTimer = null;
    void refreshCurrent();
  }, wait);
}

function startLive(): void {
  const badge = $<HTMLElement>("livebadge");
  badge.hidden = false;
  const es = new EventSource("/api/live/stream");
  es.addEventListener("changed", (ev: MessageEvent<string>) => {
    const { sessionIds } = JSON.parse(ev.data) as { sessionIds: string[] };
    if (current && sessionIds.includes(current.id)) scheduleRefresh();
  });
  es.onopen = () => badge.classList.remove("off");
  es.onerror = () => badge.classList.add("off");
}

function sessionOptionLabel(s: SessionSummary): string {
  const date = new Date(s.mtime).toLocaleDateString();
  return `${shorten(s.title, 70)} · ${s.id.slice(0, 8)} · ${s.subagentCount} sub · ${date}`;
}

async function start(): Promise<void> {
  bindHover($<HTMLElement>("chart"));
  bindHover($<HTMLElement>("table"));

  const embedded = window.__SESSION_DATA__;
  if (embedded) {
    // Exported file: no picker, no live links, no network use.
    $<HTMLElement>("picker").hidden = true;
    $<HTMLElement>("livelink").hidden = true;
    renderSession(embedded);
    return;
  }

  const list = await getJson<SessionSummary[]>("/api/sessions");
  if (list.length === 0) {
    setStatus("No sessions found under ~/.claude/projects.");
    return;
  }
  const sel = $<HTMLSelectElement>("session");
  const params = new URLSearchParams(location.search);
  const wanted = params.get("session");
  const initial = list.find((s) => s.id === wanted)?.id ?? list[0].id;
  sel.innerHTML = list
    .map((s) => `<option value="${esc(s.id)}">${esc(sessionOptionLabel(s))}</option>`)
    .join("");
  sel.value = initial;
  sel.addEventListener("change", () => {
    void selectSession(sel.value);
  });
  await selectSession(initial);
  if (params.get("live") === "1") startLive();
}

const chartEl = $<HTMLElement>("chart");
if (typeof ResizeObserver !== "undefined") {
  new ResizeObserver(() => {
    if (current && chartEl.clientWidth !== chartWidth) renderChart(current);
  }).observe(chartEl);
} else {
  window.addEventListener("resize", () => {
    if (current && chartEl.clientWidth !== chartWidth) renderChart(current);
  });
}

start().catch((e: unknown) => setStatus(`Failed to start: ${(e as Error).message}`));
