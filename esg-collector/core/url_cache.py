"""Resolve Google News encoded URLs to publisher URLs, with SQLite cache.

Why a cache: googlenewsdecoder makes a live HTTP request per URL and is
rate-limited by Google. Most ESG sub-queries return overlapping articles
across keyword groups and date chunks, so the same encoded URL appears
many times across the backfill. Caching avoids redundant decodes and
makes a re-run after `pipeline.redecode_google` essentially free.

Threading: a single module-level lock serialises decode calls within one
process to honour the 1s global pacing. Across processes only the
google_rss runner decodes inline, so contention isn't an issue.
"""

from __future__ import annotations

import threading
import time

from core import storage
from core.canonicalize import decode_google_url, is_google_news_url


_DEFAULT_RATE_LIMIT_S = 1.0

_lock = threading.Lock()
_last_call: list[float] = [0.0]


def resolve(conn, url: str, *, rate_limit_s: float = _DEFAULT_RATE_LIMIT_S) -> str:
    """Return decoded publisher URL, or the original URL when not Google
    News / decode fails permanently.

    Hits the cache first. On miss, sleeps to respect the global rate limit,
    decodes via googlenewsdecoder, then stores the outcome ('ok' or 'failed').
    """
    if not url or not is_google_news_url(url):
        return url

    cached = storage.decode_cache_get(conn, url)
    if cached is not None:
        decoded, status = cached
        return decoded if (status == "ok" and decoded) else url

    with _lock:
        gap = time.monotonic() - _last_call[0]
        if gap < rate_limit_s:
            time.sleep(rate_limit_s - gap)
        _last_call[0] = time.monotonic()

    decoded = decode_google_url(url)
    if decoded and decoded != url:
        storage.decode_cache_put(conn, url, decoded, "ok")
        return decoded
    storage.decode_cache_put(conn, url, None, "failed")
    return url
