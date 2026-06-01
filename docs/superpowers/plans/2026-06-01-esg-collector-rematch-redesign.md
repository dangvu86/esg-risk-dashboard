# ESG Collector Rematch Redesign — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `pipeline.match --rematch-all` complete on the e2-micro VM without OOM, without wedging the VM, and without changing which tickers each article matches.

**Architecture:** Three independent changes — (1) a single-pass consuming-`finditer` alias matcher (same public API, behaviour-preserving), (2) a chunked snapshot-ids-then-paginate rematch loop (bounded RAM, autocommit-safe), (3) a detached `systemd-run` rematch wrapper so the CI deploy fires-and-returns like backfill.

**Tech Stack:** Python 3 stdlib (`re`, `sqlite3`, `json`), bash + systemd on the VM, GitHub Actions. No new dependencies. Tests follow the repo's existing assert-style harness (`python -m tests.<module>`), **not** pytest.

**Spec:** `docs/superpowers/specs/2026-06-01-esg-collector-rematch-redesign-design.md`

**Branch:** `rematch-redesign` (already created).

**Conventions:**
- Tests are plain functions with `assert`, gathered in a `main()`, run via `python -m tests.<module>` from `esg-collector/`. See `tests/test_smoke.py` for the pattern (sys.path insert, Vietnamese-safe stdout).
- All commands below run from `d:/Claude/ESG scan/esg-pipeline/esg-collector/` unless noted.
- Commit after each green step.

---

## Chunk 1: Single-pass matcher + equivalence gate

Replaces the per-ticker × per-alias `re.search` loop in `core/alias_matcher.py`
with one combined longest-first alternation per weight tier, scanned with a
consuming `finditer`. Public API (`AliasHit`, `match_text`, `match_article`,
`reload`, `loaded_tickers`) is unchanged. A new equivalence test freezes a copy
of the OLD matcher and asserts the new one yields the same `{(ticker, location)}`
set over a committed fixture.

### Task 1.1: Fixture corpus

**Files:**
- Create: `tests/fixtures/matcher_corpus.jsonl`

- [ ] **Step 1: Write the fixture**

Each line is a JSON object `{"text": "...", "note": "..."}`. Cover: HCM city
false positives, real HSC hits, FRT brands, DGC full-brand vs bare "Đức Giang",
a Dabaco/subsidiary hit, diacritic word-boundary cases, negatives, and at least
one cross-ticker nested-substring case. ~40–60 lines. Example lines:

```jsonl
{"text": "Xổ số kiến thiết TP.HCM giảm lãi do bị phạt thuế 110 tỷ đồng", "note": "HCM city — must NOT hit HCM ticker"}
{"text": "Công ty Chứng khoán TP.Hồ Chí Minh (HSC) bị xử phạt 125 triệu đồng", "note": "real HSC -> HCM ticker"}
{"text": "Nhiều nhà thuốc của Công ty Cổ phần Dược phẩm FPT Long Châu bị xử phạt", "note": "FRT brand"}
{"text": "Cổ phiếu Hóa chất Đức Giang bị hạn chế giao dịch", "note": "DGC full brand"}
{"text": "Dabaco bị phạt vì xả thải ra môi trường", "note": "DBC name"}
{"text": "Nasaco vừa thông báo kế hoạch mở rộng", "note": "DBC subsidiary"}
{"text": "Đội tuyển Việt Nam thắng 3-0", "note": "negative — no ticker"}
```

**Required:** the fixture MUST include at least one **cross-ticker
nested-substring** line — the only scenario where the consuming scan can diverge
from per-alias search — so the equivalence gate is meaningful. Pick a real pair
from the alias files where one ticker's alias is a substring of another's (e.g. a
bare short token vs a longer brand containing it) and write a line where the
short token appears ONLY inside the longer one. If no such pair exists in the
current alias data, document that in the `note` and add a synthetic-looking but
representative line so the test still exercises the path.

- [ ] **Step 2: Commit**

```bash
git add tests/fixtures/matcher_corpus.jsonl
git commit -m "test: matcher equivalence fixture corpus"
```

### Task 1.2: Equivalence test (write it against the CURRENT matcher first — must pass before refactor)

**Files:**
- Create: `tests/test_rematch.py`

