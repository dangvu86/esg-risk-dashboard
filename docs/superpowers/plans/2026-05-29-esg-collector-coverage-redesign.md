# ESG Collector Coverage Redesign — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Switch esg-collector from broad keyword-pool search to a collect-broad / filter-later model — per-company alias search guarantees recall, a new rerunnable downstream ESG keyword filter provides precision.

**Architecture:** Two beats. NHỊP 1 (THU): `queue_builder` emits two task kinds — L1 single-term keyword (monthly, company-agnostic) and L2 `"<alias>"` per-company (BaoMoi deep-pass + Google/Brave monthly tail); workers store raw into `articles` with existing dedup. NHỊP 2 (LỌC): `pipeline.match` runs alias-match then a new `pipeline/esg_filter` (noise blacklist + ESG whitelist + type/severity), writing Tier-2 per_ticker JSON; rerunnable via `--rematch-all` with zero API calls. Aliases (names + subsidiaries) are auto-built from Vietstock.

**Tech Stack:** Python 3.10+, SQLite (WAL), `requests`, `BeautifulSoup`. No new dependencies. Tests follow the existing hand-rolled `tests/test_smoke.py` runner (assert-based, run via `python -m tests.test_smoke`), NOT pytest.

**Spec:** `docs/superpowers/specs/2026-05-29-esg-collector-coverage-redesign-design.md`

**Working directory for all paths:** `esg-pipeline/esg-collector/` (run all commands from there; it is the Python import root — `from config import ...`, `from core import ...`).

**Conventions to follow (existing code):**
- Timestamps via `storage._utc_now_iso()` (ISO `...Z`).
- Schema migrations: additive `ALTER TABLE` inside `storage.init_db()`, guarded by `PRAGMA table_info`.
- Commit messages: `feat:` / `fix:` / `test:` prefix; end with the repo's `Co-Authored-By` trailer if configured. Commit on a feature branch — do NOT push to `main` (push auto-deploys; see `esg-collector/CLAUDE.md`).

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `config/settings.py` | Rolling window end dates | Modify |
| `config/keywords.py` | Single source of ESG vocabulary: tagged `ESG_KEYWORDS` + `NOISE_KEYWORDS` + `HIGH_SEVERITY_KEYWORDS` + helpers | Rewrite |
| `core/storage.py` | Schema migrations; `enqueue_task` kwargs; `_ARTICLE_COLS`; rematch reset | Modify |
| `pipeline/esg_filter.py` | Pure ESG verdict for one article (noise/ESG/type/severity) | Create |
| `pipeline/match.py` | Wire esg_filter into match; body-pending deferral; per_ticker type/severity; reset esg_status | Modify |
| `core/queue_builder.py` | Emit L1 (single-term monthly) + L2 (alias) tasks; weekly helper | Modify |
| `workers/runner.py` | Read `kind`/`ticker`; stamp `ticker_hint`; weekly-split fallback | Modify |
| `backends/baomoi.py` | `MAX_PAGES` 50 → 200 | Modify |
| `alias_builder/fetch_vietstock.py` | Also parse `cong-ty-con-lien-doanh-lien-ket.htm` → subsidiaries | Modify |
| `tests/test_smoke.py` | New tests for all of the above | Modify |

Implementation order matches the spec rollout: foundations → precision filter (fixes existing data) → recall (task gen) → fallback → aliases → operational.

---

## Chunk 1: Foundations — schema, settings, storage

### Task 1.1: Schema migrations + new columns

**Files:**
- Modify: `core/storage.py` (`SCHEMA`, `init_db`, `_ARTICLE_COLS`)
- Test: `tests/test_smoke.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_smoke.py`:

```python
def test_schema_migrations() -> None:
    from core import storage
    import tempfile, sqlite3
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "m.db"
        storage.init_db(db)
        storage.init_db(db)  # idempotent — must not raise
        conn = storage.connect(db)
        qcols = {r["name"] for r in conn.execute("PRAGMA table_info(search_queue)")}
        acols = {r["name"] for r in conn.execute("PRAGMA table_info(articles)")}
        assert {"kind", "ticker"} <= qcols, qcols
        assert {"ticker_hint", "esg_status", "esg_type", "severity"} <= acols, acols
        conn.close()
    print("  schema_migrations OK")
```

Register it in `main()` (add `test_schema_migrations()` call).

- [ ] **Step 2: Run to verify it fails**

Run: `python -m tests.test_smoke`
Expected: AssertionError — `kind`/`ticker_hint` not present.

- [ ] **Step 3: Implement migrations**

In `core/storage.py`, add new columns to the `SCHEMA` `CREATE TABLE` statements (so fresh DBs have them) AND add idempotent `ALTER TABLE` blocks in `init_db()` for existing DBs (mirror the existing `title_hash`/`cached_hits` pattern):

```python
# in SCHEMA, articles table — add columns:
#   ticker_hint TEXT,
#   esg_status  TEXT DEFAULT 'pending',
#   esg_type    TEXT,
#   severity    TEXT
# in SCHEMA, search_queue table — add columns:
#   kind   TEXT DEFAULT 'keyword',
#   ticker TEXT

# in init_db(), after the existing cols block:
for col, ddl in [
    ("ticker_hint", "ALTER TABLE articles ADD COLUMN ticker_hint TEXT"),
    ("esg_status",  "ALTER TABLE articles ADD COLUMN esg_status TEXT DEFAULT 'pending'"),
    ("esg_type",    "ALTER TABLE articles ADD COLUMN esg_type TEXT"),
    ("severity",    "ALTER TABLE articles ADD COLUMN severity TEXT"),
]:
    if col not in cols:
        conn.execute(ddl)
qcols = {r["name"] for r in conn.execute("PRAGMA table_info(search_queue)")}
for col, ddl in [
    ("kind",   "ALTER TABLE search_queue ADD COLUMN kind TEXT DEFAULT 'keyword'"),
    ("ticker", "ALTER TABLE search_queue ADD COLUMN ticker TEXT"),
]:
    if col not in qcols:
        conn.execute(ddl)
conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_esg ON articles(esg_status)")
```

