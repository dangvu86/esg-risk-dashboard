"""One-shot: compute title_hash for every existing row and merge duplicates.

Replaces the abandoned `pipeline.redecode_google` approach. Instead of
asking Google to decode every encoded URL (slow + library hangs), we
deduplicate by normalised title + publish date — a local-only operation
that runs against the full database in seconds.

Steps:
  1. UPDATE every article row whose title_hash is NULL with the computed hash.
  2. For each (title_hash, published_at) group with >1 row, pick the oldest
     row as the survivor, merge non-null fields (description / sapo / body /
     source) from the donors, then DELETE the donors. The survivor is reset
     to match_status='pending' so pipeline.match re-emits per_ticker JSON.
  3. Print a summary.

Run:
  python -m pipeline.dedup_titles [--dry-run]
"""

from __future__ import annotations

import argparse
import logging

from core import storage
from core.title import title_hash


log = logging.getLogger("dedup-titles")

_MERGE_COLS = ("description", "sapo", "body", "source")


def _backfill_hashes(conn) -> int:
    rows = conn.execute(
        "SELECT article_id, title FROM articles "
        "WHERE title_hash IS NULL OR title_hash = ''"
    ).fetchall()
    n = 0
    for r in rows:
        h = title_hash(r["title"] or "")
        if h:
            conn.execute(
                "UPDATE articles SET title_hash=? WHERE article_id=?",
                (h, r["article_id"]),
            )
            n += 1
    return n


def _merge_into_survivor(conn, survivor_id: str, donor_id: str) -> None:
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
    if don["body_status"] == "fetched" and surv["body_status"] != "fetched":
        updates["body_status"] = "fetched"
    updates["match_status"] = "pending"
    updates["matched_at"] = None
    if updates:
        cols_sql = ", ".join(f"{k}=?" for k in updates)
        conn.execute(
            f"UPDATE articles SET {cols_sql} WHERE article_id=?",
            (*updates.values(), survivor_id),
        )
    conn.execute("DELETE FROM articles WHERE article_id=?", (donor_id,))


def run(dry_run: bool = False) -> dict[str, int]:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s/%(levelname)s] %(message)s",
    )
    storage.init_db()  # ensure title_hash column + index exist
    conn = storage.connect()

    log.info("step 1: backfill title_hash for rows where it is NULL")
    if dry_run:
        n = conn.execute(
            "SELECT COUNT(*) FROM articles "
            "WHERE title_hash IS NULL OR title_hash = ''"
        ).fetchone()[0]
        log.info("DRY: would compute %d hashes", n)
        hashed = 0
    else:
        hashed = _backfill_hashes(conn)
        log.info("hashed %d rows", hashed)

    log.info("step 2: find duplicate groups")
    dup_groups = conn.execute(
        "SELECT title_hash, published_at, COUNT(*) AS n, "
        "  GROUP_CONCAT(article_id) AS ids "
        "FROM articles "
        "WHERE title_hash IS NOT NULL AND title_hash <> '' "
        "  AND published_at IS NOT NULL "
        "GROUP BY title_hash, published_at "
        "HAVING n > 1"
    ).fetchall()
    log.info("found %d duplicate groups", len(dup_groups))

    merged_count = 0
    deleted_count = 0
    for g in dup_groups:
        ids = (g["ids"] or "").split(",")
        if len(ids) < 2:
            continue
        # Pick the oldest row (smallest fetched_at) as survivor
        ordered = conn.execute(
            "SELECT article_id FROM articles WHERE article_id IN (%s) "
            "ORDER BY fetched_at ASC" % ",".join("?" * len(ids)),
            ids,
        ).fetchall()
        if len(ordered) < 2:
            continue
        survivor = ordered[0]["article_id"]
        donors = [r["article_id"] for r in ordered[1:]]
        if dry_run:
            log.info("DRY: survivor=%s donors=%d", survivor[:40], len(donors))
            continue
        for donor in donors:
            _merge_into_survivor(conn, survivor, donor)
            deleted_count += 1
        merged_count += 1

    log.info(
        "done: hashed=%d, dup_groups=%d, merged=%d, donors_deleted=%d",
        hashed, len(dup_groups), merged_count, deleted_count,
    )
    conn.close()
    return {
        "hashed": hashed,
        "dup_groups": len(dup_groups),
        "merged": merged_count,
        "deleted": deleted_count,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
