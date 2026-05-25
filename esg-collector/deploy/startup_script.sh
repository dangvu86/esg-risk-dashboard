#!/usr/bin/env bash
# Startup script for esg-collector VM (registered via metadata startup-script-url
# or startup-script). Idempotent. Logs to /var/log/esg-collector-startup.log.

set -e
exec > >(tee -a /var/log/esg-collector-startup.log) 2>&1
echo "=== startup_script.sh $(date -Iseconds) ==="

# Marker so we don't rerun the full install on every reboot
if [[ -f /opt/esg-collector/.installed && "${FORCE_REINSTALL:-0}" != "1" ]]; then
  echo "Already installed — skipping. Touch /opt/esg-collector/.installed off to force."
  exit 0
fi

curl -fsSL https://raw.githubusercontent.com/dangvu86/esg-risk-dashboard/main/esg-collector/deploy/install.sh -o /tmp/install.sh
bash /tmp/install.sh

touch /opt/esg-collector/.installed
echo "=== startup_script.sh done $(date -Iseconds) ==="
