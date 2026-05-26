"""Retroactive Google News URL decoder + dedup pass.

Before workers/runner.py was patched to resolve Google News encoded links
before generating article_id, every Google RSS row was stored with
article_id = 'news.google.com::<base64-blob>' instead of the real
publisher ID. As a result the SAME story collected by BaoMoi or Brave
appears as a different article. This script repairs that:

  1. For every row with domain='news.google.com', resolve the encoded URL
     to the publisher URL (using core.url_cache so each unique URL hits
     Google at most once across runs).
  2. Recompute article_id / url_canonical / domain from the decoded URL.
  3. If the new article_id collides with an existing row, MERGE the two:
     keep the survivor, fill its NULL fields (sapo / body / description /
     source / published_at) from the donor, then DELETE the donor. The
     survivor is reset to match_status='pending' so pipeline.match picks
     it up again with the now-richer fields.
  4. Otherwise UPDATE the row in place (new id + canonical + domain),
     also resetting match_status to 'pending'.

After this finishes:
    python -m pipeline.match --rematch-all      # rebuild per_ticker JSONs
    python -m pipeline.export  --ndjson --upload

Run:
    python -m pipeline.redecode_google [--limit N] [--dry-run]

Resumable: the SQLite decode cache (url_decode_cache) survives crashes,
so re-running picks up where it left off — only un-cached URLs hit Google.
"""

from __future__ import annotations

import argparse
import logging

from bs4 import BeautifulSoup

from core import storage, url_cache
from core.canonicalize import canonicalize, dedup_key, domain_of


log = logging.getLogger("redecode")


_MERGE_COLS = ("description", "sapo", "body", "source", "published_at")


def _clean_html_descriptions(conn) -> int:
    """Strip HTML tags from `description` for any row that still contains raw
    Google News markup (anchor + font tags). Idempotent: rows already clean
    are left untouched.

    Returns the number of rows updated.
    """
    rows = conn.execute(
        "SELECT article_id, description FROM articles "
        "WHERE description LIKE '%<%' AND description LIKE '%>%'"
    ).fetchall()
    n = 0
    for r in rows:
        raw = r["description"] or ""
        clean = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
        if clean and clean != raw:
            conn.execute(
                "UPDATE articles SET description=? WHERE article_id=?",
                (clean, r["article_id"]),
            )
            n += 1
    return n


def _merge_into_survivor(conn, survivor_id: str, donor_id: str) -> None:
    """Fill survivor's NULL fields from donor, reset to pending, drop donor."""
    surv = conn.execute(
        "SELECT * FROM articles WHERE article_id=?", (survivor_id,)
    ).fetchone()
    don = conn.execute(
        "SELECT * FROM articles WHERE article_id=?", (donor_id,)
    ).fetchone()
    if not surv or not don:
        return
    updates: dict[str, object] = {}
    for col in _MERGE_COLS:
        if not surv[col] and don[col]:
            updates[col] = don[col]
    # If donor had body fetched but survivor didn't, take that status too.
    if don["body_status"] == "fetched" and surv["body_status"] != "fetched":
        updates["body_status"] = "fetched"
    updates["match_status"] = "pending"
    updates["matched_at"] = None
    cols_sql = ", ".join(f"{k}=?" for k in updates)
    conn.execute(
        f"UPDATE articles SET {cols_sql} WHERE article_id=?",
        (*updates.values(), survivor_id),
    )
    conn.execute("DELETE FROM articles WHERE article_id=?", (donor_id,))


def run(limit: int | None = None, dry_run: bool = False) -> dict[str, int]:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s/%(levelname)s] %(message)s",
    )
    conn = storage.connect()
    storage.init_db()  # ensure url_decode_cache table exists

    # Pre-step: scrub raw HTML out of `description` (forward-only fix in
    # google_rss.py doesn't touch already-stored rows).
    if not dry_run:
        cleaned = _clean_html_descriptions(conn)
        log.info("description HTML scrub: %d rows cleaned", cleaned)

    sql = (
        "SELECT article_id, url_original, url_canonical "
        "FROM articles WHERE domain='news.google.com'"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = list(conn.execute(sql))
    log.info("found %d Google News rows to process", len(rows))

    stats = {
        "decode_failed": 0,
        "unchanged": 0,
        "merged":     0,
        "renamed":    0,
        "errors":     0,
    }

    for i, r in enumerate(rows, 1):
        old_id = r["article_id"]
        old_url = r["url_original"] or r["url_canonical"]
        try:
            new_url = url_cache.resolve(conn, old_url)
        except Exception as e:
            log.warning("resolve error on %s: %s", old_id[:60], e)
            stats["errors"] += 1
            continue
        if not new_url or new_url == old_url:
            stats["decode_failed"] += 1
            continue
        new_id = dedup_key(new_url)
        new_canon = canonicalize(new_url)
        new_domain = domain_of(new_url)
        if not new_id or new_id == old_id:
            stats["unchanged"] += 1
            continue
        existing = conn.execute(
            "SELECT 1 FROM articles WHERE article_id=?", (new_id,)
        ).fetchone()
        if dry_run:
            log.info("DRY  %s → %s  (%s)",
                     old_id[:50], new_id, "collide" if existing else "rename")
            continue
        try:
            if existing:
                _merge_into_survivor(conn, new_id, old_id)
                stats["merged"] += 1
            else:
                conn.execute(
                    "UPDATE articles SET article_id=?, url_canonical=?, "
                    "url_original=?, domain=?, match_status='pending', "
                    "matched_at=NULL WHERE article_id=?",
                    (new_id, new_canon, new_url, new_domain, old_id),
                )
                stats["renamed"] += 1
        except Exception as e:
            log.warning("update error %s → %s: %s", old_id[:60], new_id, e)
            stats["errors"] += 1
            continue

        if i % 100 == 0:
            log.info("progress %d/%d  %s", i, len(rows), stats)

    log.info("done: %s", stats)
    conn.close()
    return stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None,
                    help="cap to first N rows (for smoke-test)")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would change without writing")
    args = ap.parse_args()
    run(limit=args.limit, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
