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


def main():
    if sys.platform == "win32":
        try: sys.stdout.reconfigure(encoding="utf-8")
        except Exception: pass
    print("running body_clean tests…")
    test_strips_related_link_block()
    test_keeps_prose_company_mention()
    test_empty()
    print("ALL OK")


if __name__ == "__main__":
    main()
