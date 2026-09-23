# Camera & Plug Gateway — agent guide

This service is a **gateway**: one process at `http://127.0.0.1:8002` fronting
every Wi-Fi-only TP-Link device in the lab — five Tapo cameras and two Kasa
HS300 power strips — and publishing each of them as its own STATUS_SPEC
**v1.0** equipment. Every envelope it emits reports `protocol_version: "1.0"`.
Read this before driving it; the [API reference](/agent-docs/api-reference)
lists every route with bodies and refusal codes, and `/openapi.json` carries
the exact schemas.

## The addressing model

There is no single `/status` for "the devices". Each device is addressed by
its own id under a kind prefix, and every device has a full envelope of its
own:

| surface | routes | what it describes |
|---|---|---|
| gateway | `GET /`, `GET /health`, `GET /status`, `GET /devices` | this process |
| a camera | `GET /cameras/{camera_id}/`, `/health`, `/status`, `POST /cameras/{camera_id}/control/*`, `GET /cameras/{camera_id}/media*` | one camera |
| a plug | `GET /plugs/{plug_id}/`, `/health`, `/status`, `POST /plugs/{plug_id}/control/{on,off,toggle}` | one plug or strip |

The gateway's own `GET /status` is a *web-service* envelope
(`equipment_id: "kasa_tapo_gateway"`, `equipment_kind: "other"`). It is
`ready` whenever the process answers and says **nothing** about whether any
device is reachable — that is each per-device envelope's job.

Discover ids with `GET /devices`, which enumerates cameras (with their lenses)
and plugs (with their outlets) straight from `devices.yaml` without polling any
hardware. Do not hardcode ids: the fleet is a config file, not a constant. An
id must match `^[a-z][a-z0-9_]*$` and be ≤64 characters — a path that does not
match is rejected with **422** before any lookup happens; a well-formed id that
is not in the registry is **404**.

## The fleet today

| id | model | lenses / outlets | PTZ |
|---|---|---|---|
| `cam_hte_tapo_c245` | Tapo C245D | `wide`, `tele` | yes (tele lens only) |
| `cam_echem_tapo_c245` | Tapo C245D | `wide`, `tele` | yes (tele lens only) |
| `cam_ligand_tapo_d246` | Tapo C246D | `wide`, `tele` | yes (tele lens only) |
| `cam_echem_tapo_c100` | Tapo C100 | `main` | **no** — fixed lens |
| `cam_gibbie_tapo_c100` | Tapo C100 | `main` | **no** — fixed lens |
| `plug_hte_strip_right` | Kasa HS300 | 6 outlets | — |
| `plug_hte_strip_left` | Kasa HS300 | 6 outlets | — |

On the dual-lens PTZ units the wide lens is bolted to the base; ONVIF PTZ moves
the telephoto lens only. That is hardware, not a gateway limitation.

## Health vs. activity

`GET /{kind}/{id}/health` answers **"can the gateway process answer"**, not
"is the device online". A known-but-unplugged camera still returns
`{"status": "healthy"}`. Device reachability lives in `/status`.

`activity` is **always `"unknown"`** on every envelope this gateway emits. It
never observes idle/running, so a camera that is mid-recording and a strip with
every outlet on both read `unknown`. Do not infer usage from it — use
`details` (`recording_active`, `rolling_active`) and `components` instead.

`last_error` is **always `null`**. When a device is not `ready` the reason is
in `message` (human prose) and in the booleans under `details`. Branch on
`equipment_status` and `details`, never on the wording of `message`.

### Camera `equipment_status`

| value | when | `message` |
|---|---|---|
| `ready` | ONVIF reachable, go2rtc reachable, streaming on | `null` |
| `ready` | ONVIF reachable, streaming toggled off | `Streaming disabled by user` |
| `degraded` | ONVIF reachable, go2rtc unreachable | `ONVIF up but go2rtc unreachable; live video disabled` |
| `degraded` | ONVIF unreachable, Tapo control API reachable | `Tapo API up but ONVIF unreachable; PTZ disabled` |
| `unknown` | neither ONVIF nor the Tapo API answered | `Camera unreachable: neither ONVIF nor Tapo API responded` |

A camera never reports `error`. `error` is for a reachable device reporting a
fault; a camera that answers nothing cannot be diagnosed at all, so its state
is undetermined — `unknown`. In this lab the usual cause is the Wi-Fi
association or a moved DHCP lease, not a dead camera.

