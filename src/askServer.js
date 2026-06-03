// ASK side: a stdio MCP server that exposes the bridge tools to an interactive
// Claude Code session. Claude Code spawns this over stdio (e.g. via npx in a
// .mcp.json entry), so it must NEVER write to stdout except MCP frames — all
// logging goes to stderr. Tools POST directly to the partner peer's HTTP port.

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

async function postJson(url, payload, timeoutMs) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
      signal: ctrl.signal,
    });
    const text = await resp.text();
    return { status: resp.status, text };
  } finally {
    clearTimeout(t);
  }
}

const text = (s) => ({ content: [{ type: "text", text: s }] });

export async function startAskServer({ name, partnerName, partnerUrl, askTimeoutMs, relayUrl }) {
  const server = new McpServer({ name: `bridge:${name}`, version: "0.1.0" });

  server.registerTool(
    "ask_peer",
    {
      description:
        "Ask the partner peer's project an authoritative question and get a direct answer read from its real source. " +
        "Use whenever your work depends on a fact that lives in the OTHER project (a route, JSON field name, type, " +
        "status code, config key) instead of guessing.",
      inputSchema: { question: z.string(), target: z.string().optional() },
    },
    async ({ question, target }) => {
      if (target && target !== partnerName) {
        return text(`Error: this peer only knows partner '${partnerName}', not '${target}'.`);
      }
      let r;
      try {
        r = await postJson(`${partnerUrl}/ask`, { sender: name, question }, askTimeoutMs);
      } catch (e) {
        return text(`Error: could not reach partner '${partnerName}' at ${partnerUrl} (${e.message}). Is its answer daemon running (and the tunnel up)? Try peer_status.`);
      }
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
      description: "Send a one-way note to the partner (fire-and-forget, no answer). Use to inform it of a decision or change.",
      inputSchema: { message: z.string(), target: z.string().optional() },
    },
    async ({ message, target }) => {
      if (target && target !== partnerName) {
        return text(`Error: this peer only knows partner '${partnerName}', not '${target}'.`);
      }
      try {
        const r = await postJson(`${partnerUrl}/tell`, { sender: name, message }, 10_000);
        if (![200, 202].includes(r.status)) return text(`Error delivering note (HTTP ${r.status}): ${r.text.slice(0, 300)}`);
      } catch (e) {
        return text(`Error: could not reach partner '${partnerName}' at ${partnerUrl} (${e.message}).`);
      }
      return text(`Note delivered to '${partnerName}'.`);
    },
  );

  server.registerTool(
    "peer_status",
    { description: "Report whether the partner peer is reachable and answering.", inputSchema: { target: z.string().optional() } },
    async () => {
      try {
        const ctrl = new AbortController();
        const t = setTimeout(() => ctrl.abort(), 5000);
        const resp = await fetch(`${partnerUrl}/health`, { signal: ctrl.signal });
        clearTimeout(t);
        if (resp.status === 200) {
          const h = await resp.json();
          return text(`Partner '${h.name || partnerName}' is ONLINE at ${partnerUrl} (answering=${h.answer}).`);
        }
        return text(`Partner '${partnerName}' replied HTTP ${resp.status} at ${partnerUrl}.`);
      } catch (e) {
        return text(`Partner '${partnerName}' is OFFLINE/unreachable at ${partnerUrl} (${e.message}).`);
      }
    },
  );

  server.registerTool(
    "list_peers",
    { description: "List the peers this session can talk to (self and the partner).", inputSchema: {} },
    async () => text(`You are '${name}'. Partner '${partnerName}' at ${partnerUrl} (use peer_status for liveness).`),
  );

  // In-chat answering: when a local relay is configured, expose tools so THIS
  // session can pull questions other peers asked it and answer them in-chat.
  if (relayUrl) {
    server.registerTool(
      "incoming_questions",
      {
        description:
          "List questions other peers have asked YOU that are waiting for an answer. Call this (e.g. when the user " +
          "says to check peer questions), then answer each one accurately from THIS project's real code via answer_incoming.",
        inputSchema: {},
      },
      async () => {
        try {
          const ctrl = new AbortController();
          const t = setTimeout(() => ctrl.abort(), 5000);
          const resp = await fetch(`${relayUrl}/pending`, { signal: ctrl.signal });
          clearTimeout(t);
          const data = await resp.json();
          if (!data.questions || data.questions.length === 0) return text("No pending questions.");
          return text(
            data.questions.map((q) => `[${q.id}] from "${q.sender}":\n${q.question}`).join("\n\n") +
              `\n\nAnswer each with answer_incoming(id, answer).`,
          );
        } catch (e) {
          return text(`Error reaching local relay at ${relayUrl} (${e.message}). Is the relay daemon running?`);
        }
      },
    );

    server.registerTool(
      "answer_incoming",
      {
        description:
          "Send your answer to a pending incoming question (get its id from incoming_questions). The answer is " +
          "returned to the peer that asked. Answer accurately and authoritatively from THIS project's real code.",
        inputSchema: { id: z.string(), answer: z.string() },
      },
      async ({ id, answer }) => {
        try {
          const r = await postJson(`${relayUrl}/answer`, { id, answer }, 10_000);
          if (r.status !== 200) return text(`Error sending answer (HTTP ${r.status}): ${r.text.slice(0, 300)}`);
          return text(`Answer for ${id} delivered to the asker.`);
        } catch (e) {
          return text(`Error reaching local relay at ${relayUrl} (${e.message}).`);
        }
      },
    );
  }

  await server.connect(new StdioServerTransport());
  console.error(
    `[claude-bridge] ask MCP (stdio) for '${name}' -> partner '${partnerName}' at ${partnerUrl}` +
      (relayUrl ? ` | answering incoming via relay ${relayUrl}` : ""),
  );
}
