# claude-bridge

**Let two Claude Code sessions talk to each other.** A lightweight, self-hostable
peer process per project: its interactive Claude can `ask_peer(...)` the other
project, and it answers the other project's questions by reading its *own* real
source with the local `claude` CLI.

When your frontend depends on your backend's contract — a route, a JSON field
name, a type, a status code — the frontend's Claude Code session just **asks the
backend's session** instead of guessing. The answer comes from a process that
actually read the backend code, so it's authoritative. **No API key** — answering
uses your Claude subscription via the local `claude` CLI.

> Claude Code · MCP · agent-to-agent · cross-project / multi-repo AI · Node.

---

## How it works

Two Claude Code sessions can't see each other, and a tool can deliver a question
but can't make the other session *think*. So each project runs a small **peer**
with two sides (the same shape as other MCP integrations — a spawned client plus a
running backend):

| Mode | What it is |
|---|---|
| **`ask`** | a **stdio MCP server** that gives an interactive Claude Code session the tools `ask_peer` · `tell_peer` · `peer_status` · `list_peers`. Claude Code spawns it (e.g. via `npx`); it POSTs questions to the partner. |
| **`answer`** | a long-running **HTTP daemon** that answers the partner's questions by running `claude -p` read-only inside a project. |

The two peers talk **directly** over their ports — no central broker, no database.

```
LOCAL — peer "frontend"                       VPS — peer "backend"
┌──────────────────────────────┐          ┌──────────────────────────────┐
│ Claude Code (interactive)     │          │ Claude Code (interactive)     │
│   └─ ask (stdio MCP) ──┐      │          │   └─ ask (stdio MCP) ──┐      │
│ answer daemon :8081 ───┴── ask/answer over ports ──┴─── answer daemon :8082 │
│   └─ reads frontend project   │          │   └─ reads backend project    │
└──────────────────────────────┘          └──────────────────────────────┘
       (same machine: two ports · across machines: one SSH tunnel)
```

---

## Install

```bash
# install the CLI globally from GitHub (not yet published to npm):
npm install -g github:ajaysavaliya8/claude-bridge
# …or, working in a clone, just install deps:
npm install
```

