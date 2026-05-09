"""Render ``/etc/go2rtc/go2rtc.yaml`` from ``devices.yaml`` + ``.env``.

go2rtc is the RTSP-to-MSE bridge that sits between Tapo cameras and the
browser. Its config is a flat YAML with one stream per RTSP source. The
gateway's ``devices.yaml`` is the canonical list - this script renders an
authoritative go2rtc config with one stream per (camera, lens) pair.

Run as a one-shot before go2rtc starts (typical systemd usage):

.. code-block:: ini

    [Service]
    EnvironmentFile=/etc/kasa-tapo-services/.env
    ExecStartPre=/opt/kasa-tapo-services/.venv/bin/kasa-tapo-bootstrap-go2rtc
    ExecStart=/usr/local/bin/go2rtc -config /etc/go2rtc/go2rtc.yaml

The output path defaults to ``/etc/go2rtc/go2rtc.yaml`` and can be
overridden via the ``GO2RTC_CONFIG_PATH`` env var (handy for tests / dev).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import yaml

from kasa_tapo_services.config import DeviceConfig, GatewayConfig, device_credentials, load_config

logger = logging.getLogger(__name__)


def _stream_name(camera: DeviceConfig, lens_id: str) -> str:
    return f"{camera.id}_{lens_id}"


def _rtsp_url(camera: DeviceConfig, lens_path: str) -> str:
    """Build the RTSP URL for a (camera, lens) pair.

    Uses ``${VAR}`` placeholders so go2rtc itself substitutes the
    credentials at runtime - this keeps the rendered file safe to commit
    or inspect (the generated yaml never contains a plaintext password).
    """

    user_var = f"${{{camera.id.upper()}_USER}}"
    pass_var = f"${{{camera.id.upper()}_PASS}}"
    return f"rtsp://{user_var}:{pass_var}@{camera.host}:{camera.rtsp_port}/{lens_path}"


def render_go2rtc_yaml(
    config: GatewayConfig,
    *,
    listen: str = "127.0.0.1:1984",
    origin: str = "*",
) -> dict:
    streams: dict[str, list[str]] = {}
    for camera in config.cameras():
        if not camera.enabled:
            continue
        if not camera.lenses:
            continue
        for lens in camera.lenses:
            streams[_stream_name(camera, lens.id)] = [_rtsp_url(camera, lens.rtsp_path)]
    return {
        # `api.origin` controls the WebSocket Origin allow-list. We default
        # to `*` because go2rtc is bound to loopback (`api.listen`
        # `127.0.0.1:1984`) and only reachable from the same host - the
        # browser's Origin header sent on the MSE WebSocket upgrade is
        # `http://localhost:3010` (Next.js dev) or whatever the dashboard
        # serves under, which is a different origin from go2rtc's listen
        # address. Without this go2rtc returns `403 Forbidden` on the
        # upgrade and the dashboard's <video> element stays blank.
        "api": {"listen": listen, "origin": origin},
        # Hardware-accelerated transcoding off by default; the dashboard
        # only consumes the MSE feed which is a passthrough copy.
        "streams": streams,
    }


def write_yaml(target: Path, payload: dict) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(payload, sort_keys=False, default_flow_style=False)
    target.write_text(text, encoding="utf-8")


def warn_missing_credentials(config: GatewayConfig) -> None:
    """Log a warning for any camera that has no ``<ID>_USER``/``_PASS`` set.

    Doesn't abort - go2rtc will simply fail to connect to that source and
    the gateway will report the source as ``disconnected``.
    """

    for camera in config.cameras():
        creds = device_credentials(camera.id)
        if not creds.has_basic:
            logger.warning(
                "Camera %s has no credentials in env (set %s_USER and %s_PASS)",
                camera.id,
                camera.id.upper(),
                camera.id.upper(),
            )


def main(argv: list[str] | None = None) -> int:
    """Console entrypoint for ``kasa-tapo-bootstrap-go2rtc``."""

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--devices",
        default=os.environ.get("KASA_TAPO_DEVICES_PATH"),
        help="Path to devices.yaml (default: $KASA_TAPO_DEVICES_PATH or auto-discover)",
    )
    parser.add_argument(
        "--output",
        default=os.environ.get("GO2RTC_CONFIG_PATH", "/etc/go2rtc/go2rtc.yaml"),
        help="Where to write the rendered go2rtc.yaml",
    )
    parser.add_argument(
        "--listen",
        default=os.environ.get("GO2RTC_LISTEN", "127.0.0.1:1984"),
        help="api.listen address for go2rtc (default: 127.0.0.1:1984)",
    )
    parser.add_argument(
        "--origin",
        default=os.environ.get("GO2RTC_ORIGIN", "*"),
        help=(
            "api.origin allow-list for WebSocket upgrades (default: '*'). "
            "Required for the browser to talk to go2rtc directly across "
            "ports/origins; tighten this in production behind a reverse "
            "proxy that terminates TLS on the same origin as the dashboard."
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    config = load_config(args.devices)
    warn_missing_credentials(config)
    payload = render_go2rtc_yaml(config, listen=args.listen, origin=args.origin)
    target = Path(args.output)
    write_yaml(target, payload)
    logger.info("Wrote %d streams to %s", len(payload["streams"]), target)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


__all__ = ["main", "render_go2rtc_yaml", "write_yaml", "warn_missing_credentials"]
