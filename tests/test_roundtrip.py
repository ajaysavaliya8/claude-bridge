"""Full broker round-trip tests with a fake (non-Claude) responder.

Proves ask/answer blocking + unblocking, the timeout path, non-blocking ask +
/reply, notes, shared data, self-registration, auth, and that a malformed
request does not wedge the broker.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from asgi_lifespan import LifespanManager

from broker import create_app
from config import Config
from db import SqliteDatabase

pytestmark = pytest.mark.asyncio


async def _respond(client: httpx.AsyncClient, *, expect_question: str, answer: str) -> dict:
    """Act as a peer's responder: poll one question, post a canned answer."""
    msg = await client.get("/poll", params={"peer": "api", "wait": 5})
    assert msg.status_code == 200, msg.text
    data = msg.json()
    assert data["question"] == expect_question
    posted = await client.post(
        "/answer", json={"request_id": data["request_id"], "answer": answer}
    )
    assert posted.status_code == 200
    return data


async def test_health_is_open(app_client) -> None:
    client, _ = app_client
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


async def test_blocking_ask_unblocks_on_answer(app_client) -> None:
    client, _ = app_client

    async def asker() -> httpx.Response:
        return await client.post(
            "/ask",
            json={"sender": "web", "target": "api", "question": "What port?", "blocking": True},
        )

    asker_resp, _ = await asyncio.gather(
        asker(), _respond(client, expect_question="What port?", answer="8000")
    )
    assert asker_resp.status_code == 200
    body = asker_resp.json()
    assert body["status"] == "answered"
    assert body["answer"] == "8000"
    assert body["is_error"] is False


async def test_blocking_ask_times_out_to_pending(app_client) -> None:
    client, cfg = app_client
    # No responder for "ghost": the broker holds one window then returns pending.
    r = await client.post(
        "/ask",
        json={"sender": "web", "target": "ghost", "question": "anyone?", "blocking": True},
    )
    assert r.status_code == 202
    assert r.json()["status"] == "pending"


async def test_nonblocking_ask_then_reply(app_client) -> None:
    client, _ = app_client
    r = await client.post(
        "/ask",
        json={"sender": "web", "target": "api", "question": "q", "blocking": False},
    )
    assert r.status_code == 202
    request_id = r.json()["request_id"]

    await _respond(client, expect_question="q", answer="async-answer")

    reply = await client.get(f"/reply/{request_id}", params={"wait": 5})
    assert reply.status_code == 200
    assert reply.json()["answer"] == "async-answer"


async def test_reply_pending_before_answer(app_client) -> None:
    client, _ = app_client
    r = await client.post(
        "/ask", json={"sender": "web", "target": "api", "question": "q", "blocking": False}
    )
    request_id = r.json()["request_id"]
    # Nobody answered yet -> /reply returns pending (short wait).
    reply = await client.get(f"/reply/{request_id}", params={"wait": 1})
    assert reply.status_code == 202
    assert reply.json()["status"] == "pending"


async def test_note_delivery_and_ack(app_client) -> None:
    client, _ = app_client
    r = await client.post(
        "/ask",
        json={
            "sender": "web",
            "target": "api",
            "question": "heads up: renamed field",
            "blocking": False,
            "kind": "note",
        },
    )
    assert r.status_code == 202
    request_id = r.json()["request_id"]

    msg = await client.get("/poll", params={"peer": "api", "wait": 5})
    data = msg.json()
    assert data["kind"] == "note"
    assert data["question"] == "heads up: renamed field"
    await client.post("/answer", json={"request_id": request_id, "answer": "noted"})

    reply = await client.get(f"/reply/{request_id}", params={"wait": 2})
    assert reply.json()["answer"] == "noted"


async def test_shared_data_roundtrip(app_client) -> None:
    client, _ = app_client
    put = await client.post(
        "/shared",
        json={"key": "schema", "value": "CREATE TABLE ...", "description": "db schema", "peer": "api"},
    )
    assert put.status_code == 200

    got = await client.get("/shared/schema")
    assert got.status_code == 200
    assert got.json()["value"] == "CREATE TABLE ..."

    listing = await client.get("/shared")
    keys = [item["key"] for item in listing.json()["items"]]
    assert "schema" in keys


async def test_shared_value_size_limit(app_client) -> None:
    client, cfg = app_client
    too_big = "x" * (cfg.max_shared_value_bytes + 1)
    r = await client.post("/shared", json={"key": "big", "value": too_big})
    assert r.status_code == 413


async def test_peers_self_register(app_client) -> None:
    client, _ = app_client
    await client.post(
        "/ask", json={"sender": "web", "target": "api", "question": "q", "blocking": False}
    )
    await client.get("/poll", params={"peer": "api", "wait": 1})
    peers = (await client.get("/peers")).json()["peers"]
    names = {p["name"] for p in peers}
    assert {"web", "api"} <= names


async def test_malformed_request_does_not_wedge(app_client) -> None:
    client, _ = app_client
    bad = await client.post("/ask", json={"sender": "web"})  # missing target + question
    assert bad.status_code == 422
    # Broker is still responsive afterwards.
    assert (await client.get("/health")).status_code == 200


async def test_answer_unknown_request_is_404(app_client) -> None:
    client, _ = app_client
    r = await client.post("/answer", json={"request_id": "does-not-exist", "answer": "x"})
    assert r.status_code == 404


async def test_oversize_question_rejected(app_client) -> None:
    client, cfg = app_client
    big = "x" * (cfg.max_question_bytes + 1)
    r = await client.post(
        "/ask", json={"sender": "web", "target": "api", "question": big, "blocking": False}
    )
    assert r.status_code == 413