Also add `"ticker_hint"` to the `_ARTICLE_COLS` tuple so `insert_article` persists it.

- [ ] **Step 4: Run to verify it passes**

Run: `python -m tests.test_smoke`
Expected: `schema_migrations OK`, `ALL OK`.

- [ ] **Step 5: Commit**

```bash
git add core/storage.py tests/test_smoke.py
git commit -m "feat(storage): add esg + queue-kind columns with idempotent migrations"
```

### Task 1.2: `enqueue_task` accepts `kind`/`ticker`; alias task_id scheme

**Files:**
- Modify: `core/storage.py` (`enqueue_task`)
- Test: `tests/test_smoke.py`

- [ ] **Step 1: Write the failing test**

```python
def test_enqueue_kinds() -> None:
    from core import storage
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "q.db"
        storage.init_db(db)
        conn = storage.connect(db)
        assert storage.enqueue_task(conn, backend="baomoi", group_key="kw",
            sub_query_ix=3, query="ô nhiễm", after="2024-06-01", before="2024-06-30")
        assert storage.enqueue_task(conn, backend="baomoi", kind="alias",
            ticker="DBC", group_key="alias", sub_query_ix=0, query="Dabaco",
            after="2020-01-01", before="2026-05-29")
        rows = {r["task_id"]: r for r in conn.execute("SELECT * FROM search_queue")}
        assert any(r["kind"] == "alias" and r["ticker"] == "DBC" for r in rows.values())
        # alias re-enqueue is idempotent
        assert storage.enqueue_task(conn, backend="baomoi", kind="alias",
            ticker="DBC", group_key="alias", sub_query_ix=0, query="Dabaco",
            after="2020-01-01", before="2026-05-29") is False
        conn.close()
    print("  enqueue_kinds OK")
```

Register in `main()`.

- [ ] **Step 2: Run to verify it fails**

Run: `python -m tests.test_smoke`
Expected: TypeError — `enqueue_task` got unexpected kwarg `kind`.

- [ ] **Step 3: Implement**

Extend `enqueue_task` signature with `kind: str = "keyword"` and `ticker: str | None = None`. Build `task_id` per kind:

```python
def enqueue_task(conn, *, backend, group_key, sub_query_ix, query, after, before,
                 kind="keyword", ticker=None) -> bool:
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
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m tests.test_smoke`
Expected: `enqueue_kinds OK`.

- [ ] **Step 5: Commit**

```bash
git add core/storage.py tests/test_smoke.py
git commit -m "feat(storage): enqueue_task supports keyword/alias task kinds"
```

### Task 1.3: Rolling window end in settings

**Files:**
- Modify: `config/settings.py`
- Test: `tests/test_smoke.py`

- [ ] **Step 1: Write the failing test**

```python
def test_window_reaches_today() -> None:
    from datetime import date
    from config import settings
    # ends must be >= 2025 so the 2025 gap is in-window
    for end in (settings.BACKFILL_END, settings.BAOMOI_WINDOW_END, settings.BRAVE_WINDOW_END):
        assert end >= "2025-01-01", f"window end {end} predates 2025"
    assert settings.BACKFILL_END >= date.today().isoformat()[:7], "backfill end not rolling to current month"
    print("  window_reaches_today OK")
```

Register in `main()`.

- [ ] **Step 2: Run to verify it fails**

Run: `python -m tests.test_smoke`
Expected: AssertionError — `2024-12-31` predates 2025-current.

- [ ] **Step 3: Implement**

In `config/settings.py`, compute a rolling end (VN today) and use it for the broad ends, keeping the per-backend start floors:

```python
from datetime import datetime
try:
    from zoneinfo import ZoneInfo
    _VN = ZoneInfo("Asia/Ho_Chi_Minh")
except Exception:
    _VN = None
_TODAY = (datetime.now(_VN) if _VN else datetime.utcnow()).date().isoformat()

BACKFILL_START = "2020-01-01"
BACKFILL_END   = _TODAY
BAOMOI_WINDOW_START = "2022-01-01"
BAOMOI_WINDOW_END   = _TODAY
BRAVE_WINDOW_START  = "2020-01-01"
BRAVE_WINDOW_END    = "2021-12-31"   # Brave only fills the pre-BaoMoi tail
```

(Leave `daily` mode in `queue_builder` unchanged — it still tops up recent days.)

- [ ] **Step 4: Run to verify it passes**

Run: `python -m tests.test_smoke`
Expected: `window_reaches_today OK`.

- [ ] **Step 5: Commit**

```bash
git add config/settings.py tests/test_smoke.py
git commit -m "fix(settings): roll backfill/baomoi window end to today (was hardcoded 2024-12-31)"
```

---

## Chunk 2: Precision filter — keywords, esg_filter, match integration

This chunk fixes precision on **already-collected** data — verifiable with `--rematch-all`, no new crawling.

### Task 2.1: Unified keyword config

**Files:**
- Rewrite: `config/keywords.py`
- Reference (read, do not import): `cloud-function/keyword_classifier.py` for the NOISE / ESG / HIGH_SEVERITY term lists.
- Test: `tests/test_smoke.py`

- [ ] **Step 1: Write the failing test**

```python
def test_keyword_config() -> None:
    from config import keywords as kw
    terms = kw.search_terms()
    assert len(terms) == len(set(terms)), "search_terms not deduped"
    assert all(isinstance(t, str) and t for t in terms)
    esg = kw.esg_terms()                      # [(term, type)]
    assert all(t in ("E", "S", "G") for _, t in esg)
    assert "ô nhiễm" in {t for t, _ in esg}
    assert "cổ tức" in set(kw.noise_terms())
    assert any("khởi tố" == t for t in kw.high_severity_terms())
    print("  keyword_config OK")
```

