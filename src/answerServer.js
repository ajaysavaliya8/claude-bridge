// ANSWER side: a small HTTP daemon the partner posts questions to. It runs the
// claude answering engine read-only in the project (headless). Attached images are
// saved to disk and their paths handed to claude (best-effort), then cleaned up.

import http from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { readBody, send, tokenOk, onListenError, VERSION } from "./http.js";
import { saveImages, cleanupDir, MAX_PAYLOAD_BYTES } from "./images.js";

export function startAnswerServer({ engine, port, name, token = null }) {
  let seq = 0;

  async function handle(req, res) {
    const url = (req.url || "/").split("?")[0];

    if (req.method === "GET" && url === "/health") {
      return send(res, 200, { status: "ok", name, mode: "answer", answer: true, version: VERSION });
    }
    if (!tokenOk(req, token)) return send(res, 401, { answer: "unauthorized (bad or missing token)", is_error: true });

    if (req.method === "POST" && (url === "/ask" || url === "/tell")) {
      let body;
      try { body = JSON.parse(await readBody(req, MAX_PAYLOAD_BYTES)); }
      catch { return send(res, 400, { answer: "bad request: invalid JSON", is_error: true }); }
      const sender = String(body.sender || "peer") || "peer";
      const imageDir = join(tmpdir(), "claude-bridge-answer", name, `m${++seq}`);
      const imagePaths = saveImages(body.images, imageDir);

      if (url === "/ask") {
        const question = String(body.question || "").trim();
        if (!question) { cleanupDir(imagePaths.length ? imageDir : null); return send(res, 400, { answer: "bad request: empty question", is_error: true }); }
        console.error(`answering question from '${sender}' (${question.length} chars${imagePaths.length ? `, ${imagePaths.length} image(s)` : ""})`);
        try {
          const result = await engine.answer(sender, question, imagePaths);
          return send(res, 200, result);
        } finally {
          cleanupDir(imagePaths.length ? imageDir : null);
        }
      }
      // /tell — fire-and-forget note (injected into this peer's accumulating session)
      let message = String(body.message || "").trim();
      if (imagePaths.length) message += `\n(attached image file(s): ${imagePaths.join(", ")})`;
      if (message) engine.note(sender, message).finally(() => cleanupDir(imagePaths.length ? imageDir : null));
      else cleanupDir(imagePaths.length ? imageDir : null);
      return send(res, 202, { ok: true, queued: true, note: "injected into the peer's session; fire-and-forget" });
    }

    send(res, 404, { error: "not found" });
  }

  const server = http.createServer((req, res) => {
    handle(req, res).catch((e) => {
      console.error("[answer] handler error:", e?.message || e);
      if (!res.headersSent) { try { send(res, 500, { error: "internal error", is_error: true }); } catch { /* socket gone */ } }
    });
  });
  server.requestTimeout = 0;
  server.headersTimeout = 0;
  onListenError(server, port);
  server.listen(port, "127.0.0.1", () => {
    console.error(`[claude-bridge ${VERSION}] answer daemon '${name}' on 127.0.0.1:${port}${token ? " (auth on)" : ""} (project: ${engine.projectDir})`);
  });
  return server;
}