- [ ] **Step 1: Write the test module with a frozen legacy reference matcher**

```python
"""Rematch-redesign tests — no network. Run:  python -m tests.test_rematch

  - matcher equivalence: new alias_matcher vs a frozen copy of the old
    per-alias matcher, over tests/fixtures/matcher_corpus.jsonl
  - chunked rematch correctness (Task 2.x)
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import settings  # noqa: E402

_FIELDS = ("title", "description", "sapo", "body")
_STRONG_FIELDS = ("names", "subsidiaries", "projects")
_WEAK_FIELDS = ("locations",)


# ---- frozen copy of the OLD matcher (reference implementation) ----
def _legacy_compile(alias: str) -> re.Pattern:
    esc = re.escape(alias.strip())
    return re.compile(rf"(?<!\w){esc}(?!\w)", re.IGNORECASE | re.UNICODE)


def _legacy_index(aliases_dir: Path):
    index = {}
    for p in sorted(Path(aliases_dir).glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        ticker = (data.get("ticker") or p.stem).upper()
        items, seen = [], set()
        for field, weight in [(f, 1.0) for f in _STRONG_FIELDS] + [(f, 0.3) for f in _WEAK_FIELDS]:
            for a in data.get(field) or []:
                a = (a or "").strip()
                if not a or len(a) < 2 or a.lower() in seen:
                    continue
                seen.add(a.lower())
                items.append((a, weight, _legacy_compile(a)))
        index[ticker] = items
    return index


def _legacy_match_article(index, article, include_weak=False):
    final = {}
    for field in _FIELDS:
        text = article.get(field) or ""
        if not text:
            continue
        for ticker, aliases in index.items():
            if ticker in final:
                continue
            for alias, weight, rx in aliases:
                if not include_weak and weight < 1.0:
                    continue
                if rx.search(text):
                    final[ticker] = (ticker, field)
                    break
    return {v for v in final.values()}


def _load_corpus():
    path = ROOT / "tests" / "fixtures" / "matcher_corpus.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_matcher_equivalence() -> None:
    from core import alias_matcher
    alias_matcher.reload()
    legacy = _legacy_index(settings.ALIASES_DIR)
    corpus = _load_corpus()
    divergences = []
    for row in corpus:
        art = {"title": row["text"]}  # single field keeps location deterministic
        new = {(h.ticker, h.location) for h in alias_matcher.match_article(art)}
        old = _legacy_match_article(legacy, art)
        if new != old:
            divergences.append((row["text"], sorted(old), sorted(new)))
    for text, old, new in divergences:
        print(f"  DIVERGENCE: {text!r}\n    old={old}\n    new={new}")
    assert not divergences, f"{len(divergences)} (ticker,location) divergences"
    print("  matcher_equivalence OK")


def main() -> None:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    print("running rematch tests…")
    test_matcher_equivalence()
    print("ALL OK")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run it against the UNCHANGED matcher — must PASS now**

Run: `python -m tests.test_rematch`
Expected: `matcher_equivalence OK` / `ALL OK`. (The current matcher trivially
equals itself-as-reference. This proves the test + fixture are wired correctly
**before** we change the matcher.)

- [ ] **Step 3: Commit**

```bash
git add tests/test_rematch.py
git commit -m "test: matcher equivalence harness (passes against current matcher)"
```

### Task 1.3: Refactor the matcher to single-pass

**Files:**
- Modify: `core/alias_matcher.py` (full rewrite of the index/match internals; API unchanged)
- Test: `tests/test_rematch.py::test_matcher_equivalence`, `tests/test_smoke.py::test_alias_matcher`

- [ ] **Step 1: Rewrite `core/alias_matcher.py`**

```python
"""Match free text against the alias pool to determine which tickers it covers.

Aliases live in `config/aliases/<TICKER>.json`. Loaded once at import (also via
`reload()`). All strong aliases (names/subsidiaries/projects) are compiled into
ONE longest-first alternation; weak aliases (locations) into a second one. Each
text field is scanned with a single consuming `finditer`, and the matched alias
text is mapped back to its owning ticker(s).

Strong aliases: names, subsidiaries, projects (weight 1.0).
Weak  aliases: locations (weight 0.3, filtered out by default).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from config import settings


@dataclass(frozen=True)
class AliasHit:
    ticker: str
    alias: str
    location: str    # title | description | sapo | body
    weight: float    # 1.0 strong, 0.3 weak


_STRONG_FIELDS = ("names", "subsidiaries", "projects")
_WEAK_FIELDS = ("locations",)

# lowercased alias string -> list of (ticker, original_alias, weight)
_OWNERS: dict[str, list[tuple[str, str, float]]] = {}
# lowercased alias A -> owners of OTHER aliases B that are word-bounded
# substrings of A. So when the consuming scan matches the longer A, we also
# emit the tickers of every shorter alias nested inside it — recovering the
# overlapping matches a non-overlapping `finditer` would otherwise drop, and
# making the new matcher exactly equivalent to the old per-alias search.
_NESTED: dict[str, list[tuple[str, str, float]]] = {}
# every ticker whose file loaded (even with no usable aliases)
_TICKERS: set[str] = set()
# combined consuming patterns
_PATTERN_STRONG: re.Pattern | None = None
_PATTERN_ALL: re.Pattern | None = None


def _build_pattern(aliases: set[str]) -> re.Pattern | None:
    if not aliases:
        return None
    # longest-first so the longer alias wins at a shared position
    ordered = sorted(aliases, key=len, reverse=True)
    alt = "|".join(re.escape(a) for a in ordered)
    return re.compile(rf"(?<!\w)(?:{alt})(?!\w)", re.IGNORECASE | re.UNICODE)


def _bounded(needle: str, haystack: str) -> bool:
    """True if `needle` occurs in `haystack` with our word boundaries."""
    rx = re.compile(rf"(?<!\w){re.escape(needle)}(?!\w)", re.IGNORECASE | re.UNICODE)
    return rx.search(haystack) is not None


def reload(aliases_dir: Path = settings.ALIASES_DIR) -> None:
    global _PATTERN_STRONG, _PATTERN_ALL
    _OWNERS.clear()
    _NESTED.clear()
    _TICKERS.clear()
    strong: set[str] = set()
    alla: set[str] = set()
    for p in sorted(Path(aliases_dir).glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        ticker = (data.get("ticker") or p.stem).upper()
        _TICKERS.add(ticker)
        seen: set[str] = set()
        for field, weight in [(f, 1.0) for f in _STRONG_FIELDS] + [(f, 0.3) for f in _WEAK_FIELDS]:
            for a in data.get(field) or []:
                a = (a or "").strip()
                if not a or len(a) < 2 or a.lower() in seen:
                    continue
                seen.add(a.lower())
                _OWNERS.setdefault(a.lower(), []).append((ticker, a, weight))
                alla.add(a)
                if weight >= 1.0:
                    strong.add(a)
    _PATTERN_STRONG = _build_pattern(strong)
    _PATTERN_ALL = _build_pattern(alla)
    # Precompute nested map: for each alias A, the owners of every OTHER alias B
    # that is a word-bounded substring of A. Cheap `in` prefilter before the
    # boundary check keeps this ~O(n^2) load-time pass fast.
    al = sorted(alla, key=len)  # short -> long
    for i, b in enumerate(al):
        bl = b.lower()
        b_owners = _OWNERS.get(bl, ())
        for a in al[i + 1:]:
            if len(a) <= len(b):
                continue
            if bl in a.lower() and _bounded(b, a):
                _NESTED.setdefault(a.lower(), []).extend(b_owners)


def loaded_tickers() -> list[str]:
    return sorted(_TICKERS)


def match_text(text: str, *, include_weak: bool = False) -> list[AliasHit]:
    if not text:
        return []
    pattern = _PATTERN_ALL if include_weak else _PATTERN_STRONG
    if pattern is None:
        return []
    found: dict[str, AliasHit] = {}
    for m in pattern.finditer(text):
        key = m.group().lower()
        # owners of the matched alias + owners of any alias nested inside it
        for ticker, alias, weight in (*_OWNERS.get(key, ()), *_NESTED.get(key, ())):
            if not include_weak and weight < 1.0:
                continue
            if ticker not in found:
                found[ticker] = AliasHit(ticker, alias, "", weight)
    return list(found.values())


def match_article(
    article: dict,
    *,
    fields: tuple[str, ...] = ("title", "description", "sapo", "body"),
    include_weak: bool = False,
) -> list[AliasHit]:
    """Return ≤1 hit per ticker. Location = first field where the alias appeared."""
    final: dict[str, AliasHit] = {}
    for field in fields:
        text = article.get(field) or ""
        if not text:
            continue
        for hit in match_text(text, include_weak=include_weak):
            if hit.ticker in final:
                continue
            final[hit.ticker] = AliasHit(hit.ticker, hit.alias, field, hit.weight)
    return list(final.values())


# auto-load on import
reload()
```

- [ ] **Step 2: Run the equivalence test — must PASS (zero divergences)**

Run: `python -m tests.test_rematch`
Expected: `matcher_equivalence OK` with **zero** divergences. The `_NESTED`
recovery is what makes this hold even on the fixture's cross-ticker
nested-substring lines (e.g. `Hòa Phát`⊂`Nông nghiệp Hòa Phát`,
`Masan`⊂`Hàng tiêu dùng Masan`, `FPT`⊂`FPT Shop`) — the longer alias matches and
the nested map re-emits the parent ticker, reproducing the old per-alias result.
If it prints any DIVERGENCE line, that is a real bug in the new matcher (or the
`_NESTED` build) — fix it before proceeding; do not relax the gate.

- [ ] **Step 3: Run the existing smoke suite — must still PASS**

Run: `python -m tests.test_smoke`
Expected: `alias_matcher OK` among `ALL OK` (proves DBC/Nasaco/negative still work and the API didn't break).

- [ ] **Step 4: Commit**

```bash
git add core/alias_matcher.py
git commit -m "perf(matcher): single-pass longest-first alternation (behaviour-preserving)"
```

---

## Chunk 2: Chunked rematch + status output

Stops the OOM by never holding the whole corpus in RAM, using a snapshot-ids
list then paginated full-row fetches. Adds a `--status-json` output so the
detached wrapper (Chunk 3) can report counts.

### Task 2.1: `storage.fetch_articles_by_ids` helper

**Files:**
- Modify: `core/storage.py` (add helper near `iter_articles`)
- Test: `tests/test_rematch.py::test_fetch_by_ids`

- [ ] **Step 1: Write the failing test (add to `tests/test_rematch.py`, register in `main()`)**

```python
def test_fetch_by_ids() -> None:
    from core import storage
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "f.db"
        storage.init_db(db)
        conn = storage.connect(db)
        for i in range(5):
            storage.insert_article(conn, {
                "article_id": f"d::{i}", "url_canonical": f"u{i}", "url_original": f"u{i}",
                "domain": "d", "title": f"t{i}", "backend": "google_rss",
                "group_key": "kw", "sub_query_ix": 0})
        ids = [f"d::{i}" for i in range(5)]
        rows = storage.fetch_articles_by_ids(conn, ids)
        assert {r["article_id"] for r in rows} == set(ids), rows
        # sub-chunking: force tiny chunk size, still returns all
        rows2 = storage.fetch_articles_by_ids(conn, ids, chunk=2)
        assert {r["article_id"] for r in rows2} == set(ids)
        assert storage.fetch_articles_by_ids(conn, []) == []
        conn.close()
    print("  fetch_by_ids OK")
```

- [ ] **Step 2: Run — expect FAIL** (`AttributeError: module 'core.storage' has no attribute 'fetch_articles_by_ids'`)

Run: `python -m tests.test_rematch`

- [ ] **Step 3: Implement the helper in `core/storage.py`**

```python
def fetch_articles_by_ids(
    conn: sqlite3.Connection, ids: list[str], *, chunk: int = 500
) -> list[sqlite3.Row]:
    """Fetch full rows for the given article_ids. Sub-chunks the IN(...) list to
    stay under SQLite's bound-variable cap. Order is not guaranteed."""
    out: list[sqlite3.Row] = []
    for i in range(0, len(ids), chunk):
        part = ids[i:i + chunk]
        ph = ",".join("?" * len(part))
        out.extend(conn.execute(
            f"SELECT * FROM articles WHERE article_id IN ({ph})", part
        ).fetchall())
    return out
