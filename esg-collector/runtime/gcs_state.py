"""Checkout/checkin of the single SQLite DB blob on GCS.

download_db -> local file + its generation (None if the blob does not exist yet).
upload_db(if_generation) -> new generation; pass if_generation=0 for create-only,
or the generation returned by download_db for a safe overwrite. Raises
google.api_core.exceptions.PreconditionFailed if the blob moved underneath us.
"""
from __future__ import annotations

from pathlib import Path

from runtime import gcs

DB_BLOB = "state/articles.db"
PER_TICKER_PREFIX = "per_ticker/"


def download_db(bucket, local_path) -> int | None:
    return gcs.download_file(bucket, DB_BLOB, local_path)


def download_per_ticker(bucket, dest_dir) -> int:
    """Restore the accumulated per_ticker/*.json from GCS into dest_dir.

    The incremental match MERGES new hits into the existing per_ticker files
    (pipeline.match._load_existing), but Cloud Run's /tmp starts empty every
    run — so without this restore the match would rewrite each touched ticker
    with ONLY that run's matches and the web export would collapse the
    dashboard to a handful of events. Returns the count restored. Backfill's
    --rematch-all wipes + rebuilds these from the DB, so the restore is a
    harmless no-op overwrite there."""
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    n = 0
    for name in gcs.list_blob_names(bucket, PER_TICKER_PREFIX):
        if not name.endswith(".json"):
            continue
        gcs.download_file(bucket, name, dest / Path(name).name)
        n += 1
    return n


def upload_db(bucket, local_path, *, if_generation) -> int:
    return gcs.upload_file(bucket, DB_BLOB, local_path,
                           if_generation_match=if_generation)
