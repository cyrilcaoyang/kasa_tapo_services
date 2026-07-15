"""Shared pytest fixtures: a stub registry and a TestClient app."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kasa_tapo_services.config import GatewayConfig, MediaConfig
from kasa_tapo_services.kasa.plug_client import OutletState, PlugState
from kasa_tapo_services.models import PresetEntry
from kasa_tapo_services.poller import StatusCache
from kasa_tapo_services.routes import build_camera_router, build_plug_router
from kasa_tapo_services.routes.registry import CameraClients, DeviceRegistry, PlugClients
from kasa_tapo_services.tapo.media import RecordingHandle
from kasa_tapo_services.tapo.onvif_client import PtzNudgeOutcome

FIXTURES = Path(__file__).parent / "fixtures"


def _stub_camera_clients(cfg) -> CameraClients:
    tapo = AsyncMock()
    tapo.privacy_mode = AsyncMock(return_value=False)
    tapo.set_privacy_mode = AsyncMock()
    tapo.set_day_night = AsyncMock()
    tapo.close = AsyncMock()

    onvif = AsyncMock()
    onvif.is_reachable = AsyncMock(return_value=True)
    onvif.continuous_move = AsyncMock()
    onvif.stop = AsyncMock()
    onvif.nudge = AsyncMock(return_value=PtzNudgeOutcome(detected=True))
    onvif.get_position = AsyncMock(return_value=(0.0, 0.0))
    onvif.list_presets = AsyncMock(return_value=[
        PresetEntry(id="1", name="home"),
        PresetEntry(id="2", name="bench"),
    ])
    onvif.save_preset = AsyncMock(return_value="9")
    onvif.goto_preset = AsyncMock()
    onvif.delete_preset = AsyncMock()
    onvif.close = AsyncMock()
    return CameraClients(config=cfg, tapo=tapo, onvif=onvif)


def _stub_plug_clients(cfg) -> PlugClients:
    kasa = AsyncMock()
    if cfg.kind == "power_strip":
        kasa.state = AsyncMock(
            return_value=PlugState(
                is_on=True,
                model="HS300",
                alias=cfg.name,
                rssi=-55,
                outlets=[
                    OutletState(index=i, label=None, is_on=(i % 2 == 0))
                    for i in range(6)
                ],
                is_strip=True,
            )
        )
    else:
        kasa.state = AsyncMock(
            return_value=PlugState(
                is_on=False,
                model="HS103",
                alias=cfg.name,
                rssi=-60,
                outlets=[],
                is_strip=False,
            )
        )
    kasa.turn_on = AsyncMock()
    kasa.turn_off = AsyncMock()
    kasa.toggle = AsyncMock(return_value=True)
    kasa.is_reachable = AsyncMock(return_value=True)
    kasa.close = AsyncMock()
    return PlugClients(config=cfg, kasa=kasa)


def _stub_media_manager(tmp_path: Path) -> MagicMock:
    """A fake :class:`CameraMediaManager` that touches files instead of ffmpeg.

    Snapshots write a tiny ``b"jpg"`` payload; recordings create a
    ``.partial`` file and return a stub ``RecordingHandle`` whose
    ``process`` is also mocked so ``stop`` / ``cancel`` behave like the
    real thing without ever spawning a child process.
    """

    media = MagicMock(spec_set=("ffmpeg", "take_snapshot", "start_recording", "stop_recording", "cancel_recording"))

    def _validate_lens(camera, lens_id):
        if lens_id is None:
            if not camera.lenses:
                raise ValueError(f"Camera {camera.id!r} has no lenses configured")
            return camera.lenses[0].id
        for lens in camera.lenses or []:
            if lens.id == lens_id:
                return lens.id
        raise ValueError(
            f"Camera {camera.id!r} has no lens id={lens_id!r}"
        )

    async def _take_snapshot(*, camera, creds, lens_id=None, timeout_s=8.0):  # noqa: ARG001
        del creds, timeout_s
        lens = _validate_lens(camera, lens_id)
        target_dir = tmp_path / "snapshots" / camera.id / lens
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "stub.jpg"
        target.write_bytes(b"jpg")
        return target, lens, lens

    def _make_handle(camera, lens_id, max_duration_s):
        lens = _validate_lens(camera, lens_id)
        target_dir = tmp_path / "recordings" / camera.id / lens
        target_dir.mkdir(parents=True, exist_ok=True)
        partial = target_dir / "stub.mp4.partial"
        partial.write_bytes(b"mp4-partial")
        proc = MagicMock()
        proc.returncode = None
        proc.send_signal = MagicMock()
        proc.kill = MagicMock()

        async def _wait():
            proc.returncode = 0

        proc.wait = AsyncMock(side_effect=_wait)
        return RecordingHandle(
            recording_id="stub" + lens,
            camera_id=camera.id,
            lens_id=lens,
            started_at=datetime.now(tz=timezone.utc),
            target_path=partial.with_suffix(""),
            partial_path=partial,
            process=proc,
            max_duration_s=max_duration_s,
        )

    async def _start_recording(*, camera, creds, lens_id=None, max_duration_s=3600):  # noqa: ARG001
        del creds
        return _make_handle(camera, lens_id, max_duration_s)

    async def _stop_recording(handle, timeout_s=10.0):  # noqa: ARG001
        if handle.partial_path.exists():
            handle.partial_path.rename(handle.target_path)
        return handle.target_path

    async def _cancel_recording(handle, timeout_s=5.0):  # noqa: ARG001
        if handle.partial_path.exists():
            handle.partial_path.unlink()
            return handle.partial_path
        return None

    media.take_snapshot = AsyncMock(side_effect=_take_snapshot)
    media.start_recording = AsyncMock(side_effect=_start_recording)
    media.stop_recording = AsyncMock(side_effect=_stop_recording)
    media.cancel_recording = AsyncMock(side_effect=_cancel_recording)
    media.ffmpeg = "ffmpeg-stub"
    return media


class StubRegistry(DeviceRegistry):
    """Mirror of ``DeviceRegistry`` that swaps in fakes for every backend."""

    def __init__(self, config: GatewayConfig, media_root: Path) -> None:
        # Skip the parent's __init__ - we don't want it to instantiate
        # real Tapo / Kasa / ONVIF clients during tests.
        self._config = config
        self._cameras: dict[str, CameraClients] = {}
        self._plugs: dict[str, PlugClients] = {}
        from unittest.mock import AsyncMock as _AM

        go2rtc = _AM()
        go2rtc.is_reachable = _AM(return_value=True)
        go2rtc.stream_state = _AM(return_value="connected")
        go2rtc.add_stream = _AM()
        go2rtc.remove_stream = _AM()
        go2rtc.close = _AM()
        self._go2rtc = go2rtc  # type: ignore[assignment]
        self._media = _stub_media_manager(media_root)  # type: ignore[assignment]
        self._status_cache = StatusCache()
        self._pollers = {}

        for device in config.devices:
            if not device.enabled:
                continue
            if device.kind == "camera":
                self._cameras[device.id] = _stub_camera_clients(device)
            else:
                self._plugs[device.id] = _stub_plug_clients(device)

    async def aclose(self) -> None:  # pragma: no cover - test only
        return None


@pytest.fixture
def example_config() -> GatewayConfig:
    """Full ``devices.yaml`` covering every kind, used by most tests."""

    raw = yaml.safe_load((FIXTURES / "devices_example.yaml").read_text())
    return GatewayConfig.model_validate(raw)


@pytest.fixture
def stub_registry(
    example_config: GatewayConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> StubRegistry:
    # Point the cameras at a tmp media root so any test-driven
    # snapshot/recording call materialises files there.
    devices = []
    for d in example_config.devices:
        if d.kind == "camera":
            devices.append(
                d.model_copy(
                    update={
                        "media": MediaConfig(
                            snapshots_dir=str(tmp_path / "snapshots" / d.id),
                            recordings_dir=str(tmp_path / "recordings" / d.id),
                        )
                    }
                )
            )
            # Fake RTSP credentials so route-level guards in the snapshot /
            # recording handlers don't 503. The stubbed media manager
            # never actually uses these.
            monkeypatch.setenv(f"{d.id.upper()}_USER", "stub-user")
            monkeypatch.setenv(f"{d.id.upper()}_PASS", "stub-pass")
        else:
            devices.append(d)
    cfg = GatewayConfig(devices=devices)
    return StubRegistry(cfg, tmp_path)


@pytest.fixture
def app(stub_registry: StubRegistry) -> FastAPI:
    """A FastAPI app wired with the stub registry already on app.state."""

    app = FastAPI()
    app.state.registry = stub_registry
    app.state.boot_time = datetime.now(timezone.utc)
    app.include_router(build_camera_router())
    app.include_router(build_plug_router())
    return app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)
