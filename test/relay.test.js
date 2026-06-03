// Relay (in-chat answering inbox) tests: a question is queued and the asker's
// request is held open until /answer resolves it. No claude needed.

import { test } from "node:test";
import assert from "node:assert/strict";

import { startRelayServer } from "../src/relayServer.js";

async function start() {
  const server = startRelayServer({ port: 0, name: "B", holdSeconds: 10 });
  if (!server.listening) await new Promise((r) => server.once("listening", r));
  return { server, base: `http://127.0.0.1:${server.address().port}` };
}

test("queues a question, holds /ask, resolves it on /answer", async () => {
  const { server, base } = await start();
  try {
    // Fire /ask WITHOUT awaiting — it blocks until the question is answered.
    const askP = fetch(`${base}/ask`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ sender: "A", question: "What port?" }),
    }).then((r) => r.json());

    // The question should appear in /pending.
    let pend;
    for (let i = 0; i < 100; i++) {
      pend = await (await fetch(`${base}/pending`)).json();
      if (pend.questions.length) break;
      await new Promise((r) => setTimeout(r, 10));
    }
    assert.equal(pend.questions.length, 1);
    const q = pend.questions[0];
    assert.equal(q.sender, "A");
    assert.equal(q.question, "What port?");

    // Answer it → the held /ask resolves with that answer.
    const ack = await (await fetch(`${base}/answer`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ id: q.id, answer: "9001" }),
    })).json();
    assert.equal(ack.ok, true);

    const got = await askP;
    assert.equal(got.is_error, false);
    assert.equal(got.answer, "9001");

    const after = await (await fetch(`${base}/pending`)).json();
    assert.equal(after.questions.length, 0);
  } finally {
    server.close();
  }
});

test("saves an attached image and surfaces its path in /pending", async () => {
  const { existsSync } = await import("node:fs");
  const { server, base } = await start();
  try {
    const PNG_1x1 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==";
    fetch(`${base}/ask`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ sender: "A", question: "see this?", images: [{ name: "s.png", media_type: "image/png", data: PNG_1x1 }] }),
    }).catch(() => {}); // held open; we don't await it here

    let q;
    for (let i = 0; i < 100; i++) {
      const pend = await (await fetch(`${base}/pending`)).json();
      if (pend.questions.length) { q = pend.questions[0]; break; }
      await new Promise((r) => setTimeout(r, 10));
    }
    assert.equal(q.images.length, 1);
    assert.ok(existsSync(q.images[0]), "saved image file should exist on disk");
  } finally {
    server.close();
  }
});

test("/tell queues a note, flagged kind:note in /pending, with an honest ack", async () => {
  const { server, base } = await start();
  try {
    const ack = await (await fetch(`${base}/tell`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ sender: "A", message: "fyi: renamed a field" }),
    })).json();
    assert.equal(ack.ok, true);
    assert.equal(ack.queued, true);
    const pend = await (await fetch(`${base}/pending`)).json();
    assert.equal(pend.questions.length, 1);
    assert.equal(pend.questions[0].kind, "note");
    assert.equal(pend.questions[0].question, "fyi: renamed a field");
  } finally {
    server.close();
  }
});

test("enforces the shared-secret token when one is set (health stays open)", async () => {
  const { startRelayServer } = await import("../src/relayServer.js");
  const server = startRelayServer({ port: 0, name: "B", holdSeconds: 5, token: "s3cret" });
  if (!server.listening) await new Promise((r) => server.once("listening", r));
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    const noTok = await fetch(`${base}/tell`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ sender: "A", message: "x" }) });
    assert.equal(noTok.status, 401);
    const withTok = await fetch(`${base}/tell`, { method: "POST", headers: { "content-type": "application/json", authorization: "Bearer s3cret" }, body: JSON.stringify({ sender: "A", message: "x" }) });
    assert.equal(withTok.status, 202);
    assert.equal((await fetch(`${base}/health`)).status, 200); // health is unauthenticated
  } finally {
    server.close();
  }
});

test("rejects /answer for an unknown id", async () => {
  const { server, base } = await start();
  try {
    const r = await fetch(`${base}/answer`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ id: "nope", answer: "x" }),
    });
    assert.equal(r.status, 404);
  } finally {
    server.close();
  }
});

test("health reports relay mode + version", async () => {
  const { server, base } = await start();
  try {
    const h = await (await fetch(`${base}/health`)).json();
    assert.equal(h.status, "ok");
    assert.equal(h.mode, "relay");
    assert.equal(h.answer, true);
    assert.ok(h.version);
  } finally {
    server.close();
  }
});

const post = (base, path, body) =>
  fetch(`${base}${path}`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
const pendCount = async (base) => (await (await fetch(`${base}/pending`)).json()).questions.length;
async function until(fn, ms = 2000) { const end = Date.now() + ms; while (Date.now() < end) { if (await fn()) return true; await new Promise((r) => setTimeout(r, 10)); } return false; }

test("hold expiry returns a timed_out error and clears the question (T3)", async () => {
  const { startRelayServer } = await import("../src/relayServer.js");
  const server = startRelayServer({ port: 0, name: "B", holdSeconds: 0.05 });
  if (!server.listening) await new Promise((r) => server.once("listening", r));
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    const got = await (await post(base, "/ask", { sender: "A", question: "q" })).json();
    assert.equal(got.is_error, true);
    assert.equal(got.meta.timed_out, true);
    assert.equal(await pendCount(base), 0);
  } finally {
    server.close();
  }
});

test("asker disconnect drops the pending question (T2)", async () => {
  const { server, base } = await start(); // holdSeconds 10
  try {
    const ctrl = new AbortController();
    fetch(`${base}/ask`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ sender: "A", question: "q" }), signal: ctrl.signal }).catch(() => {});
    assert.ok(await until(async () => (await pendCount(base)) === 1), "question should queue");
    ctrl.abort();
    assert.ok(await until(async () => (await pendCount(base)) === 0), "question should be dropped on disconnect");
  } finally {
    server.close();
  }
});

test("enforces the per-sender cap with 429 (T5)", async () => {
  const { server, base } = await start(); // holdSeconds 10 — requests stay held
  try {
    for (let i = 0; i < 20; i++) post(base, "/ask", { sender: "A", question: `q${i}` }).catch(() => {}); // MAX_PER_SENDER
    assert.ok(await until(async () => (await pendCount(base)) >= 20), "20 should queue");
    const over = await post(base, "/ask", { sender: "A", question: "too-many" });
    assert.equal(over.status, 429);
  } finally {
    server.close();
  }
});

test("/tell rejects an empty note (R6)", async () => {
  const { server, base } = await start();
  try {
    assert.equal((await post(base, "/tell", { sender: "A", message: "   " })).status, 400);
  } finally {
    server.close();
  }
});
