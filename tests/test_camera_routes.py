"""Camera router tests - all backends are stubbed via the fixture."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient
from zeep.exceptions import Fault

from kasa_tapo_services.tapo.onvif_client import OnvifCameraClient


def test_probe(client: TestClient) -> None:
    r = client.get("/cameras/cam_lab499_west/")
    assert r.status_code == 200
    body = r.json()
    assert body["equipment_id"] == "cam_lab499_west"
    assert body["protocol_version"] == "1.0"


def test_health_404_for_unknown_camera(client: TestClient) -> None:
    assert client.get("/cameras/no_such_camera/health").status_code == 404


def test_status_envelope(client: TestClient) -> None:
    r = client.get("/cameras/cam_lab499_west/status")
    assert r.status_code == 200
    body = r.json()
    assert body["equipment_kind"] == "camera"
    assert body["equipment_status"] in ("ready", "degraded")
    details = body["details"]
    assert details["lenses"], "lenses must be populated"
    assert {lens["id"] for lens in details["lenses"]} == {"wide", "tele"}
    assert details["onvif_reachable"] is True
    assert details["go2rtc_reachable"] is True
    assert "preset/save" in body["allowed_actions"]


def test_unreachable_camera_reports_unknown_not_error(
    client: TestClient, stub_registry
) -> None:
    """A camera the gateway cannot reach at all (ONVIF + Tapo both down) is
    `unknown` (state undeterminable), not `error` (which is reserved for a
    reachable camera whose subsystem reports a fault)."""

    cam = stub_registry.camera("cam_lab499_west")
    cam.onvif.is_reachable = AsyncMock(return_value=False)
    cam.tapo.privacy_mode = AsyncMock(return_value=None)
    r = client.get("/cameras/cam_lab499_west/status")
    assert r.status_code == 200
    body = r.json()
    assert body["equipment_status"] == "unknown"
    assert "unreachable" in body["message"].lower()


def test_ptz_nudge(client: TestClient) -> None:
    r = client.post(
        "/cameras/cam_lab499_west/control/ptz",
        json={"direction": "left", "speed": 0.5, "duration_ms": 200},
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


def test_ptz_nudge_at_limit_soft_fails(client: TestClient, stub_registry) -> None:
    """A nudge that hits the physical pan/tilt limit returns 200 with
    ok:false so the dashboard can surface it — not an HTTP error."""

    from kasa_tapo_services.tapo.onvif_client import PtzNudgeOutcome

    cam = stub_registry.camera("cam_lab499_west")
    cam.onvif.nudge = AsyncMock(
        return_value=PtzNudgeOutcome(limited_axes=("pan",), detected=True)
    )
    r = client.post(
        "/cameras/cam_lab499_west/control/ptz",
        json={"direction": "left", "speed": 0.5, "duration_ms": 200},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["message"] == "pan limit reached"


def test_ptz_nudge_undetected_outcome_is_ok(client: TestClient, stub_registry) -> None:
    """When limit detection could not run (no position support), the nudge
    acks ok:true exactly as before."""

    from kasa_tapo_services.tapo.onvif_client import PtzNudgeOutcome

    cam = stub_registry.camera("cam_lab499_west")
    cam.onvif.nudge = AsyncMock(return_value=PtzNudgeOutcome())
    r = client.post(
        "/cameras/cam_lab499_west/control/ptz",
        json={"direction": "up", "speed": 0.5, "duration_ms": 200},
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


def test_ptz_continuous(client: TestClient) -> None:
    r = client.post(
        "/cameras/cam_lab499_west/control/ptz",
        json={"pan": 0.5, "tilt": 0.0},
    )
    assert r.status_code == 200, r.text


def test_ptz_stop_via_zero_velocity(client: TestClient) -> None:
    r = client.post(
        "/cameras/cam_lab499_west/control/ptz",
        json={"pan": 0.0, "tilt": 0.0, "zoom": 0.0},
    )
    assert r.status_code == 200
    assert r.json()["message"] == "stopped"


def test_save_and_goto_preset(client: TestClient) -> None:
    r = client.post(
        "/cameras/cam_lab499_west/control/preset/save",
        json={"name": "home"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"]["preset_id"] == "9"

    r = client.post(
        "/cameras/cam_lab499_west/control/preset/goto",
        json={"preset_id": "9"},
    )
    assert r.status_code == 200


def test_delete_preset(client: TestClient) -> None:
    r = client.delete("/cameras/cam_lab499_west/control/preset/9")
    assert r.status_code == 200


@pytest.mark.parametrize(
    ("fault", "status", "message"),
    [
        ("Number of presets limit reached", 409, "Camera preset storage is full"),
        ("Device unavailable", 502, "Save failed: Device unavailable"),
    ],
)
def test_preset_save_camera_fault(client, stub_registry, fault, status, message) -> None:
    """Exercise the real ONVIF adapter through the route, with only SOAP stubbed."""
    onvif = OnvifCameraClient("203.0.113.1", 2020, "u", "p")
    onvif._cam = object()
    onvif._media_token = "profile1"
    ptz = Mock()
    ptz.create_type.return_value = SimpleNamespace()
    ptz.SetPreset = AsyncMock(side_effect=Fault(fault))
    onvif._ptz = ptz
    stub_registry.camera("cam_lab499_west").onvif = onvif

    response = client.post(
        "/cameras/cam_lab499_west/control/preset/save", json={"name": "HOME"}
    )

    assert response.status_code == status
    assert message in response.json()["detail"]
    ptz.SetPreset.assert_awaited_once()
    request = ptz.SetPreset.call_args.args[0]
    assert request.ProfileToken == "profile1"
    assert request.PresetName == "HOME"
    assert not hasattr(request, "PresetToken")
    ptz.RemovePreset.assert_not_called()


def test_privacy_toggle(client: TestClient) -> None:
    r = client.post("/cameras/cam_lab499_west/control/privacy", json={"enabled": True})
    assert r.status_code == 200
    assert r.json()["state"]["privacy_mode"] is True


def test_streaming_toggle_flips_in_memory_flag(client: TestClient, stub_registry) -> None:
    """The streaming toggle must NOT call go2rtc - those streams are
    statically configured in ``go2rtc.yaml``. Instead it flips an
    in-memory ``streaming_enabled`` flag on the camera bundle, which
    drives whether ``mse_url`` is exposed to the dashboard."""

    # Default is enabled -> the next /status should expose lens MSE URLs.
    r = client.get("/cameras/cam_lab499_west/status")
    assert r.status_code == 200
    lenses = r.json()["details"]["lenses"]
    assert all(lens["mse_url"] for lens in lenses)
    assert r.json()["details"]["streaming_enabled"] is True

    # Flip OFF.
    r = client.post(
        "/cameras/cam_lab499_west/control/streaming",
        json={"enabled": False},
    )
    assert r.status_code == 200
    assert r.json()["state"]["streaming_enabled"] is False

    # Status now hides the URLs.
    r = client.get("/cameras/cam_lab499_west/status")
    body = r.json()
    assert body["details"]["streaming_enabled"] is False
    assert all(lens["mse_url"] is None for lens in body["details"]["lenses"])

    # No mutation of go2rtc.
    assert stub_registry.go2rtc.add_stream.await_count == 0
    assert stub_registry.go2rtc.remove_stream.await_count == 0

    # Flip back ON and confirm URLs reappear.
    client.post(
        "/cameras/cam_lab499_west/control/streaming",
        json={"enabled": True},
    )
    r = client.get("/cameras/cam_lab499_west/status")
    assert all(lens["mse_url"] for lens in r.json()["details"]["lenses"])


# -- Zoom directions ------------------------------------------------------
#
# ``zoom_in`` / ``zoom_out`` ride the same nudge body as pan/tilt. Whether
# they *work* is a property of the camera's ONVIF PTZ node (``details.
# has_zoom``); the client refuses a zoom velocity on a node with no zoom
# axis and the route turns that into 409.


@pytest.mark.parametrize(("direction", "expected_zoom"), [("zoom_in", 0.5), ("zoom_out", -0.5)])
def test_ptz_zoom_nudge_drives_only_the_zoom_axis(
    client: TestClient, stub_registry, direction: str, expected_zoom: float
) -> None:
    from kasa_tapo_services.tapo.onvif_client import PtzNudgeOutcome

    cam = stub_registry.camera("cam_lab499_west")
    cam.onvif.nudge = AsyncMock(return_value=PtzNudgeOutcome())
    r = client.post(
        "/cameras/cam_lab499_west/control/ptz",
        json={"direction": direction, "speed": 0.5, "duration_ms": 300},
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert r.json()["message"] == f"nudged {direction}"
    kwargs = cam.onvif.nudge.await_args.kwargs
    assert kwargs["pan"] == 0.0 and kwargs["tilt"] == 0.0
    assert kwargs["zoom"] == pytest.approx(expected_zoom)
    assert kwargs["duration_ms"] == 300


def test_ptz_zoom_nudge_without_zoom_axis_is_409(client: TestClient, stub_registry) -> None:
    from kasa_tapo_services.tapo.onvif_client import ZoomUnsupportedError

    cam = stub_registry.camera("cam_lab499_west")
    cam.onvif.nudge = AsyncMock(
        side_effect=ZoomUnsupportedError("camera 203.0.113.1 has no zoom axis")
    )
    r = client.post(
        "/cameras/cam_lab499_west/control/ptz",
        json={"direction": "zoom_in"},
    )
    assert r.status_code == 409, r.text
    assert "no zoom axis" in r.json()["detail"]


def test_ptz_continuous_zoom_without_zoom_axis_is_409(client: TestClient, stub_registry) -> None:
    from kasa_tapo_services.tapo.onvif_client import ZoomUnsupportedError

    cam = stub_registry.camera("cam_lab499_west")
    cam.onvif.continuous_move = AsyncMock(
        side_effect=ZoomUnsupportedError("camera 203.0.113.1 has no zoom axis")
    )
    r = client.post(
        "/cameras/cam_lab499_west/control/ptz",
        json={"pan": 0.0, "tilt": 0.0, "zoom": 0.4},
    )
    assert r.status_code == 409, r.text


def test_status_reports_zoom_axis(client: TestClient, stub_registry) -> None:
    r = client.get("/cameras/cam_lab499_west/status")
    assert r.status_code == 200
    assert r.json()["details"]["has_zoom"] is False
    # ``ptz`` stays advertised: pan/tilt work on a zoom-less head.
    assert "ptz" in r.json()["allowed_actions"]

    stub_registry.camera("cam_lab499_west").onvif.has_zoom = True
    r = client.get("/cameras/cam_lab499_west/status")
    assert r.json()["details"]["has_zoom"] is True


def test_status_no_zoom_without_ptz(client: TestClient, stub_registry) -> None:
    """A fixed camera cannot have a zoom axis even if the stub claims one."""

    cam = stub_registry.camera("cam_lab499_west")
    cam.onvif.has_ptz = False
    cam.onvif.has_zoom = True
    r = client.get("/cameras/cam_lab499_west/status")
    assert r.json()["details"]["has_zoom"] is False
    assert "ptz" not in r.json()["allowed_actions"]
