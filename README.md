# claude-bridge

**Let separate Claude Code sessions talk to each other.** A lightweight,
self-hostable bridge that lets AI coding agents working in different projects —
on different machines — ask each other authoritative questions and get answers
read straight from the other project's real source code.

When your frontend depends on your backend's contract — a route, a JSON field
name, a type, a status code — your frontend's Claude Code session can just **ask
the backend's session** instead of guessing. The answer comes from a process that
actually read the backend code, so it's authoritative. Questions can carry
screenshots, too.

> Keywords: Claude Code · Model Context Protocol (MCP) · MCP server ·
> agent-to-agent communication · cross-project / multi-repo AI · headless Claude ·
> vision / screenshots · Python · FastAPI · SQLite · self-hosted.

---

## Why I built this

I kept hitting the same wall working on two repos at once: a frontend on my laptop
and a backend on a VPS. While writing frontend code I'd guess at the backend's
response shape — a field name, whether `id` was a string or an int, which status
codes an endpoint returned — get it subtly wrong, and only find out at runtime. I
wanted the frontend's session to just **ask** the backend's session for the real
contract, and get an answer from something that had actually read the backend code.
That's what this is. I run it across my own projects daily.

---

## Features

- **Two-way Q&A between Claude Code sessions** — any peer can ask any other.
- **Authoritative answers** — produced by headless Claude reading the *real*
  project, not guessing.
- **Stack-agnostic** — a peer is just a name + a project directory + a machine.
  Same code for React, FastAPI, Go, Android, anything. No per-stack logic.
- **Images / screenshots** — attach a screenshot to a question; it's read via the
  Anthropic vision API (reliably, not hallucinated).
- **Live dashboard** at `/ui` — watch every question and answer stream by, with
  cost and a per-session "chat id" filter.
- **Read-only & safe** — answering Claude is restricted to `Read,Grep,Glob` and
  time-bounded, so a question can never edit files or run shell.
- **Tiny footprint** — Python, FastAPI, SQLite; no token or account to configure.

---

## How it works

Two constraints shape the design: MCP servers can't see each other (so a shared
**broker** routes messages), and a tool can deliver a question but can't make the
other session *think* (so the answering side runs **headless Claude** on its own
project, autonomously).

Each peer runs two small processes — a **bridge MCP server** (the `ask_peer`
tools, attached to that peer's interactive Claude) and a **responder** (answers by
reading its project). One **broker** sits in the middle.

```
LOCAL — peer "frontend"                  VPS — peer "backend"
┌─────────────────────────┐            ┌─────────────────────────┐
│ Claude Code (interactive)│            │ Claude Code (interactive)│
│   └─ bridge MCP server ─┐│            │   └─ bridge MCP server ─┐│
│ responder ◀ reads proj  ││            │ responder ◀ reads proj  ││
└─────────────────────────┼┘            └─────────────────────────┼┘
   ssh tunnel :8765 ───────┴────────▶ broker (127.0.0.1:8765) ◀───┘
```

**📖 Full setup + usage: see [GUIDE.md](GUIDE.md).** It covers the implementation,
install, configuration, running, the tools, the dashboard, adding peers,
troubleshooting, and how to extend it.

---

## Quick start

Install on both machines (`python -m venv .venv` then `pip install -e ".[dev]"`),
create a tiny `.env` on each (`PEER_SELF`, `PROJECT_DIR`, `ANTHROPIC_API_KEY`),
then run — no token, no account:

```bash
# On the host both peers can reach (the broker + that peer's responder):
python broker.py          # the switchboard, binds 127.0.0.1:8765
python responder.py       # answers questions about this peer's project

# On the other peer's machine:
ssh -N -L 8765:127.0.0.1:8765 user@broker-host       # tunnel to the broker
python scripts/register_mcp.py <this-peer> <target>  # add the MCP tools (once)
```

Open a Claude Code session in the peer's project and call `ask_peer("…")`. Watch
it live at `http://127.0.0.1:8765/ui`. Step-by-step instructions are in
[GUIDE.md](GUIDE.md).

---

## Tools your Claude gets

`ask_peer` · `tell_peer` · `list_peers` · `peer_status` · `share_data` ·
`get_shared_data` · `list_shared_data`. Drop [peer-snippet.md](peer-snippet.md)
into each project's `CLAUDE.md` so its Claude uses them automatically.

---

## Security model

No application-level auth by design: the broker binds to `127.0.0.1` and
cross-machine access goes through an SSH tunnel — that's the trust boundary.
Anything that can reach the port can use the bridge, so keep both hosts trusted
and never expose it publicly. Answering is read-only and time-bounded.

---

## Tests

```bash
pip install -e ".[dev]"
pytest -q
```

No API key or `claude` binary needed to run the tests.

---

## Author

**Ajaykumar Savaliya** — Senior Software Engineer · **Python · Rust · AI/ML · Algo
Trading** · 8+ years building production systems.

🔗 **LinkedIn:** [www.linkedin.com/in/ajay-okdev](https://www.linkedin.com/in/ajay-okdev)

*Open to Senior / Quant / AI Engineer roles.* If this project is useful to you or
your team, a connection or a ⭐ on the repo is appreciated.

---

## License

[MIT](LICENSE) © Ajaykumar Savaliya.
