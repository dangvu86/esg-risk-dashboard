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
_ESG_SET = [t for t, _ in _ESG_TERMS]
_NOISE = kw.noise_terms()
_HIGH = kw.high_severity_terms()
_FINE = re.compile(r'(\d+[\.,]?\d*)\s*(tỷ|triệu)', re.IGNORECASE)


def _content(article: dict) -> str:
    return " ".join((article.get(f) or "") for f in ("title", "sapo", "body")).lower()


def _hits(text: str, terms) -> bool:
    return any(t.lower() in text for t in terms)


def _classify_type(text: str) -> str:
    score = {"E": 0, "S": 0, "G": 0}
    for term, typ in _ESG_TERMS:
        if term.lower() in text:
            score[typ] += 1
    # E beats G and S on strict majority
    if score["E"] > score["G"] and score["E"] > score["S"]:
        return "E"
    if score["S"] > score["E"] and score["S"] > score["G"]:
        return "S"
    # Tie-breaking: E > S > G (environment-specific terms outrank generic governance
    # terms such as "xử phạt" when counts are equal, e.g. E=1 G=1 → E)
    if score["E"] > 0 and score["E"] >= score["G"] and score["E"] >= score["S"]:
        return "E"
    if score["S"] > 0 and score["S"] >= score["G"]:
        return "S"
    if score["G"]:
        return "G"
    return "G"


def _severity(text: str) -> str:
    if _hits(text, _HIGH):
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
    if _hits(text, _NOISE) and not _hits(text, _HIGH):
        return Verdict(False, "noise", None, None)
    if not _hits(text, _ESG_SET):
        return Verdict(False, "non_esg", None, None)
    return Verdict(True, "esg", _classify_type(text), _severity(text))
