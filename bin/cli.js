#!/usr/bin/env node
// claude-bridge — let two Claude Code sessions talk. Two modes:
//
//   answer  long-running HTTP daemon that answers the partner's questions by
//           running the local `claude` CLI read-only in a project (no API key).
//   ask     stdio MCP server exposing ask_peer/tell_peer/peer_status/list_peers
//           to an interactive Claude Code session; Claude Code spawns it (e.g.
//           via npx in a .mcp.json entry). It POSTs questions to the partner.
//
// Examples:
//   claude-bridge answer --project /path/to/backend --current-port 8082 --name backend
//   claude-bridge ask --partner-port 8082 --name frontend --partner-name backend
//
//   // .mcp.json (Claude Code auto-spawns the ask client via npx — one-line attach):
//   { "mcpServers": { "bridge": {
//       "command": "npx", "args": ["-y", "github:ajaysavaliya8/claude-bridge", "ask", "--partner-port", "8082"] } } }

import { homedir } from "node:os";
import { join } from "node:path";
import { statSync, mkdirSync, writeFileSync, existsSync } from "node:fs";

import { AnswerEngine } from "../src/answerEngine.js";
import { startAnswerServer } from "../src/answerServer.js";
import { startAskServer } from "../src/askServer.js";
import { startRelayServer } from "../src/relayServer.js";

function parseArgs(argv) {
  const out = { _: [] };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a.startsWith("--")) {
      const key = a.slice(2);
      const next = argv[i + 1];
      if (next === undefined || next.startsWith("--")) out[key] = true;
      else { out[key] = next; i++; }
    } else out._.push(a);
  }
  return out;
}

const env = (name, dflt) => {
  const v = process.env[name];
  return v === undefined || v === "" ? dflt : v;
};

function die(msg) {
  console.error(`error: ${msg}`);
  process.exit(2);
}

function port(value, flag) {
  // No default by design: the port must be set explicitly for both ask and answer.
  if (value === undefined || value === true || value === "") {
    die(`${flag} is required — set a port explicitly (there is no default)`);
  }
  const n = Number(value);
  if (!Number.isInteger(n) || n < 1 || n > 65535) die(`${flag} must be a port 1-65535, got "${value}"`);
  return n;
}

const HELP = `claude-bridge <answer|ask> [options]

answer  (HTTP daemon — answers the partner, runs the local claude CLI)
  --project PATH       project this peer answers about (or env PROJECT_DIR)  [required]
  --current-port N     port to listen on (or CURRENT_PORT)  [required, no default]
  --name NAME          this peer's name (or PEER_SELF; default "peer")
  --chat-id ID         resume this Claude conversation when answering
  --claude-bin PATH    claude CLI (or CLAUDE_BIN; default "claude")
  --allowed-tools STR  read-only allowlist (default "Read,Grep,Glob")
  --max-turns N        (default 15)        --timeout SEC  per-answer cap (default 240)

ask  (stdio MCP — gives an interactive Claude the bridge tools)
  --partner-port N     partner's port (or PARTNER_PORT)  [required, no default]
  --partner-host HOST  partner host (default 127.0.0.1; use the SSH tunnel)
  --name NAME          this peer's name (or PEER_SELF; default "peer")
  --partner-name NAME  partner's name (or DEFAULT_TARGET; default "partner")
  --relay-port N       your local relay's port (or RELAY_PORT) — enables in-chat
                       answering tools (incoming_questions / answer_incoming)

relay  (in-chat answering inbox — queues incoming questions for this session)
  --current-port N     port to listen on (or CURRENT_PORT)  [required, no default]
  --name NAME          this peer's name (or PEER_SELF; default "peer")
  --hold SEC           how long to hold an unanswered question (default 1800)

Answering modes (pick one per peer): 'answer' = instant headless; 'relay' = your
interactive Claude pulls + answers in its own chat (run 'relay' AND register the
ask client with --relay-port). For two-way, run the same combo on both peers.`;

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const mode = args._[0];
  if (args.help || args.h || !mode) { console.error(HELP); process.exit(mode ? 0 : 2); }

  const name = args.name || env("PEER_SELF", "peer");

  if (mode === "answer") {
    const project = args.project || env("PROJECT_DIR");
    if (!project) die("answer needs --project (or PROJECT_DIR)");
    try { if (!statSync(project).isDirectory()) throw 0; }
    catch { die(`--project is not a directory: ${project}`); }

    const sessionDir = env("BRIDGE_SESSION_DIR", join(homedir(), ".claude-bridge", "sessions"));
    const sessionFile = join(sessionDir, `${name}.session`);
    if (args["chat-id"] && !existsSync(sessionFile)) {
      mkdirSync(sessionDir, { recursive: true });
      writeFileSync(sessionFile, String(args["chat-id"]), "utf8");
    }

    const engine = new AnswerEngine({
      projectDir: project,
      claudeBin: args["claude-bin"] || env("CLAUDE_BIN", "claude"),
      allowedTools: args["allowed-tools"] || env("ALLOWED_TOOLS", "Read,Grep,Glob"),
      maxTurns: parseInt(args["max-turns"] || env("MAX_TURNS", "15"), 10),
      timeoutSec: parseInt(args.timeout || env("CLAUDE_TIMEOUT_SECONDS", "240"), 10),
      sessionFile,
    });
    const currentPort = port(args["current-port"] ?? env("CURRENT_PORT"), "--current-port");
    startAnswerServer({ engine, port: currentPort, name });
    return;
  }

  if (mode === "ask") {
    const host = args["partner-host"] || "127.0.0.1";
    const partnerUrl = `http://${host}:${port(args["partner-port"] ?? env("PARTNER_PORT"), "--partner-port")}`;
    const partnerName = args["partner-name"] || env("DEFAULT_TARGET", "partner");
    const askTimeoutMs = (parseInt(env("ASK_TIMEOUT_SECONDS", "300"), 10) + 60) * 1000;
    // Optional: a local relay enables in-chat answering (incoming_questions /
    // answer_incoming). It's optional, so unset / empty / an unsubstituted plugin
    // placeholder just disables those tools rather than erroring.
    let relayUrl = null;
    const relayPortArg = args["relay-port"] ?? env("RELAY_PORT");
    if (relayPortArg !== undefined && relayPortArg !== true && relayPortArg !== "" && !String(relayPortArg).includes("${")) {
      relayUrl = `http://127.0.0.1:${port(relayPortArg, "--relay-port")}`;
    }
    await startAskServer({ name, partnerName, partnerUrl, askTimeoutMs, relayUrl });
    return;
  }

  if (mode === "relay") {
    // In-chat answering inbox: queues incoming questions for this peer's interactive
    // Claude to pull + answer (via the ask client's --relay-port tools).
    const relayPort = port(args["current-port"] ?? env("CURRENT_PORT"), "--current-port");
    const holdSeconds = parseInt(args.hold || env("RELAY_HOLD_SECONDS", "1800"), 10);
    startRelayServer({ port: relayPort, name, holdSeconds });
    return;
  }

  die(`unknown mode '${mode}' (expected 'answer', 'ask', or 'relay')`);
}

main().catch((e) => { console.error(e); process.exit(1); });
