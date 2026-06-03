"""claude-bridge broker.

A peer-agnostic FastAPI switchboard. It never branches on a peer's name or tech
stack: ``sender``/``target`` are opaque strings and a peer self-registers the
first time it asks or polls.

Live routing (who is waiting for what) is held in memory with asyncio queues and
events; everything durable (message log, answers, shared data, attachment
metadata, peer heartbeats) goes through :mod:`db`. Attachment *bytes* live on
disk under ``attachments_dir``.

Security: binds to ``127.0.0.1`` only. There is no application-level auth — the
localhost bind plus an SSH tunnel for cross-machine access is the trust boundary.
Do not expose the port on a public interface.

Run directly (``python broker.py``) or via ``uvicorn`` against
:func:`create_app`.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import logging
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from config import Config, AttachmentError, validate_image
from dashboard import DASHBOARD_HTML
from db import Database, SqliteDatabase

logger = logging.getLogger("claude_bridge.broker")

# Wait windows are clamped so a caller cannot pin a connection open forever.
MAX_WAIT_SECONDS = 60


# --------------------------------------------------------------------------- #
# In-memory routing state (per app instance)
# --------------------------------------------------------------------------- #

class BrokerState:
    """Mutable per-app state. asyncio is single-threaded, so plain dict access
    between awaits is race-free; we only ever create-or-get these structures."""

    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        self._inboxes: dict[str, asyncio.Queue[str]] = {}
        self._answer_events: dict[str, asyncio.Event] = {}

    def inbox_for(self, peer: str) -> asyncio.Queue[str]:
        queue = self._inboxes.get(peer)
        if queue is None:
            queue = asyncio.Queue()
            self._inboxes[peer] = queue
        return queue

    def answer_event(self, request_id: str) -> asyncio.Event:
        event = self._answer_events.get(request_id)
        if event is None:
            event = asyncio.Event()
            self._answer_events[request_id] = event
        return event

    def discard_event(self, request_id: str) -> None:
        """Drop the event once it is no longer needed. Safe because the answer is
        durable in the DB and any already-suspended waiter holds its own
        reference to the Event object."""
        self._answer_events.pop(request_id, None)

    def alive_window_seconds(self) -> int:
        # A peer counts as alive if it polled within roughly two poll windows.
        return self.config.poll_wait_seconds * 2 + 10


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #

class AskBody(BaseModel):
    sender: str
    target: str
    question: str
    blocking: bool = True
    kind: str = "request"  # "request" (answer expected) | "note" (fire-and-forget)
    attachment_ids: list[str] = Field(default_factory=list)


class AnswerBody(BaseModel):
    request_id: str
    answer: str
    attachment_ids: list[str] = Field(default_factory=list)
    is_error: bool = False
    cost_usd: float | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class SharedBody(BaseModel):
    key: str
    value: str
    description: str = ""
    peer: str = ""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _check_name(value: str, field: str, max_len: int) -> None:
    """Bound an opaque peer/key name so a caller can't create giant inbox/peer
    keys or balloon memory with absurd identifiers."""
    if not value or len(value) > max_len:
        raise HTTPException(
            status_code=422,
            detail=f"{field} must be 1..{max_len} characters",
        )


# --------------------------------------------------------------------------- #
# App factory
# --------------------------------------------------------------------------- #

def create_app(config: Config, db: Database | None = None) -> FastAPI:
    store: Database = db if db is not None else SqliteDatabase(config.db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        config.attachments_dir.mkdir(parents=True, exist_ok=True)
        # Re-fill inboxes from any requests that still need answering (queued, or
        # delivered to a responder that then died), so a restart does not silently
        # drop pending questions.
        requeued = 0
        for msg in store.recoverable_requests():
            state.inbox_for(msg.target).put_nowait(msg.request_id)
            requeued += 1
        if requeued:
            logger.info("re-queued %d pending request(s) on startup", requeued)

        sweep_task: asyncio.Task | None = None
        if config.retention_days > 0:
            sweep_task = asyncio.ensure_future(_retention_loop(config, store))
        try:
            yield
        finally:
            if sweep_task is not None:
                sweep_task.cancel()
            store.close()

    app = FastAPI(title="claude-bridge broker", lifespan=lifespan)
    state = BrokerState(config, store)
    app.state.bridge = state

    @app.middleware("http")
    async def limit_body_size(request: Request, call_next):
        """Reject oversized POST bodies via Content-Length before buffering them,
        so a client can't spike the single-process broker's memory."""
        if request.method in ("POST", "PUT", "PATCH"):
            cl = request.headers.get("content-length")
            if cl is not None:
                try:
                    if int(cl) > config.max_request_bytes:
                        return JSONResponse(
                            {"detail": "request body too large"}, status_code=413
                        )
                except ValueError:
                    return JSONResponse(
                        {"detail": "invalid Content-Length"}, status_code=400
                    )
        return await call_next(request)

    def clamp_wait(wait: float | None, default: int) -> float:
        if wait is None:
            return float(default)
        return float(max(0, min(wait, MAX_WAIT_SECONDS)))

    # -- ask ------------------------------------------------------------- #
    @app.post("/ask")
    async def ask(body: AskBody) -> JSONResponse:
        if body.kind not in ("request", "note"):
            raise HTTPException(status_code=422, detail="kind must be 'request' or 'note'")
        _check_name(body.sender, "sender", config.max_name_length)
        _check_name(body.target, "target", config.max_name_length)
        if len(body.question.encode("utf-8")) > config.max_question_bytes:
            raise HTTPException(status_code=413, detail="question exceeds size limit")
        if len(body.attachment_ids) > config.max_images_per_message:
            raise HTTPException(
                status_code=413,
                detail=f"too many attachments (max {config.max_images_per_message})",
            )

        request_id = uuid.uuid4().hex
        store.touch_peer(body.sender)
        store.add_message(
            request_id=request_id,
            sender=body.sender,
            target=body.target,
            kind=body.kind,
            body=body.question,
            attachment_ids=body.attachment_ids,
            blocking=body.blocking,
        )
        # Pre-create the event only for requests (something will wait on it), so a
        # fire-and-forget note never leaves a never-awaited event behind.
        if body.kind == "request":
            state.answer_event(request_id)
        state.inbox_for(body.target).put_nowait(request_id)

        if not body.blocking:
            return JSONResponse(
                {"request_id": request_id, "status": "queued"}, status_code=202
            )

        answer = await _await_answer(state, request_id, clamp_wait(None, config.ask_wait_seconds))
        if answer is None:
            return JSONResponse(
                {"request_id": request_id, "status": "pending"}, status_code=202
            )
        return JSONResponse({"request_id": request_id, "status": "answered", **answer})

    # -- poll ------------------------------------------------------------ #
    @app.get("/poll")
    async def poll(peer: str, wait: float | None = None) -> Response:
        _check_name(peer, "peer", config.max_name_length)
        store.touch_peer(peer)
        queue = state.inbox_for(peer)
        try:
            request_id = await asyncio.wait_for(
                queue.get(), timeout=clamp_wait(wait, config.poll_wait_seconds)
            )
        except asyncio.TimeoutError:
            return Response(status_code=204)

        msg = store.get_message(request_id)
        if msg is None:  # answered/removed underneath us — tell caller to re-poll
            return Response(status_code=204)
        store.set_message_status(request_id, "delivered")
        return JSONResponse(
            {
                "request_id": msg.request_id,
                "sender": msg.sender,
                "target": msg.target,
                "kind": msg.kind,
                "question": msg.body,
                "attachment_ids": msg.attachment_ids,
                "blocking": msg.blocking,
            }
        )

    # -- answer ---------------------------------------------------------- #
    @app.post("/answer")
    async def answer(body: AnswerBody) -> JSONResponse:
        msg = store.get_message(body.request_id)
        if msg is None:
            raise HTTPException(status_code=404, detail="unknown request_id")
        # First-writer-wins: never clobber an already-stored answer (a retrying
        # responder or stray client must not overwrite a delivered answer).
        if store.get_answer(body.request_id) is not None:
            return JSONResponse(
                {"ok": True, "request_id": body.request_id, "duplicate": True}
            )
        store.save_answer(
            request_id=body.request_id,
            answer=body.answer,
            attachment_ids=body.attachment_ids,
            is_error=body.is_error,
            cost_usd=body.cost_usd,
            meta=body.meta,
        )
        store.set_message_status(body.request_id, "answered")
        # Only requests have a waiter; notes are acked but never awaited.
        if msg.kind == "request":
            state.answer_event(body.request_id).set()
        return JSONResponse({"ok": True, "request_id": body.request_id})

    # -- reply (fetch / long-poll an answer) ----------------------------- #
    @app.get("/reply/{request_id}")
    async def reply(request_id: str, wait: float | None = None) -> JSONResponse:
        if store.get_message(request_id) is None:
            raise HTTPException(status_code=404, detail="unknown request_id")
        answer = await _await_answer(state, request_id, clamp_wait(wait, config.ask_wait_seconds))
        if answer is None:
            return JSONResponse({"request_id": request_id, "status": "pending"}, status_code=202)
        return JSONResponse({"request_id": request_id, "status": "answered", **answer})

    # -- attachments ----------------------------------------------------- #
    @app.post("/attachment")
    async def upload_attachment(request: Request) -> JSONResponse:
        content_type = request.headers.get("content-type", "")
        peer = ""
        original_name = ""
        try:
            if content_type.startswith("multipart/form-data"):
                form = await request.form()
                upload = form.get("file")
                if upload is None or not hasattr(upload, "read"):
                    raise HTTPException(status_code=422, detail="missing 'file' part")
                data = await upload.read()  # type: ignore[union-attr]
                original_name = getattr(upload, "filename", "") or ""
                peer = str(form.get("peer", "") or "")
            elif content_type.startswith("application/json"):
                payload = await request.json()
                raw = payload.get("data")
                if not isinstance(raw, str):
                    raise HTTPException(status_code=422, detail="missing base64 'data'")
                try:
                    data = base64.b64decode(raw, validate=True)
                except (binascii.Error, ValueError):
                    raise HTTPException(status_code=422, detail="invalid base64 'data'")
                original_name = str(payload.get("filename", "") or "")
                peer = str(payload.get("peer", "") or "")
            else:
                raise HTTPException(
                    status_code=415, detail="use multipart/form-data or application/json"
                )
        except HTTPException:
            raise
        except Exception as exc:  # malformed body must not wedge the broker
            logger.warning("attachment upload parse error: %s", exc)
            raise HTTPException(status_code=400, detail="could not parse upload") from exc

        # Authoritative validation: magic-byte sniff + size, ignoring any
        # caller-claimed content type.
        try:
            media_type = validate_image(data)
        except AttachmentError as exc:
            raise HTTPException(status_code=415, detail=str(exc)) from exc

        attachment_id = uuid.uuid4().hex
        sha256 = hashlib.sha256(data).hexdigest()
        path = config.attachments_dir / attachment_id
        try:
            path.write_bytes(data)
            store.save_attachment(
                attachment_id=attachment_id,
                media_type=media_type,
                size=len(data),
                sha256=sha256,
                path=str(path),
                original_name=original_name,
                peer=peer,
            )
        except Exception as exc:  # noqa: BLE001 - report, never wedge
            path.unlink(missing_ok=True)  # don't orphan a blob if the DB write failed
            logger.exception("failed to persist attachment")
            raise HTTPException(status_code=503, detail="could not store attachment") from exc
        return JSONResponse(
            {"attachment_id": attachment_id, "media_type": media_type, "size": len(data)}
        )

    @app.get("/attachment/{attachment_id}")
    async def download_attachment(attachment_id: str) -> Response:
        rec = store.get_attachment(attachment_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="unknown attachment")
        # Read from the stored path (the DB row is the source of truth), so blobs
        # can be relocated without breaking downloads.
        try:
            data = Path(rec.path).read_bytes()
        except FileNotFoundError:
            raise HTTPException(status_code=410, detail="attachment bytes are gone")
        return Response(content=data, media_type=rec.media_type)

    # -- shared data ----------------------------------------------------- #
    @app.post("/shared")
    async def put_shared(body: SharedBody) -> JSONResponse:
        if len(body.value.encode("utf-8")) > config.max_shared_value_bytes:
            raise HTTPException(status_code=413, detail="shared value exceeds size limit")
        rec = store.put_shared(
            key=body.key, value=body.value, description=body.description, peer=body.peer
        )
        return JSONResponse({"key": rec.key, "size": rec.size, "updated_at": rec.updated_at})

    @app.get("/shared/{key}")
    async def get_shared(key: str) -> JSONResponse:
        rec = store.get_shared(key)
        if rec is None:
            raise HTTPException(status_code=404, detail="unknown key")
        return JSONResponse(
            {
                "key": rec.key,
                "value": rec.value,
                "description": rec.description,
                "peer": rec.peer,
                "size": rec.size,
                "updated_at": rec.updated_at,
            }
        )

    @app.get("/shared")
    async def list_shared() -> JSONResponse:
        items = [
            {
                "key": r.key,
                "description": r.description,
                "size": r.size,
                "peer": r.peer,
                "updated_at": r.updated_at,
            }
            for r in store.list_shared()
        ]
        return JSONResponse({"items": items})

    # -- peers / health -------------------------------------------------- #
    def _peer_view() -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        window = state.alive_window_seconds()
        out: list[dict[str, Any]] = []
        for p in store.list_peers():
            try:
                last = datetime.fromisoformat(p.last_seen)
                alive = (now - last).total_seconds() <= window
            except ValueError:
                alive = False
            out.append({"name": p.name, "last_seen": p.last_seen, "alive": alive})
        return out

    @app.get("/peers")
    async def peers() -> JSONResponse:
        return JSONResponse({"peers": _peer_view()})

    @app.get("/metrics")
    async def metrics() -> JSONResponse:
        stats = store.answer_stats()
        stats["peers"] = _peer_view()
        return JSONResponse(stats)

    @app.get("/messages")
    async def messages(limit: int = 150, session_id: str | None = None) -> JSONResponse:
        capped = min(max(limit, 1), 500)
        rows = store.recent_messages(capped)
        if session_id:
            rows = [r for r in rows if r.get("session_id") == session_id]
        return JSONResponse({"messages": rows})

    @app.get("/ui")
    async def ui() -> HTMLResponse:
        return HTMLResponse(DASHBOARD_HTML)

    @app.get("/health")
    async def health() -> JSONResponse:
        # Liveness probe + which peers are connected.
        return JSONResponse(
            {
                "status": "ok",
                "time": datetime.now(timezone.utc).isoformat(),
                "peers": _peer_view(),
            }
        )

    return app


async def _await_answer(
    state: BrokerState, request_id: str, wait: float
) -> dict[str, Any] | None:
    """Return the answer payload for ``request_id`` or ``None`` if it does not
    arrive within ``wait`` seconds. Checks the durable store first so an answer
    that landed before the caller started waiting is returned immediately."""
    event = state.answer_event(request_id)
    existing = state.db.get_answer(request_id)
    if existing is not None:
        state.discard_event(request_id)
        return _answer_payload(existing)
    try:
        await asyncio.wait_for(event.wait(), timeout=wait)
    except asyncio.TimeoutError:
        # Reclaim the event on timeout too — a later /answer or /reply re-creates
        # it on demand (get-or-create) and the durable answer is always re-read.
        state.discard_event(request_id)
        return None
    finally:
        if event.is_set():
            state.discard_event(request_id)
    answer = state.db.get_answer(request_id)
    return _answer_payload(answer) if answer is not None else None


def _answer_payload(answer: Any) -> dict[str, Any]:
    return {
        "answer": answer.answer,
        "attachment_ids": answer.attachment_ids,
        "is_error": answer.is_error,
        "cost_usd": answer.cost_usd,
        "meta": answer.meta,
    }


async def _retention_loop(config: Config, store: Database) -> None:
    """Periodically purge answered messages/answers and attachment blobs older
    than ``retention_days``. Disabled when ``retention_days == 0``."""
    while True:
        try:
            await asyncio.sleep(config.retention_sweep_seconds)
            cutoff = (
                datetime.now(timezone.utc) - timedelta(days=config.retention_days)
            ).isoformat()
            deleted_paths = store.purge_before(cutoff)
            for p in deleted_paths:
                try:
                    Path(p).unlink(missing_ok=True)
                except OSError:  # pragma: no cover - best effort
                    pass
            if deleted_paths:
                logger.info("retention sweep removed %d attachment blob(s)", len(deleted_paths))
        except asyncio.CancelledError:  # pragma: no cover - shutdown
            raise
        except Exception:  # noqa: BLE001 - a bad sweep must not kill the broker
            logger.exception("retention sweep failed")


def main() -> None:
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = Config.from_env()
    app = create_app(config)
    logger.info("starting broker on %s:%d", config.broker_host, config.broker_port)
    uvicorn.run(app, host=config.broker_host, port=config.broker_port, log_level="info")


if __name__ == "__main__":
    main()
