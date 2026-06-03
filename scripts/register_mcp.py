#!/usr/bin/env python
"""Register this peer's bridge MCP server with Claude Code.

Crucially this sets a per-call ``timeout`` on the server entry that is ABOVE
``ASK_TIMEOUT_SECONDS`` — otherwise Claude Code's default tool timeout cuts off
the blocking ``ask_peer`` mid-wait and the (eventually produced) answer is
discarded. It uses ``claude mcp add-json`` via subprocess with an argv list, so
the JSON config is passed as one argument with no shell-quoting pitfalls on
either Windows or POSIX.

Usage:
    python scripts/register_mcp.py PEER_SELF [DEFAULT_TARGET] [--scope user] [--name bridge]

Reads BROKER_URL, ASK_TIMEOUT_SECONDS, PYTHON from the environment or a sibling
.env file.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

BRIDGE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env_path = BRIDGE_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        # Match POSIX `. ./.env` sourcing: strip one matching pair of surrounding
        # quotes so e.g. BROKER_URL="http://x" doesn't reach the config as `"http://x"`.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def _ask_timeout_seconds() -> int:
    raw = os.environ.get("ASK_TIMEOUT_SECONDS") or "300"
    try:
        return int(raw)
    except ValueError:
        print(
            f"warning: ASK_TIMEOUT_SECONDS={raw!r} is not an integer; using 300",
            file=sys.stderr,
        )
        return 300


def build_invocation(
    *,
    peer_self: str,
    default_target: str,
    broker_url: str,
    python_bin: str,
    ask_timeout: int,
    name: str,
    scope: str,
) -> tuple[list[str], int]:
    """Pure builder (unit-testable): returns the `claude mcp add-json` argv and the
    per-call timeout in ms, derived from the ask deadline."""
    timeout_ms = (ask_timeout + 60) * 1000  # comfortably above the ask deadline
    env: dict[str, str] = {
        "PEER_SELF": peer_self,
        "BROKER_URL": broker_url,
    }
    if default_target:
        env["DEFAULT_TARGET"] = default_target
    config = {
        "command": python_bin,
        "args": [str(BRIDGE_DIR / "bridge_mcp.py")],
        "env": env,
        "timeout": timeout_ms,
    }
    claude = shutil.which("claude") or "claude"
    cmd = [claude, "mcp", "add-json", name, json.dumps(config), "--scope", scope]
    return cmd, timeout_ms


def main() -> int:
    _load_dotenv()
    ap = argparse.ArgumentParser(description="Register the bridge MCP server with a sane timeout.")
    ap.add_argument("peer_self")
    ap.add_argument("default_target", nargs="?", default="")
    ap.add_argument("--scope", default=os.environ.get("MCP_SCOPE", "user"))
    ap.add_argument("--name", default="bridge")
    args = ap.parse_args()

    cmd, timeout_ms = build_invocation(
        peer_self=args.peer_self,
        default_target=args.default_target,
        broker_url=os.environ.get("BROKER_URL", "http://127.0.0.1:8765"),
        python_bin=os.environ.get("PYTHON") or sys.executable,
        ask_timeout=_ask_timeout_seconds(),
        name=args.name,
        scope=args.scope,
    )
    print(
        f"+ {cmd[0]} mcp add-json {args.name} <config> --scope {args.scope} "
        f"(per-call timeout {timeout_ms} ms)",
        file=sys.stderr,
    )
    try:
        return subprocess.run(cmd).returncode
    except FileNotFoundError:
        print(
            "error: 'claude' CLI not found on PATH. Install Claude Code or set it on PATH.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
