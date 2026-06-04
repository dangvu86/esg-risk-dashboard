"""Strip the related-news / recommendation link-list block Jina captures from a
full page, line by line, over the whole body (related blocks also appear
mid-body, so no positional cut). Pure + side-effect free for easy testing."""
from __future__ import annotations

import re

# A markdown list item that is an image/link entry = nav / related-news widget.
# `!` (image marker) is mandatory so a prose bullet merely starting with the
# word "Image" isn't dropped.
_LINK_LINE = re.compile(r"^[\*\-]\s*\[?!\[?Image", re.IGNORECASE)
_LINK_LIST = re.compile(r"^[\*\-]\s+\[.*\]\(https?://", re.IGNORECASE)


def strip_related_blocks(body: str | None) -> str | None:
    if not body:
        return body
    kept = [
        line for line in body.splitlines()
        if not (_LINK_LINE.match(line.lstrip()) or _LINK_LIST.match(line.lstrip()))
    ]
    return "\n".join(kept).strip()
