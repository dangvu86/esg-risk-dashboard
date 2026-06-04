"""GCS-object mutex for the pipeline.

acquire(): create state/pipeline.lock with if_generation_match=0 (atomic create).
If it already exists, parse its body; take over only if started_at+ttl is past.
Returns a LockHandle (carries the blob generation we hold) or None if a live
owner holds it. release()/refresh() act through that handle's generation so two
jobs can't stomp each other.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from google.api_core.exceptions import PreconditionFailed
from runtime import gcs

log = logging.getLogger("runtime.gcs_lock")

LOCK_BLOB = "state/pipeline.lock"


@dataclass
class LockHandle:
    owner: str
    generation: int


def _parse(iso: str) -> datetime:
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _is_stale(body: dict, now: str) -> bool:
    try:
        started = _parse(body["started_at"])
        ttl = int(body.get("ttl_seconds", 3600))
    except Exception:
        return True  # unparseable lock → treat as abandoned
    return _parse(now) > started + timedelta(seconds=ttl)


def _write(bucket, *, owner, mode, now, ttl_seconds, if_generation) -> LockHandle | None:
    body = json.dumps({"owner": owner, "mode": mode,
                       "started_at": now, "ttl_seconds": ttl_seconds})
    try:
        gen = gcs.upload_text(bucket, LOCK_BLOB, body, if_generation_match=if_generation)
        return LockHandle(owner=owner, generation=gen)
    except PreconditionFailed:
        return None


def acquire(bucket, *, owner: str, mode: str, now: str,
            ttl_seconds: int = 3600) -> LockHandle | None:
    # 1. try create-only
    h = _write(bucket, owner=owner, mode=mode, now=now,
               ttl_seconds=ttl_seconds, if_generation=0)
    if h is not None:
        return h
    # 2. exists — read it; take over only if stale, using its generation as the
    #    precondition so a racing taker-over loses cleanly.
    cur = gcs.read_text(bucket, LOCK_BLOB)
    if cur is None:
        return None  # vanished between create-fail and read; let caller retry
    text, gen = cur
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        body = {}
    if not _is_stale(body, now):
        log.info("lock held by %s — skipping", body.get("owner"))
        return None
    log.warning("stale lock (owner=%s) — taking over", body.get("owner"))
    return _write(bucket, owner=owner, mode=mode, now=now,
                  ttl_seconds=ttl_seconds, if_generation=gen)


def refresh(bucket, handle: LockHandle, *, now: str, mode: str = "daily",
            ttl_seconds: int = 3600) -> LockHandle | None:
    h = _write(bucket, owner=handle.owner, mode=mode, now=now,
               ttl_seconds=ttl_seconds, if_generation=handle.generation)
    return h  # None if someone else moved the lock; caller should abort


def release(bucket, handle: LockHandle) -> None:
    try:
        gcs.delete(bucket, LOCK_BLOB, if_generation_match=handle.generation)
    except PreconditionFailed:
        log.warning("lock moved before release — not ours to delete")
