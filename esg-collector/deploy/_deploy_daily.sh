#!/usr/bin/env bash
# Update on VM: git pull + install daily timer unit + enable.
# Used as one-shot startup-script.
set -e
exec > >(tee -a /var/log/esg-collector-deploy.log) 2>&1
echo "=== _deploy_daily.sh $(date -Iseconds) ==="

cd /opt/esg-collector
sudo -u esg git pull --ff-only

APP_DIR=/opt/esg-collector/esg-collector
install -m 644 "$APP_DIR/deploy/esg-collector-daily.service" /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/esg-collector-daily.timer"   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now esg-collector-daily.timer

echo "--- timers ---"
systemctl list-timers --no-pager esg-collector-* || true

echo "--- run daily once now to verify ---"
systemctl start esg-collector-daily.service
sleep 5
tail -20 /var/log/esg-collector/daily.log 2>/dev/null || echo "(no daily log yet)"

echo "=== _deploy_daily.sh done ==="
