"""Shared reader for the company universe (COMPANIES_CSV).

Both the queue builder and the alias builder need the ticker list with the
same column-name fallback (the CSV header is sometimes "Mã CK", sometimes the
unaccented "Ma CK"), so the logic lives here once.
"""

from __future__ import annotations

import csv

from config import settings


def read_tickers() -> list[str]:
    """Return all non-empty tickers from COMPANIES_CSV, in file order."""
    with open(settings.COMPANIES_CSV, encoding="utf-8-sig") as f:
        tickers = [
            (r.get("Mã CK") or r.get("Ma CK") or "").strip()
            for r in csv.DictReader(f)
        ]
    return [t for t in tickers if t]
