"""Cloud Run Job entrypoint: own the lock + DB blob, run the pipeline stages.

  python -m runtime.job --mode daily
  python -m runtime.job --mode backfill [--tickers DBC HPG]

Lifecycle: acquire lock → download DB blob → init_db → run stages (each a
subprocess on the shared ESG_DATA_DIR SQLite) → upload blob (generation-match)
→ release lock. If the lock is held by a live owner, exit 0 ("skipped").

Each periodic refresh re-writes the lock blob (new generation) and returns a
NEW handle; we rebind `handle` to it so the next refresh and the final release
present the correct generation precondition. If a refresh returns None the lock
was taken over by another job (stale-takeover) — we abort WITHOUT checking in
the DB (don't clobber the other job) and WITHOUT releasing (not ours).
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

from google.api_core.exceptions import PreconditionFailed

from config import settings
from core import storage
from runtime import gcs, gcs_lock, gcs_state

log = logging.getLogger("runtime.job")

PY = sys.executable
BACKENDS = ("google_rss", "baomoi", "brave")
ENRICH_LIMIT = 25


def stage_commands(mode: str, tickers: list[str] | None):
    """Pure: the stages for one run, grouped so the caller consumes structure
    instead of re-parsing argv strings. Returns (enqueue, fetch_cmds, post_fetch):
    `enqueue` is one argv; `fetch_cmds` are the backend argvs run concurrently;
    `post_fetch` are the sequential argvs (body → match → [enrich] → export
    ndjson → export web)."""
    enqueue = [PY, "-m", "core.queue_builder", "--mode", mode]
    if tickers:
        enqueue += ["--tickers", *tickers]

    fetch_cmds = [[PY, "-m", "workers.runner", "--backend", b, "--drain"]
                  for b in BACKENDS]

    post_fetch: list[list[str]] = [[PY, "-m", "workers.body_fetcher", "--drain"]]
    if mode == "backfill":
        post_fetch.append([PY, "-m", "pipeline.match", "--rematch-all"])
        # enrich intentionally skipped in backfill (daily catches up)
    else:  # daily
        post_fetch.append([PY, "-m", "pipeline.match"])
        post_fetch.append([PY, "-m", "enrich.runner", "--limit", str(ENRICH_LIMIT)])
    # Two export stages: export.run() makes --upload target the WEB files when
    # --web is present (see export.py), so a combined --ndjson --web --upload
    # would silently NOT push the raw_esg NDJSON + per_ticker. Split:
    post_fetch.append([PY, "-m", "pipeline.export", "--ndjson", "--upload"])  # raw_esg + per_ticker
    post_fetch.append([PY, "-m", "pipeline.export", "--web", "--upload"])     # web/*.json
    return enqueue, fetch_cmds, post_fetch


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_fetch_concurrently(cmds, env, *, bucket, handle, mode, ttl_seconds,
                            refresh_every: int = 1800, poll_seconds: int = 5):
    """Run the fetch backends concurrently, refreshing the lock every
    refresh_every seconds so a long drain (esp. backfill) never trips its TTL.
    Returns the latest lock handle. Propagates LockLost if the lock was taken
    over (caller aborts without check-in)."""
    procs = [subprocess.Popen(c, env=env) for c in cmds]
    since_refresh = 0
    while any(p.poll() is None for p in procs):
        time.sleep(poll_seconds)
        since_refresh += poll_seconds
        if since_refresh >= refresh_every:
            handle = _refresh(bucket, handle, mode=mode, ttl_seconds=ttl_seconds)
            since_refresh = 0
    for p in procs:
        if p.returncode:
            log.warning("fetch subprocess exited rc=%d", p.returncode)
    return handle


class LockLost(Exception):
    """Raised when a periodic refresh finds the lock was taken over."""


def _run_stage(cmd: list[str], env) -> None:
    """Run one pipeline stage as a subprocess (seam for tests).

    Post-fetch stages (match/enrich/export) are intentionally NON-fatal: a
    failing stage is logged but does not abort the run. The batch model relies
    on the next run catching up, and aborting here would skip the DB check-in
    and lose the fetch/match progress already made. The non-zero rc is logged
    so a failed export/match is greppable in Cloud Run logs.
    """
    rc = subprocess.run(cmd, env=env, check=False).returncode
    if rc != 0:
        log.warning("stage exited rc=%d: %s", rc, " ".join(cmd))


def _refresh(bucket, handle, *, mode: str, ttl_seconds: int):
    h = gcs_lock.refresh(bucket, handle, now=_now(), mode=mode, ttl_seconds=ttl_seconds)
    if h is None:
        raise LockLost()
    return h


def run(mode: str, tickers: list[str] | None, *, ttl_seconds: int, bucket=None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s/%(levelname)s] %(message)s")
    owner = os.environ.get("CLOUD_RUN_EXECUTION", _now())
    bucket = bucket if bucket is not None else gcs.get_bucket()

    handle = gcs_lock.acquire(bucket, owner=owner, mode=mode,
                              now=_now(), ttl_seconds=ttl_seconds)
    if handle is None:
        log.info("another run holds the lock — skipping this execution")
        return 0

    owns_lock = True
    try:
        gen = gcs_state.download_db(bucket, settings.DB_PATH)
        # Restore accumulated per-ticker JSONs so the incremental match merges
        # into the full set; /tmp is empty each Cloud Run run, and without this
        # the web export would collapse the dashboard to just this run's matches.
        restored = gcs_state.download_per_ticker(bucket, settings.PER_TICKER_DIR)
        log.info("restored %d per_ticker files from GCS", restored)
        storage.init_db()  # apply migrations on the downloaded (or fresh) blob

        enqueue, fetch_cmds, post_fetch = stage_commands(mode, tickers)
        # Subprocesses inherit this env, so Secret Manager values (BRAVE/JINA/
        # GROQ keys) reach each stage only if Cloud Run injects them as env vars
        # (--set-secrets), not as mounted files.
        env = dict(os.environ)

        _run_stage(enqueue, env)
        handle = _refresh(bucket, handle, mode=mode, ttl_seconds=ttl_seconds)

        handle = _run_fetch_concurrently(
            fetch_cmds, env, bucket=bucket, handle=handle,
            mode=mode, ttl_seconds=ttl_seconds)
        handle = _refresh(bucket, handle, mode=mode, ttl_seconds=ttl_seconds)

        for c in post_fetch:  # body -> match -> [enrich] -> export(ndjson) -> export(web)
            _run_stage(c, env)
            handle = _refresh(bucket, handle, mode=mode, ttl_seconds=ttl_seconds)

        # Defensive: all stage subprocesses have exited (connections closed, so
        # SQLite already auto-checkpointed), but fold any residual WAL into the
        # main .db before upload so the blob can never miss committed rows.
        _ck = storage.connect(); _ck.execute("PRAGMA wal_checkpoint(TRUNCATE)"); _ck.close()
        new_gen = 0 if gen is None else gen
        gcs_state.upload_db(bucket, settings.DB_PATH, if_generation=new_gen)
        log.info("checked in DB blob; run complete")
        return 0
    except LockLost:
        owns_lock = False  # another job took over — do NOT release or check in
        log.error("lost the pipeline lock mid-run (stale-takeover) — aborting without check-in")
        return 1
    except PreconditionFailed:
        # DB blob generation moved between download and check-in (we still hold
        # the lock, so release in finally). Should not happen under the lock;
        # abort without clobbering — the next run re-downloads and redoes the work.
        log.error("DB blob moved before check-in — aborting without overwrite")
        return 1
    finally:
        if owns_lock:
            gcs_lock.release(bucket, handle)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("daily", "backfill"), required=True)
    ap.add_argument("--tickers", nargs="+", default=None)
    ap.add_argument("--lock-ttl", type=int, default=int(os.environ.get("LOCK_TTL_SECONDS", "7200")))
    args = ap.parse_args()
    raise SystemExit(run(args.mode, args.tickers, ttl_seconds=args.lock_ttl))


if __name__ == "__main__":
    main()