async def test_invalid_kind_rejected(app_client) -> None:
    client, _ = app_client
    r = await client.post(
        "/ask",
        json={"sender": "web", "target": "api", "question": "q", "blocking": False, "kind": "bogus"},
    )
    assert r.status_code == 422


async def test_overlong_name_rejected(app_client) -> None:
    client, cfg = app_client
    r = await client.post(
        "/ask",
        json={"sender": "x" * (cfg.max_name_length + 1), "target": "api", "question": "q", "blocking": False},
    )
    assert r.status_code == 422


async def test_answer_is_idempotent_first_writer_wins(app_client) -> None:
    client, _ = app_client
    r = await client.post(
        "/ask", json={"sender": "web", "target": "api", "question": "q", "blocking": False}
    )
    rid = r.json()["request_id"]
    await client.get("/poll", params={"peer": "api", "wait": 2})
    first = await client.post("/answer", json={"request_id": rid, "answer": "first"})
    assert first.status_code == 200
    second = await client.post("/answer", json={"request_id": rid, "answer": "SECOND"})
    assert second.status_code == 200
    assert second.json().get("duplicate") is True
    # The stored answer is the first one, not clobbered.
    reply = await client.get(f"/reply/{rid}", params={"wait": 2})
    assert reply.json()["answer"] == "first"


async def test_ui_page_served(app_client) -> None:
    client, _ = app_client
    r = await client.get("/ui")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "claude-bridge" in r.text


async def test_messages_feed_and_session_filter(app_client) -> None:
    client, _ = app_client
    # Answer one request with a session id in meta (mirrors the responder).
    r = await client.post(
        "/ask", json={"sender": "web", "target": "api", "question": "what port?", "blocking": False}
    )
    rid = r.json()["request_id"]
    await client.get("/poll", params={"peer": "api", "wait": 2})
    await client.post(
        "/answer",
        json={"request_id": rid, "answer": "8000", "cost_usd": 0.02,
              "meta": {"path": "headless-text", "session_id": "sess-abc"}},
    )

    feed = (await client.get("/messages")).json()["messages"]
    assert len(feed) == 1
    row = feed[0]
    assert row["sender"] == "web" and row["target"] == "api"
    assert row["answered"] is True and row["answer"] == "8000"
    assert row["session_id"] == "sess-abc"
    assert row["cost_usd"] == 0.02

    # Filter by the matching / a non-matching session id.
    match = (await client.get("/messages", params={"session_id": "sess-abc"})).json()["messages"]
    assert len(match) == 1
    miss = (await client.get("/messages", params={"session_id": "nope"})).json()["messages"]
    assert miss == []


async def test_metrics_endpoint(app_client) -> None:
    client, _ = app_client
    r = await client.post(
        "/ask", json={"sender": "web", "target": "api", "question": "q", "blocking": False}
    )
    rid = r.json()["request_id"]
    await client.get("/poll", params={"peer": "api", "wait": 2})
    await client.post("/answer", json={"request_id": rid, "answer": "a", "cost_usd": 0.05})
    metrics = await client.get("/metrics")
    assert metrics.status_code == 200
    body = metrics.json()
    assert body["answers"] == 1
    assert abs(body["total_cost_usd"] - 0.05) < 1e-9


async def test_request_body_size_limit(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BRIDGE_DB_PATH", str(tmp_path / "s.db"))
    monkeypatch.setenv("BRIDGE_ATTACHMENTS_DIR", str(tmp_path / "att"))
    monkeypatch.setenv("MAX_REQUEST_BYTES", "500")
    cfg = Config.from_env()
    app = create_app(cfg, db=SqliteDatabase(cfg.db_path))
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://broker.test") as client:
            r = await client.post(
                "/ask",
                json={"sender": "web", "target": "api", "question": "x" * 2000, "blocking": False},
            )
            assert r.status_code == 413


async def test_answer_event_reclaimed_after_roundtrip(config) -> None:
    # The answer-Event map must not grow without bound: it should be empty again
    # after a blocking ask is answered.
    app = create_app(config, db=SqliteDatabase(config.db_path))
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://broker.test"
        ) as client:
            async def asker():
                return await client.post(
                    "/ask", json={"sender": "web", "target": "api", "question": "q", "blocking": True}
                )

            async def responder():
                m = await client.get("/poll", params={"peer": "api", "wait": 5})
                await client.post("/answer", json={"request_id": m.json()["request_id"], "answer": "a"})

            await asyncio.gather(asker(), responder())
            assert app.state.bridge._answer_events == {}


async def test_answer_event_reclaimed_on_timeout(config) -> None:
    # A blocking ask that times out (no responder) must also reclaim its Event.
    app = create_app(config, db=SqliteDatabase(config.db_path))
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://broker.test"
        ) as client:
            r = await client.post(
                "/ask", json={"sender": "web", "target": "ghost", "question": "q", "blocking": True}
            )
            assert r.json()["status"] == "pending"
            assert app.state.bridge._answer_events == {}


async def test_recoverable_requeue_on_startup(config) -> None:
    # A request delivered to a responder that then died must be re-delivered after
    # a broker restart (H4). Seed it, then boot a fresh app and poll.
    seed = SqliteDatabase(config.db_path)
    seed.add_message(
        request_id="rq", sender="web", target="api", kind="request",
        body="seeded question", attachment_ids=[], blocking=True,
    )
    seed.set_message_status("rq", "delivered")
    seed.close()

    app = create_app(config, db=SqliteDatabase(config.db_path))
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://broker.test") as client:
            r = await client.get("/poll", params={"peer": "api", "wait": 3})
            assert r.status_code == 200
            assert r.json()["request_id"] == "rq"
            assert r.json()["question"] == "seeded question"
