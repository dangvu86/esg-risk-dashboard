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
import logging
from datetime import date, timedelta
from typing import Iterator

from config import settings
from core import storage

log = logging.getLogger("queue_builder")


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
    storage.init_db(db_path)
    conn = storage.connect(db_path)
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

    Routing (backfill, window=None):
    - BaoMoi: names + subsidiaries, one deep pass each over its full settings
      window as a single task — BaoMoi ignores date params and paginates
      client-side (early-stopping at the window start), so chunking is wasteful.
    - Google RSS / Brave: NAMES ONLY, monthly chunks. Tail is 2020-01-01 to
      2021-12-31 for Google (the pre-BaoMoi gap) and settings.BRAVE_WINDOW_*.

    When `window` is provided (daily incremental) it overrides EVERY backend,
    including BaoMoi — the flow is identical, only the time window shrinks.
    BaoMoi's early-stop keeps a 4–5 day daily pass to ~1–2 pages per alias.

    Subsidiaries are searched ONLY on BaoMoi; they remain alias-match targets
    downstream (match.py) regardless of which backend found the article.

    Args:
        tickers: List of ticker symbols. Defaults to all tickers in COMPANIES_CSV.
                 Pass a subset to backfill just-added companies (idempotent — the
                 already-enqueued tickers' tasks are skipped by INSERT OR IGNORE).
        window:  (start, end) date strings overriding ALL backends' windows
                 (daily). Leave None for the historical backfill routing above.
        db_path: Path to the SQLite database. Pass a temp path for hermetic tests.

    Returns:
        Dict mapping backend name to the number of newly inserted tasks.
    """
    if tickers is None:
        from config.companies import read_tickers
        tickers = read_tickers()

    storage.init_db(db_path)
    conn = storage.connect(db_path)
    inserted: dict[str, int] = {"baomoi": 0, "google_rss": 0, "brave": 0}

    # Google/Brave tail window: default is 2020–2021 (pre-BaoMoi gap) for Google
    # and settings.BRAVE_WINDOW_* for Brave. When `window` is passed (daily
    # incremental) it overrides EVERY backend including BaoMoi — same flow,
    # just a recent window. When `window` is None (backfill) each backend uses
    # its own historical settings window.
    g_tail = window if window else ("2020-01-01", "2021-12-31")
    bv_tail = window if window else (settings.BRAVE_WINDOW_START, settings.BRAVE_WINDOW_END)
    bm_window = window if window else (settings.BAOMOI_WINDOW_START, settings.BAOMOI_WINDOW_END)

    try:
        for tk in tickers:
            # Skip (don't abort) tickers without a built alias file — a multi-day
            # enqueue over ~100 companies shouldn't die on one missing file.
            if not (settings.ALIASES_DIR / f"{tk}.json").exists():
                log.warning("no alias file for %s — skipping (run fetch_vietstock --all)", tk)
                continue
            names, subs = _load_alias_lists(tk)

            # BaoMoi: names + subsidiaries, one task per alias spanning the
            # whole BaoMoi window (no chunking — BaoMoi paginates client-side).
            # NOTE: sub_query_ix here indexes `names + subs`, whereas the
            # Google/Brave loop below indexes `names` only — so the same ix
            # is NOT a stable cross-backend alias identifier. Uniqueness is
            # fine because the task_id also keys on backend.
            for ix, alias in enumerate(names + subs):
                if storage.enqueue_task(
                    conn,
                    backend="baomoi",
                    kind="alias",
                    ticker=tk,
                    group_key="alias",
                    sub_query_ix=ix,
                    query=alias,
                    after=bm_window[0],
                    before=bm_window[1],
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


def build_combined_tasks(
    *,
    window: tuple[str, str] | None = None,
    tickers: list[str] | None = None,
    db_path=None,
) -> dict[str, int]:
    """The full flow = L2 per-company alias + L1 single-term keyword.

    The SAME builder serves both jobs — only the window differs:
    - backfill (window=None): each backend uses its historical settings window;
    - daily (window=(start,end)): every backend uses that recent window.

    L2 = alias tasks (names on all backends, subsidiaries on BaoMoi); L1 = the
    single-term keyword net. Replaces the legacy broad OR-keyword pool. Counts
    are summed per backend across both passes.
    """
    counts = build_alias_tasks(tickers=tickers, window=window, db_path=db_path)
    for backend, n in build_keyword_tasks(window=window, db_path=db_path).items():
        counts[backend] = counts.get(backend, 0) + n
    return counts


def main() -> None:
    from datetime import datetime as _dt, timedelta as _td, timezone
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
                    help="backfill: full historical flow — per-company alias + "
                         "single-term keyword over each backend's settings window "
                         "(--backends ignored). "
                         "daily: the SAME flow over a recent window (default last 5 "
                         "days; --backends ignored). "
                         "keyword: just the L1 single-term keyword half (google_rss/brave). "
                         "alias: just the L2 per-company alias half "
                         "(baomoi/google_rss/brave; --backends ignored).")
    ap.add_argument("--tickers", nargs="+", default=None,
                    help="alias/daily modes: restrict to these tickers (e.g. a "
                         "newly-added company to backfill). Default: all in "
                         "COMPANIES_CSV. Idempotent, so safe to re-run.")
    ap.add_argument("--days-back", type=int, default=5,
                    help="daily mode: how many trailing days to enqueue (default 5). "
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
        today_vn = (_dt.now(_VN) if _VN else _dt.now(timezone.utc)).date()
        end = today_vn - _td(days=1)
        start = end - _td(days=max(0, args.days_back - 1))
        window = (start.isoformat(), end.isoformat())
    if args.mode == "keyword":
        counts = build_keyword_tasks(args.backends, window=window)
    elif args.mode == "alias":
        counts = build_alias_tasks(tickers=args.tickers, window=window)
    else:  # backfill or daily — same flow, window differs
        counts = build_combined_tasks(window=window, tickers=args.tickers)
    total = sum(counts.values())
    print(f"Enqueued {total} new tasks:")
    for b, n in counts.items():
        print(f"  {b}: {n}")


if __name__ == "__main__":
    main()
