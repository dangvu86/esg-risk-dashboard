"""Export articles + per-ticker files to NDJSON and upload to GCS.

Local:
    python -m pipeline.export --ndjson
Upload (requires gsutil + auth on the running host):
    python -m pipeline.export --ndjson --upload
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
from datetime import datetime
from pathlib import Path

from config import settings
from core import storage


log = logging.getLogger("export")

GCS_BUCKET = "gs://esg-scan-data"


def _export_ndjson(out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%d")
    path = out_dir / f"articles_{stamp}.ndjson"
    conn = storage.connect()
    n = 0
    with path.open("w", encoding="utf-8") as f:
        for row in conn.execute("SELECT * FROM articles"):
            rec = {k: row[k] for k in row.keys()}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
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


def run(do_ndjson: bool, do_upload: bool) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s/%(levelname)s] %(message)s",
    )
    out_dir = settings.DATA_DIR / "exports"
    ndjson_path: Path | None = None
    if do_ndjson:
        ndjson_path = _export_ndjson(out_dir)
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
    ap.add_argument("--ndjson", action="store_true", help="write data/exports/articles_<date>.ndjson")
    ap.add_argument("--upload", action="store_true", help="gsutil cp to gs://esg-scan-data/")
    args = ap.parse_args()
    if not (args.ndjson or args.upload):
        ap.error("pass --ndjson and/or --upload")
    run(do_ndjson=args.ndjson, do_upload=args.upload)


if __name__ == "__main__":
    main()
