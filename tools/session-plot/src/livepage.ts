// Browser client for /live. No external libraries; the charts are hand-built SVG.
import { isTokenKind, minuteTokens, minuteTokenTotal, TOKEN_KIND_LABELS, tokensOf, type TokenKind } from "./tokens";
import type { LiveMinute, LiveSession, LiveSnapshot } from "./types";

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
let models: string[] = [];
let tokenKind: TokenKind = loadTokenKind();

// Per-card figures mode, keyed by session id. Stored as one JSON object in localStorage.
type CardMode = "cost" | "tokens";
const CARD_MODE_KEY = "session-plot.cardMode";
let cardMode: Map<string, CardMode> = loadCardModes();
// Rendered card node and the HTML it was built from, per session id (see renderCards).
const cardEls = new Map<string, HTMLElement>();
const cardHtmls = new Map<string, string>();
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

function isCardMode(v: unknown): v is CardMode {
  return v === "cost" || v === "tokens";
}

function loadCardModes(): Map<string, CardMode> {
  const map = new Map<string, CardMode>();
  try {
    const raw: unknown = JSON.parse(localStorage.getItem(CARD_MODE_KEY) ?? "{}");
    if (raw && typeof raw === "object") {
      for (const [id, v] of Object.entries(raw)) if (isCardMode(v)) map.set(id, v);
    }
  } catch {
    // Storage blocked or not JSON: every card starts in Cost mode.
  }
  return map;
}

