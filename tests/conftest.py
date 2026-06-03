"""Shared test fixtures.

The broker is exercised end-to-end over its real ASGI app (with lifespan), but
with NO Claude Code attached: a fake responder reads ``/poll`` and posts canned
``/answer`` payloads. That is enough to prove the ask/answer plumbing without an
API key or the ``claude`` binary.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path
from typing import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager

# Make the project modules (and this conftest) importable from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from broker import create_app  # noqa: E402
from config import Config  # noqa: E402
from db import SqliteDatabase  # noqa: E402


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    """A Config pointed at a temp DB / attachments dir, with short wait windows
    so timeout-path tests run fast."""
    monkeypatch.setenv("BRIDGE_DB_PATH", str(tmp_path / "bridge.db"))
    monkeypatch.setenv("BRIDGE_ATTACHMENTS_DIR", str(tmp_path / "attachments"))
    monkeypatch.setenv("BRIDGE_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("PEER_SELF", "tester")
    monkeypatch.setenv("POLL_WAIT_SECONDS", "2")
    monkeypatch.setenv("ASK_WAIT_SECONDS", "2")
    monkeypatch.setenv("ASK_TIMEOUT_SECONDS", "4")
    return Config.from_env()


@pytest_asyncio.fixture
async def app_client(config: Config) -> AsyncIterator[tuple[httpx.AsyncClient, Config]]:
    """An httpx client bound to the broker ASGI app (lifespan on)."""
    db = SqliteDatabase(config.db_path)
    app = create_app(config, db=db)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://broker.test"
        ) as client:
            yield client, config


# A 1x1 transparent PNG — small but a genuinely valid image (correct magic bytes).
PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
