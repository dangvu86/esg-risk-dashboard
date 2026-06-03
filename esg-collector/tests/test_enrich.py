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


def test_llm_resolve_provider(monkeyenv=None) -> None:
    import importlib, os
    from enrich import llm
    saved = dict(os.environ)
    try:
        for k in list(os.environ):
            if k.endswith("_API_KEY") or k in ("LLM_PROVIDER", "LLM_MODEL"):
                del os.environ[k]
        assert llm.resolve_provider() is None          # no keys → None
        os.environ["GROQ_API_KEY"] = "gsk_test"
        p = llm.resolve_provider()
        assert p and p["name"] == "groq" and p["key"] == "gsk_test"
        assert p["schema"] == "openai" and p["model"]   # default model present
        os.environ["LLM_MODEL"] = "meta-llama/llama-4-scout-17b-16e-instruct"
        assert llm.resolve_provider()["model"] == "meta-llama/llama-4-scout-17b-16e-instruct"
        # build_request shape for openai schema
        url, payload, headers, extract = llm._build_request(p, "hello")
        assert url.startswith("https://api.groq.com") and b"hello" in payload
        assert headers["Authorization"] == "Bearer gsk_test"
    finally:
        os.environ.clear(); os.environ.update(saved)
    print("  llm_resolve_provider OK")


def test_revenue() -> None:
    from enrich import revenue
    rev = revenue.load_revenues()          # reads config/companies.csv
    assert "VIC" in rev and 2024 in rev["VIC"]
    assert rev["VIC"][2024] > 0
    # exact-year hit
    assert revenue.get_revenue_for_year(rev["VIC"], 2024) == (2024, rev["VIC"][2024])
    # missing year → closest (ties → older)
    yr, val = revenue.get_revenue_for_year({2020: 100.0, 2024: 200.0}, 2022)
    assert (yr, val) == (2020, 100.0)
    assert revenue.get_revenue_for_year({}, 2024) is None
    print("  revenue OK")


def test_sentiment_gate() -> None:
    from enrich import sentiment
    _orig = sentiment.call_llm
    try:
        events = [{"ticker": "DBC", "type": "E", "summary": "Phat vi xa thai"},
                  {"ticker": "DBC", "type": "S", "summary": "Quy thien tam ho tro nan nhan"}]
        # fake provider + monkeypatch the LLM to label [risk, not_risk]
        fake_provider = {"name": "x", "model": "m", "sleep": 0}
        sentiment.call_llm = lambda prov, prompt, retries=3: {"labels": ["risk", "not_risk"]}
        kept = sentiment.filter_negative(events, provider=fake_provider)
        assert len(kept) == 1 and kept[0]["type"] == "E"
        # LLM failure → keep all (fail-open)
        sentiment.call_llm = lambda prov, prompt, retries=3: None
        assert len(sentiment.filter_negative(events, provider=fake_provider)) == 2
        print("  sentiment_gate OK")
    finally:
        sentiment.call_llm = _orig


def test_translate() -> None:
    from enrich import translate
    _orig = translate.call_llm
    try:
        fake_provider = {"name": "x", "model": "m", "sleep": 0}
        translate.call_llm = lambda prov, prompt, retries=3: {"translations": ["Fined for discharge", "EN2"]}
        out = translate.translate_titles(["Phat vi xa thai", "tin 2"], provider=fake_provider)
        assert out == ["Fined for discharge", "EN2"]
        # LLM failure → fall back to VN input
        translate.call_llm = lambda prov, prompt, retries=3: None
        assert translate.translate_titles(["a", "b"], provider=fake_provider) == ["a", "b"]
        # length mismatch (wrong count returned) → also fall back to VN input
        translate.call_llm = lambda prov, prompt, retries=3: {"translations": ["only_one"]}
        assert translate.translate_titles(["a", "b"], provider=fake_provider) == ["a", "b"]
    finally:
        translate.call_llm = _orig
    print("  translate OK")