Register in `main()`.

- [ ] **Step 2: Run to verify it fails**

Run: `python -m tests.test_smoke`
Expected: AttributeError — `search_terms` not defined.

- [ ] **Step 3: Implement**

Rewrite `config/keywords.py` with one master tagged list plus the ported blacklists. Keep the existing `KEYWORD_GROUPS`/`all_subqueries`/`count_subqueries` for backward compatibility (other modules/tests reference them), and add:

```python
# Master ESG vocabulary — single source for L1 search AND classify.
# Tag ∈ {E,S,G}. Port/merge from cloud-function/keyword_classifier.py ENV/SOCIAL/GOV.
ESG_KEYWORDS = [
    ("ô nhiễm", "E"), ("xả thải", "E"), ("nước thải", "E"), ("khí thải", "E"),
    ("chất thải", "E"), ("rác thải", "E"), ("vi phạm môi trường", "E"),
    ("khai thác trái phép", "E"), ("hủy hoại môi trường", "E"), ("cá chết", "E"),
    # S
    ("tai nạn lao động", "S"), ("tử vong", "S"), ("cháy nổ", "S"), ("đình công", "S"),
    ("an toàn lao động", "S"), ("ngộ độc", "S"), ("nợ lương", "S"), ("nợ BHXH", "S"),
    ("cưỡng chế", "S"), ("dân kêu cứu", "S"),
    # G
    ("khởi tố", "G"), ("bắt tạm giam", "G"), ("xử phạt", "G"), ("vi phạm", "G"),
    ("thanh tra", "G"), ("tham nhũng", "G"), ("trốn thuế", "G"), ("thao túng", "G"),
    ("nội gián", "G"), ("chậm công bố", "G"), ("UBCKNN", "G"), ("truy nã", "G"),
    # ... merge the full ENV/SOCIAL/GOV lists from keyword_classifier.py here
]

NOISE_KEYWORDS = [
    # port verbatim from keyword_classifier.NOISE_KEYWORDS
    "cổ tức", "lợi nhuận tăng", "doanh thu", "kết quả kinh doanh", "khen thưởng",
    "giải thưởng", "hợp tác", "ký kết", "ra mắt", "khánh thành", "khởi công",
    "IPO", "niêm yết", "thâu tóm", "mua lại", "sáp nhập", "bóng đá", "thể thao",
    # ... (full list)
]

HIGH_SEVERITY_KEYWORDS = [
    # port verbatim from keyword_classifier.HIGH_SEVERITY_KEYWORDS
    "khởi tố", "bắt tạm giam", "truy tố", "tử vong", "chết người",
    "đình chỉ hoạt động", "thu hồi giấy phép", "cháy lớn", "nổ lớn", "sập",
    # ...
]

def search_terms() -> list[str]:
    seen, out = set(), []
    for t, _ in ESG_KEYWORDS:
        if t.lower() not in seen:
            seen.add(t.lower()); out.append(t)
    return out

def esg_terms() -> list[tuple[str, str]]:
    return list(ESG_KEYWORDS)

def noise_terms() -> list[str]:
    return list(NOISE_KEYWORDS)

def high_severity_terms() -> list[str]:
    return list(HIGH_SEVERITY_KEYWORDS)
```

> NOTE for implementer: copy the COMPLETE `ENV_KEYWORDS`/`SOCIAL_KEYWORDS`/`GOV_KEYWORDS`, `NOISE_KEYWORDS`, `HIGH_SEVERITY_KEYWORDS` term contents from `cloud-function/keyword_classifier.py`; the lists above are abbreviated. Tag ESG terms by the group they came from (ENV→E, SOCIAL→S, GOV→G).
>
> Expected overlap is fine and intended: some terms (e.g. "khởi tố", "khai thác trái phép", "quặng lậu") appear in BOTH `ESG_KEYWORDS` and `HIGH_SEVERITY_KEYWORDS`. `search_terms()` de-dups within `ESG_KEYWORDS`; the noise-vs-severity precedence in `esg_filter` (noise drops UNLESS a high-severity term is present) relies on this overlap, so do not try to make the lists disjoint. The Task 2.1 tests pass against the real ported lists ("cổ tức"∈NOISE, "khởi tố"∈HIGH_SEVERITY, "ô nhiễm"∈ENV→E).

- [ ] **Step 4: Run to verify it passes**

Run: `python -m tests.test_smoke`
Expected: `keyword_config OK`. (Also re-run `test_queue_builder_counts` — `count_subqueries` must still return 24.)

- [ ] **Step 5: Commit**

```bash
git add config/keywords.py tests/test_smoke.py
git commit -m "feat(keywords): unified tagged ESG list + ported NOISE/severity blacklists"
```

### Task 2.2: `pipeline/esg_filter.py` — pure verdict

**Files:**
- Create: `pipeline/esg_filter.py`
- Test: `tests/test_smoke.py`

- [ ] **Step 1: Write the failing test**

```python
def test_esg_filter() -> None:
    from pipeline import esg_filter
    # keep: pollution fine, type E, severity Trung bình (300 < 500 triệu)
    v = esg_filter.classify({"title": "Xử phạt Dabaco 300 triệu vì vi phạm môi trường",
                             "sapo": "", "body": ""})
    assert v.keep and v.esg_type == "E" and v.severity == "Trung bình", v
    # noise: dividends → drop
    v = esg_filter.classify({"title": "Cổ đông Dabaco sắp nhận cổ tức bằng tiền mặt",
                             "sapo": "", "body": ""})
    assert not v.keep and v.reason == "noise", v
    # high severity via fine amount (tỷ)
    v = esg_filter.classify({"title": "Phạt công ty X 2 tỷ đồng vì xả thải", "sapo": "", "body": ""})
    assert v.keep and v.severity == "Cao", v
    # non-esg: no ESG keyword
    v = esg_filter.classify({"title": "Công ty X tổ chức đại hội cổ đông thường niên",
                             "sapo": "", "body": ""})
    assert not v.keep and v.reason == "non_esg", v
    print("  esg_filter OK")
```

