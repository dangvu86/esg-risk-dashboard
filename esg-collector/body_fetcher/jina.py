"""Body fetcher via Jina Reader (https://r.jina.ai/<url>).

One HTTP call replaces googlenewsdecoder + bs4: Jina follows redirects
(including Google News encoded links) and returns clean markdown.

Free tier ~20 RPM; with JINA_API_KEY 200 RPM.
"""

from __future__ import annotations

import requests

from config import settings


ENDPOINT = "https://r.jina.ai/"


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
