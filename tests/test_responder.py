"""Responder unit tests — no real `claude` binary needed (subprocess is faked).

Covers the regression-prone hardening surfaces: subprocess timeout/kill, the
is_error guard, the narrowed session self-heal, atomic session writes, the
single-instance lock, and the _poll_or_stop shutdown-race salvage.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

import responder as r
from config import Config

pytestmark = pytest.mark.asyncio


@pytest.fixture
def make_responder(tmp_path, monkeypatch):
    def _make(**env: str) -> r.Responder:
        monkeypatch.setenv("PEER_SELF", "api")
        monkeypatch.setenv("PROJECT_DIR", str(tmp_path))
        monkeypatch.setenv("BRIDGE_SESSION_DIR", str(tmp_path / "sessions"))
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        return r.Responder(Config.from_env())

    return _make


class FakeProc:
    def __init__(self, stdout: bytes = b"{}", stderr: bytes = b"", returncode: int = 0, hang: bool = False):
        self._stdout, self._stderr, self.returncode, self._hang = stdout, stderr, returncode, hang
        self.killed = False

    async def communicate(self, input: bytes | None = None):
        if self._hang:
            await asyncio.sleep(30)  # cancelled by wait_for on timeout
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode


def _patch_exec(monkeypatch, *procs: FakeProc):
    seq = list(procs)
    calls = {"n": 0}

    async def fake(*a, **k):
        proc = seq[min(calls["n"], len(seq) - 1)]
        calls["n"] += 1
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
    return calls


async def test_run_claude_raises_on_is_error_result(make_responder, monkeypatch):
    resp = make_responder()
    body = json.dumps({"is_error": True, "subtype": "error_max_turns", "result": "partial"}).encode()
    _patch_exec(monkeypatch, FakeProc(stdout=body))
    with pytest.raises(r.ClaudeError):
        await resp._run_claude("q", resume=None)


async def test_run_claude_exit_error_on_nonzero(make_responder, monkeypatch):
    resp = make_responder()
    _patch_exec(monkeypatch, FakeProc(stderr=b"No conversation found with session ID", returncode=1))
    with pytest.raises(r.ClaudeExitError):
        await resp._run_claude("q", resume="stale")


async def test_run_claude_timeout_kills_child(make_responder, monkeypatch):
    resp = make_responder(CLAUDE_TIMEOUT_SECONDS="1")
    proc = FakeProc(hang=True)
    _patch_exec(monkeypatch, proc)
    with pytest.raises(r.ClaudeError):
        await resp._run_claude("q", resume=None)
    assert proc.killed is True


async def test_session_self_heals_on_resumed_exit_error(make_responder, monkeypatch):
    resp = make_responder()
    resp._save_session("stale-session")
    ok = json.dumps({"result": "ok", "session_id": "fresh-session"}).encode()
    calls = _patch_exec(
        monkeypatch,
        FakeProc(stderr=b"bad resume", returncode=1),  # resumed run fails
        FakeProc(stdout=ok),                           # fresh retry succeeds
    )
    result = await resp._run_session_turn("q")
    assert result["session_id"] == "fresh-session"
    assert resp._load_session() == "fresh-session"
    assert calls["n"] == 2  # it actually retried fresh


async def test_session_preserved_on_transient_timeout(make_responder, monkeypatch):
    resp = make_responder(CLAUDE_TIMEOUT_SECONDS="1")
    resp._save_session("keep-me")
    _patch_exec(monkeypatch, FakeProc(hang=True))
    with pytest.raises(r.ClaudeError):
        await resp._run_session_turn("q")
    # A timeout must NOT wipe a valid accumulating session.
    assert resp._load_session() == "keep-me"


async def test_save_and_clear_session_roundtrip(make_responder):
    resp = make_responder()
    resp._save_session("abc123")
    assert resp._load_session() == "abc123"
    resp._clear_session()
    assert resp._load_session() is None


async def test_single_instance_lock(make_responder):
    first = make_responder()
    first.acquire_lock()
    try:
        second = make_responder()
        with pytest.raises(RuntimeError):
            second.acquire_lock()
    finally:
        if first._lock_fd is not None:
            os.close(first._lock_fd)


async def test_poll_or_stop_salvages_delivered_message(make_responder):
    resp = make_responder()

    class ImmediateClient:
        async def poll(self, peer, wait):
            return {"request_id": "R1", "question": "q"}

    resp._stop.set()  # stop and poll are both ready in the same cycle
    msg = await resp._poll_or_stop(ImmediateClient())
    assert msg == {"request_id": "R1", "question": "q"}  # not dropped


async def test_poll_or_stop_returns_none_when_only_stop(make_responder):
    resp = make_responder()

    class SlowClient:
        async def poll(self, peer, wait):
            await asyncio.sleep(30)  # never returns; stop must win

    resp._stop.set()
    assert await resp._poll_or_stop(SlowClient()) is None
