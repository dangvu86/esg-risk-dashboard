"""Thin helpers over a google-cloud-storage Bucket, duck-typed so tests can
inject an in-memory fake. All functions take a `bucket` object whose `.blob(name)`
returns something with the google storage Blob API subset we use.
"""
from __future__ import annotations

import logging
from pathlib import Path

from google.api_core.exceptions import NotFound

log = logging.getLogger("runtime.gcs")

GCS_BUCKET_NAME = "esg-scan-data"


def get_bucket(name: str = GCS_BUCKET_NAME):
    """Real bucket from the default-credentials client (used outside tests)."""
    from google.cloud import storage  # imported lazily so tests need no creds
    return storage.Client().bucket(name)


def upload_file(bucket, blob_name: str, local_path, *,
                if_generation_match=None, public: bool = False) -> int:
    blob = bucket.blob(blob_name)
    blob.upload_from_filename(str(local_path), if_generation_match=if_generation_match)
    if public:
        blob.make_public()
    blob.reload()
    return blob.generation


def download_file(bucket, blob_name: str, local_path) -> int | None:
    """Download to local_path. Returns the blob generation, or None if absent."""
    blob = bucket.blob(blob_name)
    try:
        blob.download_to_filename(str(local_path))
        blob.reload()
        return blob.generation
    except NotFound:
        return None


def upload_text(bucket, blob_name: str, text: str, *,
                if_generation_match=None, public: bool = False) -> int:
    blob = bucket.blob(blob_name)
    blob.upload_from_string(text, if_generation_match=if_generation_match)
    if public:
        blob.make_public()
    blob.reload()
    return blob.generation


def read_text(bucket, blob_name: str) -> tuple[str, int] | None:
    blob = bucket.blob(blob_name)
    try:
        text = blob.download_as_text()
        blob.reload()
        return text, blob.generation
    except NotFound:
        return None


def delete(bucket, blob_name: str, *, if_generation_match=None) -> None:
    blob = bucket.blob(blob_name)
    try:
        blob.delete(if_generation_match=if_generation_match)
    except NotFound:
        pass
