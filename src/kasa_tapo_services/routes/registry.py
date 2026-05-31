"""Process-wide registry of live device clients.

The FastAPI app instantiates one :class:`DeviceRegistry` at startup. The
registry owns one client per device (``KasaPlugClient`` / ``TapoCameraClient`` /
``OnvifCameraClient``) plus the shared go2rtc client. Routes pull what
they need from the registry as a dependency.

We use a tiny custom registry (rather than a ``request.app.state.<...>``
attribute pile) so tests can override it with a stub - especially handy
for the ``test_routes.py`` suite which never touches real hardware.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from fastapi import HTTPException, Request

from kasa_tapo_services.config import (
    DeviceConfig,
    GatewayConfig,
    device_credentials,
    load_config,
)
from kasa_tapo_services.kasa import KasaPlugClient
from kasa_tapo_services.poller import DevicePoller, StatusCache
from kasa_tapo_services.tapo import Go2RtcClient, OnvifCameraClient, TapoCameraClient
from kasa_tapo_services.tapo.media import CameraMediaManager, RecordingHandle
from kasa_tapo_services.tapo.rolling_recorder import RollingRecorder

logger = logging.getLogger(__name__)


@dataclass
class CameraClients:
    """Per-camera bundle of backend clients.

    pytapo + ONVIF are independent - one camera may have either, neither,
    or both. The bundle is created up-front and the routes ask each
    member whether it is reachable before using it.

    ``streaming_enabled`` is an in-memory flag flipped by the dashboard's
    ``/control/streaming`` toggle. It controls whether the gateway
    advertises the lens MSE URLs to the frontend. Streams remain
    statically configured in ``go2rtc.yaml`` regardless - go2rtc only
    pulls from the RTSP source on demand (when a consumer subscribes),
    so leaving them registered while ``streaming_enabled=False`` does
    not cost any bandwidth or CPU.
    """

    config: DeviceConfig
    tapo: TapoCameraClient | None = None
    onvif: OnvifCameraClient | None = None
    started_at: float | None = field(default=None)
    streaming_enabled: bool = True
    # Active server-side recordings keyed by ``recording_id``. Most
    # cameras will have at most one active recording at a time, but
    # the dict shape lets future per-lens parallel recordings work
    # without a schema change.
    recordings: dict[str, RecordingHandle] = field(default_factory=dict)
    # One rolling recorder per lens (keyed by resolved lens_id). None means
    # no rolling recorder is active for that lens.
    rolling: dict[str, RollingRecorder] = field(default_factory=dict)


@dataclass
class PlugClients:
    config: DeviceConfig
    kasa: KasaPlugClient
    started_at: float | None = field(default=None)


class DeviceRegistry:
    """One process-wide registry; constructed in :func:`main.lifespan`."""

    # Default poll cadences. Plugs are fast-changing (operator-driven
    # outlet toggles) so we refresh frequently; cameras are slower per
    # cycle (ONVIF SOAP) and their state changes rarely, so a longer
    # interval is enough.
    DEFAULT_PLUG_POLL_INTERVAL_S: float = 2.0
    DEFAULT_CAMERA_POLL_INTERVAL_S: float = 5.0

    def __init__(self, config: GatewayConfig) -> None:
        self._config = config
        self._cameras: dict[str, CameraClients] = {}
        self._plugs: dict[str, PlugClients] = {}
        self._go2rtc = Go2RtcClient()
        self._media = CameraMediaManager()
        self._status_cache = StatusCache()
        self._pollers: dict[str, DevicePoller] = {}
        self._build()

    def _build(self) -> None:
        for device in self._config.devices:
            if not device.enabled:
                continue
            if device.kind == "camera":
                self._cameras[device.id] = self._build_camera(device)
            else:
                self._plugs[device.id] = self._build_plug(device)

    def _build_camera(self, cfg: DeviceConfig) -> CameraClients:
        creds = device_credentials(cfg.id)
        tapo: TapoCameraClient | None = None
        onvif: OnvifCameraClient | None = None
        if creds.has_basic:
            tapo = TapoCameraClient(cfg.host, creds.user, creds.password)
        else:
            logger.warning("camera %s: missing %s_USER/_PASS env, pytapo disabled", cfg.id, cfg.id.upper())
        if creds.effective_onvif_user and creds.effective_onvif_password:
            onvif = OnvifCameraClient(
                cfg.host,
                cfg.onvif_port,
                creds.effective_onvif_user,
                creds.effective_onvif_password,
            )
        else:
            logger.warning(
                "camera %s: missing ONVIF credentials, PTZ + presets disabled",
                cfg.id,
            )
        return CameraClients(config=cfg, tapo=tapo, onvif=onvif)

    def _build_plug(self, cfg: DeviceConfig) -> PlugClients:
        creds = device_credentials(cfg.id)
        client = KasaPlugClient(cfg.host, username=creds.user, password=creds.password)
        return PlugClients(config=cfg, kasa=client)

    @property
    def config(self) -> GatewayConfig:
        return self._config

    @property
    def go2rtc(self) -> Go2RtcClient:
        return self._go2rtc

    @property
    def media(self) -> CameraMediaManager:
        return self._media

    @property
    def status_cache(self) -> StatusCache:
        return self._status_cache

    def poller(self, device_id: str) -> DevicePoller | None:
        """Return the background poller for a device, or None when none is
        running (e.g. under the test harness's stub registry)."""

        return self._pollers.get(device_id)

    def camera(self, device_id: str) -> CameraClients:
        bundle = self._cameras.get(device_id)
        if bundle is None:
            raise HTTPException(status_code=404, detail=f"Unknown camera: {device_id}")
        return bundle

    def plug(self, device_id: str) -> PlugClients:
        bundle = self._plugs.get(device_id)
        if bundle is None:
            raise HTTPException(status_code=404, detail=f"Unknown plug: {device_id}")
        return bundle

    def list_cameras(self) -> list[CameraClients]:
        return list(self._cameras.values())

    def list_plugs(self) -> list[PlugClients]:
        return list(self._plugs.values())

    def start_pollers(
        self,
        *,
        plug_interval_s: float | None = None,
        camera_interval_s: float | None = None,
    ) -> None:
        """Spawn one :class:`DevicePoller` per device.

        Builders are imported lazily here because the routes modules
        depend on :class:`DeviceRegistry`, so the inverse direction would
        be a circular import at module load time.
        """

        # Local import to break the circular dependency with routes.*.
        from kasa_tapo_services.routes.cameras import _build_status as _build_camera_status
        from kasa_tapo_services.routes.plugs import _build_status as _build_plug_status

        plug_interval = plug_interval_s or self.DEFAULT_PLUG_POLL_INTERVAL_S
        camera_interval = camera_interval_s or self.DEFAULT_CAMERA_POLL_INTERVAL_S

        for device_id, bundle in self._plugs.items():
            if device_id in self._pollers:
                continue

            async def _build(bundle=bundle):
                return await _build_plug_status(bundle)

            poller = DevicePoller(
                device_id=device_id,
                interval_s=plug_interval,
                builder=_build,
                cache=self._status_cache,
            )
            poller.start()
            self._pollers[device_id] = poller

        for device_id, bundle in self._cameras.items():
            if device_id in self._pollers:
                continue

            async def _build(bundle=bundle):
                return await _build_camera_status(bundle, self)

            poller = DevicePoller(
                device_id=device_id,
                interval_s=camera_interval,
                builder=_build,
                cache=self._status_cache,
            )
            poller.start()
            self._pollers[device_id] = poller

        if self._pollers:
            logger.info(
                "started %d device poller(s) (plug %.1fs, camera %.1fs)",
                len(self._pollers),
                plug_interval,
                camera_interval,
            )

    async def stop_pollers(self) -> None:
        if not self._pollers:
            return
        await asyncio.gather(
            *(poller.stop() for poller in self._pollers.values()),
            return_exceptions=True,
        )
        self._pollers.clear()

    async def aclose(self) -> None:
        # Stop background pollers first so they cannot race with the
        # connection teardown below by trying to drive a device whose
        # underlying client has just been closed.
        await self.stop_pollers()

        # Stop rolling recorders first so they don't start a new segment
        # while we're shutting down manual recordings.
        rolling_coros: list = []
        for cam in self._cameras.values():
            for recorder in list(cam.rolling.values()):
                if recorder.is_running:
                    rolling_coros.append(recorder.stop())
        if rolling_coros:
            logger.info("aclose: stopping %d rolling recorder(s)", len(rolling_coros))
            await asyncio.gather(*rolling_coros, return_exceptions=True)
        for cam in self._cameras.values():
            cam.rolling.clear()

        # Stop active recordings - we want their .partial files
        # finalised to .mp4 before any other shutdown work happens, and
        # we want to do it in parallel so a single hung ffmpeg can't
        # block the whole shutdown.
        recording_coros: list = []
        for cam in self._cameras.values():
            for handle in list(cam.recordings.values()):
                recording_coros.append(self._media.stop_recording(handle))
        if recording_coros:
            logger.info("aclose: stopping %d active recording(s)", len(recording_coros))
            await asyncio.gather(*recording_coros, return_exceptions=True)
            for cam in self._cameras.values():
                cam.recordings.clear()

        coros = [self._go2rtc.close()]
        for cam in self._cameras.values():
            if cam.tapo is not None:
                coros.append(cam.tapo.close())
            if cam.onvif is not None:
                coros.append(cam.onvif.close())
        for plug in self._plugs.values():
            coros.append(plug.kasa.close())
        await asyncio.gather(*coros, return_exceptions=True)


def get_registry(request: Request) -> DeviceRegistry:
    """FastAPI dependency: pull the registry off ``app.state``."""

    registry: DeviceRegistry | None = getattr(request.app.state, "registry", None)
    if registry is None:
        raise HTTPException(status_code=503, detail="Gateway not initialised")
    return registry


async def build_registry_from_disk() -> DeviceRegistry:
    """Convenience constructor used by ``main.lifespan``."""

    config = load_config()
    return DeviceRegistry(config)


__all__ = [
    "CameraClients",
    "DeviceRegistry",
    "PlugClients",
    "build_registry_from_disk",
    "get_registry",
]
