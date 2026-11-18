// Browser client for /live. No external libraries; the chart is hand-built SVG.
import type { LiveMinute, LiveSession, LiveSnapshot } from "./types";

const CTX_LIMIT = 200_000;
const LIVE_SEC = 120;
const MODEL_VARS = ["--m1", "--m2", "--m3", "--m4", "--m5", "--m6"];
const USD_STEPS = [0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000];
const ESC: Record<string, string> = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

let snap: LiveSnapshot | null = null;
let serverOffset = 0; // server clock minus local clock, ms
let selectedT: number | null = null; // minute (epoch ms) shown in the readout
let models: string[] = [];

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

function minuteCost(m: LiveMinute): number {
  let total = 0;
  for (const v of Object.values(m.byModel)) total += v.cost;
  return total;
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
  $<HTMLElement>("legend").innerHTML =
    models.length === 0
      ? `<span class="muted">no model activity in the last hour</span>`
      : models.map((m) => `<span><i class="sw" style="background:${colorOf(m)}"></i>${esc(m)}</span>`).join("");
}

function renderChart(s: LiveSnapshot): void {
  const box = $<HTMLElement>("chart");
  const W = Math.max(280, Math.round(box.clientWidth || 343));
  const H = 220;
  const LEFT = 44;
  const RIGHT = 8;
  const TOP = 10;
  const BOTTOM = 24;
  const plotW = W - LEFT - RIGHT;
  const plotH = H - TOP - BOTTOM;
  const n = s.perMinute.length;
  const maxCost = Math.max(0, ...s.perMinute.map(minuteCost));
  const step = niceStep(maxCost * 1.1, 4, USD_STEPS);
  const yMax = Math.max(step, Math.ceil((maxCost * 1.1) / step) * step);
  const Y = (v: number): number => TOP + plotH - (v / yMax) * plotH;
  const slot = plotW / n;
  const bw = Math.max(2, slot * 0.72);

  const out: string[] = [];
  out.push(
    `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="Cost per minute, last 60 minutes, by model">`,
  );
  const nY = Math.round(yMax / step);
  const dec = Number.isInteger(step) ? 0 : 2; // 0.5 steps need two decimals; whole steps need none
  for (let i = 0; i <= nY; i++) {
    const v = i * step;
    const y = Y(v).toFixed(1);
    out.push(`<line class="grid" x1="${LEFT}" x2="${W - RIGHT}" y1="${y}" y2="${y}"/>`);
    out.push(
      `<text class="tick" x="${LEFT - 6}" y="${(Y(v) + 4).toFixed(1)}" text-anchor="end">$${v.toFixed(dec)}</text>`,
    );
  }

  s.perMinute.forEach((m, i) => {
    const x = LEFT + i * slot + (slot - bw) / 2;
    let acc = 0;
    for (const model of models) {
      const v = m.byModel[model]?.cost ?? 0;
      if (v <= 0) continue;
      const y0 = Y(acc);
      acc += v;
      const y1 = Y(acc);
      out.push(
        `<rect class="seg" x="${x.toFixed(1)}" y="${y1.toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(0.5, y0 - y1).toFixed(1)}" style="fill:${colorOf(model)}"><title>${esc(hhmm(m.t))} ${esc(model)} ${usd(v)}</title></rect>`,
      );
    }
  });

  // Labels every 15 minutes, plus the newest minute.
  s.perMinute.forEach((m, i) => {
    if (new Date(m.t).getMinutes() % 15 !== 0 && i !== n - 1) return;
    const x = LEFT + i * slot + slot / 2;
    out.push(`<text class="tick" x="${x.toFixed(1)}" y="${H - 6}" text-anchor="middle">${hhmm(m.t)}</text>`);
  });

  // Transparent full-height hit columns (hover or tap for the minute breakdown).
  s.perMinute.forEach((m, i) => {
    const sel = m.t === selectedT ? " sel" : "";
    out.push(
      `<rect class="col${sel}" data-t="${m.t}" x="${(LEFT + i * slot).toFixed(1)}" y="${TOP}" width="${slot.toFixed(1)}" height="${plotH}"/>`,
    );
  });
  out.push("</svg>");
  box.innerHTML = out.join("");
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

function renderCards(s: LiveSnapshot): void {
  const grid = $<HTMLElement>("cards");
  if (s.sessions.length === 0) {
    grid.innerHTML = `<p class="muted">No session activity in the last 24 h.</p>`;
    return;
  }
  grid.innerHTML = s.sessions.map(cardHtml).join("");
}

function cardHtml(x: LiveSession): string {
  const hasMain = x.mainTurns > 0;
  const pct = Math.min(100, (x.mainCtx / CTX_LIMIT) * 100);
  const level = x.mainCtx > 185_000 ? " red" : x.mainCtx > 150_000 ? " amber" : "";
  const subs = x.activeSubagents
    .map(
      (a) =>
        `<li><span class="sub-label">${esc(shorten(a.label, 70))}</span> ` +
        `<span class="muted">${esc(a.model ?? "—")}</span> <span class="mono">${kTok(a.ctx)}</span></li>`,
    )
    .join("");
  return (
    `<a class="card" href="/?session=${encodeURIComponent(x.id)}&amp;live=1" data-last="${x.lastActivity}">` +
    `<div class="card-head"><span class="dot" aria-hidden="true"></span><span class="ctitle">${esc(shorten(x.title, 120))}</span></div>` +
    `<div class="muted small">${esc(x.projectDir)} · <span class="ago"></span></div>` +
    `<div class="ctxrow"><span>${esc(x.mainModel ?? "main")}</span>` +
    `<span class="mono">${hasMain ? `${kTok(x.mainCtx)} / 200k` : "—"}</span></div>` +
    `<div class="gauge${level}"><i style="width:${pct.toFixed(1)}%"></i></div>` +
    `<div class="money"><span>today <b>${usd(x.costToday)}</b></span>` +
    `<span>last hour <b>${usd(x.costLastHour)}</b></span>` +
    `<span class="muted">${x.mainTurns} turns · ${x.subagentCount} subagents</span></div>` +
    (subs ? `<div class="muted small">active subagents</div><ul class="subs">${subs}</ul>` : "") +
    `</a>`
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

function selectMinute(target: EventTarget | null): void {
  const el = (target as Element | null)?.closest?.("[data-t]");
  if (!el || !snap) return;
  const t = Number(el.getAttribute("data-t"));
  if (t === selectedT) return;
  selectedT = t;
  document.querySelectorAll<Element>("#chart .col").forEach((c) => {
    c.classList.toggle("sel", Number(c.getAttribute("data-t")) === selectedT);
  });
  renderReadout(snap);
}

function render(s: LiveSnapshot): void {
  snap = s;
  models = collectModels(s);
  if (selectedT === null && s.perMinute.length > 0) selectedT = s.perMinute[s.perMinute.length - 1].t;
  renderTotals(s);
  renderLegend();
  renderChart(s);
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
chartEl.addEventListener("pointerover", (e) => selectMinute(e.target));
chartEl.addEventListener("pointerdown", (e) => selectMinute(e.target));

let lastWidth = 0;
const redraw = (): void => {
  if (!snap || chartEl.clientWidth === lastWidth) return;
  lastWidth = chartEl.clientWidth;
  renderChart(snap);
};
if (typeof ResizeObserver !== "undefined") new ResizeObserver(redraw).observe(chartEl);
else window.addEventListener("resize", redraw);

setInterval(tick, 1000);
connect();
