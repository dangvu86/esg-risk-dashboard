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


_BACKEND_WINDOWS = {
    "google_rss": (settings.BACKFILL_START, settings.BACKFILL_END),
    "baomoi":     (settings.BAOMOI_WINDOW_START, settings.BAOMOI_WINDOW_END),
    "brave":      (settings.BRAVE_WINDOW_START, settings.BRAVE_WINDOW_END),
}


def build_queue(backends: list[str] | None = None) -> dict[str, int]:
    """Enqueue every (backend, sub-query, chunk). Return inserted count per backend."""
    backends = backends or list(_BACKEND_WINDOWS.keys())
    storage.init_db()
    conn = storage.connect()
    inserted: dict[str, int] = {b: 0 for b in backends}
    try:
        for backend in backends:
            start, end = _BACKEND_WINDOWS[backend]
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backends", nargs="+", default=None,
                    help="Subset of: google_rss baomoi brave")
    args = ap.parse_args()
    counts = build_queue(args.backends)
    total = sum(counts.values())
    print(f"Enqueued {total} new tasks:")
    for b, n in counts.items():
        print(f"  {b}: {n}")


if __name__ == "__main__":
    main()
