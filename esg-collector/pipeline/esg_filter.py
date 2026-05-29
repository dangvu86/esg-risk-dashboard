"""ESG verdict for one article — pure, no I/O. Ports the noise/ESG/severity
logic of cloud-function/keyword_classifier.py onto title+sapo+body."""
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

# Hoisted once at import (pure, read-only) — classify() runs per-article in a loop.
_ESG_TERMS = kw.esg_terms()                 # [(term, tag)]
_ESG_TERMS_LC = [(t.lower(), tag) for t, tag in _ESG_TERMS]   # for _classify_type
_ESG_SET_LC = [t for t, _ in _ESG_TERMS_LC]
_NOISE = kw.noise_terms()
_NOISE_LC = [t.lower() for t in _NOISE]
_HIGH = kw.high_severity_terms()
_HIGH_LC = [t.lower() for t in _HIGH]
_FINE = re.compile(r'(\d+[\.,]?\d*)\s*(tỷ|triệu)', re.IGNORECASE)


def _content(article: dict) -> str:
    return " ".join((article.get(f) or "") for f in ("title", "sapo", "body")).lower()


def _hits(text: str, terms) -> bool:
    return any(t in text for t in terms)


def _classify_type(text: str) -> str:
    score = {"E": 0, "S": 0, "G": 0}
    for term, typ in _ESG_TERMS_LC:
        if term in text:           # text & terms are pre-lowercased (_LC module constants above)
            score[typ] += 1
    # Strict majority first
    if score["E"] > score["G"] and score["E"] > score["S"]:
        return "E"
    if score["S"] > score["E"] and score["S"] > score["G"]:
        return "S"
    # Tie / no strict winner: prefer specific categories over generic governance.
    # Generic governance verbs (xử phạt, thanh tra, vi phạm) co-occur with E and S
    # events, so they must NOT win ties. Order: E > S > G.
    if score["E"] > 0:
        return "E"
    if score["S"] > 0:
        return "S"
    return "G"


def _severity(text: str) -> str:
    if _hits(text, _HIGH_LC):
        return "Cao"
    m = _FINE.search(text)
    if m:
        amt = float(m.group(1).replace(",", "."))
        unit = m.group(2).lower()
        if unit == "tỷ" or (unit == "triệu" and amt >= 500):
            return "Cao"
    return "Trung bình"


def classify(article: dict) -> Verdict:
    text = _content(article)
    if _hits(text, _NOISE_LC) and not _hits(text, _HIGH_LC):
        return Verdict(False, "noise", None, None)
    if not _hits(text, _ESG_SET_LC):
        return Verdict(False, "non_esg", None, None)
    return Verdict(True, "esg", _classify_type(text), _severity(text))
