# -*- coding: utf-8 -*-
"""Unit tests for core/events.py — event clustering per
docs/superpowers/specs/2026-06-08-event-clustering-dedup-design.md"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _a(i, ticker, title, pub):
    return {"article_id": f"a::{i}", "ticker": ticker, "title": title,
            "published_at": pub}


def _clusters(arts, **kw):
    from core import events
    return events.cluster_events(arts, **kw)


def test_reworded_same_event_clusters_together():
    # Real ACV-arrest rewordings — must land in ONE cluster
    arts = [
        _a(1, "ACV", "Phê chuẩn khởi tố, bắt tạm giam Chủ tịch ACV Vũ Thế Phiệt",
           "2026-03-06T01:00:00Z"),
        _a(2, "ACV", "VKSND tối cao phê chuẩn khởi tố đối với Chủ tịch ACV Vũ Thế Phiệt",
           "2026-03-06T02:00:00Z"),
        _a(3, "ACV", "ACV thông báo ông Vũ Thế Phiệt bị bắt tạm giam",
           "2026-03-06T09:00:00Z"),
        _a(4, "ACV", "Vì sao Chủ tịch ACV Vũ Thế Phiệt bị bắt?",
           "2026-03-05T22:00:00Z"),
    ]
    cl = _clusters(arts)
    assert len(cl) == 1, [ [m["article_id"] for m in c] for c in cl ]
    # representative = earliest published
    assert cl[0][0]["article_id"] == "a::4"


def test_distinct_events_same_ticker_stay_separate():
    arts = [
        _a(1, "SAB", "SABECO bị phạt, truy thu gần 7,5 tỷ đồng tiền thuế",
           "2026-02-10T00:00:00Z"),
        _a(2, "SAB", "Tuyên án vụ xâm phạm nhãn hiệu Bia Sài Gòn của SABECO",
           "2026-02-12T00:00:00Z"),
    ]
    cl = _clusters(arts)
    assert len(cl) == 2, [ [m["title"] for m in c] for c in cl ]


def test_window_boundary_separates():
    arts = [
        _a(1, "HSG", "Hoa Sen lên tiếng về việc Australia điều tra chống bán phá giá thép mạ kẽm",
           "2026-05-01T00:00:00Z"),
        _a(2, "HSG", "Hoa Sen lên tiếng về vụ Australia điều tra chống bán phá giá thép mạ kẽm",
           "2026-05-20T00:00:00Z"),
    ]
    cl = _clusters(arts, window_days=10)
    assert len(cl) == 2


def test_rare_token_merges_short_headline():
    # Short headline fails Jaccard but shares the rare token "PECC3"
    arts = [
        _a(1, "PC1", "Chủ tịch Nguyễn Như Hoàng Tuấn cùng tổng giám đốc, kế toán trưởng PECC3 bị bắt",
           "2026-06-09T00:00:00Z"),
        _a(2, "PC1", "Ba lãnh đạo PECC3 bị bắt tạm giam", "2026-06-09T05:00:00Z"),
        # unrelated PC1 article in the same window, no shared rare token
        _a(3, "PC1", "PC1 trúng thầu dự án truyền tải điện mới tại miền Trung",
           "2026-06-08T00:00:00Z"),
    ]
    cl = _clusters(arts)
    sizes = sorted(len(c) for c in cl)
    assert sizes == [1, 2], [ [m["title"] for m in c] for c in cl ]


def test_ticker_is_hard_boundary():
    arts = [
        _a(1, "HSG", "Australia khởi xướng điều tra chống bán phá giá thép mạ kẽm",
           "2026-05-09T00:00:00Z"),
        _a(2, "NKG", "Australia khởi xướng điều tra chống bán phá giá thép mạ kẽm",
           "2026-05-09T00:00:00Z"),
    ]
    cl = _clusters(arts)
    assert len(cl) == 2


def test_dateless_only_clusters_with_dateless():
    arts = [
        _a(1, "VND", "VNDirect bị tấn công mạng, hệ thống tê liệt", "2026-03-25T00:00:00Z"),
        _a(2, "VND", "VNDirect bị tấn công mạng, hệ thống tê liệt nhiều ngày", ""),
    ]
    cl = _clusters(arts)
    assert len(cl) == 2


def test_singleton_and_empty_title():
    arts = [
        _a(1, "DBC", "", "2026-01-01T00:00:00Z"),
        _a(2, "DBC", "Dabaco bị thu hồi hơn 1.400 m2 đất ở Thuận Thành", "2026-01-01T00:00:00Z"),
    ]
    cl = _clusters(arts)
    assert len(cl) == 2
    assert all(len(c) == 1 for c in cl)


def test_verbatim_republication_same_cluster():
    arts = [
        _a(1, "QNS", "Phạt Công ty cổ phần Đường Quảng Ngãi gần 750 triệu đồng - Báo A",
           "2026-06-01T00:00:00Z"),
        _a(2, "QNS", "Phạt Công ty cổ phần Đường Quảng Ngãi gần 750 triệu đồng | Báo B",
           "2026-06-02T00:00:00Z"),
    ]
    cl = _clusters(arts)
    assert len(cl) == 1


def test_event_key_stable():
    from core import events
    arts = [
        _a(2, "ACV", "ACV thông báo ông Vũ Thế Phiệt bị bắt tạm giam", "2026-03-06T09:00:00Z"),
        _a(1, "ACV", "Vì sao Chủ tịch ACV Vũ Thế Phiệt bị bắt?", "2026-03-05T22:00:00Z"),
    ]
    cl = events.cluster_events(arts)
    assert events.event_key(cl[0]) == "ACV:a::1"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"  {name} OK")
    print("ALL OK")
