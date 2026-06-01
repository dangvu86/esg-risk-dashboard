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
    divergences = []
    for row in corpus:
        art = {"title": row["text"]}  # single field keeps location deterministic
        new = {(h.ticker, h.location) for h in alias_matcher.match_article(art)}
        old = _legacy_match_article(legacy, art)
        if new != old:
            divergences.append((row["text"], sorted(old), sorted(new)))
    for text, old, new in divergences:
        print(f"  DIVERGENCE: {text!r}\n    old={old}\n    new={new}")
    assert not divergences, f"{len(divergences)} (ticker,location) divergences"
    print("  matcher_equivalence OK")


def main() -> None:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    print("running rematch tests…")
    test_matcher_equivalence()
    print("ALL OK")


if __name__ == "__main__":
    main()
