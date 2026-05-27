#!/usr/bin/env bash
# One-shot: deploy the alias + Google-URL-decode + status-timer fix.
# Triggered by setting this as the VM's startup-script (via metadata) then
# resetting the VM. Self-clears the startup-script on success so it does not
# re-run on the next reboot.
#
# Logs everything to /var/log/esg-collector-deploy.log AND uploads a tarball
# to gs://esg-scan-data/_setup/deploy_log.tar.gz at the end (success or fail)
# so we can inspect remotely without SSH.

set +e
exec > >(tee -a /var/log/esg-collector-deploy.log) 2>&1
echo "=== _deploy_fix.sh $(date -Iseconds) ==="

INSTALL_DIR=/opt/esg-collector
APP_DIR=$INSTALL_DIR/esg-collector
VENV=$INSTALL_DIR/.venv/bin/python
SVC_USER=esg

fail() {
  echo "FAILED at step: $1"
  echo "Last error: $2"
  upload_log
  exit 1
}

upload_log() {
  echo "--- uploading deploy log ---"
  tar -czf /tmp/deploy_log.tar.gz \
    /var/log/esg-collector-deploy.log \
    /var/log/esg-collector/*.log 2>/dev/null
  gsutil cp /tmp/deploy_log.tar.gz gs://esg-scan-data/_setup/deploy_log.tar.gz
}

echo
echo "=== 1. Stop workers ==="
systemctl stop esg-collector-google.service \
               esg-collector-baomoi.service \
               esg-collector-brave.service \
               esg-collector-body.service
for s in google baomoi brave body; do
  echo "  $s: $(systemctl is-active esg-collector-$s.service)"
done

echo
echo "=== 2. Pull code ==="
cd $INSTALL_DIR || fail "cd" "no $INSTALL_DIR"
sudo -u $SVC_USER git pull --ff-only || fail "git pull" "$?"
echo "HEAD: $(sudo -u $SVC_USER git log -1 --oneline)"

echo
echo "=== 3. Install + enable status timer ==="
install -m 644 $APP_DIR/deploy/esg-collector-status.service /etc/systemd/system/
install -m 644 $APP_DIR/deploy/esg-collector-status.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now esg-collector-status.timer || fail "status.timer" "$?"
echo "  status.timer: $(systemctl is-active esg-collector-status.timer)"

echo
echo "=== 4. Rebuild aliases (Vietstock fetch, ~5 phút) ==="
cd $APP_DIR
sudo -u $SVC_USER $VENV -m alias_builder.fetch_vietstock --all --force \
  || fail "fetch_vietstock" "$?"

echo
echo "=== 5. Title-hash backfill + cross-backend dedup (~30 giây) ==="
sudo -u $SVC_USER $VENV -m pipeline.dedup_titles \
  || fail "dedup_titles" "$?"

echo
echo "=== 6. Re-match toàn bộ với alias mới ==="
sudo -u $SVC_USER $VENV -m pipeline.match --rematch-all \
  || fail "rematch" "$?"

echo
echo "=== 7. Export + upload GCS ==="
sudo -u $SVC_USER $VENV -m pipeline.export --ndjson --upload \
  || fail "export" "$?"

echo
echo "=== 8. Restart workers ==="
systemctl start esg-collector-google.service \
                esg-collector-baomoi.service \
                esg-collector-brave.service \
                esg-collector-body.service
for s in google baomoi brave body; do
  echo "  $s: $(systemctl is-active esg-collector-$s.service)"
done

echo
echo "=== 9. Clear startup-script so this doesn't re-run on next boot ==="
# Use metadata-from-file with empty value to remove key
gcloud compute instances remove-metadata esg-collector \
  --zone=us-central1-a \
  --keys=startup-script 2>&1 | tail -3 || echo "warn: could not clear startup-script"

echo
echo "=== DONE $(date -Iseconds) ==="
upload_log
