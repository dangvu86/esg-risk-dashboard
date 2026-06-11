"""Rematch-redesign tests — no network. Run:  python -m tests.test_rematch

  - matcher equivalence: new alias_matcher vs a frozen copy of the old
    per-alias matcher, over tests/fixtures/matcher_corpus.jsonl
  - chunked rematch correctness (Task 2.x)
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import settings  # noqa: E402

_FIELDS = ("title", "description", "sapo", "body")
_STRONG_FIELDS = ("names", "subsidiaries", "projects")
_WEAK_FIELDS = ("locations",)


# ---- frozen copy of the OLD matcher (reference implementation) ----
def _legacy_compile(alias: str) -> re.Pattern:
    esc = re.escape(alias.strip())
    return re.compile(rf"(?<!\w){esc}(?!\w)", re.IGNORECASE | re.UNICODE)


def _legacy_index(aliases_dir: Path):
    try:
        stop = {str(s).strip().upper() for s in
                json.loads(settings.AMBIGUOUS_ALIASES_PATH.read_text(encoding="utf-8"))}
    except (OSError, json.JSONDecodeError, TypeError, AttributeError):
        stop = set()
    index = {}
    for p in sorted(Path(aliases_dir).glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        ticker = (data.get("ticker") or p.stem).upper()
        items, seen = [], set()
        for field, weight in [(f, 1.0) for f in _STRONG_FIELDS] + [(f, 0.3) for f in _WEAK_FIELDS]:
            for a in data.get(field) or []:
                a = (a or "").strip()
                if not a or len(a) < 2 or a.lower() in seen:
                    continue
                if a.upper() in stop:        # mirror Fix 1+A
                    continue
                seen.add(a.lower())
                items.append((a, weight, _legacy_compile(a)))
        index[ticker] = items
    return index


def _legacy_match_article(index, article, include_weak=False):
    final = {}
    for field in _FIELDS:
        text = article.get(field) or ""
        if not text:
            continue
        for ticker, aliases in index.items():
            if ticker in final:
                continue
            for alias, weight, rx in aliases:
                if not include_weak and weight < 1.0:
                    continue
                if rx.search(text):
                    final[ticker] = (ticker, field)
                    break
    return {v for v in final.values()}


def _load_corpus():
    path = ROOT / "tests" / "fixtures" / "matcher_corpus.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_matcher_equivalence() -> None:
    from core import alias_matcher
    alias_matcher.reload()
    legacy = _legacy_index(settings.ALIASES_DIR)
    corpus = _load_corpus()
    # Cover multiple field placements + both weak tiers so the _NESTED recovery
    # and the weak-alias path are guarded, not just single-field/strong-only.
    configs = [
        (lambda t: {"title": t}, False),
        (lambda t: {"body": t}, False),
        (lambda t: {"description": t}, True),
    ]
    divergences = []
    for row in corpus:
        for make_art, weak in configs:
            art = make_art(row["text"])
            new = {(h.ticker, h.location)
                   for h in alias_matcher.match_article(art, include_weak=weak)}
            old = _legacy_match_article(legacy, art, include_weak=weak)
            if new != old:
                divergences.append((weak, row["text"], sorted(old), sorted(new)))
    for weak, text, old, new in divergences:
        print(f"  DIVERGENCE (weak={weak}): {text!r}\n    old={old}\n    new={new}")
    assert not divergences, f"{len(divergences)} (ticker,location) divergences"
    print("  matcher_equivalence OK")


def test_fetch_by_ids() -> None:
    from core import storage
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "f.db"
        storage.init_db(db)
        conn = storage.connect(db)
        for i in range(5):
            storage.insert_article(conn, {
                "article_id": f"d::{i}", "url_canonical": f"u{i}", "url_original": f"u{i}",
                "domain": "d", "title": f"t{i}", "backend": "google_rss",
                "group_key": "kw", "sub_query_ix": 0})
        ids = [f"d::{i}" for i in range(5)]
        rows = storage.fetch_articles_by_ids(conn, ids)
        assert {r["article_id"] for r in rows} == set(ids), rows
        # sub-chunking: force tiny chunk size, still returns all
        rows2 = storage.fetch_articles_by_ids(conn, ids, chunk=2)
        assert {r["article_id"] for r in rows2} == set(ids)
        assert storage.fetch_articles_by_ids(conn, []) == []
        conn.close()
    print("  fetch_by_ids OK")


def test_chunked_rematch() -> None:
    from core import storage, alias_matcher
    from pipeline import match
    from config import settings
    alias_matcher.reload()
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "r.db"
        _orig_pt, _orig_bs = settings.PER_TICKER_DIR, match.BATCH_SIZE
        try:
            settings.PER_TICKER_DIR = Path(td) / "pt"; settings.PER_TICKER_DIR.mkdir()
            match.BATCH_SIZE = 2  # force multiple batches over 3 articles
            storage.init_db(db)
            conn = storage.connect(db)
            storage.insert_article(conn, {"article_id":"a::1","url_canonical":"u1","url_original":"u1",
                "domain":"d","title":"Xử phạt Dabaco 300 triệu vì vi phạm môi trường","title_hash":"h1",
                "backend":"google_rss","group_key":"alias","sub_query_ix":0,"body_status":"fetched"})
            storage.insert_article(conn, {"article_id":"a::2","url_canonical":"u2","url_original":"u2",
                "domain":"d","title":"Cổ đông Dabaco nhận cổ tức","title_hash":"h2",
                "backend":"google_rss","group_key":"alias","sub_query_ix":0,"body_status":"fetched"})
            storage.insert_article(conn, {"article_id":"a::3","url_canonical":"u3","url_original":"u3",
                "domain":"d","title":"Xử phạt công ty xả thải ra môi trường","title_hash":"h3",
                "backend":"google_rss","group_key":"alias","sub_query_ix":0,"body_status":"fetched"})
            status = Path(td) / "status.json"
            counts = match.run(db_path=db, rematch_all=True, status_json=str(status))
            assert counts["matched"] >= 1, counts
            # a::1 (Dabaco + ESG) kept; a::2 noise dropped
            rows = {r["article_id"]: r for r in conn.execute("SELECT * FROM articles")}
            assert rows["a::1"]["esg_status"] == "esg"
            assert rows["a::2"]["esg_status"] == "noise"
            doc = json.loads((settings.PER_TICKER_DIR / "DBC.json").read_text(encoding="utf-8"))
            assert "a::1" in {a["article_id"] for a in doc["articles"]}
            # status file written with counts
            st = json.loads(status.read_text(encoding="utf-8"))
            assert st["matched"] == counts["matched"]
            conn.close()
        finally:
            settings.PER_TICKER_DIR, match.BATCH_SIZE = _orig_pt, _orig_bs
    print("  chunked_rematch OK")


def main() -> None:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    print("running rematch tests…")
    test_matcher_equivalence()
    test_fetch_by_ids()
    test_chunked_rematch()
    print("ALL OK")


if __name__ == "__main__":
    main()
