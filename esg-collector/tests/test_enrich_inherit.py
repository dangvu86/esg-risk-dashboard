# -*- coding: utf-8 -*-
"""Enrich cluster-inherit (Component 3): a pending article whose same-event
cluster already has a judged member inherits that verdict — no LLM call."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import storage


def _setup(tmp_path, monkeypatch, articles, judged):
    db = tmp_path / "articles.db"
    storage.init_db(db)
    conn = storage.connect(db)
    for aid, title, pub in articles:
        storage.insert_article(conn, {
            "article_id": aid, "url_canonical": f"https://x.vn/{aid}",
            "url_original": f"https://x.vn/{aid}", "domain": "x.vn",
            "title": title, "title_hash": None, "published_at": pub,
            "source": "X", "backend": "google_rss", "group_key": "alias",
            "sub_query_ix": 0, "body_status": "fetched",
        })
        verdict = judged.get(aid)
        if verdict == "risk":
            storage.mark_enriched(conn, aid, sentiment="risk", summary_en="EN judged")
        elif verdict == "risk+ctrl":
            storage.mark_enriched(conn, aid, sentiment="risk", summary_en="EN judged",
                                  controversy_level="Major",
                                  controversy_justification="x. y.",
                                  controversy_classified_at="2026-06-11T00:00:00Z")
        elif verdict == "drop":
            storage.mark_dropped(conn, aid)

    ptd = tmp_path / "per_ticker"
    ptd.mkdir()
    doc = {"ticker": "ACV", "articles": [
        {"article_id": aid, "title": t, "published_at": p}
        for aid, t, p in articles]}
    (ptd / "ACV.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    import enrich.runner as runner
    monkeypatch.setattr(runner.settings, "PER_TICKER_DIR", ptd)
    return conn, runner


ARTS = [
    ("a::1", "Phê chuẩn khởi tố, bắt tạm giam Chủ tịch ACV Vũ Thế Phiệt", "2026-03-06T01:00:00Z"),
    ("a::2", "Vì sao Chủ tịch ACV Vũ Thế Phiệt bị bắt?", "2026-03-06T05:00:00Z"),
    ("a::3", "ACV ký hợp đồng bảo trì đường băng sân bay quốc tế giai đoạn mới", "2026-03-04T00:00:00Z"),
]


def _events(conn, ids, severity="Cao"):
    rows = {r["article_id"]: r for r in conn.execute(
        "SELECT * FROM articles WHERE article_id IN (%s)" % ",".join("?"*len(ids)), ids)}
    return [{"article_id": i, "ticker": "ACV", "type": "G", "severity": severity,
             "summary": rows[i]["title"], "row": dict(rows[i], severity=severity)}
            for i in ids]


def test_pending_inherits_risk_verdict(tmp_path, monkeypatch):
    # Trung bình rows inherit even when the judged source has no controversy
    # (controversy is only ever classified for Cao).
    conn, runner = _setup(tmp_path, monkeypatch, ARTS, {"a::1": "risk"})
    inherited, remaining = runner._inherit_from_clusters(
        conn, _events(conn, ["a::2", "a::3"], severity="Trung bình"))
    assert inherited == 1
    assert [e["article_id"] for e in remaining] == ["a::3"]
    r = conn.execute("SELECT enrich_status, sentiment, summary_en FROM articles "
                     "WHERE article_id='a::2'").fetchone()
    assert r["enrich_status"] == "done" and r["sentiment"] == "risk"
    assert r["summary_en"] == "EN judged"


def test_cao_inherits_controversy_fields(tmp_path, monkeypatch):
    # A Cao row inheriting from a cluster whose judged member carries
    # controversy must copy those fields (the old code dropped them).
    conn, runner = _setup(tmp_path, monkeypatch, ARTS, {"a::1": "risk+ctrl"})
    inherited, remaining = runner._inherit_from_clusters(conn, _events(conn, ["a::2"]))
    assert inherited == 1 and remaining == []
    r = conn.execute("SELECT controversy_level, controversy_justification "
                     "FROM articles WHERE article_id='a::2'").fetchone()
    assert r["controversy_level"] == "Major"
    assert r["controversy_justification"] == "x. y."


def test_cao_without_controversy_source_goes_to_llm_path(tmp_path, monkeypatch):
    # No cluster member has controversy → a Cao row must NOT inherit (it would
    # freeze at empty); it goes to `remaining` for the normal LLM path.
    conn, runner = _setup(tmp_path, monkeypatch, ARTS, {"a::1": "risk"})
    inherited, remaining = runner._inherit_from_clusters(conn, _events(conn, ["a::2"]))
    assert inherited == 0
    assert [e["article_id"] for e in remaining] == ["a::2"]
    r = conn.execute("SELECT enrich_status FROM articles WHERE article_id='a::2'").fetchone()
    assert r["enrich_status"] == "pending"


def test_pending_inherits_drop_verdict(tmp_path, monkeypatch):
    conn, runner = _setup(tmp_path, monkeypatch, ARTS, {"a::1": "drop"})
    inherited, remaining = runner._inherit_from_clusters(conn, _events(conn, ["a::2", "a::3"]))
    assert inherited == 1
    assert [e["article_id"] for e in remaining] == ["a::3"]
    r = conn.execute("SELECT enrich_status, sentiment FROM articles "
                     "WHERE article_id='a::2'").fetchone()
    assert r["enrich_status"] == "dropped" and r["sentiment"] == "not_risk"


def test_no_judged_neighbor_means_no_inherit(tmp_path, monkeypatch):
    conn, runner = _setup(tmp_path, monkeypatch, ARTS, {})
    inherited, remaining = runner._inherit_from_clusters(conn, _events(conn, ["a::1", "a::2", "a::3"]))
    assert inherited == 0
    assert len(remaining) == 3


# Bug regression (real titles from the live DB): a bank-fraud governance
# article and three charity appeals get chained into ONE cluster by common
# words — "viên" ("nhân viên" ↔ "động viên"), "gái"/"ghép", "bệnh" — each a
# single-token transitive hop. Before the inherit guard the charity appeals
# inherited the fraud verdict (Cao/Minor, "bribery, corruption" justification),
# producing the wrong cards the user spotted.
BRIDGE_ARTS = [
    ("b::1", "Khởi tố cựu nhân viên Vietcombank lừa đảo chiếm đoạt tiền xin việc",
     "2026-06-11T00:00:00Z"),
    ("b::2", "Bé gái gần 2 tuổi mắc bệnh hiểm nghèo nguy kịch, chờ phép màu ghép tế bào gốc",
     "2026-06-10T00:00:00Z"),
    ("b::3", "Xúc động lá thư con gái động viên bố trước ngày phẫu thuật ghép phổi",
     "2026-06-08T00:00:00Z"),
    ("b::4", "Bệnh tật ập đến bất ngờ, gia đình nhặt ve chai bất lực trước chi phí điều trị",
     "2026-06-13T00:00:00Z"),
]


def test_common_word_bridge_is_clustered_but_not_inherited(tmp_path, monkeypatch):
    from core.events import cluster_events, same_event
    # 1. All four titles chain into ONE cluster (documents the over-merge).
    pool = [{"article_id": a, "ticker": "ACV", "title": t, "published_at": p}
            for a, t, p in BRIDGE_ARTS]
    clusters = cluster_events(pool)
    assert len(clusters) == 1, "expected the common-word bridge to over-merge"
    # 2. …but no charity appeal is the same event as the fraud article pairwise,
    #    so the guard rejects each.
    for _, charity_title, _ in BRIDGE_ARTS[1:]:
        assert not same_event(BRIDGE_ARTS[0][1], charity_title)
    # 3. End-to-end: none of the charity appeals inherit the fraud verdict.
    conn, runner = _setup(tmp_path, monkeypatch, BRIDGE_ARTS, {"b::1": "risk+ctrl"})
    inherited, remaining = runner._inherit_from_clusters(
        conn, _events(conn, ["b::2", "b::3", "b::4"]))
    assert inherited == 0
    assert sorted(e["article_id"] for e in remaining) == ["b::2", "b::3", "b::4"]
    for aid in ("b::2", "b::3", "b::4"):
        r = conn.execute("SELECT enrich_status, controversy_level FROM articles "
                         "WHERE article_id=?", (aid,)).fetchone()
        assert r["enrich_status"] == "pending" and r["controversy_level"] is None


def test_genuine_same_event_still_inherits_after_guard(tmp_path, monkeypatch):
    # Sanity: the guard must NOT block a real same-event member (high title
    # overlap) — a::2 still inherits from a::1.
    conn, runner = _setup(tmp_path, monkeypatch, ARTS, {"a::1": "risk+ctrl"})
    inherited, remaining = runner._inherit_from_clusters(conn, _events(conn, ["a::2"]))
    assert inherited == 1 and remaining == []
    r = conn.execute("SELECT controversy_level FROM articles WHERE article_id='a::2'").fetchone()
    assert r["controversy_level"] == "Major"
