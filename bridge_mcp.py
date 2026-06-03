"""Bridge MCP server — one per peer, attached to that peer's interactive Claude.

Exposes the bridge tools (ask_peer, tell_peer, list_peers, share_data, ...) over
stdio. Each tool is a thin async client to the broker.

IMPORTANT (stdio gotcha): a stdio MCP server must keep **stdout** clean — it
carries the JSON-RPC stream. All logging therefore goes to **stderr**. Never
``print()`` to stdout from here.

Configured entirely by environment variables (see .env.example):
``PEER_SELF`` (required), ``DEFAULT_TARGET`` (optional), ``BROKER_URL``.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import shutil
import sys
import tempfile
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

from broker_client import BrokerClient, BrokerError
from config import AttachmentError, Config, validate_image

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,  # keep stdout clean for JSON-RPC
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("claude_bridge.mcp")

CFG = Config.from_env()
SELF = CFG.require_peer_self()

mcp = FastMCP("bridge")

_EXT_FOR_MEDIA = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
_returned_dir: Path | None = None


def _client() -> BrokerClient:
    return BrokerClient(CFG.broker_url, default_timeout=CFG.http_timeout_seconds)


def _resolve_target(target: str | None) -> str:
    resolved = target or CFG.default_target
    if not resolved:
        raise ValueError(
            "no target peer given and DEFAULT_TARGET is not set — "
            "pass target=<peer name> (use list_peers to see options)"
        )
    return resolved


def _returned_images_dir() -> Path:
    global _returned_dir
    if _returned_dir is None:
        _returned_dir = Path(tempfile.mkdtemp(prefix="claude-bridge-returned-"))
    return _returned_dir


@atexit.register
def _cleanup_returned_dir() -> None:
    if _returned_dir is not None:
        shutil.rmtree(_returned_dir, ignore_errors=True)


async def _upload_images(client: BrokerClient, image_paths: list[str]) -> list[str]:
    """Validate and upload local image files; return attachment ids.

    Raises ValueError with a readable message on a missing/invalid/oversize file
    so the tool can surface it to the model.
    """
    if len(image_paths) > CFG.max_images_per_message:
        raise ValueError(
            f"too many images ({len(image_paths)}); max is {CFG.max_images_per_message}"
        )
    ids: list[str] = []
    for raw in image_paths:
        path = Path(raw).expanduser()
        try:
            data = path.read_bytes()
        except FileNotFoundError as exc:
            raise ValueError(f"image not found: {raw}") from exc
        except OSError as exc:
            raise ValueError(f"could not read image {raw}: {exc}") from exc
        try:
            validate_image(data)  # fail fast client-side; broker re-checks too
        except AttachmentError as exc:
            raise ValueError(f"{raw}: {exc}") from exc
        result = await client.upload_attachment(data, peer=SELF, filename=path.name)
        ids.append(result["attachment_id"])
    return ids


async def _save_returned_images(client: BrokerClient, attachment_ids: list[str]) -> list[str]:
    """Download images the peer attached to its answer and return local paths.

    Best-effort: a download or disk-write failure here must never discard an
    answer the caller already received, so every step is guarded.
    """
    saved: list[str] = []
    try:
        out_dir = _returned_images_dir()
    except OSError as exc:  # mkdtemp failed (disk full, perms) — skip saving
        logger.warning("could not create returned-images dir: %s", exc)
        return saved
    for aid in attachment_ids:
        try:
            data, media_type = await client.download_attachment(aid)
        except (BrokerError, httpx.HTTPError) as exc:
            # Transport errors raise raw httpx exceptions (before any BrokerError);
            # a failed bonus-image download must never discard the answer we have.
            logger.warning("could not download returned image %s: %s", aid, exc)
            continue
        dest = out_dir / f"{aid}{_EXT_FOR_MEDIA.get(media_type, '')}"
        try:
            dest.write_bytes(data)
        except OSError as exc:
            logger.warning("could not save returned image %s: %s", aid, exc)
            continue
        saved.append(str(dest))
    return saved


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #

@mcp.tool()
async def ask_peer(
    question: str, target: str | None = None, image_paths: list[str] | None = None
) -> str:
    """Ask another peer's project an authoritative question and get a direct answer.

    Use this whenever your work depends on a fact that lives in ANOTHER peer's
    project — a route path, JSON field name, type, response shape, auth flow,
    schema, config key, etc. — instead of guessing. The answer comes from a
    process with read access to that project, so treat it as authoritative and
    correct your own work to match it.

    Attach screenshots or diagrams via ``image_paths`` (local file paths) when an
    image helps — e.g. "do these fields match what this endpoint returns?" with a
    screenshot of your rendered screen. The peer genuinely sees the image.

    Args:
        question: The question to ask, as specific as possible.
        target: Name of the peer to ask. Omit to use the configured default peer.
        image_paths: Optional local image file paths (JPEG/PNG/GIF/WebP, <=5 MB each).

    Returns:
        The peer's direct answer. If the peer attached images, their saved local
        paths are listed so you can open them yourself.
    """
    try:
        dest = _resolve_target(target)
    except ValueError as exc:
        return f"Error: {exc}"

    async with _client() as client:
        try:
            attachment_ids = await _upload_images(client, image_paths or [])
        except ValueError as exc:
            return f"Error: {exc}"

        try:
            resp = await client.ask(
                sender=SELF,
                target=dest,
                question=question,
                blocking=True,
                attachment_ids=attachment_ids,
                wait=CFG.ask_wait_seconds,
            )
            request_id = resp.get("request_id", "")
            # Re-poll /reply until answered or the overall deadline elapses. Each
            # call is bounded, but the whole ask is one MCP tool call — keep the
            # server's per-call timeout (.mcp.json) above ASK_TIMEOUT_SECONDS.
            loop = asyncio.get_running_loop()
            deadline = loop.time() + CFG.ask_timeout_seconds
            while resp.get("status") == "pending":
                if loop.time() >= deadline:
                    return (
                        f"No answer from '{dest}' within {CFG.ask_timeout_seconds}s. "
                        f"The peer's responder may be down (check peer_status). "
                        f"request_id={request_id}"
                    )
                resp = await client.reply(request_id, wait=CFG.ask_wait_seconds)
        except BrokerError as exc:
            return f"Error talking to broker: {exc}"

        answer = str(resp.get("answer", "")).strip()
        saved = await _save_returned_images(client, resp.get("attachment_ids", []) or [])

    prefix = ""
    if resp.get("is_error"):
        prefix = f"⚠️ Peer '{dest}' reported it could not answer:\n"
    if saved:
        listing = "\n".join(f"- {p}" for p in saved)
        answer += f"\n\n[Returned images saved locally — open these directly:]\n{listing}"
    return prefix + (answer or "(empty answer)")


@mcp.tool()
async def tell_peer(
    message: str, target: str | None = None, image_paths: list[str] | None = None
) -> str:
    """Send a one-way note to another peer (fire-and-forget; no answer expected).

    Use this to inform a peer of something it should know — a decision, a change,
    a heads-up — optionally with images. The note is delivered to that peer's
    resident expert and folded into its context. For a question you need answered,
    use ask_peer instead.

    Args:
        message: The note text.
        target: Name of the peer to notify. Omit to use the configured default.
        image_paths: Optional local image file paths (JPEG/PNG/GIF/WebP, <=5 MB each).

    Returns:
        A confirmation including the request id.
    """
    try:
        dest = _resolve_target(target)
    except ValueError as exc:
        return f"Error: {exc}"

    async with _client() as client:
        try:
            attachment_ids = await _upload_images(client, image_paths or [])
            resp = await client.ask(
                sender=SELF,
                target=dest,
                question=message,
                blocking=False,
                kind="note",
                attachment_ids=attachment_ids,
            )
        except ValueError as exc:
            return f"Error: {exc}"
        except BrokerError as exc:
            return f"Error talking to broker: {exc}"
    return f"Note delivered to '{dest}' (request_id={resp.get('request_id', '')})."


@mcp.tool()
async def list_peers() -> str:
    """List peers currently known to the broker and whether each is alive.

    Use this to choose a ``target`` when more than two peers exist.
    """
    async with _client() as client:
        try:
            peers = await client.peers()
        except BrokerError as exc:
            return f"Error talking to broker: {exc}"
    if not peers:
        return "No peers known to the broker yet."
    lines = [
        f"- {p.get('name', 'unknown')}: {'alive' if p.get('alive') else 'offline'} (last seen {p.get('last_seen')})"
        for p in peers
    ]
    return "Known peers:\n" + "\n".join(lines)


@mcp.tool()
async def share_data(key: str, value: str, description: str = "") -> str:
    """Store a large TEXT payload (schema dump, table, plan) under a key.

    Use this instead of cramming a big payload into a message; peers fetch it with
    get_shared_data(key).

    Args:
        key: Identifier to retrieve the payload by.
        value: The text payload.
        description: Short human description of what this is.
    """
    async with _client() as client:
        try:
            resp = await client.put_shared(
                key=key, value=value, description=description, peer=SELF
            )
        except BrokerError as exc:
            return f"Error talking to broker: {exc}"
    return f"Stored shared data '{key}' ({resp.get('size')} bytes)."


@mcp.tool()
async def get_shared_data(key: str) -> str:
    """Retrieve a shared text payload previously stored under ``key``."""
    async with _client() as client:
        try:
            rec = await client.get_shared(key)
        except BrokerError as exc:
            return f"Error talking to broker: {exc}"
    if rec is None:
        return f"No shared data under key '{key}'."
    return str(rec.get("value", ""))


@mcp.tool()
async def list_shared_data() -> str:
    """List shared-data keys with their sizes and descriptions (not the values)."""
    async with _client() as client:
        try:
            items = await client.list_shared()
        except BrokerError as exc:
            return f"Error talking to broker: {exc}"
    if not items:
        return "No shared data stored yet."
    lines = [
        f"- {i['key']} ({i.get('size')} bytes) — {i.get('description') or 'no description'} "
        f"[by {i.get('peer') or 'unknown'}]"
        for i in items
    ]
    return "Shared data:\n" + "\n".join(lines)


@mcp.tool()
async def peer_status(target: str | None = None) -> str:
    """Report whether a peer's responder is alive (recent poll/heartbeat).

    Args:
        target: Peer name to check. Omit to report on the default peer, or on all
            peers if no default is configured.
    """
    async with _client() as client:
        try:
            peers = await client.peers()
        except BrokerError as exc:
            return f"Error talking to broker: {exc}"

    by_name = {p.get("name", ""): p for p in peers}
    wanted = target or CFG.default_target
    if wanted:
        p = by_name.get(wanted)
        if p is None:
            return f"Peer '{wanted}' is unknown to the broker (never connected)."
        return (
            f"Peer '{wanted}' is {'alive' if p.get('alive') else 'offline'} "
            f"(last seen {p.get('last_seen')})."
        )
    if not peers:
        return "No peers known to the broker yet."
    return "\n".join(
        f"- {p.get('name', 'unknown')}: {'alive' if p.get('alive') else 'offline'} (last seen {p.get('last_seen')})"
        for p in peers
    )


def main() -> None:
    logger.info("bridge MCP server for peer '%s' -> %s", SELF, CFG.broker_url)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
