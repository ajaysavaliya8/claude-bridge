// Ask-side MCP tool layer (T4): drive the real stdio `ask` client against a relay
// and exercise peer_status, target-mismatch, the in-chat round-trip, and notes.

import { test } from "node:test";
import assert from "node:assert/strict";

import { startRelayServer } from "../src/relayServer.js";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

const T = (r) => r.content.map((c) => c.text).join("");

test("ask-side tools: status, target-mismatch, in-chat round-trip, note (T4)", async () => {
  const relay = startRelayServer({ port: 0, name: "B", holdSeconds: 30 });
  if (!relay.listening) await new Promise((r) => relay.once("listening", r));
  const p = String(relay.address().port);

  const transport = new StdioClientTransport({
    command: "node",
    args: ["bin/cli.js", "ask", "--partner-port", p, "--relay-port", p, "--name", "B", "--partner-name", "A"],
  });
  const c = new Client({ name: "t", version: "1.0.0" });
  await c.connect(transport);
  try {
    const tools = (await c.listTools()).tools.map((t) => t.name);
    for (const n of ["ask_peer", "tell_peer", "peer_status", "list_peers", "incoming_questions", "answer_incoming"]) {
      assert.ok(tools.includes(n), `missing tool ${n}`);
    }

    assert.match(T(await c.callTool({ name: "peer_status", arguments: {} })), /ONLINE.*mode=relay/);
    assert.match(T(await c.callTool({ name: "ask_peer", arguments: { question: "x", target: "nope" } })), /only knows partner 'A'/);

    // in-chat round-trip: ask (held) → incoming_questions → answer_incoming → asker resolves
    const askP = c.callTool({ name: "ask_peer", arguments: { question: "what port?" } });
    let id;
    for (let i = 0; i < 200 && !id; i++) {
      id = T(await c.callTool({ name: "incoming_questions", arguments: {} })).match(/\[(q\d+)\]/)?.[1];
      if (!id) await new Promise((r) => setTimeout(r, 20));
    }
    assert.ok(id, "question should surface via incoming_questions");
    assert.match(T(await c.callTool({ name: "answer_incoming", arguments: { id, answer: "9001" } })), /delivered/);
    assert.match(T(await askP), /9001/);

    assert.match(T(await c.callTool({ name: "tell_peer", arguments: { message: "fyi" } })), /queued/i);
  } finally {
    await c.close();
    relay.close();
  }
});
