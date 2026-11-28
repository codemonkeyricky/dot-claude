// Browser client for /live. No external libraries; the charts are hand-built SVG.
import { isTokenKind, minuteTokens, minuteTokenTotal, TOKEN_KIND_LABELS, tokensOf, type TokenKind } from "./tokens";
import type { LiveBranches, LiveMinute, LiveSession, LiveSnapshot } from "./types";
import { compressAxis } from "./livebranches";
import { fmtOutPerUsd, outPerUsd } from "./cost";
import { stableOrder } from "./order";

const CTX_LIMIT = 200_000;
const LIVE_SEC = 120;
const MODEL_VARS = ["--m1", "--m2", "--m3", "--m4", "--m5", "--m6"];
const USD_STEPS = [0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000];
const TOKEN_STEPS = [
  1e3, 2e3, 5e3, 1e4, 2e4, 5e4, 1e5, 2e5, 5e5, 1e6, 2e6, 5e6, 1e7, 2e7, 5e7, 1e8, 2e8, 5e8, 1e9, 2e9, 5e9, 1e10,
];
const KIND_KEY = "session-plot.tokenKind";
const ESC: Record<string, string> = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

let snap: LiveSnapshot | null = null;
let serverOffset = 0; // server clock minus local clock, ms
let selectedT: number | null = null; // minute (epoch ms) shown in the readouts of both charts
// Every model seen so far, in colour order. Append-only: a model keeps its colour for the page load.
let models: string[] = [];
let tokenKind: TokenKind = loadTokenKind();

// Sessions whose active-subagent list is expanded. Stored as a JSON array of session ids in localStorage.
const SUBS_KEY = "session-plot.subsOpen";
let subsOpen: Set<string> = loadSubsOpen();
// Rendered card node and the HTML it was built from, per session id (see renderCards).
const cardEls = new Map<string, HTMLElement>();
const cardHtmls = new Map<string, string>();
// Session ids in on-screen order, kept across snapshots (see stableOrder in src/order.ts).
let cardOrder: string[] = [];
// The card under a press: from pointerdown on one of its toggle buttons until release. Its DOM is kept
// during the press, so the click that ends the press reaches the same button.
let held: { id: string; timer: ReturnType<typeof setTimeout> } | null = null;
const HOLD_MS = 2000;

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

function usd(n: number): string {
  return `$${n.toFixed(2)}`;
}

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

// "Sat 11-22" for the local calendar day that starts at t.
function dayLabel(t: number): string {
  const d = new Date(t);
  return `${WEEKDAYS[d.getDay()]} ${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function tokens(n: number): string {
  if (n >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}k`;
  return String(Math.round(n));
}

// Axis label: short, in k or M, without trailing zeros (500k, 1.5M).
function tickTokens(v: number): string {
  const trim = (x: number): string => String(Number(x.toFixed(2)));
  if (v === 0) return "0";
  if (v >= 1e6) return `${trim(v / 1e6)}M`;
  if (v >= 1e3) return `${trim(v / 1e3)}k`;
  return String(Math.round(v));
}

function kTok(n: number): string {
  return `${Math.round(n / 1000)}k`;
}

function shorten(s: string, max: number): string {
  return s.length > max ? `${s.slice(0, max - 1)}…` : s;
}

function agoText(sec: number): string {
  const s = Math.max(0, Math.floor(sec));
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  return `${Math.floor(m / 60)}h ${pad(m % 60)}m ago`;
}

function niceStep(span: number, maxTicks: number, steps: number[]): number {
  for (const s of steps) if (span / s <= maxTicks) return s;
  return steps[steps.length - 1];
}

function colorOf(model: string): string {
  const i = models.indexOf(model);
  return `var(${MODEL_VARS[(i < 0 ? 0 : i) % MODEL_VARS.length]})`;
}

function loadTokenKind(): TokenKind {
  try {
    const v = localStorage.getItem(KIND_KEY);
    return isTokenKind(v) ? v : "all";
  } catch {
    return "all"; // storage blocked or unavailable: use the default for this page only
  }
}

function saveTokenKind(k: TokenKind): void {
  try {
    localStorage.setItem(KIND_KEY, k);
  } catch {
    // Not persisted; the choice still applies until the page reloads.
  }
}

function loadSubsOpen(): Set<string> {
  try {
    const raw: unknown = JSON.parse(localStorage.getItem(SUBS_KEY) ?? "[]");
    return new Set(Array.isArray(raw) ? raw.filter((v): v is string => typeof v === "string") : []);
  } catch {
    return new Set();
  }
}

function saveSubsOpen(): void {
  try {
    localStorage.setItem(SUBS_KEY, JSON.stringify([...subsOpen]));
  } catch {
    // Not persisted; the expansion still applies until the page reloads.
  }
}

