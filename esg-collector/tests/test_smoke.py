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
    assert settings.BAOMOI_WINDOW_END >= date.today().isoformat()[:7], "baomoi window end not rolling to current month"
    print("  window_reaches_today OK")


def test_keyword_config() -> None:
    from config import keywords as kw
    terms = kw.search_terms()
    assert len(terms) == len(set(terms)), "search_terms not deduped"
    assert all(isinstance(t, str) and t for t in terms)
    esg = kw.esg_terms()                      # [(term, type)]
    assert all(t in ("E", "S", "G") for _, t in esg)
    assert "ô nhiễm" in {t for t, _ in esg}
    assert "cổ tức" in set(kw.noise_terms())
    assert any("khởi tố" == t for t in kw.high_severity_terms())
    print("  keyword_config OK")


def test_esg_filter() -> None:
    from pipeline import esg_filter
    v = esg_filter.classify({"title": "Xử phạt Dabaco 300 triệu vì vi phạm môi trường",
                             "sapo": "", "body": ""})
    assert v.keep and v.esg_type == "E" and v.severity == "Trung bình", v
    v = esg_filter.classify({"title": "Cổ đông Dabaco sắp nhận cổ tức bằng tiền mặt",
                             "sapo": "", "body": ""})
    assert not v.keep and v.reason == "noise", v
    v = esg_filter.classify({"title": "Phạt công ty X 2 tỷ đồng vì xả thải", "sapo": "", "body": ""})
    assert v.keep and v.severity == "Cao" and v.esg_type == "E", v
    v = esg_filter.classify({"title": "Công ty X tổ chức đại hội cổ đông thường niên",
                             "sapo": "", "body": ""})
    assert not v.keep and v.reason == "non_esg", v
    v = esg_filter.classify({"title": "Thông báo của công ty",
                             "sapo": "", "body": "Nhà máy bị xử phạt vì xả thải ra môi trường"})
    assert v.keep and v.esg_type == "E", v
    print("  esg_filter OK")


def test_match_esg_integration() -> None:
    from core import storage, alias_matcher
    from pipeline import match
    from config import settings
    import tempfile, json
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "x.db"
        _orig_pt = settings.PER_TICKER_DIR
        try:
            settings.PER_TICKER_DIR = Path(td) / "pt"; settings.PER_TICKER_DIR.mkdir()
            storage.init_db(db)
            conn = storage.connect(db)
            alias_matcher.reload()
            # (a) real ESG event → kept
            storage.insert_article(conn, {"article_id":"a::1","url_canonical":"u1","url_original":"u1",
                "domain":"d","title":"Xử phạt Dabaco 300 triệu vì vi phạm môi trường","title_hash":"h1",
                "backend":"google_rss","group_key":"alias","sub_query_ix":0,"body_status":"fetched"})
            # (b) noise → dropped (esg_status=noise, not in per_ticker)
            storage.insert_article(conn, {"article_id":"a::2","url_canonical":"u2","url_original":"u2",
                "domain":"d","title":"Cổ đông Dabaco nhận cổ tức","title_hash":"h2",
                "backend":"google_rss","group_key":"alias","sub_query_ix":0,"body_status":"fetched"})
            # (c) alias only matchable in body, body still pending → deferred
            storage.insert_article(conn, {"article_id":"a::3","url_canonical":"u3","url_original":"u3",
                "domain":"d","title":"Một nhà máy bị phạt xả thải","title_hash":"h3",
                "backend":"google_rss","group_key":"alias","sub_query_ix":0,"body_status":"pending"})
            match.run(db_path=db)
            rows = {r["article_id"]: r for r in conn.execute("SELECT * FROM articles")}
            assert rows["a::1"]["esg_status"] == "esg" and rows["a::1"]["esg_type"] == "E"
            assert rows["a::2"]["esg_status"] == "noise"
            assert rows["a::3"]["esg_status"] == "pending"
            # deferred article keeps BOTH statuses pending, not just esg_status
            assert rows["a::3"]["match_status"] == "pending"
            doc = json.loads((settings.PER_TICKER_DIR / "DBC.json").read_text(encoding="utf-8"))
            ids = {a["article_id"] for a in doc["articles"]}
            assert "a::1" in ids and "a::2" not in ids
            assert doc["articles"][0].get("type") == "E"
            # severity made it into per_ticker
            assert doc["articles"][0].get("severity")
            conn.close()
        finally:
            settings.PER_TICKER_DIR = _orig_pt
    print("  match_esg_integration OK")


