#!/usr/bin/env bash
# One-shot: pull /etc/esg-collector.env from gs://esg-scan-data/_setup/env.txt
# and restart workers. Triggered by setting this as the VM's startup-script,
# then resetting. Removes the bucket object after success.
set -e
exec > >(tee -a /var/log/esg-collector-applyenv.log) 2>&1
echo "=== _apply_env.sh $(date -Iseconds) ==="

if [[ -f /opt/esg-collector/.env-applied ]]; then
  echo "already applied — skipping"
  exit 0
fi

gsutil cp gs://esg-scan-data/_setup/env.txt /etc/esg-collector.env
chmod 600 /etc/esg-collector.env
chown root:root /etc/esg-collector.env

for svc in esg-collector-google esg-collector-baomoi esg-collector-brave esg-collector-body; do
  systemctl restart "$svc.service"
done

# clean up the bucket object so the secret doesn't linger
gsutil rm gs://esg-scan-data/_setup/env.txt || true
touch /opt/esg-collector/.env-applied

echo "=== _apply_env.sh done ==="
systemctl --no-pager status \
  esg-collector-google esg-collector-baomoi esg-collector-brave esg-collector-body || true
