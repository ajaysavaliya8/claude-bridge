// Tiny shared HTTP helpers for the node:http servers (answer daemon + relay).

import { readFileSync } from "node:fs";

export const VERSION = (() => {
  try { return JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8")).version; }
  catch { return "0.0.0"; }
})();

export function readBody(req, limitBytes = 1_000_000) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on("data", (c) => {
      size += c.length;
      if (size > limitBytes) { reject(new Error("request too large")); req.destroy(); return; }
      chunks.push(c);
    });
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    req.on("error", reject);
  });
}

export function send(res, code, obj) {
  res.writeHead(code, { "content-type": "application/json" });
  res.end(JSON.stringify(obj));
}

// Turn an opaque fetch failure into something diagnosable.
export function netError(e) {
  const code = e?.cause?.code || e?.code;
  if (e?.name === "AbortError") return "timed out — host/tunnel reachable but no response (peer hung?)";
  if (code === "ECONNREFUSED") return "connection refused — nothing is listening there (daemon down, or wrong port)";
  if (code === "ENOTFOUND" || code === "EAI_AGAIN") return "host not found — check the address / tunnel";
  if (code === "ETIMEDOUT") return "timed out — no route (tunnel down?)";
  if (code === "ECONNRESET") return "connection reset by peer";
  return e?.message || String(e);
}

// Optional shared-secret auth. If `token` is falsy, everything is allowed
// (backward compatible). Otherwise require `Authorization: Bearer <token>`.
export function tokenOk(req, token) {
  if (!token) return true;
  return req.headers["authorization"] === `Bearer ${token}`;
}

// Friendly EADDRINUSE handling: probe the port; if it's already a claude-bridge
// peer, say so (likely fine) instead of dumping a raw stack trace.
export function onListenError(server, port) {
  server.on("error", async (e) => {
    if (e.code === "EADDRINUSE") {
      let who = "another process (not claude-bridge)";
      let mine = false;
      try {
        const ctrl = new AbortController();
        const tm = setTimeout(() => ctrl.abort(), 1500);
        const r = await fetch(`http://127.0.0.1:${port}/health`, { signal: ctrl.signal });
        clearTimeout(tm);
        const h = await r.json();
        if (h && (h.mode || h.answer !== undefined)) { who = `a claude-bridge ${h.mode || "peer"} named '${h.name}'`; mine = true; }
      } catch { /* not one of ours */ }
      console.error(
        `[claude-bridge] port ${port} is already in use by ${who}.` +
          (mine ? " It's already running — you don't need to start another." : " Stop it, or pick a different port."),
      );
    } else {
      console.error(`[claude-bridge] server error: ${e.message}`);
    }
    process.exit(1);
  });
}
