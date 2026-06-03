// ANSWER side: a small HTTP daemon the partner posts questions to. It runs the
// claude answering engine read-only in the project (headless). Attached images are
// saved to disk and their paths handed to claude (best-effort — headless image
// reading is less reliable than the interactive 'relay' mode). Built on node:http.

import http from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { readBody, send } from "./http.js";
import { saveImages, MAX_PAYLOAD_BYTES } from "./images.js";

export function startAnswerServer({ engine, port, name }) {
  let seq = 0;
  const server = http.createServer(async (req, res) => {
    const url = (req.url || "/").split("?")[0];

    if (req.method === "GET" && url === "/health") {
      return send(res, 200, { status: "ok", name, answer: true });
    }

    if (req.method === "POST" && (url === "/ask" || url === "/tell")) {
      let body;
      try { body = JSON.parse(await readBody(req, MAX_PAYLOAD_BYTES)); }
      catch { return send(res, 400, { answer: "bad request: invalid JSON", is_error: true }); }
      const sender = String(body.sender || "peer") || "peer";
      const imagePaths = saveImages(body.images, join(tmpdir(), "claude-bridge-answer", name, `m${++seq}`));

      if (url === "/ask") {
        const question = String(body.question || "").trim();
        if (!question) return send(res, 400, { answer: "bad request: empty question", is_error: true });
        const imgNote = imagePaths.length ? `, ${imagePaths.length} image(s)` : "";
        console.error(`answering question from '${sender}' (${question.length} chars${imgNote})`);
        const result = await engine.answer(sender, question, imagePaths);
        return send(res, 200, result);
      }
      // /tell — fire-and-forget note
      let message = String(body.message || "").trim();
      if (imagePaths.length) message += `\n(attached image file(s): ${imagePaths.join(", ")})`;
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
