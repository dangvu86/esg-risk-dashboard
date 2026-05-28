"""Body fetcher worker.

Targets articles with `body_status='pending'` whose alias couldn't be matched
on title/description/sapo alone — fetching the body lets the alias matcher
catch mentions buried inside the article.

Concurrency: small thread pool (default 8) calling Jina; rate limiting lives
in `body_fetcher.jina.fetch` (token-bucket) so a single shared cap covers
every concurrent caller. On Jina failure we fall through to the bs4
fallback fetcher.

Run:  python -m workers.body_fetcher [--workers 8] [--limit 2000]
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict

from body_fetcher import jina, fallback
from core import alias_matcher, storage


log = logging.getLogger("body_fetcher")

_stop = False


def _on_signal(signum, _frame):
    global _stop
    log.info("signal %s — stopping", signum)
    _stop = True


def _prefetch_hits(article: dict):
    """Match alias on title/desc/sapo only. Returns list[AliasHit]."""
    return alias_matcher.match_article(
        {
            "title": article.get("title"),
            "description": article.get("description"),
            "sapo": article.get("sapo"),
        },
        fields=("title", "description", "sapo"),
    )


def _serialize_hits(hits) -> str:
    return json.dumps([asdict(h) for h in hits], ensure_ascii=False)


def _fetch_one(url: str) -> tuple[str | None, str]:
    body, status = jina.fetch(url)
    if status == "fetched":
        return body, status
    if status == "ratelimited":
        time.sleep(2)
    body, status = fallback.fetch(url)
    return body, status


def _candidate_articles(conn, limit: int) -> list[dict]:
    rows = conn.execute(
        "SELECT article_id, url_original, url_canonical, title, description, sapo "
        "FROM articles WHERE body_status='pending' AND match_status='pending' "
        "ORDER BY fetched_at ASC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def run(workers: int = 8, batch_limit: int = 500, idle_sleep: int = 60) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s/%(levelname)s] %(message)s",
    )
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    storage.init_db()
    conn = storage.connect()
    log.info("body_fetcher started: workers=%d", workers)

    while not _stop:
        candidates = _candidate_articles(conn, batch_limit)
        if not candidates:
            log.info("no pending bodies — sleeping %ds", idle_sleep)
            for _ in range(idle_sleep):
                if _stop:
                    break
                time.sleep(1)
            continue

        # Skip those that already match on title/desc/sapo; cache the hits
        # so pipeline.match can flush them to per_ticker JSON without
        # re-running the regex pool on the same article.
        to_fetch = []
        for art in candidates:
            hits = _prefetch_hits(art)
            if hits:
                storage.cache_hits(conn, art["article_id"], _serialize_hits(hits))
                storage.mark_body(conn, art["article_id"], "skipped")
            else:
                to_fetch.append(art)
        log.info("batch %d candidates → %d need body", len(candidates), len(to_fetch))

        if not to_fetch:
            continue

        # Run Jina fetch in parallel.
        urls = {art["article_id"]: (art.get("url_original") or art.get("url_canonical")) for art in to_fetch}
        pool = ThreadPoolExecutor(max_workers=workers)
        try:
            futs = {pool.submit(_fetch_one, u): aid for aid, u in urls.items()}
            for fut in as_completed(futs):
                if _stop:
                    break
                aid = futs[fut]
                try:
                    body, status = fut.result()
                except Exception as e:
                    log.warning("fetch error %s: %s", aid, e)
                    storage.mark_body(conn, aid, "failed")
                    continue
                if status == "fetched":
                    storage.mark_body(conn, aid, "fetched", body)
                else:
                    storage.mark_body(conn, aid, status)
        finally:
            # cancel_futures=True (Py 3.9+) drops queued-but-unstarted tasks
            # on shutdown signal; in-flight HTTP calls still finish, but we
            # don't wait for the rest of the batch.
            pool.shutdown(wait=not _stop, cancel_futures=_stop)
        log.info("batch done")

    conn.close()
    log.info("body_fetcher stopped")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--batch-limit", type=int, default=500)
    args = ap.parse_args()
    run(workers=args.workers, batch_limit=args.batch_limit)


if __name__ == "__main__":
    main()