// Compact token count for the cards: 12.3M, 950k, 412.
function tokShort(n: number): string {
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${Math.round(n / 1e3)}k`;
  return String(Math.round(n));
}

function costsOf(m: LiveMinute): Record<string, number> {
  return Object.fromEntries(Object.entries(m.byModel).map(([model, v]) => [model, v.cost]));
}

// Adds every model in the snapshot that has no colour yet. The last-hour models come first (sorted by
// name, as before), then models seen only in today's per-session totals and active subagents (sorted).
// Existing entries never move, so colours stay put across snapshots.
function registerModels(s: LiveSnapshot): void {
  const known = new Set(models);
  const add = (names: Iterable<string | undefined>): void => {
    const fresh = [...new Set(names)].filter((n): n is string => typeof n === "string" && !known.has(n)).sort();
    for (const n of fresh) {
      known.add(n);
      models.push(n);
    }
  };
  add(s.perMinute.flatMap((m) => Object.keys(m.byModel)));
  add(s.sessions.flatMap((x) => x.byModel.map((b) => b.model)));
  add(s.sessions.flatMap((x) => x.activeSubagents.map((a) => a.model)));
  add(s.sessions.flatMap((x) => (x.mainModel ? [x.mainModel] : [])));
  add(s.sessions.flatMap((x) => x.branches.branches.map((b) => b.model)));
}

// 7-day tile: the total, a hover listing of each day (oldest first, newest last) and one mini bar
// per day, scaled to the busiest day. Today's bar is the last one and gets the highlight class.
function renderWeek(s: LiveSnapshot): void {
  const days = s.totals.costByDay;
  const max = Math.max(0, ...days.map((x) => x.cost));
  const last = days.length - 1;
  const lines = days.map((x) => `${dayLabel(x.day)}  ${usd(x.cost)}`);
  $<HTMLElement>("t7d").textContent = usd(s.totals.cost7d);
  $<HTMLElement>("tWeek").title = lines.join("\n");
  $<HTMLElement>("tWeekBars").innerHTML = days
    .map((x, i) => {
      const pct = max > 0 ? (x.cost / max) * 100 : 0;
      const cls = i === last ? ' class="wk-today"' : "";
      return `<i${cls} style="height:${pct.toFixed(1)}%" title="${esc(lines[i])}"></i>`;
    })
    .join("");
}

function renderTotals(s: LiveSnapshot): void {
  renderWeek(s);
  $<HTMLElement>("tToday").textContent = usd(s.totals.costToday);
  $<HTMLElement>("tHour").textContent = usd(s.totals.costLastHour);
  $<HTMLElement>("tOut").textContent = tokens(s.totals.outLastHour);
  $<HTMLElement>("tOpdToday").textContent = fmtOutPerUsd(outPerUsd(s.totals.outToday, s.totals.costToday));
  $<HTMLElement>("tOpdHour").textContent = fmtOutPerUsd(outPerUsd(s.totals.outLastHour, s.totals.costLastHour));
  $<HTMLElement>("updated").textContent = `updated ${hhmmss(s.now)}`;
}

function renderLegend(): void {
  const html =
    models.length === 0
      ? `<span class="muted">no model activity in the last 24 h</span>`
      : models.map((m) => `<span><i class="sw" style="background:${colorOf(m)}"></i>${esc(m)}</span>`).join("");
  $<HTMLElement>("legend").innerHTML = html;
}

interface ChartSpec {
  box: HTMLElement;
  label: string; // aria label of the SVG
  values: (m: LiveMinute) => Record<string, number>; // per-model value of one minute
  steps: number[]; // allowed nice tick steps
  fmtTick: (v: number, step: number) => string;
  fmtValue: (v: number) => string; // hover tooltip text
}

// Stacked bars, one per minute, in the shared model order. Empty minutes and zero segments draw nothing.
function drawStacked(spec: ChartSpec, s: LiveSnapshot): void {
  const box = spec.box;
  const W = Math.max(280, Math.round(box.clientWidth || 343));
  const H = 220;
  const LEFT = 52;
  const RIGHT = 8;
  const TOP = 10;
  const BOTTOM = 24;
  const plotW = W - LEFT - RIGHT;
  const plotH = H - TOP - BOTTOM;
  const n = s.perMinute.length;
  const perMin = s.perMinute.map((m) => spec.values(m));
  let maxV = 0;
  for (const v of perMin) maxV = Math.max(maxV, Object.values(v).reduce((a, b) => a + b, 0));
  const step = niceStep(maxV * 1.1, 4, spec.steps);
  const yMax = Math.max(step, Math.ceil((maxV * 1.1) / step) * step);
  const Y = (v: number): number => TOP + plotH - (v / yMax) * plotH;
  const slot = plotW / n;
  const bw = Math.max(2, slot * 0.72);

  const out: string[] = [];
  out.push(`<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="${esc(spec.label)}">`);
  const nY = Math.round(yMax / step);
  for (let i = 0; i <= nY; i++) {
    const v = i * step;
    const y = Y(v).toFixed(1);
    out.push(`<line class="grid" x1="${LEFT}" x2="${W - RIGHT}" y1="${y}" y2="${y}"/>`);
    out.push(
      `<text class="tick" x="${LEFT - 6}" y="${(Y(v) + 4).toFixed(1)}" text-anchor="end">${esc(spec.fmtTick(v, step))}</text>`,
    );
  }

  s.perMinute.forEach((m, i) => {
    const x = LEFT + i * slot + (slot - bw) / 2;
    const vals = perMin[i];
    let acc = 0;
    for (const model of models) {
      const v = vals[model] ?? 0;
      if (v <= 0) continue;
      const y0 = Y(acc);
      acc += v;
      const y1 = Y(acc);
      out.push(
        `<rect class="seg" x="${x.toFixed(1)}" y="${y1.toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(0.5, y0 - y1).toFixed(1)}" style="fill:${colorOf(model)}"><title>${esc(hhmm(m.t))} ${esc(model)} ${esc(spec.fmtValue(v))}</title></rect>`,
      );
    }
  });

  // Labels every 15 minutes, plus the newest minute. A 15-minute label that would sit within
  // 36 px of the newest label is skipped, so the two never overlap at the right edge.
  const endX = LEFT + (n - 1) * slot + slot / 2;
  s.perMinute.forEach((m, i) => {
    if (new Date(m.t).getMinutes() % 15 !== 0 && i !== n - 1) return;
    const x = LEFT + i * slot + slot / 2;
    if (i !== n - 1 && Math.abs(x - endX) < 36) return;
    out.push(`<text class="tick" x="${x.toFixed(1)}" y="${H - 6}" text-anchor="middle">${hhmm(m.t)}</text>`);
  });

  // Transparent full-height hit columns (hover or tap for the minute breakdown). The per-minute
  // summary sits on the column, because the bar titles above it are covered by these rects.
  s.perMinute.forEach((m, i) => {
    const sel = m.t === selectedT ? " sel" : "";
    const vals = perMin[i];
    const total = Object.values(vals).reduce((a, b) => a + b, 0);
    const lines = [`${hhmm(m.t)} total ${spec.fmtValue(total)}`];
    for (const model of models) {
      const v = vals[model] ?? 0;
      if (v > 0) lines.push(`${model} ${spec.fmtValue(v)}`);
    }
    out.push(
      `<rect class="col${sel}" data-t="${m.t}" x="${(LEFT + i * slot).toFixed(1)}" y="${TOP}" width="${slot.toFixed(1)}" height="${plotH}"><title>${esc(lines.join("\n"))}</title></rect>`,
    );
  });
  out.push("</svg>");
  box.innerHTML = out.join("");
}

const costSpec = (): ChartSpec => ({
  box: $<HTMLElement>("chart"),
  label: "Cost per minute, last 60 minutes, by model",
  values: costsOf,
  steps: USD_STEPS,
  fmtTick: (v, step) => `$${v.toFixed(Number.isInteger(step) ? 0 : 2)}`, // 0.5 steps need two decimals
  fmtValue: usd,
});

const tokenSpec = (): ChartSpec => ({
  box: $<HTMLElement>("tokChart"),
  label: `Tokens per minute (${TOKEN_KIND_LABELS[tokenKind].toLowerCase()}), last 60 minutes, by model`,
  values: (m) => minuteTokens(m, tokenKind),
  steps: TOKEN_STEPS,
  fmtTick: (v) => tickTokens(v),
  fmtValue: tokens,
});

function renderChart(s: LiveSnapshot): void {
  drawStacked(costSpec(), s);
}

function renderTokens(s: LiveSnapshot): void {
  drawStacked(tokenSpec(), s);
  renderTokReadout(s);
}

function renderReadout(s: LiveSnapshot): void {
  const el = $<HTMLElement>("readout");
  const m = s.perMinute.find((x) => x.t === selectedT);
  if (!m) {
    el.textContent = "";
    return;
  }
  const rows = Object.entries(m.byModel).sort((a, b) => b[1].cost - a[1].cost);
  if (rows.length === 0) {
    el.innerHTML = `<strong>${hhmm(m.t)}</strong> · no activity`;
    return;
  }
  const total = rows.reduce((acc, [, v]) => acc + v.cost, 0);
  const lines = rows.map(
    ([model, v]) =>
      `<span class="rrow"><i class="sw" style="background:${colorOf(model)}"></i>${esc(model)} ${usd(v.cost)}` +
      ` · out ${tokens(v.out)} · in ${tokens(v.in)} · cache write ${tokens(v.cacheWrite)} · read ${tokens(v.cacheRead)}</span>`,
  );
  el.innerHTML = `<strong>${hhmm(m.t)}</strong> · ${usd(total)}<br>${lines.join("<br>")}`;
}

// Per-model token counts for the selected minute and the selected kind.
function renderTokReadout(s: LiveSnapshot): void {
  const el = $<HTMLElement>("tokReadout");
  const m = s.perMinute.find((x) => x.t === selectedT);
  if (!m) {
    el.textContent = "";
    return;
  }
  const label = TOKEN_KIND_LABELS[tokenKind].toLowerCase();
  const rows = Object.entries(minuteTokens(m, tokenKind))
    .filter(([, v]) => v > 0)
    .sort((a, b) => b[1] - a[1]);
  if (rows.length === 0) {
    el.innerHTML = `<strong>${hhmm(m.t)}</strong> · no ${esc(label)} tokens`;
    return;
  }
  const total = minuteTokenTotal(m, tokenKind);
  const lines = rows.map(
    ([model, v]) =>
      `<span class="rrow"><i class="sw" style="background:${colorOf(model)}"></i>${esc(model)} ${tokens(v)}</span>`,
  );
  el.innerHTML = `<strong>${hhmm(m.t)}</strong> · ${tokens(total)} ${esc(label)} tokens<br>${lines.join("<br>")}`;
}

function parseCard(html: string): HTMLElement {
  const tpl = document.createElement("template");
  tpl.innerHTML = html;
  return tpl.content.firstElementChild as HTMLElement;
}

// Only a card whose HTML changed is replaced, so a click or keyboard focus on an unchanged card
// survives the snapshot. A card under a press (see held) is not replaced until the press ends. Nodes are moved to match the snapshot order, and gone sessions are removed.
function renderCards(s: LiveSnapshot): void {
  const grid = $<HTMLElement>("cards");
  if (s.sessions.length === 0) {
    grid.innerHTML = `<p class="muted">No session activity in the last 24 h.</p>`;
    cardEls.clear();
    cardHtmls.clear();
    cardOrder = [];
    return;
  }
  const wanted = new Set(s.sessions.map((x) => x.id));
  for (const [id, el] of cardEls) {
    if (wanted.has(id)) continue;
    el.remove();
    cardEls.delete(id);
    cardHtmls.delete(id);
  }
  // A toggle button with keyboard focus is replaced with its card; focus goes back to the new button.
  let refocus: { id: string; sel: string } | null = null;
  // The order from the last snapshot is kept; sessions not seen before go on top (see stableOrder).
  cardOrder = stableOrder(cardOrder, s.sessions);
  const byId = new Map(s.sessions.map((x) => [x.id, x] as const));
  const ordered = cardOrder.map((id) => byId.get(id)!);
  ordered.forEach((x, idx) => {
    const html = cardHtml(x, s.now);
    let el = cardEls.get(x.id);
    if (!el) {
      el = parseCard(html);
      cardEls.set(x.id, el);
      cardHtmls.set(x.id, html);
    } else if (cardHtmls.get(x.id) !== html && held?.id !== x.id) {
      const active = document.activeElement as HTMLElement | null;
      const focusSel = active && el.contains(active) && active.tagName === "BUTTON" ? focusSelectorOf(active) : null;
      if (focusSel) refocus = { id: x.id, sel: focusSel };
      const fresh = parseCard(html);
      el.replaceWith(fresh);
      el = fresh;
      cardEls.set(x.id, el);
      // Recorded only for the DOM that was built: a held card keeps its old html here, so the
      // release re-render sees the difference.
      cardHtmls.set(x.id, html);
    }
    const at = grid.children[idx];
    if (at !== el) grid.insertBefore(el, at ?? null);
  });
  // Anything else in the grid (the empty-state message) goes after the cards.
  const current = new Set<Element>(cardEls.values());
  for (const child of [...grid.children]) if (!current.has(child)) child.remove();
  // Every card built here starts with an empty last-activity text. Fill it now, so a re-render
  // from the kind chart or a toggle does not leave it blank until the next one-second tick.
  tick();
  if (refocus) {
    const r: { id: string; sel: string } = refocus;
    cardEls.get(r.id)?.querySelector<HTMLButtonElement>(r.sel)?.focus({ preventScroll: true });
  }
}

// The selector that finds the same button in a rebuilt card: the subagent toggle.
function focusSelectorOf(btn: HTMLElement): string | null {
  return btn.classList.contains("subs-toggle") ? ".subs-toggle" : null;
}

// One stacked bar of a session's by-model split. Segments keep the server order (cost descending),
// widths are shares of the bar's total, and a zero total leaves an empty grey track.
function shareBar(kind: string, total: number, totalText: string, parts: { model: string; value: number; text: string }[]): string {
  const segs =
    total > 0
      ? parts
          .filter((p) => p.value > 0)
          .map((p) => {
            const pct = (p.value / total) * 100;
            const desc = `${p.model} ${p.text} (${pct < 1 ? "<1" : Math.round(pct)}%)`;
            return (
              `<span class="share-seg" role="img" aria-label="${esc(desc)}" title="${esc(desc)}" ` +
              `style="width:${pct.toFixed(2)}%;background:${colorOf(p.model)}"></span>`
            );
          })
          .join("")
      : "";
  return (
    `<div class="bar-row"><span class="bar-label muted small">${kind}</span>` +
    `<div class="share-track">${segs}</div><span class="bar-total mono">${esc(totalText)}</span></div>`
  );
}

// Branch graph for a session row. Built from the snapshot alone (its branches and the server clock
// now), so it changes only when a snapshot arrives, never on the one-second tick.
const BR_W = 400; // viewBox width
const BR_PITCH = 6; // px between lanes
const BR_H = 2 * 3 * BR_PITCH + 8; // 44: three lanes each side, plus margin
const BR_CURVE = 6; // horizontal extent of the fork and merge curves

function durText(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${pad(s % 60)}s`;
  return `${Math.floor(m / 60)}h ${pad(m % 60)}m`;
}

