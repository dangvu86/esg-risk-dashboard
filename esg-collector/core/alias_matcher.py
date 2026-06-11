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
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from config import settings

log = logging.getLogger("alias_matcher")


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
_PATTERN_BLOCKED: re.Pattern | None = None  # context spans that swallow alias hits
_STOPLIST: set[str] = set()  # upper-cased surface forms never matched (Fix 1+A)


def _load_stoplist() -> set[str]:
    try:
        data = json.loads(settings.AMBIGUOUS_ALIASES_PATH.read_text(encoding="utf-8"))
        return {str(s).strip().upper() for s in data if str(s).strip()}
    except FileNotFoundError:
        return set()
    except (OSError, json.JSONDecodeError, TypeError) as e:
        log.warning("ambiguous_aliases stoplist unreadable (%s) — ignoring", e)
        return set()


def _load_blocked_contexts() -> set[str]:
    """Phrases whose span suppresses any alias match fully inside it.

    The stoplist can't express these: dropping the alias kills it everywhere,
    but e.g. "Hòa Phát" (HPG's main short name) is only wrong inside
    "Khánh Hòa phát hiện/phát động/..." — a province + verb phrase boundary."""
    try:
        path = getattr(settings, "BLOCKED_CONTEXTS_PATH", None)
        if path is None:
            return set()
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return {str(s).strip() for s in data if str(s).strip()}
    except FileNotFoundError:
        return set()
    except (OSError, json.JSONDecodeError, TypeError) as e:
        log.warning("blocked_contexts unreadable (%s) — ignoring", e)
        return set()


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
    global _PATTERN_STRONG, _PATTERN_ALL, _PATTERN_BLOCKED, _STOPLIST
    _STOPLIST = _load_stoplist()
    _PATTERN_BLOCKED = _build_pattern(_load_blocked_contexts())
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
                if a.upper() in _STOPLIST:      # Fix 1+A: drop collision surface
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


def match_text(text: str, *, include_weak: bool = False,
               allow_bare_ticker: bool = True) -> list[AliasHit]:
    if not text:
        return []
    pattern = _PATTERN_ALL if include_weak else _PATTERN_STRONG
    if pattern is None:
        return []
    blocked: list[tuple[int, int]] = (
        [(b.start(), b.end()) for b in _PATTERN_BLOCKED.finditer(text)]
        if _PATTERN_BLOCKED else [])
    found: dict[str, AliasHit] = {}
    for m in pattern.finditer(text):
        if any(s <= m.start() and m.end() <= e for s, e in blocked):
            continue   # hit lives inside a blocked context span
        key = m.group().lower()
        for ticker, alias, weight in (*_OWNERS.get(key, ()), *_NESTED.get(key, ())):
            if not include_weak and weight < 1.0:
                continue
            if not allow_bare_ticker and alias.upper() == ticker:
                # Bare ticker codes are too weak as BODY evidence: incidental
                # mentions ("tài khoản ngân hàng của Thân (MB, VCB, VIB)")
                # attribute fraud stories to the banks. Title/desc/sapo keep
                # them (a bare ticker in a headline is usually aboutness).
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
        for hit in match_text(text, include_weak=include_weak,
                              allow_bare_ticker=(field != "body")):
            if hit.ticker in final:
                continue
            final[hit.ticker] = AliasHit(hit.ticker, hit.alias, field, hit.weight)
    return list(final.values())


# auto-load on import
reload()
