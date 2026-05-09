"""Thin client over go2rtc's ``/api`` for source health and add/remove.

go2rtc is the RTSP-to-MSE/HLS/WebRTC gateway. We don't run it - it lives
in its own systemd unit (``ac-go2rtc.service``) - but we poll it for source
health and we toggle individual sources on/off via PUT/DELETE on
``/api/streams`` when the dashboard's "Streaming" toggle flips.

go2rtc's HTTP surface is documented in the upstream README; we use:

* ``GET /api/streams`` - returns a dict ``{name: producers[]}``.
* ``GET /api/streams?src=<name>`` - per-stream info; we look at
  ``producers[*].state`` ("connected" / "reconnecting" / etc.).
* ``PUT /api/streams?src=<name>&{name}=<rtsp_url>`` - add or replace.
* ``DELETE /api/streams?src=<name>`` - remove.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class Go2RtcClient:
    def __init__(self, base_url: str | None = None, *, timeout: float = 1.5) -> None:
        self._base = (base_url or os.environ.get("GO2RTC_BASE_URL") or "http://127.0.0.1:1984").rstrip("/")
        self._client: httpx.AsyncClient | None = None
        self._timeout = timeout

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self._base, timeout=self._timeout)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def is_reachable(self) -> bool:
        try:
            client = await self._get_client()
            r = await client.get("/api/streams")
            return r.status_code == 200
        except Exception:
            return False

    async def list_streams(self) -> dict[str, Any]:
        client = await self._get_client()
        r = await client.get("/api/streams")
        r.raise_for_status()
        body = r.json()
        return body if isinstance(body, dict) else {}

    async def stream_state(self, name: str) -> str:
        """Return the source-side connection state for ``name``.

        The values come from go2rtc directly; we only special-case
        ``""``  (empty/no producer) -> ``"disconnected"`` for the caller's
        convenience.
        """

        try:
            client = await self._get_client()
            r = await client.get("/api/streams", params={"src": name})
            if r.status_code == 404:
                return "disconnected"
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            logger.debug("go2rtc stream_state(%s) failed: %s", name, exc)
            return "unknown"
        producers = (data or {}).get("producers") or []
        for prod in producers:
            state = prod.get("state")
            if state:
                return str(state)
        return "disconnected"

    async def is_stream_connected(self, name: str) -> bool:
        return (await self.stream_state(name)) == "connected"

    async def add_stream(self, name: str, source_url: str) -> None:
        client = await self._get_client()
        r = await client.put("/api/streams", params={"src": name, "name": source_url})
        r.raise_for_status()

    async def remove_stream(self, name: str) -> None:
        client = await self._get_client()
        try:
            r = await client.delete("/api/streams", params={"src": name})
            if r.status_code not in (200, 204, 404):
                r.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("go2rtc remove_stream(%s) failed: %s", name, exc)


__all__ = ["Go2RtcClient"]