Note what the roll-up weighs: **ONVIF and go2rtc only**. The Tapo control plane
is not in it, so a camera whose privacy control is completely broken still
reports `ready`. See *Credentials* below.

### Plug `equipment_status`

`ready` when the Kasa read succeeded; `unknown` with the failure in `message`
when it did not. Same reasoning: an unreachable plug's on/off state cannot be
determined, and that is not the same thing as a fault.

## Polling, caching and staleness

Each device has a background poller that rebuilds its envelope on an interval —
**2 s for plugs, 5 s for cameras** by default (`KASA_TAPO_PLUG_POLL_INTERVAL_S`,
`KASA_TAPO_CAMERA_POLL_INTERVAL_S`). `/status` serves that cache, so dashboard
fan-out is decoupled from the 1–2 s ONVIF SOAP round trip and the multi-second
HS300 emeter sweep. Before the first poll completes, `/status` returns `unknown` while waiting
for the initial reading.

Polls have a 10 s deadline. Failed or timed-out polls replace cached readiness
with `unknown` and discard old readings and actions. Cached envelopes older
than 30 s are also served as `unknown`. `device_time` retains the time of the
last device reading. Camera reachability requires a fresh ONVIF request even
when the client is already connected.

Every `/control/*` route wakes its device's poller immediately, so a follow-up
`/status` reflects the change within one cycle. The `state` in a `ControlAck`
is a snapshot taken right after the action, not a readback; `/status` is ground
truth.

## There is no claim protocol

This service is STATUS_SPEC v1.0 *with* control and *without* claims: no
`/control/claim`, no `X-Claim-Token`, no **423** anywhere. Any caller that can
reach the port can swing a PTZ head or cut mains power, and two agents can
fight over the same camera with no arbitration. That is why the process binds
to `127.0.0.1` and sits behind the dashboard's reverse proxy. Coordinate
out-of-band.

## What each camera can actually do

`allowed_actions` is recomputed on every poll from what that camera's control
planes answered:

| advertised action(s) | condition |
|---|---|
| `ptz`, `preset/save`, `preset/goto`, `preset/{id}` | ONVIF reachable **and** the camera exposes an ONVIF PTZ service |
| `privacy` | the Tapo control API answered this poll |
| `streaming` | go2rtc reachable |
| `snapshot`, `recording/start`, `recording/stop`, `recording/cancel`, `rolling/start`, `rolling/stop` | always — these need only RTSP credentials, which are checked per request |

The gateway advertises only what the hardware supports. A C100 answers ONVIF
device and media calls but has no PTZ service at all, so `ptz` and `preset/*`
never appear for it — and that absence is permanent, not a transient outage.
The routes themselves are **not** gated on `allowed_actions`: POSTing
`/control/ptz` to a C100 still reaches the ONVIF layer and comes back **502**
(`camera ... has no PTZ service (fixed lens)`). Read `allowed_actions` first.

### Driving PTZ

One route, two body shapes: a **nudge** (`{direction, speed, duration_ms}`,
mousedown/mouseup style — the gateway runs the move and stops it for you) or a
**continuous** vector (`{pan, tilt, zoom, duration_ms?}`, which runs until you
send an all-zero body or `duration_ms` elapses).

They are an untagged union, and that has a sharp edge: a body that fails to
validate as a nudge falls through to the continuous shape, where unknown fields
are ignored and every velocity defaults to `0.0` — and an all-zero continuous
move means **stop**. So `{"direction": "upp"}` or `{"direction": "left",
"speed": 2.0}` does not 422; it returns **200** `{"message": "stopped"}`.
Check the acknowledgement: a real nudge answers `nudged <direction>`.

No camera in the fleet has a zoom axis. `details.has_zoom` is read from the
ONVIF PTZ node at connect time and is `false` on every unit here (probed live);
the "zoom" in the Tapo app is the wide→tele lens switch, and the dashboard
layers a digital zoom on the stream. `zoom_in` / `zoom_out`, and any non-zero
`zoom` in a continuous body, are refused with **409** rather than being handed
to firmware that would silently ignore them.

A nudge that ran but did not move the head answers **200 with `ok: false`** and
`"<axis> limit reached"` — the head is against a physical pan/tilt stop. This
is not an HTTP error: the ONVIF call succeeded, the hardware just had nowhere
to go. Limit detection compares the reported position before and after, so it
covers pan/tilt only and only when `|velocity| × duration_ms ≥ 40`; smaller
nudges and zoom-only moves come back `ok: true` whether or not anything moved.

