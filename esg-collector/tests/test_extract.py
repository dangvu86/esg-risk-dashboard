"""extract_main keeps the article, drops nav/footer chrome (trafilatura).
Run: python -m tests.test_extract"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


_HTML = """<!DOCTYPE html>
<html><head><title>Tai nan lao dong</title></head>
<body>
<header><nav>
  <a href="/cong-nghe">Thế giới số</a>
  <a href="/kinh-doanh">Kinh doanh</a>
</nav></header>
<article>
  <h1>Số vụ tai nạn lao động tăng cao trên cả nước</h1>
  <p>Theo thông tin từ Bộ Lao động, Thương binh và Xã hội, trong năm qua cả nước
  ghi nhận hàng nghìn vụ tai nạn lao động nghiêm trọng, khiến nhiều người thiệt
  mạng và bị thương. Các vụ việc tập trung ở lĩnh vực xây dựng và khai thác mỏ.</p>
  <p>Cơ quan chức năng yêu cầu các doanh nghiệp tăng cường công tác an toàn vệ
  sinh lao động, kiểm tra thiết bị định kỳ và tập huấn cho người lao động nhằm
  giảm thiểu rủi ro trong quá trình sản xuất tại các công trường trên cả nước.</p>
</article>
<footer>
  <div class="related"><a href="/hdbank-2026">HDBank ưu đãi cuối năm</a></div>
</footer>
</body></html>"""


def test_extract_keeps_article_drops_chrome():
    from body_fetcher import extract
    text = extract.extract_main(_HTML)
    assert text, "extract returned nothing"
    assert "tai nạn lao động" in text.lower()
    # nav-link section name and footer ad-bank name must NOT survive
    assert "Thế giới số" not in text, text
    assert "HDBank" not in text, text
    print("  extract_keeps_article_drops_chrome OK")


def test_extract_empty_or_tiny_returns_none():
    from body_fetcher import extract
    assert extract.extract_main("") is None
    assert extract.extract_main(None) is None
    assert extract.extract_main("<html><body><p>x</p></body></html>") is None
    print("  extract_empty_or_tiny_returns_none OK")


def main():
    if sys.platform == "win32":
        try: sys.stdout.reconfigure(encoding="utf-8")
        except Exception: pass
    print("running extract tests…")
    test_extract_keeps_article_drops_chrome()
    test_extract_empty_or_tiny_returns_none()
    print("ALL OK")


if __name__ == "__main__":
    main()
