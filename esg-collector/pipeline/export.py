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
Upload (requires google-cloud-storage + ADC on the running host):
    python -m pipeline.export --ndjson --upload
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from config import settings
from core import storage
from runtime import gcs


log = logging.getLogger("export")

GCS_BUCKET_NAME = gcs.GCS_BUCKET_NAME  # "esg-scan-data"

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


def _upload(ndjson: Path, *, bucket=None) -> None:
    bucket = bucket if bucket is not None else gcs.get_bucket()
    gcs.upload_file(bucket, f"raw_esg/{ndjson.name}", ndjson)
    if settings.PER_TICKER_DIR.exists():
        for p in sorted(settings.PER_TICKER_DIR.glob("*.json")):
            gcs.upload_file(bucket, f"per_ticker/{p.name}", p)


WEB_PREFIX = "web"


def _company_names() -> dict[str, str]:
    """ticker -> short company name, from config/companies.csv (Mã CK, Tên Công ty)."""
    out: dict[str, str] = {}
    p = settings.COMPANIES_CSV
    if not p.exists():
        return out
    with open(p, "r", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            t = (row.get("Mã CK") or "").strip()
            if t:
                out[t] = (row.get("Tên Công ty") or "").strip()
    return out


def build_esg_events(db_path=None, per_ticker_dir: Path | None = None,
                     companies: dict | None = None) -> list[dict]:
    """Join per_ticker/*.json with enriched articles columns → web EsgEvent list,
    risk-only, deduped by title_hash (earliest kept), sorted by date desc."""
    per_ticker_dir = per_ticker_dir or settings.PER_TICKER_DIR
    companies = companies if companies is not None else _company_names()
    conn = storage.connect(db_path)
    try:
        # enrich columns keyed by article_id — only fully-enriched rows (bounds memory)
        enr: dict[str, dict] = {}
        for r in conn.execute(
            "SELECT article_id, title_hash, sentiment, summary_en, controversy_level, "
            "controversy_justification, controversy_classified_at, fetched_at "
            "FROM articles WHERE enrich_status='done'"
        ):
            enr[r["article_id"]] = {k: r[k] for k in r.keys()}
    finally:
        conn.close()

    seen: set[tuple[str, str]] = set()          # (ticker, title_hash) already emitted
    events: list[dict] = []
    for pj in sorted(Path(per_ticker_dir).glob("*.json")):
        try:
            doc = json.loads(pj.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        ticker = (doc.get("ticker") or pj.stem).upper()
        # earliest first so the kept representative is the earliest published
        arts = sorted(doc.get("articles") or [],
                      key=lambda a: a.get("published_at") or "")
        for a in arts:
            row = enr.get(a.get("article_id"))
            if not row or row.get("sentiment") != "risk":
                continue            # not enriched, dropped, or pending → skip
            th = row.get("title_hash") or a.get("article_id")
            if not th:
                continue   # no stable identity → cannot dedup safely, skip
            key = (ticker, th)
            if key in seen:
                continue            # same incident already emitted (earliest wins)
            seen.add(key)
            events.append({
                "ticker": ticker,
                "company": companies.get(ticker, ""),
                "type": a.get("type"),
                "date": (a.get("published_at") or "")[:10],
                "summary": a.get("title") or "",
                "summary_en": row.get("summary_en") or "",
                "severity": a.get("severity"),
                "source": a.get("source") or "",
                "url": a.get("url") or "",
                "controversy_level": row.get("controversy_level") or "",
                "controversy_justification": row.get("controversy_justification") or "",
                "controversy_classified_at": row.get("controversy_classified_at") or "",
                "created_at": row.get("fetched_at"),
                # optional passthrough (web may surface later; not displayed now):
                "backend": a.get("backend"),
                "matched_alias": a.get("matched_alias"),
            })
    events.sort(key=lambda e: e["date"], reverse=True)
    return events


def _write_web_files() -> tuple[Path, Path]:
    settings.WEB_DIR.mkdir(parents=True, exist_ok=True)
    companies = _company_names()
    events = build_esg_events(companies=companies)
    ev_path = settings.WEB_DIR / "esg_events.json"
    ev_path.write_text(json.dumps(events, ensure_ascii=False), encoding="utf-8")
    top = [{"ticker": t, "company": c} for t, c in sorted(companies.items())]
    top_path = settings.WEB_DIR / "top100.json"
    top_path.write_text(json.dumps(top, ensure_ascii=False), encoding="utf-8")
    log.info("web export: %d events, %d tickers", len(events), len(top))
    return ev_path, top_path


def _upload_web(ev_path: Path, top_path: Path, *, bucket=None) -> None:
    bucket = bucket if bucket is not None else gcs.get_bucket()
    for src in (ev_path, top_path):
        # objects are overwritten each run → re-apply public-read ACL each time
        # (requires UBLA OFF on the bucket).
        gcs.upload_file(bucket, f"{WEB_PREFIX}/{src.name}", src, public=True)


def run(do_ndjson: bool, do_upload: bool, *, full: bool = False,
        do_web: bool = False) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s/%(levelname)s] %(message)s",
    )
    out_dir = settings.DATA_DIR / "exports"
    ndjson_path: Path | None = None
    if do_ndjson:
        ndjson_path = _export_ndjson(out_dir, full=full)
    # NDJSON upload: only on the NDJSON/data path (match unit: --ndjson --upload, or a
    # bare --upload to re-push the latest existing file). When --web is the action,
    # --upload targets the web files below, NOT the NDJSON — otherwise an enrich run
    # (`--web --upload`) would re-push stale NDJSON or SystemExit on a fresh VM.
    if do_upload and not do_web:
        if ndjson_path is None:
            # find latest export
            candidates = sorted(out_dir.glob("articles_*.ndjson"))
            if not candidates:
                raise SystemExit("no NDJSON to upload — pass --ndjson")
            ndjson_path = candidates[-1]
        _upload(ndjson_path)
    if do_web:
        ev_path, top_path = _write_web_files()
        if do_upload:
            _upload_web(ev_path, top_path)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ndjson", action="store_true",
                    help="write data/exports/articles_{full|delta}_<ts>.ndjson")
    ap.add_argument("--upload", action="store_true", help="gsutil cp to gs://esg-scan-data/")
    ap.add_argument("--full", action="store_true",
                    help="force full snapshot instead of delta-since-last-export")
    ap.add_argument("--web", action="store_true",
                    help="build+upload web/esg_events.json")
    args = ap.parse_args()
    if not (args.ndjson or args.upload or args.web):
        ap.error("pass --ndjson, --upload and/or --web")
    run(do_ndjson=args.ndjson, do_upload=args.upload, full=args.full, do_web=args.web)


if __name__ == "__main__":
    main()
