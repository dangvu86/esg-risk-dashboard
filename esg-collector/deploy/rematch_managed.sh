#!/usr/bin/env bash
# Managed rematch — runs detached via systemd-run (as root). Owns the whole
# lifecycle so the CI deploy can fire-and-return. Writes a status file to GCS so
# progress is visible without SSH.
set +e

INSTALL_DIR=/opt/esg-collector
APP_DIR=$INSTALL_DIR/esg-collector
VENV=$INSTALL_DIR/.venv/bin/python
SVC_USER=esg
STATUS_LOCAL=/tmp/rematch_status.json
STATUS_GCS=gs://esg-scan-data/_setup/rematch_status.json
WORKERS="esg-collector-google.service esg-collector-baomoi.service esg-collector-brave.service esg-collector-body.service"

write_status() {  # $1=state  $2=extra-json (optional)
  ts=$(date -Iseconds)
  echo "{\"state\":\"$1\",\"at\":\"$ts\"${2:+,$2}}" > "$STATUS_LOCAL"
  sudo -u "$SVC_USER" gsutil cp "$STATUS_LOCAL" "$STATUS_GCS" 2>/dev/null
}

restart_workers() { systemctl start $WORKERS; }
trap 'restart_workers' EXIT  # whatever happens, workers come back

write_status running
systemctl stop $WORKERS

cd "$APP_DIR" || { write_status failed '"error":"cd failed"'; exit 1; }

sudo -u "$SVC_USER" "$VENV" -m pipeline.match --rematch-all --status-json "$STATUS_LOCAL.counts"
rc=$?
if [ $rc -ne 0 ]; then
  write_status failed "\"error\":\"match rc=$rc\""
  exit 1   # trap restarts workers
fi

sudo -u "$SVC_USER" "$VENV" -m pipeline.export --ndjson --upload

counts=$(cat "$STATUS_LOCAL.counts" 2>/dev/null || echo '{}')
restart_workers
trap - EXIT
write_status done "\"counts\":$counts"
