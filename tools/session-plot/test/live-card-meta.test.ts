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
    subagentCount: 0,
    activeSubagents: [],
  };
}

function snapshot(sessions: LiveSession[]): LiveSnapshot {
  return { now: Date.now(), windowMin: 60, totals: { costToday: 1, costLastHour: 0.5, outLastHour: 2 }, perMinute: [], sessions };
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

function modeButton(id: string, mode: string): HTMLButtonElement {
  const btn = w.document.querySelector<HTMLButtonElement>(`.card[data-id="${id}"] button[data-mode="${mode}"]`);
  if (!btn) throw new Error(`no ${mode} button for ${id}`);
  return btn;
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

function modeOf(id: string): string | undefined {
  return w.document.querySelector<HTMLElement>(`.card[data-id="${id}"] button[aria-pressed="true"]`)?.dataset.mode;
}

function otherMode(mode: string | undefined): string {
  return mode === "tokens" ? "cost" : "tokens";
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

test("every card keeps its last-activity text across toggles, kind changes and unchanged snapshots", async () => {
  await loaded;
  push(snapshot([session("aaa", 5_000), session("bbb", 40_000), session("ccc", 90_000)]));
  for (const c of metaTexts()) assert.match(c.ago, /^last activity /, `initial ${c.id}`);

  // Pointer toggle on the first card, keyboard toggle on the second.
  clickButton(modeButton("aaa", "tokens"));
  keyboardActivate(modeButton("bbb", "tokens"));
  for (const c of metaTexts()) assert.match(c.ago, /^last activity /, `after toggles ${c.id}`);

  // Changing the token kind rebuilds the Tokens cards. The text must be there before any tick.
  kindButton("output").dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
  for (const c of metaTexts()) assert.match(c.ago, /^last activity /, `after kind change ${c.id}`);

  // Same snapshot again: nothing is rebuilt, and the text is still there.
  push(snapshot([session("aaa", 5_000), session("bbb", 40_000), session("ccc", 90_000)]));
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

test("keyboard focus stays on a toggle button when its card is rebuilt", async () => {
  await loaded;
  const btn = modeButton("bbb", "tokens");
  btn.focus();
  assert.equal(w.document.activeElement, btn);
  // Kind change replaces the Tokens cards, including the focused one.
  kindButton("input").dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
  const focused = w.document.activeElement as HTMLElement | null;
  assert.equal(focused?.dataset.mode, "tokens");
  assert.equal(focused?.closest(".card")?.getAttribute("data-id"), "bbb");
  assert.equal(focused?.isConnected, true);
});

const SESSIONS = (costAaa = 1) => [session("aaa", 5_000, costAaa), session("bbb", 40_000), session("ccc", 90_000)];

test("a press cancelled by the browser (touch scroll) does not toggle", async () => {
  await loaded;
  push(snapshot(SESSIONS()));
  const before = modeOf("ccc");
  const btn = modeButton("ccc", otherMode(before));
  pointerPress(btn);
  releaseOn("pointercancel", w.document.body);
  await flush();
  assert.equal(modeOf("ccc"), before);
});

test("a snapshot during a press keeps the button; the click after release toggles once and shows the newest numbers", async () => {
  await loaded;
  push(snapshot(SESSIONS()));
  const before = modeOf("aaa");
  const btn = modeButton("aaa", otherMode(before));
  let clicks = 0;
  btn.addEventListener("click", () => clicks++);
  pointerPress(btn);
  push(snapshot(SESSIONS(123.45)));
  assert.equal(btn.isConnected, true, "the held card's button was not replaced");
  assert.equal(modeOf("aaa"), before, "the snapshot did not toggle the card");
  releaseOn("pointerup", btn);
  clickButton(btn);
  assert.equal(clicks, 1, "the click reached the same button node");
  assert.equal(modeOf("aaa"), otherMode(before), "toggled exactly once");
  await flush();
  assert.match(cardText("aaa"), /\$123\.45/, "the card shows the newest snapshot after release");
});

test("keyboard Enter (click with detail 0) toggles once and keeps focus on the new button", async () => {
  await loaded;
  push(snapshot(SESSIONS()));
  const before = modeOf("bbb");
  clickButton(modeButton("bbb", otherMode(before)), 0);
  assert.equal(modeOf("bbb"), otherMode(before));
  assert.equal(w.document.activeElement, modeButton("bbb", otherMode(before)));
});

test("a click with no preceding pointerdown (synthetic) toggles once", async () => {
  await loaded;
  push(snapshot(SESSIONS()));
  const before = modeOf("ccc");
  clickButton(modeButton("ccc", otherMode(before)), 1);
  assert.equal(modeOf("ccc"), otherMode(before));
});

test("a mouse press dragged off the button and released elsewhere does not toggle", async () => {
  await loaded;
  push(snapshot(SESSIONS()));
  const before = modeOf("bbb");
  const btn = modeButton("bbb", otherMode(before));
  pointerPress(btn);
  releaseOn("pointerup", w.document.getElementById("cards") as Element);
  // A click on the common ancestor (the cards container), as the browser dispatches it.
  clickButton(w.document.getElementById("cards") as Element);
  await flush();
  assert.equal(modeOf("bbb"), before);
});

test("the hold ends after 2 s without a release, so a held card catches up", async () => {
  await loaded;
  push(snapshot(SESSIONS()));
  const btn = modeButton("aaa", otherMode(modeOf("aaa")));
  pointerPress(btn);
  push(snapshot(SESSIONS(77.5)));
  assert.equal(btn.isConnected, true);
  await new Promise((r) => setTimeout(r, 2_100));
  await flush();
  assert.equal(btn.isConnected, false, "the safety release re-rendered the card");
  assert.match(cardText("aaa"), /\$77\.50/);
});
