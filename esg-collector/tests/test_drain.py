import sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import storage


def _db(tmp):
    db = Path(tmp) / "q.db"
    storage.init_db(db)
    return db


def test_has_remaining_counts_pending_and_backoff():
    with tempfile.TemporaryDirectory() as td:
        db = _db(td); conn = storage.connect(db)
        assert storage.has_remaining_tasks(conn, "google_rss") is False
        storage.enqueue_task(conn, backend="google_rss", kind="keyword",
                             ticker=None, group_key="kw", sub_query_ix=0,
                             query="x", after="2026-01-01", before="2026-01-07")
        assert storage.has_remaining_tasks(conn, "google_rss") is True
        # a different backend is unaffected
        assert storage.has_remaining_tasks(conn, "brave") is False
        conn.close()


def test_done_and_failed_do_not_count():
    with tempfile.TemporaryDirectory() as td:
        db = _db(td); conn = storage.connect(db)
        storage.enqueue_task(conn, backend="brave", kind="keyword", ticker=None,
                             group_key="kw", sub_query_ix=0, query="x",
                             after="2026-01-01", before="2026-01-07")
        t = storage.next_task(conn, "brave")
        storage.mark_task_done(conn, t["task_id"], 0)
        assert storage.has_remaining_tasks(conn, "brave") is False
        conn.close()
