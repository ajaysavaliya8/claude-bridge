# claude-bridge — full guide

One document: how it's built, and exactly how to set it up and use it. The
running example is a **frontend** on a local Windows machine talking to a
**backend** on a cloud VPS, but nothing is specific to those stacks.

---

## 1. What it is

claude-bridge lets two (or more) separate Claude Code sessions ask each other
**authoritative questions about their own projects**. While writing frontend code
that depends on the backend's contract — a route, a JSON field name, a type, a
status code — the frontend's session calls `ask_peer(...)` and gets a real answer
produced by reading the *actual* backend code, instead of guessing. Questions can
carry screenshots too.

It's **stack-agnostic**: a peer is just a *name*, a *project directory*, and the
*machine* it runs on. The same code serves React, FastAPI, Go, Android — anything.

---

## 2. How it works (the implementation)

### The two constraints that shape the design

1. **MCP servers can't see each other.** Each Claude Code session spawns its own
   MCP server over stdio; those processes are isolated. So a shared **broker**
   must sit in the middle and route messages.
2. **A tool can deliver a question but can't make the other session *think*.** So
   the answering side must work **autonomously** — it runs **headless Claude**
   (`claude -p`) against its own project, with no human at that terminal.

### The three processes

| Process | Runs | Does |
|---|---|---|
| **broker** | once, on a host both peers reach (the VPS) | localhost-only HTTP switchboard: routes questions → answers, stores the log, serves the dashboard |
| **responder** | one per peer | long-polls the broker; answers questions about *its* project by reading it with headless `claude` (read-only) |
| **bridge MCP server** | one per peer | gives that peer's *interactive* Claude the `ask_peer`/`tell_peer`/… tools |

```
LOCAL (Windows) — peer "frontend"          VPS (Linux) — peer "backend"
┌──────────────────────────────┐          ┌──────────────────────────────┐
│ Claude Code (interactive)     │          │ Claude Code (interactive)     │
│   └─ bridge MCP server ───┐   │          │   └─ bridge MCP server ───┐   │
│ responder ◀ reads project │   │          │ responder ◀ reads project │   │
└───────────────────────────┼───┘          └───────────────────────────┼───┘
   ssh tunnel :8765 ─────────┴──────────▶  broker (127.0.0.1:8765) ◀────┘
```

### What happens when you call `ask_peer`

1. The frontend's interactive Claude calls the MCP tool `ask_peer("…")`.
2. The MCP server `POST`s the question to the broker, which queues it for the
   target peer and holds the connection open (long-poll).
3. The backend's **responder** is long-polling `/poll`; it receives the question.
4. It runs `claude -p "<question>" --output-format json --allowedTools
   Read,Grep,Glob --max-turns 15 --resume <session>` **inside the backend project
   dir**. Claude reads the real code and produces an answer.
5. The responder `POST`s the answer back; the broker hands it to the waiting
   frontend tool call, which returns it to the frontend's Claude.
6. The exchange is logged and visible live at the dashboard (`/ui`).

The responder keeps a per-peer `--resume` **session id**, so context accumulates
across questions. That session id is what the dashboard calls the "chat id."

### Text vs. image questions (important)

- **Text questions** → headless `claude -p` (step 4 above).
- **Image questions** → the responder does **not** use headless Claude. In
  headless mode the Read tool *hallucinates* image contents (a known Claude Code
  bug). Instead the responder sends the image as a real base64 vision block to the
  **Anthropic Messages API** (`vision.py`), optionally after a quick text-only
  retrieval to ground the answer in the project. That's why a responder needs
  `ANTHROPIC_API_KEY`.

### Storage

