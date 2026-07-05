#!/usr/bin/env python3
"""Control a Tapo camera remotely through the AC Organic Lab dashboard API.

This is a thin command-line client for steering a lab Tapo PTZ camera (and
reading its state) from any machine on the lab Tailnet - the scripting
equivalent of clicking the PTZ buttons on the dashboard.

How it works
============

The camera itself speaks proprietary protocols (Tapo HTTPS, RTSP, ONVIF) and
lives on the isolated lab LAN. It is fronted by the ``kasa-tapo-services``
gateway, which translates those protocols into a normalized HTTP surface
(``/cameras/{id}/control/ptz`` etc.). Critically, that gateway is bound to
``127.0.0.1:8002`` on the dashboard server, so it is NOT reachable from your
laptop - only processes on the dashboard host can hit it directly.

So instead of talking to the gateway, this script talks to the dashboard,
which is the one component allowed to reach every piece of equipment. The
dashboard exposes a generic control passthrough that the browser already
uses; we hit the exact same endpoint::

    POST <dashboard>/api/equipment/{camera_id}/control/{action}

The full request path for, e.g., ``ptz left``::

    this script                                   (your laptop, on the Tailnet)
        |  POST http://100.64.254.6:8000/api/equipment/cam_.../control/ptz
        v
    Next.js web server  :8000                     (dashboard host)
        |  rewrites /api/* -> http://127.0.0.1:8001/api/*
        v
    FastAPI aggregator  :8001  (api/app/control.py)
        |  looks the camera up in equipment.yaml, rewrites base_url +
        |  status_path into the gateway's /control/{action} URL, adds an
        |  audit-log row and (for STATUS_SPEC v1.1 devices) a claim/release
        |  dance, then forwards the JSON body verbatim
        v
    kasa-tapo-services gateway  :8002             (loopback on dashboard host)
        |  ONVIF ContinuousMove / Tapo API call
        v
    Tapo camera                                   (lab LAN)

Reads work the same way but in the GET direction:

* ``status``  -> ``GET /api/equipment/{id}/status``  (the live STATUS_SPEC
  envelope: PTZ presets, lens metadata, privacy mode, stream health). The
  dashboard serves this from a cache its aggregator refreshes every ~2-3 s by
  polling the gateway; control, by contrast, is a direct request/response and
  is never cached.
* ``devices`` -> ``GET /api/equipment``  (every device the dashboard hosts).

Note: this controls the camera and reads its *status*. The live *video* feed
is a separate path (go2rtc transcodes RTSP -> MSE/WebRTC, consumed by the
browser through ``/streams/*``); it is intentionally out of scope here.

Endpoint map (CLI subcommand -> dashboard route -> JSON body)
-------------------------------------------------------------

* ``status``         GET  /api/equipment/{id}/status
* ``devices``        GET  /api/equipment
* ``presets``        GET  /api/equipment/{id}/status  (extracts details.presets)
                     prints id + name for each preset saved on the camera
* ``ptz``            POST /api/equipment/{id}/control/ptz
                     body: {direction, speed, duration_ms}  (nudge)
* ``ptz-continuous`` POST /api/equipment/{id}/control/ptz
                     body: {pan, tilt, zoom, duration_ms?}  (all-zero = stop)
* ``preset-goto``    GET  /api/equipment/{id}/status  (name lookup, if needed)
                     POST /api/equipment/{id}/control/preset/goto
                     body: {preset_id}  (accepts a name or numeric id)
* ``privacy``        POST /api/equipment/{id}/control/privacy
                     body: {enabled}
* ``snapshot``       POST /api/equipment/{id}/control/snapshot
                     body: {lens?}  (returns server-side path/url; the JPEG
                     itself is downloadable via /api/equipment/{id}/media/...)

The bodies are forwarded unchanged to the gateway, so their shapes match the
gateway's models in ``src/kasa_tapo_services/routes/cameras.py``.

Configuration (constants below, overridable via env)
-----------------------------------------------------

* ``KASA_TAPO_DASHBOARD_URL`` - dashboard base URL
  (default ``http://100.64.254.6:8000``). This is the host you open in the
  browser to view/control the camera - the dashboard Linux server's Tailnet
  address, NOT your laptop. Port 8000 is the dashboard's Next.js server, which
  proxies ``/api/*`` to the FastAPI aggregator on the same host. Plain HTTP
  over the Tailnet; no token needed (access is gated by Tailscale ACLs).
* ``CAMERA_ID`` - default camera id; override per-invocation with ``--camera``.

Exit codes / errors
-------------------

* ``0`` on success (the JSON response is printed to stdout).
* ``1`` on failure. A non-2xx dashboard response prints the status code and
  body (e.g. 404 = unknown camera id, 502/504 = dashboard could not reach the
  gateway, 503 = ONVIF not configured). A transport failure (connection
  refused/timeout) prints a hint to check the Tailnet connection.

Requirements: be connected to the lab Tailnet.

Examples::

    python scripts/control_camera.py status
    python scripts/control_camera.py devices
    python scripts/control_camera.py ptz left --speed 0.5 --duration-ms 600
    python scripts/control_camera.py ptz-continuous --pan -1.0 --duration-ms 2000
    python scripts/control_camera.py ptz stop
    python scripts/control_camera.py presets
    python scripts/control_camera.py preset-goto Overview
    python scripts/control_camera.py preset-goto 1
    python scripts/control_camera.py privacy on
    python scripts/control_camera.py snapshot --lens wide

    # point at a different dashboard host / camera:
    KASA_TAPO_DASHBOARD_URL=http://sdl2-server-gaia.tail6a1dd7.ts.net:8000 \\
        python scripts/control_camera.py --camera cam_hte_tapo_c245 status

Commands to run it:
# read current state (also lists presets/lenses)
uv run python scripts/control_camera.py status
# nudge left
uv run python scripts/control_camera.py ptz left --speed 0.5 --duration-ms 600

uv run python scripts/control_camera.py preset-goto Cytation5
uv run python scripts/control_camera.py preset-goto "Shaker and Filtration"
uv run python scripts/control_camera.py preset-goto 1

"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import httpx

DASHBOARD_URL = os.environ.get("KASA_TAPO_DASHBOARD_URL", "http://100.64.254.6:8000")
CAMERA_ID = os.environ.get("CAMERA_ID", "cam_hte_tapo_c245")

# Valid PTZ directions, mirroring _DIRECTION_VECTORS in
# src/kasa_tapo_services/routes/cameras.py.
DIRECTIONS = [
    "up",
    "down",
    "left",
    "right",
    "up_left",
    "up_right",
    "down_left",
    "down_right",
    "stop",
]

DEFAULT_TIMEOUT = 15.0


def _request(method: str, path: str, *, json_body: dict[str, Any] | None = None) -> Any:
    """Issue one HTTP request to the dashboard API and return the parsed JSON.

    Raises ``httpx.HTTPStatusError`` on a non-2xx response so the caller (or
    ``main``) surfaces the dashboard/gateway error detail instead of silently
    passing.
    """

    url = f"{DASHBOARD_URL.rstrip('/')}{path}"
    with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
        resp = client.request(method, url, json=json_body)
        resp.raise_for_status()
        if resp.content:
            return resp.json()
        return None


# --- Thin endpoint wrappers (dashboard /api/equipment surface) -------------


def get_status(camera_id: str) -> Any:
    return _request("GET", f"/api/equipment/{camera_id}/status")


def list_devices() -> Any:
    return _request("GET", "/api/equipment")


def _control(camera_id: str, action: str, body: dict[str, Any] | None) -> Any:
    return _request(
        "POST", f"/api/equipment/{camera_id}/control/{action}", json_body=body
    )


def ptz(camera_id: str, direction: str, *, speed: float, duration_ms: int) -> Any:
    """Discrete PTZ nudge (move briefly, then auto-stop)."""

    body: dict[str, Any] = {"direction": direction}
    if direction != "stop":
        body["speed"] = speed
        body["duration_ms"] = duration_ms
    return _control(camera_id, "ptz", body)


def ptz_continuous(
    camera_id: str,
    *,
    pan: float,
    tilt: float,
    zoom: float,
    duration_ms: int | None,
) -> Any:
    """Continuous PTZ vector. All-zero values halt the move."""

    body: dict[str, Any] = {"pan": pan, "tilt": tilt, "zoom": zoom}
    if duration_ms is not None:
        body["duration_ms"] = duration_ms
    return _control(camera_id, "ptz", body)


def list_presets(camera_id: str) -> list[dict[str, str]]:
    """Return the presets stored on the camera as [{id, name}, ...].

    Presets live on the camera itself (ONVIF flash storage) - there is no
    config file. The gateway fetches them live via GetPresets on each status
    poll and embeds the list in ``status.details.presets`` of the dashboard's
    equipment envelope. An empty list means either no presets have been saved
    yet, or ONVIF is not reachable on this camera.

    Note: the dashboard's /api/equipment/{id}/status wraps the STATUS_SPEC
    envelope under a top-level ``"status"`` key, so presets live at
    response["status"]["details"]["presets"], not response["details"]["presets"].
    """
    response = get_status(camera_id)
    return response.get("status", {}).get("details", {}).get("presets", [])


def _resolve_preset_id(camera_id: str, name_or_id: str) -> str:
    """Return the numeric ONVIF preset token for a name or pass-through an id.

    If ``name_or_id`` is already numeric (e.g. ``"1"``) it is returned
    unchanged without an extra HTTP call. Otherwise the camera's preset list
    is fetched and searched case-insensitively by name.
    """
    if name_or_id.isdigit():
        return name_or_id

    presets = list_presets(camera_id)
    if not presets:
        raise ValueError(
            "camera has no saved presets (is ONVIF reachable?). "
            "Run 'presets' to check."
        )
    for p in presets:
        if p.get("name", "").lower() == name_or_id.lower():
            return str(p["id"])
    available = ", ".join(p.get("name", p["id"]) for p in presets)
    raise ValueError(
        f"no preset named {name_or_id!r}; available: {available}"
    )


def goto_preset(camera_id: str, name_or_id: str) -> Any:
    """Move to a saved preset, accepting either a name or a numeric id."""
    preset_id = _resolve_preset_id(camera_id, name_or_id)
    return _control(camera_id, "preset/goto", {"preset_id": preset_id})


def set_privacy(camera_id: str, enabled: bool) -> Any:
    return _control(camera_id, "privacy", {"enabled": enabled})


def snapshot(camera_id: str, lens: str | None) -> Any:
    body = {"lens": lens} if lens else {}
    return _control(camera_id, "snapshot", body)


# --- CLI -------------------------------------------------------------------


def _print(result: Any) -> None:
    print(json.dumps(result, indent=2, sort_keys=True))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Control a Tapo camera via the AC Organic Lab dashboard API.",
    )
    parser.add_argument(
        "--camera",
        default=CAMERA_ID,
        help=f"Camera id (default: {CAMERA_ID!r}; or set CAMERA_ID env).",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Print the camera's full status envelope.")
    sub.add_parser("devices", help="List every device the dashboard hosts.")
    sub.add_parser("presets", help="List saved presets on the camera (id + name).")

    p_ptz = sub.add_parser("ptz", help="Nudge the PTZ head in a direction.")
    p_ptz.add_argument("direction", choices=DIRECTIONS)
    p_ptz.add_argument("--speed", type=float, default=0.5, help="0.0-1.0 (default 0.5).")
    p_ptz.add_argument(
        "--duration-ms", type=int, default=400, help="Move duration in ms (default 400)."
    )

    p_cont = sub.add_parser(
        "ptz-continuous", help="Send a raw continuous PTZ velocity vector."
    )
    p_cont.add_argument("--pan", type=float, default=0.0, help="-1.0..1.0")
    p_cont.add_argument("--tilt", type=float, default=0.0, help="-1.0..1.0")
    p_cont.add_argument("--zoom", type=float, default=0.0, help="-1.0..1.0")
    p_cont.add_argument(
        "--duration-ms",
        type=int,
        default=None,
        help="Optional auto-stop after this many ms.",
    )

    p_preset = sub.add_parser(
        "preset-goto",
        help="Move to a saved preset by name or numeric id.",
    )
    p_preset.add_argument(
        "preset_id",
        metavar="name_or_id",
        help=(
            "Preset name (e.g. 'Overview') or numeric id (e.g. '1'). "
            "Names are matched case-insensitively. Run 'presets' to see all."
        ),
    )

    p_privacy = sub.add_parser("privacy", help="Turn privacy (lens-cover) mode on/off.")
    p_privacy.add_argument("state", choices=["on", "off"])

    p_snap = sub.add_parser("snapshot", help="Capture a JPEG from a lens.")
    p_snap.add_argument("--lens", default=None, help="Lens id (defaults to first lens).")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    cam = args.camera

    try:
        if args.command == "status":
            _print(get_status(cam))
        elif args.command == "devices":
            _print(list_devices())
        elif args.command == "presets":
            presets = list_presets(cam)
            if not presets:
                print(
                    "no presets saved on this camera (or ONVIF is not reachable).",
                    file=sys.stderr,
                )
                return 1
            for p in presets:
                print(f"{p['id']:<6}{p.get('name', '(unnamed)')}")
            return 0
        elif args.command == "ptz":
            _print(ptz(cam, args.direction, speed=args.speed, duration_ms=args.duration_ms))
        elif args.command == "ptz-continuous":
            _print(
                ptz_continuous(
                    cam,
                    pan=args.pan,
                    tilt=args.tilt,
                    zoom=args.zoom,
                    duration_ms=args.duration_ms,
                )
            )
        elif args.command == "preset-goto":
            _print(goto_preset(cam, args.preset_id))
        elif args.command == "privacy":
            _print(set_privacy(cam, args.state == "on"))
        elif args.command == "snapshot":
            _print(snapshot(cam, args.lens))
        else:  # pragma: no cover - argparse enforces a valid subcommand
            return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text
        print(
            f"dashboard returned {exc.response.status_code}: {detail}",
            file=sys.stderr,
        )
        return 1
    except httpx.HTTPError as exc:
        print(
            f"request to {DASHBOARD_URL} failed: {exc}\n"
            "(are you connected to the lab Tailnet?)",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
