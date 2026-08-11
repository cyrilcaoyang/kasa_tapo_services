"""Sanity tests for the STATUS_SPEC envelope re-exported from sdl-lab-contract.

``test_envelope_round_trip`` pins the version this gateway reports: it has a
``/control/*`` surface but no claim protocol, so STATUS_SPEC §9 keeps it at
v1.0 no matter which version of the contract package supplies the types.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kasa_tapo_services.models import (
    CameraDetails,
    EquipmentStatus,
    LensEntry,
    PresetEntry,
    PtzNudgeRequest,
)


def test_envelope_round_trip() -> None:
    payload = {
        "equipment_id": "cam_test",
        "equipment_name": "Test Camera",
        "equipment_kind": "camera",
        "equipment_status": "ready",
        "device_time": datetime.now(timezone.utc).isoformat(),
    }
    env = EquipmentStatus.model_validate(payload)
    assert env.protocol_version == "1.0"
    assert env.equipment_kind == "camera"
    assert env.required_actions == []
    assert env.allowed_actions == []


def test_camera_details_dump() -> None:
    details = CameraDetails(
        lenses=[LensEntry(id="wide", label="Wide", rtsp_path="stream1")],
        presets=[PresetEntry(id="1", name="home")],
        privacy_mode=True,
    )
    dumped = details.model_dump()
    assert dumped["privacy_mode"] is True
    assert dumped["lenses"][0]["label"] == "Wide"


def test_ptz_nudge_validates_speed() -> None:
    PtzNudgeRequest(direction="left", speed=0.5)
    with pytest.raises(ValueError):
        PtzNudgeRequest(direction="left", speed=2.0)
