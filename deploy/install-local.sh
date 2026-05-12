#!/usr/bin/env bash
# Install the kasa-tapo-services and ac-go2rtc systemd units for the local
# (sdl2-user, /home-based) layout. Run once as yourself - it will sudo
# only for the steps that need root.
set -euo pipefail

REPO=/home/sdl2/caoyang/kasa_tapo_services
DEPLOY=$REPO/deploy

echo "==> Creating /etc/kasa-tapo-services/ and installing .env"
sudo install -d -o root -g sdl2 -m 0750 /etc/kasa-tapo-services
sudo install -m 0640 -o root -g sdl2 "$REPO/.env" /etc/kasa-tapo-services/.env

echo "==> Creating go2rtc config dir"
mkdir -p /home/sdl2/.config/go2rtc

echo "==> Creating media root for snapshots / recordings"
sudo install -d -o sdl2 -g sdl2 -m 0755 /var/lib/kasa-tapo-media

echo "==> Installing systemd units"
sudo cp "$DEPLOY/kasa-tapo-services.local.service" /etc/systemd/system/kasa-tapo-services.service
sudo cp "$DEPLOY/ac-go2rtc.local.service"          /etc/systemd/system/ac-go2rtc.service
sudo systemctl daemon-reload

echo "==> Enabling and starting services"
sudo systemctl enable --now kasa-tapo-services.service ac-go2rtc.service

echo ""
echo "Done. Check status with:"
echo "  systemctl status kasa-tapo-services.service ac-go2rtc.service"
echo "Tail logs with:"
echo "  journalctl -u kasa-tapo-services.service -f"
echo "  journalctl -u ac-go2rtc.service -f"