// Turn-based share: main turns that ran while a subagent was working (agentic) and the rest (talk).
function branchShare(b: LiveBranches): { p: number; q: number } | null {
  if (b.mainTurns <= 0) return null;
  const p = Math.min(100, Math.round((b.agenticTurns / b.mainTurns) * 100));
  return { p, q: 100 - p };
}

// The model most subagents ran on (the first one to reach the top count); the agentic segment uses it.
function branchModel(b: LiveBranches): string | undefined {
  const count = new Map<string, number>();
  let best: string | undefined;
  let bestN = 0;
  for (const br of b.branches) {
    if (!br.model) continue;
    const n = (count.get(br.model) ?? 0) + 1;
    count.set(br.model, n);
    if (n > bestN) {
      best = br.model;
      bestN = n;
    }
  }
  return best;
}

function branchHeader(x: LiveSession): string {
  const b = x.branches;
  const n = b.branches.length;
  const share = branchShare(b);
  const mix = share ? `agentic ${share.p}% · talk ${share.q}%` : "agentic — · talk —";
  const folded = b.maxLane > 3 ? ` · +${b.maxLane - 3} lanes folded` : "";
  return `Branches · ${n} subagent${n === 1 ? "" : "s"} · ${mix}${folded}`;
}

// Two-segment ratio bar, 60x6 px: the agentic share in the subagents' model colour, the talk share
// in the main model's colour. Empty when there are no main turns.
function branchRatio(x: LiveSession): string {
  const b = x.branches;
  const share = branchShare(b);
  if (!share) return "";
  const agentic = branchModel(b);
  const agenticColor = agentic ? colorOf(agentic) : "var(--muted)";
  const talkColor = x.mainModel ? colorOf(x.mainModel) : "var(--grid)";
  const title = `${b.agenticTurns} of ${b.mainTurns} main turns ran while subagents were working`;
  return (
    `<span class="br-ratio" title="${esc(title)}"><i style="width:${share.p}%;background:${agenticColor}"></i>` +
    `<i style="width:${share.q}%;background:${talkColor}"></i></span>`
  );
}

