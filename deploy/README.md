# Deploying `kasa-tapo-services`

This folder contains the systemd units and a Caddy snippet for running the gateway alongside the AC Organic Lab dashboard on a Tailscale-attached Linux server.

There are **two deployment layouts** depending on whether you use a dedicated
service account (`ac-lab`) or run everything under your own user account.
Use the **local layout** for a single-developer machine; use the **production
layout** for a shared or hardened server.

---

## Local layout (single-user machine, e.g. `sdl2`)

Use this when the repo lives under `/home/<you>` and you haven't created a
dedicated `ac-lab` system account. The `.local.service` unit files in this
folder are pre-configured for this layout.

```
/home/sdl2/caoyang/kasa_tapo_services/   # git checkout (WorkingDirectory)
  .venv/                                 # uv-managed venv
  devices.yaml                           # gateway device registry
/etc/kasa-tapo-services/.env             # credentials (root:sdl2 0640)
/home/sdl2/.config/go2rtc/go2rtc.yaml   # rendered by ExecStartPre
/home/sdl2/.local/bin/go2rtc            # static binary
/usr/bin/ffmpeg                          # snapshot + recording subprocess
/var/lib/kasa-tapo-media/               # snapshots + recordings (default)
  snapshots/<camera_id>/<lens_id>/*.jpg
  recordings/<camera_id>/<lens_id>/*.mp4
```

### Why the camera service died on SSH disconnect

When go2rtc or uvicorn is started directly in an SSH shell they are children
of that shell's process group. Closing the SSH connection sends SIGHUP to the
shell, which propagates to every process it spawned — they all die. Running
them as systemd units moves ownership to PID 1, so they survive completely
independently of any login session.

### One-time install (local layout)

A convenience script handles all of the steps below:

```bash
bash /home/sdl2/caoyang/kasa_tapo_services/deploy/install-local.sh
```

Or manually:

```bash
# 1. Credentials — copy repo .env to a root-owned, user-readable location.
sudo install -d -o root -g sdl2 -m 0750 /etc/kasa-tapo-services
sudo install -m 0640 -o root -g sdl2 \
    /home/sdl2/caoyang/kasa_tapo_services/.env \
    /etc/kasa-tapo-services/.env

# 2. go2rtc config dir (written by ExecStartPre at each start).
mkdir -p /home/sdl2/.config/go2rtc

# 3. Media root for snapshots + recordings.
sudo install -d -o sdl2 -g sdl2 -m 0755 /var/lib/kasa-tapo-media

# 4. Install systemd units.
sudo cp deploy/kasa-tapo-services.local.service /etc/systemd/system/kasa-tapo-services.service
sudo cp deploy/ac-go2rtc.local.service          /etc/systemd/system/ac-go2rtc.service
sudo systemctl daemon-reload

# 5. Enable at boot and start now.
sudo systemctl enable --now kasa-tapo-services.service ac-go2rtc.service
```

After the install the services start automatically at every boot — no SSH
session required.

### Updating credentials (local layout)

Edit `/etc/kasa-tapo-services/.env` directly with sudo, then restart:

```bash
sudoedit /etc/kasa-tapo-services/.env
sudo systemctl restart kasa-tapo-services.service ac-go2rtc.service
```

---

## Production layout (dedicated `ac-lab` service account)

Use this on a shared or hardened server. The plain `.service` files (without
`.local`) in this folder are for this layout.

```
/opt/kasa-tapo-services/                  # checkout of this repo
  .venv/                                  # uv-managed venv
  devices.yaml                            # gateway-local registry
/etc/kasa-tapo-services/.env              # credentials (root:ac-lab 0640)
/etc/go2rtc/go2rtc.yaml                   # rendered by ExecStartPre
/usr/local/bin/go2rtc                     # static binary (downloaded)
/usr/bin/ffmpeg                           # snapshot + recording subprocess
/var/lib/kasa-tapo-media/                 # snapshots + recordings (default)
  snapshots/<camera_id>/<lens_id>/*.jpg
  recordings/<camera_id>/<lens_id>/*.mp4
```

### One-time install (production layout)

```bash
sudo useradd --system --user-group --home-dir /opt/kasa-tapo-services --shell /usr/sbin/nologin ac-lab
sudo install -d -o ac-lab -g ac-lab /opt/kasa-tapo-services /etc/kasa-tapo-services /etc/go2rtc
sudo install -d -o ac-lab -g ac-lab -m 0755 /var/lib/kasa-tapo-media

# System dependencies. ffmpeg is required for the /control/snapshot and
# /control/recording/* routes; without it those routes return 503.
sudo apt-get update && sudo apt-get install -y ffmpeg

# Source clone (or use `git pull` to update later).
sudo -u ac-lab git clone https://github.com/cyrilcaoyang/kasa_tapo_services.git /opt/kasa-tapo-services

# Python deps via uv.
sudo -u ac-lab bash -lc 'cd /opt/kasa-tapo-services && uv venv && uv pip install -e .'

# Credentials (see ../.env.example for the variable conventions).
sudo install -m 0640 -o root -g ac-lab /opt/kasa-tapo-services/.env.example /etc/kasa-tapo-services/.env
sudoedit /etc/kasa-tapo-services/.env

# go2rtc binary (pin to a known release; pick linux_amd64 or linux_arm64).
sudo curl -fsSL -o /usr/local/bin/go2rtc \
    https://github.com/AlexxIT/go2rtc/releases/download/v1.9.14/go2rtc_linux_amd64
sudo chmod 0755 /usr/local/bin/go2rtc

# Install systemd units.
sudo cp /opt/kasa-tapo-services/deploy/kasa-tapo-services.service /etc/systemd/system/
sudo cp /opt/kasa-tapo-services/deploy/ac-go2rtc.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kasa-tapo-services.service ac-go2rtc.service
```

