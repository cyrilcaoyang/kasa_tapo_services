# kasa-tapo-services

A small FastAPI gateway that publishes lab LAN-only TP-Link **Kasa smart plugs** (HS103, HS300) and **Tapo cameras** (C200/C210/C220/C225/C245D) as STATUS_SPEC v1.0-conformant equipment to the [AC Organic Self-driving Lab](https://github.com/your-org/ac-organic-lab) dashboard.

This package exists for two reasons:

1. **Tapo cameras and Kasa plugs do not speak HTTP**. They speak proprietary protocols (Kasa `XOR`/`KLAP` over TCP, Tapo HTTPS/RTSP/ONVIF). The gateway translates them to the dashboard's normalized HTTP envelope.
2. **They live on the lab LAN, not the tailnet.** The gateway runs on the dashboard host (which has both interfaces) and re-exposes the devices to the rest of the lab through the same `equipment.yaml` registry as every other piece of equipment.

```
┌─────────── lab LAN ───────────┐    ┌──────── dashboard host ────────┐
│                               │    │                                │
│  Tapo C245D  (RTSP + ONVIF)   │◀───┤  go2rtc  :1984  (RTSP→MSE/WS)  │
│  Tapo C210                    │◀───┤  kasa-tapo-services  :8002     │◀── tailnet
│  Kasa HS103                   │◀───┤    cameras + plugs gateway     │
│  Kasa HS300                   │◀───┤                                │
└───────────────────────────────┘    └────────────────────────────────┘
```

## Supported device kinds

| Kind          | Models               | Backend                          | Control surface                    |
|---------------|----------------------|----------------------------------|------------------------------------|
| `camera`      | Tapo C200/C210/C220, C225, **C245D** (dual lens)  | `pytapo` (privacy/day-night) + ONVIF (PTZ + presets) | nudge/continuous PTZ, save/goto/delete preset, privacy/streaming |
| `smart_plug`  | Kasa **HS103** (and HS100/HS105/HS110) | `python-kasa` | `on` / `off` / `toggle` |
| `power_strip` | Kasa **HS300** (6 outlets, KP303 too)  | `python-kasa` | `on` / `off` / `toggle` (whole strip or per-outlet via `outlet:`) |

Cameras emit a `details.lenses[]` block (one entry per physical lens) and a `details.presets[]` list. Power strips emit one `ComponentStatus` per outlet under `components` (`outlet_0`, `outlet_1`, …) so the dashboard can render the outlet grid generically.

## Install

```bash
cd /path/to/kasa_tapo_services
uv venv
uv pip install -e ".[dev]"

# Required for /control/snapshot + /control/recording/* (stream-copy MP4):
#   macOS:   brew install ffmpeg
#   Debian:  sudo apt-get install ffmpeg
ffmpeg -version | head -1
```

## Configure

1. Copy `devices.yaml.example` to `devices.yaml` and fill in each device's lab-LAN IP, kind, and (for cameras) per-lens RTSP paths and ONVIF port.
2. Copy `.env.example` to `.env` and fill in per-device credentials. Variable names follow the pattern `<DEVICE_ID_UPPERCASE>_USER` / `<DEVICE_ID_UPPERCASE>_PASS`.
3. (Cameras only) On the camera, create a **Tapo Camera Account** (Tapo app → camera → Settings → Advanced → Camera Account) and an **ONVIF account** (Settings → Advanced → ONVIF). Use the same credentials for both unless you need them to differ.

### Verify camera RTSP

Tapo dual-lens models (e.g. **C245D**) publish the two lenses on
**non-adjacent** RTSP paths per TP-Link's RTSP/ONVIF FAQ:

* wide-angle: `/stream1` (HD), `/stream2` (SD)
* telephoto: `/stream6` (HD), `/stream7` (SD)

Single-lens models (C200/C210/C220) only expose `/stream1` and
`/stream2`. Confirm before deploying:

```bash
ffprobe -v error -hide_banner rtsp://$USER:$PASS@$IP:554/stream1
ffprobe -v error -hide_banner rtsp://$USER:$PASS@$IP:554/stream6   # tele on dual-lens
```

If `ffprobe` returns identical resolution / fingerprint for `/stream2`
and `/stream6`, your camera is single-lens and `/stream2` is just an SD
copy of the wide stream - do **not** wire it as a separate lens. Update
the `rtsp_path` field in `devices.yaml` to whatever works.

## Run

```bash
# Foreground:
uv run uvicorn kasa_tapo_services.main:app --host 127.0.0.1 --port 8002

# Render the go2rtc config (one-shot; usually run as systemd ExecStartPre):
uv run kasa-tapo-bootstrap-go2rtc
```

The gateway binds to `127.0.0.1` by default. The dashboard's reverse proxy (Caddy) is the only thing that should hit `:8002` directly.

## Wire into the dashboard

In `ac-organic-lab/equipment.yaml`:

```yaml
- id: cam_hte_tapo_c245
  name: HTE Camera
  platform: hte
  kind: camera
  adapter: http
  protocol: "1.0"
  base_url: http://127.0.0.1:8002
  status_path: /cameras/cam_hte_tapo_c245/status
  poll_timeout_seconds: 5.0     # ONVIF SOAP can take 1-2s on its own
  tile: { w: 2, h: 3 }
  camera:
    host: 192.168.1.42
    onvif_port: 2020
    lenses:
      - { id: wide, label: Wide, rtsp_path: stream1 }
      - { id: tele, label: Tele, rtsp_path: stream6 }   # /stream2 is SD-wide on dual-lens
```

Restart `ac-dashboard-api.service` and the camera tile (with PTZ pad,
preset selector, and snapshot/record buttons) should appear on the
matching platform's panel and on `/platforms/{platform}`.

## REST surface

For each device the gateway publishes:

| Method | Path                                                  | Body                                  |
|--------|-------------------------------------------------------|---------------------------------------|
| GET    | `/cameras/{id}/`                                      | -                                     |
| GET    | `/cameras/{id}/health`                                | -                                     |
| GET    | `/cameras/{id}/status`                                | -                                     |
| POST   | `/cameras/{id}/control/ptz`                           | `{direction, speed?, duration_ms?}` or `{pan, tilt, zoom?}` (continuous) |
| POST   | `/cameras/{id}/control/preset/save`                   | `{name}`                              |
| POST   | `/cameras/{id}/control/preset/goto`                   | `{preset_id}`                         |
| DELETE | `/cameras/{id}/control/preset/{preset_id}`            | -                                     |
| POST   | `/cameras/{id}/control/privacy`                       | `{enabled}`                           |
| POST   | `/cameras/{id}/control/streaming`                     | `{enabled}`                           |
| POST   | `/cameras/{id}/control/snapshot`                      | `{lens?}` (defaults to first lens)    |
| POST   | `/cameras/{id}/control/recording/start`               | `{lens?}`                             |
| POST   | `/cameras/{id}/control/recording/stop`                | `{recording_id?}`                     |
| POST   | `/cameras/{id}/control/recording/cancel`              | `{recording_id?}` (deletes partial)   |
| GET    | `/cameras/{id}/media`                                 | - (lists snapshots + recordings)      |
| GET    | `/cameras/{id}/media/{snapshots\|recordings}/{lens}/{name}` | - (binary file download)        |
| GET    | `/plugs/{id}/`                                        | -                                     |
| GET    | `/plugs/{id}/health`                                  | -                                     |
| GET    | `/plugs/{id}/status`                                  | -                                     |
| POST   | `/plugs/{id}/control/on`                              | `{outlet?}` (omit for whole device)   |
| POST   | `/plugs/{id}/control/off`                             | `{outlet?}`                           |
| POST   | `/plugs/{id}/control/toggle`                          | `{outlet?}`                           |

### Media capture

Snapshots and recordings are written by an `ffmpeg` subprocess pulling
from the camera's RTSP stream (`-c:v copy -c:a copy`, no re-encode) so
recordings are MP4 files containing the original H.264/AAC bytes. The
gateway names files with an ISO 8601 UTC timestamp:

```
<root>/snapshots/<camera_id>/<lens_id>/2026-05-09T02-24-56Z.jpg
<root>/recordings/<camera_id>/<lens_id>/2026-05-09T02-25-21Z_<recording_id>.mp4
```

`<root>` is, in priority order:

1. The per-camera `media.snapshots_dir` / `media.recordings_dir` from
   `devices.yaml` (paths starting with `~` are expanded against the
   gateway process's HOME).
2. `${KASA_TAPO_MEDIA_ROOT}/{snapshots,recordings}/<camera_id>/`.
3. `~/kasa-tapo-media/{snapshots,recordings}/<camera_id>/` (fallback).

`recording/cancel` removes the in-progress `.mp4.partial` file; `recording/stop`
finalises it by renaming to `.mp4`. Lens entries on `/cameras/{id}/status`
include `recording_active: bool` and `recording_started_at: datetime?` so
the dashboard can render a live "Recording …" indicator without polling
a separate endpoint. See `deploy/README.md` for the production filesystem
layout and required `ReadWritePaths=` whitelist.

Plus the gateway-level routes: `GET /` (service info), `GET /health`, `GET /devices` (enumerate all devices).

## Tests

```bash
uv run pytest -q
```

Tests use mocked Tapo/ONVIF/Kasa clients - they do not require live hardware.

## Conformance

`kasa-tapo-services` conforms to lab status spec **v1.0** (the per-device surface), with the following extensions:

* `equipment_kind` may be `camera`, `smart_plug`, or `power_strip`.
* For cameras, `details` contains:
  * `lenses: [{id, label, rtsp_path, mse_url, stream_connected, recording_active, recording_started_at}]`
  * `presets: [{id, name}]`
  * `privacy_mode: bool`
  * `streaming_enabled: bool`
* For power strips, `components` contains one `ComponentStatus` per outlet (`outlet_0`, `outlet_1`, …).
