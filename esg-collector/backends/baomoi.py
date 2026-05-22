"""BaoMoi backend — uses the same HMAC-signed w-api as `experiments/vn_scrape/baomoi.py`.

BaoMoi's API doesn't support date filters; we paginate until items fall
below `after`. Each chunk is short (1 month) so we usually stop early.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse
from datetime import datetime

from backends import base


name = "baomoi"

HOST = "https://w-api.baomoi.com"
PATH = "/api/v1/content/get/list-by-custom"
API_KEY = "kI44ARvPwaqL7v0KuDSM0rGORtdY1nnw"
SECRET = b"882QcNXV4tUZbvAsjmFOHqNC1LpcBRKW"
SIG_WHITELIST = {"ctime", "listType", "page", "version"}
VERSION = "0.8.4"

MAX_PAGES = 50  # chunk-internal page cap; usually we stop sooner via date cutoff


def _sign(params: dict) -> str:
    filtered = {k: v for k, v in params.items() if k in SIG_WHITELIST}
    parts = []
    for k in sorted(filtered.keys()):
        parts.append(
            f"{urllib.parse.quote(str(k), safe='')}="
            f"{urllib.parse.quote(str(filtered[k]), safe='')}"
        )
    msg = PATH + "".join(parts)
    return hmac.new(SECRET, msg.encode(), hashlib.sha256).hexdigest()


def _fetch_page(session, keyword: str, page: int) -> list[dict] | None:
    ctime = int(time.time())
    params = {
        "listType": "search",
        "keyword": keyword,
        "page": page,
        "ctime": ctime,
        "version": VERSION,
    }
    params["sig"] = _sign(params)
    params["apiKey"] = API_KEY
    url = HOST + PATH + "?" + urllib.parse.urlencode(params)
    r = base.get(session, url, timeout=20)
    try:
        data = json.loads(r.text)
    except json.JSONDecodeError:
        raise base.BackendError("BaoMoi: invalid JSON")
    if data.get("err") != 0:
        # err != 0 with valid HTTP is a logical error, not rate-limit
        raise base.BackendError(f"BaoMoi err: {data.get('msg')}")
    d = data.get("data") or {}
    return d.get("contents") or d.get("items") or []


def _to_item(raw: dict) -> dict | None:
    title = (raw.get("title") or "").strip()
    url_path = raw.get("url") or raw.get("redirectUrl") or ""
    if not title or not url_path:
        return None
    url = f"https://baomoi.com{url_path}" if url_path.startswith("/") else url_path
    ts = int(raw.get("date") or 0)
    publisher = raw.get("publisher") or {}
    source = publisher.get("name") or publisher.get("hostName") or "baomoi.com"
    published = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d") if ts else None
    sapo = (raw.get("sapo") or "").strip() or None
    desc = (raw.get("description") or "").strip() or None
    return {
        "url": url,
        "title": title,
        "description": desc,
        "sapo": sapo,
        "published_at": published,
        "source": source,
        "_ts": ts,
    }


def fetch(query: str, after: str, before: str) -> list[dict]:
    after_ts = int(datetime.fromisoformat(after).timestamp())
    before_ts = int(datetime.fromisoformat(before).timestamp()) + 86400  # inclusive
    session = base.build_session()
    out: list[dict] = []
    seen = set()
    for page in range(1, MAX_PAGES + 1):
        raw_list = _fetch_page(session, query, page)
        if not raw_list:
            break
        page_items = [it for it in (_to_item(r) for r in raw_list) if it]
        if not page_items:
            break
        new_items = [i for i in page_items if i["url"] not in seen]
        for i in new_items:
            seen.add(i["url"])
        if not new_items:
            break
        oldest_ts = min((it.get("_ts", 0) or 0 for it in page_items if it.get("_ts")), default=0)
        for it in new_items:
            ts = it.get("_ts", 0)
            if ts and (ts < after_ts or ts > before_ts):
                continue
            it_clean = {k: v for k, v in it.items() if k != "_ts"}
            out.append(it_clean)
        if oldest_ts and oldest_ts < after_ts:
            break
        time.sleep(0.3)  # small per-page courtesy delay
    return out
