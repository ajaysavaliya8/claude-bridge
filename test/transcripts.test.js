// Transcript search/read core — over a fake ~/.claude/projects tree (no real data).

import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { searchTranscripts, readSession } from "../src/transcripts.js";

function fakeRoot() {
  const root = mkdtempSync(join(tmpdir(), "cbtx-"));
  const proj = join(root, "-home-user-myapp");
  mkdirSync(proj, { recursive: true });
  const lines = [
    JSON.stringify({ type: "user", message: { role: "user", content: "How does login work?" } }),
    JSON.stringify({ type: "assistant", message: { role: "assistant", content: [{ type: "text", text: "Login uses JWT in auth.py" }] } }),
    JSON.stringify({ type: "user", message: { role: "user", content: "What about refresh tokens?" } }),
  ].join("\n");
  writeFileSync(join(proj, "11111111-2222-3333-4444-555555555555.jsonl"), lines);
  return root;
}

test("searchTranscripts finds a substring match with snippet + session", () => {
  const r = searchTranscripts({ query: "JWT", root: fakeRoot() });
  assert.equal(r.matches.length, 1);
  assert.match(r.matches[0].snippet, /JWT/);
  assert.equal(r.matches[0].session, "11111111-2222-3333-4444-555555555555");
  assert.equal(r.matches[0].role, "assistant");
});

test("searchTranscripts supports /regex/ and is case-insensitive", () => {
  const root = fakeRoot();
  assert.ok(searchTranscripts({ query: "/refresh tokens/", root }).matches.length >= 1);
  assert.ok(searchTranscripts({ query: "LOGIN", root }).matches.length >= 1);
});

test("searchTranscripts requires a query", () => {
  assert.ok(searchTranscripts({ query: "  ", root: fakeRoot() }).error);
});

test("searchTranscripts returns a note when there are no transcripts", () => {
  const r = searchTranscripts({ query: "x", root: join(tmpdir(), "definitely-not-here-xyz") });
  assert.ok(r.note);
  assert.deepEqual(r.matches, []);
});

test("readSession returns the last N messages of the latest session", () => {
  const r = readSession({ lastN: 2, root: fakeRoot() });
  assert.equal(r.messages.length, 2);
  assert.equal(r.messages.at(-1).text, "What about refresh tokens?");
});

test("readSession sinceLastUserPrompt slices from the last user turn", () => {
  const r = readSession({ sinceLastUserPrompt: true, root: fakeRoot() });
  assert.equal(r.messages[0].role, "user");
  assert.equal(r.messages[0].text, "What about refresh tokens?");
});
