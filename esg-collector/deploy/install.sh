#!/usr/bin/env bash
# Install esg-collector on a fresh Debian 12 GCE VM.
# Run as root (or via sudo). Idempotent — safe to re-run after pulling new code.
#
# Before running, scp deploy/install.sh + git clone happens here.
# Requires /etc/esg-collector.env to exist with BRAVE_API_KEY + JINA_API_KEY
# (use deploy/esg-collector.env.example as template).

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/dangvu86/esg-risk-dashboard.git}"
INSTALL_DIR="/opt/esg-collector"
APP_DIR="$INSTALL_DIR/esg-collector"
SERVICE_USER="esg"
LOG_DIR="/var/log/esg-collector"

echo "==> Install dir: $INSTALL_DIR"

# 1. System packages
apt-get update
apt-get install -y --no-install-recommends \
  git python3 python3-venv python3-pip python3-lxml ca-certificates curl

# 2. Service user
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

# 3. Code
if [[ ! -d "$INSTALL_DIR/.git" ]]; then
  echo "Cloning $REPO_URL …"
  git clone "$REPO_URL" "$INSTALL_DIR"
else
  echo "Pulling latest…"
  git -C "$INSTALL_DIR" pull --ff-only
fi

# 4. Virtualenv
if [[ ! -d "$INSTALL_DIR/.venv" ]]; then
  python3 -m venv "$INSTALL_DIR/.venv"
fi
"$INSTALL_DIR/.venv/bin/pip" install --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

# 5. Logs + data dirs
mkdir -p "$LOG_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR" "$LOG_DIR"

# 6. Env file (must exist before services start)
if [[ ! -f /etc/esg-collector.env ]]; then
  echo "WARNING: /etc/esg-collector.env missing — copy from $APP_DIR/deploy/esg-collector.env.example"
  echo "Services will fail to start until BRAVE_API_KEY + JINA_API_KEY are set."
  cp "$APP_DIR/deploy/esg-collector.env.example" /etc/esg-collector.env
  chmod 600 /etc/esg-collector.env
fi

# 7. systemd units
install -m 644 "$APP_DIR/deploy/esg-collector-google.service" /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/esg-collector-baomoi.service" /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/esg-collector-brave.service"  /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/esg-collector-body.service"   /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/esg-collector-match.service"  /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/esg-collector-match.timer"    /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/esg-collector-enrich.service" /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/esg-collector-enrich.timer"   /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/esg-collector-daily.service"  /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/esg-collector-daily.timer"    /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/esg-collector-status.service" /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/esg-collector-status.timer"   /etc/systemd/system/
systemctl daemon-reload

# 8. Initial DB + queue (idempotent). Backfill is the whole flow (per-company
#    alias + single-term keyword) across all backends — there is no per-backend
#    subset to pass. Build alias files first (fetch_vietstock --all) for full
#    coverage; missing-alias tickers are skipped with a warning, not an error.
sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/python" -m core.queue_builder \
  --mode backfill \
  || echo "queue_builder failed — re-run manually after fixing config"

# 9. Enable + start
for svc in esg-collector-google esg-collector-baomoi esg-collector-brave esg-collector-body; do
  systemctl enable --now "$svc.service"
done
systemctl enable --now esg-collector-match.timer
systemctl enable --now esg-collector-enrich.timer
systemctl enable --now esg-collector-daily.timer
systemctl enable --now esg-collector-status.timer

echo
echo "Status:"
systemctl --no-pager status \
  esg-collector-google esg-collector-baomoi esg-collector-brave \
  esg-collector-body esg-collector-match.timer || true

echo
echo "Tail logs:  journalctl -u esg-collector-google -f"
echo "Queue:      sudo -u $SERVICE_USER $INSTALL_DIR/.venv/bin/python -c 'from core import storage; print(storage.queue_stats(storage.connect()))'"
