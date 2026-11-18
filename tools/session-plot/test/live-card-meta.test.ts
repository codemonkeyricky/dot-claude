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

function session(id: string, ago: number): LiveSession {
  return {
    id,
    projectDir: `-home-example-${id}`,
    title: `session ${id}`,
    lastActivity: T0 - ago,
    mainModel: "model-a",
    mainCtx: 1000,
    mainPeakCtx: 1000,
    mainTurns: 2,
    costToday: 1,
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
  pointerPress(modeButton("aaa", "tokens"));
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
