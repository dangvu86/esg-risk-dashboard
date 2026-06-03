"""Enrich-stage tests — no network (LLM call is monkeypatched). Run:
    python -m tests.test_enrich
"""
from __future__ import annotations
import sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_enrich_columns_and_queries() -> None:
    from core import storage
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "e.db"
        storage.init_db(db)
        storage.init_db(db)  # idempotent
        conn = storage.connect(db)
        acols = {r["name"] for r in conn.execute("PRAGMA table_info(articles)")}
        assert {"summary_en", "sentiment", "controversy_level",
                "controversy_justification", "controversy_classified_at",
                "enrich_status"} <= acols, acols
        # an esg-kept, pending row is returned; a non-esg row is not
        conn.execute("INSERT INTO articles (article_id,url_canonical,title,esg_status) "
                     "VALUES ('a::1','u1','Phat cong ty vi xa thai','esg')")
        conn.execute("INSERT INTO articles (article_id,url_canonical,title,esg_status) "
                     "VALUES ('a::2','u2','khong esg','noise')")
        # the DEFAULT must backfill enrich_status='pending' on a freshly-inserted row
        row0 = conn.execute("SELECT enrich_status FROM articles WHERE article_id='a::1'").fetchone()
        assert row0["enrich_status"] == "pending", f"DEFAULT not applied: {row0['enrich_status']!r}"
        pend = storage.get_pending_enrich(conn, limit=10)
        ids = {r["article_id"] for r in pend}
        assert ids == {"a::1"}, ids
        # mark_enriched moves it out of pending and stores fields
        storage.mark_enriched(conn, "a::1", sentiment="risk", summary_en="EN",
                              controversy_level="Minor", controversy_justification="x. y.",
                              controversy_classified_at="2026-06-03T00:00:00Z")
        assert not storage.get_pending_enrich(conn, limit=10)
        row = conn.execute("SELECT * FROM articles WHERE article_id='a::1'").fetchone()
        assert row["enrich_status"] == "done" and row["summary_en"] == "EN"
        assert row["sentiment"] == "risk" and row["controversy_level"] == "Minor"
        # mark_dropped path
        conn.execute("INSERT INTO articles (article_id,url_canonical,title,esg_status) "
                     "VALUES ('a::3','u3','t','esg')")
        storage.mark_dropped(conn, "a::3")
        r3 = conn.execute("SELECT enrich_status,sentiment FROM articles WHERE article_id='a::3'").fetchone()
        assert r3["enrich_status"] == "dropped" and r3["sentiment"] == "not_risk"
        conn.close()
    print("  enrich_columns_and_queries OK")


def main() -> None:
    if sys.platform == "win32":
        try: sys.stdout.reconfigure(encoding="utf-8")
        except Exception: pass
    print("running enrich tests…")
    test_enrich_columns_and_queries()
    print("ALL OK")


if __name__ == "__main__":
    main()
