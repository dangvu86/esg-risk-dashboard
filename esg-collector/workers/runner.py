"""Search worker: drain `search_queue` for one backend.

Run one process per backend:
    python -m workers.runner --backend google_rss
    python -m workers.runner --backend baomoi
    python -m workers.runner --backend brave

Each iteration:
  1. pull next task whose `next_attempt` <= now
  2. backend.fetch(query, after, before)
  3. INSERT OR IGNORE into articles
  4. mark task done; sleep throttle±jitter
  5. on RateLimitError → schedule backoff; on too many → mark failed
"""

from __future__ import annotations

import argparse
import importlib
import logging
import signal
import sys
import time
from datetime import datetime

from backends import base
from config import settings
from core import queue_builder, storage, url_cache
from core.canonicalize import canonicalize, dedup_key, domain_of
from core.title import title_hash as _title_hash


log = logging.getLogger("runner")


def _field(task, key):
    """Safe field access that works for both sqlite3.Row and plain dict.

    sqlite3.Row raises IndexError for missing keys; dict raises KeyError.
    Both are caught here so callers get None for absent columns (e.g. legacy
    rows without kind/ticker, or plain-dict tasks in tests).
    """
    try:
        return task[key]
    except (KeyError, IndexError):
        return None


BACKEND_MODULES = {
    "google_rss": "backends.google_rss",
    "baomoi":     "backends.baomoi",
    "brave":      "backends.brave",
}


_stop = False


def _on_signal(signum, _frame):
    global _stop
    log.info("signal %s — stopping after current task", signum)
    _stop = True


def _load_backend(name: str):
    return importlib.import_module(BACKEND_MODULES[name])


def _process_task(conn, backend_mod, task) -> tuple[int, int]:
    items = backend_mod.fetch(task["query"], task["after"], task["before"])
    fetched = len(items)
    inserted = 0
    is_google = backend_mod.name == "google_rss"
    # Stamp advisory provenance for alias tasks so downstream pipeline can use
    # ticker_hint as a soft signal (attribution still lives in alias_matcher).
    ticker_hint = _field(task, "ticker") if _field(task, "kind") == "alias" else None
    for it in items:
        url = it.get("url") or ""
        if not url:
            continue
        # Google News links are opaque base64 redirects — resolve to the real
        # publisher URL before deriving article_id, otherwise the same story
        # fetched by BaoMoi / Brave will look like a different article.
        if is_google:
            url = url_cache.resolve(conn, url)
        canon = canonicalize(url)
        key = dedup_key(url)
        if not key:
            continue
        title = it.get("title") or ""
        rec = {
            "article_id":    key,
            "url_canonical": canon or url,
            "url_original":  url,
            "domain":        domain_of(url),
            "title":         title,
            "title_hash":    _title_hash(title),
            "description":   it.get("description"),
            "sapo":          it.get("sapo"),
            "body":          None,
            "body_status":   "pending",
            "published_at":  it.get("published_at"),
            "source":        it.get("source"),
            "backend":       backend_mod.name,
            "group_key":     task["group_key"],
            "sub_query_ix":  task["sub_query_ix"],
            "ticker_hint":   ticker_hint,
        }
        if storage.insert_article(conn, rec):
            inserted += 1
    # Return BOTH counts: inserted drives coverage/items_found, while fetched
    # (pre-dedup) drives the near-cap split decision — on augment/re-runs the
    # articles already exist so inserted is near 0, but fetched still reveals a
    # capped month that must be split into weeks.
    return inserted, fetched


# Google News RSS caps each query at ~100 results. An alias×month task that
# returns at/near the cap was very likely truncated, so we re-enqueue that
# month split into ~weekly children (each returns fewer → under the cap). This
# is the ONLY place a worker writes to search_queue.
_NEAR_CAP = 90


def _maybe_split(conn, backend_mod, task, n_items: int) -> None:
    """Re-enqueue a near-cap Google alias month as ~weekly child alias tasks.

    Narrowly scoped: only fires for the google_rss backend on an alias task
    that returned >= _NEAR_CAP items. Keyword tasks, other backends, and
    below-threshold results are left untouched.
    """
    if backend_mod.name != "google_rss":
        return
    if _field(task, "kind") != "alias":
        return
    if n_items < _NEAR_CAP:
        return
    for wa, wb in queue_builder.weekly_subchunks(_field(task, "after"), _field(task, "before")):
        storage.enqueue_task(
            conn,
            backend="google_rss",
            kind="alias",
            ticker=_field(task, "ticker"),
            group_key="alias",
            sub_query_ix=_field(task, "sub_query_ix"),
            query=_field(task, "query"),
            after=wa,
            before=wb,
        )


def run(backend_name: str) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s/%(levelname)s] %(message)s",
    )
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    backend_mod = _load_backend(backend_name)
    throttle = settings.THROTTLE[backend_name]
    backoff_sched = settings.BACKOFF[backend_name]

    storage.init_db()
    conn = storage.connect()
    log.info("runner started: backend=%s throttle=%.1fs", backend_name, throttle)

    idle_sleep = 60
    while not _stop:
        task = storage.next_task(conn, backend_name)
        if task is None:
            log.info("no task ready — sleeping %ds", idle_sleep)
            for _ in range(idle_sleep):
                if _stop:
                    break
                time.sleep(1)
            continue

        log.info("task %s [%s %s→%s] q=%r",
                 task["task_id"], task["group_key"], task["after"], task["before"], task["query"])

        try:
            n_inserted, n_fetched = _process_task(conn, backend_mod, task)
            storage.mark_task_done(conn, task["task_id"], n_inserted)
            # Split on the FETCHED (pre-dedup) count so re-runs whose articles
            # are already in the pool still split a capped month into weeks.
            _maybe_split(conn, backend_mod, task, n_fetched)
            log.info("  → inserted=%d fetched=%d", n_inserted, n_fetched)
        except base.RateLimitError as e:
            attempts = task["attempts"] + 1
            wait = backoff_sched[min(attempts - 1, len(backoff_sched) - 1)]
            outcome = storage.mark_task_backoff(
                conn, task["task_id"], wait, str(e),
                max_attempts=settings.MAX_ATTEMPTS,
            )
            log.warning("  ratelimit (%s) attempts=%d → %s (retry in %ds)",
                        e, attempts, outcome, wait)
            # Pause this worker a bit too, not just the task
            time.sleep(min(wait, 300))
            continue
        except base.BackendError as e:
            attempts = task["attempts"] + 1
            wait = backoff_sched[min(attempts - 1, len(backoff_sched) - 1)] // 2
            storage.mark_task_backoff(
                conn, task["task_id"], wait, str(e),
                max_attempts=settings.MAX_ATTEMPTS,
            )
            log.warning("  backend error: %s — retry in %ds", e, wait)
        except Exception as e:
            log.exception("  unexpected error on task %s: %s", task["task_id"], e)
            storage.mark_task_backoff(
                conn, task["task_id"], 600, f"unexpected: {e}",
                max_attempts=settings.MAX_ATTEMPTS,
            )

        base.sleep_with_jitter(throttle)

    conn.close()
    log.info("runner stopped")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", required=True, choices=list(BACKEND_MODULES.keys()))
    args = ap.parse_args()
    run(args.backend)


if __name__ == "__main__":
    main()
