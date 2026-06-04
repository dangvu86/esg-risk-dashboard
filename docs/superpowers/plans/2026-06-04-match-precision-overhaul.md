# Match Precision Overhaul Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut false-positive company↔article attributions in esg-collector via four small, independently-testable fixes, without losing real ESG events.

**Architecture:** Fixes 1+A drop collision surface forms (bare tickers + generic fragments) via one stoplist enforced at alias load. Fix C cleans the stored article body (Jina content-selector for new fetches + a one-shot idempotent strip of the related-news link/image block for old bodies). Fix B drops non-title attributions in articles that name ≥3 companies (roundup/listicle guard) inside the matcher. All four take effect on already-stored data via the existing (separately-pending) chunked rematch.

**Tech Stack:** Python 3.9+ (stdlib `re`, `json`, `sqlite3`), the esg-collector package layout (`config/`, `core/`, `body_fetcher/`, `workers/`, `pipeline/`, `tests/`). **No pytest** — tests are plain modules with `test_*()` functions and a `main()`, run via `python -m tests.test_<name>` from the `esg-collector/` directory. DB is SQLite in **autocommit** mode (`connect()` sets `isolation_level=None`), so no explicit `commit()` is needed.

**Spec:** [`docs/superpowers/specs/2026-06-04-match-precision-overhaul-design.md`](../specs/2026-06-04-match-precision-overhaul-design.md)

**Working directory for all commands:** `esg-pipeline/esg-collector/`
**Branch:** `feature/esg-enrich-pipeline`

---

## File Structure

**Fix 1 + A — alias stoplist**
- Create `config/ambiguous_aliases.json` — the stoplist (collision tickers + generic fragments), audited.
- Modify `config/settings.py` — add `AMBIGUOUS_ALIASES_PATH`.
- Modify `core/alias_matcher.py` — load the stoplist + skip stoplisted surfaces in `reload()`.
- Create `tests/test_stoplist.py`.

**Fix C — clean body**
- Create `body_fetcher/body_clean.py` — pure `strip_related_blocks(body)`.
- Modify `body_fetcher/jina.py` — send `X-Target-Selector` (reuse `fallback.ARTICLE_SELECTORS`) + empty-response retry without the selector.
- Modify `workers/body_fetcher.py` — clean every fetched body before store (uniform, covers both Jina and fallback paths).
- Create `pipeline/clean_bodies.py` — one-shot, `export_state`-gated backfill that strips stored bodies.
- Create `tests/test_body_clean.py`.

**Fix B — roundup gate**
- Modify `pipeline/match.py` — `_process_article`: drop non-title hits when an article matched ≥3 companies.
- Create `tests/test_match_roundup.py`.

**Rollout (Chunk 4)** — audit the stoplist, run the body-clean backfill, deploy, trigger rematch, verify.

---

## Chunk 1: Fix 1 + A — alias stoplist

### Task 1.1: Stoplist data file + settings constant

**Files:**
- Create: `config/ambiguous_aliases.json`
- Modify: `config/settings.py:20` (after `COMPANIES_CSV`)

- [ ] **Step 1: Create the stoplist file**

`config/ambiguous_aliases.json` (initial audited list — Chunk 4 Task 4.1 re-audits before deploy):

```json
[
  "GAS", "KDC", "VND", "PAN", "BID", "BMP", "POW", "SIP", "HCM",
  "DELTA", "APATIT", "BH", "PHÁT TRIỂN ĐÔ THỊ"
]
```

- [ ] **Step 2: Add the settings constant**

In `config/settings.py`, immediately after line 20 (`COMPANIES_CSV = ROOT / "config" / "companies.csv"`):

```python
AMBIGUOUS_ALIASES_PATH = ROOT / "config" / "ambiguous_aliases.json"
```

- [ ] **Step 3: Commit**

```bash
git add config/ambiguous_aliases.json config/settings.py
git commit -m "feat(match): add ambiguous-alias stoplist data + settings path"
```

### Task 1.2: Enforce the stoplist at alias load

**Files:**
- Modify: `core/alias_matcher.py` (imports ~15-22; globals ~36-40; `reload()` 56-91)
- Test: `tests/test_stoplist.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_stoplist.py`:

