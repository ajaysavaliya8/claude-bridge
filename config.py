"""Central configuration for claude-bridge.

Every component (broker, responder, bridge MCP server) reads its settings from
environment variables through this module so that defaults, limits and timeouts
live in exactly one place. Nothing here is stack-specific: a peer is described
only by an opaque ``name``, a ``project_dir`` and the ``broker`` it talks to.

Load a snapshot with :func:`Config.from_env`. Pure constants and the image
sniffing helpers are module-level so the broker and the MCP server can validate
attachments identically.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Project root = directory that contains this file. On-disk defaults (SQLite
# file, attachment blob store) hang off here unless overridden by env.
BASE_DIR: Path = Path(__file__).resolve().parent


# --------------------------------------------------------------------------- #
# Attachment / image policy (shared by broker + MCP server)
# --------------------------------------------------------------------------- #

#: Media types we accept as image attachments. Anything else is rejected.
ALLOWED_MEDIA_TYPES: frozenset[str] = frozenset(
    {"image/jpeg", "image/png", "image/gif", "image/webp"}
)

#: Hard cap per image. The Anthropic Messages API also enforces 5 MB.
MAX_IMAGE_BYTES: int = 5 * 1024 * 1024

#: Cap on images attached to a single message.
MAX_IMAGES_PER_MESSAGE: int = 10

# Map of file extension -> media type, used only as a fallback hint. The
# authoritative check is magic-byte sniffing (see ``sniff_media_type``), because
# extension-based MIME detection is unreliable and a known source of bugs.
_EXTENSION_HINTS: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def sniff_media_type(data: bytes) -> str | None:
    """Return the image media type by inspecting the leading magic bytes.

    Returns ``None`` if the bytes are not one of the supported image formats.
    This never trusts a filename or a caller-supplied content type.
    """
    if len(data) < 12:
        return None
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def extension_hint(filename: str) -> str | None:
    """Best-effort media type from a filename extension (non-authoritative)."""
    return _EXTENSION_HINTS.get(Path(filename).suffix.lower())


class AttachmentError(ValueError):
    """Raised when an attachment fails type or size validation."""


def validate_image(data: bytes) -> str:
    """Validate ``data`` as an allowed image and return its media type.

    Raises :class:`AttachmentError` on an unsupported type or an oversize blob.
    """
    if len(data) == 0:
        raise AttachmentError("attachment is empty")
    if len(data) > MAX_IMAGE_BYTES:
        raise AttachmentError(
            f"attachment is {len(data)} bytes; limit is {MAX_IMAGE_BYTES} bytes (5 MB)"
        )
    media_type = sniff_media_type(data)
    if media_type is None or media_type not in ALLOWED_MEDIA_TYPES:
        raise AttachmentError(
            "attachment is not a supported image (allowed: JPEG, PNG, GIF, WebP)"
        )
    return media_type


# --------------------------------------------------------------------------- #
# Environment helpers
# --------------------------------------------------------------------------- #

def _get(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def _require(name: str) -> str:
    value = os.environ.get(name)
    if value is None or value == "":
        raise RuntimeError(
            f"required environment variable {name} is not set; see .env.example"
        )
    return value


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - defensive
        raise RuntimeError(f"environment variable {name} must be an integer") from exc


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# --------------------------------------------------------------------------- #
# Config snapshot
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Config:
    """Immutable snapshot of the environment for one process.

    Not every field is meaningful to every component (the broker ignores
    ``project_dir``; the MCP server ignores ``anthropic_api_key``), but keeping a
    single struct avoids duplicating env parsing across three entrypoints.
    """

    # --- identity / wiring -------------------------------------------------- #
    peer_self: str | None
    default_target: str | None
    broker_url: str

    # --- broker bind -------------------------------------------------------- #
    broker_host: str
    broker_port: int

    # --- storage ------------------------------------------------------------ #
    db_path: Path
    attachments_dir: Path

    # --- responder ---------------------------------------------------------- #
    project_dir: Path | None
    allowed_tools: str
    max_turns: int
    claude_bin: str
    anthropic_api_key: str | None
    vision_model: str
    retrieval_model: str
    session_state_dir: Path
    inject_notes: bool

    # --- timeouts / long-poll windows (seconds) ----------------------------- #
    poll_wait_seconds: int          # broker holds /poll open this long
    ask_wait_seconds: int           # broker holds one /ask or /reply attempt
    ask_timeout_seconds: int        # MCP ask_peer overall deadline
    http_timeout_seconds: int       # generic client request timeout
    responder_poll_loop_pause: int  # backoff after a poll error
    claude_timeout_seconds: int     # wall-clock cap on a single headless claude run

    # --- retention (broker housekeeping) ------------------------------------ #
    retention_days: int             # purge answered messages/blobs older than this (0 = off)
    retention_sweep_seconds: int    # how often the retention task runs

    # --- limits ------------------------------------------------------------- #
    max_question_bytes: int
    max_shared_value_bytes: int
    max_request_bytes: int          # reject POST bodies larger than this (pre-parse)
    max_name_length: int            # cap on peer/sender/target/key string length
    max_images_per_message: int = field(default=MAX_IMAGES_PER_MESSAGE)
    max_image_bytes: int = field(default=MAX_IMAGE_BYTES)

    @classmethod
    def from_env(cls) -> "Config":
        broker_host = _get("BROKER_HOST", "127.0.0.1") or "127.0.0.1"
        broker_port = _int("BROKER_PORT", 8765)
        broker_url = _get("BROKER_URL", f"http://{broker_host}:{broker_port}")
        assert broker_url is not None  # default guarantees a value

        db_path = Path(_get("BRIDGE_DB_PATH", str(BASE_DIR / "claude_bridge.db")))  # type: ignore[arg-type]
        attachments_dir = Path(
            _get("BRIDGE_ATTACHMENTS_DIR", str(BASE_DIR / "attachments"))  # type: ignore[arg-type]
        )

        project_dir_raw = _get("PROJECT_DIR")
        project_dir = Path(project_dir_raw).expanduser().resolve() if project_dir_raw else None

        session_state_dir = Path(
            _get("BRIDGE_SESSION_DIR", str(BASE_DIR / ".sessions"))  # type: ignore[arg-type]
        )

        return cls(
            peer_self=_get("PEER_SELF"),
            default_target=_get("DEFAULT_TARGET"),
            broker_url=broker_url,
            broker_host=broker_host,
            broker_port=broker_port,
            db_path=db_path,
            attachments_dir=attachments_dir,
            project_dir=project_dir,
            allowed_tools=_get("ALLOWED_TOOLS", "Read,Grep,Glob") or "Read,Grep,Glob",
            max_turns=_int("MAX_TURNS", 15),
            claude_bin=_get("CLAUDE_BIN", "claude") or "claude",
            anthropic_api_key=_get("ANTHROPIC_API_KEY"),
            vision_model=_get("VISION_MODEL", "claude-sonnet-4-6") or "claude-sonnet-4-6",
            retrieval_model=_get("RETRIEVAL_MODEL", "claude-sonnet-4-6") or "claude-sonnet-4-6",
            session_state_dir=session_state_dir,
            inject_notes=_bool("INJECT_NOTES", True),
            poll_wait_seconds=_int("POLL_WAIT_SECONDS", 25),
            ask_wait_seconds=_int("ASK_WAIT_SECONDS", 25),
            ask_timeout_seconds=_int("ASK_TIMEOUT_SECONDS", 300),
            http_timeout_seconds=_int("HTTP_TIMEOUT_SECONDS", 30),
            responder_poll_loop_pause=_int("RESPONDER_POLL_LOOP_PAUSE", 3),
            claude_timeout_seconds=_int("CLAUDE_TIMEOUT_SECONDS", 240),
            retention_days=_int("BRIDGE_RETENTION_DAYS", 0),
            retention_sweep_seconds=_int("BRIDGE_RETENTION_SWEEP_SECONDS", 3600),
            max_question_bytes=_int("MAX_QUESTION_BYTES", 100_000),
            max_shared_value_bytes=_int("MAX_SHARED_VALUE_BYTES", 5_000_000),
            max_request_bytes=_int("MAX_REQUEST_BYTES", 12_000_000),
            max_name_length=_int("MAX_NAME_LENGTH", 128),
        )

    # Convenience accessors that fail loudly when a required field is missing
    # for the component that needs it.
    def require_peer_self(self) -> str:
        if not self.peer_self:
            raise RuntimeError("PEER_SELF is required for this component")
        return self.peer_self

    def require_project_dir(self) -> Path:
        if self.project_dir is None:
            raise RuntimeError("PROJECT_DIR is required for the responder")
        return self.project_dir

    def require_anthropic_key(self) -> str:
        if not self.anthropic_api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is required to answer image questions via the vision API"
            )
        return self.anthropic_api_key
