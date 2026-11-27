import { test } from "node:test";
import assert from "node:assert/strict";
import { assembleSession, type SubagentInput } from "../src/parse";

const SID = "aaaaaaaa-0000-0000-0000-000000000002";
const T0 = Date.parse("2026-11-17T10:00:00.000Z");
const iso = (ms: number): string => new Date(ms).toISOString();

function assistant(msgId: string, ts: number | null, extra: Record<string, unknown> = {}): string {
  const o: Record<string, unknown> = {
    type: "assistant",
    message: {
      id: msgId,
      model: "claude-opus-5-5",
      usage: { input_tokens: 1000, output_tokens: 100, cache_creation_input_tokens: 0, cache_read_input_tokens: 0 },
      ...extra,
    },
  };
  if (ts !== null) o.timestamp = iso(ts);
  return JSON.stringify(o);
}

function user(text: string, ts: number | null): string {
  const o: Record<string, unknown> = { type: "user", message: { role: "user", content: text } };
  if (ts !== null) o.timestamp = iso(ts);
  return JSON.stringify(o);
}

// Main session: first line at T0, a tool_use at T0+60 s, last line at T0+120 s.
const mainLines = [
  user("do the thing", T0),
  assistant("m1", T0 + 60_000, {
    content: [{ type: "tool_use", id: "tu-spawn", name: "Agent", input: {} }],
  }),
  assistant("m2", T0 + 120_000),
];

function sub(agentId: string, lines: string[], meta: Record<string, unknown> | null = null): SubagentInput {
  return { agentId, lines, meta, forked: null, forkedExists: false };
}

test("subagent with no timestamps and no matching toolUseId never starts at 0 or before the parent", () => {
  const untimed = [user("hello", null), assistant("s1", null)];
  const { data } = assembleSession(SID, mainLines, [sub("untimed", untimed, { agentType: "other-agent" })]);
  assert.equal(data.subagents.length, 1);
  const s = data.subagents[0];
  assert.notEqual(s.start, 0);
  assert.ok(s.start > 0);
  assert.ok(s.start >= data.main.start, "start must not precede the parent's first timestamp");
  assert.equal(s.start, data.main.end, "falls back to the parent's last timestamp");
  assert.ok(s.end >= s.start);
  assert.equal(s.spawn, undefined);
});

test("subagent with a matching toolUseId spawns at the tool_use timestamp", () => {
  const timed = [user("hello", T0 + 70_000), assistant("s1", T0 + 75_000)];
  const { data, stats } = assembleSession(SID, mainLines, [
    sub("timed", timed, { agentType: "qorvax-tel", toolUseId: "tu-spawn" }),
  ]);
  const s = data.subagents[0];
  assert.equal(s.spawn, T0 + 60_000);
  assert.equal(s.start, T0 + 60_000);
  assert.equal(stats.spawnFromToolUse, 1);
  assert.equal(s.kind, "executor");
});

test("subagent with no time anywhere is left off the time axis, and the axis stays finite", () => {
  const untimedMain = [user("no clock", null), assistant("m1", null)];
  const { data } = assembleSession(SID, untimedMain, [sub("ghost", [user("x", null)])]);
  assert.equal(data.subagents.length, 0);
  for (const s of [data.main, ...data.subagents]) assert.ok(s.start >= 0);
});

test("a later custom-title record replaces the earlier one", () => {
  const lines = [
    user("prompt", T0),
    JSON.stringify({ type: "custom-title", customTitle: "First name", sessionId: SID }),
    assistant("m1", T0 + 1000),
    JSON.stringify({ type: "custom-title", customTitle: "Second name", sessionId: SID }),
  ];
  const { data } = assembleSession(SID, lines, []);
  assert.equal(data.title, "Second name");
});

test("without a custom title the title falls back to the first user prompt", () => {
  const { data } = assembleSession(SID, [user("Fix the build", T0), assistant("m1", T0 + 1000)], []);
  assert.equal(data.title, "Fix the build");
});
