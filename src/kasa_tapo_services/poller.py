"""Background pollers + status cache for the gateway.

Every device gets a long-running async task that rebuilds its
:class:`EquipmentStatus` envelope on a fixed interval. The `/status`
routes serve from the cache rather than calling the device on every
request, so dashboard fan-out is decoupled from the per-device
round-trip cost (3-4 s for an HS300 emeter sweep, 1-2 s for an ONVIF
SOAP call).

Lifecycle is driven by ``main.lifespan``: pollers start once the
registry is built and are cancelled on shutdown. Control endpoints
that mutate device state can call :meth:`DevicePoller.request_refresh`
to wake the poller immediately; the in-cache envelope catches up
within one poll cycle.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from kasa_tapo_services.models import EquipmentStatus

logger = logging.getLogger(__name__)


@dataclass
class _CacheEntry:
    status: EquipmentStatus
    fetched_at: float  # time.monotonic()


class StatusCache:
    """In-memory dict of device_id -> latest EquipmentStatus.

    asyncio is single-threaded so dict reads/writes don't need locking;
    we keep the API explicit (``put`` / ``get`` / ``age_seconds``) so
    callers don't accidentally treat the cache as a plain dict.
    """

    def __init__(self) -> None:
        self._entries: dict[str, _CacheEntry] = {}

    def put(self, device_id: str, status: EquipmentStatus) -> None:
        self._entries[device_id] = _CacheEntry(status=status, fetched_at=time.monotonic())

    def get(self, device_id: str) -> EquipmentStatus | None:
        entry = self._entries.get(device_id)
        return entry.status if entry else None

    def age_seconds(self, device_id: str) -> float | None:
        entry = self._entries.get(device_id)
        return time.monotonic() - entry.fetched_at if entry else None


StatusBuilder = Callable[[], Awaitable[EquipmentStatus]]


class DevicePoller:
    """One async task that periodically refreshes a single device's envelope.

    The builder callable is invoked once per ``interval_s`` (or sooner if
    :meth:`request_refresh` is called). Builder failures are logged and
    swallowed: the cache keeps its previous envelope so a single device
    hiccup doesn't blank the dashboard tile.
    """

    def __init__(
        self,
        device_id: str,
        interval_s: float,
        builder: StatusBuilder,
        cache: StatusCache,
    ) -> None:
        self._device_id = device_id
        self._interval_s = interval_s
        self._builder = builder
        self._cache = cache
        self._refresh_event = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name=f"poll-{self._device_id}")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    def request_refresh(self) -> None:
        """Wake the poll task to refresh immediately instead of waiting out the interval."""

        self._refresh_event.set()

    async def _run(self) -> None:
        while True:
            try:
                status = await self._builder()
                self._cache.put(self._device_id, status)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("poll %s failed: %s", self._device_id, exc)
            try:
                await asyncio.wait_for(self._refresh_event.wait(), timeout=self._interval_s)
            except asyncio.TimeoutError:
                pass
            self._refresh_event.clear()


__all__ = ["DevicePoller", "StatusBuilder", "StatusCache"]
