"""URL canonicalization + article_id extraction.

Goal: produce a stable dedup key per article across multiple sources
(Google News encoded link, Báo Mới redirect link, direct paper URL with
tracking params, AMP/mobile variants, http vs https, ...).

Two outputs per URL:
  - article_id     : "<domain>::<id>"  e.g. "vnexpress.net::4612345"
                     Extracted via per-domain regex on the canonical path.
                     This is the PRIMARY dedup key.
  - url_canonical  : normalized URL string.
                     Fallback dedup key when no article_id regex matches.

decode_google_url() resolves news.google.com/articles/<encoded> to the real
publisher URL via googlenewsdecoder. Caller decides when to call it (decoding
is a network call; the canonical functions accept either decoded or raw URLs).
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode


# Query-string params that uniquely identify an article and must be kept.
_KEEP_QUERY = {"id", "p", "ArticleId", "articleid", "story_fbid"}


# Per-domain regex to extract a stable article id from the path.
# Order matters: most specific patterns first within a domain.
_ID_PATTERNS = [
    # vnexpress.net  -> last 7+ digits before .html
    ("vnexpress.net",        re.compile(r"-(\d{7,})\.html?$")),
    # dantri.com.vn  -> 15+ digit timestamp-id before .htm
    ("dantri.com.vn",        re.compile(r"-(\d{15,})\.htm$")),
    # tuoitre.vn     -> 8+ digit id before .htm
    ("tuoitre.vn",           re.compile(r"-(\d{8,})\.htm$")),
    # cafef.vn       -> 15+ digit id before .chn
    ("cafef.vn",             re.compile(r"-(\d{15,})\.chn$")),
    # baodautu.vn    -> -d<id>.html or d<id>.html
    ("baodautu.vn",          re.compile(r"-d?(\d{5,})\.html?$")),
    # baomoi.com     -> -c<id>.epi
    ("baomoi.com",           re.compile(r"-c(\d+)\.epi$")),
    # soha.vn        -> -<id>.htm (10+ digits)
    ("soha.vn",              re.compile(r"-(\d{10,})\.htm$")),
    # vnbusiness.vn  -> -1\d{6,} suffix
    ("vnbusiness.vn",        re.compile(r"-(1\d{6,})\.?html?$")),
    # vietnamindex.vn-> -i<id>
    ("vietnamindex.vn",      re.compile(r"-i(\d+)\.html?$")),
    # daibieunhandan.vn -> -post<id>.html
    ("daibieunhandan.vn",    re.compile(r"-post(\d+)\.html?$")),
    # generic fallback: any -<digits>.{html,htm,chn,epi,aspx}$
    ("*",                    re.compile(r"-(\d{6,})\.(?:html?|htm|chn|epi|aspx)$")),
]


def _strip_host(host: str) -> str:
    """Lowercase host; strip leading www. / m. / amp. / mobile. / mobi.; drop port."""
    host = host.lower().split(":", 1)[0]
    for prefix in ("www.", "m.", "amp.", "mobile.", "mobi."):
        if host.startswith(prefix):
            host = host[len(prefix):]
            break
    return host


def _strip_path(path: str) -> str:
    """Drop AMP markers and trailing slash."""
    p = path
    # /amp/ prefix or /amp suffix
    if p.startswith("/amp/"):
        p = "/" + p[len("/amp/"):]
    if p.endswith("/amp"):
        p = p[:-4]
    if p.endswith("/amp/"):
        p = p[:-5]
    # collapse double slashes inside path
    while "//" in p:
        p = p.replace("//", "/")
    if len(p) > 1 and p.endswith("/"):
        p = p[:-1]
    return p


def _clean_query(query: str) -> str:
    """Drop tracking params; keep only whitelisted identifying params (sorted)."""
    if not query:
        return ""
    pairs = parse_qsl(query, keep_blank_values=False)
    kept = [(k, v) for k, v in pairs if k in _KEEP_QUERY]
    if not kept:
        return ""
    kept.sort()
    return urlencode(kept)


def canonicalize(url: str) -> str:
    """Return a normalized URL string. Empty string for invalid input.

    Does NOT decode Google News links; pass the decoded URL if you want a
    canonical form that points at the publisher.
    """
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
    except Exception:
        return ""
    if not parts.scheme or not parts.netloc:
        return ""
    scheme = "https"  # force https; HTTP variants point at the same article
    host = _strip_host(parts.netloc)
    path = _strip_path(parts.path or "/")
    query = _clean_query(parts.query or "")
    return urlunsplit((scheme, host, path, query, ""))


def domain_of(url: str) -> str:
    """Return canonical hostname (no www./m./amp.) or '' if URL invalid."""
    try:
        parts = urlsplit(url.strip())
    except Exception:
        return ""
    if not parts.netloc:
        return ""
    return _strip_host(parts.netloc)


def article_id(url: str) -> str:
    """Return '<domain>::<id>' or '' if no id pattern matches.

    The caller should fall back to url_canonical when this returns ''.
    """
    canon = canonicalize(url)
    if not canon:
        return ""
    parts = urlsplit(canon)
    dom = parts.netloc
    path = parts.path
    # Try domain-specific patterns first
    for d, rx in _ID_PATTERNS:
        if d == "*":
            continue
        if dom == d or dom.endswith("." + d):
            m = rx.search(path)
            if m:
                return f"{d}::{m.group(1)}"
    # Generic fallback
    generic = _ID_PATTERNS[-1][1]
    m = generic.search(path)
    if m:
        return f"{dom}::{m.group(1)}"
    return ""


def dedup_key(url: str) -> str:
    """Best available dedup key: article_id if extractable, else canonical URL."""
    aid = article_id(url)
    if aid:
        return aid
    return canonicalize(url)


# -------- Google News decoder (network call, cache externally if needed) --------

def is_google_news_url(url: str) -> bool:
    if not url:
        return False
    try:
        host = urlsplit(url).netloc.lower()
    except Exception:
        return False
    return host.endswith("news.google.com")


def decode_google_url(url: str, interval: float = 1.0, timeout_s: float = 15.0) -> str:
    """Resolve a news.google.com encoded link to the publisher URL.

    Returns '' on failure (including timeout). Each call hits Google; cache
    results at the caller.

    `timeout_s` guards against gnewsdecoder hanging — the upstream library
    does not set a request timeout, so a stuck connection would otherwise
    block the worker indefinitely. Uses SIGALRM (Unix-only) — on Windows
    falls back to no timeout protection.
    """
    if not is_google_news_url(url):
        return url
    try:
        from googlenewsdecoder import gnewsdecoder
    except ImportError:
        return ""

    import signal

    def _raise(signum, frame):
        raise TimeoutError("gnewsdecoder stuck")

    armed = False
    old = None
    if hasattr(signal, "SIGALRM"):
        try:  # ValueError on non-main thread; fall back to no-timeout there
            old = signal.signal(signal.SIGALRM, _raise)
            signal.alarm(max(1, int(timeout_s)))
            armed = True
        except (ValueError, OSError):
            armed = False

    try:
        r = gnewsdecoder(url, interval=interval)
    except (TimeoutError, Exception):
        r = None
    finally:
        if armed:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)

    if r and r.get("status") and r.get("decoded_url"):
        return r["decoded_url"]
    return ""
