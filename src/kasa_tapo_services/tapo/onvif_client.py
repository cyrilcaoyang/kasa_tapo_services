"""ONVIF Profile S/T client for PTZ + presets.

Tapo C-series cameras expose an ONVIF Profile T (PTZ) service on the port
configured under "Settings → Advanced → ONVIF" in the Tapo app (default
``2020``). We use it for:

* :meth:`OnvifCameraClient.continuous_move` - mousedown/mouseup PTZ
* :meth:`OnvifCameraClient.stop` - stops a continuous move
* :attr:`OnvifCameraClient.has_zoom` - whether the PTZ node advertises a
  zoom axis at all (read from ``GetNodes`` at connect time). Tapo's
  dual-lens units (C245D, C246D) do **not**: their "zoom" is the fixed wide
  lens vs. the tele lens on the PTZ head. A non-zero zoom velocity on such
  a camera raises :class:`ZoomUnsupportedError` instead of being silently
  ignored by the firmware.
* :meth:`OnvifCameraClient.list_presets` - GetPresets
* :meth:`OnvifCameraClient.save_preset` - SetPreset
* :meth:`OnvifCameraClient.goto_preset` - GotoPreset
* :meth:`OnvifCameraClient.delete_preset` - RemovePreset

The :pypi:`onvif-zeep-async` library is async-native; we drive it directly.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any

from kasa_tapo_services.models import PresetEntry

logger = logging.getLogger(__name__)

# PTZ-limit detection tuning (validated live on Tapo C245D, 2026-07-14):
# position reads in the ONVIF generic space [-1, 1] have ~1e-3 granularity
# and pin exactly at the range edge when the head hits a physical limit,
# while ``MoveStatus`` stays ``UNKNOWN`` — so a before/after position delta
# is the only reliable limit signal on this hardware.
_LIMIT_EPSILON = 0.005
# |velocity| * duration_ms below which a commanded move is too small to
# observe (~0.03 generic units on a C245) — skip detection rather than
# false-positive a "limit" on a no-op nudge.
_MIN_DETECTABLE_CMD = 40.0
# Seconds to let the reported position settle after Stop before re-reading.
_POSITION_SETTLE_S = 0.3


@dataclass(frozen=True)
class PtzNudgeOutcome:
    """Result of a :meth:`OnvifCameraClient.nudge`.

    ``limited_axes`` lists commanded axes whose position did not change —
    the head is at that axis's physical pan/tilt limit. ``detected`` is
    False when detection could not run (position unreadable, or the
    commanded move was too small to observe); the nudge itself still ran.
    """

    limited_axes: tuple[str, ...] = ()
    detected: bool = False

    @property
    def limit_hit(self) -> bool:
        return bool(self.limited_axes)


class OnvifError(RuntimeError):
    """Raised when an ONVIF call fails - swallowed by callers and surfaced
    via ``last_error`` in the status envelope.
    """


class PresetCapacityError(OnvifError):
    """The camera cannot allocate another saved position."""


class ZoomUnsupportedError(OnvifError):
    """A zoom velocity was commanded on a camera whose PTZ node has no zoom
    axis. Routes map this to HTTP 409 (a fixed property of the hardware,
    not a fault and not a transient precondition)."""


class OnvifCameraClient:
    """One ONVIF camera. Connects lazily, holds the PTZ + Media services."""

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
    ) -> None:
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._cam: Any = None
        self._ptz: Any = None
        self._has_zoom = False
        self._media_token: str | None = None
        self._lock = asyncio.Lock()

    @property
    def has_ptz(self) -> bool:
        """True once connected to a camera that exposes a PTZ service.

        Fixed cameras (e.g. Tapo C100) answer ONVIF device/media calls but
        have no PTZ service; they are still "reachable".
        """

        return self._ptz is not None

    @property
    def has_zoom(self) -> bool:
        """True once connected to a PTZ node that advertises a continuous
        zoom velocity space. False for fixed cameras, for PTZ heads without
        a zoom axis (every Tapo dual-lens unit probed so far), and before
        the first connect."""

        return self._ptz is not None and self._has_zoom

    async def _connect(self) -> None:
        if self._cam is not None:
            return
        async with self._lock:
            if self._cam is not None:
                return
            try:
                import onvif as _onvif_pkg  # type: ignore[import-untyped]
                from onvif import ONVIFCamera  # type: ignore[import-untyped]
            except ImportError as exc:
                raise OnvifError(
                    "onvif-zeep-async is not installed; install kasa-tapo-services"
                    " with the cameras extra"
                ) from exc

            # onvif-zeep-async's default wsdl path walks up two directories
            # from `onvif/client.py` and ends up looking at
            # `<site-packages>/wsdl/`, but the WSDL files actually ship
            # inside the package at `<site-packages>/onvif/wsdl/`. Pass
            # the right directory explicitly so we don't depend on the
            # broken default.
            wsdl_dir = os.path.join(os.path.dirname(_onvif_pkg.__file__), "wsdl")
            cam = ONVIFCamera(
                self._host,
                self._port,
                self._user,
                self._password,
                wsdl_dir=wsdl_dir,
            )
            await cam.update_xaddrs()
            # Fixed cameras (Tapo C100/C110/...) have no PTZ service; the
            # library refuses create_ptz_service with "Device doesn`t
            # support service: ptz". Treat that as "reachable, no PTZ"
            # instead of failing the whole connection. Any other PTZ
            # bring-up failure still propagates.
            try:
                ptz = await cam.create_ptz_service()
            except Exception as exc:
                if "support service" not in str(exc).lower():
                    raise
                logger.info(
                    "ONVIF %s:%s has no PTZ service (fixed camera); PTZ disabled",
                    self._host,
                    self._port,
                )
                ptz = None
            has_zoom = await self._detect_zoom_axis(ptz) if ptz is not None else False
            media = await cam.create_media_service()
            profiles = await media.GetProfiles()
            if not profiles:
                raise OnvifError(f"ONVIF: camera {self._host} returned no media profiles")
            self._cam = cam
            self._ptz = ptz
            self._has_zoom = has_zoom
            self._media_token = profiles[0].token

    async def _detect_zoom_axis(self, ptz: Any) -> bool:
        """Ask the PTZ node whether it has a zoom axis.

        ONVIF advertises each axis as a *space*; a node with no
        ``ContinuousZoomVelocitySpace`` cannot execute a zoom velocity (the
        C245D / C246D also report ``Position.Zoom`` as absent). A failed
        ``GetNodes`` is treated as "no zoom" rather than failing the connect
        - pan/tilt still work, and advertising an axis we cannot confirm is
        the worse error.
        """

        try:
            nodes = await ptz.GetNodes()
        except Exception as exc:
            logger.debug("ONVIF GetNodes failed for %s:%s: %s", self._host, self._port, exc)
            return False
        for node in nodes or []:
            spaces = getattr(node, "SupportedPTZSpaces", None)
            if getattr(spaces, "ContinuousZoomVelocitySpace", None):
                return True
        logger.info("ONVIF %s:%s PTZ node has no zoom axis; zoom disabled", self._host, self._port)
        return False

    async def close(self) -> None:
        """Best-effort teardown - the underlying zeep client lacks an explicit close."""

        self._cam = None
        self._ptz = None
        self._has_zoom = False
        self._media_token = None

    # -- Reachability probe -----------------------------------------------

    async def is_reachable(self) -> bool:
        try:
            await self._connect()
            # A cached client does not prove the camera is still online.
            service = await self._cam.create_devicemgmt_service()
            await asyncio.wait_for(service.GetDeviceInformation(), timeout=5.0)
            return True
        except Exception as exc:
            logger.debug("ONVIF probe %s:%s failed: %s", self._host, self._port, exc)
            await self.close()
            return False

    # -- PTZ ---------------------------------------------------------------

    async def continuous_move(
        self,
        pan: float = 0.0,
        tilt: float = 0.0,
        zoom: float = 0.0,
        duration_ms: int | None = None,
    ) -> None:
        """Start a continuous move. Call :meth:`stop` to halt.

        ``pan``/``tilt``/``zoom`` are in [-1.0, 1.0]. If ``duration_ms`` is
        set, schedule a ``stop`` after that many milliseconds.

        Raises :class:`ZoomUnsupportedError` for a non-zero ``zoom`` when the
        PTZ node has no zoom axis (see :attr:`has_zoom`).
        """

        await self._connect()
        self._require_ptz()
        if zoom and not self._has_zoom:
            raise ZoomUnsupportedError(
                f"camera {self._host} has no zoom axis (its ONVIF PTZ node advertises no zoom space)"
            )
        assert self._media_token is not None

        request = self._ptz.create_type("ContinuousMove")
        request.ProfileToken = self._media_token
        request.Velocity = {
            "PanTilt": {"x": float(pan), "y": float(tilt)},
            "Zoom": {"x": float(zoom)},
        }
        await self._ptz.ContinuousMove(request)

        if duration_ms and duration_ms > 0:
            asyncio.get_running_loop().call_later(
                duration_ms / 1000.0,
                lambda: asyncio.create_task(self._safe_stop()),
            )

    def _require_ptz(self) -> None:
        if self._ptz is None:
            raise OnvifError(
                f"camera {self._host} has no PTZ service (fixed lens)"
            )

    async def stop(self) -> None:
        await self._connect()
        self._require_ptz()
        assert self._media_token is not None
        request = self._ptz.create_type("Stop")
        request.ProfileToken = self._media_token
        request.PanTilt = True
        request.Zoom = True
        await self._ptz.Stop(request)

    async def _safe_stop(self) -> None:
        try:
            await self.stop()
        except Exception as exc:  # pragma: no cover - best-effort cleanup
            logger.warning("ONVIF deferred stop failed: %s", exc)

    async def get_position(self) -> tuple[float, float] | None:
        """Current (pan, tilt) in the ONVIF generic space, or None.

        Returns None instead of raising when the camera doesn't report a
        position (or the read fails) so callers can degrade gracefully.
        """

        try:
            await self._connect()
            self._require_ptz()
            assert self._media_token is not None
            status = await self._ptz.GetStatus({"ProfileToken": self._media_token})
            pan_tilt = getattr(getattr(status, "Position", None), "PanTilt", None)
            if pan_tilt is None:
                return None
            return float(pan_tilt.x), float(pan_tilt.y)
        except Exception as exc:
            logger.debug("ONVIF GetStatus position read failed: %s", exc)
            return None

    async def nudge(self, pan: float, tilt: float, zoom: float, duration_ms: int) -> PtzNudgeOutcome:
        """Start a continuous move and stop it after ``duration_ms``.

        Awaits the stop so the call returns only after the move is complete.
        Reads the PTZ position before and after: a commanded axis whose
        position did not change is at its physical limit (see the module
        constants for why the delta is the only reliable signal on Tapo
        hardware).
        """

        # Limit detection covers pan/tilt only: ``GetStatus`` reports no zoom
        # position on the hardware this was validated against, so a zoom-only
        # nudge simply runs undetected (``detected=False``).
        watched = [
            axis
            for axis, velocity in (("pan", pan), ("tilt", tilt))
            if abs(velocity) * duration_ms >= _MIN_DETECTABLE_CMD
        ]
        before = await self.get_position() if watched else None

        await self.continuous_move(pan=pan, tilt=tilt, zoom=zoom)
        await asyncio.sleep(max(0, duration_ms) / 1000.0)
        try:
            await self.stop()
        except Exception as exc:
            logger.warning("ONVIF nudge stop failed: %s", exc)
            return PtzNudgeOutcome()

        if before is None:
            return PtzNudgeOutcome()
        await asyncio.sleep(_POSITION_SETTLE_S)
        after = await self.get_position()
        if after is None:
            return PtzNudgeOutcome()
        deltas = {"pan": after[0] - before[0], "tilt": after[1] - before[1]}
        limited = tuple(axis for axis in watched if abs(deltas[axis]) < _LIMIT_EPSILON)
        return PtzNudgeOutcome(limited_axes=limited, detected=True)

    # -- Presets ----------------------------------------------------------

    async def list_presets(self) -> list[PresetEntry]:
        await self._connect()
        if self._ptz is None:
            return []  # fixed camera - no PTZ, no presets
        assert self._media_token is not None
        try:
            raw = await self._ptz.GetPresets({"ProfileToken": self._media_token})
        except Exception as exc:
            logger.warning("ONVIF GetPresets failed: %s", exc)
            return []

        out: list[PresetEntry] = []
        for entry in raw or []:
            token = getattr(entry, "token", None) or (entry.get("token") if isinstance(entry, dict) else None)
            name = getattr(entry, "Name", None) or (entry.get("Name") if isinstance(entry, dict) else None)
            if token is None:
                continue
            out.append(PresetEntry(id=str(token), name=str(name or token)))
        return out

    async def save_preset(self, name: str) -> str:
        await self._connect()
        self._require_ptz()
        assert self._media_token is not None
        request = self._ptz.create_type("SetPreset")
        request.ProfileToken = self._media_token
        request.PresetName = name
        try:
            result = await self._ptz.SetPreset(request)
        except Exception as exc:
            # C245D firmware reports this SOAP fault when its preset slots
            # are full. Do not delete or overwrite a position implicitly.
            if "number of presets limit reached" in str(exc).lower():
                raise PresetCapacityError(
                    "Camera preset storage is full. Delete an unused preset "
                    "and then save again. Existing presets have not been changed."
                ) from exc
            raise
        # ``SetPreset`` returns the assigned token (string in Profile T,
        # an object with a ``PresetToken`` attribute on some stacks).
        if hasattr(result, "PresetToken"):
            return str(result.PresetToken)
        if isinstance(result, str):
            return result
        return str(result)

    async def goto_preset(self, preset_id: str) -> None:
        await self._connect()
        self._require_ptz()
        assert self._media_token is not None
        request = self._ptz.create_type("GotoPreset")
        request.ProfileToken = self._media_token
        request.PresetToken = preset_id
        await self._ptz.GotoPreset(request)

    async def delete_preset(self, preset_id: str) -> None:
        await self._connect()
        self._require_ptz()
        assert self._media_token is not None
        request = self._ptz.create_type("RemovePreset")
        request.ProfileToken = self._media_token
        request.PresetToken = preset_id
        await self._ptz.RemovePreset(request)


__all__ = [
    "OnvifCameraClient",
    "OnvifError",
    "PresetCapacityError",
    "PtzNudgeOutcome",
    "ZoomUnsupportedError",
]
