import { test, mock } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { JSDOM } from "jsdom";
import type { LiveSession, LiveSnapshot } from "../src/types";

// Drives the real browser client (src/livepage.ts) in jsdom. The module starts itself on import,
// so the DOM, EventSource and timers are set up before the import. The EventSource stub keeps the
// last "snapshot" listener, and each test pushes snapshots through it.
const T0 = 1_800_000_000_000;
const here = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(here, "..", "live.html"), "utf8");
const body = html.slice(html.indexOf("<body"), html.indexOf("</body>")).replace(/^<body[^>]*>/, "");

let snapshotListener: ((ev: { data: string }) => void) | undefined;
class FakeEventSource {
  onopen: unknown = null;
  onerror: unknown = null;
  constructor(_url: string) {}
  addEventListener(type: string, fn: (ev: { data: string }) => void): void {
    if (type === "snapshot") snapshotListener = fn;
  }
}

const dom = new JSDOM(`<!doctype html><html><body>${body}</body></html>`, { url: "http://127.0.0.1/live" });
const w = dom.window;
for (const [k, v] of Object.entries({ window: w, document: w.document, localStorage: w.localStorage, EventSource: FakeEventSource })) {
  Object.defineProperty(globalThis, k, { value: v, configurable: true, writable: true });
}

// Fake clock: Date and setInterval (livepage's one-second tick) advance only when told to.
mock.timers.enable({ apis: ["Date", "setInterval"], now: T0 });

function session(id: string, ago: number, costToday = 1): LiveSession {
  return {
    id,
    projectDir: `-home-example-${id}`,
    title: `session ${id}`,
    lastActivity: T0 - ago,
    mainModel: "model-a",
    mainCtx: 1000,
    mainPeakCtx: 1000,
    mainTurns: 2,
    costToday,
    costLastHour: 0.5,
    tokToday: { in: 10, cacheWrite: 0, cacheRead: 0, out: 5 },
    tokLastHour: { in: 4, cacheWrite: 0, cacheRead: 0, out: 2 },
    byModel: [],
    subagentCount: 0,
    activeSubagents: [],
  };
}

function subagent(id: string, ago: number): LiveSession["activeSubagents"][number] {
  return {
    id,
    label: `sub ${id}`,
    model: "model-a",
    ctx: 100,
    lastActivity: T0 - ago,
    costUsd: 0.1,
    tokens: { in: 1, cacheWrite: 0, cacheRead: 0, out: 1 },
  };
}

function snapshot(sessions: LiveSession[]): LiveSnapshot {
  return { now: Date.now(), windowMin: 60, totals: { costToday: 1, costLastHour: 0.5, outLastHour: 2, outToday: 3 }, perMinute: [], sessions };
}

function push(s: LiveSnapshot): void {
  if (!snapshotListener) throw new Error("livepage did not subscribe to snapshots");
  snapshotListener({ data: JSON.stringify(s) });
}

function metaTexts(): Array<{ id: string; ago: string }> {
  return [...w.document.querySelectorAll<HTMLElement>(".card")].map((c) => ({
    id: c.dataset.id ?? "",
    ago: c.querySelector(".ago")?.textContent ?? "",
  }));
}

function kindButton(kind: string): HTMLButtonElement {
  const btn = w.document.querySelector<HTMLButtonElement>(`#tokKind button[data-kind="${kind}"]`);
  if (!btn) throw new Error(`no token kind button ${kind}`);
  return btn;
}

// A mouse click as a browser dispatches it: detail 1 for a pointer press, 0 for keyboard activation.
function clickButton(btn: Element, detail = 1): void {
  btn.dispatchEvent(new w.MouseEvent("click", { bubbles: true, detail }));
}

// Pointer release and cancel reach the document, as they do in a browser.
function releaseOn(type: "pointerup" | "pointercancel", target: Element): void {
  target.dispatchEvent(new w.MouseEvent(type, { bubbles: true }));
}

// The subagent toggle's aria-expanded value ("true" or "false"), or undefined without a toggle.
function openOf(id: string): string | null | undefined {
  return subsToggle(id)?.getAttribute("aria-expanded");
}

function flipped(v: string | null | undefined): string {
  return v === "true" ? "false" : "true";
}

// A session with one active subagent, so its card has the subagent toggle.
function withSub(s: LiveSession): LiveSession {
  return { ...s, activeSubagents: [subagent(`${s.id}-s`, 1_000)], subagentCount: 1 };
}

function subsButton(id: string): HTMLButtonElement {
  const btn = subsToggle(id);
  if (!btn) throw new Error(`no subagent toggle for ${id}`);
  return btn;
}

