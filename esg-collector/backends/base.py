"""Shared backend interface + HTTP helpers.

Item shape returned by `fetch`:
    {
        "url": str,                  # publisher URL (or Google encoded for google_rss)
        "title": str,
        "description": str | None,   # snippet / og:description
        "sapo": str | None,          # BaoMoi sapo when available
        "published_at": str | None,  # ISO YYYY-MM-DD
        "source": str | None,        # publisher name
    }
"""

from __future__ import annotations

import random
import time
from typing import Protocol

import requests

from config import settings


class RateLimitError(Exception):
    """Raised when a backend returns 429/5xx — triggers backoff schedule."""


class BackendError(Exception):
    """Transient backend error worth retrying once before backoff."""


class Backend(Protocol):
    name: str
    def fetch(self, query: str, after: str, before: str) -> list[dict]: ...


def build_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = random.choice(settings.USER_AGENTS)
    s.headers["Accept-Language"] = "vi,en;q=0.9"
    return s


def sleep_with_jitter(base: float, frac: float = 0.33) -> None:
    delta = base * frac
    time.sleep(max(0.1, base + random.uniform(-delta, delta)))


def rotate_ua(session: requests.Session) -> None:
    session.headers["User-Agent"] = random.choice(settings.USER_AGENTS)


def get(session: requests.Session, url: str, *, timeout: int = 30, **kwargs) -> requests.Response:
    """GET with rate-limit detection. Raises RateLimitError on 429/5xx."""
    r = session.get(url, timeout=timeout, **kwargs)
    if r.status_code in (429, 503, 502, 504):
        raise RateLimitError(f"{r.status_code} from {url}")
    if r.status_code >= 400:
        raise BackendError(f"HTTP {r.status_code} from {url}: {r.text[:200]}")
    return r
