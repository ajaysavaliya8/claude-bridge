// Answer-daemon HTTP layer (T1): /health, /ask, empty-question 400, /tell, auth.
// Uses a fake engine — no real claude.

import { test } from "node:test";
import assert from "node:assert/strict";

import { startAnswerServer } from "../src/answerServer.js";

class FakeEngine {
  constructor() { this.projectDir = "/x"; this.timeoutSec = 5; this.notes = []; }
  async answer(sender, q) { return { answer: `A:${q}`, is_error: false, cost_usd: 0, meta: {} }; }
  async note(s, m) { this.notes.push(m); }
}

async function start(token) {
  const engine = new FakeEngine();
  const server = startAnswerServer({ engine, port: 0, name: "B", token });
  if (!server.listening) await new Promise((r) => server.once("listening", r));
  return { engine, server, base: `http://127.0.0.1:${server.address().port}` };
}
const post = (base, path, body, headers = {}) =>
  fetch(`${base}${path}`, { method: "POST", headers: { "content-type": "application/json", ...headers }, body: JSON.stringify(body) });

test("answer daemon: /health, /ask, empty-question 400, /tell", async () => {
  const { engine, server, base } = await start(null);
  try {
    const h = await (await fetch(`${base}/health`)).json();
    assert.equal(h.mode, "answer");
    assert.equal(h.answer, true);
    assert.ok(h.version);

    const ok = await (await post(base, "/ask", { sender: "A", question: "hi" })).json();
    assert.equal(ok.answer, "A:hi");
    assert.equal(ok.is_error, false);

    assert.equal((await post(base, "/ask", { sender: "A", question: "   " })).status, 400);
    assert.equal((await fetch(`${base}/ask`, { method: "POST", headers: { "content-type": "application/json" }, body: "not json" })).status, 400);

    assert.equal((await post(base, "/tell", { sender: "A", message: "note!" })).status, 202);
    await new Promise((r) => setTimeout(r, 30));
    assert.deepEqual(engine.notes, ["note!"]);
  } finally {
    server.close();
  }
});

test("answer daemon: enforces the token (health stays open)", async () => {
  const { server, base } = await start("sec");
  try {
    assert.equal((await post(base, "/ask", { sender: "A", question: "x" })).status, 401);
    const ok = await (await post(base, "/ask", { sender: "A", question: "x" }, { authorization: "Bearer sec" })).json();
    assert.equal(ok.answer, "A:x");
    assert.equal((await fetch(`${base}/health`)).status, 200);
  } finally {
    server.close();
  }
});
