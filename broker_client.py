"""Async HTTP client for the broker, shared by the MCP server and the responder.

Centralises the base URL and timeout handling so the two consumers never
duplicate wire details. Long-poll calls (``poll``,
``reply``, blocking ``ask``) use a request timeout slightly above the broker's
server-side wait window so the socket never closes before the broker replies.
"""

from __future__ import annotations

import base64
from typing import Any

import httpx


class BrokerError(RuntimeError):
    """Non-success response from the broker (carries status + server detail)."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"broker returned {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class BrokerClient:
    """Thin async wrapper over the broker's HTTP API."""

    def __init__(
        self,
        base_url: str,
        *,
        default_timeout: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._default_timeout = default_timeout
        self._client = httpx.AsyncClient(base_url=self._base_url)

    async def __aenter__(self) -> "BrokerClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _long_poll_timeout(wait: float) -> float:
        return wait + 10.0

    @staticmethod
    def _raise_for_status(resp: httpx.Response, *, allow: tuple[int, ...] = (200,)) -> None:
        if resp.status_code in allow:
            return
        detail = resp.text
        try:
            detail = resp.json().get("detail", detail)
        except Exception:  # noqa: BLE001 - best-effort detail extraction
            pass
        raise BrokerError(resp.status_code, detail)

    # -- asking ---------------------------------------------------------- #
    async def ask(
        self,
        *,
        sender: str,
        target: str,
        question: str,
        blocking: bool,
        kind: str = "request",
        attachment_ids: list[str] | None = None,
        wait: float = 25.0,
    ) -> dict[str, Any]:
        """POST /ask. Returns the JSON body. For a blocking ask the body is
        either ``status=answered`` (with the answer) or ``status=pending``."""
        timeout = self._long_poll_timeout(wait) if blocking else self._default_timeout
        resp = await self._client.post(
            "/ask",
            json={
                "sender": sender,
                "target": target,
                "question": question,
                "blocking": blocking,
                "kind": kind,
                "attachment_ids": attachment_ids or [],
            },
            timeout=timeout,
        )
        self._raise_for_status(resp, allow=(200, 202))
        return resp.json()

    async def reply(self, request_id: str, *, wait: float = 25.0) -> dict[str, Any]:
        """GET /reply/{id}. ``status`` is ``answered`` or ``pending``."""
        resp = await self._client.get(
            f"/reply/{request_id}",
            params={"wait": wait},
            timeout=self._long_poll_timeout(wait),
        )
        self._raise_for_status(resp, allow=(200, 202))
        return resp.json()

    # -- answering side -------------------------------------------------- #
    async def poll(self, peer: str, *, wait: float = 25.0) -> dict[str, Any] | None:
        """GET /poll. Returns the next question for ``peer`` or ``None`` on a
        204 (nothing within the wait window)."""
        resp = await self._client.get(
            "/poll",
            params={"peer": peer, "wait": wait},
            timeout=self._long_poll_timeout(wait),
        )
        if resp.status_code == 204:
            return None
        self._raise_for_status(resp, allow=(200,))
        return resp.json()

    async def answer(
        self,
        *,
        request_id: str,
        answer: str,
        attachment_ids: list[str] | None = None,
        is_error: bool = False,
        cost_usd: float | None = None,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        resp = await self._client.post(
            "/answer",
            json={
                "request_id": request_id,
                "answer": answer,
                "attachment_ids": attachment_ids or [],
                "is_error": is_error,
                "cost_usd": cost_usd,
                "meta": meta or {},
            },
            timeout=self._default_timeout,
        )
        self._raise_for_status(resp, allow=(200,))
        return resp.json()

    # -- attachments ----------------------------------------------------- #
    async def upload_attachment(
        self, data: bytes, *, peer: str = "", filename: str = ""
    ) -> dict[str, Any]:
        """Upload image bytes (base64 JSON path). The broker sniffs and
        validates the type itself; we just hand it the bytes."""
        resp = await self._client.post(
            "/attachment",
            json={
                "data": base64.b64encode(data).decode("ascii"),
                "filename": filename,
                "peer": peer,
            },
            timeout=self._default_timeout,
        )
        self._raise_for_status(resp, allow=(200,))
        return resp.json()

    async def download_attachment(self, attachment_id: str) -> tuple[bytes, str]:
        """Return ``(bytes, media_type)`` for an attachment."""
        resp = await self._client.get(
            f"/attachment/{attachment_id}", timeout=self._default_timeout
        )
        self._raise_for_status(resp, allow=(200,))
        return resp.content, resp.headers.get("content-type", "application/octet-stream")

    # -- shared data ----------------------------------------------------- #
    async def put_shared(
        self, *, key: str, value: str, description: str = "", peer: str = ""
    ) -> dict[str, Any]:
        resp = await self._client.post(
            "/shared",
            json={"key": key, "value": value, "description": description, "peer": peer},
            timeout=self._default_timeout,
        )
        self._raise_for_status(resp, allow=(200,))
        return resp.json()

    async def get_shared(self, key: str) -> dict[str, Any] | None:
        resp = await self._client.get(f"/shared/{key}", timeout=self._default_timeout)
        if resp.status_code == 404:
            return None
        self._raise_for_status(resp, allow=(200,))
        return resp.json()

    async def list_shared(self) -> list[dict[str, Any]]:
        resp = await self._client.get("/shared", timeout=self._default_timeout)
        self._raise_for_status(resp, allow=(200,))
        return resp.json().get("items", [])

    # -- peers ----------------------------------------------------------- #
    async def peers(self) -> list[dict[str, Any]]:
        resp = await self._client.get("/peers", timeout=self._default_timeout)
        self._raise_for_status(resp, allow=(200,))
        return resp.json().get("peers", [])

    async def health(self) -> dict[str, Any]:
        resp = await self._client.get("/health", timeout=self._default_timeout)
        self._raise_for_status(resp, allow=(200,))
        return resp.json()
