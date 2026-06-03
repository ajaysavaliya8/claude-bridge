"""Unit tests for the SQLite-backed storage abstraction."""

from __future__ import annotations

from pathlib import Path

from db import SqliteDatabase


def make_db(tmp_path: Path) -> SqliteDatabase:
    return SqliteDatabase(tmp_path / "unit.db")


def test_message_lifecycle(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    msg = db.add_message(
        request_id="r1",
        sender="web",
        target="api",
        kind="request",
        body="what fields?",
        attachment_ids=["a1", "a2"],
        blocking=True,
    )
    assert msg.status == "queued"
    assert msg.attachment_ids == ["a1", "a2"]

    fetched = db.get_message("r1")
    assert fetched is not None
    assert fetched.sender == "web"
    assert fetched.blocking is True

    # Recoverable = needs answering. A queued request qualifies...
    assert [m.request_id for m in db.recoverable_requests()] == ["r1"]
    # ...and so does a delivered-but-unanswered one (responder may have died).
    db.set_message_status("r1", "delivered")
    assert [m.request_id for m in db.recoverable_requests()] == ["r1"]
    # Once answered, it is no longer recoverable.
    db.save_answer(
        request_id="r1", answer="x", attachment_ids=[], is_error=False, cost_usd=None, meta={}
    )
    assert db.recoverable_requests() == []
    db.close()


def test_answer_roundtrip(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    db.add_message(
        request_id="r1", sender="web", target="api", kind="request",
        body="q", attachment_ids=[], blocking=True,
    )
    assert db.get_answer("r1") is None
    db.save_answer(
        request_id="r1",
        answer="here is the answer",
        attachment_ids=["img1"],
        is_error=False,
        cost_usd=0.0123,
        meta={"path": "headless-text", "num_turns": 3},
    )
    ans = db.get_answer("r1")
    assert ans is not None
    assert ans.answer == "here is the answer"
    assert ans.attachment_ids == ["img1"]
    assert ans.cost_usd == 0.0123
    assert ans.meta["num_turns"] == 3
    db.close()


def test_shared_data(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    rec = db.put_shared(key="schema", value="big payload", description="the schema", peer="api")
    assert rec.size == len("big payload".encode("utf-8"))
    assert db.get_shared("schema").value == "big payload"

    # Overwrite updates value + size.
    db.put_shared(key="schema", value="bigger payload!!", description="v2", peer="api")
    assert db.get_shared("schema").value == "bigger payload!!"
    assert db.get_shared("schema").size == len("bigger payload!!".encode("utf-8"))
    assert len(db.list_shared()) == 1
    assert db.get_shared("missing") is None
    db.close()


def test_attachment_metadata(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    rec = db.save_attachment(
        attachment_id="att1",
        media_type="image/png",
        size=1234,
        sha256="deadbeef",
        path="/blobs/att1",
        original_name="screen.png",
        peer="web",
    )
    assert rec.media_type == "image/png"
    got = db.get_attachment("att1")
    assert got is not None
    assert got.sha256 == "deadbeef"
    assert got.original_name == "screen.png"
    assert db.get_attachment("nope") is None
    db.close()


def test_peer_heartbeat(tmp_path: Path, monkeypatch) -> None:
    # Strictly increasing timestamps so we can actually prove last_seen advances
    # while first_seen is preserved (real clock can return identical ISO stamps).
    import db as db_module

    stamps = iter([f"2026-01-01T00:00:0{i}+00:00" for i in range(9)])
    monkeypatch.setattr(db_module, "_now", lambda: next(stamps))

    db = make_db(tmp_path)
    assert db.get_peer("web") is None
    db.touch_peer("web")
    first = db.get_peer("web")
    assert first is not None
    db.touch_peer("web")  # heartbeat advances last_seen, preserves first_seen
    second = db.get_peer("web")
    assert second.first_seen == first.first_seen
    assert second.last_seen > second.first_seen
    db.touch_peer("api")
    assert {p.name for p in db.list_peers()} == {"web", "api"}
    db.close()


def test_answer_stats(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    for rid, err, cost in [("a", False, 0.01), ("b", True, 0.02)]:
        db.add_message(
            request_id=rid, sender="w", target="a", kind="request",
            body="q", attachment_ids=[], blocking=True,
        )
        db.save_answer(
            request_id=rid, answer="x", attachment_ids=[], is_error=err, cost_usd=cost, meta={}
        )
    # One unanswered request should count as pending.
    db.add_message(
        request_id="c", sender="w", target="a", kind="request",
        body="q", attachment_ids=[], blocking=True,
    )
    stats = db.answer_stats()
    assert stats["answers"] == 2
    assert stats["errors"] == 1
    assert abs(stats["total_cost_usd"] - 0.03) < 1e-9
    assert stats["pending_requests"] == 1
    db.close()


def test_purge_before(tmp_path: Path, monkeypatch) -> None:
    import db as db_module

    monkeypatch.setattr(db_module, "_now", lambda: "2020-01-01T00:00:00+00:00")
    db = make_db(tmp_path)
    db.add_message(
        request_id="old", sender="w", target="a", kind="request",
        body="q", attachment_ids=[], blocking=True,
    )
    db.save_answer(
        request_id="old", answer="x", attachment_ids=[], is_error=False, cost_usd=None, meta={}
    )
    db.save_attachment(
        attachment_id="att", media_type="image/png", size=1, sha256="d",
        path="/blobs/att", original_name="a.png", peer="w",
    )
    paths = db.purge_before("2021-01-01T00:00:00+00:00")
    assert paths == ["/blobs/att"]
    assert db.get_answer("old") is None
    assert db.get_message("old") is None
    assert db.get_attachment("att") is None
    db.close()


def test_purge_before_protects_in_flight_attachments(tmp_path: Path, monkeypatch) -> None:
    import db as db_module

    monkeypatch.setattr(db_module, "_now", lambda: "2020-01-01T00:00:00+00:00")
    db = make_db(tmp_path)
    # In-flight (unanswered) image request referencing an attachment.
    db.save_attachment(
        attachment_id="live", media_type="image/png", size=1, sha256="d",
        path="/blobs/live", original_name="a.png", peer="w",
    )
    db.add_message(
        request_id="pending", sender="w", target="a", kind="request",
        body="see image", attachment_ids=["live"], blocking=True,
    )
    # Answered request with its own (purgeable) attachment.
    db.save_attachment(
        attachment_id="done-att", media_type="image/png", size=1, sha256="d",
        path="/blobs/done", original_name="b.png", peer="w",
    )
    db.add_message(
        request_id="done", sender="w", target="a", kind="request",
        body="q", attachment_ids=["done-att"], blocking=True,
    )
    db.save_answer(
        request_id="done", answer="x", attachment_ids=[], is_error=False, cost_usd=None, meta={}
    )

    paths = db.purge_before("2021-01-01T00:00:00+00:00")
    assert paths == ["/blobs/done"]                  # only the answered one purged
    assert db.get_attachment("live") is not None     # in-flight blob protected
    assert db.get_attachment("done-att") is None
    db.close()
