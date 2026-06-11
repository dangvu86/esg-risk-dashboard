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


def test_blocked_context_swallows_hit():
    """An alias hit fully inside a blocked-context span must be suppressed:
    "Khánh Hòa phát hiện..." is province news, not HPG's "Hòa Phát"."""
    from core import alias_matcher
    _orig_stop = settings.AMBIGUOUS_ALIASES_PATH
    _orig_blocked = settings.BLOCKED_CONTEXTS_PATH
    with tempfile.TemporaryDirectory() as td:
        ad = Path(td) / "aliases"; ad.mkdir()
        (ad / "HPG.json").write_text(json.dumps(
            {"ticker": "HPG", "names": ["HPG", "Hòa Phát"],
             "subsidiaries": [], "projects": [], "locations": []},
            ensure_ascii=False), encoding="utf-8")
        bp = Path(td) / "blocked.json"
        bp.write_text(json.dumps(["Khánh Hòa phát"], ensure_ascii=False), encoding="utf-8")
        try:
            settings.AMBIGUOUS_ALIASES_PATH = Path(td) / "nostop.json"  # none
            settings.BLOCKED_CONTEXTS_PATH = bp
            alias_matcher.reload(ad)
            # inside the blocked span → suppressed
            assert not alias_matcher.match_text("Khánh Hòa phát hiện lưới bẫy chim hoang dã")
            assert not alias_matcher.match_text("Mỗi ngày Khánh Hòa phát sinh 226 tấn rác")
            # normal mentions still match
            assert any(h.ticker == "HPG" for h in
                       alias_matcher.match_text("Hòa Phát xây nhà máy mới tại Dung Quất"))
            # blocked phrase broken by punctuation → alias is NOT inside the span
            assert any(h.ticker == "HPG" for h in
                       alias_matcher.match_text("Khánh Hòa: Hòa Phát khởi công khu liên hợp"))
        finally:
            settings.AMBIGUOUS_ALIASES_PATH = _orig_stop
            settings.BLOCKED_CONTEXTS_PATH = _orig_blocked
            alias_matcher.reload()
    print("  blocked_context_swallows_hit OK")


def test_bare_ticker_blocked_in_body():
    """A bare ticker code must not attribute via BODY text (incidental
    mentions like bank-account lists), but stays valid in title/desc/sapo,
    and the full company name still matches anywhere including body."""
    from core import alias_matcher
    _orig = settings.AMBIGUOUS_ALIASES_PATH
    with tempfile.TemporaryDirectory() as td:
        ad = Path(td) / "aliases"; ad.mkdir()
        (ad / "VCB.json").write_text(json.dumps(
            {"ticker": "VCB", "names": ["VCB", "Vietcombank"],
             "subsidiaries": [], "projects": [], "locations": []},
            ensure_ascii=False), encoding="utf-8")
        try:
            settings.AMBIGUOUS_ALIASES_PATH = Path(td) / "nostop.json"
            alias_matcher.reload(ad)
            art = {"title": "Ông chủ Triệu nụ cười lừa đảo: khởi tố thêm 3 bị can",
                   "description": "", "sapo": "",
                   "body": "tiền chuyển đến các tài khoản ngân hàng của Thân (MB, VCB, VIB)."}
            assert not alias_matcher.match_article(art)        # bare VCB in body → no hit
            art2 = dict(art, title="VCB bị xử phạt vì vi phạm công bố thông tin")
            hits = alias_matcher.match_article(art2)
            assert any(h.ticker == "VCB" and h.location == "title" for h in hits)
            art3 = dict(art, body="Vietcombank bị thanh tra kết luận sai phạm tín dụng.")
            hits = alias_matcher.match_article(art3)
            assert any(h.ticker == "VCB" and h.location == "body" for h in hits)
        finally:
            settings.AMBIGUOUS_ALIASES_PATH = _orig; alias_matcher.reload()
    print("  bare_ticker_blocked_in_body OK")


def main():
    if sys.platform == "win32":
        try: sys.stdout.reconfigure(encoding="utf-8")
        except Exception: pass
    print("running stoplist tests…")
    test_stoplisted_surface_dropped()
    test_nonstoplisted_ticker_kept()
    test_missing_stoplist_ok()
    test_blocked_context_swallows_hit()
    test_bare_ticker_blocked_in_body()
    print("ALL OK")


if __name__ == "__main__":
    main()
