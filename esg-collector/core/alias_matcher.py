"""Match free text against the alias pool to determine which tickers it covers.

Aliases live in `config/aliases/<TICKER>.json`. Loaded once at import (also via
`reload()`). All strong aliases (names/subsidiaries/projects) are compiled into
ONE longest-first alternation; weak aliases (locations) into a second one. Each
text field is scanned with a single consuming `finditer`, and the matched alias
text is mapped back to its owning ticker(s). A precomputed `_NESTED` map also
emits the tickers of any shorter alias that is a word-bounded substring of the
matched alias, so overlapping matches a non-overlapping scan would drop are
recovered — keeping the result exactly equivalent to independent per-alias search.

Strong aliases: names, subsidiaries, projects (weight 1.0).
Weak  aliases: locations (weight 0.3, filtered out by default).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from config import settings


@dataclass(frozen=True)
class AliasHit:
    ticker: str
    alias: str
    location: str    # title | description | sapo | body
    weight: float    # 1.0 strong, 0.3 weak


_STRONG_FIELDS = ("names", "subsidiaries", "projects")
_WEAK_FIELDS = ("locations",)

_OWNERS: dict[str, list[tuple[str, str, float]]] = {}
_NESTED: dict[str, list[tuple[str, str, float]]] = {}
_TICKERS: set[str] = set()
_PATTERN_STRONG: re.Pattern | None = None
_PATTERN_ALL: re.Pattern | None = None


def _build_pattern(aliases: set[str]) -> re.Pattern | None:
    if not aliases:
        return None
    ordered = sorted(aliases, key=len, reverse=True)
    alt = "|".join(re.escape(a) for a in ordered)
    return re.compile(rf"(?<!\w)(?:{alt})(?!\w)", re.IGNORECASE | re.UNICODE)


def _bounded(needle: str, haystack: str) -> bool:
    rx = re.compile(rf"(?<!\w){re.escape(needle)}(?!\w)", re.IGNORECASE | re.UNICODE)
    return rx.search(haystack) is not None


def reload(aliases_dir: Path = settings.ALIASES_DIR) -> None:
    global _PATTERN_STRONG, _PATTERN_ALL
    _OWNERS.clear()
    _NESTED.clear()
    _TICKERS.clear()
    strong: set[str] = set()
    alla: set[str] = set()
    for p in sorted(Path(aliases_dir).glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        ticker = (data.get("ticker") or p.stem).upper()
        _TICKERS.add(ticker)
        seen: set[str] = set()
        for field, weight in [(f, 1.0) for f in _STRONG_FIELDS] + [(f, 0.3) for f in _WEAK_FIELDS]:
            for a in data.get(field) or []:
                a = (a or "").strip()
                if not a or len(a) < 2 or a.lower() in seen:
                    continue
                seen.add(a.lower())
                _OWNERS.setdefault(a.lower(), []).append((ticker, a, weight))
                alla.add(a)
                if weight >= 1.0:
                    strong.add(a)
    _PATTERN_STRONG = _build_pattern(strong)
    _PATTERN_ALL = _build_pattern(alla)
    al = sorted(alla, key=len)
    for i, b in enumerate(al):
        bl = b.lower()
        b_owners = _OWNERS.get(bl, ())
        for a in al[i + 1:]:
            if len(a) <= len(b):
                continue
            if bl in a.lower() and _bounded(b, a):
                _NESTED.setdefault(a.lower(), []).extend(b_owners)


def loaded_tickers() -> list[str]:
    return sorted(_TICKERS)


def match_text(text: str, *, include_weak: bool = False) -> list[AliasHit]:
    if not text:
        return []
    pattern = _PATTERN_ALL if include_weak else _PATTERN_STRONG
    if pattern is None:
        return []
    found: dict[str, AliasHit] = {}
    for m in pattern.finditer(text):
        key = m.group().lower()
        for ticker, alias, weight in (*_OWNERS.get(key, ()), *_NESTED.get(key, ())):
            if not include_weak and weight < 1.0:
                continue
            if ticker not in found:
                found[ticker] = AliasHit(ticker, alias, "", weight)
    return list(found.values())


def match_article(
    article: dict,
    *,
    fields: tuple[str, ...] = ("title", "description", "sapo", "body"),
    include_weak: bool = False,
) -> list[AliasHit]:
    """Return ≤1 hit per ticker. Location = first field where the alias appeared."""
    final: dict[str, AliasHit] = {}
    for field in fields:
        text = article.get(field) or ""
        if not text:
            continue
        for hit in match_text(text, include_weak=include_weak):
            if hit.ticker in final:
                continue
            final[hit.ticker] = AliasHit(hit.ticker, hit.alias, field, hit.weight)
    return list(final.values())


# auto-load on import
reload()
