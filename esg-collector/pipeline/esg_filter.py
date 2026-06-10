"""ESG verdict for one article — pure, no I/O. V2 ruleset (2026-06-10).

The original port of cloud-function/keyword_classifier.py had two measured
failure modes (full-DB audit: ~350-540 real violation articles suppressed,
≈20-30% of kept volume):

1) noise-override: ANY noise term anywhere vetoed the article unless a
   HIGH_SEVERITY term hit — so "xử phạt"-class events died to a single PR
   word, noise substrings fired inside longer ESG terms ("niêm yết" inside
   "hủy niêm yết") and inside negated phrases ("KHÔNG đạt chuẩn").
2) vocab gap: bare "vi phạm", "sự cố", "thu hồi … đất", "tuyên phạt"… were
   absent from the vocabulary entirely.

V2 layers (each validated by full-DB simulation against hand-labeled
samples — recall 30/35 real events, junk resurrection ~14%):

- STRONG_EVENT terms override noise like HIGH does, but are scanned in
  title+sapo ONLY (body is too often polluted by sidebar/related-news
  fragments), and are disarmed by a preceding positive-direction word
  ("không bị", "ngoại trừ", …).
- WEAK_TITLE terms count as ESG evidence only inside the title;
  QUALITY terms count in title+sapo. Neither overrides noise.
- Noise occurrences are ignored when nested inside a longer ESG-term match
  (overlap guard) or preceded by "không "/"chưa " (negation guard).
- The trailing " - <source>" segment is stripped from the title before
  classification, but ONLY when it matches the article's source field:
  blind stripping ate real content after internal hyphens
  ("Bia Sài Gòn - Hà Tĩnh vừa bị phạt").
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from config import keywords as kw

@dataclass(frozen=True)
class Verdict:
    keep: bool
    reason: str           # 'esg' | 'noise' | 'non_esg'
    esg_type: str | None  # E|S|G
    severity: str | None  # Cao|Trung bình


def _rx(terms) -> re.Pattern:
    """One alternation, longest-first so finditer consumes the longest term
    at each position — the overlap guard depends on that."""
    return re.compile("|".join(
        re.escape(t) for t in sorted(set(terms), key=len, reverse=True)))


def _rx_bounded(terms) -> re.Pattern:
    """Word-bounded alternation for the V2 tiers. Plain substring scan lets
    "hàng giả" fire inside "ngân HÀNG GIẢm lãi suất" — and these tiers can
    override noise, so they must not. Legacy vocab keeps substring semantics
    (changing it would shift the already-validated kept set)."""
    alt = "|".join(re.escape(t) for t in sorted(set(terms), key=len, reverse=True))
    return re.compile(rf"(?<!\w)(?:{alt})(?!\w)", re.UNICODE)


# Hoisted once at import (pure, read-only) — classify() runs per-article in a loop.
_ESG_TERMS_LC = [(t.lower(), tag) for t, tag in kw.esg_terms()]
_STRONG_LC    = [(t.lower(), tag) for t, tag in kw.strong_event_terms()]
_WTITLE_LC    = [(t.lower(), tag) for t, tag in kw.weak_title_terms()]
_QUALITY_LC   = [(t.lower(), tag) for t, tag in kw.quality_terms()]
_NOISE_LC     = [t.lower() for t in kw.noise_terms()]
_HIGH_LC      = [t.lower() for t in kw.high_severity_terms()]
_POS_GUARDS   = tuple(g.lower() for g in kw.positive_guards())

_RX_OLD     = _rx(t for t, _ in _ESG_TERMS_LC)
_RX_STRONG  = _rx_bounded(t for t, _ in _STRONG_LC)
_RX_WTITLE  = _rx_bounded(t for t, _ in _WTITLE_LC)
_RX_QUALITY = _rx_bounded(t for t, _ in _QUALITY_LC)
_RX_NOISE   = _rx(_NOISE_LC)
_RX_HIGH    = _rx(_HIGH_LC)

_FINE = re.compile(r'(\d+[\.,]?\d*)\s*(tỷ|triệu)', re.IGNORECASE)
# "Phạt Công ty cổ phần Đường Quảng Ngãi gần 750 triệu" — company names push
# the amount well past 30 chars, hence the 60-char window.
_FINEPROX = re.compile(r'(?<!\w)phạt[^.;:]{0,60}?\d[\d.,]*\s*(?:tỷ|triệu)', re.UNICODE)
# "thu hồi hơn 11.000m2 đất" — the gap may contain digits with dots, so only
# clause punctuation breaks the match.
_THUHOI = re.compile(r'(?<!\w)thu hồi[^,;:]{0,30}?đất(?!\w)', re.UNICODE)
_NEG = ("không ", "chưa ")
_SUFFIX_RX = re.compile(r"\s+[-–|]\s+[^-–|]{2,60}\s*$")


def _strip_source_suffix(title: str, source: str) -> str:
    """Drop a trailing ' - X' only when X matches the source field, so feed
    suffixes like '- Báo Thanh tra' can't inject ESG terms ('thanh tra')."""
    if not title or not source:
        return title or ""
    m = _SUFFIX_RX.search(title)
    if not m:
        return title
    suf = m.group(0).strip(" \t-–|").lower().replace(" ", "")
    src = source.strip().lower().replace(" ", "")
    if suf and src and (suf in src or src in suf):
        return title[: m.start()]
    return title


