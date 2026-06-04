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
from datetime import datetime, timezone

from config import settings
from core import storage
from runtime import gcs, gcs_lock, gcs_state

log = logging.getLogger("runtime.job")

PY = sys.executable
BACKENDS = ("google_rss", "baomoi", "brave")
ENRICH_LIMIT = 25


def stage_commands(mode: str, tickers: list[str] | None) -> list[list[str]]:
    """Pure: the ordered argv lists for one run. Fetch backends are listed
    separately but the orchestrator runs the three concurrently (see run())."""
    enqueue = [PY, "-m", "core.queue_builder", "--mode", mode]
    if tickers:
        enqueue += ["--tickers", *tickers]

    cmds: list[list[str]] = [enqueue]
    for b in BACKENDS:
        cmds.append([PY, "-m", "workers.runner", "--backend", b, "--drain"])
    cmds.append([PY, "-m", "workers.body_fetcher", "--drain"])

    if mode == "backfill":
        cmds.append([PY, "-m", "pipeline.match", "--rematch-all"])
        # enrich intentionally skipped in backfill (daily catches up)
    else:  # daily
        cmds.append([PY, "-m", "pipeline.match"])
        cmds.append([PY, "-m", "enrich.runner", "--limit", str(ENRICH_LIMIT)])
    # Two export stages: export.run() makes --upload target the WEB files when
    # --web is present (see export.py:210-213), so a combined --ndjson --web
    # --upload would silently NOT push the raw_esg NDJSON + per_ticker. Split:
    cmds.append([PY, "-m", "pipeline.export", "--ndjson", "--upload"])  # raw_esg + per_ticker
    cmds.append([PY, "-m", "pipeline.export", "--web", "--upload"])     # web/*.json
    return cmds


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_fetch_concurrently(cmds: list[list[str]], env) -> None:
    """Run the three fetch-backend subprocesses in parallel, wait for all."""
    procs = [subprocess.Popen(c, env=env) for c in cmds]
    for p in procs:
        rc = p.wait()
        if rc != 0:
            log.warning("fetch subprocess exited rc=%d (continuing)", rc)


class LockLost(Exception):
    """Raised when a periodic refresh finds the lock was taken over."""


def _run_stage(cmd: list[str], env) -> None:
    """Run one pipeline stage as a subprocess (seam for tests)."""
    subprocess.run(cmd, env=env, check=False)


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
        storage.init_db()  # apply migrations on the downloaded (or fresh) blob

        cmds = stage_commands(mode, tickers)
        env = dict(os.environ)
        fetch_cmds = [c for c in cmds if "workers.runner" in " ".join(c)]
        other_cmds = [c for c in cmds if c not in fetch_cmds]
        enqueue, *post_fetch = other_cmds  # enqueue is first non-fetch cmd by construction

        _run_stage(enqueue, env)
        handle = _refresh(bucket, handle, mode=mode, ttl_seconds=ttl_seconds)

        _run_fetch_concurrently(fetch_cmds, env)
        handle = _refresh(bucket, handle, mode=mode, ttl_seconds=ttl_seconds)

        for c in post_fetch:  # body -> match -> [enrich] -> export(ndjson) -> export(web)
            _run_stage(c, env)
            handle = _refresh(bucket, handle, mode=mode, ttl_seconds=ttl_seconds)

        new_gen = 0 if gen is None else gen
        gcs_state.upload_db(bucket, settings.DB_PATH, if_generation=new_gen)
        log.info("checked in DB blob; run complete")
        return 0
    except LockLost:
        owns_lock = False  # another job took over — do NOT release or check in
        log.error("lost the pipeline lock mid-run (stale-takeover) — aborting without check-in")
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