```

- [ ] **Step 4: Run — expect PASS**

Run: `python -m tests.test_rematch`
Expected: `fetch_by_ids OK`

- [ ] **Step 5: Commit**

```bash
git add core/storage.py tests/test_rematch.py
git commit -m "feat(storage): fetch_articles_by_ids with IN-cap sub-chunking"
```

### Task 2.2: Chunked rematch loop + `--status-json` in `pipeline/match.py`

**Files:**
- Modify: `pipeline/match.py` (`run()` signature + loop; `main()` arg)
- Test: `tests/test_rematch.py::test_chunked_rematch`

- [ ] **Step 1: Write the failing test (multi-batch via tiny BATCH_SIZE) — add + register in `main()`**

```python
def test_chunked_rematch() -> None:
    from core import storage, alias_matcher
    from pipeline import match
    from config import settings
    alias_matcher.reload()
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "r.db"
        _orig_pt, _orig_bs = settings.PER_TICKER_DIR, match.BATCH_SIZE
        try:
            settings.PER_TICKER_DIR = Path(td) / "pt"; settings.PER_TICKER_DIR.mkdir()
            match.BATCH_SIZE = 2  # force multiple batches over 3 articles
            storage.init_db(db)
            conn = storage.connect(db)
            storage.insert_article(conn, {"article_id":"a::1","url_canonical":"u1","url_original":"u1",
                "domain":"d","title":"Xử phạt Dabaco 300 triệu vì vi phạm môi trường","title_hash":"h1",
                "backend":"google_rss","group_key":"alias","sub_query_ix":0,"body_status":"fetched"})
            storage.insert_article(conn, {"article_id":"a::2","url_canonical":"u2","url_original":"u2",
                "domain":"d","title":"Cổ đông Dabaco nhận cổ tức","title_hash":"h2",
                "backend":"google_rss","group_key":"alias","sub_query_ix":0,"body_status":"fetched"})
            storage.insert_article(conn, {"article_id":"a::3","url_canonical":"u3","url_original":"u3",
                "domain":"d","title":"Xử phạt công ty xả thải ra môi trường","title_hash":"h3",
                "backend":"google_rss","group_key":"alias","sub_query_ix":0,"body_status":"fetched"})
            status = Path(td) / "status.json"
            counts = match.run(db_path=db, rematch_all=True, status_json=str(status))
            assert counts["matched"] >= 1, counts
            # a::1 (Dabaco + ESG) kept; a::2 noise dropped
            rows = {r["article_id"]: r for r in conn.execute("SELECT * FROM articles")}
            assert rows["a::1"]["esg_status"] == "esg"
            assert rows["a::2"]["esg_status"] == "noise"
            doc = json.loads((settings.PER_TICKER_DIR / "DBC.json").read_text(encoding="utf-8"))
            assert "a::1" in {a["article_id"] for a in doc["articles"]}
            # status file written with counts
            st = json.loads(status.read_text(encoding="utf-8"))
            assert st["matched"] == counts["matched"]
            conn.close()
        finally:
            settings.PER_TICKER_DIR, match.BATCH_SIZE = _orig_pt, _orig_bs
    print("  chunked_rematch OK")