Register in `main()`.

- [ ] **Step 2: Run to verify it fails**

Run: `python -m tests.test_smoke`
Expected: ModuleNotFoundError — `pipeline.esg_filter`.

- [ ] **Step 3: Implement**

Create `pipeline/esg_filter.py` porting the classify logic (steps 2–4 of `keyword_classifier`; attribution/step-1 stays in `match.py` via `alias_matcher`):

```python
"""ESG verdict for one article — pure, no I/O. Ports the noise/ESG/severity
logic of cloud-function/keyword_classifier.py onto title+sapo+body."""
from __future__ import annotations
import re
from dataclasses import dataclass
from config import keywords as kw

@dataclass(frozen=True)
class Verdict:
    keep: bool
    reason: str           # 'esg' | 'noise' | 'non_esg'
    esg_type: str | None  # E|S|G
    severity: str | None  # Cao|Trung bình

_FINE = re.compile(r'(\d+[\.,]?\d*)\s*(tỷ|triệu)', re.IGNORECASE)

def _content(article: dict) -> str:
    return " ".join((article.get(f) or "") for f in ("title", "sapo", "body")).lower()

def _hits(text: str, terms) -> bool:
    return any(t.lower() in text for t in terms)

def _classify_type(text: str) -> str:
    score = {"E": 0, "S": 0, "G": 0}
    for term, typ in kw.esg_terms():
        if term.lower() in text:
            score[typ] += 1
    if score["E"] > score["G"] and score["E"] > score["S"]:
        return "E"
    if score["S"] > score["E"] and score["S"] > score["G"]:
        return "S"
    if score["G"]: return "G"
    if score["E"]: return "E"
    if score["S"]: return "S"
    return "G"

def _severity(text: str) -> str:
    if _hits(text, kw.high_severity_terms()):
        return "Cao"
    m = _FINE.search(text)
    if m:
        amt = float(m.group(1).replace(",", "."))
        unit = m.group(2).lower()
        if unit == "tỷ" or (unit == "triệu" and amt >= 500):
            return "Cao"
    return "Trung bình"

def classify(article: dict) -> Verdict:
    text = _content(article)
    esg_set = [t for t, _ in kw.esg_terms()]
    high = kw.high_severity_terms()
    if _hits(text, kw.noise_terms()) and not _hits(text, high):
        return Verdict(False, "noise", None, None)
    if not _hits(text, esg_set):
        return Verdict(False, "non_esg", None, None)
    return Verdict(True, "esg", _classify_type(text), _severity(text))
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m tests.test_smoke`
Expected: `esg_filter OK`.

- [ ] **Step 5: Commit**

```bash
git add pipeline/esg_filter.py tests/test_smoke.py
git commit -m "feat(pipeline): add esg_filter (noise/ESG/type/severity verdict)"
```

### Task 2.3: Wire esg_filter into `pipeline/match.py` (with body-pending deferral)

**Files:**
- Modify: `pipeline/match.py` (`run`, per_ticker record, rematch reset)
- Modify: `core/storage.py` — add `mark_esg(conn, article_id, status, esg_type=None, severity=None)`
- Test: `tests/test_smoke.py`

- [ ] **Step 1: Write the failing test**

```python
def test_match_esg_integration() -> None:
    from core import storage, alias_matcher
    from pipeline import match
    from config import settings
    import tempfile, json
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "x.db"
        # point per_ticker + db at temp
        settings.PER_TICKER_DIR = Path(td) / "pt"; settings.PER_TICKER_DIR.mkdir()
        storage.init_db(db)
        conn = storage.connect(db)
        alias_matcher.reload()  # needs DBC.json present in config/aliases
        # (a) real ESG event → kept
        storage.insert_article(conn, {"article_id":"a::1","url_canonical":"u1","url_original":"u1",
            "domain":"d","title":"Xử phạt Dabaco 300 triệu vì vi phạm môi trường","title_hash":"h1",
            "backend":"google_rss","group_key":"alias","sub_query_ix":0,"body_status":"fetched"})
        # (b) noise → dropped (esg_status=noise, not in per_ticker)
        storage.insert_article(conn, {"article_id":"a::2","url_canonical":"u2","url_original":"u2",
            "domain":"d","title":"Cổ đông Dabaco nhận cổ tức","title_hash":"h2",
            "backend":"google_rss","group_key":"alias","sub_query_ix":0,"body_status":"fetched"})
        # (c) alias only matchable in body, body still pending → deferred
        storage.insert_article(conn, {"article_id":"a::3","url_canonical":"u3","url_original":"u3",
            "domain":"d","title":"Một nhà máy bị phạt xả thải","title_hash":"h3",
            "backend":"google_rss","group_key":"alias","sub_query_ix":0,"body_status":"pending"})
        match.run(db_path=db)  # see note in Step 3 re: making run() accept db_path
        rows = {r["article_id"]: r for r in conn.execute("SELECT * FROM articles")}
        assert rows["a::1"]["esg_status"] == "esg" and rows["a::1"]["esg_type"] == "E"
        assert rows["a::2"]["esg_status"] == "noise"
        assert rows["a::3"]["esg_status"] == "pending"  # deferred, body not fetched
        doc = json.loads((settings.PER_TICKER_DIR / "DBC.json").read_text(encoding="utf-8"))
        ids = {a["article_id"] for a in doc["articles"]}
        assert "a::1" in ids and "a::2" not in ids
        assert doc["articles"][0].get("type") == "E"
        conn.close()
    print("  match_esg_integration OK")
```

