"""One-shot: strip related-news link/image blocks from already-stored bodies.

Idempotent via an export_state flag (re-runs are no-ops unless --force). No
re-fetch — reads/rewrites `articles.body` in place. The body fetcher already
cleans NEW bodies (workers/body_fetcher); this fixes the pre-existing ones.

Run:  python -m pipeline.clean_bodies [--force]
"""
from __future__ import annotations

import argparse
import logging

from core import storage
from body_fetcher.body_clean import strip_related_blocks

log = logging.getLogger("clean_bodies")
FLAG = "bodies_cleaned_v1"


def run(db_path=None, *, force: bool = False) -> dict:
    storage.init_db(db_path)
    conn = storage.connect(db_path)
    try:
        if not force and storage.get_meta(conn, FLAG):
            return {"skipped": True, "cleaned": 0, "scanned": 0}
        cleaned = scanned = 0
        rows = list(storage.iter_articles(conn, body_status="fetched"))
        for r in rows:
            scanned += 1
            body = r["body"] or ""
            new = strip_related_blocks(body)
            if new != body:
                storage.mark_body(conn, r["article_id"], "fetched", new)
                cleaned += 1
        storage.set_meta(conn, FLAG, "done")
        return {"skipped": False, "cleaned": cleaned, "scanned": scanned}
    finally:
        conn.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s/%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="ignore the done-flag")
    args = ap.parse_args()
    result = run(force=args.force)
    log.info("clean_bodies: %s", result)


if __name__ == "__main__":
    main()
