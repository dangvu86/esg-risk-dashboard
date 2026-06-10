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


def _events(conn, ids):
    rows = {r["article_id"]: r for r in conn.execute(
        "SELECT * FROM articles WHERE article_id IN (%s)" % ",".join("?"*len(ids)), ids)}
    return [{"article_id": i, "ticker": "ACV", "type": "G", "severity": "Cao",
             "summary": rows[i]["title"], "row": rows[i]} for i in ids]


def test_pending_inherits_risk_verdict(tmp_path, monkeypatch):
    conn, runner = _setup(tmp_path, monkeypatch, ARTS, {"a::1": "risk"})
    inherited, remaining = runner._inherit_from_clusters(conn, _events(conn, ["a::2", "a::3"]))
    assert inherited == 1
    assert [e["article_id"] for e in remaining] == ["a::3"]
    r = conn.execute("SELECT enrich_status, sentiment, summary_en FROM articles "
                     "WHERE article_id='a::2'").fetchone()
    assert r["enrich_status"] == "done" and r["sentiment"] == "risk"
    assert r["summary_en"] == "EN judged"


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
