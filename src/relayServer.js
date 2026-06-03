// RELAY (in-chat answering): instead of answering headlessly, this daemon QUEUES
// each incoming question and holds the asker's HTTP request open until this peer's
// interactive Claude answers it (via the incoming_questions / answer_incoming MCP
// tools). That makes the Q&A visible in this peer's own chat. One per peer — run
// it on both sides for fully two-way in-chat answering.

import http from "node:http";

import { readBody, send } from "./http.js";

export function startRelayServer({ port, name, holdSeconds = 1800 }) {
  // id -> { id, sender, question, ts, res, timer }. `res` is the asker's held
  // response, completed when /answer arrives (or timed out).
  const pending = new Map();
  let seq = 0;

  const server = http.createServer(async (req, res) => {
    const url = (req.url || "/").split("?")[0];

    if (req.method === "GET" && url === "/health") {
      return send(res, 200, { status: "ok", name, mode: "relay", pending: pending.size });
    }

    // From the partner's ask client: enqueue and HOLD open until answered.
    if (req.method === "POST" && url === "/ask") {
      let body;
      try { body = JSON.parse(await readBody(req)); }
      catch { return send(res, 400, { answer: "bad request: invalid JSON", is_error: true }); }
      const question = String(body.question || "").trim();
      const sender = String(body.sender || "peer") || "peer";
      if (!question) return send(res, 400, { answer: "bad request: empty question", is_error: true });

      const id = `q${++seq}`;
      const timer = setTimeout(() => {
        if (pending.delete(id)) {
          send(res, 200, { answer: `(no answer from '${name}' within ${holdSeconds}s)`, is_error: true, meta: { id, timed_out: true } });
        }
      }, holdSeconds * 1000);
      pending.set(id, { id, sender, question, ts: Date.now(), res, timer });
      console.error(`[relay] queued ${id} from '${sender}' (${question.length} chars) — ${pending.size} pending`);
      return; // held open; resolved by /answer or the timer
    }

    // For this peer's interactive Claude (via MCP tools):
    if (req.method === "GET" && url === "/pending") {
      const questions = [...pending.values()].map((e) => ({ id: e.id, sender: e.sender, question: e.question, ts: e.ts }));
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
      const answer = String(body.answer || "").trim() || "(empty answer)";
      send(entry.res, 200, { answer, is_error: !!body.is_error, meta: { answered_by: "interactive", id } });
      console.error(`[relay] answered ${id}`);
      return send(res, 200, { ok: true, id });
    }

    send(res, 404, { error: "not found" });
  });

  server.requestTimeout = 0;   // questions wait for a human; never cut the held /ask
  server.headersTimeout = 0;
  server.listen(port, "127.0.0.1", () => {
    console.error(`[claude-bridge] relay '${name}' on 127.0.0.1:${port} — incoming questions are answered in this peer's chat`);
  });
  return server;
}
