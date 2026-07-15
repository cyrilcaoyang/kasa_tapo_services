"""ONVIF Profile S/T client for PTZ + presets.

Tapo C-series cameras expose an ONVIF Profile T (PTZ) service on the port
configured under "Settings → Advanced → ONVIF" in the Tapo app (default
``2020``). We use it for:

* :meth:`OnvifCameraClient.continuous_move` - mousedown/mouseup PTZ
* :meth:`OnvifCameraClient.stop` - stops a continuous move
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
        self._media_token: str | None = None
        self._lock = asyncio.Lock()

    async def _connect(self) -> None:
        if self._cam is not None and self._ptz is not None:
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
            ptz = await cam.create_ptz_service()
            media = await cam.create_media_service()
            profiles = await media.GetProfiles()
            if not profiles:
                raise OnvifError(f"ONVIF: camera {self._host} returned no media profiles")
            self._cam = cam
            self._ptz = ptz
            self._media_token = profiles[0].token

    async def close(self) -> None:
        """Best-effort teardown - the underlying zeep client lacks an explicit close."""

        self._cam = None
        self._ptz = None
        self._media_token = None

    # -- Reachability probe -----------------------------------------------

    async def is_reachable(self) -> bool:
        try:
            await self._connect()
            return True
        except Exception as exc:
            logger.debug("ONVIF probe %s:%s failed: %s", self._host, self._port, exc)
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
        """

        await self._connect()
        assert self._ptz is not None and self._media_token is not None

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

    async def stop(self) -> None:
        await self._connect()
        assert self._ptz is not None and self._media_token is not None
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
            assert self._ptz is not None and self._media_token is not None
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
        assert self._ptz is not None and self._media_token is not None
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
        assert self._ptz is not None and self._media_token is not None
        request = self._ptz.create_type("SetPreset")
        request.ProfileToken = self._media_token
        request.PresetName = name
        result = await self._ptz.SetPreset(request)
        # ``SetPreset`` returns the assigned token (string in Profile T,
        # an object with a ``PresetToken`` attribute on some stacks).
        if hasattr(result, "PresetToken"):
            return str(result.PresetToken)
        if isinstance(result, str):
            return result
        return str(result)

    async def goto_preset(self, preset_id: str) -> None:
        await self._connect()
        assert self._ptz is not None and self._media_token is not None
        request = self._ptz.create_type("GotoPreset")
        request.ProfileToken = self._media_token
        request.PresetToken = preset_id
        await self._ptz.GotoPreset(request)

    async def delete_preset(self, preset_id: str) -> None:
        await self._connect()
        assert self._ptz is not None and self._media_token is not None
        request = self._ptz.create_type("RemovePreset")
        request.ProfileToken = self._media_token
        request.PresetToken = preset_id
        await self._ptz.RemovePreset(request)


__all__ = ["OnvifCameraClient", "OnvifError", "PtzNudgeOutcome"]
