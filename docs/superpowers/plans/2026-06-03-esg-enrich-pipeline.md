# ESG Enrich Pipeline Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an `enrich` stage to `esg-collector` that runs the 3 LLM stages (sentiment filter → title translation → controversy classification) on `articles.db`, then export a web-shaped `esg_events.json` to `gs://esg-scan-data/web/` and repoint the web at it.

**Architecture:** A new pure-logic `enrich/` package (LLM registry + 3 stages, each input→verdict with no DB access) driven by `enrich/runner.py`, which drains a bounded chunk of `esg_status='esg' AND enrich_status='pending'` rows, writes results into new `articles` columns, and is idempotent (failures stay `pending`). `pipeline/export.py` gains `build_esg_events()` that joins `per_ticker/*.json` with the enriched `articles` columns, dedups by `title_hash`, and uploads a public web file. A new systemd timer runs enrich after match under a tight memory cap.

**Tech Stack:** Python 3.13 (stdlib only — `urllib`, `sqlite3`, `csv`, `json`), SQLite (WAL), systemd, gsutil/GCS, Next.js 16 (the 2 API-route repoint). Tests are plain `assert` functions run via `python -m tests.test_enrich`, with the LLM call monkeypatched (no network).

**Spec:** `docs/superpowers/specs/2026-06-03-esg-enrich-pipeline-design.md`

**Working dirs:** Python work in `esg-pipeline/esg-collector/` (run `python`/tests from there). Web repoint in `esg-pipeline/web/`. Git from repo root `esg-pipeline/` — commit with paths like `git add esg-collector/enrich/llm.py`. **Never `git add -A`** (unrelated WIP exists in `cloud-function/`). Branch is `feature/esg-enrich-pipeline` (already checked out) — do not create/switch/push branches.

**Deploy note (do NOT trigger):** pushing `esg-collector/**` to `main` auto-deploys to the VM (and resets the DB-touching workflow). This plan only commits locally on the feature branch; deploy is a later, human-gated step.

**Port sources (read before porting; reproduce logic, change only what each task says):**
- `cloud-function/controversy_classifier.py` — provider registry + `_build_request`/`_call_llm` + `CLASSIFY_PROMPT` + `_validate`/`classify_event`. **Drop** `fetch_article_body`/`_decode_google_news_url` (Jina) entirely.
- `cloud-function/sentiment_filter.py` — `FILTER_PROMPT` + `filter_negative` (batch 5).
- `cloud-function/translator.py` — `TRANSLATE_PROMPT` + `translate_summaries` (batch 30).
- `cloud-function/rss_fetcher.py:102-150` — `load_revenues` + `get_revenue_for_year` (read from `config/companies.csv`, which already has the year/revenue columns).

---

## Chunk 1: Storage columns, LLM registry, revenue

### Task 1: Add enrich columns + queries to `core/storage.py`

**Files:**
- Modify: `esg-collector/core/storage.py`
- Test: `esg-collector/tests/test_enrich.py` (new)

- [ ] **Step 1: Write the failing test**

Create `esg-collector/tests/test_enrich.py`:

```python
"""Enrich-stage tests — no network (LLM call is monkeypatched). Run:
    python -m tests.test_enrich
"""
from __future__ import annotations
import sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_enrich_columns_and_queries() -> None:
    from core import storage
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "e.db"
        storage.init_db(db)
        storage.init_db(db)  # idempotent
        conn = storage.connect(db)
        acols = {r["name"] for r in conn.execute("PRAGMA table_info(articles)")}
        assert {"summary_en", "sentiment", "controversy_level",
                "controversy_justification", "controversy_classified_at",
                "enrich_status"} <= acols, acols
        # an esg-kept, pending row is returned; a non-esg row is not
        conn.execute("INSERT INTO articles (article_id,url_canonical,title,esg_status) "
                     "VALUES ('a::1','u','Phat cong ty vi xa thai','esg')")
        conn.execute("INSERT INTO articles (article_id,url_canonical,title,esg_status) "
                     "VALUES ('a::2','u','khong esg','noise')")
        pend = storage.get_pending_enrich(conn, limit=10)
        ids = {r["article_id"] for r in pend}
        assert ids == {"a::1"}, ids
        # mark_enriched moves it out of pending and stores fields
        storage.mark_enriched(conn, "a::1", sentiment="risk", summary_en="EN",
                              controversy_level="Minor", controversy_justification="x. y.",
                              controversy_classified_at="2026-06-03T00:00:00Z")
        assert not storage.get_pending_enrich(conn, limit=10)
        row = conn.execute("SELECT * FROM articles WHERE article_id='a::1'").fetchone()
        assert row["enrich_status"] == "done" and row["summary_en"] == "EN"
        assert row["sentiment"] == "risk" and row["controversy_level"] == "Minor"
        # mark_dropped path
        conn.execute("INSERT INTO articles (article_id,url_canonical,title,esg_status) "
                     "VALUES ('a::3','u','t','esg')")
        storage.mark_dropped(conn, "a::3")
        r3 = conn.execute("SELECT enrich_status,sentiment FROM articles WHERE article_id='a::3'").fetchone()
        assert r3["enrich_status"] == "dropped" and r3["sentiment"] == "not_risk"
        conn.close()
    print("  enrich_columns_and_queries OK")


def main() -> None:
    if sys.platform == "win32":
        try: sys.stdout.reconfigure(encoding="utf-8")
        except Exception: pass
    print("running enrich tests…")
    test_enrich_columns_and_queries()
    print("ALL OK")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `esg-collector/`): `python -m tests.test_enrich`
Expected: FAIL — `get_pending_enrich` does not exist / columns missing.

- [ ] **Step 3: Add columns to SCHEMA and init_db migration**

In `core/storage.py`, in the `SCHEMA` string's `articles` table, add these columns before the closing `)` (after `severity TEXT`):

```
  summary_en                TEXT,
  sentiment                 TEXT,
  controversy_level         TEXT,
  controversy_justification TEXT,
  controversy_classified_at TEXT,
  enrich_status             TEXT DEFAULT 'pending'
