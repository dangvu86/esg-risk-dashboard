"""_fetch_one: direct HTML → Jina fallback → trafilatura extraction.
Run: python -m tests.test_fetch_one_clean"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_direct_html_then_extract():
    from workers import body_fetcher
    from body_fetcher import fallback, extract
    of, oe = fallback.fetch, extract.extract_main
    fallback.fetch = lambda url: ("<html>full page</html>", "fetched")
    extract.extract_main = lambda html: "Bài sạch về ô nhiễm môi trường."
    try:
        body, status = body_fetcher._fetch_one("http://e/a")
        assert status == "fetched", status
        assert body == "Bài sạch về ô nhiễm môi trường.", body
    finally:
        fallback.fetch, extract.extract_main = of, oe
    print("  direct_html_then_extract OK")


def test_falls_back_to_jina_when_direct_fails():
    from workers import body_fetcher
    from body_fetcher import fallback, jina, extract
    of, oj, oe = fallback.fetch, jina.fetch, extract.extract_main
    fallback.fetch = lambda url: (None, "failed")
    jina.fetch = lambda url: ("<html>jina page</html>", "fetched")
    extract.extract_main = lambda html: "Bài lấy từ Jina."
    try:
        body, status = body_fetcher._fetch_one("http://e/a")
        assert status == "fetched" and body == "Bài lấy từ Jina.", (status, body)
    finally:
        fallback.fetch, jina.fetch, extract.extract_main = of, oj, oe
    print("  falls_back_to_jina_when_direct_fails OK")


def test_extract_miss_marks_failed():
    from workers import body_fetcher
    from body_fetcher import fallback, jina, extract
    of, oj, oe = fallback.fetch, jina.fetch, extract.extract_main
    fallback.fetch = lambda url: ("<html>only chrome</html>", "fetched")
    jina.fetch = lambda url: (None, "failed")
    extract.extract_main = lambda html: None  # trafilatura found no article
    try:
        body, status = body_fetcher._fetch_one("http://e/a")
        assert body is None and status == "failed", (status, body)
    finally:
        fallback.fetch, jina.fetch, extract.extract_main = of, oj, oe
    print("  extract_miss_marks_failed OK")


def main():
    if sys.platform == "win32":
        try: sys.stdout.reconfigure(encoding="utf-8")
        except Exception: pass
    print("running fetch_one tests…")
    test_direct_html_then_extract()
    test_falls_back_to_jina_when_direct_fails()
    test_extract_miss_marks_failed()
    print("ALL OK")


if __name__ == "__main__":
    main()
