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
from urllib.parse import quote

import yaml

from kasa_tapo_services.config import DeviceConfig, GatewayConfig, device_credentials, load_config

logger = logging.getLogger(__name__)


def _stream_name(camera: DeviceConfig, lens_id: str) -> str:
    return f"{camera.id}_{lens_id}"


def _rtsp_url(camera: DeviceConfig, lens_path: str) -> str:
    """Build the RTSP URL for a (camera, lens) pair.

    When credentials are present in the process environment, render them
    directly and percent-encode URL-special characters. go2rtc does not
    reliably expand ``${VAR}`` placeholders inside RTSP userinfo across
    versions, and unescaped ``@`` / ``:`` / ``#`` characters break parsing.

    If credentials are missing, retain placeholders so the generated shape is
    still inspectable and ``warn_missing_credentials`` can tell the operator
    what to set.
    """

    creds = device_credentials(camera.id)
    if creds.has_basic:
        user = quote(creds.user or "", safe="")
        password = quote(creds.password or "", safe="")
    else:
        user = f"${{{camera.id.upper()}_USER}}"
        password = f"${{{camera.id.upper()}_PASS}}"
    return f"rtsp://{user}:{password}@{camera.host}:{camera.rtsp_port}/{lens_path}"


def render_go2rtc_yaml(
    config: GatewayConfig,
    *,
    listen: str = "127.0.0.1:1984",
    origin: str = "*",
    webrtc_listen: str | None = "0.0.0.0:8555/tcp",
    webrtc_host: str | None = None,
) -> dict:
    streams: dict[str, list[str]] = {}
    for camera in config.cameras():
        if not camera.enabled:
            continue
        if not camera.lenses:
            continue
        for lens in camera.lenses:
            streams[_stream_name(camera, lens.id)] = [_rtsp_url(camera, lens.rtsp_path)]
    payload: dict = {
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

    # Optional WebRTC opt-in. The MSE pipeline (port 1984, exposed via
    # Caddy at /streams/*) keeps working alongside this — the dashboard
    # picks one of the two protocols per <video> element. We default to
    # TCP-only because UDP through Caddy/Tailscale needs additional
    # firewall coordination and ICE candidate plumbing. TCP is slightly
    # higher latency than UDP but still 5–10× better than MSE and works
    # over any path that already allows the browser to reach the
    # dashboard host.
    if webrtc_listen:
        webrtc: dict = {"listen": webrtc_listen}
        # `webrtc.candidates` is what go2rtc advertises to the browser as
        # the address to connect to. Without it the browser would try to
        # connect to whatever ICE thinks is the server's address — which
        # for a server behind Caddy on a Tailnet is usually wrong.
        if webrtc_host:
            # Strip any port the caller passed in; we always pair the
            # candidate with the same port as `listen`.
            host = webrtc_host.split(":", 1)[0]
            port = webrtc_listen.split("/", 1)[0].rsplit(":", 1)[-1]
            webrtc["candidates"] = [f"{host}:{port}"]
        # Tailnet-private deployment: no STUN/TURN. Listing an empty
        # ice_servers keeps go2rtc from auto-discovering anything.
        webrtc["ice_servers"] = []
        payload["webrtc"] = webrtc

    return payload


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
    parser.add_argument(
        "--webrtc-listen",
        default=os.environ.get("GO2RTC_WEBRTC_LISTEN", "0.0.0.0:8555/tcp"),
        help=(
            "webrtc.listen address (default: '0.0.0.0:8555/tcp'). Set to "
            "empty string to disable the WebRTC block entirely (MSE keeps "
            "working). UDP is supported by go2rtc ('0.0.0.0:8555') but "
            "default is TCP-only — friendlier to Caddy/Tailscale routing."
        ),
    )
    parser.add_argument(
        "--webrtc-host",
        default=os.environ.get("GO2RTC_WEBRTC_HOST"),
        help=(
            "Hostname (or IP) the browser should use to connect for "
            "WebRTC media. Typically the dashboard host's Tailscale "
            "MagicDNS name (e.g. gaia.tail6a1dd7.ts.net). When unset, "
            "go2rtc tries to auto-detect, which often picks the wrong "
            "interface on multi-homed hosts."
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    config = load_config(args.devices)
    warn_missing_credentials(config)
    # Empty --webrtc-listen disables the WebRTC block (MSE keeps working).
    webrtc_listen = args.webrtc_listen or None
    payload = render_go2rtc_yaml(
        config,
        listen=args.listen,
        origin=args.origin,
        webrtc_listen=webrtc_listen,
        webrtc_host=args.webrtc_host,
    )
    target = Path(args.output)
    write_yaml(target, payload)
    if "webrtc" in payload:
        logger.info(
            "WebRTC enabled: listen=%s candidates=%s",
            payload["webrtc"]["listen"],
            payload["webrtc"].get("candidates", []),
        )
        if not payload["webrtc"].get("candidates"):
            logger.warning(
                "GO2RTC_WEBRTC_HOST is unset — browsers may receive an "
                "unreachable ICE candidate. Set to the dashboard's "
                "MagicDNS hostname (e.g. gaia.tail6a1dd7.ts.net)."
            )
    logger.info("Wrote %d streams to %s", len(payload["streams"]), target)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


__all__ = ["main", "render_go2rtc_yaml", "write_yaml", "warn_missing_credentials"]
