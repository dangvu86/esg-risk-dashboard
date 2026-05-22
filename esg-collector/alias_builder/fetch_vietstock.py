"""Bootstrap a minimal `config/aliases/<TICKER>.json` from Vietstock.

Vietstock's `ho-so-doanh-nghiep.htm` exposes the full corporate name and HQ
address in the static HTML. Subsidiaries are AJAX-loaded and aren't reliably
scrapeable here; the 3 hand-curated alias files (DBC/KDH/DGC) include them.

This builder produces a baseline that's accurate enough for keyword matching:
  - `names`        : full corporate name + short brand + ticker
  - `locations`    : province / city parsed from HQ address (weak match)
  - `subsidiaries` / `projects` : empty (caller can enrich manually)

CLI:
  python -m alias_builder.fetch_vietstock --ticker DBC
  python -m alias_builder.fetch_vietstock --all                 # uses companies.csv
  python -m alias_builder.fetch_vietstock --all --force         # overwrite existing
"""

from __future__ import annotations

import argparse
import csv
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
            rest = headline.split(":", 1)[1].strip()
            # split off " - SUFFIX - Hồ sơ..."
            parts = [p.strip() for p in rest.split(" - ")]
            if not parts:
                continue
            full = parts[0] or None
            short = parts[1] if len(parts) >= 2 and parts[1] and "Hồ sơ" not in parts[1] else None
            return full, short
    # Fallback: <title>
    title = (soup.title.text if soup.title else "").strip()
    if ticker.upper() + ":" in title:
        rest = title.split(":", 1)[1].split("|")[0]
        full = rest.split(" - ")[0].strip()
        return full or None, None
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

    return {
        "ticker": ticker.upper(),
        "company_name": full_name or "",
        "headquarters": addr or "",
        "derived_from": ["vietstock_profile_minimal"],
        "names": names,
        "subsidiaries": [],
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


def _read_companies() -> list[str]:
    tickers: list[str] = []
    with open(settings.COMPANIES_CSV, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            t = (row.get("Mã CK") or row.get("Ma CK") or "").strip()
            if t:
                tickers.append(t)
    return tickers


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

    tickers = [args.ticker] if args.ticker else _read_companies()
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
