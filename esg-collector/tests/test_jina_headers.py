"""Jina fetcher requests raw HTML and returns it. Run: python -m tests.test_jina_headers"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class _Resp:
    def __init__(self, text, status=200):
        self.text, self.status_code = text, status


def test_requests_html_format_and_returns_body():
    from body_fetcher import jina
    calls = []

    def fake_get(url, headers=None, timeout=45):
        calls.append((url, headers or {}))
        return _Resp("<html>real article body</html> " * 200)  # > _MIN_HTML

    orig = jina.requests.get
    jina.requests.get = fake_get
    try:
        body, status = jina.fetch("http://example.com/a")
        assert status == "fetched" and body, (status, bool(body))
        assert calls[0][0] == "https://r.jina.ai/http://example.com/a", calls[0][0]
        assert calls[0][1].get("X-Return-Format") == "html", calls[0][1]
    finally:
        jina.requests.get = orig
    print("  requests_html_format_and_returns_body OK")


def test_429_is_ratelimited():
    from body_fetcher import jina
    orig = jina.requests.get
    jina.requests.get = lambda url, headers=None, timeout=45: _Resp("", 429)
    try:
        body, status = jina.fetch("http://example.com/a")
        assert body is None and status == "ratelimited", (status, body)
    finally:
        jina.requests.get = orig
    print("  429_is_ratelimited OK")


def main():
    if sys.platform == "win32":
        try: sys.stdout.reconfigure(encoding="utf-8")
        except Exception: pass
    print("running jina fetcher tests…")
    test_requests_html_format_and_returns_body()
    test_429_is_ratelimited()
    print("ALL OK")


if __name__ == "__main__":
    main()