Register in `main()`.

- [ ] **Step 2: Run to verify it fails**

Run: `python -m tests.test_smoke`
Expected: FAIL — `esg_status` never set / `type` missing in per_ticker / `run` has no `db_path`.

- [ ] **Step 3: Implement**

1. Add `storage.mark_esg`:

```python
def mark_esg(conn, article_id, status, esg_type=None, severity=None) -> None:
    conn.execute(
        "UPDATE articles SET esg_status=?, esg_type=?, severity=? WHERE article_id=?",
        (status, esg_type, severity, article_id),
    )
```

2. In `pipeline/match.py`:
   - Allow `run(..., db_path=None)` and pass to `storage.connect(db_path)` (for testability; default keeps current behavior).
   - After alias match yields `hits` for an article, call `esg_filter.classify(art_d)`:
     - If `verdict.keep`: `storage.mark_esg(conn, id, "esg", verdict.esg_type, verdict.severity)`, and append to per_ticker with extra keys `"type": verdict.esg_type, "severity": verdict.severity, "match_source": <field where alias matched>`. Keep existing `mark_match(... "matched")`.
     - If not keep AND `body_status` in `_BODY_TERMINAL`: `storage.mark_esg(conn, id, verdict.reason, None, None)` and `mark_match(... "unmatched")` (not ESG-relevant).
     - If not keep AND `body_status == "pending"`: leave both `match_status` and `esg_status` as pending (deferred — counts["deferred"]).
   - If alias match found NO ticker: keep existing pending/unmatched logic; esg_status stays `pending` until body terminal, then set `non_esg`.
   - In `rematch_all`: extend the reset to `UPDATE articles SET match_status='pending', matched_at=NULL, esg_status='pending', esg_type=NULL, severity=NULL`.

> Boundary note: alias attribution stays entirely with `alias_matcher` (authoritative). `ticker_hint` is NOT consulted here — it is advisory provenance only.

- [ ] **Step 4: Run to verify it passes**

Run: `python -m tests.test_smoke`
Expected: `match_esg_integration OK`.

- [ ] **Step 5: Commit**

```bash
git add core/storage.py pipeline/match.py tests/test_smoke.py
git commit -m "feat(match): apply esg_filter, defer on pending body, reset esg in --rematch-all"
```

### Task 2.4: Verify precision fix on real data (manual checkpoint)

> Manual/operational — requires a populated DB and is **skipped by an automated executor**. Do not block plan completion on it; flag for the human to run on a host with `data/articles.db`.

- [ ] **Step 1:** On a machine with a populated `data/articles.db` (or a downloaded snapshot), run:
  `python -m pipeline.match --rematch-all`
- [ ] **Step 2:** Confirm `per_ticker/DBC.json` no longer contains the "cổ tức" item and still contains the 3 real environment fines. (Zero API calls — pure reclassification.)
- [ ] **Step 3:** No commit (operational verification).

---

## Chunk 3: Recall — queue generation, worker, backend

### Task 3.1: Weekly/monthly chunk helper + L1 single-term enqueue

**Files:**
- Modify: `core/queue_builder.py`
- Test: `tests/test_smoke.py`

- [ ] **Step 1: Write the failing test**

```python
def test_l1_keyword_tasks() -> None:
    from core import queue_builder as qb
    from config import keywords as kw
    import tempfile
    from pathlib import Path
    terms = kw.search_terms()
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "l1.db"
        # 2 monthly chunks (June, July) × len(terms), one backend
        n = qb.build_keyword_tasks(backends=["google_rss"],
                                   window=("2024-06-01", "2024-07-31"), db_path=db)
        assert n["google_rss"] == 2 * len(terms), n
    print("  l1_keyword_tasks OK")
```

Register in `main()`. (The `db_path` kwarg makes the test hermetic — without it the builder would write into the real `data/articles.db`.)

- [ ] **Step 2: Run to verify it fails**

Run: `python -m tests.test_smoke`
Expected: AttributeError — `build_keyword_tasks` not defined.

- [ ] **Step 3: Implement**

Add `build_keyword_tasks` (L1) to `core/queue_builder.py`, reusing `date_chunks` (monthly), enqueuing `kind='keyword'` one task per (term_index, chunk):

```python
def build_keyword_tasks(backends=None, *, window=None, db_path=None) -> dict[str, int]:
    from config.keywords import search_terms
    backends = backends or ["google_rss", "brave"]
    terms = search_terms()
    storage.init_db(db_path) if db_path else storage.init_db()
    conn = storage.connect(db_path) if db_path else storage.connect()
    inserted = {b: 0 for b in backends}
    try:
        for backend in backends:
            start, end = window if window else (settings.BACKFILL_START, settings.BACKFILL_END)
            for after, before in date_chunks(start, end, settings.CHUNK_MONTHS):
                for ix, term in enumerate(terms):
                    if storage.enqueue_task(conn, backend=backend, kind="keyword",
                            group_key="kw", sub_query_ix=ix, query=term,
                            after=after, before=before):
                        inserted[backend] += 1
    finally:
        conn.close()
    return inserted
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m tests.test_smoke`
Expected: `l1_keyword_tasks OK`.

- [ ] **Step 5: Commit**

```bash
git add core/queue_builder.py tests/test_smoke.py
git commit -m "feat(queue): L1 single-term keyword task generation"
```

### Task 3.2: L2 alias task generation (names all backends; subs BaoMoi-only)

**Files:**
- Modify: `core/queue_builder.py`
- Modify: `core/alias_matcher.py` — expose `strong_aliases(ticker)` split into names vs subsidiaries (or a small loader helper that reads the alias JSON `names`/`subsidiaries`)
- Test: `tests/test_smoke.py`

