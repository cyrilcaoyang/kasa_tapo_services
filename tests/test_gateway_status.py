"""Service-level ``GET /status`` (the gateway's own STATUS_SPEC envelope)."""

from datetime import datetime, timezone

from fastapi.testclient import TestClient

import kasa_tapo_services.main as main


def test_gateway_status_envelope(stub_registry) -> None:
    main.app.state.registry = stub_registry
    main.app.state.boot_time = datetime.now(timezone.utc)
    # Plain TestClient (no context manager) skips lifespan, so the real
    # build_registry_from_disk never runs — the stub above is what the
    # handler sees.
    resp = TestClient(main.app).get("/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["protocol_version"] == "1.0"
    assert body["equipment_id"] == "kasa_tapo_gateway"
    assert body["equipment_kind"] == "other"
    assert body["equipment_status"] == "ready"
    assert body["uptime_seconds"] >= 0
    assert body["metrics"]["cameras"]["unit"] == "count"
    assert body["metrics"]["plugs"]["unit"] == "count"
    assert body["metrics"]["cameras"]["value"] == len(stub_registry.list_cameras())