function branchesSvg(x: LiveSession, now: number): string {
  const b = x.branches;
  const mid = BR_H / 2;
  // The axis runs to now while the session is recent, and to its last message once it is older.
  const x1 = now - b.end < 3_600_000 ? now : b.end;
  // Long idle gaps are compressed to a fixed-width break; every x below goes through this mapper.
  const axis = compressAxis(b, x1);
  const X = axis.x;
  const f = (n: number): string => n.toFixed(2);
  const trunkColor = x.mainModel ? colorOf(x.mainModel) : "var(--muted)";
  const out: string[] = [];
  out.push(
    `<line class="br-idle" x1="0" y1="${f(mid)}" x2="${BR_W}" y2="${f(mid)}" style="stroke:var(--grid)" ` +
      `stroke-width="1" vector-effect="non-scaling-stroke"/>`,
  );
  for (const bx of axis.breaks) {
    out.push(
      `<path class="br-break" d="M${f(bx - 3)} ${f(mid + 4)}L${f(bx - 1)} ${f(mid - 4)}M${f(bx + 1)} ${f(mid + 4)}L${f(bx + 3)} ${f(mid - 4)}" ` +
        `fill="none" stroke-width="1" style="stroke:var(--muted)" vector-effect="non-scaling-stroke"/>`,
    );
  }
  for (const [a, c] of b.trunk) {
    let xa = X(a);
    let xc = X(c);
    if (xc - xa < 1.5) {
      xa = Math.min(xa, BR_W - 1.5);
      xc = xa + 1.5;
    }
    out.push(
      `<line class="br-trunk" x1="${f(xa)}" y1="${f(mid)}" x2="${f(xc)}" y2="${f(mid)}" ` +
        `style="stroke:${trunkColor}" stroke-width="2.5" vector-effect="non-scaling-stroke"/>`,
    );
  }
  for (const br of b.branches) {
    const fx = X(br.fork);
    const mx = X(br.merge);
    const ly = mid - br.lane * BR_PITCH;
    const color = br.model ? colorOf(br.model) : "var(--muted)";
    const w = mx - fx;
    let d: string;
    let end = "";
    if (w < 2 * BR_CURVE) {
      // Too short for the curves: a straight vertical tick at the middle of the branch.
      const cx = (fx + mx) / 2;
      d = `M${f(cx)} ${f(mid)}L${f(cx)} ${f(ly)}`;
      if (br.active) end = `<circle class="br-end" cx="${f(cx)}" cy="${f(ly)}" r="2" style="fill:${color}"/>`;
    } else if (br.active) {
      // Still running: leaves the trunk, runs flat, and ends at its lane.
      d =
        `M${f(fx)} ${f(mid)}C${f(fx + BR_CURVE / 2)} ${f(mid)} ${f(fx + BR_CURVE / 2)} ${f(ly)} ${f(fx + BR_CURVE)} ${f(ly)}` +
        `L${f(mx)} ${f(ly)}`;
      end = `<circle class="br-end" cx="${f(mx)}" cy="${f(ly)}" r="2" style="fill:${color}"/>`;
    } else {
      d =
        `M${f(fx)} ${f(mid)}C${f(fx + BR_CURVE / 2)} ${f(mid)} ${f(fx + BR_CURVE / 2)} ${f(ly)} ${f(fx + BR_CURVE)} ${f(ly)}` +
        `L${f(mx - BR_CURVE)} ${f(ly)}C${f(mx - BR_CURVE / 2)} ${f(ly)} ${f(mx - BR_CURVE / 2)} ${f(mid)} ${f(mx)} ${f(mid)}`;
    }
    const title = `${br.label} · ${br.model ?? "—"} · ${durText(br.merge - br.fork)}`;
    out.push(
      `<path class="br-branch" d="${d}" fill="none" stroke-width="1.5" style="stroke:${color}" ` +
        `vector-effect="non-scaling-stroke"><title>${esc(title)}</title></path>${end}`,
    );
  }
  const share = branchShare(b);
  const label = `Branches: ${b.branches.length} subagents, ${share ? `agentic ${share.p}%` : "no activity"}`;
  return (
    `<svg class="branches" viewBox="0 0 ${BR_W} ${BR_H}" preserveAspectRatio="none" role="img" ` +
    `aria-label="${esc(label)}">${out.join("")}</svg>`
  );
}