## Media storage (snapshots + recordings)

The gateway writes JPEG snapshots and MP4 recordings via an `ffmpeg`
subprocess (stream-copy, no re-encode) when the dashboard / API hits
the new `/control/snapshot` and `/control/recording/{start,stop,cancel}`
endpoints. There are three ways to tell it where to put the files, in
priority order:

1. **Per-camera in `devices.yaml`** (preferred for production - keeps
   different cameras' recordings on different volumes if needed):
   ```yaml
   - id: cam_hte_tapo_c245
     # ...
     media:
       snapshots_dir: /var/lib/kasa-tapo-media/snapshots/cam_hte_tapo_c245
       recordings_dir: /mnt/lab-nas/recordings/cam_hte_tapo_c245
   ```
2. **`KASA_TAPO_MEDIA_ROOT` env var** in `kasa-tapo-services.service`
   (default `/var/lib/kasa-tapo-media`). Cameras without an explicit
   `media:` block fall back to
   `${KASA_TAPO_MEDIA_ROOT}/{snapshots,recordings}/<camera_id>/`.
3. **Process HOME fallback** when neither is set:
   `~/kasa-tapo-media/{snapshots,recordings}/<camera_id>/`.

`ProtectSystem=strict` in the unit file makes the OS filesystem read-only
outside of `ReadWritePaths=`. Any write target not listed there will get
`EROFS` at runtime. If you set a per-camera `media:` path that is **not**
under `/var/lib/kasa-tapo-media`, append it to `ReadWritePaths=` in the
unit and reload (`sudo systemctl daemon-reload`).

The production unit also sets `ProtectHome=true` (the `ac-lab` home is under
`/opt`). The local unit omits `ProtectHome` because the venv and config live
under `/home/sdl2`; if you add a per-camera path under `/home` on the
production layout you must also drop `ProtectHome=true` from that unit.

Filenames are ISO 8601 UTC timestamps:

```
/var/lib/kasa-tapo-media/snapshots/cam_hte_tapo_c245/wide/2026-05-09T02-24-56Z.jpg
/var/lib/kasa-tapo-media/recordings/cam_hte_tapo_c245/wide/2026-05-09T02-25-21Z_d554a2c38e3f.mp4
```

The dashboard's `/api/equipment/{id}/media` route proxies the gateway's
`/cameras/{id}/media` listing so the browser can browse and download
captures without the user ever needing shell access to the server.

## Caddy

Append the contents of `Caddyfile.snippet` to the existing dashboard Caddy block, then `sudo systemctl reload caddy`.

## Day-to-day operations

* Tail logs:
  ```bash
  journalctl -u kasa-tapo-services.service -f
  journalctl -u ac-go2rtc.service -f
  ```
* Check service status:
  ```bash
  systemctl status kasa-tapo-services.service ac-go2rtc.service
  ```
* Restart after editing `devices.yaml`:
  ```bash
  sudo systemctl restart kasa-tapo-services.service ac-go2rtc.service
  ```
* Re-render go2rtc config without bouncing the gateway (local layout):
  ```bash
  /home/sdl2/caoyang/kasa_tapo_services/.venv/bin/kasa-tapo-bootstrap-go2rtc \
      --output /home/sdl2/.config/go2rtc/go2rtc.yaml
  sudo systemctl restart ac-go2rtc.service
  ```
* Re-render go2rtc config without bouncing the gateway (production layout):
  ```bash
  sudo -u ac-lab bash -lc 'cd /opt/kasa-tapo-services && uv run kasa-tapo-bootstrap-go2rtc --output /etc/go2rtc/go2rtc.yaml'
  sudo systemctl restart ac-go2rtc.service
  ```

## Adding a Tapo camera

1. Open the Tapo phone app, select the camera, then **Settings → Advanced Settings**.
2. Under **Camera Account**, create or note down a username + password (this is what go2rtc uses for RTSP and what pytapo uses for privacy/day-night).
3. Under **ONVIF**, enable ONVIF and create an ONVIF-only username + password (or reuse the Camera Account one).
4. Find the camera's lab-LAN IP from the Tapo app or from the lab DHCP server.
5. Verify the RTSP paths from the dashboard host:
   ```bash
   ffprobe -v error rtsp://USER:PASS@IP:554/stream1
   ffprobe -v error rtsp://USER:PASS@IP:554/stream2   # or /stream3 on some dual-lens models
   ```
6. Add an entry to `devices.yaml` in the repo checkout (see `../devices.yaml.example` for the shape).
7. Add `<DEVICE_ID_UPPERCASE>_USER`, `<DEVICE_ID_UPPERCASE>_PASS`, and (if used) the `_ONVIF_*` pair to `/etc/kasa-tapo-services/.env`.
8. `sudo systemctl restart kasa-tapo-services.service ac-go2rtc.service`.
9. In the dashboard repo, add a matching `equipment.yaml` entry pointing `base_url: http://127.0.0.1:8002` and `status_path: /cameras/<id>/status`, then restart `ac-dashboard-api.service`.

## Adding a Kasa plug

HS103 (single plug) and HS300 (6-outlet strip) ship in legacy Kasa protocol mode by default - no credentials are needed. If you've migrated the plug to the TP-Link cloud (KLAP protocol), add the cloud account email/password as `<DEVICE_ID_UPPERCASE>_USER` / `_PASS` in `.env`.

1. Discover the plug's IP (`kasa discover` from the gateway host, or check the Kasa app).
2. Add an entry to `devices.yaml` with `kind: smart_plug` (HS103) or `kind: power_strip` + `outlets:` (HS300).
3. Restart the gateway.
4. Add the matching dashboard `equipment.yaml` entry with `status_path: /plugs/<id>/status`.
