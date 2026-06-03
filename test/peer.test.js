// Tests for the answering engine and prompts. No real `claude` needed — a tiny
// fake `claude` script (branches on FAKE_MODE / --resume) drives the REAL engine
// through its actual spawn + JSON-parse + session paths. Run: npm test

import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync, chmodSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { AnswerEngine } from "../src/answerEngine.js";
import { buildQuestionPrompt, buildNotePrompt } from "../src/prompts.js";

// A stand-in for the claude CLI: a real executable (node shebang) the engine
// spawns exactly like the real one. It prints canned JSON based on FAKE_MODE and,
// in "heal" mode, exits non-zero when asked to --resume the stale id.
function fakeClaude() {
  const dir = mkdtempSync(join(tmpdir(), "cbnode-"));
  const path = join(dir, "fake-claude.mjs");
  writeFileSync(
    path,
    `#!/usr/bin/env node
const a = process.argv.slice(2);
const ri = a.indexOf("--resume");
const resume = ri >= 0 ? a[ri + 1] : null;
const mode = process.env.FAKE_MODE;
process.stdin.resume(); process.stdin.on("data", () => {}); // drain the prompt
process.stdin.on("end", () => {
  if (mode === "err") { process.stdout.write(JSON.stringify({ is_error: true, subtype: "error_max_turns", result: "x" })); process.exit(0); }
  if (mode === "heal" && resume === "stale") { process.stderr.write("No conversation found"); process.exit(1); }
  const session_id = resume === "stale" ? "stale" : (mode === "heal" ? "fresh" : "s1");
  process.stdout.write(JSON.stringify({ result: "  hi  ", session_id, total_cost_usd: 0.1, num_turns: 2 }));
  process.exit(0);
});
`,
    "utf8",
  );
  chmodSync(path, 0o755);
  return { dir, path };
}

function makeEngine(claudePath, dir) {
  return new AnswerEngine({
    projectDir: dir,
    claudeBin: claudePath,
    allowedTools: "Read,Grep,Glob",
    maxTurns: 5,
    timeoutSec: 10,
    sessionFile: join(dir, "peer.session"),
  });
}

test("prompt builders embed sender + payload in tags", () => {
  const q = buildQuestionPrompt("frontend", "what port?");
  assert.match(q, /sender="frontend"/);
  assert.match(q, /what port\?/);
  assert.match(buildNotePrompt("frontend", "renamed field"), /peer_note/);
});

test("answer: success parses result, strips, reports cost, persists session", async () => {
  const { path, dir } = fakeClaude();
  process.env.FAKE_MODE = "ok";
  const eng = makeEngine(path, dir);
  const res = await eng.answer("beta", "q");
  assert.equal(res.is_error, false);
  assert.equal(res.answer, "hi");
  assert.equal(res.cost_usd, 0.1);
  assert.equal(readFileSync(eng.sessionFile, "utf8").trim(), "s1");
  delete process.env.FAKE_MODE;
});

test("answer: is_error result is flagged, not thrown", async () => {
  const { path, dir } = fakeClaude();
  process.env.FAKE_MODE = "err";
  const eng = makeEngine(path, dir);
  const res = await eng.answer("beta", "q");
  assert.equal(res.is_error, true);
  delete process.env.FAKE_MODE;
});

test("session self-heals on resumed non-zero exit", async () => {
  const { path, dir } = fakeClaude();
  process.env.FAKE_MODE = "heal";
  const eng = makeEngine(path, dir);
  eng._saveSession("stale");
  const res = await eng.answer("beta", "q");
  assert.equal(res.is_error, false);
  assert.equal(readFileSync(eng.sessionFile, "utf8").trim(), "fresh");
  delete process.env.FAKE_MODE;
});