function cardText(id: string): string {
  return w.document.querySelector<HTMLElement>(`.card[data-id="${id}"]`)?.textContent ?? "";
}

// Lets the post-release macrotask run.
function flush(): Promise<void> {
  return new Promise((r) => setTimeout(r, 5));
}

function pointerPress(btn: HTMLButtonElement): void {
  btn.dispatchEvent(new w.MouseEvent("pointerdown", { bubbles: true, button: 0 }));
}

function keyboardActivate(btn: HTMLButtonElement): void {
  // A keyboard activation dispatches click with detail 0.
  btn.dispatchEvent(new w.MouseEvent("click", { bubbles: true, detail: 0 }));
}

// Loaded once, after the DOM and fake clock exist.
const loaded = import("../src/livepage");

function subsToggle(id: string): HTMLButtonElement | null {
  return w.document.querySelector<HTMLButtonElement>(`.card[data-id="${id}"] .subs-toggle`);
}

test("active subagents fold into one toggle line: none, singular, plural", async () => {
  await loaded;
  push(snapshot([session("f0", 5_000)]));
  assert.equal(subsToggle("f0"), null, "zero subagents render no line");

  const one = { ...session("f1", 5_000), activeSubagents: [subagent("x1", 1_000)], subagentCount: 1 };
  push(snapshot([one]));
  assert.equal(subsToggle("f1")?.textContent, "▸ 1 active subagent");
  assert.equal(subsToggle("f1")?.getAttribute("aria-expanded"), "false");
  assert.equal(w.document.querySelector(`.card[data-id="f1"] ul.subs`), null, "list hidden while collapsed");

  const two = { ...session("f2", 5_000), activeSubagents: [subagent("y1", 1_000), subagent("y2", 2_000)], subagentCount: 2 };
  push(snapshot([two]));
  assert.equal(subsToggle("f2")?.textContent, "▸ 2 active subagents");
});

test("expanding a subagent line shows its list, survives snapshots, and collapses again", async () => {
  await loaded;
  const two = { ...session("g1", 5_000), activeSubagents: [subagent("z1", 1_000), subagent("z2", 2_000)], subagentCount: 2 };
  push(snapshot([two]));
  clickButton(subsToggle("g1")!);
  assert.equal(subsToggle("g1")?.getAttribute("aria-expanded"), "true");
  assert.equal(subsToggle("g1")?.textContent, "▾ 2 active subagents");
  assert.equal(w.document.querySelectorAll(`.card[data-id="g1"] ul.subs li`).length, 2);

  push(snapshot([{ ...two, costToday: 9 }]));
  assert.equal(subsToggle("g1")?.getAttribute("aria-expanded"), "true", "expansion kept across a re-render");

  clickButton(subsToggle("g1")!);
  assert.equal(subsToggle("g1")?.getAttribute("aria-expanded"), "false");
  assert.equal(w.document.querySelector(`.card[data-id="g1"] ul.subs`), null);
});

test("keyboard activation of the subagent toggle keeps focus on the rebuilt toggle", async () => {
  await loaded;
  const two = { ...session("g2", 5_000), activeSubagents: [subagent("q1", 1_000), subagent("q2", 2_000)], subagentCount: 2 };
  push(snapshot([two]));
  const btn = subsToggle("g2")!;
  btn.focus();
  keyboardActivate(btn);
  const now = subsToggle("g2");
  assert.equal(now?.getAttribute("aria-expanded"), "true");
  assert.equal(w.document.activeElement, now, "focus is on the new toggle");
});

test("by-model bars split cost and tokens by model, sum to the totals, and render an empty track at zero", async () => {
  await loaded;
  const base = session("h1", 5_000, 3);
  const withShares: LiveSession = {
    ...base,
    byModel: [
      { model: "model-a", cost: 2, tokens: { in: 6, cacheWrite: 0, cacheRead: 0, out: 4 } },
      { model: "model-b", cost: 1, tokens: { in: 4, cacheWrite: 0, cacheRead: 0, out: 1 } },
    ],
  };
  push(snapshot([withShares]));
  const segs = [...w.document.querySelectorAll<HTMLElement>(`.card[data-id="h1"] .share-seg`)];
  assert.equal(segs.length, 4, "two models in the cost bar and two in the tokens bar");
  const costSegs = segs.slice(0, 2);
  assert.equal(costSegs[0].style.width, "66.67%");
  assert.equal(costSegs[0].getAttribute("aria-label")?.startsWith("model-a"), true);
  assert.match(costSegs[0].getAttribute("aria-label") ?? "", /\(67%\)$/);
  assert.equal(costSegs[0].getAttribute("title"), costSegs[0].getAttribute("aria-label"));
  const totals = [...w.document.querySelectorAll<HTMLElement>(`.card[data-id="h1"] .bar-total`)].map((t) => t.textContent);
  assert.equal(totals[0], "$3.00");
  assert.equal(totals[1], "15 tok");

  push(snapshot([{ ...session("h2", 5_000, 0), byModel: [] }]));
  const empty = w.document.querySelector<HTMLElement>(`.card[data-id="h2"] .share-track`);
  assert.equal(empty?.querySelectorAll(".share-seg").length, 0, "zero total leaves an empty track");
});

