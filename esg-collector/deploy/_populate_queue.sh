#!/usr/bin/env bash
set -e
exec > >(tee -a /var/log/esg-collector-queue.log) 2>&1
echo "=== _populate_queue.sh $(date -Iseconds) ==="

cd /opt/esg-collector/esg-collector
sudo -u esg /opt/esg-collector/.venv/bin/python -m core.queue_builder \
  --backends google_rss baomoi brave

echo "Queue stats:"
sudo -u esg /opt/esg-collector/.venv/bin/python -c \
  "from core import storage; storage.init_db(); print(storage.queue_stats(storage.connect()))"

echo "=== _populate_queue.sh done ==="
