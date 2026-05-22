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
from core import storage
from core.canonicalize import canonicalize, dedup_key, domain_of


log = logging.getLogger("runner")

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


def _process_task(conn, backend_mod, task) -> int:
    items = backend_mod.fetch(task["query"], task["after"], task["before"])
    inserted = 0
    for it in items:
        url = it.get("url") or ""
        if not url:
            continue
        canon = canonicalize(url)
        key = dedup_key(url)
        if not key:
            continue
        rec = {
            "article_id":    key,
            "url_canonical": canon or url,
            "url_original":  url,
            "domain":        domain_of(url),
            "title":         it.get("title") or "",
            "description":   it.get("description"),
            "sapo":          it.get("sapo"),
            "body":          None,
            "body_status":   "pending",
            "published_at":  it.get("published_at"),
            "source":        it.get("source"),
            "backend":       backend_mod.name,
            "group_key":     task["group_key"],
            "sub_query_ix":  task["sub_query_ix"],
        }
        if storage.insert_article(conn, rec):
            inserted += 1
    return inserted


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
            n_items = _process_task(conn, backend_mod, task)
            storage.mark_task_done(conn, task["task_id"], n_items)
            log.info("  → %d items inserted/found", n_items)
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
