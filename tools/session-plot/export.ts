// Usage: npm run export -- <sessionId> [outFile]
// Writes one self-contained HTML file (data and client code inlined, no network requests).
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { parseSession, SessionNotFoundError } from "./src/parse";
import { bundleClient } from "./src/bundle";

const here = dirname(fileURLToPath(import.meta.url));
const id = process.argv[2];
if (!id) {
  console.error("usage: npm run export -- <sessionId> [outFile]");
  process.exit(2);
}
const outFile = process.argv[3] ? resolve(process.argv[3]) : join(here, "out", `${id.slice(0, 8)}.html`);

// Escape characters that could end the inline script or break JS string literals.
const BS = String.fromCharCode(92);
const LS = String.fromCharCode(0x2028);
const PS = String.fromCharCode(0x2029);
let session;
try {
  session = parseSession(id);
} catch (err) {
  if (err instanceof SessionNotFoundError) {
    console.error(`session not found: ${id}`);
    process.exit(1);
  }
  throw err;
}
const dataJson = JSON.stringify(session)
  .split("<").join(BS + "u003c")
  .split(LS).join(BS + "u2028")
  .split(PS).join(BS + "u2029");

let js = await bundleClient(true);
js = js.split("</script").join("<" + BS + "/script").split("<!--").join("<" + BS + "!--");

const SCRIPT_TAG = '<script type="module" src="/main.js"></script>';
const html = readFileSync(join(here, "index.html"), "utf8");
if (!html.includes(SCRIPT_TAG)) throw new Error("index.html is missing the main.js script tag");
const inlined = html.replace(
  SCRIPT_TAG,
  () => `<script>window.__SESSION_DATA__=${dataJson};</script>\n<script type="module">${js}</script>`,
);

mkdirSync(dirname(outFile), { recursive: true });
writeFileSync(outFile, inlined, "utf8");
console.log(`wrote ${outFile} (${inlined.length} chars)`);