```

- [ ] **Step 2: Run — expect FAIL** (`AttributeError: module 'pipeline.match' has no attribute 'BATCH_SIZE'` / `run() got unexpected keyword 'status_json'`)

Run: `python -m tests.test_rematch`

- [ ] **Step 3: Edit `pipeline/match.py`**

Add near the top (after `log = logging.getLogger("match")`):

```python
BATCH_SIZE = 2000  # rows held in RAM per pagination batch (tunable)
```

Replace the `run(...)` signature and the snapshot+loop. New signature:

```python
def run(
    since: str | None = None,
    limit: int | None = None,
    *,
    rematch_all: bool = False,
    db_path: str | None = None,
    status_json: str | None = None,
) -> dict[str, int]:
```

Replace the body from the `pending = list(...)` line through the end of the
per-article `for` loop with a snapshot + paginated loop. The reset/wipe prelude
(lines ~100-108) stays as-is **before** the snapshot. The per-article body is
unchanged — only how rows are sourced changes:

```python
    # snapshot the work list FIRST (autocommit + we mutate match_status below,
    # so we must not iterate a live SELECT on that column)
    sql = "SELECT article_id FROM articles WHERE match_status='pending'"
    args: list = []
    if since:
        sql += " AND published_at >= ?"; args.append(since)
    sql += " ORDER BY published_at DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    ids = [r["article_id"] for r in conn.execute(sql, args)]
    log.info("scanning %d pending articles in batches of %d", len(ids), BATCH_SIZE)

    per_ticker: dict[str, dict] = {}
    counts = {"matched": 0, "unmatched": 0, "deferred": 0}

    for i in range(0, len(ids), BATCH_SIZE):
        batch = storage.fetch_articles_by_ids(conn, ids[i:i + BATCH_SIZE])
        for art in batch:
            _process_article(conn, art, per_ticker, counts)  # extracted; see below