function branchesBlock(x: LiveSession, now: number): string {
  const head = branchHeader(x);
  return (
    `<div class="card-branches"><div class="br-head muted small">` +
    `<span class="br-text" title="${esc(head)}">${esc(head)}</span>${branchRatio(x)}</div>` +
    `${branchesSvg(x, now)}</div>`
  );
}

function cardHtml(x: LiveSession, now: number): string {
  const hasMain = x.mainTurns > 0;
  const pct = Math.min(100, (x.mainCtx / CTX_LIMIT) * 100);
  const level = x.mainCtx > 185_000 ? " red" : x.mainCtx > 150_000 ? " amber" : "";
  // The gauge is filled with the main model's colour; without a main model it keeps the CSS default.
  const gaugeStyle = `width:${pct.toFixed(1)}%` + (x.mainModel ? `;background:${colorOf(x.mainModel)}` : "");
  const subs = x.activeSubagents
    .map(
      (a) =>
        `<li><span class="sub-label">${esc(shorten(a.label, 70))}</span> ` +
        `<span class="muted">${esc(a.model ?? "—")}</span> <span class="mono">${kTok(a.ctx)}</span> ` +
        `<span class="mono">${tokShort(tokensOf(a.tokens, tokenKind))} tok</span></li>`,
    )
    .join("");
  const counts = `${x.mainTurns} turns · ${x.subagentCount} subagents`;
  // Two stacked bars: cost and tokens (of the selected kind), each split by model with the
  // same colours as the per-minute charts. Their segment sums equal costToday and tokToday.
  const tokTotal = tokensOf(x.tokToday, tokenKind);
  const bars =
    shareBar(
      "cost",
      x.costToday,
      usd(x.costToday),
      x.byModel.map((b) => ({ model: b.model, value: b.cost, text: usd(b.cost) })),
    ) +
    shareBar(
      "tokens",
      tokTotal,
      `${tokShort(tokTotal)} tok`,
      x.byModel.map((b) => {
        const v = tokensOf(b.tokens, tokenKind);
        return { model: b.model, value: v, text: `${tokShort(v)} tok` };
      }),
    );
  const figures =
    `<div class="money"><span>today <b>${usd(x.costToday)}</b></span>` +
    `<span>last hour <b>${usd(x.costLastHour)}</b></span>` +
    `<span>out/$ <b>${fmtOutPerUsd(outPerUsd(x.tokToday.out, x.costToday))}</b></span></div>`;
  // Active subagents fold into one line; the list shows only while this session is expanded.
  const nSubs = x.activeSubagents.length;
  const subsOn = nSubs > 0 && subsOpen.has(x.id);
  // The toggle sits under the title line; the expanded list is a full-width row below the figures.
  const toggle =
    nSubs === 0
      ? ""
      : `<button type="button" class="subs-toggle" aria-expanded="${String(subsOn)}">` +
        `${subsOn ? "▾" : "▸"} ${nSubs} active subagent${nSubs === 1 ? "" : "s"}</button>`;
  const subsBlock = subsOn ? `<div class="card-subs"><ul class="subs">${subs}</ul></div>` : "";
  const modelFull = x.mainModel ?? "main";
  const modelShort = modelFull.replace(/^claude-/, "");
  return (
    `<div class="card" data-id="${esc(x.id)}" data-last="${x.lastActivity}">` +
    `<span class="dot" aria-hidden="true"></span>` +
    `<div class="card-main">` +
    `<a class="card-link" href="/?session=${encodeURIComponent(x.id)}&amp;live=1"><span class="ctitle">${esc(shorten(x.title, 120))}</span></a>` +
    `<div class="muted small card-meta">${esc(x.projectDir)} · <span class="ago"></span></div>` +
    toggle +
    `</div>` +
    branchesBlock(x, now) +
    `<div class="card-ctx">` +
    `<div class="ctxrow"><span title="${esc(modelFull)}">${esc(modelShort)}</span>` +
    `<span class="mono${level}">${hasMain ? `${kTok(x.mainCtx)}/200k` : "—"}</span></div>` +
    `<div class="gauge"><i style="${gaugeStyle}"></i></div>` +
    `<div class="muted small">${counts}</div>` +
    `</div>` +
    `<div class="card-figs">${figures}<div class="card-bars">${bars}</div></div>` +
    subsBlock +
    `</div>`
  );
}

