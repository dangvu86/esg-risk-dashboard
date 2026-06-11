"""Main-article extraction from raw HTML via trafilatura.

Replaces the old "Jina full-page markdown + line-strip" approach. trafilatura
is a purpose-built boilerplate remover: it isolates the article body and drops
nav / ad banners / related-news lists / footer across arbitrary VN news sites
with no per-site configuration. This is what stops Tier-2 alias matching from
hitting stray company names buried in page chrome (the body-match FP class).
"""
from __future__ import annotations

import trafilatura

_MIN_BODY = 200  # below this, treat as an extraction miss (e.g. paywall/empty)


def extract_main(html: str | None) -> str | None:
    """Return the clean article text, or None if extraction yields too little.

    `favor_precision` biases trafilatura toward dropping borderline blocks
    (teasers, related widgets) over keeping them — we want precision here so a
    stray company name in a recommendation block never reaches the matcher.
    """
    if not html:
        return None
    text = trafilatura.extract(
        html,
        favor_precision=True,
        include_comments=False,
        include_tables=False,
    )
    if not text:
        return None
    text = text.strip()
    return text if len(text) >= _MIN_BODY else None