```

Extract the existing per-article body (everything currently inside
`for art in pending:`) verbatim into a module-level helper
`def _process_article(conn, art, per_ticker, counts): ...` so the loop above
calls it. Keep `_load_existing`, `_save`, `_snippet`, `_load_cached_hits`
unchanged. After the per_ticker write-out, add the status file:

```python
    if status_json:
        Path(status_json).write_text(json.dumps(counts), encoding="utf-8")
    conn.close()
    return counts
```

Update `main()` to pass it through:

```python
    ap.add_argument("--status-json", help="write final {matched,unmatched,deferred} counts to this path")
    args = ap.parse_args()
    run(since=args.since, limit=args.limit, rematch_all=args.rematch_all,
        status_json=args.status_json)
```

- [ ] **Step 4: Run — expect PASS**

Run: `python -m tests.test_rematch`
Expected: `chunked_rematch OK`

- [ ] **Step 5: Run the smoke suite — `test_match_esg_integration` must still PASS** (proves the extracted `_process_article` preserved behaviour)

Run: `python -m tests.test_smoke`
Expected: `match_esg_integration OK` within `ALL OK`

- [ ] **Step 6: Commit**

```bash
git add pipeline/match.py tests/test_rematch.py
git commit -m "perf(match): chunked snapshot-then-paginate rematch + --status-json"
```

---

## Chunk 3: Detached deploy (deferred live verification)

Makes the rematch fire-and-return on the VM. **Cannot be unit-tested locally**
(needs systemd + the VM); the gate here is review + a `bash -n` syntax check,
with live verification deferred to the first real run after the VM is back up.

### Task 3.1: `deploy/rematch_managed.sh` wrapper

**Files:**
- Create: `deploy/rematch_managed.sh`

- [ ] **Step 1: Write the wrapper**

```bash
#!/usr/bin/env bash
# Managed rematch — runs detached via systemd-run (as root). Owns the whole
# lifecycle so the CI deploy can fire-and-return. Writes a status file to GCS so
# progress is visible without SSH.
set +e

