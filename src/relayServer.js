// RELAY (in-chat answering): queues each incoming question and holds the asker's
// HTTP request open until this peer's interactive Claude answers it (via the
// incoming_questions / answer_incoming MCP tools) — so the Q&A is visible in this
// peer's own chat. One-way notes (/tell) are queued too, flagged as notes. Run one
// per peer; run on both sides for two-way in-chat answering.

import http from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { readBody, send, tokenOk, onListenError, VERSION } from "./http.js";
import { saveImages, cleanupDir, MAX_PAYLOAD_BYTES } from "./images.js";

const MAX_PENDING = 100;     // total queued items cap (DoS guard)
const MAX_PER_SENDER = 20;   // per-sender cap

export function startRelayServer({ port, name, holdSeconds = 1800, token = null }) {
  // id -> { id, kind:'question'|'note', sender, question, imagePaths, imageDir, ts, res, timer }
  const pending = new Map();
  let seq = 0;
  const countFrom = (sender) => [...pending.values()].filter((e) => e.sender === sender).length;

  function drop(id) {
    const e = pending.get(id);
    if (!e) return null;
    clearTimeout(e.timer);
    cleanupDir(e.imageDir);
    pending.delete(id);
    return e;
  }

  async function handle(req, res) {
    const url = (req.url || "/").split("?")[0];

    if (req.method === "GET" && url === "/health") {
      return send(res, 200, { status: "ok", name, mode: "relay", answer: true, version: VERSION, pending: pending.size });
    }
    if (!tokenOk(req, token)) return send(res, 401, { error: "unauthorized (bad or missing token)", is_error: true });

    // From the partner: a question to enqueue + HOLD open until answered.
    if (req.method === "POST" && url === "/ask") {
      let body;
      try { body = JSON.parse(await readBody(req, MAX_PAYLOAD_BYTES)); }
      catch { return send(res, 400, { answer: "bad request: invalid JSON", is_error: true }); }
      const question = String(body.question || "").trim();
      const sender = String(body.sender || "peer") || "peer";
      if (!question) return send(res, 400, { answer: "bad request: empty question", is_error: true });
      if (pending.size >= MAX_PENDING) return send(res, 429, { answer: `'${name}' is overloaded (${pending.size} pending)`, is_error: true });
      if (countFrom(sender) >= MAX_PER_SENDER) return send(res, 429, { answer: `too many pending questions from '${sender}'`, is_error: true });

      const id = `q${++seq}`;
      const imageDir = join(tmpdir(), "claude-bridge", name, id);
      const imagePaths = saveImages(body.images, imageDir);
      const timer = setTimeout(() => {
        if (drop(id)) send(res, 200, { answer: `(no answer from '${name}' within ${holdSeconds}s)`, is_error: true, meta: { id, timed_out: true } });
      }, holdSeconds * 1000);
      // If the asker disconnects (gave up), drop the question so we never write to a dead socket.
      res.on("close", () => { if (pending.has(id)) { drop(id); console.error(`[relay] ${id} asker disconnected — dropped`); } });
      pending.set(id, { id, kind: "question", sender, question, imagePaths, imageDir: imagePaths.length ? imageDir : null, ts: Date.now(), res, timer });
      console.error(`[relay] queued ${id} from '${sender}' (${question.length} chars${imagePaths.length ? `, ${imagePaths.length} image(s)` : ""}) — ${pending.size} pending`);
      return; // held open
    }

    // One-way note: queue it (flagged) so it surfaces in incoming_questions; ack honestly.
    if (req.method === "POST" && url === "/tell") {
      let body;
      try { body = JSON.parse(await readBody(req, MAX_PAYLOAD_BYTES)); }
      catch { return send(res, 400, { ok: false }); }
      const sender = String(body.sender || "peer") || "peer";
      const message = String(body.message || "").trim();
      if (pending.size >= MAX_PENDING) return send(res, 429, { ok: false, error: "overloaded" });
      const id = `n${++seq}`;
      const imageDir = join(tmpdir(), "claude-bridge", name, id);
      const imagePaths = saveImages(body.images, imageDir);
      const timer = setTimeout(() => drop(id), holdSeconds * 1000);
      pending.set(id, { id, kind: "note", sender, question: message, imagePaths, imageDir: imagePaths.length ? imageDir : null, ts: Date.now(), res: null, timer });
      console.error(`[relay] note ${id} from '${sender}': ${message.slice(0, 120)}`);
      return send(res, 202, { ok: true, queued: true, id, note: "queued for the peer's session (it surfaces via incoming_questions); fire-and-forget, no read receipt" });
    }

    // For this peer's interactive Claude (local MCP):
    if (req.method === "GET" && url === "/pending") {
      const questions = [...pending.values()].map((e) => ({ id: e.id, kind: e.kind, sender: e.sender, question: e.question, images: e.imagePaths || [], ts: e.ts }));
      return send(res, 200, { questions });
    }

    if (req.method === "POST" && url === "/answer") {
      let body;
      try { body = JSON.parse(await readBody(req)); }
      catch { return send(res, 400, { ok: false, error: "invalid JSON" }); }
      const id = String(body.id || "");
      const entry = pending.get(id);
      if (!entry) return send(res, 404, { ok: false, error: `no pending item '${id}' — already handled, expired, or the asker gave up` });
      const { kind, res: askerRes } = entry;
      drop(id);
      if (kind === "note") return send(res, 200, { ok: true, kind: "note", note: "note acknowledged (no reply is sent for notes)" });
      const answer = String(body.answer || "").trim() || "(empty answer)";
      if (askerRes && !askerRes.writableEnded && !askerRes.destroyed) {
        send(askerRes, 200, { answer, is_error: !!body.is_error, meta: { answered_by: "interactive", id } });
        console.error(`[relay] answered ${id}`);
        return send(res, 200, { ok: true, delivered: true, id });
      }
      console.error(`[relay] ${id} answered but asker had already gone`);
      return send(res, 200, { ok: true, delivered: false, id, note: "the asker already gave up; answer not delivered" });
    }

    send(res, 404, { error: "not found" });
  }

  const server = http.createServer((req, res) => {
    handle(req, res).catch((e) => {
      console.error("[relay] handler error:", e?.message || e);
      if (!res.headersSent) { try { send(res, 500, { error: "internal error", is_error: true }); } catch { /* socket gone */ } }
    });
  });
  server.requestTimeout = 0;
  server.headersTimeout = 0;
  onListenError(server, port);
  server.listen(port, "127.0.0.1", () => {
    console.error(`[claude-bridge ${VERSION}] relay '${name}' on 127.0.0.1:${port}${token ? " (auth on)" : ""} — incoming questions/notes answered in this peer's chat`);
  });
  return server;
}