def test_controversy() -> None:
    from enrich import controversy
    _orig = controversy.call_llm
    try:
        fake_provider = {"name": "x", "model": "m", "sleep": 0}
        captured = {}
        def fake_call(prov, prompt, retries=3):
            captured["prompt"] = prompt
            return {"level": "Major", "cg_indicator": None,
                    "justification": "Worker died at plant. Material consequence, no resolution.",
                    "confidence": 90}
        controversy.call_llm = fake_call
        event = {"ticker": "HPG", "company": "Hoa Phat", "type": "S",
                 "date": "2026-05-26", "summary": "Cong nhan tu vong", "summary_en": "Worker death",
                 "source": "Tuoi Tre"}
        body = "Long article body about a fatal accident at the Dung Quat plant " * 50
        out = controversy.classify_event(event, fake_provider, today="2026-06-03",
                                         body=body, revenues={"HPG": {2026: 150000.0}})
        assert out["level"] == "Major" and out["cg_indicator"] is None
        assert out["justification"].count(". ") >= 1
        # body content was injected into the prompt and revenue injected
        assert "Dung Quat" in captured["prompt"]
        assert "150,000 billion VND" in captured["prompt"] or "150000" in captured["prompt"]
        # oversized body (> ARTICLE_BODY_MAX_CHARS) is truncated with a marker in the prompt
        big = "X" * (controversy.ARTICLE_BODY_MAX_CHARS + 1000)
        controversy.classify_event(event, fake_provider, today="2026-06-03",
                                   body=big, revenues={})
        assert "...[truncated]" in captured["prompt"]
        assert big not in captured["prompt"]   # full untruncated body must not survive
        # invalid LLM level → None
        controversy.call_llm = lambda prov, prompt, retries=3: {"level": "Nope", "justification": "x."}
        assert controversy.classify_event(event, fake_provider, today="2026-06-03", body="", revenues={}) is None
    finally:
        controversy.call_llm = _orig
    print("  controversy OK")


def test_runner_end_to_end() -> None:
    import tempfile, json
    from pathlib import Path
    from core import storage, alias_matcher
    from enrich import runner, sentiment, translate, controversy
    alias_matcher.reload()
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "r.db"
        storage.init_db(db)
        conn = storage.connect(db)
        # one Cao Dabaco E article (kept) + one article dropped by the sentiment gate
        conn.execute("INSERT INTO articles (article_id,url_canonical,title,esg_status,esg_type,severity,body) "
                     "VALUES ('a::1','u1','Dabaco bi phat vi xa thai','esg','E','Cao','body text')")
        conn.execute("INSERT INTO articles (article_id,url_canonical,title,esg_status,esg_type,severity) "
                     "VALUES ('a::2','u2','Quy thien tam Dabaco ho tro','esg','S','Trung bình')")
        conn.close()
        _orig_filter = runner.sentiment.filter_negative
        _orig_translate = runner.translate.translate_titles
        _orig_classify = runner.controversy.classify_event
        _orig_provider = runner.resolve_provider
        try:
            # monkeypatch stages: sentiment drops a::2; translate echoes EN; controversy → Minor
            runner.sentiment.filter_negative = lambda evs, provider=None: [e for e in evs if "thien tam" not in e["summary"]]
            runner.translate.translate_titles = lambda titles, provider=None: ["EN:" + t for t in titles]
            runner.controversy.classify_event = lambda e, p, today, *, body, revenues=None: {
                "level": "Minor", "cg_indicator": None, "justification": "a. b.", "confidence": 80}
            # force a provider so the runner proceeds
            runner.resolve_provider = lambda: {"name": "x", "model": "m", "sleep": 0}
            n = runner.run(limit=10, db_path=db)
            conn = storage.connect(db)
            r1 = conn.execute("SELECT * FROM articles WHERE article_id='a::1'").fetchone()
            r2 = conn.execute("SELECT * FROM articles WHERE article_id='a::2'").fetchone()
            assert r1["enrich_status"] == "done" and r1["sentiment"] == "risk"
            assert r1["summary_en"] == "EN:Dabaco bi phat vi xa thai"
            assert r1["controversy_level"] == "Minor" and r1["controversy_classified_at"]
            assert r2["enrich_status"] == "dropped" and r2["sentiment"] == "not_risk"
            assert not storage.get_pending_enrich(conn, limit=10)  # all drained
            conn.close()
        finally:
            runner.sentiment.filter_negative = _orig_filter
            runner.translate.translate_titles = _orig_translate
            runner.controversy.classify_event = _orig_classify
            runner.resolve_provider = _orig_provider
    print("  runner_end_to_end OK")


