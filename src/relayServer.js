// RELAY (in-chat answering): instead of answering headlessly, this daemon QUEUES
// each incoming question and holds the asker's HTTP request open until this peer's
// interactive Claude answers it (via the incoming_questions / answer_incoming MCP
// tools). That makes the Q&A visible in this peer's own chat. One per peer — run
// it on both sides for fully two-way in-chat answering. Attached images are saved
// to disk so the answering Claude can Read them, then cleaned up after answering.

import http from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { readBody, send } from "./http.js";
import { saveImages, cleanupDir, MAX_PAYLOAD_BYTES } from "./images.js";

export function startRelayServer({ port, name, holdSeconds = 1800 }) {
  // id -> { id, sender, question, imagePaths, imageDir, ts, res, timer }.
  const pending = new Map();
  let seq = 0;

  async function handle(req, res) {
    const url = (req.url || "/").split("?")[0];

    if (req.method === "GET" && url === "/health") {
      return send(res, 200, { status: "ok", name, mode: "relay", pending: pending.size });
    }

    // From the partner's ask client: enqueue and HOLD open until answered.
    if (req.method === "POST" && url === "/ask") {
      let body;
      try { body = JSON.parse(await readBody(req, MAX_PAYLOAD_BYTES)); }
      catch { return send(res, 400, { answer: "bad request: invalid JSON", is_error: true }); }
      const question = String(body.question || "").trim();
      const sender = String(body.sender || "peer") || "peer";
      if (!question) return send(res, 400, { answer: "bad request: empty question", is_error: true });

      const id = `q${++seq}`;
      const imageDir = join(tmpdir(), "claude-bridge", name, id);
      const imagePaths = saveImages(body.images, imageDir);
      const timer = setTimeout(() => {
        if (pending.delete(id)) {
          cleanupDir(imagePaths.length ? imageDir : null);
          send(res, 200, { answer: `(no answer from '${name}' within ${holdSeconds}s)`, is_error: true, meta: { id, timed_out: true } });
        }
      }, holdSeconds * 1000);
      pending.set(id, { id, sender, question, imagePaths, imageDir: imagePaths.length ? imageDir : null, ts: Date.now(), res, timer });
      const imgNote = imagePaths.length ? `, ${imagePaths.length} image(s)` : "";
      console.error(`[relay] queued ${id} from '${sender}' (${question.length} chars${imgNote}) — ${pending.size} pending`);
      return; // held open; resolved by /answer or the timer
    }

    // One-way note (fire-and-forget) — logged for visibility, not queued for reply.
    if (req.method === "POST" && url === "/tell") {
      let body;
      try { body = JSON.parse(await readBody(req, MAX_PAYLOAD_BYTES)); }
      catch { return send(res, 400, { ok: false }); }
      const sender = String(body.sender || "peer") || "peer";
      const message = String(body.message || "").trim();
      const imgs = saveImages(body.images, join(tmpdir(), "claude-bridge", name, `note${++seq}`));
      console.error(`[relay] note from '${sender}': ${message.slice(0, 200)}${imgs.length ? ` (+${imgs.length} image(s): ${imgs.join(", ")})` : ""}`);
      return send(res, 202, { ok: true });
    }

    // For this peer's interactive Claude (via MCP tools):
    if (req.method === "GET" && url === "/pending") {
      const questions = [...pending.values()].map((e) => ({
        id: e.id, sender: e.sender, question: e.question, images: e.imagePaths || [], ts: e.ts,
      }));
      return send(res, 200, { questions });
    }

    if (req.method === "POST" && url === "/answer") {
      let body;
      try { body = JSON.parse(await readBody(req)); }
      catch { return send(res, 400, { ok: false, error: "invalid JSON" }); }
      const id = String(body.id || "");
      const entry = pending.get(id);
      if (!entry) return send(res, 404, { ok: false, error: `no pending question with id '${id}' (already answered or expired)` });
      pending.delete(id);
      clearTimeout(entry.timer);
      cleanupDir(entry.imageDir);
      const answer = String(body.answer || "").trim() || "(empty answer)";
      send(entry.res, 200, { answer, is_error: !!body.is_error, meta: { answered_by: "interactive", id } });
      console.error(`[relay] answered ${id}`);
      return send(res, 200, { ok: true, id });
    }

    send(res, 404, { error: "not found" });
  }

  const server = http.createServer((req, res) => {
    handle(req, res).catch((e) => {
      console.error("[relay] handler error:", e?.message || e);
      if (!res.headersSent) { try { send(res, 500, { error: "internal error", is_error: true }); } catch { /* socket gone */ } }
    });
  });

  server.requestTimeout = 0;   // questions wait for a human; never cut the held /ask
  server.headersTimeout = 0;
  server.listen(port, "127.0.0.1", () => {
    console.error(`[claude-bridge] relay '${name}' on 127.0.0.1:${port} — incoming questions are answered in this peer's chat`);
  });
  return server;
}
