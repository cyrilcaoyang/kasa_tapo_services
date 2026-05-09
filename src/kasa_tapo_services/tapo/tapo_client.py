"""Thin async wrapper around ``pytapo`` for Tapo-app-style operations.

We use pytapo only for things ONVIF does not expose: privacy-mode toggle,
day/night override, and model/firmware metadata. PTZ + presets go through
ONVIF (see :mod:`kasa_tapo_services.tapo.onvif_client`) because pytapo's
preset endpoints are not consistent across the C-series and ONVIF support
is more uniform.

pytapo is synchronous; calls happen in :func:`asyncio.to_thread` so the
gateway stays cooperative under FastAPI's event loop.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TapoModelInfo:
    """Subset of the Tapo ``deviceInfo`` payload we surface on /status."""

    model: str | None = None
    hw_version: str | None = None
    fw_version: str | None = None
    mac: str | None = None


class TapoCameraClient:
    """Async-friendly facade over a single ``pytapo.Tapo`` instance.

    The underlying ``Tapo`` object is created lazily on first use and
    reused for the lifetime of the gateway. pytapo internally caches a
    session token so subsequent calls are cheap.
    """

    def __init__(self, host: str, user: str, password: str) -> None:
        self._host = host
        self._user = user
        self._password = password
        self._inner: Any = None
        self._lock = asyncio.Lock()

    async def _get(self) -> Any:
        """Lazily construct and return the underlying ``pytapo.Tapo`` client."""

        if self._inner is not None:
            return self._inner
        async with self._lock:
            if self._inner is None:
                # Imported here so that machines without pytapo installed
                # still pick up the rest of the package (the import is
                # only required when a camera is actually configured).
                from pytapo import Tapo  # type: ignore

                self._inner = await asyncio.to_thread(
                    Tapo, self._host, self._user, self._password
                )
        return self._inner

    async def close(self) -> None:
        """Drop the cached session - we don't share it across processes."""

        self._inner = None

    # -- Read --------------------------------------------------------------

    async def device_info(self) -> TapoModelInfo:
        try:
            inner = await self._get()
            info = await asyncio.to_thread(inner.getBasicInfo)
        except Exception as exc:
            logger.warning("tapo.getBasicInfo failed: %s", exc)
            return TapoModelInfo()

        # ``getBasicInfo`` returns ``{"device_info": {"basic_info": {...}}}``.
        device = info.get("device_info", {}).get("basic_info", {}) if isinstance(info, dict) else {}
        return TapoModelInfo(
            model=device.get("device_model"),
            hw_version=device.get("hw_version"),
            fw_version=device.get("sw_version"),
            mac=device.get("mac"),
        )

    async def privacy_mode(self) -> bool | None:
        """Return the camera's current privacy-mode setting, or ``None`` on error."""

        try:
            inner = await self._get()
            res = await asyncio.to_thread(inner.getPrivacyMode)
        except Exception as exc:
            logger.warning("tapo.getPrivacyMode failed: %s", exc)
            return None
        if isinstance(res, dict):
            # Tapo returns ``{"enabled": "on"|"off"}`` for most C-series.
            value = res.get("enabled")
            if isinstance(value, str):
                return value.lower() == "on"
            if isinstance(value, bool):
                return value
        return bool(res)

    # -- Write -------------------------------------------------------------

    async def set_privacy_mode(self, enabled: bool) -> None:
        inner = await self._get()
        await asyncio.to_thread(inner.setPrivacyMode, enabled)

    async def set_day_night(self, mode: str) -> None:
        """Override the day/night sensor. ``mode`` is ``"day"|"night"|"auto"``."""

        inner = await self._get()
        await asyncio.to_thread(inner.setDayNightMode, mode)


__all__ = ["TapoCameraClient", "TapoModelInfo"]