def test_l1_keyword_tasks() -> None:
    from core import queue_builder as qb
    from config import keywords as kw
    import tempfile
    from pathlib import Path
    terms = kw.search_terms()
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "l1.db"
        # 2 monthly chunks (June, July) × len(terms), one backend
        n = qb.build_keyword_tasks(backends=["google_rss"],
                                   window=("2024-06-01", "2024-07-31"), db_path=db)
        assert n["google_rss"] == 2 * len(terms), n
    print("  l1_keyword_tasks OK")


def test_l2_alias_tasks() -> None:
    from core import queue_builder as qb
    from core.queue_builder import _load_alias_lists
    from config import settings
    import tempfile
    from pathlib import Path
    names, subs = _load_alias_lists("DBC")   # reads config/aliases/DBC.json (must exist)
    assert names and subs, "DBC.json must have names + subsidiaries"
    a_sub = subs[0]
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "l2.db"
        n = qb.build_alias_tasks(tickers=["DBC"], db_path=db)
        assert n["baomoi"] > 0 and n["google_rss"] > 0 and n["brave"] > 0, n
        from core import storage
        conn = storage.connect(db)
        baomoi_q = {r["query"] for r in conn.execute(
            "SELECT query FROM search_queue WHERE backend='baomoi' AND kind='alias'")}
        google_q = {r["query"] for r in conn.execute(
            "SELECT query FROM search_queue WHERE backend='google_rss' AND kind='alias'")}
        assert a_sub in baomoi_q, "subsidiary not searched on baomoi"
        assert a_sub not in google_q, "subsidiary must NOT be searched on google"
        afters = {r["after"] for r in conn.execute(
            "SELECT after FROM search_queue WHERE backend='baomoi' AND kind='alias'")}
        assert afters == {settings.BAOMOI_WINDOW_START}, afters
        conn.close()
    print("  l2_alias_tasks OK")


def test_worker_stamps_ticker_hint() -> None:
    from workers import runner
    from core import storage
    import tempfile
    from pathlib import Path
    class FakeBackend:
        name = "baomoi"
        @staticmethod
        def fetch(q, a, b):
            return [{"url":"https://x.vn/a-1.html","title":"t","published_at":"2024-06-01","source":"s"}]
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "w.db"; storage.init_db(db); conn = storage.connect(db)
        task = {"task_id":"t1","query":"Dabaco","after":"2020-01-01","before":"2026-01-01",
                "group_key":"alias","sub_query_ix":0,"kind":"alias","ticker":"DBC"}
        runner._process_task(conn, FakeBackend, task)
        row = conn.execute("SELECT ticker_hint FROM articles LIMIT 1").fetchone()
        assert row["ticker_hint"] == "DBC", dict(row)
        conn.close()
    print("  worker_ticker_hint OK")


def test_weekly_subchunks() -> None:
    from core.queue_builder import weekly_subchunks
    weeks = weekly_subchunks("2024-06-01", "2024-06-30")
    assert len(weeks) >= 4 and weeks[0][0] == "2024-06-01"
    assert all(a <= b for a, b in weeks)
    assert weeks[-1][1] == "2024-06-30", weeks
    print("  weekly_subchunks OK")


