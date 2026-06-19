"""Plug router tests - HS103 + HS300 stubs."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_smart_plug_status(client: TestClient) -> None:
    r = client.get("/plugs/plug_balance_lamp/status")
    assert r.status_code == 200
    body = r.json()
    assert body["equipment_kind"] == "smart_plug"
    assert "plug" in body["components"]
    assert body["components"]["plug"]["state"] == "off"
    assert body["allowed_actions"] == ["on", "off", "toggle"]


def test_power_strip_status_has_one_component_per_outlet(client: TestClient) -> None:
    r = client.get("/plugs/plug_hotplate_strip/status")
    assert r.status_code == 200
    body = r.json()
    assert body["equipment_kind"] == "power_strip"
    components = body["components"]
    assert {f"outlet_{i}" for i in range(6)} <= set(components.keys())
    # Even-indexed outlets are stubbed as "on".
    assert components["outlet_0"]["state"] == "on"
    assert components["outlet_1"]["state"] == "off"
    # The labels we put in devices.yaml flow through.
    assert components["outlet_2"]["message"] == "Stirrer"


def test_turn_on_specific_outlet(client: TestClient, stub_registry) -> None:
    r = client.post("/plugs/plug_hotplate_strip/control/on", json={"outlet": 1})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["state"]["outlet"] == 1
    stub_registry.plug("plug_hotplate_strip").kasa.turn_on.assert_awaited_once_with(1)


def test_turn_off_whole_strip(client: TestClient, stub_registry) -> None:
    r = client.post("/plugs/plug_hotplate_strip/control/off", json={})
    assert r.status_code == 200
    stub_registry.plug("plug_hotplate_strip").kasa.turn_off.assert_awaited_once_with(None)


def test_toggle_smart_plug(client: TestClient) -> None:
    r = client.post("/plugs/plug_balance_lamp/control/toggle")
    assert r.status_code == 200
    assert r.json()["state"]["is_on"] is True


def test_unreachable_plug_reports_unknown_not_error(
    client: TestClient, stub_registry
) -> None:
    """A plug the gateway cannot reach is `unknown` (state undeterminable),
    not `error` (reserved for a fault the hardware itself reports)."""

    stub_registry.plug("plug_balance_lamp").kasa.state.side_effect = OSError(
        "Unable to connect to the device: 172.31.60.19:9999: "
        "[Errno 113] No route to host"
    )
    r = client.get("/plugs/plug_balance_lamp/status")
    assert r.status_code == 200
    body = r.json()
    assert body["equipment_status"] == "unknown"
    assert "No route to host" in body["message"]


def test_outlet_index_validation(client: TestClient, stub_registry) -> None:
    """A bad outlet that the model accepts but python-kasa rejects bubbles up as 400."""

    bundle = stub_registry.plug("plug_hotplate_strip")
    bundle.kasa.turn_on.side_effect = ValueError("outlet=12 out of range for 6 outlets")
    r = client.post("/plugs/plug_hotplate_strip/control/on", json={"outlet": 12})
    assert r.status_code == 400
    assert "out of range" in r.json()["detail"]


def test_outlet_index_rejected_by_pydantic(client: TestClient) -> None:
    """Outlets above the schema bound (>31) are rejected with 422 before reaching the device."""

    r = client.post("/plugs/plug_hotplate_strip/control/on", json={"outlet": 99})
    assert r.status_code == 422
