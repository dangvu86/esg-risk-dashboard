"""clean_bodies backfill (Fix C). Run: python -m tests.test_clean_bodies"""
from __future__ import annotations
import sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_backfill_cleans_once_and_is_idempotent():
    from core import storage
    from pipeline import clean_bodies
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "c.db"
        storage.init_db(db); conn = storage.connect(db)
        noisy = "Prose về ô nhiễm.\n* [![Image 1: x](https://a/b)"
        storage.insert_article(conn, {"article_id": "d::1", "url_canonical": "u",
            "url_original": "u", "domain": "d", "title": "t", "backend": "google_rss",
            "group_key": "kw", "sub_query_ix": 0})
        storage.mark_body(conn, "d::1", "fetched", noisy)
        # a skipped row must NOT be touched
        storage.insert_article(conn, {"article_id": "d::2", "url_canonical": "u2",
            "url_original": "u2", "domain": "d", "title": "t2", "backend": "google_rss",
            "group_key": "kw", "sub_query_ix": 0})
        storage.mark_body(conn, "d::2", "skipped")
        conn.close()

        r1 = clean_bodies.run(db_path=db)
        assert r1["skipped"] is False and r1["cleaned"] == 1, r1

        conn = storage.connect(db)
        body = conn.execute("SELECT body FROM articles WHERE article_id='d::1'").fetchone()["body"]
        assert "Image 1" not in body and "ô nhiễm" in body
        conn.close()

        # idempotent: second run is gated off
        r2 = clean_bodies.run(db_path=db)
        assert r2["skipped"] is True, r2
    print("  backfill_cleans_once_and_is_idempotent OK")


def main():
    if sys.platform == "win32":
        try: sys.stdout.reconfigure(encoding="utf-8")
        except Exception: pass
    print("running clean_bodies test…")
    test_backfill_cleans_once_and_is_idempotent()
    print("ALL OK")


if __name__ == "__main__":
    main()