```python
"""Alias stoplist tests (Fix 1 + A). Run: python -m tests.test_stoplist"""
from __future__ import annotations
import json, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import settings  # noqa: E402


def _write_aliases(d: Path) -> None:
    (d / "KDC.json").write_text(json.dumps(
        {"ticker": "KDC", "names": ["KDC", "Kido"],
         "subsidiaries": [], "projects": [], "locations": []},
        ensure_ascii=False), encoding="utf-8")
    (d / "ACV.json").write_text(json.dumps(
        {"ticker": "ACV", "names": ["ACV", "Tổng công ty Cảng hàng không Việt Nam"],
         "subsidiaries": [], "projects": [], "locations": []},
        ensure_ascii=False), encoding="utf-8")


def _reload_with(ad: Path, stoplist_path: Path):
    from core import alias_matcher
    settings.AMBIGUOUS_ALIASES_PATH = stoplist_path
    alias_matcher.reload(ad)
    return alias_matcher


def test_stoplisted_surface_dropped():
    from core import alias_matcher
    _orig = settings.AMBIGUOUS_ALIASES_PATH
    with tempfile.TemporaryDirectory() as td:
        ad = Path(td) / "aliases"; ad.mkdir(); _write_aliases(ad)
        sp = Path(td) / "stop.json"; sp.write_text(json.dumps(["KDC"]), encoding="utf-8")
        try:
            am = _reload_with(ad, sp)
            assert not any(h.ticker == "KDC" for h in am.match_text("Sai phạm KDC Bình Đa"))
            assert any(h.ticker == "KDC" for h in am.match_text("Tập đoàn Kido bị phạt"))
        finally:
            settings.AMBIGUOUS_ALIASES_PATH = _orig; alias_matcher.reload()
    print("  stoplisted_surface_dropped OK")


def test_nonstoplisted_ticker_kept():
    from core import alias_matcher
    _orig = settings.AMBIGUOUS_ALIASES_PATH
    with tempfile.TemporaryDirectory() as td:
        ad = Path(td) / "aliases"; ad.mkdir(); _write_aliases(ad)
        sp = Path(td) / "stop.json"; sp.write_text(json.dumps(["KDC"]), encoding="utf-8")
        try:
            am = _reload_with(ad, sp)
            assert any(h.ticker == "ACV" for h in am.match_text("ACV bị phạt 270 triệu"))
        finally:
            settings.AMBIGUOUS_ALIASES_PATH = _orig; alias_matcher.reload()
    print("  nonstoplisted_ticker_kept OK")


def test_missing_stoplist_ok():
    from core import alias_matcher
    _orig = settings.AMBIGUOUS_ALIASES_PATH
    with tempfile.TemporaryDirectory() as td:
        ad = Path(td) / "aliases"; ad.mkdir(); _write_aliases(ad)
        try:
            am = _reload_with(ad, Path(td) / "nope.json")  # missing file
            assert any(h.ticker == "KDC" for h in am.match_text("Sai phạm KDC Bình Đa"))
        finally:
            settings.AMBIGUOUS_ALIASES_PATH = _orig; alias_matcher.reload()
    print("  missing_stoplist_ok OK")


def main():
    if sys.platform == "win32":
        try: sys.stdout.reconfigure(encoding="utf-8")
        except Exception: pass
    print("running stoplist tests…")
    test_stoplisted_surface_dropped()
    test_nonstoplisted_ticker_kept()
    test_missing_stoplist_ok()
    print("ALL OK")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m tests.test_stoplist`
