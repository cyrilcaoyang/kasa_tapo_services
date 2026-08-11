# kasa-tapo-services

A small FastAPI gateway that publishes the lab's Wi-Fi-only TP-Link **Kasa smart plugs** (HS103, HS300) and **Tapo cameras** (C100/C200/C210/C220/C225/C245D) as STATUS_SPEC-conformant equipment to the [AC Organic Self-driving Lab](https://github.com/AccelerationConsortium/ac-organic-lab) dashboard.

This package exists for two reasons:

1. **Tapo cameras and Kasa plugs do not speak HTTP**. They speak proprietary protocols (Kasa `XOR`/`KLAP` over TCP, Tapo HTTPS/RTSP/ONVIF). The gateway translates them to the dashboard's normalized HTTP envelope.
2. **They are only reachable over the lab Wi-Fi**, which is neither the tailnet nor the wired network the rest of the lab's device PCs use. The gateway runs on the dashboard host — the one machine attached to all three — and re-exposes the devices through the same `equipment.yaml` registry as every other piece of equipment.

### Which network, exactly

This is the detail that trips people up, so it is worth stating plainly: **these devices are on Wi-Fi (WLAN), not on the wired LAN.** The dashboard host is multi-homed, and only one of its three interfaces can see a camera:

| Interface | Network | What lives there |
|---|---|---|
| `wlp9s0` (**Wi-Fi**) | `172.31.0.0/16` | **Every camera and plug in this repo.** The gateway's only path to them. |
| `eno1` (wired) | `10.21.10.0/16` | Institutional wired network. **No route to any camera.** |
| `tailscale0` | `100.64.254.6` | How the dashboard and the rest of the lab reach *this gateway*. |

Consequences worth internalizing:

* A camera's `host:` in `devices.yaml` is always a **`172.31.x.x` Wi-Fi address**. Putting a wired or tailnet address there cannot work.
* Wi-Fi is the weakest link in the chain. A camera reporting `unknown` / "unreachable" is far more often a Wi-Fi association or DHCP-lease problem than a dead camera — check `ping` from the gateway host before touching the device.
* The devices hold **DHCP leases**, so an address can move. If a previously-working camera goes unreachable, re-check its current address in the Tapo/Kasa app before assuming hardware failure.

```
┌────────── lab Wi-Fi (wlp9s0, 172.31.0.0/16) ──────────┐   ┌────── dashboard host (gaia) ──────┐
│                                                       │   │                                   │
│  Tapo C245D  ×2   (dual lens, PTZ, RTSP + ONVIF)      │◀──┤  go2rtc  :1984   (RTSP→MSE/WebRTC)│
│  Tapo C100   ×1   (fixed, no PTZ)                     │◀──┤  kasa-tapo-services  :8002        │◀── tailnet
│  Kasa HS300  ×2   (6-outlet strips)                   │◀──┤    cameras + plugs gateway        │    (100.64.254.6)
│                                                       │   │                                   │
└───────────────────────────────────────────────────────┘   └───────────────────────────────────┘
```

### Current fleet

The five devices this gateway fronts today (see `devices.yaml`):

| `id` | Model | Lenses / outlets | Notes |
|---|---|---|---|
| `cam_hte_tapo_c245` | Tapo C245D | `wide` → `stream1`, `tele` → `stream6` | PTZ on the tele lens only |
| `cam_echem_tapo_c245` | Tapo C245D | `wide` → `stream1`, `tele` → `stream6` | Echem bench; same credentials as the HTE unit |
| `cam_echem_tapo_c100` | Tapo C100 | `main` → `stream1` | **Fixed** — no PTZ service, no presets |
| `plug_hte_strip_right` | Kasa HS300 | 6 outlets | Legacy XOR protocol, port 9999, no credentials |
| `plug_hte_strip_left` | Kasa HS300 | 6 outlets | Legacy XOR protocol, port 9999, no credentials |

> **PTZ on the C245D is tele-only.** The wide lens is fixed to the camera base; ONVIF PTZ moves only the telephoto lens. This is a property of the hardware, not a gateway limitation.

## Supported device kinds

