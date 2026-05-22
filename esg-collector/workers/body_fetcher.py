"""Body fetcher worker.

Targets articles with `body_status='pending'` whose alias couldn't be matched
on title/description/sapo alone — fetching the body lets the alias matcher
catch mentions buried inside the article.

Concurrency: small thread pool (default 8) calling Jina, with a per-call
throttle to stay under the Jina 200 RPM cap when authenticated. On Jina
failure we fall through to the bs4 fallback fetcher.

Run:  python -m workers.body_fetcher [--workers 8] [--limit 2000]
"""

from __future__ import annotations

import argparse
import logging
import signal
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from body_fetcher import jina, fallback
from core import alias_matcher, storage


log = logging.getLogger("body_fetcher")

_stop = False


def _on_signal(signum, _frame):
    global _stop
    log.info("signal %s — stopping", signum)
    _stop = True


def _needs_body(article: dict) -> bool:
    """Skip body fetch if title/description/sapo already match some alias."""
    hits = alias_matcher.match_article(
        {
            "title": article.get("title"),
            "description": article.get("description"),
            "sapo": article.get("sapo"),
        },
        fields=("title", "description", "sapo"),
    )
    return len(hits) == 0


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

        # Skip those that already match on title/desc/sapo; mark body_status='skipped'.
        to_fetch = []
        for art in candidates:
            if _needs_body(art):
                to_fetch.append(art)
            else:
                storage.mark_body(conn, art["article_id"], "skipped")
        log.info("batch %d candidates → %d need body", len(candidates), len(to_fetch))

        if not to_fetch:
            continue

        # Run Jina fetch in parallel
        urls = {art["article_id"]: (art.get("url_original") or art.get("url_canonical")) for art in to_fetch}
        with ThreadPoolExecutor(max_workers=workers) as pool:
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
