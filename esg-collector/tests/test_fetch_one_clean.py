"""_fetch_one cleans body (Fix C). Run: python -m tests.test_fetch_one_clean"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_fetch_one_strips_related():
    from workers import body_fetcher
    from body_fetcher import jina
    noisy = ("Bài viết thật về ô nhiễm.\n"
             "* [![Image 1: Tin khác](https://x/y)")
    orig = jina.fetch
    jina.fetch = lambda url: (noisy, "fetched")
    try:
        body, status = body_fetcher._fetch_one("http://e/a")
        assert status == "fetched"
        assert "ô nhiễm" in body and "Image 1" not in body
    finally:
        jina.fetch = orig
    print("  fetch_one_strips_related OK")


def main():
    if sys.platform == "win32":
        try: sys.stdout.reconfigure(encoding="utf-8")
        except Exception: pass
    print("running fetch_one clean test…")
    test_fetch_one_strips_related()
    print("ALL OK")


if __name__ == "__main__":
    main()
