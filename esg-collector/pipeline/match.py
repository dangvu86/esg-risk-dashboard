"""Tier 1 RAW → Tier 2 CLASSIFIED.

Scans `articles` with match_status='pending', applies alias_matcher, and
writes per-ticker JSON files at `data/per_ticker/<TICKER>.json`.

Two-stage match per article:
  1. Title + description + sapo. Match found → done (saves Jina calls).
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
from datetime import datetime
from pathlib import Path

from config import settings
from core import alias_matcher, storage


log = logging.getLogger("match")

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
    doc["generated_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


def _snippet(art: dict) -> str:
    for f in ("sapo", "description", "body"):
        v = (art.get(f) or "").strip()
        if v:
            return v[:300]
    return ""


def run(
    since: str | None = None,
    limit: int | None = None,
    *,
    rematch_all: bool = False,
) -> dict[str, int]:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s/%(levelname)s] %(message)s",
    )
    alias_matcher.reload()
    log.info("loaded aliases for %d tickers", len(alias_matcher.loaded_tickers()))

    conn = storage.connect()
    if rematch_all:
        n = conn.execute(
            "UPDATE articles SET match_status='pending', matched_at=NULL"
        ).rowcount
        log.info("rematch_all: reset %d articles to pending", n)
        # Per-ticker JSONs are also stale — wipe so they rebuild cleanly.
        for p in settings.PER_TICKER_DIR.glob("*.json"):
            p.unlink()
    pending = list(storage.iter_articles(
        conn, match_status="pending", since=since, limit=limit,
    ))
    log.info("scanning %d pending articles", len(pending))

    per_ticker: dict[str, dict] = {}
    counts = {"matched": 0, "unmatched": 0, "deferred": 0}

    for art in pending:
        art_d = dict(art)
        # Stage 1: title/desc/sapo
        hits = alias_matcher.match_article(
            art_d, fields=("title", "description", "sapo"),
        )
        if not hits and art["body_status"] == "fetched":
            hits = alias_matcher.match_article(
                art_d, fields=("title", "description", "sapo", "body"),
            )
        if hits:
            storage.mark_match(conn, art["article_id"], "matched")
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
                    "location":       hit.location,
                    "snippet":        _snippet(art_d),
                })
        elif art["body_status"] in _BODY_TERMINAL:
            storage.mark_match(conn, art["article_id"], "unmatched")
            counts["unmatched"] += 1
        else:
            counts["deferred"] += 1  # waiting for body fetch

    for ticker, doc in per_ticker.items():
        doc["articles"].sort(key=lambda a: (a.get("published_at") or ""), reverse=True)
        _save(_per_ticker_path(ticker), doc)
        log.info("  %s: %d articles total", ticker, len(doc["articles"]))

    log.info("done: matched=%d unmatched=%d deferred=%d",
             counts["matched"], counts["unmatched"], counts["deferred"])
    conn.close()
    return counts


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="ISO date — only match articles published on/after")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--rematch-all", action="store_true",
                    help="reset match_status='pending' for every article and wipe per_ticker/ first "
                         "(use after fixing aliases)")
    args = ap.parse_args()
    run(since=args.since, limit=args.limit, rematch_all=args.rematch_all)


if __name__ == "__main__":
    main()
