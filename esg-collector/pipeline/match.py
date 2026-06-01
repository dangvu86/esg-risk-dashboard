"""Tier 1 RAW → Tier 2 CLASSIFIED.

Scans `articles` with match_status='pending', applies alias_matcher, and
writes per-ticker JSON files at `data/per_ticker/<TICKER>.json`.

Two-stage match per article:
  1. Title + description + sapo. Match found → done (saves Jina calls).
     If the body fetcher already pre-checked this article and cached its
     hits, we trust the cache and skip re-running the regex pool.
  2. If nothing yet and body_status='fetched' → also match body.
If still nothing and body is fetched/skipped/failed (terminal) → set
match_status='unmatched'. If body is still 'pending', leave article in
'pending' so the body fetcher can pick it up.

Run:  python -m pipeline.match [--since 2024-06-01]
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from config import settings
from core import alias_matcher, storage
from core.alias_matcher import AliasHit
from pipeline import esg_filter


log = logging.getLogger("match")

BATCH_SIZE = 2000  # rows held in RAM per pagination batch (tunable)

_BODY_TERMINAL = {"fetched", "skipped", "failed"}


def _per_ticker_path(ticker: str) -> Path:
    return settings.PER_TICKER_DIR / f"{ticker}.json"


def _load_existing(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"ticker": path.stem, "generated_at": "", "articles": []}


def _save(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_cached_hits(raw: str | None) -> list[AliasHit] | None:
    """Rehydrate the JSON written by body_fetcher.cache_hits. None if absent
    or malformed — caller will fall back to running alias_matcher live."""
    if not raw:
        return None
    try:
        items = json.loads(raw)
    except (TypeError, ValueError):
        return None
    out: list[AliasHit] = []
    for it in items:
        try:
            out.append(AliasHit(
                ticker=it["ticker"], alias=it["alias"],
                location=it.get("location", ""), weight=float(it.get("weight", 1.0)),
            ))
        except (KeyError, TypeError):
            return None
    return out


def _snippet(art: dict) -> str:
    for f in ("sapo", "description", "body"):
        v = (art.get(f) or "").strip()
        if v:
            return v[:300]
    return ""


def _process_article(conn, art, per_ticker, counts):
    art_d = dict(art)
    # Stage 1: title/desc/sapo — reuse body_fetcher's cached hits if any.
    hits = _load_cached_hits(art_d.get("cached_hits"))
    if hits is None:
        hits = alias_matcher.match_article(
            art_d, fields=("title", "description", "sapo"),
        )
    if not hits and art["body_status"] == "fetched":
        hits = alias_matcher.match_article(
            art_d, fields=("title", "description", "sapo", "body"),
        )
    # ESG verdict layered on top of alias attribution, using the same
    # title/desc/sapo/body dict the matcher saw.
    verdict = esg_filter.classify(art_d)
    if hits and verdict.keep:
        storage.mark_match(conn, art["article_id"], "matched")
        storage.mark_esg(conn, art["article_id"], "esg",
                         verdict.esg_type, verdict.severity)
        counts["matched"] += 1
        for hit in hits:
            doc = per_ticker.get(hit.ticker)
            if doc is None:
                doc = _load_existing(_per_ticker_path(hit.ticker))
                per_ticker[hit.ticker] = doc
            # dedup by article_id
            if any(a["article_id"] == art["article_id"] for a in doc["articles"]):
                continue
            doc["articles"].append({
                "article_id":     art["article_id"],
                "url":            art["url_canonical"],
                "url_original":   art["url_original"],
                "title":          art["title"],
                "published_at":   art["published_at"],
                "source":         art["source"],
                "backend":        art["backend"],
                "group_key":      art["group_key"],
                "matched_alias":  hit.alias,
                # hit.location is the matched FIELD name (title|description|
                # sapo|body), not geography. Kept under both keys for
                # backward compat with existing dashboard consumers.
                "location":       hit.location,
                "match_source":   hit.location,
                "snippet":        _snippet(art_d),
                "type":           verdict.esg_type,
                "severity":       verdict.severity,
            })
    elif art["body_status"] in _BODY_TERMINAL:
        # Terminal body: either no ticker, or ticker but ESG filter rejected
        # it. Either way the article is finished — drop it from coverage.
        storage.mark_match(conn, art["article_id"], "unmatched")
        storage.mark_esg(conn, art["article_id"], verdict.reason, None, None)
        counts["unmatched"] += 1
    else:
        # body still pending — leave match_status AND esg_status pending so
        # the body fetcher can fill the body and the next cycle re-runs.
        counts["deferred"] += 1


def run(
    since: str | None = None,
    limit: int | None = None,
    *,
    rematch_all: bool = False,
    db_path: str | None = None,
    status_json: str | None = None,
) -> dict[str, int]:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s/%(levelname)s] %(message)s",
    )
    alias_matcher.reload()
    log.info("loaded aliases for %d tickers", len(alias_matcher.loaded_tickers()))

    conn = storage.connect(db_path)
    if rematch_all:
        n = conn.execute(
            "UPDATE articles SET match_status='pending', matched_at=NULL, "
            "esg_status='pending', esg_type=NULL, severity=NULL"
        ).rowcount
        log.info("rematch_all: reset %d articles to pending", n)
        # Per-ticker JSONs are also stale — wipe so they rebuild cleanly.
        for p in settings.PER_TICKER_DIR.glob("*.json"):
            p.unlink()
    # snapshot the work list FIRST (autocommit + we mutate match_status below,
    # so we must not iterate a live SELECT on that column)
    sql = "SELECT article_id FROM articles WHERE match_status='pending'"
    args: list = []
    if since:
        sql += " AND published_at >= ?"; args.append(since)
    sql += " ORDER BY published_at DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    ids = [r["article_id"] for r in conn.execute(sql, args)]
    log.info("scanning %d pending articles in batches of %d", len(ids), BATCH_SIZE)

    per_ticker: dict[str, dict] = {}
    counts = {"matched": 0, "unmatched": 0, "deferred": 0}

    for i in range(0, len(ids), BATCH_SIZE):
        batch = storage.fetch_articles_by_ids(conn, ids[i:i + BATCH_SIZE])
        for art in batch:
            _process_article(conn, art, per_ticker, counts)

    for ticker, doc in per_ticker.items():
        doc["articles"].sort(key=lambda a: (a.get("published_at") or ""), reverse=True)
        _save(_per_ticker_path(ticker), doc)
        log.info("  %s: %d articles total", ticker, len(doc["articles"]))

    log.info("done: matched=%d unmatched=%d deferred=%d",
             counts["matched"], counts["unmatched"], counts["deferred"])
    if status_json:
        sp = Path(status_json)
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps(counts), encoding="utf-8")
    conn.close()
    return counts


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="ISO date — only match articles published on/after")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--rematch-all", action="store_true",
                    help="reset match_status='pending' for every article and wipe per_ticker/ first "
                         "(use after fixing aliases)")
    ap.add_argument("--status-json", help="write final {matched,unmatched,deferred} counts to this path")
    args = ap.parse_args()
    run(since=args.since, limit=args.limit, rematch_all=args.rematch_all,
        status_json=args.status_json)


if __name__ == "__main__":
    main()