Presets are ONVIF tokens. `details.presets` lists `{id, name}`; `preset/save`
returns the id the camera assigned. When the camera's preset storage is full
the save is refused with **409** and nothing is overwritten — the gateway will
not silently recycle someone else's saved position. (`max_presets` exists in
`devices.yaml` but the gateway does not enforce it; capacity is the camera's
own limit, surfaced as that 409.)

## Credentials — why `ready` does not mean privacy works

Each device reads its credentials from the process environment, keyed by the
uppercased device id, and a Tapo camera needs up to three different ones:

| variable | used for |
|---|---|
| `<ID>_USER` / `<ID>_PASS` | Tapo Camera Account: RTSP (snapshots, recordings, go2rtc) and pytapo control on older firmware |
| `<ID>_ONVIF_USER` / `<ID>_ONVIF_PASS` | the separate ONVIF account for PTZ and presets; falls back to the Camera Account pair when unset |
| `<ID>_CLOUD_PASS` | the TP-Link **cloud account** password. Newer Tapo firmware rejects the Camera Account on the control API, so when this is set the gateway logs into the control API as local user `admin` with it. RTSP and ONVIF keep using the account pairs above. |

The consequence worth internalising: **a camera can be perfectly healthy for
ONVIF and RTSP while privacy control is dead.** If the control API rejects the
credentials it has, `details.tapo_reachable` goes `false`, `privacy` drops out
of `allowed_actions`, `POST /control/privacy` returns **502** — and
`equipment_status` stays `ready`, because the health roll-up does not weigh the
Tapo plane. Check `details.tapo_reachable`, not `equipment_status`, before
trusting privacy control.

**502 means configured but the call failed; 503 means never configured.** If a
camera had no ONVIF credentials at boot the client was never built and PTZ
answers 503 (`ONVIF not configured for this camera`); with no Tapo credentials
at all, privacy answers 503 (`pytapo not configured for this camera`); with no
`_USER`/`_PASS` pair, snapshot and recording answer 503 naming the two
variables to set. The ONVIF, Tapo and Kasa clients are constructed **once at
startup**, so adding a credential to the environment needs a service restart.

## Privacy and streaming are not the same switch

**Privacy** is on the camera. `POST /control/privacy {enabled}` calls the Tapo
control API's privacy (lens-cover) mode and `details.privacy_mode` reports what
the camera says it is. The gateway does not cross-check it anywhere: it will
happily start a snapshot or a recording on a camera in privacy mode and hand
you whatever the RTSP feed then contains.

**Streaming** is in this gateway — nothing else. `POST /control/streaming
{enabled}` flips an in-memory per-camera flag and touches neither the camera
nor go2rtc. When it is `false` each lens's `mse_url` is omitted from `/status`,
the dashboard player renders "Streaming disabled" instead of opening a
WebSocket, and go2rtc falls back to its idle no-consumer state (it only pulls
RTSP while something is watching, so leaving the stream configured costs
nothing). Two things follow: the flag is **not persisted**, so a gateway
restart puts every camera back to streaming enabled, and it does **not** stop
snapshots or recordings, which pull RTSP directly and never go through go2rtc.

`mse_url` is `/streams/api/ws?src=<camera_id>_<lens_id>` — a path on the
*dashboard's* reverse proxy (Caddy → go2rtc), not a route on this gateway.

## Snapshots, recordings and rolling buffers

Three capture mechanisms, all of them ffmpeg subprocesses pulling the camera's
RTSP feed directly. They bypass go2rtc on purpose: go2rtc's frame endpoint only
works once a consumer is already attached (racy for a one-shot still), and
routing recordings through it would let a go2rtc restart silently drop them.

| | snapshot | recording | rolling |
|---|---|---|---|
| produces | one JPEG | one MP4 you start and stop | continuous MP4 segments, oldest pruned |
| directory | `<root>/snapshots/<cam>/<lens>/` | `<root>/recordings/<cam>/<lens>/` | `<root>/rolling/<cam>/<lens>/` |
| listed by `GET /media` | yes | yes | **no** |
| downloadable over HTTP | yes | yes | **no — on-disk only** |
| audio | n/a | never (video stream copy) | optional `include_audio` (PCMA→AAC) |
| concurrency | any time | one per lens (**409**) | one per lens (**409**) |

