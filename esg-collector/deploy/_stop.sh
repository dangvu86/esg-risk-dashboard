#!/usr/bin/env bash
# Stop all collector services without wiping data/queue. Used as a startup-script,
# so VM reboot triggers it; safe to re-run.
set +e
exec > >(tee -a /var/log/esg-collector-stop.log) 2>&1
echo "=== _stop.sh $(date -Iseconds) ==="

for svc in esg-collector-google esg-collector-baomoi esg-collector-brave esg-collector-body; do
  systemctl stop "$svc.service"
  systemctl disable "$svc.service"
done
systemctl stop esg-collector-match.timer
systemctl disable esg-collector-match.timer

echo "--- after stop ---"
for s in google baomoi brave body; do
  echo "  esg-collector-$s: $(systemctl is-active "esg-collector-$s.service")"
done

echo "--- queue + articles snapshot ---"
cd /opt/esg-collector/esg-collector
sudo -u esg /opt/esg-collector/.venv/bin/python -c "
from core import storage
import sqlite3
conn = storage.connect()
print('queue:', storage.queue_stats(conn))
c = sqlite3.connect('/opt/esg-collector/esg-collector/data/articles.db')
print('articles total:', c.execute('SELECT COUNT(*) FROM articles').fetchone()[0])
print('by backend:', dict(c.execute('SELECT backend, COUNT(*) FROM articles GROUP BY backend')))
print('body fetched:', c.execute(\"SELECT COUNT(*) FROM articles WHERE body_status='fetched'\").fetchone()[0])
print('matched:', c.execute(\"SELECT COUNT(*) FROM articles WHERE match_status='matched'\").fetchone()[0])
"
echo "=== _stop.sh done ==="
