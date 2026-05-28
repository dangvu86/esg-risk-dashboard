"""Export articles + per-ticker files to NDJSON and upload to GCS.

Default is **incremental**: only articles fetched/modified since the last
export run are written. This keeps the per-cycle file small (a few MB)
rather than re-dumping the entire DB every 6h (would grow into hundreds of
MB after a 5y backfill).

Use --full to force a complete snapshot — useful for periodic backups or
the first run after a schema change.

Local:
    python -m pipeline.export --ndjson
    python -m pipeline.export --ndjson --full      # full snapshot
Upload (requires gsutil + auth on the running host):
    python -m pipeline.export --ndjson --upload
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from config import settings
from core import storage


log = logging.getLogger("export")

GCS_BUCKET = "gs://esg-scan-data"

_LAST_EXPORT_KEY = "last_ndjson_export_at"


def _export_ndjson(out_dir: Path, *, full: bool) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%d_%H%M%S")
    kind = "full" if full else "delta"
    path = out_dir / f"articles_{kind}_{stamp}.ndjson"

    conn = storage.connect()
    since: str | None = None
    if not full:
        since = storage.get_meta(conn, _LAST_EXPORT_KEY)
        if since:
            log.info("incremental export since %s", since)
        else:
            log.info("no prior export recorded — first run writes a full snapshot")
            kind = "full"  # rename below
            path = out_dir / f"articles_full_{stamp}.ndjson"

    n = 0
    if since and not full:
        cursor = conn.execute(
            "SELECT * FROM articles WHERE fetched_at >= ? OR matched_at >= ?",
            (since, since),
        )
    else:
        cursor = conn.execute("SELECT * FROM articles")
    with path.open("w", encoding="utf-8") as f:
        for row in cursor:
            rec = {k: row[k] for k in row.keys()}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1

    # Record the cutoff *after* the read so any concurrent writes during the
    # SELECT are picked up by the next delta (some overlap > missed rows).
    storage.set_meta(conn, _LAST_EXPORT_KEY, now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    conn.close()
    log.info("wrote %d articles → %s", n, path)
    return path


def _gsutil_cp(src: Path, dst: str) -> None:
    cmd = ["gsutil", "cp", str(src), dst]
    log.info("$ %s", " ".join(cmd))
    subprocess.run(cmd, check=True)


def _upload(ndjson: Path) -> None:
    _gsutil_cp(ndjson, f"{GCS_BUCKET}/raw_esg/{ndjson.name}")
    if settings.PER_TICKER_DIR.exists():
        # `gsutil cp -r dir gs://...` keeps the dir name; we want flat per_ticker/
        cmd = [
            "gsutil", "-m", "cp",
            str(settings.PER_TICKER_DIR / "*.json"),
            f"{GCS_BUCKET}/per_ticker/",
        ]
        log.info("$ %s", " ".join(cmd))
        # use shell=True so the glob expands on Windows; on Linux gsutil handles it
        subprocess.run(" ".join(cmd), shell=True, check=True)


def run(do_ndjson: bool, do_upload: bool, *, full: bool = False) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s/%(levelname)s] %(message)s",
    )
    out_dir = settings.DATA_DIR / "exports"
    ndjson_path: Path | None = None
    if do_ndjson:
        ndjson_path = _export_ndjson(out_dir, full=full)
    if do_upload:
        if ndjson_path is None:
            # find latest export
            candidates = sorted(out_dir.glob("articles_*.ndjson"))
            if not candidates:
                raise SystemExit("no NDJSON to upload — pass --ndjson")
            ndjson_path = candidates[-1]
        _upload(ndjson_path)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ndjson", action="store_true",
                    help="write data/exports/articles_{full|delta}_<ts>.ndjson")
    ap.add_argument("--upload", action="store_true", help="gsutil cp to gs://esg-scan-data/")
    ap.add_argument("--full", action="store_true",
                    help="force full snapshot instead of delta-since-last-export")
    args = ap.parse_args()
    if not (args.ndjson or args.upload):
        ap.error("pass --ndjson and/or --upload")
    run(do_ndjson=args.ndjson, do_upload=args.upload, full=args.full)


if __name__ == "__main__":
    main()
