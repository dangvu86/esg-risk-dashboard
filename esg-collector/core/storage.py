"""SQLite storage for esg-collector.

Two tables:
  - articles      : deduped pool of fetched items, body+match status tracked here
  - search_queue  : persistent task queue per backend, crash-safe resume

Connection helper uses WAL so multiple worker processes (1 per backend +
body fetcher + match) can share the same file safely.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from config.settings import DB_PATH


def _utc_now_iso() -> str:
    """Single source of truth for timestamps written from Python.

    Uses 'T' separator + 'Z' suffix so values are unambiguous UTC and
    sort/parse identically whether SQLite or Python writes them.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# strftime('%Y-%m-%dT%H:%M:%SZ','now') keeps SQLite-written defaults in the
# same ISO-T-Z format that _utc_now_iso() produces from Python, so range
# queries on fetched_at / next_attempt sort and parse uniformly.
SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
  article_id    TEXT PRIMARY KEY,
  url_canonical TEXT NOT NULL,
  url_original  TEXT,
  domain        TEXT,
  title         TEXT,
  title_hash    TEXT,
  description   TEXT,
  sapo          TEXT,
  body          TEXT,
  body_status   TEXT DEFAULT 'pending',
  published_at  TEXT,
  source        TEXT,
  backend       TEXT,
  group_key     TEXT,
  sub_query_ix  INTEGER,
  fetched_at    TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  matched_at    TEXT,
  match_status  TEXT DEFAULT 'pending',
  cached_hits   TEXT,
  ticker_hint   TEXT,
  esg_status    TEXT DEFAULT 'pending',
  esg_type      TEXT,
  severity      TEXT
);
CREATE INDEX IF NOT EXISTS idx_articles_match      ON articles(match_status, body_status);
CREATE INDEX IF NOT EXISTS idx_articles_date       ON articles(published_at);
CREATE INDEX IF NOT EXISTS idx_articles_fetched_at ON articles(fetched_at);
-- title_hash index is created after init_db's ALTER TABLE migration to avoid
-- failing on legacy databases where the column does not yet exist.

CREATE TABLE IF NOT EXISTS search_queue (
  task_id      TEXT PRIMARY KEY,
  backend      TEXT,
  group_key    TEXT,
  sub_query_ix INTEGER,
  query        TEXT,
  after        TEXT,
  before       TEXT,
  status       TEXT DEFAULT 'pending',
  attempts     INTEGER DEFAULT 0,
  next_attempt TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  last_error   TEXT,
  items_found  INTEGER DEFAULT 0,
  done_at      TEXT,
  kind         TEXT DEFAULT 'keyword',
  ticker       TEXT
);
CREATE INDEX IF NOT EXISTS idx_queue_next ON search_queue(backend, status, next_attempt);

