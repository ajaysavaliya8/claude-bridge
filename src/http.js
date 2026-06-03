// Tiny shared HTTP helpers for the node:http servers (answer daemon + relay).

import { readFileSync } from "node:fs";
import { timingSafeEqual } from "node:crypto";

export const VERSION = (() => {
  try { return JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8")).version; }
  catch { return "0.0.0"; }
})();

// Read a request body with a size cap AND an idle bound (a connection that
// trickles or never finishes is reaped instead of held open forever).
export function readBody(req, limitBytes = 1_000_000, idleMs = 20_000) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    let idle;
    const settle = (fn, arg) => { clearTimeout(idle); fn(arg); };
    const arm = () => { clearTimeout(idle); idle = setTimeout(() => { req.destroy(); reject(new Error("request body idle timeout")); }, idleMs); };
    arm();
    req.on("data", (c) => {
      size += c.length;
      if (size > limitBytes) { req.destroy(); settle(reject, new Error("request too large")); return; }
      chunks.push(c);
      arm();
    });
    req.on("end", () => settle(resolve, Buffer.concat(chunks).toString("utf8")));
    req.on("error", (e) => settle(reject, e));
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
// (backward compatible). Otherwise require `Authorization: Bearer <token>`,
// compared in constant time.
export function tokenOk(req, token) {
  if (!token) return true;
  const got = req.headers["authorization"];
  if (typeof got !== "string") return false;
  const a = Buffer.from(got);
  const b = Buffer.from(`Bearer ${token}`);
  return a.length === b.length && timingSafeEqual(a, b);
}

// Friendly EADDRINUSE handling, and only exit on a real startup/bind failure —
// a transient post-listening server error is logged, not fatal.
export function onListenError(server, port) {
  let up = false;
  server.on("listening", () => { up = true; });
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
      console.error(`[claude-bridge] port ${port} is already in use by ${who}.` + (mine ? " It's already running — you don't need to start another." : " Stop it, or pick a different port."));
      process.exit(1);
    } else if (!up) {
      console.error(`[claude-bridge] failed to start on port ${port}: ${e.message}`);
      process.exit(1);
    } else {
      console.error(`[claude-bridge] server error (continuing): ${e.message}`);
    }
  });
}

// Graceful shutdown: stop accepting, then exit (with a hard fallback so held
// long-poll sockets can't block the exit).
export function installShutdown(server) {
  const bye = () => {
    try { server.close(() => process.exit(0)); } catch { process.exit(0); }
    setTimeout(() => process.exit(0), 1000).unref();
  };
  process.once("SIGINT", bye);
  process.once("SIGTERM", bye);
}
