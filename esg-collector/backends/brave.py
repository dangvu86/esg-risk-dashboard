"""Brave Web Search API backend.

Uses date-range freshness for the 4-5y window where Báo Mới archive runs
out. Paginates via `offset` (max 9 per Brave docs) until empty or 200 items.
"""

from __future__ import annotations

import json
import time
import urllib.parse
from datetime import datetime, timedelta

from backends import base
from config import settings


name = "brave"

ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
PER_PAGE = 20
MAX_OFFSET = 9  # Brave hard cap; gives 200 results per query when count=20
PAGE_DELAY = 1.1  # courtesy delay between offset pages


def _freshness(after: str, before: str) -> str:
    return f"{after}to{before}"


def _build_url(query: str, freshness: str, offset: int) -> str:
    params = {
        "q": query,
        "count": PER_PAGE,
        "offset": offset,
        "search_lang": "vi",
        "freshness": freshness,
        "spellcheck": "0",
    }
    return ENDPOINT + "?" + urllib.parse.urlencode(params)


def _parse_age(age: str, page_age: str) -> str | None:
    if page_age:
        try:
            return datetime.fromisoformat(page_age.replace("Z", "+00:00")).strftime("%Y-%m-%d")
        except Exception:
            pass
    if not age:
        return None
    a = age.lower()
    now = datetime.utcnow()
    try:
        num = int("".join(c for c in a.split()[0] if c.isdigit()))
    except (ValueError, IndexError):
        return None
    if "minute" in a or "hour" in a:
        return now.strftime("%Y-%m-%d")
    if "day" in a:
        return (now - timedelta(days=num)).strftime("%Y-%m-%d")
    if "week" in a:
        return (now - timedelta(weeks=num)).strftime("%Y-%m-%d")
    if "month" in a:
        return (now - timedelta(days=num * 30)).strftime("%Y-%m-%d")
    if "year" in a:
        return (now - timedelta(days=num * 365)).strftime("%Y-%m-%d")
    return None


def _call(session, query: str, freshness: str, offset: int) -> list[dict]:
    api_key = settings.BRAVE_API_KEY
    if not api_key:
        raise base.BackendError("BRAVE_API_KEY not set")
    url = _build_url(query, freshness, offset)
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": api_key,
    }
    r = base.get(session, url, headers=headers, timeout=20)
    try:
        data = json.loads(r.text)
    except json.JSONDecodeError:
        raise base.BackendError("Brave: invalid JSON")
    return (
        (data.get("web", {}) or {}).get("results", [])
        or data.get("results", [])
        or []
    )


def fetch(query: str, after: str, before: str) -> list[dict]:
    freshness = _freshness(after, before)
    session = base.build_session()
    seen = set()
    out: list[dict] = []
    for offset in range(MAX_OFFSET + 1):
        try:
            results = _call(session, query, freshness, offset)
        except base.BackendError as e:
            # 422 = bad query; treat as empty rather than retry
            if "422" in str(e):
                return out
            raise
        if not results:
            break
        new_count = 0
        for r in results:
            url = (r.get("url") or "").strip()
            title = (r.get("title") or "").strip()
            if not url or not title or url in seen:
                continue
            seen.add(url)
            meta = r.get("meta_url") or {}
            source = (meta.get("hostname") or "").strip() if isinstance(meta, dict) else ""
            out.append({
                "url": url,
                "title": title,
                "description": (r.get("description") or "").strip() or None,
                "sapo": None,
                "published_at": _parse_age(r.get("age", ""), r.get("page_age", "")),
                "source": source or None,
            })
            new_count += 1
        if new_count == 0:
            break
        time.sleep(PAGE_DELAY)
    return out
