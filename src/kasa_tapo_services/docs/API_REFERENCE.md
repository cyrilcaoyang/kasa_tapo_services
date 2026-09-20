# Camera & Plug Gateway — API reference

Base: `http://127.0.0.1:8002` (loopback; the dashboard's Caddy is the only
thing that should hit it directly). All timestamps UTC ISO-8601. Schemas:
`/openapi.json`. Tags: `meta`, `cameras`, `plugs`, `documentation`.

`{camera_id}` and `{plug_id}` must match `^[a-z][a-z0-9_]*$`, 1–64 characters.
A path that does not match is **422**; a well-formed id that is not in
`devices.yaml` is **404** (`Unknown camera: <id>` / `Unknown plug: <id>`). Every
device route also answers **503** `Gateway not initialised` if the process has
not finished startup. No route takes authentication and none takes a claim
token — there is no claim protocol here, so **423 never occurs**.

## Gateway level (tag `meta`)

| route | gate | returns |
|---|---|---|
| `GET /` | — | `{service, version, boot_time, device_count}` — the identity probe a human curls |
| `GET /health` | — | `{"status": "healthy"}` |
| `GET /status` | — | `EquipmentStatus` for the gateway itself: `equipment_id: "kasa_tapo_gateway"`, `equipment_kind: "other"`, always `ready`, `uptime_seconds`, `metrics.cameras` / `metrics.plugs` (counts). Says nothing about device reachability. |
| `GET /devices` | — | `{cameras: [{id, name, host, lenses[], tapo_configured, onvif_configured}], plugs: [{id, name, kind, host, outlets[]}]}` — read straight from config, no hardware polled |
| `GET /openapi.json`, `GET /docs` | — | OpenAPI document, Swagger UI |
| `GET /agent-docs`, `GET /agent-docs/api-reference`, `GET /llms.txt` | — | this documentation (`text/markdown`, `text/plain`) |

## Camera reads (tag `cameras`, side-effect-free)

| route | gate | returns |
|---|---|---|
| `GET /cameras/{camera_id}/` | — | `ProbeResponse` — `equipment_id`, `equipment_name`, `protocol_version` (`"1.0"`) |
| `GET /cameras/{camera_id}/health` | — | `{"status": "healthy"}` for any known id, **even when the camera is unreachable** — this is process liveness |
| `GET /cameras/{camera_id}/status` | — | full `EquipmentStatus`; served from the poller cache (5 s default), built live before the first poll |

`EquipmentStatus` for a camera carries `equipment_kind: "camera"`, `host`,
`equipment_status`, `message`, `allowed_actions`, `components`, `details`,
`device_time`. `activity` is always `"unknown"` and `last_error` always `null`.

`components` has one `lens_<lens_id>` entry per lens: `{connected, state,
message}` where `state` is go2rtc's producer state (`connected`,
`reconnecting`, `disconnected`, `unknown`, or `error: <text>`) and `message` is
the lens label. Stream health is informational and does not gate
`equipment_status`.

`details` (`CameraDetails`):

| field | meaning |
|---|---|
| `lenses[]` | `{id, label, rtsp_path, mse_url, stream_connected, recording_active, recording_started_at, rolling_active, rolling_started_at, rolling_segment_count}`. `mse_url` is `/streams/api/ws?src=<camera_id>_<lens_id>` on the dashboard's proxy, or `null` when streaming is toggled off. |
| `presets[]` | `{id, name}` from ONVIF; empty on a camera with no PTZ service |
| `privacy_mode` | what the Tapo control API reports; `false` when that plane is unreachable |
| `streaming_enabled` | the gateway's in-memory flag (see `POST /control/streaming`) |
| `onvif_reachable`, `tapo_reachable`, `go2rtc_reachable` | which control planes answered this poll |
| `has_zoom` | whether the ONVIF PTZ node advertises a continuous zoom velocity space. `false` on every camera in this fleet. |