test("every card keeps its last-activity text across toggles, kind changes and unchanged snapshots", async () => {
  await loaded;
  push(snapshot([withSub(session("aaa", 5_000)), withSub(session("bbb", 40_000)), session("ccc", 90_000)]));
  for (const c of metaTexts()) assert.match(c.ago, /^last activity /, `initial ${c.id}`);

  // Pointer toggle on the first card, keyboard toggle on the second.
  clickButton(subsButton("aaa"));
  keyboardActivate(subsButton("bbb"));
  for (const c of metaTexts()) assert.match(c.ago, /^last activity /, `after toggles ${c.id}`);

  // Changing the token kind rebuilds the cards. The text must be there before any tick.
  kindButton("output").dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
  for (const c of metaTexts()) assert.match(c.ago, /^last activity /, `after kind change ${c.id}`);

  // Same snapshot again: nothing is rebuilt, and the text is still there.
  push(snapshot([withSub(session("aaa", 5_000)), withSub(session("bbb", 40_000)), session("ccc", 90_000)]));
  for (const c of metaTexts()) assert.match(c.ago, /^last activity /, `after unchanged snapshot ${c.id}`);
});

test("the relative time keeps advancing on a card whose HTML did not change", async () => {
  await loaded;
  const before = metaTexts().find((c) => c.id === "ccc")?.ago;
  mock.timers.tick(30_000);
  const after = metaTexts().find((c) => c.id === "ccc")?.ago;
  assert.notEqual(after, before);
  assert.match(after ?? "", /^last activity /);
});

test("keyboard focus stays on the subagent toggle when its card is rebuilt", async () => {
  await loaded;
  const btn = subsButton("bbb");
  btn.focus();
  assert.equal(w.document.activeElement, btn);
  // A kind change rebuilds the card, including the focused toggle (its subagent line shows the
  // token count of the selected kind, so the card's HTML changes).
  const other = kindButton("cacheRead").getAttribute("aria-pressed") === "true" ? "output" : "cacheRead";
  kindButton(other).dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
  assert.equal(btn.isConnected, false, "the card was rebuilt");
  const focused = w.document.activeElement as HTMLElement | null;
  assert.equal(focused?.classList.contains("subs-toggle"), true);
  assert.equal(focused?.closest(".card")?.getAttribute("data-id"), "bbb");
  assert.equal(focused?.isConnected, true);
});

const SESSIONS = (costAaa = 1) => [withSub(session("aaa", 5_000, costAaa)), withSub(session("bbb", 40_000)), withSub(session("ccc", 90_000))];

test("a press cancelled by the browser (touch scroll) does not toggle", async () => {
  await loaded;
  push(snapshot(SESSIONS()));
  const before = openOf("ccc");
  pointerPress(subsButton("ccc"));
  releaseOn("pointercancel", w.document.body);
  await flush();
  assert.equal(openOf("ccc"), before);
});

test("a snapshot during a press keeps the button; the click after release toggles once and shows the newest numbers", async () => {
  await loaded;
  push(snapshot(SESSIONS()));
  const before = openOf("aaa");
  const btn = subsButton("aaa");
  let clicks = 0;
  btn.addEventListener("click", () => clicks++);
  pointerPress(btn);
  push(snapshot(SESSIONS(123.45)));
  assert.equal(btn.isConnected, true, "the held card's button was not replaced");
  assert.equal(openOf("aaa"), before, "the snapshot did not toggle the card");
  releaseOn("pointerup", btn);
  clickButton(btn);
  assert.equal(clicks, 1, "the click reached the same button node");
  assert.equal(openOf("aaa"), flipped(before), "toggled exactly once");
  await flush();
  assert.match(cardText("aaa"), /\$123\.45/, "the card shows the newest snapshot after release");
});