// Keeps "last activity" text and the live marker current between snapshots.
function tick(): void {
  const now = Date.now() + serverOffset;
  document.querySelectorAll<HTMLElement>(".card[data-last]").forEach((el) => {
    const sec = (now - Number(el.dataset.last)) / 1000;
    el.classList.toggle("is-live", sec < LIVE_SEC);
    const ago = el.querySelector(".ago");
    if (ago) ago.textContent = `last activity ${agoText(sec)}`;
  });
}

function setConn(ok: boolean): void {
  $<HTMLElement>("conn").className = `conn ${ok ? "ok" : "off"}`;
  $<HTMLElement>("connText").textContent = ok ? "connected" : "reconnecting…";
}

// Hovering or tapping a minute on either chart selects it on both.
function selectMinute(target: EventTarget | null): void {
  const el = (target as Element | null)?.closest?.("[data-t]");
  if (!el || !snap) return;
  const t = Number(el.getAttribute("data-t"));
  if (t === selectedT) return;
  selectedT = t;
  document.querySelectorAll<Element>(".chart .col").forEach((c) => {
    c.classList.toggle("sel", Number(c.getAttribute("data-t")) === selectedT);
  });
  renderReadout(snap);
  renderTokReadout(snap);
}

function updateKindButtons(): void {
  document.querySelectorAll<HTMLButtonElement>("#tokKind button[data-kind]").forEach((b) => {
    b.setAttribute("aria-pressed", String(b.dataset.kind === tokenKind));
  });
}

