"""Direct article-HTML fetcher (PRIMARY).

Plain `requests` GET with full browser headers. Measured to fetch 11/12 of the
top VN news domains with no rate limit and no cost, so it is the primary path;
`jina` is the fallback for the few sites that block direct requests. Google
News encoded links are resolved to the publisher URL first.

Returns RAW HTML — extraction into clean article text happens in
`body_fetcher.extract` via trafilatura.
"""
from __future__ import annotations

import random

import requests

from config import settings
from core.canonicalize import decode_google_url, is_google_news_url


# Sites reject requests that don't look like a real browser; a bare UA isn't
# enough — Accept / Accept-Language matter too.
_BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "vi,en;q=0.9",
}
_MIN_HTML = 2000  # shorter than this is a block/redirect stub, not a real page


def _ua() -> str:
    return random.choice(settings.USER_AGENTS)


def fetch(url: str, timeout: int = 15) -> tuple[str | None, str]:
    """Return (html, status). status in {fetched, failed}."""
    if not url:
        return None, "failed"
    real = decode_google_url(url) if is_google_news_url(url) else url
    if not real:
        return None, "failed"
    headers = dict(_BASE_HEADERS, **{"User-Agent": _ua()})
    try:
        r = requests.get(real, headers=headers, timeout=timeout)
    except requests.RequestException:
        return None, "failed"
    if r.status_code != 200 or not r.text or len(r.text) < _MIN_HTML:
        return None, "failed"
    return r.text, "fetched"
