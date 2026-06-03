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

test("health reports relay mode", async () => {
  const { server, base } = await start();
  try {
    const h = await (await fetch(`${base}/health`)).json();
    assert.equal(h.status, "ok");
    assert.equal(h.mode, "relay");
  } finally {
    server.close();
  }
});
