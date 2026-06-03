"""Company revenue (billion VND) per ticker/year, for the controversy 20% rule.
Reimplemented from cloud-function/rss_fetcher.py — reads config/companies.csv."""
from __future__ import annotations
import csv
from pathlib import Path

from config import settings


def load_revenues(csv_path: Path | str | None = None) -> dict[str, dict[int, float]]:
    """ticker -> {year_int: revenue_billion_vnd}. Year columns are the numeric
    headers; values are billions with comma thousands (" 110,490 " -> 110490.0)."""
    path = Path(csv_path) if csv_path else settings.COMPANIES_CSV
    revenues: dict[str, dict[int, float]] = {}
    if not path.exists():
        return revenues
    with open(path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        year_cols = []
        for col in reader.fieldnames or []:
            try:
                year_cols.append((int(col.strip()), col))
            except ValueError:
                continue
        for row in reader:
            ticker = (row.get("Mã CK") or "").strip()
            if not ticker:
                continue
            per_year: dict[int, float] = {}
            for yr, col in year_cols:
                raw = (row.get(col) or "").strip().replace(",", "")
                if not raw:
                    continue
                try:
                    per_year[yr] = float(raw)
                except ValueError:
                    continue
            if per_year:
                revenues[ticker] = per_year
    return revenues


def get_revenue_for_year(per_year: dict[int, float], year: int) -> tuple[int, float] | None:
    """Exact-year match; else closest available year (ties → older). (year, rev) or None."""
    if not per_year:
        return None
    if year in per_year:
        return (year, per_year[year])
    chosen = min(per_year.keys(), key=lambda y: (abs(y - year), y))
    return (chosen, per_year[chosen])
