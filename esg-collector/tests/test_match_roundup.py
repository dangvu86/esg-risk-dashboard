"""Roundup gate (Fix B). Run: python -m tests.test_match_roundup"""
from __future__ import annotations
import json, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import settings  # noqa: E402


def _in_pt(pt_dir: Path, tk: str, aid: str) -> bool:
    p = pt_dir / f"{tk}.json"
    if not p.exists():
        return False
    return aid in {a["article_id"] for a in json.loads(p.read_text("utf-8"))["articles"]}


def test_roundup_drops_nontitle_keeps_title():
    # match.run() reloads the REAL alias pool (match.py:164), so we must use real
    # distinctive names: Vinhomes->VHM, Novaland->NVL, Sacombank->STB. Companies
    # go in DESCRIPTION (a Stage-1 field the matcher scans, so all enter `hits`);
    # the ESG keyword goes in the TITLE (esg_filter reads title+sapo+body, NOT
    # description) so classify().keep is True. body_status='skipped' (terminal,
    # so an emptied hit list routes to 'unmatched').
    from core import storage, alias_matcher
    from pipeline import match
    _opt, _obs = settings.PER_TICKER_DIR, match.BATCH_SIZE
    with tempfile.TemporaryDirectory() as td:
        try:
            settings.PER_TICKER_DIR = Path(td) / "pt"; settings.PER_TICKER_DIR.mkdir()
            match.BATCH_SIZE = 10
            db = Path(td) / "m.db"; storage.init_db(db); conn = storage.connect(db)
            # (1) roundup: 3 companies in description, none in title -> all dropped
            storage.insert_article(conn, {"article_id": "r::1", "url_canonical": "u1",
                "url_original": "u1", "domain": "d", "title": "Thanh tra phát hiện sai phạm",
                "description": "Liên quan Vinhomes, Novaland và Sacombank.",
                "title_hash": "h1", "backend": "google_rss", "group_key": "kw",
                "sub_query_ix": 0, "body_status": "skipped"})
            # (2) 3 companies, ONE (Vinhomes) in title -> only the title one kept
            storage.insert_article(conn, {"article_id": "r::2", "url_canonical": "u2",
                "url_original": "u2", "domain": "d",
                "title": "Vinhomes bị xử phạt vì vi phạm",
                "description": "Novaland và Sacombank cũng liên quan.",
                "title_hash": "h2", "backend": "google_rss", "group_key": "kw",
                "sub_query_ix": 0, "body_status": "skipped"})
            # (3) only 2 companies -> untouched (both kept)
            storage.insert_article(conn, {"article_id": "r::3", "url_canonical": "u3",
                "url_original": "u3", "domain": "d", "title": "Xử phạt vi phạm môi trường",
                "description": "Liên quan Vinhomes và Novaland.",
                "title_hash": "h3", "backend": "google_rss", "group_key": "kw",
                "sub_query_ix": 0, "body_status": "skipped"})
            conn.close()

            match.run(db_path=db)
            pt = settings.PER_TICKER_DIR

            # (1) roundup, none in title -> no attribution
            assert not _in_pt(pt, "VHM", "r::1")
            assert not _in_pt(pt, "NVL", "r::1")
            assert not _in_pt(pt, "STB", "r::1")
            # (2) only the title company kept (gate truly exercised: 3 Stage-1 hits)
            assert _in_pt(pt, "VHM", "r::2")
            assert not _in_pt(pt, "NVL", "r::2")
            assert not _in_pt(pt, "STB", "r::2")
            # (3) 2-company article -> both kept
            assert _in_pt(pt, "VHM", "r::3")
            assert _in_pt(pt, "NVL", "r::3")
        finally:
            settings.PER_TICKER_DIR, match.BATCH_SIZE = _opt, _obs
            alias_matcher.reload()
    print("  roundup_drops_nontitle_keeps_title OK")


def main():
    if sys.platform == "win32":
        try: sys.stdout.reconfigure(encoding="utf-8")
        except Exception: pass
    print("running roundup gate test…")
    test_roundup_drops_nontitle_keeps_title()
    print("ALL OK")


if __name__ == "__main__":
    main()