INSTALL_DIR=/opt/esg-collector
APP_DIR=$INSTALL_DIR/esg-collector
VENV=$INSTALL_DIR/.venv/bin/python
SVC_USER=esg
STATUS_LOCAL=/tmp/rematch_status.json
STATUS_GCS=gs://esg-scan-data/_setup/rematch_status.json
WORKERS="esg-collector-google.service esg-collector-baomoi.service esg-collector-brave.service esg-collector-body.service"

write_status() {  # $1=state  $2=extra-json (optional)
  ts=$(date -Iseconds)
  echo "{\"state\":\"$1\",\"at\":\"$ts\"${2:+,$2}}" > "$STATUS_LOCAL"
  sudo -u "$SVC_USER" gsutil cp "$STATUS_LOCAL" "$STATUS_GCS" 2>/dev/null
}

restart_workers() { systemctl start $WORKERS; }
trap 'restart_workers' EXIT  # whatever happens, workers come back

write_status running
systemctl stop $WORKERS

cd "$APP_DIR" || { write_status failed '"error":"cd failed"'; exit 1; }

sudo -u "$SVC_USER" "$VENV" -m pipeline.match --rematch-all --status-json "$STATUS_LOCAL.counts"
rc=$?
if [ $rc -ne 0 ]; then
  write_status failed "\"error\":\"match rc=$rc\""
  exit 1   # trap restarts workers
fi

sudo -u "$SVC_USER" "$VENV" -m pipeline.export --ndjson --upload

