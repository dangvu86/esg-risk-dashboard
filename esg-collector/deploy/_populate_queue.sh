#!/usr/bin/env bash
set -e
exec > >(tee -a /var/log/esg-collector-queue.log) 2>&1
echo "=== _populate_queue.sh $(date -Iseconds) ==="

cd /opt/esg-collector/esg-collector
# Backfill = whole flow (alias + keyword) across all backends; no --backends subset.
sudo -u esg /opt/esg-collector/.venv/bin/python -m core.queue_builder \
  --mode backfill

echo "Queue stats:"
sudo -u esg /opt/esg-collector/.venv/bin/python -c \
  "from core import storage; storage.init_db(); print(storage.queue_stats(storage.connect()))"

echo "=== _populate_queue.sh done ==="
