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
from core.events import cluster_events, same_event, token_df
from body_fetcher.body_clean import editorial_body
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


def _inherit_from_clusters(conn, events) -> tuple[int, list[dict]]:
    """Cluster-skip (event-clustering spec, Component 3): a pending article in
    the same event cluster as an already-judged member inherits that verdict
    without an LLM call. Returns (inherited_count, remaining_events).

    Cluster pool per ticker = the ticker's per_ticker/*.json articles (all
    matched, any enrich state) — written by pipeline.match, which always runs
    before enrich in the job pipeline."""
    by_ticker: dict[str, list[dict]] = {}
    remaining: list[dict] = []
    for e in events:
        if e.get("ticker"):
            by_ticker.setdefault(e["ticker"].upper(), []).append(e)
        else:
            remaining.append(e)        # no ticker → cannot cluster, judge normally

    inherited = 0
    for ticker, evs in by_ticker.items():
        pool: list[dict] = []
        seen_ids: set[str] = set()
        try:
            doc = json.loads((settings.PER_TICKER_DIR / f"{ticker}.json")
                             .read_text(encoding="utf-8"))
            for a in doc.get("articles") or []:
                if a.get("article_id") in seen_ids:
                    continue
                seen_ids.add(a.get("article_id"))
                pool.append({"article_id": a.get("article_id"), "ticker": ticker,
                             "title": a.get("title"),
                             "published_at": a.get("published_at")})
        except (OSError, json.JSONDecodeError):
            pass
        for e in evs:                  # pending rows missing from the doc still cluster
            if e["article_id"] not in seen_ids:
                seen_ids.add(e["article_id"])
                pool.append({"article_id": e["article_id"], "ticker": ticker,
                             "title": e["row"]["title"],
                             "published_at": e["row"]["published_at"]})

        verdicts: dict[str, dict] = {}     # judged members of this ticker
        ids = sorted(seen_ids)
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            for r in conn.execute(
                "SELECT article_id, enrich_status, sentiment, summary_en, "
                "controversy_level, controversy_justification, "
                "controversy_classified_at FROM articles "
                f"WHERE article_id IN ({','.join('?' * len(chunk))}) "
                "AND enrich_status IN ('done','dropped')", chunk):
                verdicts[r["article_id"]] = {k: r[k] for k in r.keys()}

        pending = {e["article_id"]: e for e in evs}
        id2title = {p["article_id"]: p.get("title") or "" for p in pool}
        df, npool = token_df(id2title.values())
        for cluster in cluster_events(pool):
            judged_ids = [m["article_id"] for m in cluster
                          if m["article_id"] in verdicts]
            judged = [verdicts[i] for i in judged_ids]
            if not judged:
                for m in cluster:
                    if m["article_id"] in pending:
                        remaining.append(pending[m["article_id"]])
                continue
            src = judged[0]            # earliest judged member (cluster earliest-first)
            # controversy source: any judged member carrying the fields (the
            # earliest may predate the controversy stage / be Trung bình)
            ctrl = next((j for j in judged if j.get("controversy_level")), None)
            for m in cluster:
                e = pending.get(m["article_id"])
                if e is None:
                    continue
                # Inherit guard: cluster_events can over-merge via a single
                # common word (e.g. "viên" bridging "nhân viên Vietcombank
                # lừa đảo" with "động viên bố … ghép phổi"). Only a member that
                # is *genuinely* the same event as a judged article may inherit;
                # a transitively-attached outsider is sent to the LLM path so it
                # is judged on its own merits instead of inheriting a wrong
                # verdict.
                if not any(same_event(id2title.get(m["article_id"], ""),
                                      id2title.get(jid, ""), df, npool)
                           for jid in judged_ids):
                    remaining.append(e)
                    continue
                if src["sentiment"] == "risk":
                    if ctrl is None:
                        # No cluster member carries a controversy verdict yet:
                        # inheriting would freeze this row at empty forever
                        # (controversy is now classified for ALL severities, and
                        # the web export reads controversy off the rep's enrich
                        # row). Send it through the normal LLM path below instead.
                        remaining.append(e)
                        continue
                    storage.mark_enriched(
                        conn, e["article_id"], sentiment="risk",
                        summary_en=src["summary_en"],
                        controversy_level=(ctrl or {}).get("controversy_level"),
                        controversy_justification=(ctrl or {}).get("controversy_justification"),
                        controversy_classified_at=(ctrl or {}).get("controversy_classified_at"))
                else:
                    storage.mark_dropped(conn, e["article_id"])
                inherited += 1
    return inherited, remaining


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
        # Cluster-skip BEFORE the LLM gate: same-event members inherit the
        # already-judged verdict, so each event costs one LLM call total.
        inherited, events = _inherit_from_clusters(conn, events)
        if inherited:
            log.info("inherited %d verdicts from same-event clusters (no LLM)", inherited)
        if not events:
            return len(rows)
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
            # Controversy is classified for EVERY severity (previously Cao-only).
            # Trung bình events resolve to Minor/No far more often than Major, but
            # the level still carries signal — and a "No" verdict flags incidental
            # matches that should be filtered out.
            event = {"ticker": e["ticker"], "company": _company_for(e["ticker"]),
                     "type": e["type"], "date": (r["published_at"] or "")[:10],
                     "summary": e["summary"], "summary_en": en, "source": r["source"] or ""}
            res = controversy.classify_event(event, provider, today,
                                             body=editorial_body(r["body"]),
                                             revenues=revenues)
            if res:
                level, just, classified_at = res["level"], res["justification"], now_iso
            else:
                # classify failed (LLM error / invalid output). Marking the
                # row done here would freeze controversy at empty forever —
                # leave it pending so the next run retries (costs one extra
                # sentiment+translate pass for this row, acceptable).
                log.warning("controversy classify failed for %s — left pending for retry",
                            e["article_id"])
                continue
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
