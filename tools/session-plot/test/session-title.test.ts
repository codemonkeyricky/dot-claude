import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { sessionTitleFromFile } from "../src/parse";

function userLine(text: string): string {
  return JSON.stringify({ type: "user", message: { role: "user", content: text } });
}

function assistantLine(i: number): string {
  return JSON.stringify({
    type: "assistant",
    message: {
      id: `msg-${i}`,
      model: "claude-opus-5-5",
      usage: { input_tokens: 10, output_tokens: 5, cache_creation_input_tokens: 0, cache_read_input_tokens: 0 },
      content: [{ type: "text", text: `filler ${i} ${"x".repeat(200)}` }],
    },
  });
}

function customTitleLine(title: string): string {
  return JSON.stringify({ type: "custom-title", customTitle: title, sessionId: "s" });
}

function withFile(lines: string[], fn: (file: string) => void): void {
  const dir = mkdtempSync(join(tmpdir(), "session-title-"));
  try {
    const file = join(dir, "s.jsonl");
    writeFileSync(file, lines.join("\n") + "\n", "utf8");
    fn(file);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

test("a custom title near the end of a >200-line transcript is shown in the picker", () => {
  const lines = [userLine("first prompt")];
  for (let i = 0; i < 300; i++) lines.push(assistantLine(i));
  lines.push(customTitleLine("Renamed late"));
  withFile(lines, (file) => {
    assert.equal(sessionTitleFromFile(file), "Renamed late");
  });
});

test("with two custom-title records in the tail, the later one wins", () => {
  const lines = [userLine("first prompt")];
  for (let i = 0; i < 300; i++) lines.push(assistantLine(i));
  lines.push(customTitleLine("Older name"));
  for (let i = 300; i < 400; i++) lines.push(assistantLine(i));
  lines.push(customTitleLine("Newer name"));
  withFile(lines, (file) => {
    assert.equal(sessionTitleFromFile(file), "Newer name");
  });
});

test("a custom title in the head still applies when the tail has none", () => {
  const lines = [userLine("first prompt"), customTitleLine("Head name")];
  for (let i = 0; i < 400; i++) lines.push(assistantLine(i));
  withFile(lines, (file) => {
    assert.equal(sessionTitleFromFile(file), "Head name");
  });
});

test("a small file is read in full and the first prompt is used without a custom title", () => {
  withFile([userLine("hello there"), assistantLine(0)], (file) => {
    assert.equal(sessionTitleFromFile(file), "hello there");
  });
});
