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
    """Return (body_markdown, status).

    status ∈ {fetched, failed, ratelimited}.
    """
    if not url:
        return None, "failed"
    headers = {
        "X-Return-Format": "markdown",
        "Accept": "text/markdown, text/plain",
    }
    if settings.JINA_API_KEY:
        headers["Authorization"] = f"Bearer {settings.JINA_API_KEY}"
    _pace()
    try:
        r = requests.get(ENDPOINT + url, headers=headers, timeout=timeout)
    except requests.RequestException:
        return None, "failed"
    if r.status_code == 429:
        return None, "ratelimited"
    if r.status_code >= 400:
        return None, "failed"
    body = (r.text or "").strip()
    if not body:
        return None, "failed"
    return body, "fetched"
