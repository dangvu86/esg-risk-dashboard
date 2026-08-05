"""Title normalisation + hashing for cross-backend deduplication.

Two articles about the same story may arrive via Google News (encoded URL) and
BaoMoi (own ID). Their `article_id`
differs, but the headline is usually identical or nearly so. We compute a
stable hash of the normalised title so that an INSERT can fall back to a
"same title + same publish date → same story" check after the article_id
dedup fails.

Normalisation strips:
  - Vietnamese diacritics (NFD decomposition)
  - Punctuation, multiple spaces, leading/trailing whitespace
  - Trailing source-name suffix after the last " - " or " | " when the
    suffix is short (≤ 60 chars) — Google News headlines often append
    "- Báo Lao Động", "| VnExpress", etc.
  - Capitalisation
The first 120 chars of the result are hashed (sha1, hex-encoded). 120 chars
keeps enough title context that distinct stories don't collide even after
truncation.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata


_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_WS_RE = re.compile(r"\s+")
_SUFFIX_RE = re.compile(r"\s*[\-\|–—]\s*[^\-\|–—]{1,60}$")


def normalise(title: str) -> str:
    """Return a comparable form of the title (lowercase ASCII, no punct)."""
    if not title:
        return ""
    s = title.strip()
    # Strip a trailing publisher suffix ("- Báo XYZ", "| ABC News"), up to 3 times
    # in case the publisher itself contains a separator.
    for _ in range(3):
        m = _SUFFIX_RE.search(s)
        if not m:
            break
        s = s[: m.start()].rstrip()
    s = s.lower()
    # NFD splits "ạ" → "a" + combining acute; remove combining marks
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    # Vietnamese đ / Đ are not handled by NFD; map manually
    s = s.replace("đ", "d").replace("ð", "d")
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


def title_hash(title: str) -> str:
    """Return the first 16 hex chars of sha1(normalised(title)[:120]).

    Empty / very short titles return '' so the caller can skip storing them
    (a 3-character normalised title would alias many unrelated articles).
    """
    n = normalise(title)
    if len(n) < 8:
        return ""
    return hashlib.sha1(n[:120].encode("utf-8")).hexdigest()[:16]
