import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

// The chart's bar rects carry class "seg" (src/livepage.ts). A CSS rule for ".seg" would restyle
// them (a width rule overrides the SVG width attribute and collapses the bars to zero).
test("live.html has no CSS selector .seg, which the chart bars use", () => {
  const here = dirname(fileURLToPath(import.meta.url));
  const css = readFileSync(join(here, "..", "live.html"), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
  assert.doesNotMatch(css, /\.seg(?![\w-])/);
});
