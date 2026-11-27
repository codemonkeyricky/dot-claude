import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

// The chart's SVG classes. A CSS rule that targets one of them outside ".chart" would restyle
// the chart (for example ".seg" width collapses the bars).
const CHART_CLASSES = ["grid", "tick", "seg", "col", "sel"];
// Class names used by the session cards. None may be a chart class.
const CARD_CLASSES = ["card", "card-main", "card-meta", "card-ctx", "card-figs", "card-subs", "card-link", "ctitle", "dot", "ago"];

function liveCss(): string {
  const here = dirname(fileURLToPath(import.meta.url));
  return readFileSync(join(here, "..", "live.html"), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
}

// Selector text of every rule (the part before each "{").
function selectors(css: string): string[] {
  return [...css.matchAll(/([^{}]+)\{/g)].map((m) => m[1].trim());
}

// The bar rects carry class "seg" (src/livepage.ts). A CSS rule for ".seg" would restyle them.
test("live.html has no CSS selector .seg, which the chart bars use", () => {
  assert.doesNotMatch(liveCss(), /\.seg(?![\w-])/);
});

test("every CSS selector that names a chart class is scoped under .chart", () => {
  const offenders = selectors(liveCss()).filter((sel) => {
    const usesChartClass = CHART_CLASSES.some((c) => new RegExp(`\\.${c}(?![\\w-])`).test(sel));
    return usesChartClass && !sel.split(",").every((part) => part.trim().startsWith(".chart"));
  });
  assert.deepEqual(offenders, []);
});

test("the session card class names do not collide with chart SVG classes", () => {
  const clash = CARD_CLASSES.filter((c) => CHART_CLASSES.includes(c));
  assert.deepEqual(clash, []);
});

// The row has no Cost/Tokens figures toggle any more, and the page no longer reads or writes the
// old per-card mode key or its classes.
test("the live page has no figures toggle and no card-mode storage", () => {
  const here = dirname(fileURLToPath(import.meta.url));
  const src = readFileSync(join(here, "..", "src", "livepage.ts"), "utf8");
  const page = readFileSync(join(here, "..", "live.html"), "utf8");
  for (const text of [src, page]) {
    assert.doesNotMatch(text, /mode-toggle|mode-btn|session-plot\.cardMode|cardMode/);
  }
});