`<root>` is `KASA_TAPO_MEDIA_ROOT` (default `~/kasa-tapo-media`), overridable
per camera in `devices.yaml` — snapshots and recordings independently, so
recordings can point at a NAS mount. Filenames are UTC timestamps
(`YYYY-MM-DDTHH-MM-SSZ`, colons swapped for dashes), with the recording id
appended for recordings, so sorting by name sorts by time. `SnapshotResponse`
carries `width`/`height` fields but the gateway never fills them in; they are
always `null`.

A recording writes to `<name>.mp4.partial` and is renamed to `.mp4` on stop.
`finalized: false` in the stop response means that rename did not produce a
`.mp4` — usually because ffmpeg had to be SIGKILLed after the 10 s stop
timeout, leaving a file with no moov atom, which `ffmpeg -i broken.mp4 -c copy
fixed.mp4` normally repairs. `max_duration_s` (default 3600, max 86400) is a
server-side watchdog so a forgotten recording cannot fill the disk. Omit
`recording_id` to address the camera's only running recording: none → **409**,
more than one → **400**, unknown id → **404**.

Rolling recorders hold `max_segments × segment_duration_s` of video per lens —
the defaults (96 × 1800 s) keep 48 hours. Pruning runs only after a segment
finalises, so the in-flight segment is always on top of that budget. A
transient RTSP failure makes the loop wait 15 s and retry rather than give up.
`rolling/stop` takes its lens as a **query parameter**, not a body, and with no
lens it stops every rolling recorder on that camera.

Rolling and a manual recording can run on the same lens simultaneously —
separate directories, separate bookkeeping. That is two concurrent RTSP pulls
off one camera; the cameras tolerate it, but it is not free.

On gateway shutdown, rolling recorders and in-flight recordings are stopped and
finalised. **Nothing resumes on restart** — neither recordings nor rolling
recorders, and the streaming flag returns to enabled.

## Plugs: this switches mains power

`POST /plugs/{id}/control/{on,off,toggle}` with an optional
`{"outlet": <int>}`. The outlet index is zero-based; **omit it to act on the
whole device**, which on a six-outlet HS300 means all six at once. An index out
of range, or any index on a device with no addressable outlets, is **400**.

Both strips carry live instruments — a balance, motors, a shaker. The outlet
labels in `components.outlet_<i>.message` (and `details.outlets[].label`) are
the only mapping from index to instrument, and they come from `devices.yaml`.
There is no claim, no interlock and no confirmation step: an `off` is immediate
and unconditional, and whatever is on that outlet loses power mid-operation.
Read the labels out of `/status` and be certain of the index before switching.

Per-outlet telemetry is published as metrics (`power_outlet_<i>` in W,
`current_outlet_<i>` in A, `energy_kwh_today_outlet_<i>` in kWh) plus `rssi` in
dBm for the device; `details.outlets[]` adds `voltage_v` and
`energy_kwh_total`. A missing metric means the device did not report it — never
read it as zero. A single-outlet plug has one `plug` component instead and no
per-outlet metrics.

`toggle` returns the new state in `ControlAck.state.is_on`; `on` and `off`
return the state they requested, not a readback. Re-poll `/status` for truth.

## Refusal codes

| code | meaning |
|---|---|
| **200 + `ok: false`** | the action ran and the hardware declined — today only "PTZ limit reached" |
| **400** | unknown lens id, outlet out of range, ambiguous recording (more than one running), unknown media kind, path traversal, `.partial` file requested |
| **404** | unknown camera/plug id, unknown `recording_id`, media file not on disk |
| **409** | conflicts with the device's fixed configuration or current state: zoom on a camera with no zoom axis, camera preset storage full, lens already recording, no active recording, rolling recorder already running, no active rolling recorder |
| **422** | request validation — malformed device id, out-of-range field (note the PTZ union caveat above) |
| **502** | the backend call failed: ONVIF, the Tapo control API, python-kasa, or ffmpeg |
| **503** | that subsystem was never configured for this device (no ONVIF credentials, no Tapo credentials, no RTSP credentials), or the gateway has not finished initialising |

There is no **423** and no **412**: no claims, and no precondition gates —
`allowed_actions` is advisory, and the refusal surfaces at the backend.

## Discovery

`/llms.txt` (index), `/agent-docs` (this guide), `/agent-docs/api-reference`,
`/openapi.json`, `/docs` (Swagger UI).