| Kind          | Models               | Backend                          | Control surface                    |
|---------------|----------------------|----------------------------------|------------------------------------|
| `camera`      | Tapo C200/C210/C220, C225, **C245D** (dual lens), **C100** (fixed, no PTZ)  | `pytapo` (privacy/day-night) + ONVIF (PTZ + presets) | nudge/continuous PTZ, save/goto/delete preset, privacy/streaming |
| `smart_plug`  | Kasa **HS103** (and HS100/HS105/HS110) | `python-kasa` | `on` / `off` / `toggle` |
| `power_strip` | Kasa **HS300** (6 outlets, KP303 too)  | `python-kasa` | `on` / `off` / `toggle` (whole strip or per-outlet via `outlet:`) |

Cameras emit a `details.lenses[]` block (one entry per physical lens) and a `details.presets[]` list. **Fixed cameras** (C100/C110/…) answer ONVIF device + media calls but expose no PTZ service; they are fully reachable and streamable, and the gateway simply omits `ptz` / `preset/*` from `allowed_actions` and reports no presets. Power strips emit one `ComponentStatus` per outlet under `components` (`outlet_0`, `outlet_1`, …) so the dashboard can render the outlet grid generically.

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

1. Copy `devices.yaml.example` to `devices.yaml` and fill in each device's **Wi-Fi address** (`172.31.x.x` — see [Which network, exactly](#which-network-exactly)), kind, and (for cameras) per-lens RTSP paths and ONVIF port.
2. Copy `.env.example` to `.env` and fill in per-device credentials. Variable names follow the pattern `<DEVICE_ID_UPPERCASE>_USER` / `<DEVICE_ID_UPPERCASE>_PASS`.
3. (Cameras only) On the camera, create a **Tapo Camera Account** (Tapo app → camera → Settings → Advanced → Camera Account) and an **ONVIF account** (Settings → Advanced → ONVIF). Use the same credentials for both unless you need them to differ.

### Verify camera RTSP

Tapo dual-lens models (e.g. **C245D**) publish the two lenses on
**non-adjacent** RTSP paths per TP-Link's RTSP/ONVIF FAQ:

* wide-angle: `/stream1` (HD), `/stream2` (SD)
* telephoto: `/stream6` (HD), `/stream7` (SD)

Single-lens models (**C100**, C200/C210/C220) only expose `/stream1` (HD)
and `/stream2` (SD) — wire just `/stream1` as the single lens. Confirm
before deploying:

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

### Video latency: MSE vs WebRTC

The bridge ships two protocols to the browser side-by-side; dashboards
pick per-`<video>` element.

| Protocol | Port path | Typical latency | Notes |
|----------|-----------|-----------------|-------|
| **MSE**     | WebSocket via `/streams/*` (Caddy → go2rtc :1984) | 0.5 – 1.5 s | No special firewalling; works through any HTTP reverse proxy. Default; good enough for static observation. |
| **WebRTC**  | Signaling via `/streams/api/webrtc`, **media via TCP `:8555` direct** to dashboard host | 100 – 300 ms | Lower latency makes PTZ feel real-time. TCP-only by default so Tailscale ACLs only need one port hole. |

WebRTC is enabled by default in the rendered `go2rtc.yaml`. Point the
browser at the right host by setting one env var in
`/etc/kasa-tapo-services/.env`:

```
GO2RTC_WEBRTC_HOST=sdl2-server-gaia.tail6a1dd7.ts.net   # dashboard's MagicDNS name
```

The bootstrap renders this into `webrtc.candidates`, so the browser
opens its TCP connection to `<host>:8555` directly (raw DTLS/SRTP — no
HTTP, no Caddy passthrough). To disable WebRTC entirely and stay
MSE-only, set `GO2RTC_WEBRTC_LISTEN=` (empty string).

> Both consumers have shipped on the dashboard side. `web/`'s
> `CameraPlayer` picks per browser: `WebRtcPlayer` where `MediaSource`
> is unavailable (notably iPhone Safari), `MsePlayer` everywhere else.

## Wire into the dashboard

In `ac-organic-lab/equipment.yaml`:

```yaml
- id: cam_hte_tapo_c245
  name: HTE Camera
  kind: camera
  adapter: http
  protocol: "1.0"
  base_url: http://127.0.0.1:8002
  status_path: /cameras/cam_hte_tapo_c245/status
  poll_timeout_seconds: 5.0     # ONVIF SOAP can take 1-2s on its own
  tiles:
    hte: { w: 2, h: 3 }         # keyed by the platforms.yaml section id
  pills: {}
  camera:
    host: 172.31.60.16          # Wi-Fi address, not wired and not tailnet
    onvif_port: 2020
    lenses:
      - { id: wide, label: Wide, rtsp_path: stream1 }
      - { id: tele, label: Tele, rtsp_path: stream6 }   # /stream2 is SD-wide on dual-lens
```

