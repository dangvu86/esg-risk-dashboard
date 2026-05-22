"""Match free text against the alias pool to determine which tickers it covers.

Aliases live in `config/aliases/<TICKER>.json`. We load once at import time
(also exposed via `reload()` for callers that hot-swap files). Each alias
becomes a compiled regex with case-insensitive Unicode word boundaries.

Strong aliases: names, subsidiaries, projects.
Weak  aliases: locations (filter out by default; they cause false positives
                like every news article mentioning a province).
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


# {ticker: [(alias_str, weight, compiled_regex), ...]}
_INDEX: dict[str, list[tuple[str, float, re.Pattern]]] = {}


def _compile(alias: str) -> re.Pattern:
    # `\b` doesn't match Vietnamese diacritics well in Python, so we use
    # an explicit non-word lookaround that treats Latin + Vietnamese chars
    # as "word".
    esc = re.escape(alias.strip())
    return re.compile(rf"(?<!\w){esc}(?!\w)", re.IGNORECASE | re.UNICODE)


def _load_one(path: Path) -> tuple[str, list[tuple[str, float, re.Pattern]]] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    ticker = (data.get("ticker") or path.stem).upper()
    aliases: list[tuple[str, float, re.Pattern]] = []
    seen: set[str] = set()
    for field in _STRONG_FIELDS:
        for a in data.get(field) or []:
            a = (a or "").strip()
            if not a or len(a) < 2 or a.lower() in seen:
                continue
            seen.add(a.lower())
            aliases.append((a, 1.0, _compile(a)))
    for field in _WEAK_FIELDS:
        for a in data.get(field) or []:
            a = (a or "").strip()
            if not a or len(a) < 2 or a.lower() in seen:
                continue
            seen.add(a.lower())
            aliases.append((a, 0.3, _compile(a)))
    return ticker, aliases


def reload(aliases_dir: Path = settings.ALIASES_DIR) -> None:
    _INDEX.clear()
    for p in sorted(Path(aliases_dir).glob("*.json")):
        loaded = _load_one(p)
        if loaded:
            ticker, items = loaded
            _INDEX[ticker] = items


def loaded_tickers() -> list[str]:
    return sorted(_INDEX.keys())


def match_text(text: str, *, include_weak: bool = False) -> list[AliasHit]:
    if not text:
        return []
    hits: list[AliasHit] = []
    for ticker, aliases in _INDEX.items():
        for alias, weight, rx in aliases:
            if not include_weak and weight < 1.0:
                continue
            if rx.search(text):
                hits.append(AliasHit(ticker, alias, "", weight))
                break  # one hit per ticker per text is enough
    return hits


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
