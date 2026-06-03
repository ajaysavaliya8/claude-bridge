<!--
Paste this block into EACH peer project's own CLAUDE.md (one per project that has
the `bridge` MCP server registered). It is intentionally generic — it works for
any stack and any peer name. The only thing to customize is the peer list at the
bottom if you want to name them explicitly.
-->

## Talking to other peers (claude-bridge)

This project is one **peer** in a `claude-bridge` network. Other peers are
separate projects (possibly other stacks, on other machines) that this project
depends on or that depend on it. You have `bridge` MCP tools to talk to them.

**When your work depends on a fact that lives in ANOTHER peer's project — a route
path, an HTTP method, a JSON field name or type, a response/status shape, an auth
flow, an enum value, a schema, a config key — do NOT guess and do NOT infer it
from this project's code. Ask the owning peer with `ask_peer`.** The answer comes
from a process with read access to that project, so treat it as authoritative and
make your code match it.

- `ask_peer(question, target=None, image_paths=[])` — ask and block for a direct
  answer. Be specific. Quote the exact symbol/endpoint you're unsure about.
- Attach a screenshot or diagram with `image_paths=["/abs/path.png"]` when a
  picture helps — e.g. "does my rendered screen match what this endpoint
  returns?" The peer genuinely sees the image.
- Pass `target="<peer-name>"` to choose who to ask. If there are only two peers,
  you can omit it (a default is configured). Use `list_peers()` to see who's
  online when there are more than two.
- `tell_peer(message, target=None, image_paths=[])` — send a one-way heads-up
  (e.g. "I renamed this field") with no answer expected.
- `share_data` / `get_shared_data` / `list_shared_data` — pass large text
  payloads (schema dumps, tables, plans) out of band instead of pasting them into
  a message.
- `peer_status(target)` — check whether a peer is online before relying on it.

Good habit: before implementing anything against another peer's contract, ask
first, then code to the answer. A 10-second question prevents a class of
integration bugs (wrong field names, wrong casing, wrong response shape).