test("keyboard Enter (click with detail 0) toggles once and keeps focus on the new button", async () => {
  await loaded;
  push(snapshot(SESSIONS()));
  const before = openOf("bbb");
  clickButton(subsButton("bbb"), 0);
  assert.equal(openOf("bbb"), flipped(before));
  assert.equal(w.document.activeElement, subsButton("bbb"));
});

test("a click with no preceding pointerdown (synthetic) toggles once", async () => {
  await loaded;
  push(snapshot(SESSIONS()));
  const before = openOf("ccc");
  clickButton(subsButton("ccc"), 1);
  assert.equal(openOf("ccc"), flipped(before));
});

test("a mouse press dragged off the button and released elsewhere does not toggle", async () => {
  await loaded;
  push(snapshot(SESSIONS()));
  const before = openOf("bbb");
  pointerPress(subsButton("bbb"));
  releaseOn("pointerup", w.document.getElementById("cards") as Element);
  // A click on the common ancestor (the cards container), as the browser dispatches it.
  clickButton(w.document.getElementById("cards") as Element);
  await flush();
  assert.equal(openOf("bbb"), before);
});

test("the hold ends after 2 s without a release, so a held card catches up", async () => {
  await loaded;
  push(snapshot(SESSIONS()));
  const btn = subsButton("aaa");
  pointerPress(btn);
  push(snapshot(SESSIONS(77.5)));
  assert.equal(btn.isConnected, true);
  await new Promise((r) => setTimeout(r, 2_100));
  await flush();
  assert.equal(btn.isConnected, false, "the safety release re-rendered the card");
  assert.match(cardText("aaa"), /\$77\.50/);
});

test("a card has no Cost/Tokens figures toggle and no 'tokens:' label; figures are always cost", async () => {
  await loaded;
  push(snapshot([withSub(session("tg1", 5_000))]));
  const card = w.document.querySelector<HTMLElement>(`.card[data-id="tg1"]`);
  assert.ok(card);
  assert.equal(card.querySelector(".mode-toggle, .mode-btn, [data-mode]"), null, "no figures toggle");
  assert.doesNotMatch(card.textContent ?? "", /tokens:/);
  assert.match(card.textContent ?? "", /today \$1\.00/);
  assert.match(card.textContent ?? "", /last hour \$0\.50/);
});

test("the expanded subagent list shows each subagent's token count", async () => {
  await loaded;
  push(snapshot([{ ...session("tk1", 5_000), activeSubagents: [subagent("t1", 1_000)], subagentCount: 1 }]));
  if (openOf("tk1") !== "true") clickButton(subsButton("tk1"));
  const li = w.document.querySelector<HTMLElement>(`.card[data-id="tk1"] ul.subs li`);
  assert.ok(li);
  assert.match(li.textContent ?? "", / \d+ tok$/, "token count shown in cost mode too");
});

// Legend swatch colour per model name, read from the rendered legend.
function legendColours(): Map<string, string> {
  const out = new Map<string, string>();
  for (const span of w.document.querySelectorAll<HTMLElement>("#legend > span")) {
    out.set(span.textContent?.trim() ?? "", span.querySelector("i")?.getAttribute("style") ?? "");
  }
  return out;
}

test("a model seen only today gets its own colour; existing colours stay; the legend lists every model", async () => {
  await loaded;
  const tok = (cost: number) => ({ cost, out: 1, in: 1, cacheWrite: 0, cacheRead: 0 });
  const lastHour = { ...snapshot([]), perMinute: [{ t: T0 - 60_000, byModel: { "lh-model": tok(0.5) } }] };
  const today = { ...withSub(session("cl1", 5_000)), byModel: [{ model: "today-only", cost: 1, tokens: tok(1) }] };
  push({ ...lastHour, sessions: [today] });
  const first = legendColours();
  const lhColour = first.get("lh-model");
  const todayColour = first.get("today-only");
  assert.ok(lhColour && todayColour, "both models are in the legend");
  assert.notEqual(todayColour, lhColour, "today-only model has its own colour");
  for (const [name, colour] of first) if (name !== "today-only") assert.notEqual(colour, todayColour, `${name} differs`);

  // The today-only model's bar segment uses the legend colour.
  const seg = w.document.querySelector<HTMLElement>(`.card[data-id="cl1"] .share-seg`);
  assert.equal(seg?.getAttribute("style")?.includes(todayColour.replace(/^background:/, "")), true, "bar matches legend");

  // Next snapshot without the today-only session: colours are unchanged, and the model stays listed.
  push({ ...lastHour, sessions: [] });
  const second = legendColours();
  assert.equal(second.get("lh-model"), lhColour, "last-hour colour unchanged");
  assert.equal(second.get("today-only"), todayColour, "today-only colour kept");
});
