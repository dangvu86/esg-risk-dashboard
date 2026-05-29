"""Smoke tests — no network. Run with:  python -m tests.test_smoke

Verifies the core glue (canonicalize / storage / alias_matcher / queue_builder)
without any HTTP calls.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Make `import config.*` etc work when running this file directly.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_canonicalize() -> None:
    from core import canonicalize as c
    cases = [
        ("http://www.vnexpress.net/abc-4612345.html?utm_source=fb",
         "vnexpress.net::4612345"),
        ("https://m.tuoitre.vn/foo-20241231123.htm",
         "tuoitre.vn::20241231123"),
        ("https://cafef.vn/news-20231231000000.chn",
         "cafef.vn::20231231000000"),
        ("https://baomoi.com/title-c12345.epi",
         "baomoi.com::12345"),
    ]
    for url, want in cases:
        got = c.article_id(url)
        assert got == want, f"article_id({url!r}) = {got!r}, want {want!r}"
    assert c.canonicalize("HTTPS://WWW.VnExpress.net/x/").startswith("https://vnexpress.net/")
    assert c.dedup_key("https://no-domain-id.example.com/foo") == c.canonicalize(
        "https://no-domain-id.example.com/foo"
    )
    print("  canonicalize OK")


def test_alias_matcher() -> None:
    from core import alias_matcher
    alias_matcher.reload()
    assert "DBC" in alias_matcher.loaded_tickers(), "DBC alias not loaded"
    hits = alias_matcher.match_text("Dabaco bị phạt vì xả thải ra môi trường")
    tickers = [h.ticker for h in hits]
    assert "DBC" in tickers, f"expected DBC in {tickers}"
    # subsidiary should also hit
    hits2 = alias_matcher.match_text("Nasaco vừa thông báo...")
    assert "DBC" in [h.ticker for h in hits2]
    # negative
    hits3 = alias_matcher.match_text("Đội tuyển Việt Nam thắng 3-0")
    assert "DBC" not in [h.ticker for h in hits3]
    print("  alias_matcher OK")


def test_storage_roundtrip() -> None:
    from core import storage
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "t.db"
        storage.init_db(db)
        conn = storage.connect(db)
        rec = {
            "article_id": "test.vn::123",
            "url_canonical": "https://test.vn/a-123.html",
            "url_original": "https://test.vn/a-123.html",
            "domain": "test.vn",
            "title": "Hello",
            "description": "snip",
            "backend": "google_rss",
            "group_key": "E",
            "sub_query_ix": 0,
        }
        assert storage.insert_article(conn, rec) is True
        # second insert: same id, with body → should merge body
        rec2 = dict(rec, body="long body text", sapo="sapo here")
        assert storage.insert_article(conn, rec2) is False
        row = conn.execute("SELECT body, sapo FROM articles WHERE article_id=?",
                           (rec["article_id"],)).fetchone()
        assert row["body"] == "long body text"
        assert row["sapo"] == "sapo here"

        # cached_hits round-trip
        storage.cache_hits(conn, rec["article_id"], '[{"ticker":"DBC","alias":"Dabaco","location":"title","weight":1.0}]')
        row = conn.execute(
            "SELECT cached_hits FROM articles WHERE article_id=?", (rec["article_id"],)
        ).fetchone()
        assert row["cached_hits"] and "DBC" in row["cached_hits"]

        # enqueue + atomic claim + done
        storage.enqueue_task(
            conn, backend="google_rss", group_key="E", sub_query_ix=0,
            query="ô nhiễm", after="2024-06-01", before="2024-06-30",
        )
        t = storage.next_task(conn, "google_rss")
        assert t is not None and t["query"] == "ô nhiễm"
        # second worker calling next_task immediately must NOT get the same
        # task — the atomic claim should have moved it to 'in_progress'
        # with next_attempt pushed into the future.
        t_dup = storage.next_task(conn, "google_rss")
        assert t_dup is None, f"race: second claim returned {t_dup['task_id']}"
        storage.mark_task_done(conn, t["task_id"], 5)
        t2 = storage.next_task(conn, "google_rss")
        assert t2 is None

        # timestamp format sanity — defaults should use ISO 'T' with 'Z'
        ts = conn.execute("SELECT fetched_at FROM articles LIMIT 1").fetchone()["fetched_at"]
        assert "T" in ts and ts.endswith("Z"), f"unexpected fetched_at format: {ts!r}"

        # meta round-trip (used by incremental export)
        storage.set_meta(conn, "last_ndjson_export_at", "2026-01-01T00:00:00Z")
        assert storage.get_meta(conn, "last_ndjson_export_at") == "2026-01-01T00:00:00Z"
        storage.set_meta(conn, "last_ndjson_export_at", "2026-01-02T00:00:00Z")
        assert storage.get_meta(conn, "last_ndjson_export_at") == "2026-01-02T00:00:00Z"
        conn.close()
    print("  storage OK")


def test_schema_migrations() -> None:
    from core import storage
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "m.db"
        storage.init_db(db)
        storage.init_db(db)  # idempotent — must not raise
        conn = storage.connect(db)
        qcols = {r["name"] for r in conn.execute("PRAGMA table_info(search_queue)")}
        acols = {r["name"] for r in conn.execute("PRAGMA table_info(articles)")}
        assert {"kind", "ticker"} <= qcols, qcols
        assert {"ticker_hint", "esg_status", "esg_type", "severity"} <= acols, acols
        conn.close()
    print("  schema_migrations OK")


def test_enqueue_kinds() -> None:
    from core import storage
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "q.db"
        storage.init_db(db)
        conn = storage.connect(db)
        assert storage.enqueue_task(conn, backend="baomoi", group_key="kw",
            sub_query_ix=3, query="ô nhiễm", after="2024-06-01", before="2024-06-30")
        assert "baomoi:kw:3:2024-06-01" in {r["task_id"] for r in conn.execute("SELECT task_id FROM search_queue")}
        assert storage.enqueue_task(conn, backend="baomoi", kind="alias",
            ticker="DBC", group_key="alias", sub_query_ix=0, query="Dabaco",
            after="2020-01-01", before="2026-05-29")
        rows = {r["task_id"]: r for r in conn.execute("SELECT * FROM search_queue")}
        assert any(r["kind"] == "alias" and r["ticker"] == "DBC" for r in rows.values())
        # alias re-enqueue is idempotent
        assert storage.enqueue_task(conn, backend="baomoi", kind="alias",
            ticker="DBC", group_key="alias", sub_query_ix=0, query="Dabaco",
            after="2020-01-01", before="2026-05-29") is False
        kw_row = conn.execute("SELECT * FROM search_queue WHERE task_id='baomoi:kw:3:2024-06-01'").fetchone()
        assert kw_row["ticker"] is None and kw_row["kind"] == "keyword"
        # guard: alias kind without ticker must raise
        try:
            storage.enqueue_task(conn, backend="baomoi", kind="alias", ticker=None,
                group_key="alias", sub_query_ix=9, query="x", after="2020-01-01", before="2020-01-31")
            raise AssertionError("expected ValueError for alias without ticker")
        except ValueError:
            pass
        conn.close()
    print("  enqueue_kinds OK")


def test_queue_builder_counts() -> None:
    from config.keywords import count_subqueries
    from core import queue_builder
    # 24 sub-queries × monthly chunks per window
    expected_per_chunk = count_subqueries()
    chunks = list(queue_builder.date_chunks("2024-06-01", "2024-06-30", 1))
    assert chunks == [("2024-06-01", "2024-06-30")], chunks
    chunks2 = list(queue_builder.date_chunks("2024-01-01", "2024-03-31", 1))
    assert len(chunks2) == 3
    assert expected_per_chunk == 24, f"expected 24 sub-queries, got {expected_per_chunk}"
    print("  queue_builder OK")


def test_window_reaches_today() -> None:
    from datetime import date
    from config import settings
    # BACKFILL_END and BAOMOI_WINDOW_END roll to today; BRAVE_WINDOW_END stays at 2021-12-31
    for end in (settings.BACKFILL_END, settings.BAOMOI_WINDOW_END):
        assert end >= "2025-01-01", f"window end {end} predates 2025"
    assert settings.BRAVE_WINDOW_END == "2021-12-31", (
        f"Brave window end should stay 2021-12-31 (pre-BaoMoi tail), got {settings.BRAVE_WINDOW_END}"
    )
    assert settings.BACKFILL_END >= date.today().isoformat()[:7], "backfill end not rolling to current month"
    print("  window_reaches_today OK")


def main() -> None:
    if sys.platform == "win32":
        # ensure stdout can print Vietnamese
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    print("running smoke tests…")
    test_canonicalize()
    test_alias_matcher()
    test_storage_roundtrip()
    test_schema_migrations()
    test_enqueue_kinds()
    test_queue_builder_counts()
    test_window_reaches_today()
    print("ALL OK")


if __name__ == "__main__":
    main()
