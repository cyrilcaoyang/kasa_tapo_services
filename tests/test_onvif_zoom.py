"""Zoom-axis detection and refusal in ``OnvifCameraClient``.

ONVIF advertises each PTZ axis as a *space* on the node. The Tapo
dual-lens heads this gateway fronts (C245D, C246D — probed live 2026-09-10)
advertise pan/tilt spaces only: no ``ContinuousZoomVelocitySpace``, no
``AbsoluteZoomPositionSpace``, and ``GetStatus`` reports no zoom position.
A zoom velocity sent to such a node is silently ignored by the firmware,
so the client must refuse it up front and the status envelope must say
``has_zoom: false`` so a UI never offers a zoom button that does nothing.

The ONVIF library is faked here so the tests need no hardware.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kasa_tapo_services.tapo.onvif_client import OnvifCameraClient, ZoomUnsupportedError

_ZOOM_SPACE = SimpleNamespace(
    URI="http://www.onvif.org/ver10/tptz/ZoomSpaces/VelocityGenericSpace",
    XRange=SimpleNamespace(Min=-1.0, Max=1.0),
)


def _node(*, zoom: bool) -> SimpleNamespace:
    """A ``GetNodes`` entry shaped like the live C246D's (zeep leaves an
    unadvertised space as ``None``)."""

    return SimpleNamespace(
        token="PTZNODETOKEN",
        SupportedPTZSpaces=SimpleNamespace(
            ContinuousPanTiltVelocitySpace=[SimpleNamespace(URI="…PanTiltSpaces/VelocityGenericSpace")],
            ContinuousZoomVelocitySpace=[_ZOOM_SPACE] if zoom else None,
        ),
    )


class _FakeCamera:
    def __init__(self, ptz: MagicMock) -> None:
        self._ptz = ptz
        self.update_xaddrs = AsyncMock()

    async def create_devicemgmt_service(self):
        return SimpleNamespace(GetDeviceInformation=AsyncMock())

    async def create_ptz_service(self):
        return self._ptz

    async def create_media_service(self):
        media = MagicMock()
        profile = MagicMock()
        profile.token = "profile_1"
        media.GetProfiles = AsyncMock(return_value=[profile])
        return media


@pytest.fixture
def fake_onvif(monkeypatch: pytest.MonkeyPatch):
    """Install a fake ``onvif`` module; returns the PTZ service mock so a
    test can shape ``GetNodes`` and inspect ``ContinuousMove``."""

    ptz = MagicMock(name="ptz_service")
    ptz.create_type = MagicMock(side_effect=lambda _name: SimpleNamespace())
    ptz.ContinuousMove = AsyncMock()
    ptz.Stop = AsyncMock()
    ptz.GetNodes = AsyncMock(return_value=[_node(zoom=False)])

    module = types.ModuleType("onvif")
    module.__file__ = "/nonexistent/onvif/__init__.py"
    module.ONVIFCamera = lambda *a, **kw: _FakeCamera(ptz)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "onvif", module)
    return ptz


async def test_node_without_zoom_space_reports_no_zoom(fake_onvif) -> None:
    client = OnvifCameraClient("203.0.113.1", 2020, "u", "p")
    assert await client.is_reachable() is True
    assert client.has_ptz is True
    assert client.has_zoom is False


async def test_node_with_zoom_space_reports_zoom(fake_onvif) -> None:
    fake_onvif.GetNodes = AsyncMock(return_value=[_node(zoom=True)])
    client = OnvifCameraClient("203.0.113.1", 2020, "u", "p")
    assert await client.is_reachable() is True
    assert client.has_zoom is True


async def test_getnodes_failure_degrades_to_no_zoom(fake_onvif) -> None:
    # Pan/tilt must keep working when the capability query fails; only the
    # unconfirmed axis is withheld.
    fake_onvif.GetNodes = AsyncMock(side_effect=RuntimeError("boom"))
    client = OnvifCameraClient("203.0.113.1", 2020, "u", "p")
    assert await client.is_reachable() is True
    assert client.has_ptz is True
    assert client.has_zoom is False


async def test_zoom_velocity_refused_without_axis(fake_onvif) -> None:
    client = OnvifCameraClient("203.0.113.1", 2020, "u", "p")
    with pytest.raises(ZoomUnsupportedError, match="no zoom axis"):
        await client.continuous_move(zoom=0.5)
    fake_onvif.ContinuousMove.assert_not_awaited()


async def test_pan_tilt_still_move_without_zoom_axis(fake_onvif) -> None:
    client = OnvifCameraClient("203.0.113.1", 2020, "u", "p")
    await client.continuous_move(pan=0.5, tilt=-0.25)
    fake_onvif.ContinuousMove.assert_awaited_once()
    request = fake_onvif.ContinuousMove.await_args.args[0]
    assert request.Velocity["PanTilt"] == {"x": 0.5, "y": -0.25}
    assert request.Velocity["Zoom"] == {"x": 0.0}


async def test_zoom_velocity_sent_when_axis_present(fake_onvif) -> None:
    fake_onvif.GetNodes = AsyncMock(return_value=[_node(zoom=True)])
    client = OnvifCameraClient("203.0.113.1", 2020, "u", "p")
    await client.continuous_move(zoom=-0.5)
    request = fake_onvif.ContinuousMove.await_args.args[0]
    assert request.Velocity["Zoom"] == {"x": -0.5}


async def test_nudge_propagates_zoom_refusal(fake_onvif, monkeypatch) -> None:
    from kasa_tapo_services.tapo import onvif_client as mod

    monkeypatch.setattr(mod, "_POSITION_SETTLE_S", 0.0)
    client = OnvifCameraClient("203.0.113.1", 2020, "u", "p")
    with pytest.raises(ZoomUnsupportedError):
        await client.nudge(pan=0.0, tilt=0.0, zoom=0.5, duration_ms=100)


async def test_close_forgets_zoom_axis(fake_onvif) -> None:
    fake_onvif.GetNodes = AsyncMock(return_value=[_node(zoom=True)])
    client = OnvifCameraClient("203.0.113.1", 2020, "u", "p")
    await client.is_reachable()
    assert client.has_zoom is True
    await client.close()
    assert client.has_zoom is False
