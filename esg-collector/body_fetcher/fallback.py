"""Fallback body fetcher: googlenewsdecoder + requests + bs4.

Used when Jina fails (rate-limited, banned URL, transient). Resolves Google
News encoded URLs to publisher URLs, then grabs the article body via a few
common selectors plus og:description.
"""

from __future__ import annotations

import random

import requests
from bs4 import BeautifulSoup

from config import settings
from core.canonicalize import decode_google_url, is_google_news_url


ARTICLE_SELECTORS = [
    "article",
    "div.detail-content",
    "div.article-body",
    "div[itemprop='articleBody']",
    "div.fck_detail",
    "div.singular-content",
    "div.entry-content",
    "div.content-news",
]


def _ua() -> str:
    return random.choice(settings.USER_AGENTS)


def _extract(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for sel in ARTICLE_SELECTORS:
        el = soup.select_one(sel)
        if el:
            text = el.get_text(separator="\n", strip=True)
            if len(text) > 200:
                return text
    og = soup.find("meta", property="og:description")
    if og and og.get("content"):
        return og["content"].strip()
    return ""


def fetch(url: str, timeout: int = 25) -> tuple[str | None, str]:
    if not url:
        return None, "failed"
    real = decode_google_url(url) if is_google_news_url(url) else url
    if not real:
        return None, "failed"
    try:
        r = requests.get(real, headers={"User-Agent": _ua()}, timeout=timeout)
    except requests.RequestException:
        return None, "failed"
    if r.status_code >= 400:
        return None, "failed"
    text = _extract(r.text)
    if not text:
        return None, "failed"
    return text, "fetched"
