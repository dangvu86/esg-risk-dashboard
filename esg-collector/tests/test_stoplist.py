"""Alias stoplist tests (Fix 1 + A). Run: python -m tests.test_stoplist"""
from __future__ import annotations
import json, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import settings  # noqa: E402


def _write_aliases(d: Path) -> None:
    (d / "KDC.json").write_text(json.dumps(
        {"ticker": "KDC", "names": ["KDC", "Kido"],
         "subsidiaries": [], "projects": [], "locations": []},
        ensure_ascii=False), encoding="utf-8")
    (d / "ACV.json").write_text(json.dumps(
        {"ticker": "ACV", "names": ["ACV", "Tổng công ty Cảng hàng không Việt Nam"],
         "subsidiaries": [], "projects": [], "locations": []},
        ensure_ascii=False), encoding="utf-8")


def _reload_with(ad: Path, stoplist_path: Path):
    from core import alias_matcher
    settings.AMBIGUOUS_ALIASES_PATH = stoplist_path
    alias_matcher.reload(ad)
    return alias_matcher


def test_stoplisted_surface_dropped():
    from core import alias_matcher
    _orig = settings.AMBIGUOUS_ALIASES_PATH
    with tempfile.TemporaryDirectory() as td:
        ad = Path(td) / "aliases"; ad.mkdir(); _write_aliases(ad)
        sp = Path(td) / "stop.json"; sp.write_text(json.dumps(["KDC"]), encoding="utf-8")
        try:
            am = _reload_with(ad, sp)
            assert not any(h.ticker == "KDC" for h in am.match_text("Sai phạm KDC Bình Đa"))
            assert any(h.ticker == "KDC" for h in am.match_text("Tập đoàn Kido bị phạt"))
        finally:
            settings.AMBIGUOUS_ALIASES_PATH = _orig; alias_matcher.reload()
    print("  stoplisted_surface_dropped OK")


def test_nonstoplisted_ticker_kept():
    from core import alias_matcher
    _orig = settings.AMBIGUOUS_ALIASES_PATH
    with tempfile.TemporaryDirectory() as td:
        ad = Path(td) / "aliases"; ad.mkdir(); _write_aliases(ad)
        sp = Path(td) / "stop.json"; sp.write_text(json.dumps(["KDC"]), encoding="utf-8")
        try:
            am = _reload_with(ad, sp)
            assert any(h.ticker == "ACV" for h in am.match_text("ACV bị phạt 270 triệu"))
        finally:
            settings.AMBIGUOUS_ALIASES_PATH = _orig; alias_matcher.reload()
    print("  nonstoplisted_ticker_kept OK")


def test_missing_stoplist_ok():
    from core import alias_matcher
    _orig = settings.AMBIGUOUS_ALIASES_PATH
    with tempfile.TemporaryDirectory() as td:
        ad = Path(td) / "aliases"; ad.mkdir(); _write_aliases(ad)
        try:
            am = _reload_with(ad, Path(td) / "nope.json")  # missing file
            assert any(h.ticker == "KDC" for h in am.match_text("Sai phạm KDC Bình Đa"))
        finally:
            settings.AMBIGUOUS_ALIASES_PATH = _orig; alias_matcher.reload()
    print("  missing_stoplist_ok OK")


def main():
    if sys.platform == "win32":
        try: sys.stdout.reconfigure(encoding="utf-8")
        except Exception: pass
    print("running stoplist tests…")
    test_stoplisted_surface_dropped()
    test_nonstoplisted_ticker_kept()
    test_missing_stoplist_ok()
    print("ALL OK")


if __name__ == "__main__":
    main()