counts=$(cat "$STATUS_LOCAL.counts" 2>/dev/null || echo '{}')
restart_workers
trap - EXIT
write_status done "\"counts\":$counts"
```

- [ ] **Step 2: Syntax-check (local, no VM needed)**

Run: `bash -n deploy/rematch_managed.sh`
Expected: no output, exit 0.

- [ ] **Step 3: Make it executable + commit**

```bash
git update-index --chmod=+x deploy/rematch_managed.sh 2>/dev/null || chmod +x deploy/rematch_managed.sh
git add deploy/rematch_managed.sh
git commit -m "feat(deploy): managed detached rematch wrapper"
```

### Task 3.2: Workflow REMATCH control-flow split

**Files:**
- Modify: `.github/workflows/deploy-esg-collector.yml` (repo root, not under esg-collector/)

- [ ] **Step 1: Gate the existing stop (step 1) and restart (step 7) on `REMATCH=0`, and add the detached launch**

Three literal edits inside the remote heredoc (`trap restart_workers EXIT HUP TERM` stays untouched as the REMATCH=0 safety net).

**Edit 1 — step 1, gate the stop.** Wrap the existing stop block:

```bash
          echo "=== 1. Stop workers ==="
          if [ "$REMATCH" != "1" ]; then
            sudo systemctl stop \
              esg-collector-google.service \
              esg-collector-baomoi.service \
              esg-collector-brave.service \
              esg-collector-body.service
          else
            echo "  (REMATCH=1: managed rematch unit owns worker stop/start; skipping)"
          fi
```

**Edit 2 — step 6b, replace inline rematch with detached launch.** Replace:

```bash
          if [ "$REMATCH" = "1" ]; then
            echo "=== 6b. Rematch all (user-requested) ==="
            sudo -u $SVC_USER $VENV -m pipeline.match --rematch-all
          fi
```

with:

```bash
          if [ "$REMATCH" = "1" ]; then
            echo "=== 6b. Launch DETACHED managed rematch (fire-and-return) ==="
            sudo systemd-run --no-block --collect --unit=esg-rematch \
              "$APP_DIR/deploy/rematch_managed.sh"
            echo "  launched esg-rematch unit; it owns worker lifecycle, deploy returns now"
          fi
```

**Edit 3 — step 7, gate the restart + active-check.** Wrap the existing
`systemctl start … ` and the `for s in google baomoi brave body; do … active …`
block in `if [ "$REMATCH" != "1" ]; then … else echo "  (REMATCH=1: workers
managed by esg-rematch unit)"; fi`, preserving the inner heredoc indentation.

- [ ] **Step 2: Lint the YAML / shell**

Run: `python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/deploy-esg-collector.yml')); print('yaml OK')"`
(from repo root `d:/Claude/ESG scan/esg-pipeline/`)
Expected: `yaml OK`. (PyYAML may not be installed; if not, skip — the GitHub
Actions parser is the real check on push.)

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/deploy-esg-collector.yml
git commit -m "ci: launch rematch detached via systemd-run; split worker lifecycle on REMATCH"
```

### Task 3.3: Deferred live verification checklist (do AFTER the VM is back up)

Not a code step — a runbook to execute once the user resets the VM and the
branch is merged/deployed:

- [ ] Trigger deploy with `run_rematch_all` ticked; confirm the deploy run goes
      **green in ~1–2 min** (fire-and-return, like backfill #4).
- [ ] Poll `gs://esg-scan-data/_setup/rematch_status.json`: `running` → `done`
      with counts (or `failed` + error).
- [ ] After `done`, pull `per_ticker/HCM.json` (expect ~tens, not 2188) and
      confirm `per_ticker/FRT.json` now exists with data.
- [ ] Confirm the 4 workers are `active` (serial console or a follow-up status).

---

## Done criteria

- `python -m tests.test_rematch` and `python -m tests.test_smoke` both print `ALL OK`.
- `bash -n deploy/rematch_managed.sh` clean; workflow YAML parses.
- Matcher equivalence: **zero** `(ticker, location)` divergences on the fixture.
- Live: detached rematch completes, status file reaches `done`, HCM/FRT corrected, workers active. (Deferred to post-VM-reset.)
