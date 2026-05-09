"""Async wrapper around ``python-kasa`` for HS103 + HS300.

``python-kasa`` is fully async; this thin wrapper exists so the routes
layer doesn't have to know about reconnect-on-failure or the difference
between a single plug and a multi-outlet strip.

The library's modern API (``kasa>=0.7``) returns a ``Device`` polymorphic
object after :meth:`Device.connect`. We treat both kinds uniformly: a
single-plug device exposes a ``.children`` list of length 0; a power
strip exposes one child per outlet.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OutletState:
    index: int
    label: str | None
    is_on: bool
    power_w: float | None = None
    energy_wh_today: float | None = None


@dataclass(frozen=True)
class PlugState:
    """Snapshot of a Kasa plug or strip at one point in time."""

    is_on: bool  # whole-device aggregate (any outlet on for strips)
    model: str | None
    alias: str | None
    rssi: int | None
    outlets: list[OutletState]
    is_strip: bool


class KasaPlugClient:
    """One Kasa device, identified by its lab-LAN IP.

    Connect/refresh is lazy. python-kasa's ``Device`` keeps an internal
    socket which can hiccup; on any error we drop the cached device and
    retry on the next call.
    """

    def __init__(self, host: str, *, username: str | None = None, password: str | None = None) -> None:
        self._host = host
        self._username = username
        self._password = password
        self._device: Any = None
        self._lock = asyncio.Lock()

    async def _get(self) -> Any:
        if self._device is not None:
            return self._device
        async with self._lock:
            if self._device is None:
                from kasa import Device  # type: ignore

                if self._username and self._password:
                    self._device = await Device.connect(
                        host=self._host,
                        credentials=_credentials(self._username, self._password),
                    )
                else:
                    self._device = await Device.connect(host=self._host)
        return self._device

    async def _drop(self) -> None:
        device, self._device = self._device, None
        if device is None:
            return
        try:
            disconnect = getattr(device, "disconnect", None)
            if disconnect is not None:
                await disconnect()
        except Exception:  # pragma: no cover - best-effort cleanup
            pass

    async def close(self) -> None:
        await self._drop()

    async def update(self) -> Any:
        device = await self._get()
        try:
            await device.update()
        except Exception as exc:
            logger.debug("kasa update %s failed, reconnecting: %s", self._host, exc)
            await self._drop()
            device = await self._get()
            await device.update()
        return device

    # -- Read --------------------------------------------------------------

    async def state(self) -> PlugState:
        device = await self.update()
        children = list(getattr(device, "children", []) or [])
        is_strip = len(children) > 0
        outlets: list[OutletState] = []
        if is_strip:
            for idx, child in enumerate(children):
                outlets.append(
                    OutletState(
                        index=idx,
                        label=getattr(child, "alias", None),
                        is_on=bool(getattr(child, "is_on", False)),
                        power_w=_safe_float(_get_emeter_attr(child, "power")),
                        energy_wh_today=_safe_float(_get_emeter_attr(child, "today_energy")),
                    )
                )
            aggregate_on = any(o.is_on for o in outlets)
        else:
            aggregate_on = bool(getattr(device, "is_on", False))

        rssi: int | None = None
        try:
            rssi_val = device.rssi if hasattr(device, "rssi") else None
            rssi = int(rssi_val) if rssi_val is not None else None
        except Exception:  # pragma: no cover - device-specific attribute
            rssi = None

        return PlugState(
            is_on=aggregate_on,
            model=getattr(device, "model", None) or getattr(device, "device_type", None),
            alias=getattr(device, "alias", None),
            rssi=rssi,
            outlets=outlets,
            is_strip=is_strip,
        )

    async def is_reachable(self) -> bool:
        try:
            await self.update()
            return True
        except Exception:
            return False

    # -- Write -------------------------------------------------------------

    async def turn_on(self, outlet: int | None = None) -> None:
        device = await self.update()
        target = self._target(device, outlet)
        await target.turn_on()
        # python-kasa caches the on/off state; refresh so subsequent
        # reads see ground truth.
        try:
            await device.update()
        except Exception:  # pragma: no cover - benign refresh
            pass

    async def turn_off(self, outlet: int | None = None) -> None:
        device = await self.update()
        target = self._target(device, outlet)
        await target.turn_off()
        try:
            await device.update()
        except Exception:  # pragma: no cover
            pass

    async def toggle(self, outlet: int | None = None) -> bool:
        """Flip the outlet (or whole strip) on/off. Returns the new state."""

        device = await self.update()
        target = self._target(device, outlet)
        if getattr(target, "is_on", False):
            await target.turn_off()
            return False
        await target.turn_on()
        return True

    @staticmethod
    def _target(device: Any, outlet: int | None) -> Any:
        if outlet is None:
            return device
        children = list(getattr(device, "children", []) or [])
        if not children:
            raise ValueError("This device has no addressable outlets - omit `outlet`")
        if outlet < 0 or outlet >= len(children):
            raise ValueError(f"outlet={outlet} out of range for {len(children)} outlets")
        return children[outlet]


def _credentials(username: str, password: str) -> Any:  # pragma: no cover - thin wrapper
    """Build a python-kasa ``Credentials`` object lazily.

    Centralised so callers don't need to import ``kasa.Credentials``
    when they don't have credentials.
    """

    from kasa import Credentials  # type: ignore

    return Credentials(username, password)


def _get_emeter_attr(device: Any, attr: str) -> Any:
    """python-kasa's emeter attributes live on either the device itself
    (older versions) or a ``device.emeter_realtime`` mapping (newer).

    Returns ``None`` for devices without an energy meter.
    """

    realtime = getattr(device, "emeter_realtime", None)
    if realtime is not None:
        try:
            return realtime[attr]
        except Exception:
            return getattr(realtime, attr, None)
    return getattr(device, attr, None)


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = ["KasaPlugClient", "OutletState", "PlugState"]
