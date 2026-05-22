"""Google News RSS backend.

We do NOT prepend `intitle:"<name>"` — this backend searches ESG keywords
across the whole VN news index and the downstream alias matcher picks up
hits per ticker.
"""

from __future__ import annotations

import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime

from backends import base


name = "google_rss"


def _build_url(query: str, after: str, before: str) -> str:
    q = f"{query} after:{after} before:{before}"
    params = urllib.parse.urlencode({
        "q": q,
        "hl": "vi",
        "gl": "VN",
        "ceid": "VN:vi",
    })
    return f"https://news.google.com/rss/search?{params}"


def _parse_pubdate(raw: str) -> str:
    if not raw:
        return ""
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S GMT", "%a, %d %b %Y %H:%M:%S"):
        try:
            return datetime.strptime(raw.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw[:10]


def fetch(query: str, after: str, before: str) -> list[dict]:
    url = _build_url(query, after, before)
    session = base.build_session()
    r = base.get(session, url, timeout=20)
    items: list[dict] = []
    try:
        root = ET.fromstring(r.text)
    except ET.ParseError:
        return items
    for it in root.findall(".//item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        desc = (it.findtext("description") or "").strip()
        src_el = it.find("source")
        source = (src_el.text.strip() if src_el is not None and src_el.text else "")
        published = _parse_pubdate(it.findtext("pubDate") or "")
        if not title or not link:
            continue
        items.append({
            "url": link,
            "title": title,
            "description": desc or None,
            "sapo": None,
            "published_at": published or None,
            "source": source or None,
        })
    return items