def test_runner_splits_near_cap() -> None:
    from workers import runner
    from core import storage
    import tempfile
    from pathlib import Path
    class CapBackend:
        name = "google_rss"
        @staticmethod
        def fetch(q, a, b):
            return [{"url": f"https://x.vn/a-{i}.html", "title": "t", "published_at": a} for i in range(95)]
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "s.db"; storage.init_db(db); conn = storage.connect(db)
        # Enqueue the parent month exactly as build_alias_tasks would, then mark
        # it done — this is the state in which _maybe_split runs in production.
        storage.enqueue_task(conn, backend="google_rss", kind="alias", ticker="DBC",
            group_key="alias", sub_query_ix=0, query="Dabaco",
            after="2024-06-01", before="2024-06-30")
        parent_id = conn.execute("SELECT task_id FROM search_queue").fetchone()["task_id"]
        storage.mark_task_done(conn, parent_id, 95)
        task = conn.execute("SELECT * FROM search_queue WHERE task_id=?", (parent_id,)).fetchone()
        runner._maybe_split(conn, CapBackend, task, n_items=95)
        # A 30-day month yields 5 weekly children; with `before` in the alias
        # task_id none collide with the done parent (regression: the first week
        # used to share the parent's id and get dropped by INSERT OR IGNORE).
        kids = conn.execute(
            "SELECT COUNT(*) c FROM search_queue WHERE kind='alias' AND status='pending'"
        ).fetchone()["c"]
        assert kids == 5, kids
        first_week = conn.execute(
            "SELECT * FROM search_queue WHERE kind='alias' "
            "AND after='2024-06-01' AND before='2024-06-07'").fetchone()
        assert first_week is not None, "first weekly child was dropped (id collision)"
        assert first_week["ticker"] == "DBC" and first_week["query"] == "Dabaco"
        assert first_week["backend"] == "google_rss" and first_week["sub_query_ix"] == 0
        conn.close()
    print("  runner_splits_near_cap OK")


def test_parse_subsidiaries() -> None:
    from alias_builder import fetch_vietstock as fv
    from pathlib import Path
    html = Path("tests/fixtures/vietstock_DBC_subs.html").read_text(encoding="utf-8")
    subs = fv.parse_subsidiaries(html)
    # full names captured reliably
    assert any("Nasaco" in s for s in subs), subs
    assert any("Dabaco Thanh Hóa" in s for s in subs), subs
    assert any("Dabaco Quảng Ninh" in s for s in subs), subs
    shorts = fv.short_aliases(subs)
    # simple short forms derived (legal prefix stripped, short enough)
    assert "Dabaco Thanh Hóa" in shorts, shorts
    assert "Dabaco Quảng Ninh" in shorts, shorts
    # interior coined brand token extracted from a long legal name
    assert "Nasaco" in shorts, shorts
    # generic-token guard: "Minh Phát" must NOT become a standalone alias
    assert "Minh Phát" not in shorts, shorts
    # lone Vietnamese syllables (diacritic on the neighbour) must NOT leak as
    # brand tokens — these are the false positives the vowel-group guard kills.
    for fp in ("Thanh", "Ninh", "Minh"):
        assert fp not in shorts, (fp, shorts)
    # _is_brand_token unit checks: coined multi-syllable yes, lone syllable /
    # province / generic English no.
    assert fv._is_brand_token("Nasaco") and fv._is_brand_token("Dacovet")
    assert not fv._is_brand_token("Thanh")   # one vowel group
    assert not fv._is_brand_token("Power")   # generic English business noun
    assert not fv._is_brand_token("Nam")     # too short + one vowel group
    print("  parse_subsidiaries OK")


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
    test_keyword_config()
    test_esg_filter()
    test_match_esg_integration()
    test_l1_keyword_tasks()
    test_l2_alias_tasks()
    test_worker_stamps_ticker_hint()
    test_weekly_subchunks()
    test_runner_splits_near_cap()
    test_parse_subsidiaries()
    print("ALL OK")


if __name__ == "__main__":
    main()
