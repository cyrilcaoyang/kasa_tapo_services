"""Tests for ``devices.yaml`` validation and credential lookup."""

from __future__ import annotations

import pytest
import yaml

from kasa_tapo_services.config import GatewayConfig, device_credentials


def _validate(devices: list[dict]) -> GatewayConfig:
    return GatewayConfig.model_validate({"devices": devices})


def test_camera_requires_lenses() -> None:
    with pytest.raises(ValueError, match="lens"):
        _validate(
            [
                {
                    "id": "cam_x",
                    "name": "Camera X",
                    "kind": "camera",
                    "host": "192.168.1.10",
                }
            ]
        )


def test_power_strip_requires_outlets() -> None:
    with pytest.raises(ValueError, match="outlets list"):
        _validate(
            [
                {
                    "id": "p",
                    "name": "P",
                    "kind": "power_strip",
                    "host": "192.168.1.20",
                }
            ]
        )


def test_unique_lens_ids() -> None:
    with pytest.raises(ValueError, match="lens ids"):
        _validate(
            [
                {
                    "id": "cam_x",
                    "name": "Camera X",
                    "kind": "camera",
                    "host": "192.168.1.10",
                    "lenses": [
                        {"id": "wide", "label": "A", "rtsp_path": "s1"},
                        {"id": "wide", "label": "B", "rtsp_path": "s2"},
                    ],
                }
            ]
        )


def test_smart_plug_minimal() -> None:
    cfg = _validate(
        [{"id": "p1", "name": "Plug 1", "kind": "smart_plug", "host": "192.168.1.20"}]
    )
    assert len(cfg.plugs()) == 1
    assert cfg.cameras() == []


def test_device_credentials_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAM_TEST_USER", "alice")
    monkeypatch.setenv("CAM_TEST_PASS", "secret")
    monkeypatch.setenv("CAM_TEST_ONVIF_USER", "onvif_user")
    monkeypatch.delenv("CAM_TEST_ONVIF_PASS", raising=False)

    creds = device_credentials("cam_test")
    assert creds.user == "alice"
    assert creds.password == "secret"
    # Falls back to basic password when only ONVIF user is set.
    assert creds.effective_onvif_user == "onvif_user"
    assert creds.effective_onvif_password == "secret"
    assert creds.has_basic is True


def test_example_yaml_round_trip() -> None:
    """The committed example file must validate cleanly."""

    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "devices.yaml.example").read_text()
    cfg = GatewayConfig.model_validate(yaml.safe_load(text))
    # devices.yaml.example ships with 1 active camera + 3 commented-out
    # entries, so we only assert a lower bound here.
    assert len(cfg.cameras()) >= 1