Requires **Node ≥ 18** and the **`claude` CLI** installed + authenticated on any
machine that *answers* (pass `--claude-bin /path` if it isn't on `PATH`).

---

## Attach in one line (the npx way)

Add to a project's `.mcp.json` (or `claude mcp add`). Claude Code installs + spawns
it; restart the session and the tools appear:

```jsonc
{ "mcpServers": { "bridge": {
    "command": "npx", "args": ["-y", "github:ajaysavaliya8/claude-bridge", "ask", "--partner-port", "8082"]
} } }
```

## Install as a Claude Code plugin (auto-registers the tools)

The repo doubles as a plugin **marketplace**, so you can attach the `ask` tools
with no manual `claude mcp add` — from the Claude Code chat:

```text
/plugin marketplace add ajaysavaliya8/claude-bridge
/plugin install claude-bridge@claude-bridge
```

(or in a terminal: `claude plugin marketplace add ajaysavaliya8/claude-bridge && claude plugin install claude-bridge@claude-bridge`). Enabling the plugin auto-registers
the `bridge` tools — it runs the `ask` client via `npx`. You **must set the
partner port** when enabling the plugin (a required config field — no default, so
it won't connect until set). You still run one **answer daemon** per project (a
plugin can't host a long-running process), with an explicit `--current-port`:

```bash
claude-bridge answer --project /path --current-port 8082
```

## Run a peer

```bash
# answers about this project, on port 8082
claude-bridge answer --project /path/to/backend --current-port 8082 --name backend

# (the partner does the mirror image on its own machine/port)
claude-bridge answer --project /path/to/frontend --current-port 8081 --name frontend
```

**Same machine:** that's it — two ports. **Across machines:** open one SSH tunnel
from the laptop carrying both directions, then each side's `ask` reaches the other:

```bash
ssh -N -L 8082:127.0.0.1:8082 -R 8081:127.0.0.1:8081 <vps-user>@<vps-host>
```

(For one-way "frontend asks backend", drop `-R` and only the backend runs `answer`.)

---

## Answering modes — headless vs. in-chat

Each peer chooses how it answers the *other* peer's questions:

- **`answer` (headless, instant):** runs `claude` automatically; the asker gets a
  reply in seconds, no human involved. The exchange shows only in the daemon log.
- **`relay` (in-chat, visible):** queues each incoming question so **this peer's
  interactive Claude** answers it *in its own chat* — it calls `incoming_questions`,
  then `answer_incoming(id, answer)`, and the reply flows back to the asker.
  Visible/auditable on the answering side; the asker waits until you answer.

### Two-way in-chat (A ⇆ B)
Run the **same combo** on both peers — a relay (your inbox) plus an ask client
pointed at the partner *and* at your own relay:

```bash
# peer B on 8082 (partner A is on 8081):
claude-bridge relay --current-port 8082 --name B
claude mcp add bridge -- claude-bridge ask --partner-port 8081 --relay-port 8082 --name B --partner-name A

# peer A is the mirror image: relay --current-port 8081, ask --partner-port 8082 --relay-port 8081
```

Now each side sees the other's questions in its own chat and answers them there.
Say e.g. *"check peer questions"* in your session → Claude calls
`incoming_questions` and answers each with `answer_incoming`. Raise
`ASK_TIMEOUT_SECONDS` on the asker if answers take a while (a human is in the loop).

## Configuration

**No config file is required.** Everything is a flag. A few environment variables
act as *optional* fallbacks (they must be exported in the shell — there is no
`.env` auto-loading): `PROJECT_DIR`, `CLAUDE_BIN`, `PEER_SELF` (→`--name`),
`DEFAULT_TARGET` (→`--partner-name`), `ALLOWED_TOOLS`, `MAX_TURNS`,
`CLAUDE_TIMEOUT_SECONDS`, `BRIDGE_SESSION_DIR`.

```
answer  --project PATH  --current-port N  [--name N] [--chat-id ID]
        [--claude-bin PATH] [--allowed-tools "Read,Grep,Glob"] [--max-turns 15] [--timeout 240]
ask     --partner-port N  [--partner-host 127.0.0.1] [--name N] [--partner-name N]
```

`--chat-id` resumes a specific Claude conversation when answering, so context
accumulates (subscription auth, no key). It's a **headless resume** — it does not
drive an already-open interactive window (no supported API exists for that).

### Setting / changing the port

Claude Code's MCP panel has **no input fields for ports** — it only shows
Connect / Reconnect / Disable and the tool list. A peer's ports live in how you
launch it, not in the UI:

- **answer daemon** — `--current-port` on the `answer` command (the port the
  partner connects to).
- **ask client** — `--partner-port` in its registration: either the
  `claude mcp add … ask --partner-port N` arguments, or a `.mcp.json` entry.

**Ports are required — there is no default.** Set `--current-port` (answer) and
`--partner-port` (ask) explicitly, or via `CURRENT_PORT` / `PARTNER_PORT`. A peer
**refuses to start** without its port. The plugin prompts for the partner port
when you enable it (a required config field), so it won't connect until you set it.

To change the ask client's port, edit its `.mcp.json` (the port is a plain,
editable value) and click **Reconnect** in the MCP panel — or re-run
`claude mcp add`:

```jsonc
// .mcp.json — edit "8082", save, then hit Reconnect
{ "mcpServers": { "bridge": {
    "command": "npx", "args": ["-y", "github:ajaysavaliya8/claude-bridge", "ask", "--partner-port", "8082"]
} } }
```

### Sharing images (screenshots / diagrams)

Attach images to a question — **no API key needed**:

```
ask_peer("does my screen match this endpoint?", image_paths=["C:/Users/you/shot.png"])
```

The image is sent to the peer and saved on its machine. In **in-chat (`relay`)**
mode the answering Claude reads it directly (reliable). In headless **`answer`**
mode the path is handed to `claude` (best-effort — headless image reading is less
reliable). Supported: PNG, JPEG, GIF, WebP; up to 5 images, 5 MB each.

Drop [peer-snippet.md](peer-snippet.md) into each project's `CLAUDE.md` so its
Claude reaches for `ask_peer` automatically.

---

## Security model

No application-level auth by design: each peer binds to `127.0.0.1`, and
cross-machine traffic goes through an SSH tunnel — that's the trust boundary.
Answering is **read-only** (`Read,Grep,Glob`) and each `claude` run is killed
after `--timeout` seconds, so a question can't edit files, run shell, or hang the
peer. Keep both hosts trusted; never expose the ports publicly.

---

## Tests

```bash
npm test     # no claude binary needed — a fake CLI exercises the engine
```

---

## Author

**Ajaykumar Savaliya** — Senior Software Engineer · **Python · Rust · AI/ML · Algo
Trading** · 8+ years building production systems.

🔗 [www.linkedin.com/in/ajay-okdev](https://www.linkedin.com/in/ajay-okdev) ·
*Open to Senior / Quant / AI Engineer roles.* A ⭐ is appreciated if this is useful.

---

## License

[MIT](LICENSE) © Ajaykumar Savaliya.
