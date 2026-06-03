"""Attachment upload/download + type/size validation tests."""

from __future__ import annotations

import base64

import httpx
import pytest

from conftest import PNG_1x1

pytestmark = pytest.mark.asyncio


async def test_upload_download_base64_json(app_client) -> None:
    client, _ = app_client
    up = await client.post(
        "/attachment",
        json={"data": base64.b64encode(PNG_1x1).decode("ascii"), "peer": "web", "filename": "a.png"},
    )
    assert up.status_code == 200
    body = up.json()
    assert body["media_type"] == "image/png"
    assert body["size"] == len(PNG_1x1)

    down = await client.get(f"/attachment/{body['attachment_id']}")
    assert down.status_code == 200
    assert down.content == PNG_1x1
    assert down.headers["content-type"] == "image/png"


async def test_upload_multipart(app_client) -> None:
    client, _ = app_client
    up = await client.post(
        "/attachment",
        files={"file": ("shot.png", PNG_1x1, "image/png")},
        data={"peer": "web"},
    )
    assert up.status_code == 200
    aid = up.json()["attachment_id"]
    down = await client.get(f"/attachment/{aid}")
    assert down.content == PNG_1x1


async def test_reject_non_image(app_client) -> None:
    client, _ = app_client
    up = await client.post(
        "/attachment",
        json={"data": base64.b64encode(b"this is plainly not an image").decode("ascii")},
    )
    assert up.status_code == 415


async def test_reject_oversize(app_client) -> None:
    client, _ = app_client
    # Valid PNG signature but past the 5 MB cap -> the size check fires first.
    oversize = b"\x89PNG\r\n\x1a\n" + b"\x00" * (5 * 1024 * 1024 + 1)
    up = await client.post(
        "/attachment", json={"data": base64.b64encode(oversize).decode("ascii")}
    )
    assert up.status_code == 415


async def test_reject_bad_base64(app_client) -> None:
    client, _ = app_client
    up = await client.post("/attachment", json={"data": "!!!not base64!!!"})
    assert up.status_code == 422


async def test_unknown_content_type_rejected(app_client) -> None:
    client, _ = app_client
    up = await client.post(
        "/attachment", content=b"raw", headers={"content-type": "text/plain"}
    )
    assert up.status_code == 415


async def test_download_unknown_attachment_404(app_client) -> None:
    client, _ = app_client
    down = await client.get("/attachment/nope")
    assert down.status_code == 404


async def test_download_410_when_bytes_missing(app_client) -> None:
    client, cfg = app_client
    up = await client.post(
        "/attachment", json={"data": base64.b64encode(PNG_1x1).decode("ascii")}
    )
    aid = up.json()["attachment_id"]
    # Metadata remains, blob removed -> 410 Gone.
    (cfg.attachments_dir / aid).unlink()
    down = await client.get(f"/attachment/{aid}")
    assert down.status_code == 410


async def test_multipart_missing_file_part(app_client) -> None:
    client, _ = app_client
    up = await client.post(
        "/attachment", files={"notfile": ("x.bin", b"x", "application/octet-stream")}
    )
    assert up.status_code == 422


async def test_json_missing_data_field(app_client) -> None:
    client, _ = app_client
    up = await client.post("/attachment", json={"peer": "web"})
    assert up.status_code == 422


async def test_per_message_attachment_cap(app_client) -> None:
    client, cfg = app_client
    ids = [f"id{i}" for i in range(cfg.max_images_per_message + 1)]
    r = await client.post(
        "/ask",
        json={
            "sender": "web",
            "target": "api",
            "question": "q",
            "blocking": False,
            "attachment_ids": ids,
        },
    )
    assert r.status_code == 413
