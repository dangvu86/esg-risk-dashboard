"""Body fetcher via Jina Reader (https://r.jina.ai/<url>).

One HTTP call replaces googlenewsdecoder + bs4: Jina follows redirects
(including Google News encoded links) and returns clean markdown.

Free tier ~20 RPM; with JINA_API_KEY 200 RPM. The rate limit is enforced
here via a process-wide token bucket so multiple worker threads share one
budget — the body_fetcher thread pool would otherwise burst N concurrent
calls past the cap.
"""

from __future__ import annotations

import threading
import time

import requests

from config import settings
from body_fetcher.fallback import ARTICLE_SELECTORS

_TARGET_SELECTOR = ",".join(ARTICLE_SELECTORS)
_MIN_BODY = 200  # below this, treat selector result as a miss and retry full-page


ENDPOINT = "https://r.jina.ai/"

# Free tier is ~20 RPM; authenticated key raises to ~200 RPM. Keep a small
# safety margin under the documented cap so brief bursts don't trip 429.
_FREE_RPM = 18
_AUTH_RPM = 180


def _rpm() -> int:
    return _AUTH_RPM if settings.JINA_API_KEY else _FREE_RPM


_lock = threading.Lock()
_min_gap_s: float = 60.0 / _rpm()
_next_allowed_at: float = 0.0


def _pace() -> None:
    """Block the calling thread until it owns a fresh slot.

    Single global token: under N concurrent worker threads the (N-1)
    losers wait their turn instead of all firing simultaneously.
    """
    global _next_allowed_at
    with _lock:
        now = time.monotonic()
        wait = _next_allowed_at - now
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
        _next_allowed_at = max(now, _next_allowed_at) + _min_gap_s


def fetch(url: str, timeout: int = 30) -> tuple[str | None, str]:
    """Return (body_markdown, status). status ∈ {fetched, failed, ratelimited}.

    First tries with X-Target-Selector so Jina returns only the article body
    (drops nav/sidebar/related). If that yields too little (uncovered site),
    retry once without the selector (full page)."""
    if not url:
        return None, "failed"

    def _get(with_selector: bool):
        headers = {
            "X-Return-Format": "markdown",
            "Accept": "text/markdown, text/plain",
        }
        if settings.JINA_API_KEY:
            headers["Authorization"] = f"Bearer {settings.JINA_API_KEY}"
        if with_selector:
            headers["X-Target-Selector"] = _TARGET_SELECTOR
        _pace()
        try:
            r = requests.get(ENDPOINT + url, headers=headers, timeout=timeout)
        except requests.RequestException:
            return None, "failed"
        if r.status_code == 429:
            return None, "ratelimited"
        if r.status_code >= 400:
            return None, "failed"
        return (r.text or "").strip(), "fetched"

    body, status = _get(with_selector=True)
    if status == "fetched" and (not body or len(body) < _MIN_BODY):
        body, status = _get(with_selector=False)  # selector missed -> full page
    if status == "fetched" and not body:
        return None, "failed"
    return body, status
