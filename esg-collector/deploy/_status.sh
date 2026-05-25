#!/usr/bin/env bash
# Dump status to serial console + upload tail logs to GCS for inspection.
set +e
exec > >(tee -a /var/log/esg-collector-status.log) 2>&1
echo "=== _status.sh $(date -Iseconds) ==="

cd /opt/esg-collector/esg-collector

echo "--- queue stats ---"
sudo -u esg /opt/esg-collector/.venv/bin/python -c \
  "from core import storage; print(storage.queue_stats(storage.connect()))"

echo "--- articles count ---"
sudo -u esg /opt/esg-collector/.venv/bin/python -c \
  "import sqlite3; c=sqlite3.connect('/opt/esg-collector/esg-collector/data/articles.db'); print('articles:', c.execute('SELECT COUNT(*) FROM articles').fetchone()[0]); print('matched:', c.execute(\"SELECT COUNT(*) FROM articles WHERE match_status='matched'\").fetchone()[0])"

echo "--- systemctl status (one-liner each) ---"
for s in google baomoi brave body; do
  systemctl is-active "esg-collector-$s.service" | xargs -I{} echo "  $s: {}"
done

echo "--- last 20 lines per worker log ---"
for s in google_rss baomoi brave body; do
  echo "=== $s ==="
  tail -n 20 "/var/log/esg-collector/$s.log" 2>/dev/null || echo "  (no log yet)"
done

echo "--- /etc/esg-collector.env keys present (not values) ---"
grep -oE "^(BRAVE_API_KEY|JINA_API_KEY)=" /etc/esg-collector.env | sort -u

# Bundle logs to GCS for full inspection
tar -czf /tmp/esg-logs.tar.gz -C /var/log esg-collector/ esg-collector-startup.log esg-collector-applyenv.log esg-collector-queue.log esg-collector-status.log 2>/dev/null
gsutil cp /tmp/esg-logs.tar.gz gs://esg-scan-data/_setup/logs.tar.gz

echo "=== _status.sh done ==="
