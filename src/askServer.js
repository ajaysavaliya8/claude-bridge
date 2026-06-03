// ASK side: a stdio MCP server that exposes the bridge tools to an interactive
// Claude Code session. Claude Code spawns this over stdio (e.g. via npx in a
// .mcp.json entry), so it must NEVER write to stdout except MCP frames — all
// logging goes to stderr. Tools POST directly to the partner peer's HTTP port.

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

import { encodeImages } from "./images.js";
import { netError } from "./http.js";

const text = (s) => ({ content: [{ type: "text", text: s }] });

export async function startAskServer({ name, partnerName, partnerUrl, askTimeoutMs, relayUrl, token = null }) {
  const server = new McpServer({ name: `bridge:${name}`, version: "0.6.0" });
  const authHeaders = token ? { authorization: `Bearer ${token}` } : {};

  async function post(url, payload, timeoutMs) {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      const resp = await fetch(url, { method: "POST", headers: { "content-type": "application/json", ...authHeaders }, body: JSON.stringify(payload), signal: ctrl.signal });
      return { status: resp.status, text: await resp.text() };
    } finally { clearTimeout(t); }
  }
  async function get(url, timeoutMs) {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      const resp = await fetch(url, { headers: authHeaders, signal: ctrl.signal });
      return { status: resp.status, json: await resp.json().catch(() => null) };
    } finally { clearTimeout(t); }
  }

  server.registerTool(
    "ask_peer",
    {
      description:
        "Ask the partner peer's project an authoritative question and get a direct answer read from its real source. " +
        "Use whenever your work depends on a fact that lives in the OTHER project (a route, JSON field name, type, " +
        "status code, config key) instead of guessing. Attach screenshots/diagrams via image_paths (local file " +
        "paths, PNG/JPEG/GIF/WebP) — the peer genuinely sees them.",
      inputSchema: { question: z.string(), target: z.string().optional(), image_paths: z.array(z.string()).optional() },
    },
    async ({ question, target, image_paths }) => {
      if (target && target !== partnerName) return text(`Error: this peer only knows partner '${partnerName}', not '${target}'.`);
      let images;
      try { images = encodeImages(image_paths || []); } catch (e) { return text(`Error: ${e.message}`); }
      let r;
      try { r = await post(`${partnerUrl}/ask`, { sender: name, question, images }, askTimeoutMs); }
      catch (e) { return text(`Error reaching partner '${partnerName}' at ${partnerUrl}: ${netError(e)}. Try peer_status.`); }
      if (r.status === 401) return text(`Error: unauthorized — your token doesn't match partner '${partnerName}'.`);
      if (r.status !== 200) return text(`Error from partner '${partnerName}' (HTTP ${r.status}): ${r.text.slice(0, 500)}`);
      let data;
      try { data = JSON.parse(r.text); } catch { return text(`Error: partner returned non-JSON: ${r.text.slice(0, 300)}`); }
      const answer = String(data.answer || "").trim() || "(empty answer)";
      return text(data.is_error ? `⚠️ Partner '${partnerName}' could not answer:\n${answer}` : answer);
    },
  );

  server.registerTool(
    "tell_peer",
    {
      description: "Send a one-way note to the partner (fire-and-forget, no answer), optionally with images. Use to inform it of a decision or change.",
      inputSchema: { message: z.string(), target: z.string().optional(), image_paths: z.array(z.string()).optional() },
    },
    async ({ message, target, image_paths }) => {
      if (target && target !== partnerName) return text(`Error: this peer only knows partner '${partnerName}', not '${target}'.`);
      let images;
      try { images = encodeImages(image_paths || []); } catch (e) { return text(`Error: ${e.message}`); }
      let r;
      try { r = await post(`${partnerUrl}/tell`, { sender: name, message, images }, 30_000); }
      catch (e) { return text(`Error reaching partner '${partnerName}' at ${partnerUrl}: ${netError(e)}.`); }
      if (r.status === 401) return text(`Error: unauthorized — token mismatch with '${partnerName}'.`);
      if (![200, 202].includes(r.status)) return text(`Error delivering note (HTTP ${r.status}): ${r.text.slice(0, 300)}`);
      // Honest semantics: it's queued/accepted on the peer's side, not "read".
      return text(`Note queued for '${partnerName}' — fire-and-forget (it surfaces in the peer's session; there's no read receipt).`);
    },
  );

  server.registerTool(
    "peer_status",
    { description: "Report whether the partner peer is reachable, and how it answers.", inputSchema: { target: z.string().optional() } },
    async () => {
      let r;
      try { r = await get(`${partnerUrl}/health`, 5000); }
      catch (e) { return text(`Partner '${partnerName}' is OFFLINE/unreachable at ${partnerUrl}: ${netError(e)}.`); }
      if (r.status !== 200 || !r.json) return text(`Partner '${partnerName}' replied HTTP ${r.status} at ${partnerUrl}.`);
      const h = r.json;
      const extra = h.mode === "relay" ? `, ${h.pending ?? 0} pending` : "";
      return text(`Partner '${h.name || partnerName}' is ONLINE at ${partnerUrl} — mode=${h.mode || "?"}, answering=${h.answer === true}, version=${h.version || "?"}${extra}.`);
    },
  );

  server.registerTool(
    "list_peers",
    { description: "List the peers this session can talk to (self and the partner).", inputSchema: {} },
    async () => text(`You are '${name}'. Partner '${partnerName}' at ${partnerUrl} (use peer_status for liveness).`),
  );

  // In-chat answering: only when a local relay is configured.
  if (relayUrl) {
    server.registerTool(
      "incoming_questions",
      {
        description:
          "List questions/notes other peers have sent YOU that are waiting. Call this (e.g. when the user says to " +
          "check peer questions), then answer each question from THIS project's real code via answer_incoming.",
        inputSchema: {},
      },
      async () => {
        let r;
        try { r = await get(`${relayUrl}/pending`, 5000); }
        catch (e) { return text(`Error reaching local relay at ${relayUrl}: ${netError(e)}. Is the relay daemon running?`); }
        const items = r.json?.questions || [];
        if (!items.length) return text("No pending questions or notes.");
        return text(
          items
            .map((q) => {
              const tag = q.kind === "note" ? `📝 note [${q.id}]` : `[${q.id}]`;
              const imgs = q.images && q.images.length ? `\nImages (Read these files to see them): ${q.images.join(", ")}` : "";
              return `${tag} from "${q.sender}":\n${q.question}${imgs}`;
            })
            .join("\n\n") +
            `\n\nAnswer each QUESTION with answer_incoming(id, answer) (Read any images first). Notes (📝) are FYI — no reply needed.`,
        );
      },
    );

    server.registerTool(
      "answer_incoming",
      {
        description: "Send your answer to a pending incoming question (id from incoming_questions). Answer accurately from THIS project's real code.",
        inputSchema: { id: z.string(), answer: z.string() },
      },
      async ({ id, answer }) => {
        let r;
        try { r = await post(`${relayUrl}/answer`, { id, answer }, 10_000); }
        catch (e) { return text(`Error reaching local relay at ${relayUrl}: ${netError(e)}.`); }
        if (r.status === 404) return text(`No pending item '${id}' — it was already handled, expired, or the asker gave up.`);
        if (r.status !== 200) return text(`Error sending answer (HTTP ${r.status}): ${r.text.slice(0, 300)}`);
        let data;
        try { data = JSON.parse(r.text); } catch { data = {}; }
        if (data.kind === "note") return text(`Noted (${id}) — notes don't get a reply.`);
        if (data.delivered === false) return text(`⚠️ Sent ${id}, but the asker had already given up — answer not delivered.`);
        return text(`Answer for ${id} delivered to the asker.`);
      },
    );
  }

  await server.connect(new StdioServerTransport());
  console.error(
    `[claude-bridge] ask MCP (stdio) for '${name}' -> partner '${partnerName}' at ${partnerUrl}` +
      (relayUrl ? ` | answering incoming via relay ${relayUrl}` : "") + (token ? " | auth on" : ""),
  );
}
