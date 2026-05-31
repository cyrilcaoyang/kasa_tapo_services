"""Tests for the go2rtc config renderer."""

from __future__ import annotations

from kasa_tapo_services.config import GatewayConfig
from kasa_tapo_services.tapo.bootstrap_go2rtc import render_go2rtc_yaml


def test_renders_one_stream_per_lens(example_config: GatewayConfig) -> None:
    payload = render_go2rtc_yaml(example_config)
    streams = payload["streams"]
    # cam_lab499_west has 2 lenses, cam_storage has 1, so 3 total.
    assert set(streams.keys()) == {
        "cam_lab499_west_wide",
        "cam_lab499_west_tele",
        "cam_storage_main",
    }
    # Listen address is the configured default.
    assert payload["api"]["listen"] == "127.0.0.1:1984"
    # The default origin allow-list is `*` so the browser (running on a
    # different port from go2rtc) can complete the WS upgrade.
    assert payload["api"]["origin"] == "*"


def test_render_go2rtc_yaml_origin_override(example_config: GatewayConfig) -> None:
    payload = render_go2rtc_yaml(example_config, origin="https://lab.example.com")
    assert payload["api"]["origin"] == "https://lab.example.com"


def test_uses_env_var_placeholders_when_credentials_missing(example_config: GatewayConfig) -> None:
    payload = render_go2rtc_yaml(example_config)
    url = payload["streams"]["cam_lab499_west_wide"][0]
    assert "${CAM_LAB499_WEST_USER}" in url
    assert "${CAM_LAB499_WEST_PASS}" in url
    assert "192.168.1.42:554/stream1" in url


def test_url_encodes_loaded_credentials(
    example_config: GatewayConfig, monkeypatch
) -> None:
    monkeypatch.setenv("CAM_LAB499_WEST_USER", "camera:user")
    monkeypatch.setenv("CAM_LAB499_WEST_PASS", "p@ss word#1")

    payload = render_go2rtc_yaml(example_config)

    url = payload["streams"]["cam_lab499_west_wide"][0]
    assert "camera%3Auser" in url
    assert "p%40ss%20word%231" in url
    assert "${CAM_LAB499_WEST_USER}" not in url


def test_webrtc_block_present_by_default(example_config: GatewayConfig) -> None:
    """TCP-only WebRTC listen with no candidates by default."""

    payload = render_go2rtc_yaml(example_config)
    assert "webrtc" in payload
    webrtc = payload["webrtc"]
    assert webrtc["listen"] == "0.0.0.0:8555/tcp"
    # No host configured => no candidates list; go2rtc falls back to
    # autodiscovery (correct on single-homed dev hosts).
    assert "candidates" not in webrtc
    # Private tailnet: no STUN/TURN auto-discovery.
    assert webrtc["ice_servers"] == []


def test_webrtc_block_with_host_renders_candidate(
    example_config: GatewayConfig,
) -> None:
    payload = render_go2rtc_yaml(
        example_config,
        webrtc_host="gaia.tail6a1dd7.ts.net",
    )
    assert payload["webrtc"]["candidates"] == [
        "gaia.tail6a1dd7.ts.net:8555"
    ]


def test_webrtc_host_strips_user_supplied_port(
    example_config: GatewayConfig,
) -> None:
    """Caller passing host:port shouldn't double-port the candidate."""

    payload = render_go2rtc_yaml(
        example_config,
        webrtc_host="gaia.tail6a1dd7.ts.net:9999",
    )
    # Port is always paired with the listen-port (8555), not the value
    # the caller might paste in by accident.
    assert payload["webrtc"]["candidates"] == [
        "gaia.tail6a1dd7.ts.net:8555"
    ]


def test_webrtc_disabled_when_listen_empty(example_config: GatewayConfig) -> None:
    """Empty webrtc_listen drops the section entirely (MSE-only mode)."""

    payload = render_go2rtc_yaml(example_config, webrtc_listen=None)
    assert "webrtc" not in payload


def test_disabled_devices_are_skipped() -> None:
    cfg = GatewayConfig.model_validate(
        {
            "devices": [
                {
                    "id": "cam_active",
                    "name": "Active",
                    "kind": "camera",
                    "host": "192.168.1.10",
                    "lenses": [{"id": "main", "label": "Main", "rtsp_path": "stream1"}],
                },
                {
                    "id": "cam_off",
                    "name": "Off",
                    "kind": "camera",
                    "host": "192.168.1.11",
                    "enabled": False,
                    "lenses": [{"id": "main", "label": "Main", "rtsp_path": "stream1"}],
                },
            ]
        }
    )
    payload = render_go2rtc_yaml(cfg)
    assert "cam_active_main" in payload["streams"]
    assert "cam_off_main" not in payload["streams"]