`allowed_actions` is `ptz`, `preset/save`, `preset/goto`, `preset/{id}` (ONVIF
reachable **and** a PTZ service present), `privacy` (Tapo API reachable),
`streaming` (go2rtc reachable), plus `snapshot`, `recording/start`,
`recording/stop`, `recording/cancel`, `rolling/start`, `rolling/stop`
unconditionally.

## Camera control (tag `cameras`)

No claim header. The gates below are per-request; `allowed_actions` is advisory
and does not itself block a call.

| route | body | gate | refusals |
|---|---|---|---|
| `POST /cameras/{camera_id}/control/ptz` | nudge `{direction, speed=0.5 (0–1), duration_ms=400 (0–5000)}` **or** continuous `{pan, tilt, zoom ∈ [-1,1], duration_ms?≤10000}` | ONVIF client configured | **503** ONVIF not configured · **409** `Zoom not supported` when a non-zero zoom is commanded on a node with no zoom axis · **502** any other ONVIF failure (including `no PTZ service (fixed lens)` on a C100) |
| `POST /cameras/{camera_id}/control/preset/save` | `{name}` (1–64 chars) | ONVIF client configured | **503** · **409** preset storage full (nothing overwritten) · **502** |
| `POST /cameras/{camera_id}/control/preset/goto` | `{preset_id}` | ONVIF client configured | **503** · **502** (includes an unknown preset id) |
| `DELETE /cameras/{camera_id}/control/preset/{preset_id}` | — | ONVIF client configured | **503** · **502** |
| `POST /cameras/{camera_id}/control/privacy` | `{enabled: bool}` | pytapo client configured | **503** pytapo not configured · **502** the control API rejected the call (commonly a credentials problem — see the agent guide) |
| `POST /cameras/{camera_id}/control/streaming` | `{enabled: bool}` | — | none; flips an in-memory flag, never fails |
| `POST /cameras/{camera_id}/control/snapshot` | `{lens?}` (body optional; defaults to the first lens) | `<ID>_USER`/`<ID>_PASS` set | **503** no RTSP credentials · **400** unknown lens id · **502** ffmpeg failed or timed out (8 s) |
| `POST /cameras/{camera_id}/control/recording/start` | `{lens?, max_duration_s?=3600 (1–86400)}` | `<ID>_USER`/`<ID>_PASS` set | **409** that lens is already recording · **503** no RTSP credentials · **400** unknown lens · **502** ffmpeg failed |
| `POST /cameras/{camera_id}/control/recording/stop` | `{recording_id?}` | — | **404** unknown `recording_id` · **409** no active recording · **400** more than one active and no id given · **502** stop failed |
| `POST /cameras/{camera_id}/control/recording/cancel` | `{recording_id?}` | — | same resolution refusals as stop · **502** cancel failed |
| `POST /cameras/{camera_id}/control/rolling/start` | `{lens?, segment_duration_s=1800 (60–7200), max_segments=96 (1–1000), include_audio=false}` | `<ID>_USER`/`<ID>_PASS` set | **400** unknown lens · **409** a rolling recorder is already running on that lens · **503** no RTSP credentials |
| `POST /cameras/{camera_id}/control/rolling/stop` | **query** `?lens=<id>` (omit to stop every rolling recorder on the camera) | — | **400** unknown lens · **409** no active rolling recorder |

Responses:

| route | 2xx body |
|---|---|
| `ptz` | `ControlAck` — `{ok: true, message: "nudged <direction>" \| "moving" \| "stopped"}`, or `{ok: false, message: "<axes> limit reached"}` when the head is at a physical pan/tilt stop |
| `preset/save` | `ControlAck` with `state: {preset_id, name}` |
| `preset/goto`, `DELETE preset/{id}` | `ControlAck` with `state: {preset_id}` |
| `privacy` | `ControlAck` with `state: {privacy_mode}` |
| `streaming` | `ControlAck` with `state: {streaming_enabled}` |
| `snapshot` | `SnapshotResponse` `{path, url, taken_at, lens, width, height, bytes}` — `width`/`height` are always `null` |
| `recording/start` | `RecordingStartResponse` `{recording_id, path, url, lens, started_at, max_duration_s}` |
| `recording/stop` | `RecordingStopResponse` `{recording_id, path, url, started_at, stopped_at, duration_ms, bytes, finalized}` — `finalized: false` means the `.mp4.partial` did not become a `.mp4` |
| `recording/cancel` | `RecordingCancelResponse` `{recording_id, canceled, deleted_path}` |
| `rolling/start` | `ControlAck` with `state: {lens, segment_duration_s, max_segments, include_audio}` |
| `rolling/stop` | `RollingStopResponse` `{ok, message, segments_recorded}` |

Every `ControlAck.state` is a snapshot taken immediately after the action, not
a readback; re-poll `/status` for ground truth. Each control route wakes that
device's poller so the cached envelope catches up within a cycle.

## Camera media (tag `cameras`)

| route | gate | returns / refusals |
|---|---|---|
| `GET /cameras/{camera_id}/media` | — | `{snapshots: [...], recordings: [...]}`, newest first per lens; each entry `{name, lens, kind, bytes, mtime, url, abs_path}`. `.partial` files are skipped and **rolling segments are not listed** — they live in a separate directory. |
| `GET /cameras/{camera_id}/media/{kind}/{lens}/{name}` | — | the file. `kind` is `snapshots` or `recordings` only. **400** unknown kind, path traversal, or a `.partial` file (in-progress recordings are never served — poll `details.lenses[].recording_active` instead) · **404** the file is not on disk |

## Plugs (tag `plugs`)

| route | gate | returns |
|---|---|---|
| `GET /plugs/{plug_id}/` | — | `ProbeResponse` |
| `GET /plugs/{plug_id}/health` | — | `{"status": "healthy"}` for any known id, even when the plug is unreachable |
| `GET /plugs/{plug_id}/status` | — | full `EquipmentStatus`; poller cache (2 s default), built live before the first poll |

A reachable strip reports `equipment_status: "ready"`, one
`components.outlet_<i>` per outlet (`state` `on`/`off`, `message` the label from
`devices.yaml`), metrics `power_outlet_<i>` (W), `current_outlet_<i>` (A),
`energy_kwh_today_outlet_<i>` (kWh) for outlets that report them, `rssi` (dBm),
and `details` `{is_strip, alias, model, outlets: [{index, label, is_on,
power_w, voltage_v, current_a, energy_kwh_today, energy_kwh_total}]}`. A single
plug reports one `components.plug` entry and no per-outlet metrics. An
unreachable plug reports `unknown` with the failure in `message` and no
components. `allowed_actions` is `["on", "off", "toggle"]` whenever the device
answered.

| route | body | refusals |
|---|---|---|
| `POST /plugs/{plug_id}/control/on` | `{outlet?}` (0–31; omit for the whole device) | **400** outlet out of range, or an outlet given for a device with no addressable outlets · **502** the Kasa call failed |
| `POST /plugs/{plug_id}/control/off` | `{outlet?}` | same |
| `POST /plugs/{plug_id}/control/toggle` | `{outlet?}` | same |

All three return `ControlAck` with `state: {outlet, is_on}`. For `on`/`off`
`is_on` is the state that was requested; for `toggle` it is the new state the
client computed from the last read. **Omitting `outlet` on a six-outlet strip
switches all six at once.**

## Refusal summary

**200 + `ok: false`** hardware declined (PTZ limit) · **400** bad lens, outlet,
recording selection or media path · **404** unknown device, recording or file ·
**409** conflicts with fixed configuration or current state · **422** request
validation · **502** backend call failed (ONVIF / Tapo / Kasa / ffmpeg) ·
**503** subsystem never configured for this device, or gateway still starting.
No **412** (there are no precondition gates) and no **423** (there are no
claims).
