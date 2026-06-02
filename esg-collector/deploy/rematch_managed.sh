#!/usr/bin/env bash
# Managed rematch — runs detached via systemd-run (as root). Owns the whole
# lifecycle so the CI deploy can fire-and-return. Writes a status file to GCS so
# progress is visible without SSH.
set +e

# systemd-run gives this unit a minimal environment, so set an explicit PATH
# that includes the Cloud SDK so gsutil/systemctl resolve. NOTE: the first live
# run must confirm `gsutil` resolves — this path may need adjusting to wherever
# the Cloud SDK is actually installed on the VM.
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin:/opt/google-cloud-sdk/bin"

INSTALL_DIR=/opt/esg-collector
APP_DIR=$INSTALL_DIR/esg-collector
VENV=$INSTALL_DIR/.venv/bin/python
SVC_USER=esg
STATUS_LOCAL=/tmp/rematch_status.json
STATUS_GCS=gs://esg-scan-data/_setup/rematch_status.json
WORKERS="esg-collector-google.service esg-collector-baomoi.service esg-collector-brave.service esg-collector-body.service"
MATCH_TIMER=esg-collector-match.timer

write_status() {  # $1=state  $2=extra-json (optional)
  ts=$(date -Iseconds)
  echo "{\"state\":\"$1\",\"at\":\"$ts\"${2:+,$2}}" > "$STATUS_LOCAL"
  sudo -u "$SVC_USER" gsutil cp "$STATUS_LOCAL" "$STATUS_GCS" 2>/dev/null
}

resume_units() {           # whatever happens, workers + the 6h match timer come back
  systemctl start $MATCH_TIMER || true
  systemctl start $WORKERS || true
}
trap 'resume_units' EXIT

write_status running
# Stop the periodic/boot match too: otherwise match.timer (6h / OnBootSec=10min)
# can fire a SECOND matcher against the same DB mid-rematch — double RAM on a 1GB
# box. We own matching for the duration; resume_units restarts the timer at the end.
systemctl stop $MATCH_TIMER esg-collector-match.service 2>/dev/null || true
systemctl stop $WORKERS

cd "$APP_DIR" || { write_status failed '"error":"cd failed"'; exit 1; }

rm -f "$STATUS_LOCAL.counts"
# nice + idle IO so the matcher yields CPU/disk to sshd/guest-agent — the VM stays
# reachable while this grinds slowly in the background (MemoryMax/CPUQuota are set
# on the esg-rematch unit by the deploy's systemd-run --property flags).
sudo -u "$SVC_USER" nice -n 15 ionice -c3 "$VENV" -m pipeline.match --rematch-all --status-json "$STATUS_LOCAL.counts"
rc=$?
if [ $rc -ne 0 ]; then
  write_status failed "\"error\":\"match rc=$rc\""
  exit 1   # trap restarts workers
fi

sudo -u "$SVC_USER" "$VENV" -m pipeline.export --ndjson --upload
erc=$?
counts=$(cat "$STATUS_LOCAL.counts" 2>/dev/null || echo '{}')
resume_units
trap - EXIT
if [ "$erc" -ne 0 ]; then
  write_status failed "\"error\":\"export rc=$erc\",\"counts\":$counts"
else
  write_status done "\"counts\":$counts"
fi
