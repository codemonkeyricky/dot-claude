// Sanity check against one session (given as the first argument). Prints the numbers used for acceptance.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { listSessions, parseLine, parseSessionDetailed, PROJECTS_DIR } from "../src/parse";
import { cacheSplit, priceFor, turnCost } from "../src/cost";
import pricing from "../pricing.json" with { type: "json" };

const id = process.argv[2];
if (!id) {
  console.error("usage: npm run check -- <sessionId>");
  process.exit(1);
}
const { data, stats } = parseSessionDetailed(id);
const all = [data.main, ...data.subagents];
const first = Math.min(...all.map((s) => s.start));
const last = Math.max(...all.map((s) => s.end));
const r = (n: number): number => Math.round(n * 1e4) / 1e4;

const subCost = data.subagents.reduce((acc, s) => acc + s.costUsd, 0);
console.log(
  JSON.stringify(
    {
      mainTurns: data.main.turns,
      mainPeakCtx: data.main.peakCtx,
      subagents: data.subagents.length,
      spawnFromToolUse: stats.spawnFromToolUse,
      spawnFallback: stats.spawnFallback,
      kinds: stats.kinds,
      compactions: data.compactions.length,
      firstTs: new Date(first).toISOString(),
      lastTs: new Date(last).toISOString(),
      totalCostUsd: r(data.totalCostUsd),
      mainCostUsd: r(data.main.costUsd),
      subagentCostUsd: r(subCost),
      costByModel: Object.fromEntries(
        Object.entries(stats.costByModel)
          .sort((a, b) => b[1] - a[1])
          .map(([m, c]) => [m, r(c)]),
      ),
      unpricedModels: data.unpricedModels,
    },
    null,
    2,
  ),
);

// Hand-verify one turn: the last priced assistant line in the main transcript.
const session = listSessions().find((s) => s.id === id);
if (!session) throw new Error("test session not listed");
const file = join(PROJECTS_DIR, session.projectDir, `${id}.jsonl`);
let pick: { model: string; usage: Record<string, any>; msgId: string } | null = null;
for (const line of readFileSync(file, "utf8").split("\n")) {
  const o = parseLine(line);
  if (!o || o.type !== "assistant" || !o.message?.usage) continue;
  const model = String(o.message.model ?? "");
  if (model.startsWith("<") || !priceFor(model)) continue;
  pick = { model, usage: o.message.usage, msgId: String(o.message.id ?? "") };
}
if (!pick) throw new Error("no priced assistant turn found");

const u = pick.usage;
const key = pricing.models[pick.model as keyof typeof pricing.models]
  ? pick.model
  : pick.model.replace(/-\d{8}$/, "");
const p = (pricing.models as Record<string, any>)[key];
const input = u.input_tokens ?? 0;
const cc = u.cache_creation_input_tokens ?? 0;
const cr = u.cache_read_input_tokens ?? 0;
const out = u.output_tokens ?? 0;
const { w5, w1 } = cacheSplit(u);
let rIn = p.in;
let rOut = p.out;
let rCr = p.cacheRead;
if (u.speed === "fast" && p.fast) {
  rIn = p.fast.in;
  rOut = p.fast.out;
  rCr = p.fast.in * (p.cacheRead / p.in); // cache read keeps the standard cache-read/input ratio
}
if (p.longPrompt && input + cc + cr > p.longPrompt.over) {
  rIn = p.longPrompt.in;
  rOut = p.longPrompt.out;
  rCr = p.longPrompt.cacheRead;
}
const terms = {
  input: (input * rIn) / 1e6,
  write5m: (w5 * rIn * pricing.cacheWrite5mMult) / 1e6,
  write1h: (w1 * rIn * pricing.cacheWrite1hMult) / 1e6,
  cacheRead: (cr * rCr) / 1e6,
  output: (out * rOut) / 1e6,
};
const handTotal = terms.input + terms.write5m + terms.write1h + terms.cacheRead + terms.output;
const computed = turnCost(pick.model, u).usd;

console.log("--- hand-verified turn ---");
console.log(`message id: ${pick.msgId}`);
console.log(`model: ${pick.model} (rates key ${key})`);
console.log(`usage: ${JSON.stringify(u)}`);
console.log(
  `rates per M: in ${rIn}, out ${rOut}, cacheRead ${rCr}; write5m x${pricing.cacheWrite5mMult}, write1h x${pricing.cacheWrite1hMult}`,
);
console.log(
  `arithmetic: ${input}*${rIn}/1e6 = ${terms.input.toExponential(4)}; ` +
    `${w5}*${rIn}*${pricing.cacheWrite5mMult}/1e6 = ${terms.write5m.toExponential(4)}; ` +
    `${w1}*${rIn}*${pricing.cacheWrite1hMult}/1e6 = ${terms.write1h.toExponential(4)}; ` +
    `${cr}*${rCr}/1e6 = ${terms.cacheRead.toExponential(4)}; ` +
    `${out}*${rOut}/1e6 = ${terms.output.toExponential(4)}`,
);
console.log(`hand total: $${handTotal.toExponential(6)}; turnCost: $${computed.toExponential(6)}; match: ${Math.abs(handTotal - computed) < 1e-12}`);