Expected: FAIL — `test_stoplisted_surface_dropped` asserts no KDC hit, but without the stoplist guard `match_text("Sai phạm KDC Bình Đa")` still returns KDC (bare "KDC" matches). (Or an `AttributeError` if `settings.AMBIGUOUS_ALIASES_PATH` isn't set yet — do Task 1.1 first.)

- [ ] **Step 3: Implement the stoplist loader + guard**

In `core/alias_matcher.py`:

(a) After the existing imports (around line 22, after `from config import settings`):

```python
import logging

log = logging.getLogger("alias_matcher")
```

(b) With the other module globals (after `_TICKERS: set[str] = set()`, ~line 38):

```python
_STOPLIST: set[str] = set()  # upper-cased surface forms never matched (Fix 1+A)


def _load_stoplist() -> set[str]:
    try:
        data = json.loads(settings.AMBIGUOUS_ALIASES_PATH.read_text(encoding="utf-8"))
        return {str(s).strip().upper() for s in data if str(s).strip()}
    except FileNotFoundError:
        return set()
    except (OSError, json.JSONDecodeError, TypeError) as e:
        log.warning("ambiguous_aliases stoplist unreadable (%s) — ignoring", e)
        return set()
```

(c) In `reload()`, add `_STOPLIST` to the `global` line and load it near the top:

```python
def reload(aliases_dir: Path = settings.ALIASES_DIR) -> None:
    global _PATTERN_STRONG, _PATTERN_ALL, _STOPLIST
    _STOPLIST = _load_stoplist()
    _OWNERS.clear()
    ...
```

(d) In the per-field loop, skip stoplisted surfaces — insert between the
`len(a) < 2` guard and `seen.add` (after line 75, before line 76):

```python
                if not a or len(a) < 2 or a.lower() in seen:
                    continue
                if a.upper() in _STOPLIST:      # Fix 1+A: drop collision surface
                    continue
                seen.add(a.lower())
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m tests.test_stoplist`
Expected: `ALL OK` (all three tests print OK).

- [ ] **Step 5: Verify the real corpus still loads (no regression)**

Run: `python -m tests.test_rematch`
Expected: `ALL OK` — `test_matcher_equivalence` still passes (the live aliases load; with the real `ambiguous_aliases.json` present, stoplisted surfaces are simply absent — the legacy reference matcher in that test does not consult the stoplist, so confirm equivalence still holds; if it diverges only on stoplisted surfaces, that is expected — see note). 

> Note: `test_matcher_equivalence` compares against a legacy matcher that has no stoplist. If the live `ambiguous_aliases.json` causes divergences, they will be exactly the stoplisted surfaces. If the test fails for that reason, update the legacy reference in `tests/test_rematch.py` to also skip `_STOPLIST` (load it the same way) so the equivalence check stays meaningful. Make that change in this step if needed and re-run.

- [ ] **Step 6: Commit**

```bash
git add core/alias_matcher.py tests/test_stoplist.py tests/test_rematch.py
git commit -m "feat(match): drop ambiguous bare-ticker/fragment aliases at load (Fix 1+A)"
```

---

## Chunk 2: Fix C — clean article body

### Task 2.1: Pure body-cleaning function

**Files:**
- Create: `body_fetcher/body_clean.py`
- Test: `tests/test_body_clean.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_body_clean.py`:

```python
"""Body-clean tests (Fix C). Run: python -m tests.test_body_clean"""
from __future__ import annotations
import sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_strips_related_link_block():
    from body_fetcher.body_clean import strip_related_blocks
    body = (
        "Ô nhiễm nghiêm trọng tại kênh hào thành cổ Vinh. "
        "Nước đen kịt, mùi hôi nồng nặc.\n"
        "* [![Image 52: Đất Xanh vươn tầm quốc tế với BLUEMARQ GROUP](https://x/y.htm)\n"
        "* [Vietnam Airlines và Nghệ An ký hợp tác](https://a/b.htm)\n"
        "- [Vincom khai trương](http://c/d)"
    )
    out = strip_related_blocks(body)
    assert "kênh hào thành cổ Vinh" in out
    assert "BLUEMARQ" not in out
    assert "Vietnam Airlines" not in out
    assert "Vincom" not in out
    print("  strips_related_link_block OK")


def test_keeps_prose_company_mention():
    from body_fetcher.body_clean import strip_related_blocks
    body = "Theo kết luận thanh tra, Tập đoàn Kido bị xử phạt do vi phạm thuế."
    assert strip_related_blocks(body) == body
    print("  keeps_prose_company_mention OK")


def test_empty():
    from body_fetcher.body_clean import strip_related_blocks
    assert strip_related_blocks("") == ""
    assert strip_related_blocks(None) is None
    print("  empty OK")


def main():
    if sys.platform == "win32":
        try: sys.stdout.reconfigure(encoding="utf-8")
        except Exception: pass
    print("running body_clean tests…")
    test_strips_related_link_block()
    test_keeps_prose_company_mention()
    test_empty()
    print("ALL OK")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m tests.test_body_clean`
Expected: FAIL — `ModuleNotFoundError: body_fetcher.body_clean`.

- [ ] **Step 3: Implement the cleaner**

Create `body_fetcher/body_clean.py`:

```python
"""Strip the related-news / recommendation link-list block Jina captures from a
full page, line by line, over the whole body (related blocks also appear
mid-body, so no positional cut). Pure + side-effect free for easy testing."""
from __future__ import annotations

import re

# A markdown list item that is an image/link entry = nav / related-news widget.
_LINK_LINE = re.compile(r"^[\*\-]\s*\[?!?\[?Image", re.IGNORECASE)
_LINK_LIST = re.compile(r"^[\*\-]\s+\[.*\]\(https?://", re.IGNORECASE)


def strip_related_blocks(body: str | None) -> str | None:
    if not body:
        return body
    kept = [
        line for line in body.splitlines()
        if not (_LINK_LINE.match(line.lstrip()) or _LINK_LIST.match(line.lstrip()))
    ]
    return "\n".join(kept).strip()
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m tests.test_body_clean`
Expected: `ALL OK`.

- [ ] **Step 5: Commit**

```bash
git add body_fetcher/body_clean.py tests/test_body_clean.py
git commit -m "feat(body): pure strip_related_blocks() for Jina sidebar noise (Fix C)"
```

### Task 2.2: Jina content-selector + empty-response retry

**Files:**
- Modify: `body_fetcher/jina.py` (headers 62-67; `fetch` 55-80)
- Test: add `test_jina_headers.py` (no network; fake requester)

- [ ] **Step 1: Write the failing test**

Create `tests/test_jina_headers.py`:

```python
"""Jina selector + retry tests (Fix C). Run: python -m tests.test_jina_headers"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class _Resp:
    def __init__(self, text, status=200):
        self.text, self.status_code = text, status


def test_selector_header_present_then_retry_without():
    from body_fetcher import jina
    calls = []

    def fake_get(url, headers=None, timeout=30):
        calls.append(headers or {})
        # 1st call (with selector) returns empty -> triggers retry;
        # 2nd call (no selector) returns content.
        return _Resp("" if len(calls) == 1 else "real article body text " * 20)

    orig = jina.requests.get
    jina.requests.get = fake_get
    try:
        body, status = jina.fetch("http://example.com/a")
        assert status == "fetched" and body
        assert len(calls) == 2, calls
        assert "X-Target-Selector" in calls[0]
        assert "X-Target-Selector" not in calls[1]
    finally:
        jina.requests.get = orig
    print("  selector_then_retry OK")


def main():
    if sys.platform == "win32":
        try: sys.stdout.reconfigure(encoding="utf-8")
        except Exception: pass
    print("running jina header tests…")
    test_selector_header_present_then_retry_without()
    print("ALL OK")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m tests.test_jina_headers`
Expected: FAIL — current `fetch` sends no `X-Target-Selector` and does not retry (returns `(None,"failed")` on the empty first response).

- [ ] **Step 3: Implement selector + retry**

In `body_fetcher/jina.py`:

(a) Add the import near the top (after `from config import settings`):

```python
from body_fetcher.fallback import ARTICLE_SELECTORS

_TARGET_SELECTOR = ",".join(ARTICLE_SELECTORS)
_MIN_BODY = 200  # below this, treat selector result as a miss and retry full-page
```

(b) Replace `fetch` (lines 55-80) with a version that adds the selector and retries once without it:

```python
def fetch(url: str, timeout: int = 30) -> tuple[str | None, str]:
    """Return (body_markdown, status). status ∈ {fetched, failed, ratelimited}.

    First tries with X-Target-Selector so Jina returns only the article body
    (drops nav/sidebar/related). If that yields too little (uncovered site),
    retry once without the selector (full page)."""
    if not url:
        return None, "failed"

    def _get(with_selector: bool):
        headers = {
            "X-Return-Format": "markdown",
            "Accept": "text/markdown, text/plain",
        }
        if settings.JINA_API_KEY:
            headers["Authorization"] = f"Bearer {settings.JINA_API_KEY}"
        if with_selector:
            headers["X-Target-Selector"] = _TARGET_SELECTOR
        _pace()
        try:
            r = requests.get(ENDPOINT + url, headers=headers, timeout=timeout)
        except requests.RequestException:
            return None, "failed"
        if r.status_code == 429:
            return None, "ratelimited"
        if r.status_code >= 400:
            return None, "failed"
        return (r.text or "").strip(), "fetched"

    body, status = _get(with_selector=True)
    if status == "fetched" and (not body or len(body) < _MIN_BODY):
        body, status = _get(with_selector=False)  # selector missed -> full page
    if status == "fetched" and not body:
        return None, "failed"
    return body, status
```

> Keeps the existing `_pace()` token-bucket (one slot per HTTP call, so the retry is rate-limited too) and the `(None,"failed")`-on-empty contract.

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m tests.test_jina_headers`
Expected: `ALL OK`.

- [ ] **Step 5: Commit**

```bash
git add body_fetcher/jina.py tests/test_jina_headers.py
git commit -m "feat(body): Jina X-Target-Selector + full-page retry on miss (Fix C)"
```

### Task 2.3: Clean every fetched body before store

**Files:**
- Modify: `workers/body_fetcher.py` (`_fetch_one` 56-63)
- Test: `tests/test_fetch_one_clean.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_fetch_one_clean.py`:

```python
"""_fetch_one cleans body (Fix C). Run: python -m tests.test_fetch_one_clean"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_fetch_one_strips_related():
    from workers import body_fetcher
    from body_fetcher import jina
    noisy = ("Bài viết thật về ô nhiễm.\n"
             "* [![Image 1: Tin khác](https://x/y)")
    orig = jina.fetch
    jina.fetch = lambda url: (noisy, "fetched")
    try:
        body, status = body_fetcher._fetch_one("http://e/a")
        assert status == "fetched"
        assert "ô nhiễm" in body and "Image 1" not in body
    finally:
        jina.fetch = orig
    print("  fetch_one_strips_related OK")


def main():
    if sys.platform == "win32":
        try: sys.stdout.reconfigure(encoding="utf-8")
        except Exception: pass
    print("running fetch_one clean test…")
    test_fetch_one_strips_related()
    print("ALL OK")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m tests.test_fetch_one_clean`
Expected: FAIL — `_fetch_one` returns the noisy body unchanged (`Image 1` still present).

- [ ] **Step 3: Implement cleaning in `_fetch_one`**

In `workers/body_fetcher.py`: add the import near the top (with the other `body_fetcher` imports, line 25):

```python
from body_fetcher import jina, fallback
from body_fetcher.body_clean import strip_related_blocks
```

Replace `_fetch_one` (lines 56-63):

```python
def _fetch_one(url: str) -> tuple[str | None, str]:
    body, status = jina.fetch(url)
    if status != "fetched":
        if status == "ratelimited":
            time.sleep(2)
        body, status = fallback.fetch(url)
    if status == "fetched" and body:
        body = strip_related_blocks(body)
    return body, status
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m tests.test_fetch_one_clean`
Expected: `ALL OK`.

- [ ] **Step 5: Commit**

```bash
git add workers/body_fetcher.py tests/test_fetch_one_clean.py
git commit -m "feat(body): clean fetched body before store (Fix C)"
```

### Task 2.4: One-shot gated backfill for stored bodies

**Files:**
- Create: `pipeline/clean_bodies.py`
- Test: `tests/test_clean_bodies.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_clean_bodies.py`:

```python
"""clean_bodies backfill (Fix C). Run: python -m tests.test_clean_bodies"""
from __future__ import annotations
import sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_backfill_cleans_once_and_is_idempotent():
    from core import storage
    from pipeline import clean_bodies
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "c.db"
        storage.init_db(db); conn = storage.connect(db)
        noisy = "Prose về ô nhiễm.\n* [![Image 1: x](https://a/b)"
        storage.insert_article(conn, {"article_id": "d::1", "url_canonical": "u",
            "url_original": "u", "domain": "d", "title": "t", "backend": "google_rss",
            "group_key": "kw", "sub_query_ix": 0})
        storage.mark_body(conn, "d::1", "fetched", noisy)
        # a skipped row must NOT be touched
        storage.insert_article(conn, {"article_id": "d::2", "url_canonical": "u2",
            "url_original": "u2", "domain": "d", "title": "t2", "backend": "google_rss",
            "group_key": "kw", "sub_query_ix": 0})
        storage.mark_body(conn, "d::2", "skipped")
        conn.close()

        r1 = clean_bodies.run(db_path=db)
        assert r1["skipped"] is False and r1["cleaned"] == 1, r1

        conn = storage.connect(db)
        body = conn.execute("SELECT body FROM articles WHERE article_id='d::1'").fetchone()["body"]
        assert "Image 1" not in body and "ô nhiễm" in body
        conn.close()

        # idempotent: second run is gated off
        r2 = clean_bodies.run(db_path=db)
        assert r2["skipped"] is True, r2
    print("  backfill_cleans_once_and_is_idempotent OK")


def main():
    if sys.platform == "win32":
        try: sys.stdout.reconfigure(encoding="utf-8")
        except Exception: pass
    print("running clean_bodies test…")
    test_backfill_cleans_once_and_is_idempotent()
    print("ALL OK")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m tests.test_clean_bodies`
Expected: FAIL — `ModuleNotFoundError: pipeline.clean_bodies`.

- [ ] **Step 3: Implement the backfill**

Create `pipeline/clean_bodies.py`:

```python
"""One-shot: strip related-news link/image blocks from already-stored bodies.

Idempotent via an export_state flag (re-runs are no-ops unless --force). No
re-fetch — reads/rewrites `articles.body` in place. The body fetcher already
cleans NEW bodies (workers/body_fetcher); this fixes the pre-existing ones.

Run:  python -m pipeline.clean_bodies [--force]
"""
from __future__ import annotations

import argparse
import logging

from core import storage
from body_fetcher.body_clean import strip_related_blocks

log = logging.getLogger("clean_bodies")
FLAG = "bodies_cleaned_v1"


def run(db_path=None, *, force: bool = False) -> dict:
    storage.init_db(db_path)
    conn = storage.connect(db_path)
    try:
        if not force and storage.get_meta(conn, FLAG):
            return {"skipped": True, "cleaned": 0, "scanned": 0}
        cleaned = scanned = 0
        rows = list(storage.iter_articles(conn, body_status="fetched"))
        for r in rows:
            scanned += 1
            body = r["body"] or ""
            new = strip_related_blocks(body)
            if new != body:
                storage.mark_body(conn, r["article_id"], "fetched", new)
                cleaned += 1
        storage.set_meta(conn, FLAG, "done")
        return {"skipped": False, "cleaned": cleaned, "scanned": scanned}
    finally:
        conn.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s/%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="ignore the done-flag")
    args = ap.parse_args()
    result = run(force=args.force)
    log.info("clean_bodies: %s", result)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m tests.test_clean_bodies`
Expected: `ALL OK`.

- [ ] **Step 5: Commit**

```bash
git add pipeline/clean_bodies.py tests/test_clean_bodies.py
git commit -m "feat(body): one-shot gated backfill to clean stored bodies (Fix C)"
```

---

## Chunk 3: Fix B — roundup / aboutness gate

### Task 3.1: Drop non-title hits in ≥3-company articles

**Files:**
- Modify: `pipeline/match.py` (`_process_article`, after line 107 verdict, before line 108)
- Test: `tests/test_match_roundup.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_match_roundup.py`:

```python
"""Roundup gate (Fix B). Run: python -m tests.test_match_roundup"""
from __future__ import annotations
import json, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import settings  # noqa: E402


def _aliases(d: Path):
    for tk, name in [("AAA", "Alpha Corp"), ("BBB", "Beta Corp"),
                     ("CCC", "Gamma Corp")]:
        (d / f"{tk}.json").write_text(json.dumps(
            {"ticker": tk, "names": [name], "subsidiaries": [],
             "projects": [], "locations": []}, ensure_ascii=False), encoding="utf-8")


def _matched_tickers(conn):
    return {r["article_id"]: r["match_status"]
            for r in conn.execute("SELECT article_id, match_status FROM articles")}


def test_roundup_drops_nontitle_keeps_title():
    from core import storage, alias_matcher
    from pipeline import match
    _opt, _obs, _osp = settings.PER_TICKER_DIR, match.BATCH_SIZE, settings.AMBIGUOUS_ALIASES_PATH
    with tempfile.TemporaryDirectory() as td:
        ad = Path(td) / "aliases"; ad.mkdir(); _aliases(ad)
        settings.AMBIGUOUS_ALIASES_PATH = Path(td) / "none.json"  # empty stoplist
        alias_matcher.reload(ad)
        try:
            settings.PER_TICKER_DIR = Path(td) / "pt"; settings.PER_TICKER_DIR.mkdir()
            match.BATCH_SIZE = 10
            db = Path(td) / "m.db"; storage.init_db(db); conn = storage.connect(db)
            # (1) roundup: 3 companies, all in BODY, risk keyword present -> all dropped
            storage.insert_article(conn, {"article_id": "r::1", "url_canonical": "u1",
                "url_original": "u1", "domain": "d", "title": "Thanh tra phát hiện sai phạm",
                "title_hash": "h1", "backend": "google_rss", "group_key": "kw",
                "sub_query_ix": 0, "body_status": "fetched"})
            storage.mark_body(conn, "r::1", "fetched",
                "Thanh tra phát hiện sai phạm tại Alpha Corp, Beta Corp và Gamma Corp.")
            # (2) 3 companies but one in TITLE -> only the title one kept
            storage.insert_article(conn, {"article_id": "r::2", "url_canonical": "u2",
                "url_original": "u2", "domain": "d",
                "title": "Alpha Corp bị xử phạt vì vi phạm", "title_hash": "h2",
                "backend": "google_rss", "group_key": "kw", "sub_query_ix": 0,
                "body_status": "fetched"})
            storage.mark_body(conn, "r::2", "fetched",
                "Alpha Corp bị xử phạt. Beta Corp và Gamma Corp cũng được nhắc tới.")
            # (3) only 2 companies -> untouched (both kept)
            storage.insert_article(conn, {"article_id": "r::3", "url_canonical": "u3",
                "url_original": "u3", "domain": "d", "title": "Xử phạt vi phạm môi trường",
                "title_hash": "h3", "backend": "google_rss", "group_key": "kw",
                "sub_query_ix": 0, "body_status": "fetched"})
            storage.mark_body(conn, "r::3", "fetched",
                "Alpha Corp và Beta Corp bị xử phạt vì vi phạm.")
            conn.close()

            match.run(db_path=db)

            conn = storage.connect(db)
            doc_dir = settings.PER_TICKER_DIR
            def in_pt(tk, aid):
                p = doc_dir / f"{tk}.json"
                if not p.exists():
                    return False
                return aid in {a["article_id"] for a in json.loads(p.read_text("utf-8"))["articles"]}

            # (1) roundup, all body -> none attributed
            assert not in_pt("AAA", "r::1") and not in_pt("BBB", "r::1") and not in_pt("CCC", "r::1")
            # (2) only the title company kept
            assert in_pt("AAA", "r::2")
            assert not in_pt("BBB", "r::2") and not in_pt("CCC", "r::2")
            # (3) 2-company article -> both kept
            assert in_pt("AAA", "r::3") and in_pt("BBB", "r::3")
            conn.close()
        finally:
            settings.PER_TICKER_DIR, match.BATCH_SIZE, settings.AMBIGUOUS_ALIASES_PATH = _opt, _obs, _osp
            alias_matcher.reload()
    print("  roundup_drops_nontitle_keeps_title OK")


def main():
    if sys.platform == "win32":
        try: sys.stdout.reconfigure(encoding="utf-8")
        except Exception: pass
    print("running roundup gate test…")
    test_roundup_drops_nontitle_keeps_title()
    print("ALL OK")


if __name__ == "__main__":
    main()
```

> The test relies on the ESG filter keeping these (each article has a risk
> keyword: "sai phạm" / "xử phạt" / "vi phạm"). If `esg_filter.classify` rejects
> any, adjust the wording to a clear G/E keyword so the article passes the filter
> and the test isolates Fix B (not the ESG verdict).

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m tests.test_match_roundup`
Expected: FAIL — without the gate, roundup `r::1` attributes to AAA/BBB/CCC (assert `not in_pt` fails).

- [ ] **Step 3: Implement the gate**

In `pipeline/match.py` `_process_article`, insert immediately **after**
`verdict = esg_filter.classify(art_d)` (line 107) and **before**
`if hits and verdict.keep:` (line 108):

```python
        # Fix B: roundup/aboutness gate. An article naming >=3 distinct tracked
        # companies is almost always a listicle/roundup (donation lists,
        # rankings, "which bank is best"); keep only title attributions. Emptying
        # `hits` here correctly routes the article to the unmatched/deferred
        # branch below.
        if len(hits) >= 3:
            hits = [h for h in hits if h.location == "title"]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m tests.test_match_roundup`
Expected: `ALL OK`.

- [ ] **Step 5: Run the rematch suite (no regression)**

Run: `python -m tests.test_rematch`
Expected: `ALL OK`.

- [ ] **Step 6: Commit**

```bash
git add pipeline/match.py tests/test_match_roundup.py
git commit -m "feat(match): roundup gate — drop non-title hits in >=3-company articles (Fix B)"
```

---

## Chunk 4: Rollout (audit, backfill, deploy, rematch, verify)

These steps are operational; they have no new code beyond the stoplist data.
Coordinate with the (separately-pending) chunked rematch — see
[`2026-06-01-esg-collector-rematch-redesign-design.md`](../specs/2026-06-01-esg-collector-rematch-redesign-design.md).
Per `esg-collector/CLAUDE.md`, **deploy is push-to-`main`; do not SSH manually.**

### Task 4.1: Audit & finalize the stoplist

- [ ] Re-confirm each `config/ambiguous_aliases.json` entry against real matched
  articles (the spec's audit method): every entry must be a generic
  word/city/currency/abbrev whose real events are independently covered by the
  company name. Remove any entry whose bare matches are actually real (the spec
  KEEPS REE/DIG/GEX/EIB/SAB/CII). Add any newly-found collisions. Commit changes.

### Task 4.2: Local before/after measurement (optional but recommended)

- [ ] Re-pull `gs://esg-scan-data/raw_esg/articles_full_*.ndjson` + `per_ticker/*.json`
  and re-run the spec's measurement checks to confirm: stoplisted surfaces → ~0
  matches; the Vinh-moat article no longer hits BCM/DXG/HVN/VRE; ≥3-company
  roundups lose non-title hits; distinctive-ticker real events (ACV/BAF) survive.

### Task 4.3: Run the full local test suite

- [ ] Run each test module and confirm `ALL OK`:

```bash
python -m tests.test_stoplist
python -m tests.test_body_clean
python -m tests.test_jina_headers
python -m tests.test_fetch_one_clean
python -m tests.test_clean_bodies
python -m tests.test_match_roundup
python -m tests.test_rematch
```

### Task 4.4: Deploy + body-clean backfill + rematch

- [ ] Push the branch / merge to `main` (triggers the automated deploy).
- [ ] After deploy, once the chunked/detached rematch is confirmed runnable
  (per the rematch spec), run the one-shot body clean on the VM DB, then trigger
  a full rematch:
  - body clean: `python -m pipeline.clean_bodies` (idempotent; safe to re-run)
  - rematch: Actions UI → "Deploy esg-collector" → tick `run_rematch_all`
- [ ] Re-export `per_ticker/*.json` + the web bucket so the dashboard reflects
  the purge.

### Task 4.5: Verify in production

- [ ] On the live dashboard, confirm the previously-flagged false positives are
  gone (e.g. "Ô nhiễm … hào thành cổ Vinh" no longer under BCM/DXG/HVN/VRE; no
  bare-ticker city/word collisions) and that real events still appear (ACV
  pollution fine, BAF waste, Kido/Novaland events).

---

## Done criteria

- All seven test modules print `ALL OK`.
- The four fixes are committed as separate, revertable commits.
- `ambiguous_aliases.json` is audited.
- Post-rematch: total matches drop (~268 ticker + audited fragments + ~774
  roundup + the body-sidebar class); spot-checked dropped matches are junk and
  kept matches are real (no recall loss).