def test_build_esg_events() -> None:
    import tempfile, json
    from pathlib import Path
    from core import storage
    from config import settings
    from pipeline import export
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "w.db"; storage.init_db(db); conn = storage.connect(db)
        # two articles, SAME title_hash (same incident, 2 sources) — one risk one earlier
        conn.execute("INSERT INTO articles (article_id,url_canonical,url_original,title,title_hash,"
            "published_at,source,backend,esg_status,esg_type,severity,enrich_status,sentiment,summary_en,"
            "controversy_level,controversy_justification) VALUES "
            "('a::1','c1','o1','Dabaco bi phat','H','2026-05-27T00:00:00Z','Lao Dong','google_rss',"
            "'esg','E','Cao','done','risk','Dabaco fined','Minor','a. b.')")
        conn.execute("INSERT INTO articles (article_id,url_canonical,url_original,title,title_hash,"
            "published_at,source,backend,esg_status,esg_type,severity,enrich_status,sentiment,summary_en) VALUES "
            "('a::2','c2','o2','Dabaco bi phat','H','2026-05-28T00:00:00Z','CafeF','baomoi',"
            "'esg','E','Cao','done','risk','Dabaco fined')")
        # a dropped (not_risk) article must be excluded
        conn.execute("INSERT INTO articles (article_id,url_canonical,title,title_hash,published_at,"
            "esg_status,esg_type,severity,enrich_status,sentiment) VALUES "
            "('a::3','c3','x','H3','2026-05-20T00:00:00Z','esg','S','Trung bình','dropped','not_risk')")
        # a done+not_risk article: passes the SQL enrich_status='done' filter but
        # must be excluded by the in-loop sentiment != 'risk' gate
        conn.execute("INSERT INTO articles (article_id,url_canonical,title,title_hash,published_at,"
            "esg_status,esg_type,severity,enrich_status,sentiment) VALUES "
            "('a::4','c4','y','H4','2026-05-19T00:00:00Z','esg','S','Trung bình','done','not_risk')")
        conn.commit(); conn.close()
        pt = Path(td) / "pt"; pt.mkdir()
        (pt / "DBC.json").write_text(json.dumps({"ticker": "DBC", "articles": [
            {"article_id": "a::1", "url": "c1", "title": "Dabaco bi phat", "published_at": "2026-05-27T00:00:00Z",
             "source": "Lao Dong", "backend": "google_rss", "matched_alias": "Dabaco", "type": "E", "severity": "Cao"},
            {"article_id": "a::2", "url": "c2", "title": "Dabaco bi phat", "published_at": "2026-05-28T00:00:00Z",
             "source": "CafeF", "backend": "baomoi", "matched_alias": "Dabaco", "type": "E", "severity": "Cao"},
            {"article_id": "a::3", "url": "c3", "title": "x", "published_at": "2026-05-20T00:00:00Z",
             "source": "s", "backend": "google_rss", "matched_alias": "Dabaco", "type": "S", "severity": "Trung bình"},
            {"article_id": "a::4", "url": "c4", "title": "y", "published_at": "2026-05-19T00:00:00Z",
             "source": "s", "backend": "google_rss", "matched_alias": "Dabaco", "type": "S", "severity": "Trung bình"},
        ]}, ensure_ascii=False), encoding="utf-8")
        events = export.build_esg_events(db_path=db, per_ticker_dir=pt,
                                         companies={"DBC": "CTCP Tap doan Dabaco Viet Nam"})
        # one event (a::1 & a::2 collapse by title_hash to earliest; a::3 dropped;
        # a::4 done+not_risk excluded by the in-loop sentiment gate)
        assert len(events) == 1, events
        e = events[0]
        assert e["ticker"] == "DBC"
        assert e["company"] == "CTCP Tap doan Dabaco Viet Nam"   # injected dict resolved
        assert e["date"] == "2026-05-27" and e["created_at"] is not None
        assert e["summary"] == "Dabaco bi phat" and e["summary_en"] == "Dabaco fined"
        assert e["type"] == "E" and e["severity"] == "Cao"
        assert e["controversy_level"] == "Minor"
        assert e["source"] == "Lao Dong" and e["url"] == "c1"
    print("  build_esg_events OK")


def test_export_web_upload_skips_ndjson() -> None:
    from pipeline import export
    calls = {"ndjson_upload": 0, "web_files": 0, "web_upload": 0}
    _orig_upload = export._upload
    _orig_write = export._write_web_files
    _orig_uweb = export._upload_web
    try:
        export._upload = lambda *a, **k: calls.__setitem__("ndjson_upload", calls["ndjson_upload"] + 1)
        export._write_web_files = lambda: (calls.__setitem__("web_files", calls["web_files"] + 1) or (Path("ev"), Path("top")))
        export._upload_web = lambda ev, top: calls.__setitem__("web_upload", calls["web_upload"] + 1)
        # --web --upload with NO --ndjson must NOT attempt any NDJSON upload (no SystemExit,
        # no stale re-push), and MUST build + upload the web files.
        export.run(do_ndjson=False, do_upload=True, do_web=True)
        assert calls["ndjson_upload"] == 0, calls
        assert calls["web_files"] == 1 and calls["web_upload"] == 1, calls
    finally:
        export._upload = _orig_upload
        export._write_web_files = _orig_write
        export._upload_web = _orig_uweb
    print("  export_web_upload_skips_ndjson OK")


def main() -> None:
    if sys.platform == "win32":
        try: sys.stdout.reconfigure(encoding="utf-8")
        except Exception: pass
    print("running enrich tests…")
    test_enrich_columns_and_queries()
    test_llm_resolve_provider()
    test_revenue()
    test_sentiment_gate()
    test_translate()
    test_controversy()
    test_runner_end_to_end()
    test_build_esg_events()
    test_export_web_upload_skips_ndjson()
    print("ALL OK")


if __name__ == "__main__":
    main()
