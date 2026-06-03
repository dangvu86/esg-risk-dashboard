"""Enrich stage: drain a bounded chunk of kept-but-unenriched articles through
sentiment → title translation → controversy, writing results back to the DB.

Idempotent and OOM-safe: only `esg_status='esg' AND enrich_status='pending'`
rows are processed, `limit` bounds the chunk, and any failure leaves the row
`pending` for the next run. Run as a oneshot:  python -m enrich.runner
"""
from __future__ import annotations
import argparse
import json
import logging
from datetime import datetime, timezone

from core import storage, alias_matcher
from config import settings
from enrich import sentiment, translate, controversy
from enrich.llm import resolve_provider
from enrich.revenue import load_revenues

log = logging.getLogger("enrich")
DEFAULT_LIMIT = 25


def _company_for(ticker: str) -> str:
    """Canonical company name for the prompt — from the alias file, fallback ''."""
    p = settings.ALIASES_DIR / f"{ticker}.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("company_name", "") or ""
    except Exception:
        return ""


def _primary_ticker(row) -> str | None:
    """Resolve the article's matched ticker (primary = first alias hit)."""
    hits = alias_matcher.match_article({
        "title": row["title"] or "", "description": row["description"] or "",
        "sapo": row["sapo"] or "", "body": row["body"] or "",
    })
    return hits[0].ticker if hits else (row["ticker_hint"] or None)


def run(limit: int = DEFAULT_LIMIT, db_path=None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s/%(levelname)s] %(message)s")
    provider = resolve_provider()
    if not provider:
        log.warning("no LLM provider configured (set GROQ_API_KEY) — skipping enrich")
        return 0
    conn = storage.connect(db_path)
    try:
        rows = storage.get_pending_enrich(conn, limit=limit)
        if not rows:
            log.info("no pending articles to enrich")
            return 0
        log.info("enriching %d articles (provider=%s)", len(rows), provider["name"])

        # 1. sentiment gate (batch). Build minimal event dicts.
        events = [{"article_id": r["article_id"], "ticker": _primary_ticker(r) or "",
                   "type": r["esg_type"], "severity": r["severity"],
                   "summary": r["title"] or "", "row": r} for r in rows]
        kept = sentiment.filter_negative(events, provider=provider)
        kept_ids = {e["article_id"] for e in kept}
        for e in events:
            if e["article_id"] not in kept_ids:
                storage.mark_dropped(conn, e["article_id"])

        if not kept:
            return len(rows)

        # 2. translate titles (batch, order-preserving)
        titles = [e["summary"] for e in kept]
        titles_en = translate.translate_titles(titles, provider=provider)
        if len(titles_en) != len(kept):
            # translate_titles contracts to preserve length; if it ever doesn't,
            # fall back to the VN titles (same length as `kept`) rather than aborting.
            # This keeps every kept row progressing to `done` (un-translated) instead
            # of stranding the whole chunk `pending` and re-burning sentiment quota on
            # it every run — consistent with the stage's own VN-fallback policy.
            log.warning("translate_titles returned %d items for %d inputs — using VN titles",
                        len(titles_en), len(kept))
            titles_en = titles

        # 3. controversy for Cao only; write back per article
        revenues = load_revenues()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for e, en in zip(kept, titles_en):
            r = e["row"]
            level = just = classified_at = None
            if r["severity"] == "Cao":
                event = {"ticker": e["ticker"], "company": _company_for(e["ticker"]),
                         "type": e["type"], "date": (r["published_at"] or "")[:10],
                         "summary": e["summary"], "summary_en": en, "source": r["source"] or ""}
                res = controversy.classify_event(event, provider, today,
                                                 body=r["body"], revenues=revenues)
                if res:
                    level, just, classified_at = res["level"], res["justification"], now_iso
            storage.mark_enriched(conn, e["article_id"], sentiment="risk", summary_en=en,
                                  controversy_level=level, controversy_justification=just,
                                  controversy_classified_at=classified_at)
        return len(rows)
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    args = ap.parse_args()
    run(limit=args.limit)


if __name__ == "__main__":
    main()