SQLite (one file) holds the durable message log, answers, shared-data store, and
attachment metadata. Attachment *bytes* live on disk under a blob dir. Live
routing (who's waiting for what) is in-memory; answers are persisted, so a broker
restart re-delivers anything still pending.

### Security model (no auth — by design)

There is **no token or login**. The broker binds to `127.0.0.1` only, and
cross-machine access goes through an **SSH tunnel** — that's the trust boundary.
Anything that can reach `127.0.0.1:8765` (a process on the broker host, or anyone
on the tunnel) can use the bridge, so keep both hosts trusted and never expose the
port. Answering is **read-only** (`Read,Grep,Glob`) and each `claude` run is killed
after a timeout, so a question can't edit files, run shell, or hang the peer.

### File map

| File | Role |
|---|---|
| `broker.py` | the switchboard (FastAPI): `/ask /poll /answer /reply /attachment /shared /peers /metrics /messages /ui /health` |
| `bridge_mcp.py` | the MCP server and its tools |
| `responder.py` | the autonomous answerer (headless claude / vision) |
| `vision.py` | Anthropic Messages API call with image blocks |
| `broker_client.py` | shared async HTTP client |
| `prompts.py` | stack-neutral prompt templates |
| `dashboard.py` | the `/ui` live dashboard HTML |
| `db.py` | SQLite store behind a small abstraction |
| `config.py` | environment config + defaults |
| `scripts/register_mcp.py` | registers the MCP server with the right timeout |

---

## 3. Install (on **both** machines)

Requires Python 3.11+ and (on any machine that *answers*) the `claude` CLI
installed and authenticated.

```bash
# get the code (scp a git archive, or git clone once it's on GitHub), then:
cd claude-bridge
python -m venv .venv
# Linux:    source .venv/bin/activate
# Windows:  .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest -q                     # optional sanity check: all tests pass
```

To copy the code to the VPS without GitHub, from the repo on your local machine:
```powershell
git archive --format=tar.gz -o $env:TEMP\claude-bridge.tar.gz HEAD
scp $env:TEMP\claude-bridge.tar.gz <vps-user>@<vps-host>:~/
# on the VPS:  mkdir -p ~/claude-bridge && tar -xzf ~/claude-bridge.tar.gz -C ~/claude-bridge
```

---

## 4. Configure `.env` (tokenless)

Copy `.env.example` to `.env` on each machine. With no auth, the files are tiny —
everything else has sane defaults (`BROKER_URL` defaults to `http://127.0.0.1:8765`).

**VPS** (`~/claude-bridge/.env`) — runs the broker **and** the backend peer:
```ini
PEER_SELF=backend
PROJECT_DIR=/home/<vps-user>/backend-project
ANTHROPIC_API_KEY=sk-ant-...
```

**Local** (`...\claude-bridge\.env`) — the frontend peer:
```ini
PEER_SELF=frontend
DEFAULT_TARGET=backend
PROJECT_DIR=C:\path\to\frontend-project
ANTHROPIC_API_KEY=sk-ant-...
```

> Keep `BRIDGE_DB_PATH` / `BRIDGE_ATTACHMENTS_DIR` **out of a cloud-sync folder**
> (OneDrive/Dropbox) — SQLite WAL files corrupt there. Default is next to the code;
> override to e.g. `%LOCALAPPDATA%\claude-bridge` if the repo lives in OneDrive.

Useful optional knobs (all have defaults): `ALLOWED_TOOLS` (read-only set),
`MAX_TURNS`, `CLAUDE_TIMEOUT_SECONDS`, `ASK_TIMEOUT_SECONDS`, `VISION_MODEL`.

---

## 5. Run it

Each long-running process gets its own terminal/pane. The `set -a; . ./.env; set +a`
(bash) and the `Get-Content .env …` (PowerShell) lines just load `.env` into the
environment before running Python directly.

### On the VPS (use `tmux` so they survive your SSH session)

```bash
cd ~/claude-bridge && source .venv/bin/activate
set -a; . ./.env; set +a

python broker.py        # pane 1 — the switchboard (binds 127.0.0.1:8765)
python responder.py     # pane 2 — answers questions about the backend project
```
Check: `curl -s http://127.0.0.1:8765/health` → `{"status":"ok",...}`.

### On your local machine (PowerShell)

```powershell
# 1) tunnel — leave this open
ssh -N -L 8765:127.0.0.1:8765 <vps-user>@<vps-host>

# 2) register the tools (one-time)
cd C:\path\to\claude-bridge
Get-Content .env | ForEach-Object { if ($_ -match '^\s*([^#][^=]*)=(.*)$') { [Environment]::SetEnvironmentVariable($matches[1].Trim(), $matches[2].Trim()) } }
.\.venv\Scripts\python.exe scripts\register_mcp.py frontend backend
```
`register_mcp.py` runs `claude mcp add-json` with the right per-call `timeout`
(so a slow `ask_peer` isn't cut off). After it runs, **restart your Claude Code
session** so it loads the `bridge` server.

> Direct-Python equivalents (no wrappers): the broker is `python broker.py`, a
> responder is `python responder.py` (with `PEER_SELF`/`PROJECT_DIR` set), the
> tunnel is plain `ssh -N -L …`.

That's the whole thing: **broker + responder on the VPS, tunnel + register on
local** — about 5 commands, no token, no accounts.

---

## 6. Use it

### The zero-effort way
Paste the snippet from [peer-snippet.md](peer-snippet.md) into each project's own
`CLAUDE.md`. Then each Claude reaches for `ask_peer` on its own whenever it needs
a fact from the other project.

### The tools

| Tool | What it does |
|---|---|
| `ask_peer(question, target=None, image_paths=[])` | ask and block for an authoritative answer |
| `tell_peer(message, target=None, image_paths=[])` | one-way note, no answer expected |
| `list_peers()` | who's online (to pick a `target`) |
| `peer_status(target=None)` | is a peer's responder alive? |
| `share_data(key, value, description)` | stash a large text payload |
| `get_shared_data(key)` / `list_shared_data()` | fetch / list stashed payloads |

With two peers you can omit `target` (it uses `DEFAULT_TARGET`).

### Examples

In the **frontend** session:
```
ask_peer("What exact JSON shape does GET /users/{id} return? Field names and types.")
```
→ a real answer read from the backend code, e.g. *"snake_case: `display_name`,
`avatar_url` (nullable), `created_at` (ISO-8601); see app/schemas/user.py:14."*

With a screenshot (genuinely read via the vision API):
```
ask_peer("Does my rendered screen match what this endpoint returns? Flag mismatches.",
         image_paths=["C:/Users/you/Desktop/screen.png"])
```

Share a big payload instead of pasting it:
```
share_data("orders_openapi", "<...full schema...>", "Orders API v3")   # backend
get_shared_data("orders_openapi")                                       # frontend
```

### Watch it live
Open `http://127.0.0.1:8765/ui` (through the tunnel) — no login. You get the peer
list and a live feed of every question/answer with status, cost, and the claude
**session id**. Click a session id (or type it in the filter) to follow one thread.

---

## 7. Add a third peer (config only)

Say you add a `worker` service. No code changes:
1. Put the code + a `.env` (`PEER_SELF=worker`, `PROJECT_DIR=…`) on its machine.
2. Open a tunnel to the broker if it's remote.
3. Run `python responder.py`, and (if it should also *ask*) `python
   scripts/register_mcp.py worker` + open an interactive session there.

Now anyone can `ask_peer(target="worker", ...)` and `worker` can ask anyone. Use
`list_peers()` to discover who's online.

---

## 8. Keep it running

- **VPS:** run the broker + responder in `tmux`, or as `systemd` services
  (`WorkingDirectory=/…/claude-bridge`, `ExecStart=/…/.venv/bin/python broker.py`,
  `Restart=always`). They shut down cleanly on `SIGTERM`.
- **Local:** keep the tunnel up; for auto-reconnect use
  `ssh -N -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes -L 8765:127.0.0.1:8765 you@vps`.

---

## 9. Troubleshooting

| Symptom | Fix |
|---|---|
| `ask_peer` waits, then says the peer is offline | That peer's responder or the tunnel isn't running. Check `peer_status()` / `/health`, restart it. |
| `bridge` tools don't appear in Claude | Re-run `register_mcp.py`, then **restart** the Claude session. Check `claude mcp list`. |
| Responder error: `claude` not found | Set `CLAUDE_BIN` to the full path (on Windows it may be `claude.cmd`), or fix `PATH`. |
| Backend answers come back as errors | `claude` isn't authenticated on the VPS, or `ANTHROPIC_API_KEY` isn't set there. |
| Image answer looks generic/wrong | The answering responder needs a valid `ANTHROPIC_API_KEY` (image questions use the vision API). |
| `ask_peer` times out on genuinely slow answers | Raise `ASK_TIMEOUT_SECONDS` in `.env`, re-run `register_mcp.py` (it derives the MCP timeout from it). |
| Cross-machine connection refused | The broker binds `127.0.0.1` only — reach it through the SSH tunnel, never a public IP. |

---

## 10. Extending it

- **Swap SQLite → PostgreSQL:** implement `class PostgresDatabase(Database)` in
  `db.py` with the same methods (the SQL is portable), and select it in
  `create_app()`. Nothing else changes; attachment bytes already live on disk.
- **Add auth back (if you ever expose it):** add an API-key check in `broker.py`
  and an `Authorization` header in `broker_client.py`.
- **Widen what a peer can do:** `ALLOWED_TOOLS` defaults to read-only
  (`Read,Grep,Glob`). Widening it lets a question trigger more — do so only
  deliberately.
```
