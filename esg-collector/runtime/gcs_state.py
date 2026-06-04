"""Checkout/checkin of the single SQLite DB blob on GCS.

download_db -> local file + its generation (None if the blob does not exist yet).
upload_db(if_generation) -> new generation; pass if_generation=0 for create-only,
or the generation returned by download_db for a safe overwrite. Raises
google.api_core.exceptions.PreconditionFailed if the blob moved underneath us.
"""
from __future__ import annotations

from runtime import gcs

DB_BLOB = "state/articles.db"


def download_db(bucket, local_path) -> int | None:
    return gcs.download_file(bucket, DB_BLOB, local_path)


def upload_db(bucket, local_path, *, if_generation) -> int:
    return gcs.upload_file(bucket, DB_BLOB, local_path,
                           if_generation_match=if_generation)