- [ ] **Step 1: Write the failing test**

```python
def test_l2_alias_tasks() -> None:
    from core import queue_builder as qb
    from core.queue_builder import _load_alias_lists
    from config import settings
    import tempfile
    from pathlib import Path
    names, subs = _load_alias_lists("DBC")   # reads config/aliases/DBC.json (must exist in repo)
    assert names and subs, "DBC.json must have names + subsidiaries"
    a_sub = subs[0]
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "l2.db"
        n = qb.build_alias_tasks(tickers=["DBC"], db_path=db)
        # all three backends produced alias tasks
        assert n["baomoi"] > 0 and n["google_rss"] > 0 and n["brave"] > 0, n
        from core import storage
        conn = storage.connect(db)
        # subsidiaries ARE searched on baomoi but NOT on google/brave
        baomoi_q = {r["query"] for r in conn.execute(
            "SELECT query FROM search_queue WHERE backend='baomoi' AND kind='alias'")}
        google_q = {r["query"] for r in conn.execute(
            "SELECT query FROM search_queue WHERE backend='google_rss' AND kind='alias'")}
        assert a_sub in baomoi_q, "subsidiary not searched on baomoi"
        assert a_sub not in google_q, "subsidiary must NOT be searched on google"
        # baomoi alias tasks are single deep-pass: all share the window start as `after`
        afters = {r["after"] for r in conn.execute(
            "SELECT after FROM search_queue WHERE backend='baomoi' AND kind='alias'")}
        assert afters == {settings.BAOMOI_WINDOW_START}, afters
        conn.close()
    print("  l2_alias_tasks OK")
```