function render(s: LiveSnapshot): void {
  snap = s;
  registerModels(s);
  // The selected minute can leave the 60-minute window; then fall back to the newest minute.
  if (selectedT !== null && !s.perMinute.some((m) => m.t === selectedT)) selectedT = null;
  if (selectedT === null && s.perMinute.length > 0) selectedT = s.perMinute[s.perMinute.length - 1].t;
  renderTotals(s);
  renderLegend();
  renderChart(s);
  renderTokens(s);
  renderReadout(s);
  renderCards(s);
  tick();
}

function connect(): void {
  const es = new EventSource("/api/live/stream");
  es.addEventListener("snapshot", (ev) => {
    const s = JSON.parse((ev as MessageEvent<string>).data) as LiveSnapshot;
    serverOffset = s.now - Date.now();
    setConn(true);
    render(s);
  });
  es.onopen = () => setConn(true);
  // EventSource reconnects on its own; the dot goes grey until a snapshot arrives again.
  es.onerror = () => setConn(false);
}

const chartEl = $<HTMLElement>("chart");
const tokChartEl = $<HTMLElement>("tokChart");
for (const el of [chartEl, tokChartEl]) {
  el.addEventListener("pointerover", (e) => selectMinute(e.target));
  el.addEventListener("pointerdown", (e) => selectMinute(e.target));
}

