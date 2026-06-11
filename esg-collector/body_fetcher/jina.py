"""Jina Reader HTML fetcher (FALLBACK).

Used when the direct `fallback` fetch is blocked (anti-bot) or the URL is still
a Google-encoded redirect — Jina (https://r.jina.ai/<url>) follows redirects
and bypasses simple bot walls. We ask it for raw HTML (`X-Return-Format: html`)
so `body_fetcher.extract` (trafilatura) can isolate the article, instead of the
old full-page markdown that buried the article in nav/ads.

Free tier ~20 RPM; with JINA_API_KEY ~200 RPM. A process-wide token bucket
shares one budget across the worker thread pool so concurrent threads don't
burst past the cap.
"""
from __future__ import annotations

import threading
import time

import requests

from config import settings


ENDPOINT = "https://r.jina.ai/"

_FREE_RPM = 18    # small margin under the documented ~20 RPM free cap
_AUTH_RPM = 180   # margin under ~200 RPM with an API key
_MIN_HTML = 2000  # shorter than this is an error/stub, not a real page


def _rpm() -> int:
    return _AUTH_RPM if settings.JINA_API_KEY else _FREE_RPM


_lock = threading.Lock()
_min_gap_s: float = 60.0 / _rpm()
_next_allowed_at: float = 0.0


def _pace() -> None:
    """Block until this thread owns a fresh rate-limit slot (single token)."""
    global _next_allowed_at
    with _lock:
        now = time.monotonic()
        wait = _next_allowed_at - now
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
        _next_allowed_at = max(now, _next_allowed_at) + _min_gap_s


def fetch(url: str, timeout: int = 45) -> tuple[str | None, str]:
    """Return (html, status). status in {fetched, failed, ratelimited}."""
    if not url:
        return None, "failed"
    headers = {
        "X-Return-Format": "html",
        "Accept": "text/html",
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
    if r.status_code >= 400 or not r.text or len(r.text) < _MIN_HTML:
        return None, "failed"
    return r.text, "fetched"
