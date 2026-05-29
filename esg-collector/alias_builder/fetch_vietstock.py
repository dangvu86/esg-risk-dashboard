"""Bootstrap a minimal `config/aliases/<TICKER>.json` from Vietstock.

Vietstock's `ho-so-doanh-nghiep.htm` exposes the full corporate name and HQ
address in the static HTML. Subsidiaries come from the dedicated
`cong-ty-con-lien-doanh-lien-ket.htm` page (parsed by `parse_subsidiaries`).

This builder produces a baseline that's accurate enough for keyword matching:
  - `names`        : full corporate name + short brand + ticker
  - `locations`    : province / city parsed from HQ address (weak match)
  - `subsidiaries` : full names from the dedicated cong-ty-con page, plus
                     best-effort short tokens derived from them
  - `projects`     : empty (caller can enrich manually)

CLI:
  python -m alias_builder.fetch_vietstock --ticker DBC
  python -m alias_builder.fetch_vietstock --all                 # uses companies.csv
  python -m alias_builder.fetch_vietstock --all --force         # overwrite existing
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import re
import ssl
import time
import urllib.request
from pathlib import Path

from bs4 import BeautifulSoup

from config import settings
from config.companies import read_tickers


log = logging.getLogger("vietstock")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/121"
SSL_CTX = ssl.create_default_context()
SSL_CTX.options |= 0x4  # legacy renego — Vietstock needs this

PROVINCES = [
    "Hà Nội", "TP. HCM", "TP HCM", "Hồ Chí Minh", "Hải Phòng", "Đà Nẵng", "Cần Thơ",
    "Bắc Ninh", "Bắc Giang", "Bắc Kạn", "Cao Bằng", "Lạng Sơn", "Quảng Ninh",
    "Hải Dương", "Hưng Yên", "Hà Nam", "Nam Định", "Ninh Bình", "Thái Bình",
    "Vĩnh Phúc", "Phú Thọ", "Yên Bái", "Lào Cai", "Tuyên Quang", "Hà Giang",
    "Sơn La", "Điện Biên", "Lai Châu", "Hòa Bình", "Thái Nguyên",
    "Thanh Hóa", "Nghệ An", "Hà Tĩnh", "Quảng Bình", "Quảng Trị", "Thừa Thiên Huế",
    "Quảng Nam", "Quảng Ngãi", "Bình Định", "Phú Yên", "Khánh Hòa", "Ninh Thuận", "Bình Thuận",
    "Kon Tum", "Gia Lai", "Đắk Lắk", "Đắk Nông", "Lâm Đồng",
    "Bình Phước", "Tây Ninh", "Bình Dương", "Đồng Nai", "Bà Rịa - Vũng Tàu", "Vũng Tàu",
    "Long An", "Tiền Giang", "Bến Tre", "Trà Vinh", "Vĩnh Long", "Đồng Tháp",
    "An Giang", "Kiên Giang", "Hậu Giang", "Sóc Trăng", "Bạc Liêu", "Cà Mau",
]


def _fetch(url: str, timeout: int = 30) -> str | None:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return raw.decode("utf-8", errors="replace")
    except Exception as e:
        log.warning("fetch %s: %s", url, e)
        return None


_PROVINCES_LOWER = {p.lower() for p in PROVINCES}


# --- Subsidiary extraction (Vietstock dedicated, unblurred page) --------------
# Each visible row reads "<company name> <charter-capital> ( Tr. VND ) <own%>".
# Capture the NAME up to the capital number that precedes the "( Tr. VND )" marker.
# The body excludes the legal-form keywords themselves so the match starts at the
# LAST entity anchor (otherwise a header like "Tên công ty Vốn điều lệ …" bleeds
# into the first real row, since there's no paren between header and first number).
_SUB_RE = re.compile(
    r'((?:CTCP|Công ty|Tổng [Cc]ông ty|Tập đoàn)'
    r'(?:(?!CTCP|Công ty|Tổng [Cc]ông ty|Tập đoàn)[^()]){3,80}?)'
    r'\s+[\d,.]+\s*\(\s*Tr\. VND',
    re.I,
)
_LEGAL_PREFIX = re.compile(
    r'^(CTCP|Công ty (?:TNHH|cổ phần|CP)(?:\s+MTV)?|Tổng Công ty|Tập đoàn)\s+',
    re.I,
)
# Over-common tokens that would cross-match unrelated companies if kept alone.
_GENERIC = {"minh phát", "song lập", "gia phước", "thành phúc"}

# Legal-form / generic ASCII tokens that are NOT brand names. A coined brand
# token (Nasaco, Dacovet, Nutreco, Dabaco) is a no-diacritic word that ISN'T
# one of these, so _is_brand_token can extract it from a long legal name while
# these are suppressed to avoid cross-matching unrelated companies.
_ASCII_STOP = {
    # legal forms / abbreviations
    "mtv", "tnhh", "ctcp", "jsc", "co", "ltd", "llc", "corp", "inc", "plc",
    # ultra-generic English business nouns that appear across many companies
    "group", "holding", "holdings", "invest", "investment", "trading", "trade",
    "power", "energy", "food", "foods", "agri", "agro", "tech", "technology",
    "solution", "solutions", "service", "services", "capital", "finance",
    "global", "international", "industries", "industry", "construction",
    "petro", "oil", "gas", "steel", "cement", "real", "estate", "property",
    "vietnam", "viet", "asia", "pacific", "mega", "smart", "green", "city",
    # more generic sector/business nouns (would otherwise cross-match widely)
    "maritime", "marine", "logistics", "transport", "transportation",
    "pharma", "pharmaceutical", "pharmaceuticals", "medical", "health",
    "mobile", "telecom", "telecommunications", "media", "digital", "online",
    "center", "centre", "express", "delivery", "retail", "market", "mart",
    "paper", "plastic", "plastics", "rubber", "textile", "garment", "fiber",
    "seafood", "aqua", "fishery", "fisheries", "milk", "dairy", "sugar",
    "coffee", "beverage", "brewery", "mining", "mineral", "minerals",
    "port", "airport", "airline", "airlines", "aviation", "hotel", "resort",
    "tourism", "travel", "securities", "insurance", "motor", "auto",
    "electric", "electronics", "machinery", "chemical", "chemicals",
    "fertilizer", "packaging", "agriculture", "development", "ceramic",
    "ceramics", "granite", "petroleum", "refinery", "engineering", "building",
    # ASCII transliterations of provinces/cities (the diacritic forms are in
    # _PROVINCES_LOWER; these no-diacritic spellings appear in brand-ish names).
    "hanoi", "saigon", "danang", "haiphong", "cantho", "hue", "dalat",
    "nhatrang", "vungtau", "halong", "bienhoa", "thainguyen", "namdinh",
}

_VOWEL_GROUP_RE = re.compile(r"[aeiouy]+")


def parse_subsidiaries(html: str) -> list[str]:
    """Extract full subsidiary company names from the dedicated page HTML."""
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    seen: set[str] = set()
    out: list[str] = []
    for m in _SUB_RE.finditer(text):
        name = re.sub(r"\s+", " ", m.group(1)).strip(" -")
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return out


def _is_brand_token(tok: str) -> bool:
    """True for a coined brand token like 'Nasaco'/'Dacovet'/'Nutreco'.

    Heuristic: Vietnamese business words carry diacritics ('thức ăn chăn nuôi')
    and provinces are caught by _PROVINCES_LOWER, so a distinctive brand inside
    a long legal name is the no-diacritic (pure-ASCII), capitalised word that is
    neither a legal form nor a generic English business noun (_ASCII_STOP).

    The crucial guard is >= 2 vowel groups: a coined brand fuses several
    syllables into one token (Na-sa-co, Da-ba-co, Da-co-vet → 3 vowel groups),
    whereas a lone Vietnamese syllable whose diacritic happens to sit on its
    neighbour ('Thanh' in 'Thanh Hóa', 'Ninh' in 'Quảng Ninh', 'Minh' in 'Minh
    Phát') has exactly one — those must NOT become standalone aliases.
    """
    if not (4 <= len(tok) <= 20):
        return False
    if not (tok.isascii() and tok.isalpha() and tok[0].isupper()):
        return False
    low = tok.lower()
    if low in _ASCII_STOP or low in _PROVINCES_LOWER:
        return False
    if len(_VOWEL_GROUP_RE.findall(low)) < 2:
        return False
    return True


def short_aliases(full_names: list[str]) -> list[str]:
    """Best-effort short forms for subsidiary names.

    Two sources, in order: (1) strip the legal prefix and keep the remainder if
    it's short and not generic ('Công ty TNHH Dabaco Thanh Hóa' → 'Dabaco Thanh
    Hóa'); (2) pull any interior coined brand token ('...chăn nuôi Nasaco Hà
    Nam' → 'Nasaco') — the case (1) can't reach because the remainder is too
    long. Duplicates are skipped, first occurrence wins.
    """
    out: list[str] = []

    def add(s: str) -> None:
        if s and s not in out:
            out.append(s)

    for n in full_names:
        short = _LEGAL_PREFIX.sub("", n).strip()
        if (
            short
            and short.lower() not in _GENERIC
            and 1 <= len(short.split()) <= 4
            and short != n
        ):
            add(short)
        for raw in n.split():
            tok = raw.strip(",.()-")
            if _is_brand_token(tok):
                add(tok)
    return out


def _split_headline(rest: str) -> tuple[str | None, str | None]:
    """Parse '{full_name} - {short_brand} - Hồ sơ doanh nghiệp | ...'.

    Splits from the right, because full_name itself can contain ' - '
    (e.g. 'Ngân hàng TMCP Sài Gòn - Hà Nội'). Returns (full, short).
    `short` is rejected if it looks like a province name to avoid the
    "Hà Nội" alias bug that taints every news mentioning the city.
    """
    # Strip site suffix and the trailing ' - Hồ sơ doanh nghiệp' tail.
    s = rest.split("|", 1)[0].strip()
    for tail in (" - Hồ sơ doanh nghiệp", " – Hồ sơ doanh nghiệp"):
        if tail in s:
            s = s.rsplit(tail, 1)[0].strip()
            break
    if not s:
        return None, None
    # Now s = '{full} - {short}' OR just '{full}'.
    if " - " in s:
        full, short = (p.strip() for p in s.rsplit(" - ", 1))
        # Reject short if it's just a city/province (the original bug).
        if short.lower() in _PROVINCES_LOWER:
            return s, None  # keep whole thing as full, no short
        return (full or None), (short or None)
    return s, None


def _parse_name(soup: BeautifulSoup, ticker: str) -> tuple[str | None, str | None]:
    """Return (full_corporate_name, short_brand)."""
    # JSON-LD headline: "DBC: CTCP Tập đoàn Dabaco Việt Nam - DABACO - Hồ sơ doanh nghiệp | ..."
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        headline = data.get("headline")
        if isinstance(headline, str) and ":" in headline:
            return _split_headline(headline.split(":", 1)[1])
    # Fallback: <title>
    title = (soup.title.text if soup.title else "").strip()
    if ticker.upper() + ":" in title:
        return _split_headline(title.split(":", 1)[1])
    return None, None


def _parse_address(html: str) -> str | None:
    m = re.search(r"Trụ sở chính.*?Địa chỉ\s*</span>\s*<td[^>]*>([^<]+)", html, re.S)
    if not m:
        return None
    return re.sub(r"\s+", " ", m.group(1)).strip(" .,;-")


def _locations_from_address(addr: str) -> list[str]:
    if not addr:
        return []
    out = []
    for prov in PROVINCES:
        if prov.lower() in addr.lower() and prov not in out:
            out.append(prov)
    return out


def build_alias(ticker: str) -> dict | None:
    url = f"https://finance.vietstock.vn/{ticker.upper()}/ho-so-doanh-nghiep.htm"
    html = _fetch(url)
    if not html or len(html) < 5000:
        return None
    soup = BeautifulSoup(html, "html.parser")
    full_name, short = _parse_name(soup, ticker)
    addr = _parse_address(html)
    locations = _locations_from_address(addr or "")

    names: list[str] = []
    seen: set[str] = set()

    def add(x: str | None) -> None:
        if not x:
            return
        x = x.strip()
        if not x or x.lower() in seen:
            return
        if x.lower() in _PROVINCES_LOWER:
            return
        seen.add(x.lower())
        names.append(x)

    add(ticker.upper())
    add(full_name)
    add(short)
    # also strip common prefixes to derive a short form
    if full_name:
        for prefix in ("CTCP Tập đoàn", "Tập đoàn", "Công ty Cổ phần", "CTCP", "Công ty TNHH", "Tổng Công ty"):
            if full_name.startswith(prefix + " "):
                add(full_name[len(prefix):].strip())
                break

    derived_from = ["vietstock_profile_minimal"]
    subsidiaries: list[str] = []
    subs_url = f"https://finance.vietstock.vn/{ticker.upper()}/cong-ty-con-lien-doanh-lien-ket.htm"
    subs_html = _fetch(subs_url)
    if subs_html:
        full_subs = parse_subsidiaries(subs_html)
        shorts = [s for s in short_aliases(full_subs) if s not in full_subs]
        subsidiaries = full_subs + shorts
        derived_from.append("vietstock_subsidiaries")
        # Audit line: derived short tokens are the heuristic part most prone to
        # cross-match false positives — log them so a --all run can be eyeballed.
        if shorts:
            log.info("%s short subsidiary aliases: %s", ticker.upper(), shorts)

    return {
        "ticker": ticker.upper(),
        "company_name": full_name or "",
        "headquarters": addr or "",
        "derived_from": derived_from,
        "names": names,
        "subsidiaries": subsidiaries,
        "projects": [],
        "locations": locations,
    }


def write_alias(ticker: str, force: bool = False) -> str:
    path = settings.ALIASES_DIR / f"{ticker.upper()}.json"
    if path.exists() and not force:
        return "exists"
    data = build_alias(ticker)
    if data is None:
        return "failed"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return "written"


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s/%(levelname)s] %(message)s",
    )
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--delay", type=float, default=3.0)
    args = ap.parse_args()
    if not args.ticker and not args.all:
        ap.error("pass --ticker or --all")

    tickers = [args.ticker] if args.ticker else read_tickers()
    stats = {"written": 0, "exists": 0, "failed": 0}
    for i, t in enumerate(tickers, 1):
        outcome = write_alias(t, force=args.force)
        stats[outcome] += 1
        log.info("[%d/%d] %s → %s", i, len(tickers), t, outcome)
        if args.all and i < len(tickers):
            time.sleep(args.delay)
    log.info("done: %s", stats)


if __name__ == "__main__":
    main()