Two registry-schema details that changed under us and are easy to get
wrong (both are `equipment.yaml` **schema v2**):

* There is **no `platform:` field**. Section membership is declared in
  `platforms.yaml` instead — add the id to that section's `equipment:`
  list, where its position sets the display order.
* Tile sizing is `tiles: {<section_id>: {w, h}}`, **not** a bare `tile:`.
  A missing section key defaults to `{w: 2, h: 1}`.

Then restart the dashboard API — the unit is `ac-organic-lab-api.service`:

```bash
sudo systemctl restart ac-organic-lab-api.service
```

The camera tile (PTZ pad, preset selector, snapshot/record buttons)
appears on `/platforms/{section}`; the Overview card shows a collapsed
"Show stream" toggle rather than a live feed, so the landing page does
not pull video for every visitor.

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
| POST   | `/cameras/{id}/control/rolling/start`                 | `{lens?, segment_duration_s?, max_segments?, include_audio?}` |
| POST   | `/cameras/{id}/control/rolling/stop`                  | -                                     |
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

Plus the gateway-level routes, which describe the **gateway process
itself** rather than any one device:

| Method | Path       | Body / purpose |
|--------|------------|----------------|
| GET    | `/`        | Service info (`ProbeResponse`) |
| GET    | `/health`  | `{"status": "healthy"}` while the process is alive |
| GET    | `/status`  | A full `EquipmentStatus` envelope for the gateway (`equipment_id: kasa_tapo_gateway`, `kind: other`), with `metrics.cameras` / `metrics.plugs` counting what it fronts. Registered in `equipment.yaml` as its own tile, so an operator can tell "the gateway is down" from "one camera is down". |
| GET    | `/devices` | Enumerate every configured camera and plug |

## Tests

```bash
uv run pytest -q
```

Tests use mocked Tapo/ONVIF/Kasa clients - they do not require live hardware.

## Conformance

### Contract types come from `sdl-lab-contract`

