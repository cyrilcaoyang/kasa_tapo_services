"""Camera router tests - all backends are stubbed via the fixture."""

from __future__ import annotations

from unittest.mock import AsyncMock

from fastapi.testclient import TestClient


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
