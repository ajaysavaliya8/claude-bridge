// `claude-bridge doctor` — one-shot topology/health check so you don't have to
// reconstruct it from claude mcp list + manual probes.

import { VERSION } from "./http.js";

async function probe(url, token) {
  try {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 3000);
    const r = await fetch(url, { headers: token ? { authorization: `Bearer ${token}` } : {}, signal: ctrl.signal });
    clearTimeout(t);
    if (r.status !== 200) return { ok: false, detail: `HTTP ${r.status}` };
    return { ok: true, h: await r.json() };
  } catch (e) {
    return { ok: false, detail: e?.cause?.code || e?.name || e?.message };
  }
}

export async function runDoctor({ name, currentPort, partnerHost = "127.0.0.1", partnerPort, token }) {
  const L = (s) => console.log(s);
  L(`claude-bridge doctor — v${VERSION}`);
  L(`this peer: '${name || "(unset)"}'${token ? "  (auth token set)" : ""}`);
  L("");

  if (currentPort) {
    const r = await probe(`http://127.0.0.1:${currentPort}/health`, token);
    if (r.ok) L(`✓ your daemon on :${currentPort} — mode=${r.h.mode}, answering=${r.h.answer === true}, v${r.h.version || "?"}${r.h.mode === "relay" ? `, ${r.h.pending ?? 0} pending` : ""}`);
    else L(`✗ your daemon on :${currentPort} — not reachable (${r.detail}). Start it:  claude-bridge relay|answer --current-port ${currentPort} --name ${name || "me"}`);
  } else {
    L("• your daemon — pass --current-port to check it");
  }

  if (partnerPort) {
    const r = await probe(`http://${partnerHost}:${partnerPort}/health`, token);
    if (r.ok) L(`✓ partner at ${partnerHost}:${partnerPort} — '${r.h.name}', mode=${r.h.mode}, answering=${r.h.answer === true}, v${r.h.version || "?"}`);
    else L(`✗ partner at ${partnerHost}:${partnerPort} — unreachable (${r.detail}). Is its daemon up and the SSH tunnel open?`);
  } else {
    L("• partner — pass --partner-port to check it");
  }

  L("");
  L("MCP tools registered with Claude Code?   run:  claude mcp get bridge");
  L('⚠ After "claude mcp add", RELOAD your Claude Code host to load the tools');
  L('  (VS Code: Command Palette → "Developer: Reload Window"). Reopening the chat panel is NOT enough.');
}
