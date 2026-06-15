"""Body-clean tests (Fix C). Run: python -m tests.test_body_clean"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_strips_related_link_block():
    from body_fetcher.body_clean import strip_related_blocks
    body = (
        "Ô nhiễm nghiêm trọng tại kênh hào thành cổ Vinh. "
        "Nước đen kịt, mùi hôi nồng nặc.\n"
        "* [![Image 52: Đất Xanh vươn tầm quốc tế với BLUEMARQ GROUP](https://x/y.htm)\n"
        "* [Vietnam Airlines và Nghệ An ký hợp tác](https://a/b.htm)\n"
        "- [Vincom khai trương](http://c/d)"
    )
    out = strip_related_blocks(body)
    assert "kênh hào thành cổ Vinh" in out
    assert "BLUEMARQ" not in out
    assert "Vietnam Airlines" not in out
    assert "Vincom" not in out
    print("  strips_related_link_block OK")


def test_keeps_prose_company_mention():
    from body_fetcher.body_clean import strip_related_blocks
    body = "Theo kết luận thanh tra, Tập đoàn Kido bị xử phạt do vi phạm thuế."
    assert strip_related_blocks(body) == body
    print("  keeps_prose_company_mention OK")


def test_empty():
    from body_fetcher.body_clean import strip_related_blocks
    assert strip_related_blocks("") == ""
    assert strip_related_blocks(None) is None
    print("  empty OK")


_CHARITY_BODY = (
    "Sau ca phẫu thuật ghép phổi, anh Tạ Văn Thắng vẫn trong tình trạng hôn mê. "
    "Gia đình anh kiệt quệ vì 6 năm chữa bệnh.\n"
    "Bạn đọc giúp anh Tạ Văn Thắng có thể quét mã QR hoặc gửi về số tài khoản "
    "0011002643148, Ngân hàng Vietcombank, hoặc số tài khoản 114000161718, "
    "Ngân hàng VietinBank.\n"
    "Cảnh báo thủ đoạn lừa đảo\n"
    "Thời gian qua một số đối tượng giả danh nhà hảo tâm để lừa đảo chiếm đoạt."
)


def test_editorial_body_cuts_donation_and_disclaimer_footer():
    # The donation footer carries bank names (alias FP) and the scam-warning
    # block carries "lừa đảo" (ESG-filter FP). editorial_body removes both;
    # the real story is kept.
    from body_fetcher.body_clean import editorial_body
    out = editorial_body(_CHARITY_BODY)
    assert "phẫu thuật ghép phổi" in out
    assert "Vietcombank" not in out
    assert "VietinBank" not in out
    assert "lừa đảo" not in out
    print("  editorial_body_cuts_footer OK")


def test_editorial_body_leaves_real_article_untouched():
    # No donation/disclaimer marker → body returned unchanged (modulo the
    # existing related-link stripping, which this body doesn't trigger).
    from body_fetcher.body_clean import editorial_body
    body = ("Theo kết luận thanh tra, Tập đoàn Kido bị xử phạt 750 triệu đồng "
            "do vi phạm về thuế và xả thải vượt quy chuẩn ra môi trường.")
    assert editorial_body(body) == body
    print("  editorial_body_leaves_real_article OK")


def test_editorial_body_empty():
    from body_fetcher.body_clean import editorial_body
    assert editorial_body("") == ""
    assert editorial_body(None) is None
    print("  editorial_body_empty OK")


def test_match_ignores_bank_in_donation_footer():
    # End-to-end: a charity appeal whose ONLY bank mention is the donation
    # footer must not match VCB/CTG once the body is the editorial view.
    from core import alias_matcher
    art = {"title": "Xúc động lá thư con gái động viên bố trước ngày ghép phổi",
           "description": "", "sapo": "", "body": _CHARITY_BODY}
    tickers = sorted(h.ticker for h in alias_matcher.match_article(art))
    assert tickers == [], f"expected no match, got {tickers}"
    print("  match_ignores_bank_in_donation_footer OK")


def main():
    if sys.platform == "win32":
        try: sys.stdout.reconfigure(encoding="utf-8")
        except Exception: pass
    print("running body_clean tests…")
    test_strips_related_link_block()
    test_keeps_prose_company_mention()
    test_empty()
    test_editorial_body_cuts_donation_and_disclaimer_footer()
    test_editorial_body_leaves_real_article_untouched()
    test_editorial_body_empty()
    test_match_ignores_bank_in_donation_footer()
    print("ALL OK")


if __name__ == "__main__":
    main()
