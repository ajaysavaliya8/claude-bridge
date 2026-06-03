"""Persistence abstraction for claude-bridge.

The live routing of questions and answers is held in memory by the broker
(asyncio queues and futures). This module owns everything that must outlive a
single request: the durable message log, the shared-data key/value store,
attachment metadata, and peer heartbeats.

``Database`` is an abstract base class so the default :class:`SqliteDatabase`
(single file, zero infra) can later be swapped for a PostgreSQL implementation
without touching the broker: implement the same methods against ``psycopg`` and
the SQL here is deliberately portable (no SQLite-only syntax in the contract).

Attachment *bytes* live on disk (see :mod:`config`); only their metadata is
stored here.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    """Current UTC time as an ISO-8601 string (stored as TEXT)."""
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Typed records returned by the store
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class MessageRecord:
    request_id: str
    sender: str
    target: str
    kind: str               # "request" (answer expected) | "note" (fire-and-forget)
    body: str
    attachment_ids: list[str]
    blocking: bool
    status: str             # "queued" | "delivered" | "answered" | "failed"
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class AnswerRecord:
    request_id: str
    answer: str
    attachment_ids: list[str]
    is_error: bool
    cost_usd: float | None
    meta: dict[str, Any]
    created_at: str


@dataclass(frozen=True)
class SharedDataRecord:
    key: str
    value: str
    description: str
    peer: str
    size: int
    updated_at: str


@dataclass(frozen=True)
class AttachmentRecord:
    attachment_id: str
    media_type: str
    size: int
    sha256: str
    path: str
    original_name: str
    peer: str
    created_at: str


@dataclass(frozen=True)
class PeerRecord:
    name: str
    first_seen: str
    last_seen: str


# --------------------------------------------------------------------------- #
# Abstract contract
# --------------------------------------------------------------------------- #

class Database(ABC):
    """Storage contract shared by SQLite (default) and any future backend."""

    # -- messages --------------------------------------------------------- #
    @abstractmethod
    def add_message(
        self,
        *,
        request_id: str,
        sender: str,
        target: str,
        kind: str,
        body: str,
        attachment_ids: list[str],
        blocking: bool,
    ) -> MessageRecord: ...

    @abstractmethod
    def get_message(self, request_id: str) -> MessageRecord | None: ...

    @abstractmethod
    def set_message_status(self, request_id: str, status: str) -> None: ...

    @abstractmethod
    def recoverable_requests(self) -> list[MessageRecord]:
        """Requests that still need answering — used to re-fill inboxes on broker
        startup so a restart does not silently drop pending questions. Includes
        both 'queued' (never polled) and 'delivered' (polled but the responder
        died/hung before answering) requests that have no answer row yet."""

    @abstractmethod
    def answer_stats(self) -> dict[str, Any]:
        """Aggregate answer counts/cost for observability (/metrics)."""

    @abstractmethod
    def recent_messages(self, limit: int = 100) -> list[dict[str, Any]]:
        """The message log joined with answers, newest first, for the dashboard.
        Each row carries the claude session id (pulled from the answer metadata)
        so the UI can group/filter by it."""

    @abstractmethod
    def purge_before(self, cutoff_iso: str) -> list[str]:
        """Delete answered messages + their answers, and attachments, older than
        ``cutoff_iso``. Returns the on-disk paths of deleted attachment blobs so
        the caller can unlink them. Used by the broker's retention sweep."""

    # -- answers ---------------------------------------------------------- #
    @abstractmethod
    def save_answer(
        self,
        *,
        request_id: str,
        answer: str,
        attachment_ids: list[str],
        is_error: bool,
        cost_usd: float | None,
        meta: dict[str, Any],
    ) -> AnswerRecord: ...

    @abstractmethod
    def get_answer(self, request_id: str) -> AnswerRecord | None: ...

    # -- shared data ------------------------------------------------------ #
    @abstractmethod
    def put_shared(self, *, key: str, value: str, description: str, peer: str) -> SharedDataRecord: ...

    @abstractmethod
    def get_shared(self, key: str) -> SharedDataRecord | None: ...

    @abstractmethod
    def list_shared(self) -> list[SharedDataRecord]: ...

    # -- attachments ------------------------------------------------------ #
    @abstractmethod
    def save_attachment(
        self,
        *,
        attachment_id: str,
        media_type: str,
        size: int,
        sha256: str,
        path: str,
        original_name: str,
        peer: str,
    ) -> AttachmentRecord: ...

    @abstractmethod
    def get_attachment(self, attachment_id: str) -> AttachmentRecord | None: ...

    # -- peers ------------------------------------------------------------ #
    @abstractmethod
    def touch_peer(self, name: str) -> None:
        """Record that ``name`` was just seen (self-registration + heartbeat)."""

    @abstractmethod
    def get_peer(self, name: str) -> PeerRecord | None: ...

    @abstractmethod
    def list_peers(self) -> list[PeerRecord]: ...

    @abstractmethod
    def close(self) -> None: ...