$<HTMLElement>("tokKind").addEventListener("click", (e) => {
  const btn = (e.target as Element | null)?.closest?.("button[data-kind]") as HTMLButtonElement | null;
  const k = btn?.dataset.kind;
  if (!isTokenKind(k) || k === tokenKind) return;
  tokenKind = k;
  saveTokenKind(k);
  updateKindButtons();
  if (snap) {
    renderTokens(snap);
    renderCards(snap); // token-mode cards show the newly selected kind
  }
});
updateKindButtons();

let lastWidth = 0;
const redraw = (): void => {
  if (!snap || chartEl.clientWidth === lastWidth) return;
  lastWidth = chartEl.clientWidth;
  renderChart(snap);
  renderTokens(snap);
};
if (typeof ResizeObserver !== "undefined") {
  const ro = new ResizeObserver(redraw);
  ro.observe(chartEl);
  ro.observe(tokChartEl);
} else {
  window.addEventListener("resize", redraw);
}

function setSubsOpen(id: string, open: boolean, keyboard: boolean): void {
  if (subsOpen.has(id) === open) return;
  if (open) subsOpen.add(id);
  else subsOpen.delete(id);
  saveSubsOpen();
  if (snap) renderCards(snap);
  // A keyboard press replaces the subagent toggle it came from, so put focus back on the new one.
  if (keyboard) cardEls.get(id)?.querySelector<HTMLButtonElement>(".subs-toggle")?.focus();
}

// One delegated listener on the cards container handles the subagent toggle of every card.
function controlOf(e: Event): string | null {
  const btn = (e.target as Element | null)?.closest?.("button.subs-toggle") as HTMLButtonElement | null;
  const id = btn?.closest?.(".card")?.getAttribute("data-id");
  return btn && id ? id : null;
}

const cardsEl = $<HTMLElement>("cards");

// Ends the press: drops the hold and applies any newer HTML. The re-render runs as a macrotask so
// the click that follows pointerup is dispatched to the button before it is replaced.
function releaseHold(): void {
  if (!held) return;
  clearTimeout(held.timer);
  held = null;
  document.removeEventListener("pointerup", releaseHold);
  document.removeEventListener("pointercancel", releaseHold);
  document.removeEventListener("lostpointercapture", releaseHold);
  setTimeout(() => {
    if (snap) renderCards(snap);
  }, 0);
}

// A press on the subagent toggle holds its card. Touch scrolling starts a pointercancel, which ends the press
// with no click. Releasing outside the button also ends it, and the click then lands on the cards
// container, so no toggle happens. Nothing here calls preventDefault, so scrolling still works.
cardsEl.addEventListener("pointerdown", (e) => {
  if (e.pointerType === "mouse" && e.button !== 0) return;
  const id = controlOf(e);
  if (!id) return;
  if (held) releaseHold();
  held = { id, timer: setTimeout(releaseHold, HOLD_MS) };
  document.addEventListener("pointerup", releaseHold);
  document.addEventListener("pointercancel", releaseHold);
  document.addEventListener("lostpointercapture", releaseHold);
});
// The only action. Mouse, touch tap, and keyboard Enter or Space all end in click. Keyboard
// activation has detail 0 and keeps the focus restore.
cardsEl.addEventListener("click", (e) => {
  const id = controlOf(e);
  if (!id) return;
  setSubsOpen(id, !subsOpen.has(id), e.detail === 0);
});

setInterval(tick, 1000);
connect();
