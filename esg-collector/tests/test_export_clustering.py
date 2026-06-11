# -*- coding: utf-8 -*-
"""Export collapses same-event article clusters into one web card
(Component 2 of the event-clustering spec)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import storage
from pipeline import export


def _setup(tmp_path, articles, enriched):
    """articles: list of (article_id, title, published_at); enriched: id->(sentiment, en)"""
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
        if aid in enriched:
            s, en = enriched[aid]
            if s == "risk":
                storage.mark_enriched(conn, aid, sentiment="risk", summary_en=en)
            else:
                storage.mark_dropped(conn, aid)
    conn.close()

    ptd = tmp_path / "per_ticker"
    ptd.mkdir()
    doc = {"ticker": "ACV", "articles": [
        {"article_id": aid, "title": t, "published_at": p, "url": f"https://x.vn/{aid}",
         "source": "X", "backend": "google_rss", "matched_alias": "ACV",
         "location": "title", "match_source": "title", "type": "G", "severity": "Cao"}
        for aid, t, p in articles]}
    (ptd / "ACV.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return db, ptd


def test_cluster_of_reworded_risk_articles_emits_one_event(tmp_path):
    arts = [
        ("a::1", "Phê chuẩn khởi tố, bắt tạm giam Chủ tịch ACV Vũ Thế Phiệt", "2026-03-06T01:00:00Z"),
        ("a::2", "VKSND tối cao phê chuẩn khởi tố đối với Chủ tịch ACV Vũ Thế Phiệt", "2026-03-06T02:00:00Z"),
        ("a::3", "Vì sao Chủ tịch ACV Vũ Thế Phiệt bị bắt?", "2026-03-05T22:00:00Z"),
        # same cluster but NOT yet enriched — still counts as a source
        ("a::4", "ACV thông báo ông Vũ Thế Phiệt bị bắt tạm giam", "2026-03-06T09:00:00Z"),
    ]
    enr = {"a::1": ("risk", "ACV chairman arrested"),
           "a::2": ("risk", "Prosecution approved for ACV chairman"),
           "a::3": ("risk", "Why was the ACV chairman arrested?")}
    db, ptd = _setup(tmp_path, arts, enr)
    events = export.build_esg_events(db_path=db, per_ticker_dir=ptd, companies={"ACV": "ACV Corp"})
    assert len(events) == 1, [e["summary"] for e in events]
    ev = events[0]
    # representative = earliest RISK member (a::3, published 03-05)
    assert ev["summary_en"] == "Why was the ACV chairman arrested?"
    assert ev["date"] == "2026-03-05"
    assert ev["sources_count"] == 4
    assert len(ev["sources"]) == 4
    assert ev["sources"][0]["date"] == "2026-03-05"


def test_cluster_without_risk_member_emits_nothing(tmp_path):
    arts = [("a::1", "ACV tổ chức đại hội cổ đông thường niên", "2026-04-01T00:00:00Z")]
    db, ptd = _setup(tmp_path, arts, {})          # pending, not enriched
    events = export.build_esg_events(db_path=db, per_ticker_dir=ptd, companies={})
    assert events == []


def test_distinct_events_stay_separate_cards(tmp_path):
    arts = [
        ("a::1", "Bắt tạm giam Chủ tịch ACV Vũ Thế Phiệt", "2026-03-06T00:00:00Z"),
        ("a::2", "ACV bị truy thu thuế hàng chục tỷ đồng sau thanh tra toàn diện", "2026-05-01T00:00:00Z"),
    ]
    enr = {"a::1": ("risk", "ACV chairman arrested"),
           "a::2": ("risk", "ACV hit with tax arrears")}
    db, ptd = _setup(tmp_path, arts, enr)
    events = export.build_esg_events(db_path=db, per_ticker_dir=ptd, companies={})
    assert len(events) == 2
    assert {e["sources_count"] for e in events} == {1}


def test_dropped_member_does_not_resurrect_event(tmp_path):
    arts = [("a::1", "ACV khánh thành nhà ga mới hiện đại nhất", "2026-04-02T00:00:00Z")]
    db, ptd = _setup(tmp_path, arts, {"a::1": ("not_risk", "")})
    events = export.build_esg_events(db_path=db, per_ticker_dir=ptd, companies={})
    assert events == []


if __name__ == "__main__":
    import tempfile
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_"):
            with tempfile.TemporaryDirectory() as d:
                fn(Path(d))
            print(f"  {name} OK")
    print("ALL OK")