# --------------------------------------------------------------------------- #
# SQLite implementation
# --------------------------------------------------------------------------- #

class SqliteDatabase(Database):
    """Default single-file store backed by stdlib :mod:`sqlite3`.

    A single connection is shared across the event loop with ``check_same_thread
    =False`` and guarded by a re-entrant lock. Local SQLite calls are sub-
    millisecond, so running them inline from async handlers does not meaningfully
    block the loop.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        # No FK constraints are declared; referential integrity is enforced at
        # the application layer, so the foreign_keys pragma would be a no-op.
        self._create_schema()

    # -- schema ----------------------------------------------------------- #
    def _create_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    request_id     TEXT PRIMARY KEY,
                    sender         TEXT NOT NULL,
                    target         TEXT NOT NULL,
                    kind           TEXT NOT NULL,
                    body           TEXT NOT NULL,
                    attachment_ids TEXT NOT NULL DEFAULT '[]',
                    blocking       INTEGER NOT NULL DEFAULT 0,
                    status         TEXT NOT NULL DEFAULT 'queued',
                    created_at     TEXT NOT NULL,
                    updated_at     TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_target_status
                    ON messages (target, status);

                CREATE TABLE IF NOT EXISTS answers (
                    request_id     TEXT PRIMARY KEY,
                    answer         TEXT NOT NULL,
                    attachment_ids TEXT NOT NULL DEFAULT '[]',
                    is_error       INTEGER NOT NULL DEFAULT 0,
                    cost_usd       REAL,
                    meta           TEXT NOT NULL DEFAULT '{}',
                    created_at     TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS shared_data (
                    key         TEXT PRIMARY KEY,
                    value       TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    peer        TEXT NOT NULL DEFAULT '',
                    size        INTEGER NOT NULL DEFAULT 0,
                    updated_at  TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS attachments (
                    attachment_id TEXT PRIMARY KEY,
                    media_type    TEXT NOT NULL,
                    size          INTEGER NOT NULL,
                    sha256        TEXT NOT NULL,
                    path          TEXT NOT NULL,
                    original_name TEXT NOT NULL DEFAULT '',
                    peer          TEXT NOT NULL DEFAULT '',
                    created_at    TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS peers (
                    name       TEXT PRIMARY KEY,
                    first_seen TEXT NOT NULL,
                    last_seen  TEXT NOT NULL
                );
                """
            )
            self._conn.commit()

    # -- messages --------------------------------------------------------- #
    def add_message(
        self,
        *,
        request_id: str,
        sender: str,
        target: str,
        kind: str,
        body: str,
        attachment_ids: list[str],
        blocking: bool,
    ) -> MessageRecord:
        ts = _now()
        with self._lock:
            self._conn.execute(
                """INSERT INTO messages
                   (request_id, sender, target, kind, body, attachment_ids,
                    blocking, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)""",
                (
                    request_id,
                    sender,
                    target,
                    kind,
                    body,
                    json.dumps(attachment_ids),
                    1 if blocking else 0,
                    ts,
                    ts,
                ),
            )
            self._conn.commit()
        return MessageRecord(
            request_id=request_id,
            sender=sender,
            target=target,
            kind=kind,
            body=body,
            attachment_ids=attachment_ids,
            blocking=blocking,
            status="queued",
            created_at=ts,
            updated_at=ts,
        )

    def get_message(self, request_id: str) -> MessageRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM messages WHERE request_id = ?", (request_id,)
            ).fetchone()
        return self._row_to_message(row) if row else None

    def set_message_status(self, request_id: str, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE messages SET status = ?, updated_at = ? WHERE request_id = ?",
                (status, _now(), request_id),
            )
            self._conn.commit()

    def recoverable_requests(self) -> list[MessageRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE kind = 'request' "
                "AND status IN ('queued', 'delivered') "
                "AND request_id NOT IN (SELECT request_id FROM answers) "
                "ORDER BY created_at ASC"
            ).fetchall()
        return [self._row_to_message(r) for r in rows]

    def answer_stats(self) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n, "
                "COALESCE(SUM(cost_usd), 0.0) AS total_cost, "
                "COALESCE(SUM(is_error), 0) AS errors FROM answers"
            ).fetchone()
            pending = self._conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE kind = 'request' "
                "AND request_id NOT IN (SELECT request_id FROM answers)"
            ).fetchone()
        return {
            "answers": row["n"],
            "errors": row["errors"],
            "total_cost_usd": round(float(row["total_cost"]), 6),
            "pending_requests": pending["n"],
        }

    def recent_messages(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT m.request_id, m.sender, m.target, m.kind, m.body,
                          m.attachment_ids, m.status, m.created_at AS msg_created_at,
                          a.answer, a.is_error, a.cost_usd, a.meta,
                          a.created_at AS ans_created_at
                   FROM messages m
                   LEFT JOIN answers a ON a.request_id = m.request_id
                   ORDER BY m.created_at DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            meta = json.loads(r["meta"]) if r["meta"] else {}
            out.append(
                {
                    "request_id": r["request_id"],
                    "sender": r["sender"],
                    "target": r["target"],
                    "kind": r["kind"],
                    "question": r["body"],
                    "attachment_ids": json.loads(r["attachment_ids"]),
                    "status": r["status"],
                    "created_at": r["msg_created_at"],
                    "answered": r["answer"] is not None,
                    "answer": r["answer"],
                    "is_error": bool(r["is_error"]) if r["is_error"] is not None else False,
                    "cost_usd": r["cost_usd"],
                    "session_id": meta.get("session_id"),
                    "answer_path": meta.get("path"),
                    "answered_at": r["ans_created_at"],
                }
            )
        return out

    def purge_before(self, cutoff_iso: str) -> list[str]:
        with self._lock:
            # Attachments referenced by a still-in-flight (unanswered) message must
            # be protected, or that message becomes permanently unanswerable when
            # the responder later polls it and the blob is gone.
            protected: set[str] = set()
            for row in self._conn.execute(
                "SELECT attachment_ids FROM messages "
                "WHERE request_id NOT IN (SELECT request_id FROM answers)"
            ).fetchall():
                protected.update(json.loads(row["attachment_ids"]))

            # Only purge requests that already have an answer (don't drop in-flight).
            self._conn.execute(
                "DELETE FROM messages WHERE created_at < ? "
                "AND request_id IN (SELECT request_id FROM answers)",
                (cutoff_iso,),
            )
            self._conn.execute(
                "DELETE FROM answers WHERE created_at < ?", (cutoff_iso,)
            )
            # Purge old attachments, but never one still referenced by an in-flight
            # message (its created_at predates the message, so age alone is unsafe).
            deleted_paths: list[str] = []
            for row in self._conn.execute(
                "SELECT attachment_id, path FROM attachments WHERE created_at < ?",
                (cutoff_iso,),
            ).fetchall():
                if row["attachment_id"] in protected:
                    continue
                self._conn.execute(
                    "DELETE FROM attachments WHERE attachment_id = ?", (row["attachment_id"],)
                )
                deleted_paths.append(row["path"])
            self._conn.commit()
        return deleted_paths

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> MessageRecord:
        return MessageRecord(
            request_id=row["request_id"],
            sender=row["sender"],
            target=row["target"],
            kind=row["kind"],
            body=row["body"],
            attachment_ids=json.loads(row["attachment_ids"]),
            blocking=bool(row["blocking"]),
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # -- answers ---------------------------------------------------------- #
    def save_answer(
        self,
        *,
        request_id: str,
        answer: str,
        attachment_ids: list[str],
        is_error: bool,
        cost_usd: float | None,
        meta: dict[str, Any],
    ) -> AnswerRecord:
        ts = _now()
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO answers
                   (request_id, answer, attachment_ids, is_error, cost_usd, meta, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    request_id,
                    answer,
                    json.dumps(attachment_ids),
                    1 if is_error else 0,
                    cost_usd,
                    json.dumps(meta),
                    ts,
                ),
            )
            self._conn.commit()
        return AnswerRecord(
            request_id=request_id,
            answer=answer,
            attachment_ids=attachment_ids,
            is_error=is_error,
            cost_usd=cost_usd,
            meta=meta,
            created_at=ts,
        )

    def get_answer(self, request_id: str) -> AnswerRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM answers WHERE request_id = ?", (request_id,)
            ).fetchone()
        if not row:
            return None
        return AnswerRecord(
            request_id=row["request_id"],
            answer=row["answer"],
            attachment_ids=json.loads(row["attachment_ids"]),
            is_error=bool(row["is_error"]),
            cost_usd=row["cost_usd"],
            meta=json.loads(row["meta"]),
            created_at=row["created_at"],
        )

    # -- shared data ------------------------------------------------------ #
    def put_shared(self, *, key: str, value: str, description: str, peer: str) -> SharedDataRecord:
        ts = _now()
        size = len(value.encode("utf-8"))
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO shared_data
                   (key, value, description, peer, size, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (key, value, description, peer, size, ts),
            )
            self._conn.commit()
        return SharedDataRecord(
            key=key, value=value, description=description, peer=peer, size=size, updated_at=ts
        )

    def get_shared(self, key: str) -> SharedDataRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM shared_data WHERE key = ?", (key,)
            ).fetchone()
        if not row:
            return None
        return self._row_to_shared(row)

    def list_shared(self) -> list[SharedDataRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM shared_data ORDER BY updated_at DESC"
            ).fetchall()
        return [self._row_to_shared(r) for r in rows]

    @staticmethod
    def _row_to_shared(row: sqlite3.Row) -> SharedDataRecord:
        return SharedDataRecord(
            key=row["key"],
            value=row["value"],
            description=row["description"],
            peer=row["peer"],
            size=row["size"],
            updated_at=row["updated_at"],
        )

    # -- attachments ------------------------------------------------------ #
    def save_attachment(
        self,
        *,
        attachment_id: str,
        media_type: str,
        size: int,
        sha256: str,
        path: str,
        original_name: str,
        peer: str,
    ) -> AttachmentRecord:
        ts = _now()
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO attachments
                   (attachment_id, media_type, size, sha256, path, original_name, peer, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (attachment_id, media_type, size, sha256, path, original_name, peer, ts),
            )
            self._conn.commit()
        return AttachmentRecord(
            attachment_id=attachment_id,
            media_type=media_type,
            size=size,
            sha256=sha256,
            path=path,
            original_name=original_name,
            peer=peer,
            created_at=ts,
        )

    def get_attachment(self, attachment_id: str) -> AttachmentRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM attachments WHERE attachment_id = ?", (attachment_id,)
            ).fetchone()
        if not row:
            return None
        return AttachmentRecord(
            attachment_id=row["attachment_id"],
            media_type=row["media_type"],
            size=row["size"],
            sha256=row["sha256"],
            path=row["path"],
            original_name=row["original_name"],
            peer=row["peer"],
            created_at=row["created_at"],
        )

    # -- peers ------------------------------------------------------------ #
    def touch_peer(self, name: str) -> None:
        ts = _now()
        with self._lock:
            self._conn.execute(
                """INSERT INTO peers (name, first_seen, last_seen) VALUES (?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET last_seen = excluded.last_seen""",
                (name, ts, ts),
            )
            self._conn.commit()

    def get_peer(self, name: str) -> PeerRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM peers WHERE name = ?", (name,)
            ).fetchone()
        if not row:
            return None
        return PeerRecord(name=row["name"], first_seen=row["first_seen"], last_seen=row["last_seen"])

    def list_peers(self) -> list[PeerRecord]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM peers ORDER BY name ASC").fetchall()
        return [
            PeerRecord(name=r["name"], first_seen=r["first_seen"], last_seen=r["last_seen"])
            for r in rows
        ]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