```

In `init_db()`, extend the existing idempotent ALTER loop (the `for col, ddl in [...]` block that adds `ticker_hint`/`esg_status`/...) with:

```python
            ("summary_en",                "ALTER TABLE articles ADD COLUMN summary_en TEXT"),
            ("sentiment",                 "ALTER TABLE articles ADD COLUMN sentiment TEXT"),
            ("controversy_level",         "ALTER TABLE articles ADD COLUMN controversy_level TEXT"),
            ("controversy_justification", "ALTER TABLE articles ADD COLUMN controversy_justification TEXT"),
            ("controversy_classified_at", "ALTER TABLE articles ADD COLUMN controversy_classified_at TEXT"),
            ("enrich_status",             "ALTER TABLE articles ADD COLUMN enrich_status TEXT DEFAULT 'pending'"),
```

After the index block, add an index on enrich draining:

```python
        conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_enrich "
                     "ON articles(esg_status, enrich_status)")
```

Note: `ADD COLUMN enrich_status ... DEFAULT 'pending'` sets every existing row to `'pending'`; the
`get_pending_enrich` filter (`esg_status='esg'`) restricts draining to kept articles, so this is the
backfill — no separate migration flag is needed.

- [ ] **Step 4: Add the queries (after `mark_esg`)**

```python
def get_pending_enrich(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    """Kept-but-not-yet-enriched articles, oldest first, bounded by `limit`."""
    return conn.execute(
        "SELECT * FROM articles "
        "WHERE esg_status='esg' AND enrich_status='pending' "
        "ORDER BY fetched_at ASC LIMIT ?",
        (int(limit),),
    ).fetchall()


def mark_enriched(conn: sqlite3.Connection, article_id: str, *, sentiment: str,
                  summary_en: str | None,
                  controversy_level: str | None = None,
                  controversy_justification: str | None = None,
                  controversy_classified_at: str | None = None) -> None:
    conn.execute(
        "UPDATE articles SET enrich_status='done', sentiment=?, summary_en=?, "
        "controversy_level=?, controversy_justification=?, controversy_classified_at=? "
        "WHERE article_id=?",
        (sentiment, summary_en, controversy_level, controversy_justification,
         controversy_classified_at, article_id),
    )


def mark_dropped(conn: sqlite3.Connection, article_id: str) -> None:
    """Sentiment said not_risk — exclude from the web export, never re-process."""
    conn.execute(
        "UPDATE articles SET enrich_status='dropped', sentiment='not_risk' WHERE article_id=?",
        (article_id,),
    )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m tests.test_enrich`  → Expected: PASS. Also run `python -m tests.test_smoke` → still ALL OK (schema change is additive).

- [ ] **Step 6: Commit**

```bash
git add esg-collector/core/storage.py esg-collector/tests/test_enrich.py
git commit -m "feat(collector): add enrich_status + enrichment columns and queries"
```

---

### Task 2: LLM provider registry — `enrich/llm.py`

Port the registry from `cloud-function/controversy_classifier.py` (lines 45-95 + `_build_request` 221-249 + `_call_llm` 252-279). Read that file first.

**Files:**
- Create: `esg-collector/enrich/__init__.py` (empty)
- Create: `esg-collector/enrich/llm.py`
- Test: `esg-collector/tests/test_enrich.py`

- [ ] **Step 1: Write the failing test** (append a test fn + register in `main()`)

```python
def test_llm_resolve_provider(monkeyenv=None) -> None:
    import importlib, os
    from enrich import llm
    saved = dict(os.environ)
    try:
        for k in list(os.environ):
            if k.endswith("_API_KEY") or k in ("LLM_PROVIDER", "LLM_MODEL"):
                del os.environ[k]
        assert llm.resolve_provider() is None          # no keys → None
        os.environ["GROQ_API_KEY"] = "gsk_test"
        p = llm.resolve_provider()
        assert p and p["name"] == "groq" and p["key"] == "gsk_test"
        assert p["schema"] == "openai" and p["model"]   # default model present
        os.environ["LLM_MODEL"] = "meta-llama/llama-4-scout-17b-16e-instruct"
        assert llm.resolve_provider()["model"] == "meta-llama/llama-4-scout-17b-16e-instruct"
        # build_request shape for openai schema
        url, payload, headers, extract = llm._build_request(p, "hello")
        assert url.startswith("https://api.groq.com") and b"hello" in payload
        assert headers["Authorization"] == "Bearer gsk_test"
    finally:
        os.environ.clear(); os.environ.update(saved)
    print("  llm_resolve_provider OK")
```

(Register `test_llm_resolve_provider()` in `main()`.)

- [ ] **Step 2: Run test to verify it fails** — `python -m tests.test_enrich` → FAIL (`enrich` missing).

- [ ] **Step 3: Implement `enrich/llm.py`**

Create `esg-collector/enrich/__init__.py` (empty). Create `esg-collector/enrich/llm.py` by copying — **verbatim** — from `controversy_classifier.py`: the `PROVIDERS` dict, `AUTO_PICK_ORDER`, `resolve_provider()`, `_build_request(provider, prompt)`, and `_call_llm(provider, prompt, retries=3)`. Add module imports `import json, os, time, urllib.request, urllib.error`. Rename the public caller to `call_llm` (alias `call_llm = _call_llm` or rename the def). Do NOT copy the Jina/decoder functions. The file has no other dependencies.

- [ ] **Step 4: Run test to verify it passes** — `python -m tests.test_enrich` → PASS.

- [ ] **Step 5: Commit**

```bash
git add esg-collector/enrich/__init__.py esg-collector/enrich/llm.py esg-collector/tests/test_enrich.py
git commit -m "feat(enrich): port env-switchable LLM provider registry"
```

---

### Task 3: Revenue lookup — `enrich/revenue.py`

Reimplement `load_revenues` + `get_revenue_for_year` from `cloud-function/rss_fetcher.py:102-150`, reading `config/companies.csv` (already present, columns: `Mã CK, Tên Công ty, 2020..2025` with revenue in billions, comma thousands).

**Files:**
- Create: `esg-collector/enrich/revenue.py`
- Test: `esg-collector/tests/test_enrich.py`

- [ ] **Step 1: Write the failing test** (append + register)

```python
def test_revenue() -> None:
    from enrich import revenue
    rev = revenue.load_revenues()          # reads config/companies.csv
    assert "VIC" in rev and 2024 in rev["VIC"]
    assert rev["VIC"][2024] > 0
    # exact-year hit
    assert revenue.get_revenue_for_year(rev["VIC"], 2024) == (2024, rev["VIC"][2024])
    # missing year → closest (ties → older)
    yr, val = revenue.get_revenue_for_year({2020: 100.0, 2024: 200.0}, 2022)
    assert (yr, val) == (2020, 100.0)
    assert revenue.get_revenue_for_year({}, 2024) is None
    print("  revenue OK")
```

- [ ] **Step 2: Run test to verify it fails** — FAIL (`enrich.revenue` missing).

- [ ] **Step 3: Implement `enrich/revenue.py`**

```python
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


def get_revenue_for_year(per_year: dict[int, float], year: int):
    """Exact-year match; else closest available year (ties → older). (year, rev) or None."""
    if not per_year:
        return None
    if year in per_year:
        return (year, per_year[year])
    chosen = min(per_year.keys(), key=lambda y: (abs(y - year), y))
    return (chosen, per_year[chosen])
```

- [ ] **Step 4: Run test to verify it passes** — `python -m tests.test_enrich` → PASS.

- [ ] **Step 5: Commit**

```bash
git add esg-collector/enrich/revenue.py esg-collector/tests/test_enrich.py
git commit -m "feat(enrich): revenue lookup from config/companies.csv"
```

---

## Chunk 2: The three LLM stages + runner

### Task 4: Sentiment filter — `enrich/sentiment.py`

Port `FILTER_PROMPT` + `filter_negative` + `_extract_labels` from `cloud-function/sentiment_filter.py` verbatim, except imports come from `enrich.llm` and the LLM call uses `enrich.llm.call_llm`.

**Files:**
- Create: `esg-collector/enrich/sentiment.py`
- Test: `esg-collector/tests/test_enrich.py`

- [ ] **Step 1: Write the failing test** (append + register) — monkeypatch the LLM so no network:

```python
def test_sentiment_gate() -> None:
    from enrich import sentiment
    events = [{"ticker": "DBC", "type": "E", "summary": "Phat vi xa thai"},
              {"ticker": "DBC", "type": "S", "summary": "Quy thien tam ho tro nan nhan"}]
    # fake provider + monkeypatch the LLM to label [risk, not_risk]
    fake_provider = {"name": "x", "model": "m", "sleep": 0}
    sentiment.call_llm = lambda prov, prompt, retries=3: {"labels": ["risk", "not_risk"]}
    kept = sentiment.filter_negative(events, provider=fake_provider)
    assert len(kept) == 1 and kept[0]["type"] == "E"
    # LLM failure → keep all (fail-open)
    sentiment.call_llm = lambda prov, prompt, retries=3: None
    assert len(sentiment.filter_negative(events, provider=fake_provider)) == 2
    print("  sentiment_gate OK")
```

- [ ] **Step 2: Run test to verify it fails** — FAIL.

- [ ] **Step 3: Implement** — copy `sentiment_filter.py` into `enrich/sentiment.py` with these changes:
  - Replace `from controversy_classifier import resolve_provider, _build_request` with `from enrich.llm import resolve_provider, call_llm`.
  - Delete the local `_call_llm` def; replace its single call site in `filter_negative` with `parsed = call_llm(provider, prompt)`.
  - Keep `BATCH_SIZE = 5`, `FILTER_PROMPT`, `_extract_labels`, and `filter_negative(events, provider=None)` unchanged otherwise.

- [ ] **Step 4: Run test to verify it passes** — PASS.

- [ ] **Step 5: Commit**

```bash
git add esg-collector/enrich/sentiment.py esg-collector/tests/test_enrich.py
git commit -m "feat(enrich): port sentiment risk-gate (batch 5)"
```

---

### Task 5: Title translator — `enrich/translate.py`

Port `TRANSLATE_PROMPT` + `translate_summaries` + `_extract_translations` from `cloud-function/translator.py`. **Scope: titles only** (that is already what this module does). Use `enrich.llm`.

**Files:**
- Create: `esg-collector/enrich/translate.py`
- Test: `esg-collector/tests/test_enrich.py`

- [ ] **Step 1: Write the failing test** (append + register)

```python
def test_translate() -> None:
    from enrich import translate
    fake_provider = {"name": "x", "model": "m", "sleep": 0}
    translate.call_llm = lambda prov, prompt, retries=3: {"translations": ["Fined for discharge", "EN2"]}
    out = translate.translate_titles(["Phat vi xa thai", "tin 2"], provider=fake_provider)
    assert out == ["Fined for discharge", "EN2"]
    # failure / length mismatch → fall back to VN input
    translate.call_llm = lambda prov, prompt, retries=3: None
    assert translate.translate_titles(["a", "b"], provider=fake_provider) == ["a", "b"]
    print("  translate OK")
```

- [ ] **Step 2: Run test to verify it fails** — FAIL.

- [ ] **Step 3: Implement** — copy `translator.py` into `enrich/translate.py` with:
  - Replace the `from controversy_classifier import resolve_provider` + the local `_build_request`/`_call_llm` with `from enrich.llm import resolve_provider, call_llm`.
  - Rename `translate_summaries(summaries, api_key=None)` → `translate_titles(titles, provider=None)`; drop the `api_key` param; resolve provider via the passed `provider` or `resolve_provider()`.
  - Replace the call site `parsed = _call_llm(provider, prompt)` with `parsed = call_llm(provider, prompt)`.
  - Keep `BATCH_SIZE = 30`, `TRANSLATE_PROMPT`, `_extract_translations`, and the VN-fallback behavior.

- [ ] **Step 4: Run test to verify it passes** — PASS.

- [ ] **Step 5: Commit**

```bash
git add esg-collector/enrich/translate.py esg-collector/tests/test_enrich.py
git commit -m "feat(enrich): port VN→EN title translator (batch 30)"
```

---

### Task 6: Controversy classifier — `enrich/controversy.py`

Port `CLASSIFY_PROMPT`, `_validate`, `_event_year`, `_revenue_display`, `classify_event`, `classify_events` from `cloud-function/controversy_classifier.py`. **Remove all Jina/body-fetching** — body is passed in from the DB.

**Files:**
- Create: `esg-collector/enrich/controversy.py`
- Test: `esg-collector/tests/test_enrich.py`

- [ ] **Step 1: Write the failing test** (append + register)

```python
def test_controversy() -> None:
    from enrich import controversy
    fake_provider = {"name": "x", "model": "m", "sleep": 0}
    captured = {}
    def fake_call(prov, prompt, retries=3):
        captured["prompt"] = prompt
        return {"level": "Major", "cg_indicator": None,
                "justification": "Worker died at plant. Material consequence, no resolution.",
                "confidence": 90}
    controversy.call_llm = fake_call
    event = {"ticker": "HPG", "company": "Hoa Phat", "type": "S",
             "date": "2026-05-26", "summary": "Cong nhan tu vong", "summary_en": "Worker death",
             "source": "Tuoi Tre"}
    body = "Long article body about a fatal accident at the Dung Quat plant " * 50
    out = controversy.classify_event(event, fake_provider, today="2026-06-03",
                                     body=body, revenues={"HPG": {2026: 150000.0}})
    assert out["level"] == "Major" and out["cg_indicator"] is None
    assert out["justification"].count(". ") >= 1
    # body was truncated into the prompt and revenue injected
    assert "Dung Quat" in captured["prompt"]
    assert "150,000 billion VND" in captured["prompt"] or "150000" in captured["prompt"]
    # invalid LLM level → None
    controversy.call_llm = lambda prov, prompt, retries=3: {"level": "Nope", "justification": "x."}
    assert controversy.classify_event(event, fake_provider, today="2026-06-03", body="", revenues={}) is None
    print("  controversy OK")
```

- [ ] **Step 2: Run test to verify it fails** — FAIL.

- [ ] **Step 3: Implement** — copy from `controversy_classifier.py` into `enrich/controversy.py`:
  - Keep `VALID_LEVELS`, `ARTICLE_BODY_MAX_CHARS = 6000`, `SCALE_RULE_THRESHOLD_PCT`, the full `CLASSIFY_PROMPT`, `_validate`, `_event_year`.
  - Replace `from rss_fetcher import get_revenue_for_year` with `from enrich.revenue import get_revenue_for_year`.
  - Replace `from controversy_classifier import ...` / local `_build_request`/`_call_llm` with `from enrich.llm import resolve_provider, call_llm`.
  - **Delete** `JINA_READER_BASE`, `_decode_google_news_url`, `fetch_article_body`.
  - Change `classify_event` signature to `classify_event(event, provider, today, *, body, revenues=None)`. Inside, replace the `article_body = fetch_article_body(...)` block with:
    ```python
    article_body = (body or "").strip()
    if len(article_body) > ARTICLE_BODY_MAX_CHARS:
        article_body = article_body[:ARTICLE_BODY_MAX_CHARS] + "\n...[truncated]"
    if not article_body:
        article_body = "(not available — classify from title/source only)"
    ```
  - Keep `_revenue_display`, the `CLASSIFY_PROMPT.format(...)` call, `parsed = call_llm(provider, prompt)`, and `return _validate(parsed, event.get("type",""))`.
  - `classify_events` is unused by the runner (runner calls `classify_event` per row); you may keep or drop it. If kept, update its body-fetch removal similarly. (Prefer dropping it to avoid dead Jina-shaped code — YAGNI.)

- [ ] **Step 4: Run test to verify it passes** — PASS.

- [ ] **Step 5: Commit**

```bash
git add esg-collector/enrich/controversy.py esg-collector/tests/test_enrich.py
git commit -m "feat(enrich): port controversy classifier (body from DB, no Jina)"
```

---

### Task 7: Orchestrator — `enrich/runner.py`

Drains a bounded chunk; sentiment gate → translate → controversy (Cao only) → write back. Resolves the matched ticker via `alias_matcher` for the revenue lookup.

**Files:**
- Create: `esg-collector/enrich/runner.py`
- Test: `esg-collector/tests/test_enrich.py`

- [ ] **Step 1: Write the failing test** (append + register) — monkeypatch all three stages, no network:

```python
def test_runner_end_to_end() -> None:
    import tempfile, json
    from pathlib import Path
    from core import storage, alias_matcher
    from enrich import runner, sentiment, translate, controversy
    alias_matcher.reload()
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "r.db"
        storage.init_db(db)
        conn = storage.connect(db)
        # one Cao Dabaco E article (kept) + one not_risk article (kept)
        conn.execute("INSERT INTO articles (article_id,url_canonical,title,esg_status,esg_type,severity,body) "
                     "VALUES ('a::1','u','Dabaco bi phat vi xa thai','esg','E','Cao','body text')")
        conn.execute("INSERT INTO articles (article_id,url_canonical,title,esg_status,esg_type,severity) "
                     "VALUES ('a::2','u','Quy thien tam Dabaco ho tro','esg','S','Trung bình')")
        conn.close()
        # monkeypatch stages: sentiment drops a::2; translate echoes EN; controversy → Minor
        runner.sentiment.filter_negative = lambda evs, provider=None: [e for e in evs if "thien tam" not in e["summary"]]
        runner.translate.translate_titles = lambda titles, provider=None: ["EN:" + t for t in titles]
        runner.controversy.classify_event = lambda e, p, today, *, body, revenues=None: {
            "level": "Minor", "cg_indicator": None, "justification": "a. b.", "confidence": 80}
        # force a provider so the runner proceeds
        runner.resolve_provider = lambda: {"name": "x", "model": "m", "sleep": 0}
        n = runner.run(limit=10, db_path=db)
        conn = storage.connect(db)
        r1 = conn.execute("SELECT * FROM articles WHERE article_id='a::1'").fetchone()
        r2 = conn.execute("SELECT * FROM articles WHERE article_id='a::2'").fetchone()
        assert r1["enrich_status"] == "done" and r1["sentiment"] == "risk"
        assert r1["summary_en"] == "EN:Dabaco bi phat vi xa thai"
        assert r1["controversy_level"] == "Minor" and r1["controversy_classified_at"]
        assert r2["enrich_status"] == "dropped" and r2["sentiment"] == "not_risk"
        assert not storage.get_pending_enrich(conn, limit=10)  # all drained
        conn.close()
    print("  runner_end_to_end OK")
```

- [ ] **Step 2: Run test to verify it fails** — FAIL.

- [ ] **Step 3: Implement `enrich/runner.py`**

```python
"""Enrich stage: drain a bounded chunk of kept-but-unenriched articles through
sentiment → title translation → controversy, writing results back to the DB.

Idempotent and OOM-safe: only `esg_status='esg' AND enrich_status='pending'`
rows are processed, `limit` bounds the chunk, and any failure leaves the row
`pending` for the next run. Run as a oneshot:  python -m enrich.runner
"""
from __future__ import annotations
import argparse
import logging
from datetime import datetime, timezone

from core import storage, alias_matcher
from config import settings
from enrich import sentiment, translate, controversy
from enrich.llm import resolve_provider
from enrich.revenue import load_revenues

log = logging.getLogger("enrich")
DEFAULT_LIMIT = 25


def _company_for(ticker: str) -> str:
    """Canonical company name for the prompt — from the alias file, fallback ''."""
    import json
    p = settings.ALIASES_DIR / f"{ticker}.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("company_name", "") or ""
    except Exception:
        return ""


def _primary_ticker(row) -> str | None:
    """Resolve the article's matched ticker (primary = first alias hit)."""
    hits = alias_matcher.match_article({
        "title": row["title"] or "", "description": row["description"] or "",
        "sapo": row["sapo"] or "", "body": row["body"] or "",
    })
    return hits[0].ticker if hits else (row["ticker_hint"] or None)


def run(limit: int = DEFAULT_LIMIT, db_path=None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s/%(levelname)s] %(message)s")
    provider = resolve_provider()
    if not provider:
        log.warning("no LLM provider configured (set GROQ_API_KEY) — skipping enrich")
        return 0
    conn = storage.connect(db_path)
    rows = storage.get_pending_enrich(conn, limit=limit)
    if not rows:
        log.info("no pending articles to enrich")
        conn.close()
        return 0
    log.info("enriching %d articles (provider=%s)", len(rows), provider["name"])

    # 1. sentiment gate (batch). Build minimal event dicts.
    events = [{"article_id": r["article_id"], "ticker": _primary_ticker(r) or "",
               "type": r["esg_type"], "severity": r["severity"],
               "summary": r["title"] or "", "row": r} for r in rows]
    kept = sentiment.filter_negative(events, provider=provider)
    kept_ids = {e["article_id"] for e in kept}
    for e in events:
        if e["article_id"] not in kept_ids:
            storage.mark_dropped(conn, e["article_id"])

    if not kept:
        conn.close()
        return len(rows)

    # 2. translate titles (batch, order-preserving)
    titles = [e["summary"] for e in kept]
    titles_en = translate.translate_titles(titles, provider=provider)

    # 3. controversy for Cao only; write back per article
    revenues = load_revenues()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for e, en in zip(kept, titles_en):
        r = e["row"]
        level = just = classified_at = None
        if r["severity"] == "Cao":
            event = {"ticker": e["ticker"], "company": _company_for(e["ticker"]),
                     "type": e["type"], "date": (r["published_at"] or "")[:10],
                     "summary": e["summary"], "summary_en": en, "source": r["source"] or ""}
            res = controversy.classify_event(event, provider, today,
                                             body=r["body"], revenues=revenues)
            if res:
                level, just, classified_at = res["level"], res["justification"], now_iso
        storage.mark_enriched(conn, e["article_id"], sentiment="risk", summary_en=en,
                              controversy_level=level, controversy_justification=just,
                              controversy_classified_at=classified_at)
    conn.close()
    return len(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    args = ap.parse_args()
    run(limit=args.limit)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes** — `python -m tests.test_enrich` → PASS (all enrich tests). Also `python -m tests.test_smoke` → ALL OK.

- [ ] **Step 5: Commit**

```bash
git add esg-collector/enrich/runner.py esg-collector/tests/test_enrich.py
git commit -m "feat(enrich): runner — sentiment gate, translate, controversy, chunked"
```

---

## Chunk 3: Web export, repoint, deploy

### Task 8: `build_esg_events()` + web export in `pipeline/export.py`

Join `per_ticker/*.json` with enriched `articles` columns by `article_id`, filter to `sentiment='risk'`, dedup by `title_hash` (keep earliest `published_at`), map to the web `EsgEvent` shape, write `esg_events.json` + `top100.json`.

**Files:**
- Modify: `esg-collector/pipeline/export.py`
- Modify: `esg-collector/config/settings.py` (add `WEB_DIR`)
- Test: `esg-collector/tests/test_enrich.py`

- [ ] **Step 1: Write the failing test** (append + register)

```python
def test_build_esg_events() -> None:
    import tempfile, json
    from pathlib import Path
    from core import storage
    from config import settings
    from pipeline import export
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "w.db"; storage.init_db(db); conn = storage.connect(db)
        # two articles, SAME title_hash (same incident, 2 sources) — one risk one earlier
        conn.execute("INSERT INTO articles (article_id,url_canonical,url_original,title,title_hash,"
            "published_at,source,backend,esg_status,esg_type,severity,enrich_status,sentiment,summary_en,"
            "controversy_level,controversy_justification) VALUES "
            "('a::1','c1','o1','Dabaco bi phat','H','2026-05-27T00:00:00Z','Lao Dong','google_rss',"
            "'esg','E','Cao','done','risk','Dabaco fined','Minor','a. b.')")
        conn.execute("INSERT INTO articles (article_id,url_canonical,url_original,title,title_hash,"
            "published_at,source,backend,esg_status,esg_type,severity,enrich_status,sentiment,summary_en) VALUES "
            "('a::2','c2','o2','Dabaco bi phat','H','2026-05-28T00:00:00Z','CafeF','baomoi',"
            "'esg','E','Cao','done','risk','Dabaco fined')")
        # a dropped (not_risk) article must be excluded
        conn.execute("INSERT INTO articles (article_id,url_canonical,title,title_hash,published_at,"
            "esg_status,esg_type,severity,enrich_status,sentiment) VALUES "
            "('a::3','c3','x','H3','2026-05-20T00:00:00Z','esg','S','Trung bình','dropped','not_risk')")
        conn.commit(); conn.close()
        pt = Path(td) / "pt"; pt.mkdir()
        (pt / "DBC.json").write_text(json.dumps({"ticker": "DBC", "articles": [
            {"article_id": "a::1", "url": "c1", "title": "Dabaco bi phat", "published_at": "2026-05-27T00:00:00Z",
             "source": "Lao Dong", "backend": "google_rss", "matched_alias": "Dabaco", "type": "E", "severity": "Cao"},
            {"article_id": "a::2", "url": "c2", "title": "Dabaco bi phat", "published_at": "2026-05-28T00:00:00Z",
             "source": "CafeF", "backend": "baomoi", "matched_alias": "Dabaco", "type": "E", "severity": "Cao"},
            {"article_id": "a::3", "url": "c3", "title": "x", "published_at": "2026-05-20T00:00:00Z",
             "source": "s", "backend": "google_rss", "matched_alias": "Dabaco", "type": "S", "severity": "Trung bình"},
        ]}, ensure_ascii=False), encoding="utf-8")
        events = export.build_esg_events(db_path=db, per_ticker_dir=pt)
        # one event (a::1 & a::2 collapse by title_hash to earliest; a::3 dropped)
        assert len(events) == 1, events
        e = events[0]
        assert e["ticker"] == "DBC" and e["company"]   # company resolved
        assert e["date"] == "2026-05-27" and e["created_at"] is not None
        assert e["summary"] == "Dabaco bi phat" and e["summary_en"] == "Dabaco fined"
        assert e["type"] == "E" and e["severity"] == "Cao"
        assert e["controversy_level"] == "Minor"
        assert e["source"] == "Lao Dong" and e["url"] == "c1"
    print("  build_esg_events OK")
```

- [ ] **Step 2: Run test to verify it fails** — FAIL (`build_esg_events` missing).

- [ ] **Step 3: Add `WEB_DIR` to settings**

In `config/settings.py`, after `PER_TICKER_DIR = ...`:
```python
WEB_DIR = DATA_DIR / "web"
```
and add `WEB_DIR.mkdir(parents=True, exist_ok=True)` next to the other `mkdir` calls at the bottom.

- [ ] **Step 4: Implement `build_esg_events()` + web export in `pipeline/export.py`**

Add near the top: `import csv` is not needed; add `from enrich.runner import _company_for` is NOT allowed (avoid importing runner). Instead read company from companies.csv. Add this helper + builder:

```python
WEB_PREFIX = f"{GCS_BUCKET}/web"


def _company_names() -> dict[str, str]:
    """ticker -> short company name, from config/companies.csv (Mã CK, Tên Công ty)."""
    import csv
    out: dict[str, str] = {}
    p = settings.COMPANIES_CSV
    if not p.exists():
        return out
    with open(p, "r", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            t = (row.get("Mã CK") or "").strip()
            if t:
                out[t] = (row.get("Tên Công ty") or "").strip()
    return out


def build_esg_events(db_path=None, per_ticker_dir: Path | None = None) -> list[dict]:
    """Join per_ticker/*.json with enriched articles columns → web EsgEvent list,
    risk-only, deduped by title_hash (earliest kept), sorted by date desc."""
    per_ticker_dir = per_ticker_dir or settings.PER_TICKER_DIR
    companies = _company_names()
    conn = storage.connect(db_path)
    try:
        # enrich columns keyed by article_id
        enr: dict[str, dict] = {}
        for r in conn.execute(
            "SELECT article_id, title_hash, sentiment, summary_en, controversy_level, "
            "controversy_justification, controversy_classified_at, fetched_at FROM articles"
        ):
            enr[r["article_id"]] = {k: r[k] for k in r.keys()}
    finally:
        conn.close()

    seen: set[tuple[str, str]] = set()          # (ticker, title_hash) already emitted
    events: list[dict] = []
    for pj in sorted(Path(per_ticker_dir).glob("*.json")):
        try:
            doc = json.loads(pj.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        ticker = (doc.get("ticker") or pj.stem).upper()
        # earliest first so the kept representative is the earliest published
        arts = sorted(doc.get("articles") or [],
                      key=lambda a: a.get("published_at") or "")
        for a in arts:
            row = enr.get(a.get("article_id"))
            if not row or row.get("sentiment") != "risk":
                continue            # not enriched, dropped, or pending → skip
            th = row.get("title_hash") or a.get("article_id")
            key = (ticker, th)
            if key in seen:
                continue            # same incident already emitted (earliest wins)
            seen.add(key)
            events.append({
                "ticker": ticker,
                "company": companies.get(ticker, ""),
                "type": a.get("type"),
                "date": (a.get("published_at") or "")[:10],
                "summary": a.get("title") or "",
                "summary_en": row.get("summary_en") or "",
                "severity": a.get("severity"),
                "source": a.get("source") or "",
                "url": a.get("url") or "",
                "controversy_level": row.get("controversy_level") or "",
                "controversy_justification": row.get("controversy_justification") or "",
                "controversy_classified_at": row.get("controversy_classified_at") or "",
                "created_at": row.get("fetched_at"),
                # optional passthrough (web may surface later; not displayed now):
                "backend": a.get("backend"),
                "matched_alias": a.get("matched_alias"),
            })
    events.sort(key=lambda e: e["date"], reverse=True)
    return events


def _write_web_files() -> tuple[Path, Path]:
    settings.WEB_DIR.mkdir(parents=True, exist_ok=True)
    events = build_esg_events()
    ev_path = settings.WEB_DIR / "esg_events.json"
    ev_path.write_text(json.dumps(events, ensure_ascii=False), encoding="utf-8")
    top = [{"ticker": t, "company": c} for t, c in sorted(_company_names().items())]
    top_path = settings.WEB_DIR / "top100.json"
    top_path.write_text(json.dumps(top, ensure_ascii=False), encoding="utf-8")
    log.info("web export: %d events, %d tickers", len(events), len(top))
    return ev_path, top_path


def _upload_web(ev_path: Path, top_path: Path) -> None:
    for src in (ev_path, top_path):
        dst = f"{WEB_PREFIX}/{src.name}"
        _gsutil_cp(src, dst)
        # objects are overwritten each run → re-apply public-read ACL each time
        subprocess.run(["gsutil", "acl", "ch", "-u", "AllUsers:R", dst], check=True)
```

Extend `run()` and `main()` with a `--web` flag:
- add `ap.add_argument("--web", action="store_true", help="build+upload web/esg_events.json")`
- in `run(...)` add a `do_web: bool = False` param; when set: `ev, top = _write_web_files()` and, if `do_upload`, `_upload_web(ev, top)`.
- in `main()` pass `do_web=args.web` and relax the "must pass --ndjson/--upload" guard to also accept `--web`.

- [ ] **Step 5: Run test to verify it passes** — `python -m tests.test_enrich` → PASS. `python -m tests.test_smoke` → ALL OK.

- [ ] **Step 6: Commit**

```bash
git add esg-collector/pipeline/export.py esg-collector/config/settings.py esg-collector/tests/test_enrich.py
git commit -m "feat(export): build+upload web esg_events.json (title_hash dedup, public ACL)"
```

---

### Task 9: systemd enrich unit + deploy wiring

**Files:**
- Create: `esg-collector/deploy/esg-collector-enrich.service`
- Create: `esg-collector/deploy/esg-collector-enrich.timer`
- Modify: `esg-collector/deploy/install.sh` (enable the new timer — match the existing pattern)
- Modify: `esg-collector/deploy/README.md` (document the unit + the one-time public-ACL note)

- [ ] **Step 1: Create the service** `deploy/esg-collector-enrich.service`

```ini
[Unit]
Description=ESG collector — enrich (sentiment/translate/controversy) + web export (oneshot)
After=esg-collector-match.service

[Service]
Type=oneshot
User=esg
Group=esg
WorkingDirectory=/opt/esg-collector/esg-collector
EnvironmentFile=/etc/esg-collector.env
Environment=PYTHONUNBUFFERED=1
# Enrich is I/O-bound (LLM/network) and processes a small bounded chunk, so its
# footprint is tens of MB. Hard cap well under the e2-micro headroom; if it ever
# leaks, only this unit is cgroup-OOM-killed, never the kernel OOM-killer.
MemoryHigh=200M
MemoryMax=250M
CPUQuota=40%
Nice=15
IOSchedulingClass=idle
ExecStart=/opt/esg-collector/.venv/bin/python -m enrich.runner --limit 25
ExecStart=/opt/esg-collector/.venv/bin/python -m pipeline.export --web --upload
StandardOutput=append:/var/log/esg-collector/enrich.log
StandardError=append:/var/log/esg-collector/enrich.log
```

- [ ] **Step 2: Create the timer** `deploy/esg-collector-enrich.timer`

```ini
[Unit]
Description=Run esg-collector enrich + web export every 6 hours, offset after match

[Timer]
# match runs at OnBootSec=10min; enrich starts 40min after boot so it lands well
# after a match run (which is short relative to the 6h cycle), then every 6h.
OnBootSec=40min
OnUnitActiveSec=6h
Persistent=true

[Install]
WantedBy=timers.target
```

- [ ] **Step 3: Wire into `install.sh`**

Read `deploy/install.sh`; wherever it `systemctl enable --now esg-collector-match.timer` (or copies units), add `esg-collector-enrich.timer` the same way. Match the existing copy/enable pattern exactly — do not invent a new mechanism.

- [ ] **Step 4: Document in `deploy/README.md`**

Add a short section: the enrich unit runs the 3 LLM stages + web export; it needs `GROQ_API_KEY` (and optional `LLM_MODEL`) in `/etc/esg-collector.env`; and a one-time public-read ACL is required on the two web objects (`gsutil acl ch -u AllUsers:R gs://esg-scan-data/web/esg_events.json` and `.../top100.json`) — noting the bucket must allow fine-grained ACLs (not Uniform Bucket-Level Access); if UBLA is on, fall back to a public sub-bucket. The export re-applies the ACL after each upload.

- [ ] **Step 5: Verify units parse (best-effort, no VM)**

Run (from repo root): `python - <<'PY'` to sanity-check the ini files are non-empty and contain `[Service]`/`[Timer]`:
```python
for f in ("esg-collector/deploy/esg-collector-enrich.service",
          "esg-collector/deploy/esg-collector-enrich.timer"):
    t = open(f, encoding="utf-8").read()
    assert "[Unit]" in t and t.strip(), f
print("unit files OK")
PY
```
Expected: `unit files OK`.

- [ ] **Step 6: Commit**

```bash
git add esg-collector/deploy/esg-collector-enrich.service esg-collector/deploy/esg-collector-enrich.timer esg-collector/deploy/install.sh esg-collector/deploy/README.md
git commit -m "deploy(collector): enrich timer (capped, after match) + web export + ACL note"
```

---

### Task 10: Repoint the web to the new bucket

**Files:**
- Modify: `web/app/api/events/route.ts`
- Modify: `web/app/api/tickers/route.ts`

- [ ] **Step 1: Repoint events route**

In `web/app/api/events/route.ts`, change `DATA_URL` to:
`https://storage.googleapis.com/esg-scan-data/web/esg_events.json`

- [ ] **Step 2: Repoint tickers route**

In `web/app/api/tickers/route.ts`, change `DATA_URL` to:
`https://storage.googleapis.com/esg-scan-data/web/top100.json`

- [ ] **Step 3: Build the web to confirm it compiles**

Run (from `web/`): `npm run build` → Expected: success (the routes are unchanged in shape; only the URL constant changed).

- [ ] **Step 4: Commit**

```bash
git add web/app/api/events/route.ts web/app/api/tickers/route.ts
git commit -m "feat(web): read events/tickers from esg-scan-data/web (new collector)"
```

---

### Task 11: Full verification

Use @superpowers:verification-before-completion — confirm against real observed output before claiming done.

**Files:** none (verification only).

- [ ] **Step 1: All collector tests pass**

Run (from `esg-collector/`): `python -m tests.test_enrich` and `python -m tests.test_smoke`
Expected: both print `ALL OK`.

- [ ] **Step 2: Dry local web export against a copy of the live DB (if available)**

If a local `articles.db` (or a `raw_esg` NDJSON) is available, run `python -m pipeline.export --web` (no `--upload`) and inspect `data/web/esg_events.json`: confirm it is a JSON array of objects with the `EsgEvent` keys, that DBC contains at least one `type:"E"` event (the wastewater fines), and that no `not_risk` rows appear. If no local DB is available, note this step as deferred to the VM deploy and say so explicitly.

- [ ] **Step 3: Web build + lint**

Run (from `web/`): `npm run build` and `npm run lint` → both clean.

- [ ] **Step 4: Commit (only if fixes were needed)**

```bash
git add -- esg-collector web    # explicit paths only; never -A
git commit -m "fix: address enrich verification findings"
```

---

## Out of scope (do NOT do here)
- Retiring `cloud-function/` and `gs://esg-risk-dashboard`.
- Displaying `backend`/`matched_alias` in the web UI.
- Deploying to the VM / applying the GCS ACL (human-gated; pushing `esg-collector/**` to main auto-deploys).
- Touching `cloud-function/` files (they keep the old pipeline alive until cutover).
