#!/usr/bin/env bash
# Fill the 2025-01 → 2026-05 gap that the original backfill window missed
# (settings.BACKFILL_END was hardcoded to 2024-12-31). One-shot enqueue.
set -e
exec > >(tee -a /var/log/esg-collector-gapfill.log) 2>&1
echo "=== _gap_fill.sh $(date -Iseconds) ==="

cd /opt/esg-collector
sudo -u esg git pull --ff-only || true

cd /opt/esg-collector/esg-collector

# Enqueue 2025-01-01 → 2026-05-23 (day before today's daily window)
sudo -u esg /opt/esg-collector/.venv/bin/python -m core.queue_builder \
  --since 2025-01-01 --until 2026-05-23 \
  --backends google_rss baomoi

echo "--- queue stats ---"
sudo -u esg /opt/esg-collector/.venv/bin/python -c \
  "from core import storage; print(storage.queue_stats(storage.connect()))"

echo "=== _gap_fill.sh done ==="