Register in `main()`. (Assertions are behavioral — they lock in the design intent (subs-on-BaoMoi-only, single deep pass) rather than a brittle raw count. Note: BaoMoi count (~names+subs) is actually *smaller* than Google's (names × monthly tail chunks); do NOT assert `baomoi > google`.)

- [ ] **Step 2: Run to verify it fails**

Run: `python -m tests.test_smoke`
Expected: AttributeError — `build_alias_tasks`.

- [ ] **Step 3: Implement**

Add an alias loader (read `config/aliases/<TICKER>.json` → `{"names": [...], "subsidiaries": [...]}`) and `build_alias_tasks`:

```python
def _load_alias_lists(ticker):
    import json
    p = settings.ALIASES_DIR / f"{ticker}.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    return data.get("names") or [], data.get("subsidiaries") or []

def build_alias_tasks(tickers=None, *, window=None, db_path=None) -> dict[str,int]:
    import csv
    if tickers is None:
        with open(settings.COMPANIES_CSV, encoding="utf-8-sig") as f:
            tickers = [ (r.get("Mã CK") or r.get("Ma CK") or "").strip()
                        for r in csv.DictReader(f) ]
            tickers = [t for t in tickers if t]
    storage.init_db(db_path) if db_path else storage.init_db()
    conn = storage.connect(db_path) if db_path else storage.connect()
    inserted = {"baomoi": 0, "google_rss": 0, "brave": 0}
    try:
        for tk in tickers:
            names, subs = _load_alias_lists(tk)
            # BaoMoi: names + subs, one deep pass each (no date chunk → single task spanning full window)
            for ix, alias in enumerate(names + subs):
                if storage.enqueue_task(conn, backend="baomoi", kind="alias", ticker=tk,
                        group_key="alias", sub_query_ix=ix, query=alias,
                        after=settings.BAOMOI_WINDOW_START, before=settings.BAOMOI_WINDOW_END):
                    inserted["baomoi"] += 1
            # Google/Brave: NAMES only, monthly chunks over the 2020-2021 tail
            for backend, (start, end) in (("google_rss", ("2020-01-01","2021-12-31")),
                                          ("brave", (settings.BRAVE_WINDOW_START, settings.BRAVE_WINDOW_END))):
                for after, before in date_chunks(start, end, settings.CHUNK_MONTHS):
                    for ix, alias in enumerate(names):
                        if storage.enqueue_task(conn, backend=backend, kind="alias", ticker=tk,
                                group_key="alias", sub_query_ix=ix, query=alias,
                                after=after, before=before):
                            inserted[backend] += 1
    finally:
        conn.close()
    return inserted
```

> NOTE: BaoMoi alias task uses one chunk = the whole window because BaoMoi ignores date params and paginates client-side (the `after_ix` in task_id stays unique per alias because `after` is constant per alias; that's fine — one task per alias). If two aliases share the same index across name/sub lists, the combined `names + subs` enumerate keeps `ix` globally unique within the ticker.

- [ ] **Step 4: Run to verify it passes**

Run: `python -m tests.test_smoke`
Expected: `l2_alias_tasks OK`.

- [ ] **Step 5: Commit**

```bash
git add core/queue_builder.py core/alias_matcher.py tests/test_smoke.py
git commit -m "feat(queue): L2 per-company alias tasks (names all backends, subs BaoMoi-only)"
```

### Task 3.3: CLI wiring for the new builders

**Files:**
- Modify: `core/queue_builder.py` (`main`)
- Test: manual (CLI smoke)

- [ ] **Step 1:** Extend `queue_builder.main()` argparse with `--mode {backfill,daily,keyword,alias}` (keyword → `build_keyword_tasks`, alias → `build_alias_tasks`); keep existing backfill/daily behavior.
- [ ] **Step 2:** Run `python -m core.queue_builder --mode alias --since 2024-06-01 --until 2024-06-30` against a temp/local DB; confirm it prints per-backend counts without error.
- [ ] **Step 3: Commit**

```bash
git add core/queue_builder.py
git commit -m "feat(queue): CLI modes for keyword/alias task generation"
```

### Task 3.4: Worker reads kind/ticker, stamps ticker_hint

**Files:**
- Modify: `workers/runner.py` (`_process_task`)
- Test: `tests/test_smoke.py`

- [ ] **Step 1: Write the failing test**

```python
def test_worker_stamps_ticker_hint() -> None:
    from workers import runner
    from core import storage
    import tempfile
    from pathlib import Path
    class FakeBackend:
        name = "baomoi"
        @staticmethod
        def fetch(q, a, b):
            return [{"url":"https://x.vn/a-1.html","title":"t","published_at":"2024-06-01","source":"s"}]
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "w.db"; storage.init_db(db); conn = storage.connect(db)
        task = {"task_id":"t1","query":"Dabaco","after":"2020-01-01","before":"2026-01-01",
                "group_key":"alias","sub_query_ix":0,"kind":"alias","ticker":"DBC"}
        runner._process_task(conn, FakeBackend, task)
        row = conn.execute("SELECT ticker_hint FROM articles LIMIT 1").fetchone()
        assert row["ticker_hint"] == "DBC", dict(row)
        conn.close()
    print("  worker_ticker_hint OK")
```

Register in `main()`.

- [ ] **Step 2: Run to verify it fails**

Run: `python -m tests.test_smoke`
Expected: ticker_hint is None.

- [ ] **Step 3: Implement**

In `runner._process_task`, read `task["kind"]`/`task["ticker"]` (use `.get` for legacy rows) and add `"ticker_hint": task.get("ticker")` to the `rec` dict for alias tasks.

- [ ] **Step 4: Run to verify it passes**

Run: `python -m tests.test_smoke`
Expected: `worker_ticker_hint OK`.

- [ ] **Step 5: Commit**

```bash
git add workers/runner.py tests/test_smoke.py
git commit -m "feat(runner): stamp ticker_hint provenance for alias tasks"
```

### Task 3.5: BaoMoi MAX_PAGES bump

**Files:**
- Modify: `backends/baomoi.py:28`

- [ ] **Step 1:** Change `MAX_PAGES = 50` → `MAX_PAGES = 200`. (Early-stop on date cutoff keeps low-volume queries fast.)
- [ ] **Step 2:** Run existing smoke tests (`python -m tests.test_smoke`) — no behavior change expected (no network in smoke).
- [ ] **Step 3: Commit**

```bash
git add backends/baomoi.py
git commit -m "fix(baomoi): raise MAX_PAGES to 200 so alias deep-pass reaches the archive floor"
```

---

## Chunk 4: Weekly-split fallback (worker emits child tasks)

### Task 4.1: Google alias-month near-cap → re-enqueue as 4 weekly child tasks

**Files:**
- Modify: `workers/runner.py` (`_process_task` return / `run` loop)
- Modify: `core/queue_builder.py` — add `weekly_subchunks(after, before)` helper
- Test: `tests/test_smoke.py`

- [ ] **Step 1: Write the failing test**

```python
def test_weekly_subchunks() -> None:
    from core.queue_builder import weekly_subchunks
    weeks = weekly_subchunks("2024-06-01", "2024-06-30")
    assert len(weeks) >= 4 and weeks[0][0] == "2024-06-01"
    assert all(a <= b for a, b in weeks)
    print("  weekly_subchunks OK")

def test_runner_splits_near_cap() -> None:
    from workers import runner
    from core import storage
    import tempfile
    from pathlib import Path
    class CapBackend:
        name = "google_rss"
        @staticmethod
        def fetch(q, a, b):
            return [{"url": f"https://x.vn/a-{i}.html", "title": "t", "published_at": a} for i in range(95)]
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "s.db"; storage.init_db(db); conn = storage.connect(db)
        task = {"task_id":"google_rss:alias:DBC:0:2024-06-01","query":"Dabaco",
                "after":"2024-06-01","before":"2024-06-30","group_key":"alias",
                "sub_query_ix":0,"kind":"alias","ticker":"DBC"}
        runner._maybe_split(conn, CapBackend, task, n_items=95)
        kids = conn.execute("SELECT COUNT(*) c FROM search_queue WHERE kind='alias'").fetchone()["c"]
        assert kids >= 4, kids
        conn.close()
    print("  runner_splits_near_cap OK")
```

Register both in `main()`.

- [ ] **Step 2: Run to verify they fail**

Run: `python -m tests.test_smoke`
Expected: AttributeError — `weekly_subchunks` / `_maybe_split`.

- [ ] **Step 3: Implement**

1. `core/queue_builder.weekly_subchunks(after, before)` → list of (after, before) ~7-day spans covering the month.
2. `runner._maybe_split(conn, backend_mod, task, n_items)`: only when `backend_mod.name == "google_rss"`, `task["kind"] == "alias"`, and `n_items >= 90`, enqueue weekly child tasks via `storage.enqueue_task(... kind="alias", ticker=task["ticker"], after=week_after, before=week_before)`. The child `task_id` differs from the parent because `after` differs, so no collision. Call `_maybe_split` from `run()` right after a successful `mark_task_done`.

> This is the ONLY place a worker writes to `search_queue`. Keep it isolated in `_maybe_split` for clarity.

- [ ] **Step 4: Run to verify they pass**

Run: `python -m tests.test_smoke`
Expected: `weekly_subchunks OK`, `runner_splits_near_cap OK`.

- [ ] **Step 5: Commit**

```bash
git add workers/runner.py core/queue_builder.py tests/test_smoke.py
git commit -m "feat(runner): split near-cap Google alias months into weekly child tasks"
```

---

## Chunk 5: Subsidiary alias builder

### Task 5.1: Parse subsidiaries from Vietstock dedicated page

**Files:**
- Modify: `alias_builder/fetch_vietstock.py`
- Test: `tests/test_smoke.py` (parse a saved HTML fixture — no network in CI)

- [ ] **Step 1: Save a fixture (manual, one-time)**

Download once and commit a trimmed fixture:
`python -c "import urllib.request,ssl,gzip; ctx=ssl.create_default_context(); ctx.options|=0x4; ..."`
Save to `tests/fixtures/vietstock_DBC_subs.html` (the implementer fetches `https://finance.vietstock.vn/DBC/cong-ty-con-lien-doanh-lien-ket.htm` once and saves it). If network is unavailable in the dev env, hand-craft a minimal fixture — it MUST contain rows whose visible text matches `_SUB_RE`, i.e. `<company name> <number> ( Tr. VND )`, e.g. `Công ty TNHH Thức ăn chăn nuôi Nasaco Hà Nam 85,000 ( Tr. VND ) 99.94` and `Công ty TNHH Dabaco Thanh Hóa 100,000 ( Tr. VND ) 99.99`. Otherwise `parse_subsidiaries` returns `[]` and the `any(...)` assertions fail with a confusing message.

- [ ] **Step 2: Write the failing test**

```python
def test_parse_subsidiaries() -> None:
    from alias_builder import fetch_vietstock as fv
    from pathlib import Path
    html = Path("tests/fixtures/vietstock_DBC_subs.html").read_text(encoding="utf-8")
    subs = fv.parse_subsidiaries(html)
    assert any("Nasaco" in s for s in subs)
    assert any("Dabaco Thanh Hóa" in s for s in subs)
    shorts = fv.short_aliases(subs)
    assert "Nasaco" in shorts
    # generic-token guard: do not emit bare over-common tokens
    assert "Minh Phát" not in shorts
    print("  parse_subsidiaries OK")
```

Register in `main()`.

- [ ] **Step 3: Run to verify it fails**

Run: `python -m tests.test_smoke`
Expected: AttributeError — `parse_subsidiaries`.

- [ ] **Step 4: Implement**

Add to `alias_builder/fetch_vietstock.py`:

```python
import re
_SUB_RE = re.compile(r'((?:CTCP|Công ty|Tổng [Cc]ông ty|Tập đoàn)[^()]{3,80}?)\s+[\d,.]+\s*\(\s*Tr\. VND', re.I)
_LEGAL_PREFIX = re.compile(r'^(CTCP|Công ty (?:TNHH|cổ phần|CP)(?: MTV)?|Tổng Công ty|Tập đoàn)\s+', re.I)
_GENERIC = {"minh phát", "song lập", "gia phước", "thành phúc"}  # extend as needed

def parse_subsidiaries(html: str) -> list[str]:
    seen, out = set(), []
    for m in _SUB_RE.finditer(BeautifulSoup(html, "html.parser").get_text(" ", strip=True)):
        name = re.sub(r"\s+", " ", m.group(1)).strip(" -")
        if name and name.lower() not in seen:
            seen.add(name.lower()); out.append(name)
    return out

def short_aliases(full_names: list[str]) -> list[str]:
    out = []
    for n in full_names:
        short = _LEGAL_PREFIX.sub("", n).strip()
        # keep the full name always (specific & safe); add short token if distinctive
        if short and short.lower() not in _GENERIC and len(short.split()) <= 4:
            out.append(short)
    return out
```

Then in `build_alias(ticker)`: fetch the second URL `…/cong-ty-con-lien-doanh-lien-ket.htm`, run `parse_subsidiaries`, set `data["subsidiaries"] = parse_subsidiaries(subs_html)` and add `short_aliases(...)` distinctive tokens; update `derived_from` to include `"vietstock_subsidiaries"`.

- [ ] **Step 5: Run to verify it passes**

Run: `python -m tests.test_smoke`
Expected: `parse_subsidiaries OK`.

- [ ] **Step 6: Commit**

```bash
git add alias_builder/fetch_vietstock.py tests/fixtures/vietstock_DBC_subs.html tests/test_smoke.py
git commit -m "feat(alias): auto-build subsidiaries from Vietstock dedicated page"
```

### Task 5.2: Build aliases for all tickers (operational)

- [ ] **Step 1:** Run `python -m alias_builder.fetch_vietstock --all --delay 3` (writes `config/aliases/<TICKER>.json` for the ~97 missing; existing DBC/KDH/DGC skipped unless `--force`).
- [ ] **Step 2:** Spot-check 3–5 generated files for sane `names`/`subsidiaries`.
- [ ] **Step 3: Commit** the generated alias JSONs.

```bash
git add config/aliases/
git commit -m "chore(alias): build name+subsidiary aliases for all tracked tickers"
```

---

## Chunk 6: Operational rollout (no code — run after merge/deploy)

### Task 6.1: Deploy and enqueue

- [ ] **Step 1:** Merge the feature branch to `main` (push auto-deploys via the GitHub Action; see `esg-collector/CLAUDE.md`). The deploy runs `storage.init_db()` → applies migrations.
- [ ] **Step 2:** On the VM (via the documented manual-trigger path, not ad-hoc SSH), enqueue: `python -m core.queue_builder --mode alias` then `--mode keyword`.
- [ ] **Step 3:** Let the 4 workers drain over several days; monitor via the existing `status.timer` GCS bundle (`_setup/logs.tar.gz`) — watch `queue_stats` (`done`/`backoff`/`failed`) and `matched`/`esg` counts.
- [ ] **Step 4:** Once drained, verify `per_ticker/DBC.json` contains the Nov-2025 fine cluster and the cổ tức item is absent.

---

## Verification Checklist (whole plan)

- [ ] `python -m tests.test_smoke` → `ALL OK` (all new tests registered in `main()`).
- [ ] `--rematch-all` reclassifies the existing pool with zero API calls; cổ tức dropped, real fines kept.
- [ ] `queue_builder --mode alias`/`--mode keyword` emit the expected task kinds.
- [ ] Live: a DBC alias run lands the Nov-2025 cluster in per_ticker.
- [ ] Migrations idempotent on the production DB (init_db twice is a no-op).