The STATUS_SPEC types are **imported**, not vendored. `models.py` used to
carry a hand-copied mirror of the v1.0 envelope; it now re-exports the
shared [`sdl-lab-contract`](https://github.com/AccelerationConsortium/sdl-lab-contract)
package (pinned by tag in `[tool.uv.sources]`), which is the single
source for these models across the lab — ac-organic-lab
`ARCHITECTURE.md` LG5.

```python
from kasa_tapo_services.models import EquipmentStatus, ComponentStatus, MetricValue
# -> re-exported from sdl_lab_contract; identical to what every other
#    device repo and the lab-skills SDK parse against.
```

Two consequences of the swap:

* The `camera` / `smart_plug` / `power_strip` kinds are now **part of the
  shared enum**, so there is nothing left to extend locally. The comment
  in the old `models.py` promising to keep a private copy in sync with
  `lab_skills.models` is obsolete — there is one definition now.
* The envelope gained the v1.2 fields `activity` and `activity_since`.
  This gateway leaves them at their defaults (`"unknown"` / `null`),
  which is the honest answer: it does not observe a "primary operation"
  for a camera or a plug. Readers already treat an absent or `unknown`
  activity as "the device did not say" (spec §8), so nothing downstream
  changes.

### Why this gateway stays on `protocol_version: "1.0"`

Importing v1.2 types does **not** make this a v1.2 device, and the
version it reports is deliberately unchanged.

This gateway exposes a real `/control/*` surface (PTZ, presets, privacy,
recording, plug switching) but implements **no claim protocol** — no
`/control/claim`, `/control/heartbeat`, or `/control/release`, and no
`X-Claim-Token` enforcement. STATUS_SPEC §9 is explicit about this case:
the read-only exemption that lets a monitoring-only device declare v1.1
or v1.2 on read-side merit alone applies only to a device with *nothing
to claim*. **Partial control without claims stays v1.0.** The exemption
is for having no actions to serialize, not for finding claims
inconvenient.

Practically this means concurrent writers are not arbitrated here: two
clients can both move the same camera. That is tolerable for the
convenience-class actions this gateway offers (nothing it exposes can
damage hardware or a sample, which is also why the dashboard leaves
`kind: camera` out of its control-password gate) and it is the reason
plug outlets driving *equipment* are gated on the dashboard side
instead. Adding claims is the prerequisite for ever reporting a higher
version.

### Per-device surface

Beyond the baseline envelope:

* `equipment_kind` may be `camera`, `smart_plug`, or `power_strip`.
* For cameras, `details` contains:
  * `lenses: [{id, label, rtsp_path, mse_url, stream_connected, recording_active, recording_started_at}]`
  * `presets: [{id, name}]`
  * `privacy_mode: bool`
  * `streaming_enabled: bool`
* For power strips, `components` contains one `ComponentStatus` per outlet (`outlet_0`, `outlet_1`, …).

### Reachability and `equipment_status` (unreachable backing hardware)

This gateway fronts hardware (cameras, plugs) over the lab Wi-Fi. When the
gateway process is healthy but **cannot reach the backing device** (Wi-Fi
association lost, DHCP lease moved, power off — e.g. `No route to host`, or a
camera where neither ONVIF nor the Tapo API responds), `/status` still returns
**HTTP 200** (the gateway itself is alive, per spec best-practice #2) and
reports:

* `equipment_status: "unknown"` — the device's state *cannot be determined*.
  This is deliberately **not** `error`: nothing faulted, we simply can't reach
  it. `error` / `degraded` are reserved for a *reachable* device whose subsystem
  reports a fault (e.g. camera answering ONVIF but with go2rtc down → `degraded`).
* `message` carries the reason (e.g. `"Camera unreachable: neither ONVIF nor
  Tapo API responded"`).

The dashboard renders a gateway-fronted device reporting `unknown` as
**"unreachable"** (offline / counted as down), since the gateway answering 200
means there is no transport-level `fetch_error` for the aggregator to key on.
See the lab contract's [`STATUS_SPEC.md` §2.1 — `unknown` vs `error` vs
"unreachable"](../ac-organic-lab/docs/STATUS_SPEC.md) for the normative rules.

### Diagnosing an unreachable device

Because the path to every device is Wi-Fi, work outward from the radio
before suspecting the camera. Run all of this **on the gateway host** —
reachability from anywhere else proves nothing:

```bash
# 1. Is the Wi-Fi interface up and on the device subnet?
ip -br addr show wlp9s0            # expect 172.31.x.x/16, state UP

# 2. Does the route to the device actually leave via Wi-Fi?
ip route get 172.31.60.16          # expect "dev wlp9s0"

# 3. Is the device answering at all?
ping -c3 172.31.60.16

# 4. Are its service ports open?  (cameras: 554 RTSP + 2020 ONVIF;
#    Kasa plugs: 9999, legacy XOR protocol)
nc -vz 172.31.60.16 554 2020

# 5. Does RTSP actually negotiate with these credentials?
ffprobe -v error -hide_banner rtsp://$USER:$PASS@172.31.60.16:554/stream1
```

Reading the result:

| Where it fails | Most likely cause |
|---|---|
| Step 1 or 2 | Gateway host's Wi-Fi dropped, or the route is going out the wired interface. Nothing device-side to fix. |
| Step 3 | Device is off, or its DHCP lease moved. Re-check the address in the Tapo/Kasa app and update `devices.yaml`. |
| Step 4 (ONVIF only) | ONVIF disabled or wrong port — Tapo app → Advanced → ONVIF, default 2020. Tile goes `degraded`, not unreachable. |
| Step 5 | Credentials. `<ID>_USER`/`<ID>_PASS` must be the **Camera Account**, which is distinct from the ONVIF account and from your TP-Link cloud login. |

Two failure modes that look like a dead device but are not:

* **`stream_connected: false` on every lens is normal when nobody is
  watching.** go2rtc connects to RTSP on demand, so an idle camera shows
  no producer. Only worry if it stays false while a stream is open.
* **`tapo_reachable: false` while `onvif_reachable: true`** costs you the
  privacy and day/night toggles but nothing else — health needs only
  ONVIF and go2rtc, so the tile stays `ready`. Repeated failed pytapo
  logins trigger a device-side lockout (`Temporary Suspension: Try again
  in N seconds`) that the poll loop *keeps renewing*, so it will not
  clear on its own: fix the Camera Account credentials, then restart the
  gateway to stop the retry loop feeding it.
