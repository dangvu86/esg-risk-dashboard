#!/usr/bin/env bash
# One-shot: fetch Vietstock profiles for all tickers in companies.csv
# (skips ones that already have alias JSON). Then trigger pipeline.match
# immediately so we don't wait for the 6h timer.
set -e
exec > >(tee -a /var/log/esg-collector-aliases.log) 2>&1
echo "=== _build_aliases.sh $(date -Iseconds) ==="

cd /opt/esg-collector
sudo -u esg git pull --ff-only

cd /opt/esg-collector/esg-collector

echo "--- alias count BEFORE ---"
ls config/aliases/*.json 2>/dev/null | wc -l

sudo -u esg /opt/esg-collector/.venv/bin/python -m alias_builder.fetch_vietstock --all

echo "--- alias count AFTER ---"
ls config/aliases/*.json 2>/dev/null | wc -l

echo "--- triggering pipeline.match ---"
# Reset matched articles to pending so the new aliases get a chance
sudo -u esg /opt/esg-collector/.venv/bin/python -c "
from core import storage
conn = storage.connect()
n = conn.execute(\"UPDATE articles SET match_status='pending', matched_at=NULL WHERE match_status IN ('matched','unmatched')\").rowcount
print(f'reset {n} articles to pending')
"

sudo -u esg /opt/esg-collector/.venv/bin/python -m pipeline.match

echo "--- per-ticker JSON count ---"
ls data/per_ticker/*.json 2>/dev/null | wc -l

echo "--- upload per_ticker to GCS ---"
gsutil -m cp data/per_ticker/*.json gs://esg-scan-data/per_ticker/ 2>&1 | tail -5

echo "=== _build_aliases.sh done ==="