function saveCardModes(): void {
  try {
    localStorage.setItem(CARD_MODE_KEY, JSON.stringify(Object.fromEntries(cardMode)));
  } catch {
    // Not persisted; the modes still apply until the page reloads.
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

function collectModels(s: LiveSnapshot): string[] {
  const set = new Set<string>();
  for (const m of s.perMinute) for (const k of Object.keys(m.byModel)) set.add(k);
  return [...set].sort();
}

function renderTotals(s: LiveSnapshot): void {
  $<HTMLElement>("tToday").textContent = usd(s.totals.costToday);
  $<HTMLElement>("tHour").textContent = usd(s.totals.costLastHour);
  $<HTMLElement>("tOut").textContent = tokens(s.totals.outLastHour);
  $<HTMLElement>("updated").textContent = `updated ${hhmmss(s.now)}`;
}

function renderLegend(): void {
  const html =
    models.length === 0
      ? `<span class="muted">no model activity in the last hour</span>`
      : models.map((m) => `<span><i class="sw" style="background:${colorOf(m)}"></i>${esc(m)}</span>`).join("");
  $<HTMLElement>("legend").innerHTML = html;
  $<HTMLElement>("tokLegend").innerHTML = html;
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
  let refocus: { id: string; mode: CardMode } | null = null;
  s.sessions.forEach((x, idx) => {
    const html = cardHtml(x);
    let el = cardEls.get(x.id);
    if (!el) {
      el = parseCard(html);
      cardEls.set(x.id, el);
      cardHtmls.set(x.id, html);
    } else if (cardHtmls.get(x.id) !== html && held?.id !== x.id) {
      const active = document.activeElement as HTMLElement | null;
      const focusMode = active && el.contains(active) && active.tagName === "BUTTON" ? active.dataset.mode : undefined;
      if (isCardMode(focusMode)) refocus = { id: x.id, mode: focusMode };
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
    const r: { id: string; mode: CardMode } = refocus;
    cardEls.get(r.id)?.querySelector<HTMLButtonElement>(`button[data-mode="${r.mode}"]`)?.focus({ preventScroll: true });
  }
}

function cardHtml(x: LiveSession): string {
  const hasMain = x.mainTurns > 0;
  const pct = Math.min(100, (x.mainCtx / CTX_LIMIT) * 100);
  const level = x.mainCtx > 185_000 ? " red" : x.mainCtx > 150_000 ? " amber" : "";
  const tokMode = cardMode.get(x.id) === "tokens";
  const subs = x.activeSubagents
    .map((a) => {
      const fig = tokMode ? ` <span class="mono">${tokShort(tokensOf(a.tokens, tokenKind))} tok</span>` : "";
      return (
        `<li><span class="sub-label">${esc(shorten(a.label, 70))}</span> ` +
        `<span class="muted">${esc(a.model ?? "—")}</span> <span class="mono">${kTok(a.ctx)}</span>${fig}</li>`
      );
    })
    .join("");
  const figures = tokMode
    ? `<div class="muted small card-kind">tokens: ${esc(TOKEN_KIND_LABELS[tokenKind].toLowerCase())}</div>` +
      `<div class="money"><span>today <b>${tokShort(tokensOf(x.tokToday, tokenKind))} tok</b></span>` +
      `<span>last hour <b>${tokShort(tokensOf(x.tokLastHour, tokenKind))} tok</b></span>` +
      `<span class="muted">${x.mainTurns} turns · ${x.subagentCount} subagents</span></div>`
    : `<div class="money"><span>today <b>${usd(x.costToday)}</b></span>` +
      `<span>last hour <b>${usd(x.costLastHour)}</b></span>` +
      `<span class="muted">${x.mainTurns} turns · ${x.subagentCount} subagents</span></div>`;
  // The toggle sits above the card's stretched title link, so a press on it never navigates.
  const toggle =
    `<div class="mode-toggle" role="group" aria-label="Card figures">` +
    `<button type="button" class="mode-btn" data-mode="cost" aria-pressed="${String(!tokMode)}">Cost</button>` +
    `<button type="button" class="mode-btn" data-mode="tokens" aria-pressed="${String(tokMode)}">Tokens</button>` +
    `</div>`;
  return (
    `<div class="card" data-id="${esc(x.id)}" data-last="${x.lastActivity}">` +
    `<div class="card-head"><span class="dot" aria-hidden="true"></span>` +
    `<a class="card-link" href="/?session=${encodeURIComponent(x.id)}&amp;live=1"><span class="ctitle">${esc(shorten(x.title, 120))}</span></a>` +
    toggle +
    `</div>` +
    `<div class="muted small">${esc(x.projectDir)} · <span class="ago"></span></div>` +
    `<div class="ctxrow"><span>${esc(x.mainModel ?? "main")}</span>` +
    `<span class="mono">${hasMain ? `${kTok(x.mainCtx)} / 200k` : "—"}</span></div>` +
    `<div class="gauge${level}"><i style="width:${pct.toFixed(1)}%"></i></div>` +
    figures +
    (subs ? `<div class="muted small">active subagents</div><ul class="subs">${subs}</ul>` : "") +
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
  models = collectModels(s);
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

function setCardMode(id: string, mode: CardMode, keyboard: boolean): void {
  if (cardMode.get(id) === mode) return;
  cardMode.set(id, mode);
  saveCardModes();
  if (snap) renderCards(snap);
  tick();
  // A keyboard press replaces the button it came from, so put focus back on the new one.
  if (keyboard) cardEls.get(id)?.querySelector<HTMLButtonElement>(`button[data-mode="${mode}"]`)?.focus();
}

// One delegated listener on the cards container handles every card's toggle.
function modeButtonOf(e: Event): { id: string; mode: CardMode } | null {
  const btn = (e.target as Element | null)?.closest?.("button[data-mode]") as HTMLButtonElement | null;
  const id = btn?.closest?.(".card")?.getAttribute("data-id");
  const mode = btn?.dataset.mode;
  if (!btn || !id || !isCardMode(mode)) return null;
  return { id, mode };
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

// A press on a toggle holds its card. Touch scrolling starts a pointercancel, which ends the press
// with no click. Releasing outside the button also ends it, and the click then lands on the cards
// container, so no toggle happens. Nothing here calls preventDefault, so scrolling still works.
cardsEl.addEventListener("pointerdown", (e) => {
  if (e.pointerType === "mouse" && e.button !== 0) return;
  const hit = modeButtonOf(e);
  if (!hit) return;
  if (held) releaseHold();
  held = { id: hit.id, timer: setTimeout(releaseHold, HOLD_MS) };
  document.addEventListener("pointerup", releaseHold);
  document.addEventListener("pointercancel", releaseHold);
  document.addEventListener("lostpointercapture", releaseHold);
});
// The only action. Mouse, touch tap, and keyboard Enter or Space all end in click. Keyboard
// activation has detail 0 and keeps the focus restore.
cardsEl.addEventListener("click", (e) => {
  const hit = modeButtonOf(e);
  if (hit) setCardMode(hit.id, hit.mode, e.detail === 0);
});

setInterval(tick, 1000);
connect();
