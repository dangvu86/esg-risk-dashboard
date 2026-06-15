"""Body cleaning: isolate the *editorial* portion of a fetched article body so
the downstream stages (alias match, ESG filter, controversy LLM) never read
publisher boilerplate as if it were editorial content about the company.

Two non-editorial regions are removed:
  - related-news / recommendation link lists (markdown image/link bullets),
    which can appear mid-body — stripped line by line;
  - the trailing donation / charity-appeal + anti-scam-disclaimer footer that
    Vietnamese papers append to "hoàn cảnh khó khăn" pieces. That footer lists
    bank account numbers ("…Ngân hàng Vietcombank, hoặc số tài khoản…
    VietinBank") which match bank aliases, and a scam-warning block containing
    ESG trigger words ("lừa đảo") — both pure false positives. The footer is
    always a trailing block, so we cut from the earliest donation/disclaimer
    marker to the end. The markers are mostly genre-level (donation intent),
    so this generalises across outlets and every bank ticker; one
    ("báo vietnamnet khuyến cáo") is a publisher-keyed disclaimer line, kept
    because it is intent-specific enough to never hit real editorial text.

Pure + side-effect free for easy testing."""
from __future__ import annotations

import re

# A markdown list item that is an image/link entry = nav / related-news widget.
# `!` (image marker) is mandatory so a prose bullet merely starting with the
# word "Image" isn't dropped.
_LINK_LINE = re.compile(r"^[\*\-]\s*\[?!\[?Image", re.IGNORECASE)
_LINK_LIST = re.compile(r"^[\*\-]\s+\[.*\]\(https?://", re.IGNORECASE)

# Donation/charity-appeal + anti-scam-disclaimer markers. Matched
# case-insensitively. Each marks the start of the non-editorial footer — the
# real story always precedes it, so the earliest hit truncates the body.
# Every marker MUST carry donation/appeal/disclaimer *intent*: a bare phrase
# like "quét mã QR" is deliberately excluded because a real governance article
# can editorially describe scam victims ("quét mã QR giả"), and we must not
# truncate that. The charity footers these target are always introduced by an
# appeal line ("Bạn đọc giúp…", "Mọi sự giúp đỡ…") that cuts earlier anyway.
# Validated against the full corpus: only charity appeals hit.
_FOOTER_MARKERS = (
    "mọi sự giúp đỡ",
    "mọi đóng góp",
    "mọi sự ủng hộ",
    "bạn đọc giúp",
    "bạn đọc ủng hộ",
    "ủng hộ qua số tài khoản",
    "báo vietnamnet khuyến cáo",
    "cảnh báo thủ đoạn lừa đảo",
)
# One case-insensitive scan finds the leftmost (== earliest) marker — cheaper
# than a `.find()` per marker plus a full-body `.lower()` copy on the rematch
# hot path, and exactly equivalent.
_FOOTER_RX = re.compile("|".join(re.escape(m) for m in _FOOTER_MARKERS),
                        re.IGNORECASE | re.UNICODE)


def strip_related_blocks(body: str | None) -> str | None:
    if not body:
        return body
    kept = [
        line for line in body.splitlines()
        if not (_LINK_LINE.match(line.lstrip()) or _LINK_LIST.match(line.lstrip()))
    ]
    return "\n".join(kept).strip()


def strip_appeal_footer(body: str | None) -> str | None:
    """Cut a trailing donation/charity-appeal + scam-disclaimer footer.

    Returns the body unchanged when no marker is present (the common case)."""
    if not body:
        return body
    m = _FOOTER_RX.search(body)
    if not m:
        return body
    return body[:m.start()].rstrip()


def editorial_body(body: str | None) -> str | None:
    """The editorial view of a body: related-link lists removed and the trailing
    donation/scam-disclaimer footer cut. Idempotent; pure. Consumed by the alias
    matcher (body field), the ESG filter, and the controversy LLM so all three
    interpret the same boilerplate-free text."""
    return strip_appeal_footer(strip_related_blocks(body))