CREATE TABLE IF NOT EXISTS url_decode_cache (
  encoded_url   TEXT PRIMARY KEY,
  decoded_url   TEXT,
  status        TEXT,                -- 'ok' | 'failed'
  attempted_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS export_state (
  key       TEXT PRIMARY KEY,
  value     TEXT,
  updated_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
"""


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: Path | str | None = None) -> None:
    if db_path is None:
        db_path = DB_PATH
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        # Idempotent column-adds for older databases.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(articles)")}
        if "title_hash" not in cols:
            conn.execute("ALTER TABLE articles ADD COLUMN title_hash TEXT")
        if "cached_hits" not in cols:
            conn.execute("ALTER TABLE articles ADD COLUMN cached_hits TEXT")
        # cols captured above before any ALTER; safe because no column appears in both the if-blocks and this loop
        # Idempotent column-adds: articles ESG verdict columns.
        for col, ddl in [
            ("ticker_hint", "ALTER TABLE articles ADD COLUMN ticker_hint TEXT"),
            ("esg_status",  "ALTER TABLE articles ADD COLUMN esg_status TEXT DEFAULT 'pending'"),
            ("esg_type",    "ALTER TABLE articles ADD COLUMN esg_type TEXT"),
            ("severity",    "ALTER TABLE articles ADD COLUMN severity TEXT"),
        ]:
            if col not in cols:
                conn.execute(ddl)
        # Idempotent column-adds: search_queue task-kind columns.
        qcols = {r["name"] for r in conn.execute("PRAGMA table_info(search_queue)")}
        for col, ddl in [
            ("kind",   "ALTER TABLE search_queue ADD COLUMN kind TEXT DEFAULT 'keyword'"),
            ("ticker", "ALTER TABLE search_queue ADD COLUMN ticker TEXT"),
        ]:
            if col not in qcols:
                conn.execute(ddl)
        # Indexes created here (not in SCHEMA above) so legacy DBs survive init.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_esg ON articles(esg_status)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_articles_title_hash "
            "ON articles(title_hash, published_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_articles_fetched_at "
            "ON articles(fetched_at)"
        )
    finally:
        conn.close()


# ---------------- articles ----------------

_ARTICLE_COLS = (
    "article_id", "url_canonical", "url_original", "domain", "title", "title_hash",
    "description", "sapo", "body", "body_status", "published_at",
    "source", "backend", "group_key", "sub_query_ix", "cached_hits",
    "ticker_hint",
)

_MERGE_COLS = ("description", "sapo", "body", "source")


def _merge_into(conn: sqlite3.Connection, target_id: str, rec: dict[str, Any]) -> None:
    """Fill any NULL/empty fields on the target row from the new record."""
    for col in _MERGE_COLS:
        v = rec.get(col)
        if v:
            conn.execute(
                f"UPDATE articles SET {col}=? "
                f"WHERE article_id=? AND (COALESCE({col},'')='')",
                (v, target_id),
            )


def insert_article(conn: sqlite3.Connection, rec: dict[str, Any]) -> bool:
    """INSERT OR IGNORE with two dedup layers. Return True if a new row was
    inserted, False if it merged into an existing one.

    Dedup order:
      1. `article_id` PRIMARY KEY (URL-derived) — catches repeats from the
         same backend.
      2. `(title_hash, published_at)` — catches the same story arriving via
         a different backend with a different article_id (e.g. Google News
         encoded link vs. BaoMoi internal id pointing at the same Lao Dong
         article). Only consulted when title_hash is non-empty.

    On any conflict we merge missing fields (sapo / body / description /
    source) so a later, richer fetch upgrades the earlier sparse one.
    """
    rec = dict(rec)
    rec.setdefault("body_status", "pending")
    cols = [c for c in _ARTICLE_COLS if c in rec]
    placeholders = ",".join("?" for _ in cols)
    sql = (
        f"INSERT OR IGNORE INTO articles ({','.join(cols)}) VALUES ({placeholders})"
    )
    cur = conn.execute(sql, [rec[c] for c in cols])
    if cur.rowcount:
        # New article_id — still need to check title_hash for cross-backend dup.
        th = rec.get("title_hash")
        pub = rec.get("published_at")
        if th and pub:
            sibling = conn.execute(
                "SELECT article_id FROM articles "
                "WHERE title_hash=? AND published_at=? AND article_id<>? "
                "ORDER BY fetched_at ASC LIMIT 1",
                (th, pub, rec["article_id"]),
            ).fetchone()
            if sibling:
                # Merge new row into the existing sibling and drop the new row.
                _merge_into(conn, sibling["article_id"], rec)
                conn.execute(
                    "DELETE FROM articles WHERE article_id=?", (rec["article_id"],)
                )
                return False
        return True
    # article_id collision — same backend repeating itself. Just merge.
    _merge_into(conn, rec["article_id"], rec)
    return False


def mark_body(conn: sqlite3.Connection, article_id: str, status: str, body: str | None = None) -> None:
    if body is not None:
        conn.execute(
            "UPDATE articles SET body=?, body_status=? WHERE article_id=?",
            (body, status, article_id),
        )
    else:
        conn.execute(
            "UPDATE articles SET body_status=? WHERE article_id=?",
            (status, article_id),
        )


def mark_match(conn: sqlite3.Connection, article_id: str, status: str) -> None:
    conn.execute(
        "UPDATE articles SET match_status=?, matched_at=? WHERE article_id=?",
        (status, _utc_now_iso(), article_id),
    )


def mark_esg(conn: sqlite3.Connection, article_id: str, status: str,
             esg_type: str | None = None, severity: str | None = None) -> None:
    conn.execute(
        "UPDATE articles SET esg_status=?, esg_type=?, severity=? WHERE article_id=?",
        (status, esg_type, severity, article_id),
    )


def cache_hits(conn: sqlite3.Connection, article_id: str, hits_json: str) -> None:
    """Persist alias matches discovered by the body_fetcher's pre-check so
    pipeline.match can skip re-running the regex pool on the same article.
    """
    conn.execute(
        "UPDATE articles SET cached_hits=? WHERE article_id=?",
        (hits_json, article_id),
    )


def iter_articles(
    conn: sqlite3.Connection,
    *,
    match_status: str | None = None,
    body_status: str | None = None,
    since: str | None = None,
    limit: int | None = None,
) -> Iterable[sqlite3.Row]:
    sql = "SELECT * FROM articles WHERE 1=1"
    args: list[Any] = []
    if match_status:
        sql += " AND match_status=?"
        args.append(match_status)
    if body_status:
        sql += " AND body_status=?"
        args.append(body_status)
    if since:
        sql += " AND published_at >= ?"
        args.append(since)
    sql += " ORDER BY published_at DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, args)


# ---------------- queue ----------------

def enqueue_task(
    conn: sqlite3.Connection,
    *,
    backend: str,
    group_key: str,
    sub_query_ix: int,
    query: str,
    after: str,
    before: str,
    kind: str = "keyword",
    ticker: str | None = None,
) -> bool:
    if kind not in ("keyword", "alias"):
        raise ValueError(f"Unknown task kind: {kind!r}")
    if kind == "alias" and ticker is None:
        raise ValueError("ticker is required when kind='alias'")
    if kind == "alias":
        task_id = f"{backend}:alias:{ticker}:{sub_query_ix}:{after}"
    else:
        task_id = f"{backend}:{group_key}:{sub_query_ix}:{after}"
    cur = conn.execute(
        "INSERT OR IGNORE INTO search_queue "
        "(task_id, backend, group_key, sub_query_ix, query, after, before, kind, ticker) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (task_id, backend, group_key, sub_query_ix, query, after, before, kind, ticker),
    )
    return cur.rowcount > 0


# Tasks claimed but never marked done/backoff (worker crashed mid-fetch)
# become reclaimable after this window. Chosen larger than any single
# backend.fetch call we expect; tune if a backend regularly runs longer.
_CLAIM_TTL_SECONDS = 1800


def next_task(conn: sqlite3.Connection, backend: str) -> sqlite3.Row | None:
    """Atomically claim the next ready task for `backend`.

    Uses UPDATE ... RETURNING (SQLite 3.35+, shipped in Python 3.10's bundled
    SQLite). Without atomic claim, two workers of the same backend racing on
    `SELECT then UPDATE` would both process the same task; even though we
    deploy one worker per backend today, the persistent queue is the kind of
    thing that drifts to N workers eventually.

    `in_progress` rows older than _CLAIM_TTL_SECONDS are also eligible — a
    worker that crashed mid-fetch otherwise leaves the task stuck forever.
    On claim, next_attempt is bumped by the TTL so a healthy worker keeps
    exclusive ownership until it finishes or the TTL expires.
    """
    now_iso = _utc_now_iso()
    reclaim_at = (datetime.now(timezone.utc) + timedelta(seconds=_CLAIM_TTL_SECONDS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    row = conn.execute(
        "UPDATE search_queue "
        "SET status='in_progress', next_attempt=? "
        "WHERE task_id = ("
        "  SELECT task_id FROM search_queue "
        "  WHERE backend=? AND status IN ('pending','backoff','in_progress') "
        "        AND next_attempt <= ? "
        "  ORDER BY next_attempt ASC LIMIT 1"
        ") "
        "RETURNING *",
        (reclaim_at, backend, now_iso),
    ).fetchone()
    return row


def mark_task_done(conn: sqlite3.Connection, task_id: str, items_found: int) -> None:
    conn.execute(
        "UPDATE search_queue SET status='done', items_found=?, "
        "done_at=?, last_error=NULL WHERE task_id=?",
        (items_found, _utc_now_iso(), task_id),
    )


def mark_task_backoff(
    conn: sqlite3.Connection,
    task_id: str,
    retry_after_seconds: int,
    error: str,
    *,
    max_attempts: int,
) -> str:
    row = conn.execute(
        "SELECT attempts FROM search_queue WHERE task_id=?", (task_id,)
    ).fetchone()
    attempts = (row["attempts"] if row else 0) + 1
    if attempts >= max_attempts:
        conn.execute(
            "UPDATE search_queue SET status='failed', attempts=?, last_error=? WHERE task_id=?",
            (attempts, error[:500], task_id),
        )
        return "failed"
    retry_at = (datetime.now(timezone.utc) + timedelta(seconds=retry_after_seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    conn.execute(
        "UPDATE search_queue SET status='backoff', attempts=?, "
        "next_attempt=?, last_error=? WHERE task_id=?",
        (attempts, retry_at, error[:500], task_id),
    )
    return "backoff"


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM export_state WHERE key=?", (key,)
    ).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO export_state (key, value, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, _utc_now_iso()),
    )


# ---------------- url decode cache ----------------

def decode_cache_get(conn: sqlite3.Connection, encoded_url: str) -> tuple[str | None, str] | None:
    """Return (decoded_url, status) if cached, else None."""
    row = conn.execute(
        "SELECT decoded_url, status FROM url_decode_cache WHERE encoded_url=?",
        (encoded_url,),
    ).fetchone()
    if row is None:
        return None
    return (row["decoded_url"], row["status"])


def decode_cache_put(
    conn: sqlite3.Connection,
    encoded_url: str,
    decoded_url: str | None,
    status: str,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO url_decode_cache "
        "(encoded_url, decoded_url, status, attempted_at) VALUES (?,?,?,?)",
        (encoded_url, decoded_url, status, _utc_now_iso()),
    )


def queue_stats(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    rows = conn.execute(
        "SELECT backend, status, COUNT(*) c FROM search_queue GROUP BY backend, status"
    ).fetchall()
    out: dict[str, dict[str, int]] = {}
    for r in rows:
        out.setdefault(r["backend"], {})[r["status"]] = r["c"]
    return out