def _strong_spans(ts: str) -> list[tuple[int, int]]:
    spans = []
    for m in _RX_STRONG.finditer(ts):
        back = ts[max(0, m.start() - 25):m.start()]
        if any(g in back for g in _POS_GUARDS):
            continue          # "không bị … áp thuế chống bán phá giá"
        spans.append(m.span())
    for pat in (_FINEPROX, _THUHOI):
        m = pat.search(ts)
        if m:
            spans.append(m.span())
    return spans


def _classify_type(text: str, tclean: str, ts: str) -> str:
    score = {"E": 0, "S": 0, "G": 0}
    for term, typ in _ESG_TERMS_LC:    # legacy scoring first — keeps E/S/G
        if term in text:               # assignment identical for old keeps
            score[typ] += 1
    if not any(score.values()):        # kept purely via the V2 vocabulary
        for term, typ in _STRONG_LC:
            if term in ts:
                score[typ] += 1
        for term, typ in _WTITLE_LC:
            if term in tclean:
                score[typ] += 1
        for term, typ in _QUALITY_LC:
            if term in ts:
                score[typ] += 1
    # Strict majority first
    if score["E"] > score["G"] and score["E"] > score["S"]:
        return "E"
    if score["S"] > score["E"] and score["S"] > score["G"]:
        return "S"
    # Tie / no strict winner: prefer specific categories over generic
    # governance verbs (xử phạt, thanh tra…) which co-occur with E/S events.
    if score["E"] > 0:
        return "E"
    if score["S"] > 0:
        return "S"
    return "G"


def _severity(text: str) -> str:
    if _RX_HIGH.search(text):
        return "Cao"
    m = _FINE.search(text)
    if m:
        amt = float(m.group(1).replace(",", "."))
        unit = m.group(2).lower()
        if unit == "tỷ" or (unit == "triệu" and amt >= 500):
            return "Cao"
    return "Trung bình"


def classify(article: dict) -> Verdict:
    tclean = _strip_source_suffix(
        article.get("title") or "", article.get("source") or "").lower()
    sapo = (article.get("sapo") or "").lower()
    body = (article.get("body") or "").lower()
    ts = f"{tclean} {sapo}"            # scope for strong/quality terms
    text = f"{ts} {body}"              # scope for legacy vocab + noise

    strong = _strong_spans(ts)
    esg_any = bool(strong) or bool(_RX_OLD.search(text)) \
        or bool(_RX_WTITLE.search(tclean)) or bool(_RX_QUALITY.search(ts))

    if not strong and not _RX_HIGH.search(text) and _RX_NOISE.search(text):
        # ts is a prefix of text, so title/sapo spans index text directly.
        spans = []
        if esg_any:
            spans = [m.span() for m in _RX_OLD.finditer(text)]
            spans += [m.span() for m in _RX_WTITLE.finditer(tclean)]
            spans += [m.span() for m in _RX_QUALITY.finditer(ts)]
        for m in _RX_NOISE.finditer(text):
            s, e = m.span()
            if any(s0 <= s and e <= e0 for s0, e0 in spans):
                continue               # overlap guard: inside a longer ESG term
            if any(n in text[max(0, s - 12):s] for n in _NEG):
                continue               # negation guard: "không đạt chuẩn"
            return Verdict(False, "noise", None, None)

    if not esg_any:
        return Verdict(False, "non_esg", None, None)
    return Verdict(True, "esg", _classify_type(text, tclean, ts), _severity(text))
