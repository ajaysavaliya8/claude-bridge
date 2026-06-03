<!--
Paste this block into EACH peer project's own CLAUDE.md (one per project that has
the `bridge` MCP server registered). It is intentionally generic — it works for
any stack and any peer name.
-->

## Talking to the other peer (claude-bridge)

This project is one **peer** in a `claude-bridge` setup. The other peer is a
separate project (possibly another stack, on another machine) that this project
depends on or that depends on it. You have `bridge` MCP tools to talk to it.

**When your work depends on a fact that lives in the OTHER peer's project — a
route path, an HTTP method, a JSON field name or type, a response/status shape,
an auth flow, an enum value, a schema, a config key — do NOT guess and do NOT
infer it from this project's code. Ask the owning peer with `ask_peer`.** The
answer comes from a process with read access to that project, so treat it as
authoritative and make your code match it.

- `ask_peer(question, target=None)` — ask and block for a direct answer. Be
  specific. Quote the exact symbol/endpoint you're unsure about.
- `tell_peer(message, target=None)` — send a one-way heads-up (e.g. "I renamed
  this field") with no answer expected.
- `peer_status(target=None)` — check whether the partner is online before relying
  on it.
- `list_peers()` — show who you can talk to (yourself and the partner).

Good habit: before implementing anything against the other peer's contract, ask
first, then code to the answer. A 10-second question prevents a class of
integration bugs (wrong field names, wrong casing, wrong response shape).
