// ANSWER side: a small HTTP daemon the partner posts questions to. It runs the
// claude answering engine read-only in the project. Built on node:http (no deps).

import http from "node:http";

import { readBody, send } from "./http.js";

export function startAnswerServer({ engine, port, name }) {
  const server = http.createServer(async (req, res) => {
    const url = (req.url || "/").split("?")[0];

    if (req.method === "GET" && url === "/health") {
      return send(res, 200, { status: "ok", name, answer: true });
    }

    if (req.method === "POST" && (url === "/ask" || url === "/tell")) {
      let body;
      try { body = JSON.parse(await readBody(req)); }
      catch { return send(res, 400, { answer: "bad request: invalid JSON", is_error: true }); }
      const sender = String(body.sender || "peer") || "peer";

      if (url === "/ask") {
        const question = String(body.question || "").trim();
        if (!question) return send(res, 400, { answer: "bad request: empty question", is_error: true });
        console.error(`answering question from '${sender}' (${question.length} chars)`);
        const result = await engine.answer(sender, question);
        return send(res, 200, result);
      }
      // /tell — fire-and-forget note
      const message = String(body.message || "").trim();
      if (message) engine.note(sender, message); // do not await
      return send(res, 202, { ok: true });
    }

    send(res, 404, { error: "not found" });
  });

  // Answers can legitimately take minutes (claude reading a big project); don't let
  // Node's default request timeout (5 min) cut a long answer short.
  server.requestTimeout = 0;
  server.headersTimeout = 0;

  server.listen(port, "127.0.0.1", () => {
    console.error(`[claude-bridge] answer daemon '${name}' on 127.0.0.1:${port} (project: ${engine.projectDir})`);
  });
  return server;
}
