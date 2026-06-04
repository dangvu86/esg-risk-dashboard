"""Jina selector + retry tests (Fix C). Run: python -m tests.test_jina_headers"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class _Resp:
    def __init__(self, text, status=200):
        self.text, self.status_code = text, status


def test_selector_header_present_then_retry_without():
    from body_fetcher import jina
    calls = []

    def fake_get(url, headers=None, timeout=30):
        calls.append(headers or {})
        # 1st call (with selector) returns empty -> triggers retry;
        # 2nd call (no selector) returns content.
        return _Resp("" if len(calls) == 1 else "real article body text " * 20)

    orig = jina.requests.get
    jina.requests.get = fake_get
    try:
        body, status = jina.fetch("http://example.com/a")
        assert status == "fetched" and body
        assert len(calls) == 2, calls
        assert "X-Target-Selector" in calls[0]
        assert "X-Target-Selector" not in calls[1]
    finally:
        jina.requests.get = orig
    print("  selector_then_retry OK")


def main():
    if sys.platform == "win32":
        try: sys.stdout.reconfigure(encoding="utf-8")
        except Exception: pass
    print("running jina header tests…")
    test_selector_header_present_then_retry_without()
    print("ALL OK")


if __name__ == "__main__":
    main()
