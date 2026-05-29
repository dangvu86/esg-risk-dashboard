"""Populate `search_queue` with one task per (backend × sub-query × chunk).

Each backend has its own date window from `settings.py`:
  - google_rss : full BACKFILL window
  - baomoi     : BAOMOI_WINDOW (modern coverage)
  - brave      : BRAVE_WINDOW  (older coverage where BaoMoi runs out)

Idempotent: rerun is safe (INSERT OR IGNORE on task_id).
Run with:  python -m core.queue_builder
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta
from typing import Iterator

from config import settings
from config.keywords import KEYWORD_GROUPS
from core import storage


def _parse(d: str) -> date:
    return date.fromisoformat(d)


def date_chunks(start: str, end: str, months: int = 1) -> Iterator[tuple[str, str]]:
    """Yield (after, before) inclusive-exclusive monthly chunks."""
    cur = _parse(start)
    end_d = _parse(end)
    while cur <= end_d:
        # advance `months` months by stepping ~30 days; precise enough for chunking
        nxt = cur
        for _ in range(months):
            # jump to first of next month
            year = nxt.year + (1 if nxt.month == 12 else 0)
            month = 1 if nxt.month == 12 else nxt.month + 1
            nxt = date(year, month, 1)
        before = min(nxt - timedelta(days=1), end_d)
        yield cur.isoformat(), before.isoformat()
        cur = nxt


def weekly_subchunks(after: str, before: str) -> list[tuple[str, str]]:
    """Split [after, before] inclusive into contiguous ~7-day (after, before) spans.

    Used to re-enqueue a Google News alias month that came back near the
    ~100-result cap: each weekly child returns fewer items, dodging truncation.

    The first span starts at `after`; the last span ends at `before`; spans are
    contiguous and non-overlapping, and every span has a <= b. A 30-day month
    yields 5 spans (four 7-day spans + a short tail).
    """
    start = _parse(after)
    end = _parse(before)
    spans: list[tuple[str, str]] = []
    cur = start
    while cur <= end:
        span_end = min(cur + timedelta(days=6), end)
        spans.append((cur.isoformat(), span_end.isoformat()))
        cur = span_end + timedelta(days=1)
    return spans


_BACKEND_WINDOWS = {
    "google_rss": (settings.BACKFILL_START, settings.BACKFILL_END),
    "baomoi":     (settings.BAOMOI_WINDOW_START, settings.BAOMOI_WINDOW_END),
    "brave":      (settings.BRAVE_WINDOW_START, settings.BRAVE_WINDOW_END),
}


def build_queue(
    backends: list[str] | None = None,
    *,
    window: tuple[str, str] | None = None,
) -> dict[str, int]:
    """Enqueue every (backend, sub-query, chunk). Return inserted count per backend.

    If `window` is given, it overrides each backend's default range — used for
    daily incremental fetches (1 day, 1 chunk per backend).
    """
    backends = backends or list(_BACKEND_WINDOWS.keys())
    storage.init_db()
    conn = storage.connect()
    inserted: dict[str, int] = {b: 0 for b in backends}
    try:
        for backend in backends:
            start, end = window if window else _BACKEND_WINDOWS[backend]
            for after, before in date_chunks(start, end, settings.CHUNK_MONTHS):
                for grp, subs in KEYWORD_GROUPS.items():
                    for ix, query in enumerate(subs):
                        if storage.enqueue_task(
                            conn,
                            backend=backend,
                            group_key=grp,
                            sub_query_ix=ix,
                            query=query,
                            after=after,
                            before=before,
                        ):
                            inserted[backend] += 1
    finally:
        conn.close()
    return inserted


def build_keyword_tasks(
    backends: list[str] | None = None,
    *,
    window: tuple[str, str] | None = None,
    db_path=None,
) -> dict[str, int]:
    """Enqueue L1 single-term keyword tasks: one per (backend × term × chunk).

    Args:
        backends: List of backend names. Defaults to ["google_rss", "brave"].
        window: (start, end) date strings override default backend windows.
        db_path: Path to the SQLite database. Pass a temp path for hermetic tests.

    Returns:
        Dict mapping each backend name to the number of newly inserted tasks.
    """
    from config.keywords import search_terms
    backends = backends or ["google_rss", "brave"]
    terms = search_terms()
    storage.init_db(db_path) if db_path else storage.init_db()
    conn = storage.connect(db_path) if db_path else storage.connect()
    inserted: dict[str, int] = {b: 0 for b in backends}
    try:
        for backend in backends:
            start, end = window if window else (settings.BACKFILL_START, settings.BACKFILL_END)
            for after, before in date_chunks(start, end, settings.CHUNK_MONTHS):
                for ix, term in enumerate(terms):
                    if storage.enqueue_task(
                        conn,
                        backend=backend,
                        kind="keyword",
                        group_key="kw",
                        sub_query_ix=ix,
                        query=term,
                        after=after,
                        before=before,
                    ):
                        inserted[backend] += 1
    finally:
        conn.close()
    return inserted


def _load_alias_lists(ticker: str) -> tuple[list[str], list[str]]:
    """Load names and subsidiaries from config/aliases/<TICKER>.json.

    Returns (names, subsidiaries). Either list may be empty if the key is
    absent in the JSON file.
    """
    import json
    p = settings.ALIASES_DIR / f"{ticker}.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    return data.get("names") or [], data.get("subsidiaries") or []


def build_alias_tasks(
    tickers: list[str] | None = None,
    *,
    window: tuple[str, str] | None = None,
    db_path=None,
) -> dict[str, int]:
    """Enqueue L2 per-company alias tasks. Return inserted count per backend.

    Routing:
    - BaoMoi: names + subsidiaries, one deep pass each (full BaoMoi window as
      a single task — BaoMoi ignores date params and paginates client-side, so
      chunking is wasteful).
    - Google RSS / Brave: NAMES ONLY, monthly chunks. Default tail is
      2020-01-01 to 2021-12-31 (the pre-BaoMoi coverage gap). If `window` is
      provided it overrides the Google/Brave start+end; BaoMoi always uses its
      full settings window regardless.

    Subsidiaries are searched ONLY on BaoMoi; they remain alias-match targets
    downstream (match.py) regardless of which backend found the article.

    Args:
        tickers: List of ticker symbols. Defaults to all tickers in COMPANIES_CSV.
        window:  (start, end) date strings to override the Google/Brave tail
                 window. Leave None to use the standard 2020–2021 tail for
                 Google and settings.BRAVE_WINDOW_* for Brave. BaoMoi always
                 uses its full settings window regardless of this parameter.
        db_path: Path to the SQLite database. Pass a temp path for hermetic tests.

    Returns:
        Dict mapping backend name to the number of newly inserted tasks.
    """
    import csv
    if tickers is None:
        with open(settings.COMPANIES_CSV, encoding="utf-8-sig") as f:
            tickers = [
                (r.get("Mã CK") or r.get("Ma CK") or "").strip()
                for r in csv.DictReader(f)
            ]
            tickers = [t for t in tickers if t]

    storage.init_db(db_path) if db_path else storage.init_db()
    conn = storage.connect(db_path) if db_path else storage.connect()
    inserted: dict[str, int] = {"baomoi": 0, "google_rss": 0, "brave": 0}

    # Google/Brave tail window: default is 2020–2021 (pre-BaoMoi gap) for Google
    # and settings.BRAVE_WINDOW_* for Brave. The caller may pass `window` to
    # override BOTH (e.g. for daily incremental runs on the tail).
    # BaoMoi window is always taken from settings regardless of `window`.
    g_tail = window if window else ("2020-01-01", "2021-12-31")
    bv_tail = window if window else (settings.BRAVE_WINDOW_START, settings.BRAVE_WINDOW_END)

    try:
        for tk in tickers:
            names, subs = _load_alias_lists(tk)

            # BaoMoi: names + subsidiaries, one task per alias spanning the
            # whole BaoMoi window (no chunking — BaoMoi paginates client-side).
            for ix, alias in enumerate(names + subs):
                if storage.enqueue_task(
                    conn,
                    backend="baomoi",
                    kind="alias",
                    ticker=tk,
                    group_key="alias",
                    sub_query_ix=ix,
                    query=alias,
                    after=settings.BAOMOI_WINDOW_START,
                    before=settings.BAOMOI_WINDOW_END,
                ):
                    inserted["baomoi"] += 1

            # Google RSS + Brave: NAMES ONLY, monthly chunks over the tail.
            for backend, (start, end) in (
                ("google_rss", g_tail),
                ("brave",      bv_tail),
            ):
                for after, before in date_chunks(start, end, settings.CHUNK_MONTHS):
                    for ix, alias in enumerate(names):
                        if storage.enqueue_task(
                            conn,
                            backend=backend,
                            kind="alias",
                            ticker=tk,
                            group_key="alias",
                            sub_query_ix=ix,
                            query=alias,
                            after=after,
                            before=before,
                        ):
                            inserted[backend] += 1
    finally:
        conn.close()

    return inserted


def main() -> None:
    from datetime import datetime as _dt, timedelta as _td
    try:
        from zoneinfo import ZoneInfo
        _VN = ZoneInfo("Asia/Ho_Chi_Minh")
    except Exception:
        _VN = None  # fallback: VM clock is assumed UTC, accept the offset

    ap = argparse.ArgumentParser()
    ap.add_argument("--backends", nargs="+", default=None,
                    help="Subset of: google_rss baomoi brave")
    ap.add_argument("--mode", choices=("backfill", "daily", "keyword", "alias"),
                    default="backfill",
                    help="backfill: use settings.py windows (default). "
                         "daily: rolling window ending yesterday (VN time). "
                         "keyword: enqueue L1 single-term keyword tasks (google_rss/brave). "
                         "alias: enqueue L2 per-company alias tasks (baomoi/google_rss/brave; "
                         "--backends ignored).")
    ap.add_argument("--days-back", type=int, default=3,
                    help="daily mode: how many trailing days to enqueue (default 3). "
                         "Wider window catches late-indexed Google News articles + "
                         "the 7h UTC<->VN offset. Dedup is idempotent so re-enqueueing "
                         "the same day is free.")
    ap.add_argument("--since", help="Override window start (YYYY-MM-DD)")
    ap.add_argument("--until", help="Override window end (YYYY-MM-DD)")
    args = ap.parse_args()
    window: tuple[str, str] | None = None
    if args.since and args.until:
        window = (args.since, args.until)
    elif args.mode == "daily":
        today_vn = (_dt.now(_VN) if _VN else _dt.utcnow()).date()
        end = today_vn - _td(days=1)
        start = end - _td(days=max(0, args.days_back - 1))
        window = (start.isoformat(), end.isoformat())
    if args.mode == "keyword":
        counts = build_keyword_tasks(args.backends, window=window)
    elif args.mode == "alias":
        counts = build_alias_tasks(window=window)
    else:  # backfill or daily
        counts = build_queue(args.backends, window=window)
    total = sum(counts.values())
    print(f"Enqueued {total} new tasks:")
    for b, n in counts.items():
        print(f"  {b}: {n}")


if __name__ == "__main__":
    main()
