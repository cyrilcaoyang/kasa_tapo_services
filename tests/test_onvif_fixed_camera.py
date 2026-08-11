"""Fixed (non-PTZ) cameras must still count as ONVIF-reachable.

A Tapo C100 answers ONVIF device + media calls but has no PTZ service:
``create_ptz_service`` raises "Device doesn`t support service: ptz". Before
this was handled, ``_connect`` propagated that and ``is_reachable`` returned
False, so the camera's whole status envelope read ``unknown`` /
"Camera unreachable" even though ONVIF, RTSP and go2rtc were all fine
(observed live on cam_echem_tapo_c100, 2026-08-04).

The ONVIF library is faked here so the tests need no hardware.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from kasa_tapo_services.tapo.onvif_client import OnvifCameraClient, OnvifError


class _FakeCamera:
    """Stands in for ``onvif.ONVIFCamera``.

    ``ptz_error`` is raised by ``create_ptz_service`` to model a camera
    without a PTZ service; ``None`` models a PTZ-capable one.
    """

    def __init__(self, ptz_error: Exception | None) -> None:
        self._ptz_error = ptz_error
        self.update_xaddrs = AsyncMock()

    async def create_ptz_service(self):
        if self._ptz_error is not None:
            raise self._ptz_error
        return MagicMock(name="ptz_service")

    async def create_media_service(self):
        media = MagicMock()
        profile = MagicMock()
        profile.token = "profile_1"
        media.GetProfiles = AsyncMock(return_value=[profile])
        return media


@pytest.fixture
def fake_onvif(monkeypatch: pytest.MonkeyPatch):
    """Install a fake ``onvif`` module and return a setter for the PTZ error."""

    holder: dict[str, Exception | None] = {"ptz_error": None}

    module = types.ModuleType("onvif")
    module.__file__ = "/nonexistent/onvif/__init__.py"
    module.ONVIFCamera = lambda *a, **kw: _FakeCamera(holder["ptz_error"])  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "onvif", module)

    def _set(exc: Exception | None) -> None:
        holder["ptz_error"] = exc

    return _set


# The exact message onvif-zeep-async raises for a camera with no PTZ service.
_NO_PTZ = RuntimeError(
    "Unknown error: Device doesn`t support service: ptz with namespace"
    " http://www.onvif.org/ver20/ptz/wsdl"
)


async def test_fixed_camera_is_reachable_without_ptz(fake_onvif) -> None:
    fake_onvif(_NO_PTZ)
    client = OnvifCameraClient("203.0.113.1", 2020, "u", "p")

    assert await client.is_reachable() is True
    assert client.has_ptz is False


async def test_ptz_camera_reports_ptz(fake_onvif) -> None:
    fake_onvif(None)
    client = OnvifCameraClient("203.0.113.2", 2020, "u", "p")

    assert await client.is_reachable() is True
    assert client.has_ptz is True


async def test_fixed_camera_has_no_presets(fake_onvif) -> None:
    """Presets are a PTZ concept; a fixed camera reports none rather than
    raising, so the status envelope composes normally."""

    fake_onvif(_NO_PTZ)
    client = OnvifCameraClient("203.0.113.1", 2020, "u", "p")

    assert await client.list_presets() == []


async def test_fixed_camera_refuses_ptz_calls(fake_onvif) -> None:
    """A move must fail loudly, not silently no-op: the caller surfaces it
    as an error rather than reporting a move that never happened."""

    fake_onvif(_NO_PTZ)
    client = OnvifCameraClient("203.0.113.1", 2020, "u", "p")

    with pytest.raises(OnvifError, match="no PTZ service"):
        await client.continuous_move(pan=0.5)
    with pytest.raises(OnvifError, match="no PTZ service"):
        await client.goto_preset("1")


async def test_other_ptz_failures_still_propagate(fake_onvif) -> None:
    """Only "unsupported service" means "fixed camera". An auth failure or
    a network error must keep making the camera unreachable, or a genuinely
    broken PTZ camera would silently read as healthy-but-fixed."""

    fake_onvif(RuntimeError("HTTP 401 Unauthorized"))
    client = OnvifCameraClient("203.0.113.3", 2020, "u", "p")

    assert await client.is_reachable() is False


# --- Route level: allowed_actions must match the hardware ----------------


def test_fixed_camera_status_is_ready_without_ptz_actions(
    client: TestClient, stub_registry
) -> None:
    """A fixed camera is ``ready`` (ONVIF + go2rtc up) but must not advertise
    actions it cannot perform — STATUS_SPEC §6.2: allowed_actions never
    lists something the device would refuse."""

    stub_registry.camera("cam_lab499_west").onvif.has_ptz = False

    body = client.get("/cameras/cam_lab499_west/status").json()

    assert body["equipment_status"] == "ready"
    assert body["details"]["onvif_reachable"] is True
    actions = body["allowed_actions"]
    for ptz_action in ("ptz", "preset/save", "preset/goto", "preset/{id}"):
        assert ptz_action not in actions
    # Streaming and media stay available — they need RTSP, not PTZ.
    assert "streaming" in actions
    assert "snapshot" in actions


def test_ptz_camera_status_still_advertises_ptz(client: TestClient) -> None:
    body = client.get("/cameras/cam_lab499_west/status").json()

    assert "ptz" in body["allowed_actions"]
    assert "preset/goto" in body["allowed_actions"]
