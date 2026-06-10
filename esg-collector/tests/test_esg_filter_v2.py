# -*- coding: utf-8 -*-
"""Golden regression tests for the V2 esg_filter ruleset.

Each case is a real article (title verbatim) from the 2026-06-10 false-negative
audit: production filter killed ~350-540 real violation articles via two
mechanisms (noise-override and vocab gap). Run: python -m tests.test_esg_filter_v2
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _v(title, sapo="", body="", source=""):
    from pipeline import esg_filter
    return esg_filter.classify(
        {"title": title, "sapo": sapo, "body": body, "source": source})


# ---- mechanism 1: STRONG event terms must beat the noise blacklist ----

def test_strong_xu_phat_beats_noise_dat_chuan() -> None:
    # Nasaco/DBC fine — killed by noise "đạt chuẩn" matching inside "KHÔNG đạt chuẩn"
    v = _v("Công ty Nasaco Hà Nam bị xử phạt do sản xuất thức ăn chăn nuôi không đạt chuẩn")
    assert v.keep, v
    print("  strong xử phạt > noise đạt chuẩn OK")


def test_strong_thao_tung_beats_noise_in_body() -> None:
    # PDR manipulation fine — the whole story was noise-killed by body PR words
    v = _v("Phạt 3 tỷ đồng hành vi thao túng cổ phiếu PDR",
           body="Nhà đầu tư cần bảo vệ tài sản và kỳ vọng tăng trưởng dài hạn.")
    assert v.keep and v.severity == "Cao", v
    print("  strong thao túng > body noise OK")


def test_fine_amount_within_60_chars() -> None:
    # QNS — company name pushes the amount 33 chars past "Phạt"; old 30-char window missed
    v = _v("Phạt Công ty cổ phần Đường Quảng Ngãi gần 750 triệu đồng")
    assert v.keep and v.severity == "Cao", v
    print("  fineprox 60-char window OK")


# ---- mechanism 2: vocab gap (title-level violation words) ----

def test_bare_vi_pham_in_title() -> None:
    # DBC case #4 — bare "vi phạm" was not in vocab (only vi phạm môi trường/pháp luật/quy định)
    v = _v("Tập đoàn xây dựng Hòa Bình và DABACO Việt Nam: có dấu hiệu vi phạm "
           "khi thi công san lấp mặt bằng dự án Khu chăn nuôi lợn giống")
    assert v.keep, v
    print("  bare vi phạm title OK")


def test_thu_hoi_dat_with_digits() -> None:
    # IDICO — "thu hồi hơn 11.000m2 đất": digits+dot sit between "thu hồi" and "đất"
    v = _v("Đồng Nai thu hồi hơn 11.000m2 đất của IDICO và Sông Đà Đồng Nai")
    assert v.keep, v
    print("  thu hồi …đất regex OK")


def test_su_co_in_title() -> None:
    v = _v("Công ty chứng khoán VNDirect gặp sự cố, nhà đầu tư không thể đặt lệnh mua, bán")
    assert v.keep, v
    print("  sự cố title OK")


def test_bi_chay_in_title() -> None:
    v = _v("Hoa Sen Group (HSG): Nhà máy bị cháy đã ngưng hoạt động từ 2019")
    assert v.keep, v
    print("  bị cháy OK")


# ---- guards ----

def test_overlap_guard_huy_niem_yet() -> None:
    # noise "niêm yết" lives INSIDE esg term "hủy niêm yết" — must not self-destruct
    v = _v("Cổ phiếu HBC trước nguy cơ bị hủy niêm yết")
    assert v.keep, v
    print("  overlap guard OK")


def test_negation_guard_dam_bao_an_toan() -> None:
    # noise "đảm bảo an toàn" negated by "không" = the violation itself
    v = _v("Kết luận thanh tra: nhà máy không đảm bảo an toàn lao động")
    assert v.keep, v
    print("  negation guard OK")


def test_body_strong_term_does_not_rescue() -> None:
    # anti body-sidebar: strong terms count in title+sapo only
    v = _v("Lãi suất ngân hàng hôm nay: 33 ngân hàng giảm lãi suất",
           body="Tin khác: triệt phá đường dây hàng giả quy mô lớn tại biên giới.")
    assert not v.keep and v.reason == "non_esg", v
    print("  body-scope guard OK")


def test_positive_direction_guard_cbpg() -> None:
    # trade-defense GOOD news must not enter via "chống bán phá giá"
    v = _v("Thép cuộn cán nóng của Hòa Phát chính thức không bị Ấn Độ áp thuế "
           "chống bán phá giá")
    assert not v.keep, v
    print("  positive-direction guard OK")


# ---- source-aware title suffix strip ----

def test_source_suffix_stripped_when_matches_source() -> None:
    # "- Báo Thanh tra" suffix used to trigger esg term "thanh tra" on junk PR
    v = _v("ATM thế hệ mới của Techcombank có thể rút được 20 triệu đồng/lần - Báo Thanh tra",
           source="Báo Thanh tra")
    assert not v.keep and v.reason == "non_esg", v
    print("  source suffix strip OK")


def test_internal_hyphen_content_not_stripped() -> None:
    # blind strip ate " - Hà Tĩnh vừa bị phạt"; source-aware strip must keep it
    v = _v("Soi sức khỏe tài chính Bia Sài Gòn - Hà Tĩnh vừa bị phạt", source="CafeF")
    assert v.keep, v
    print("  internal hyphen kept OK")


# ---- junk must stay dead (regression guards) ----

def test_pr_noise_still_rejected() -> None:
    v = _v("PV GAS hưởng ứng ngày Môi trường Thế giới với chủ đề 'Chống ô nhiễm nhựa'")
    assert not v.keep and v.reason == "noise", v
    print("  PR noise still dead OK")


def test_sports_still_rejected() -> None:
    v = _v("Tin thể thao (26-7): Messi bị kỷ luật, cầu mây Việt Nam tạo 'địa chấn'")
    assert not v.keep, v
    print("  sports still dead OK")


def test_plain_non_esg_still_rejected() -> None:
    v = _v("Công ty X tổ chức đại hội cổ đông thường niên")
    assert not v.keep and v.reason == "non_esg", v
    print("  plain non_esg OK")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ALL OK")
