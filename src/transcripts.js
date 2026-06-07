// Cross-machine transcript search/read: lets a peer grep + read THIS machine's
// Claude Code chat transcripts (~/.claude/projects/<encoded-cwd>/<session>.jsonl)
// so the partner can ask "what did your session actually discuss/decide?", not
// just "what's in the code". Read-only; bounded by a scan-size cap.

import { readdirSync, readFileSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const DEFAULT_ROOT = join(homedir(), ".claude", "projects");
const MAX_SCAN_BYTES = 200_000_000; // 200 MB safety cap (mirrors the reference impl)

// Build a case-insensitive matcher: /.../ → regex, otherwise substring.
function toRegex(query) {
  const m = /^\/(.*)\/([a-z]*)$/.exec(query);
  try { return m ? new RegExp(m[1], m[2].includes("i") ? m[2] : m[2] + "i") : new RegExp(query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "i"); }
  catch { return new RegExp(query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "i"); }
}

// Pull human-readable text + role from one JSONL event line (best-effort;
// falls back to the raw line so a substring search still matches).
function lineText(line) {
  try {
    const o = JSON.parse(line);
    const role = o.type || o.role || o.message?.role || "";
    const c = o.message?.content ?? o.summary ?? o.content;
    let text = "";
    if (typeof c === "string") text = c;
    else if (Array.isArray(c)) text = c.map((b) => (typeof b === "string" ? b : b?.text || "")).filter(Boolean).join(" ");
    return { role, text: text || "", ts: o.timestamp || o.ts || null };
  } catch {
    return { role: "", text: line, ts: null };
  }
}

function listDirs(root, project) {
  let dirs;
  try { dirs = readdirSync(root, { withFileTypes: true }).filter((d) => d.isDirectory()).map((d) => d.name); }
  catch { return null; }
  return project ? dirs.filter((d) => d.toLowerCase().includes(String(project).toLowerCase())) : dirs;
}

function snippet(text, rx, ctx = 200) {
  const i = text.search(rx);
  if (i < 0) return text.slice(0, ctx * 2);
  return (i > ctx ? "…" : "") + text.slice(Math.max(0, i - ctx), i + ctx) + (text.length > i + ctx ? "…" : "");
}

// Search across this machine's transcripts. Returns { matches, ... } or
// { scope_too_large } / { note }.
export function searchTranscripts({ query, project, limit = 30, contextChars = 200, root = DEFAULT_ROOT } = {}) {
  if (!query || !String(query).trim()) return { error: "query is required" };
  const dirs = listDirs(root, project);
  if (dirs === null) return { matches: [], note: "no Claude Code transcripts on this peer" };
  const rx = toRegex(String(query));
  const matches = [];
  let scanned = 0;
  for (const d of dirs) {
    let files;
    try { files = readdirSync(join(root, d)).filter((f) => f.endsWith(".jsonl")); } catch { continue; }
    for (const f of files) {
      const fp = join(root, d, f);
      let size = 0;
      try { size = statSync(fp).size; } catch { continue; }
      scanned += size;
      if (scanned > MAX_SCAN_BYTES) return { matches, scope_too_large: true, note: `stopped after ~${Math.round(scanned / 1e6)} MB — narrow with project=<substring>` };
      let content;
      try { content = readFileSync(fp, "utf8"); } catch { continue; }
      for (const line of content.split("\n")) {
        if (!line || !rx.test(line)) continue; // cheap raw prefilter
        const { role, text, ts } = lineText(line);
        if (!text || !rx.test(text)) continue;
        matches.push({ session: f.replace(/\.jsonl$/, ""), project: d, role, ts, snippet: snippet(text, rx, contextChars) });
        if (matches.length >= limit) return { matches, truncated: true };
      }
    }
  }
  return { matches };
}

// Read one session's transcript (by session id, or the most-recent under an
// optional project filter): last N messages, or only since the last user prompt.
export function readSession({ session, project, lastN = 20, sinceLastUserPrompt = false, root = DEFAULT_ROOT } = {}) {
  const dirs = listDirs(root, project);
  if (dirs === null) return { error: "no Claude Code transcripts on this peer" };
  let chosen = null; // { fp, session, project, mtime }
  for (const d of dirs) {
    let files;
    try { files = readdirSync(join(root, d)).filter((f) => f.endsWith(".jsonl")); } catch { continue; }
    for (const f of files) {
      const sid = f.replace(/\.jsonl$/, "");
      if (session && sid !== session) continue;
      const fp = join(root, d, f);
      let mtime = 0;
      try { mtime = statSync(fp).mtimeMs; } catch { continue; }
      if (!chosen || mtime > chosen.mtime) chosen = { fp, session: sid, project: d, mtime };
    }
  }
  if (!chosen) return { error: session ? `session '${session}' not found on this peer` : "no sessions found" };
  let msgs;
  try {
    msgs = readFileSync(chosen.fp, "utf8").split("\n").filter(Boolean)
      .map(lineText).filter((m) => m.text && (m.role === "user" || m.role === "assistant" || m.role === "summary"));
  } catch (e) { return { error: `could not read session: ${e.message}` }; }
  if (sinceLastUserPrompt) {
    const lastUser = msgs.map((m) => m.role).lastIndexOf("user");
    if (lastUser >= 0) msgs = msgs.slice(lastUser);
  } else {
    msgs = msgs.slice(-Math.max(1, lastN));
  }
  return { session: chosen.session, project: chosen.project, messages: msgs };
}
