# ESG Collector → Cloud Run Migration Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move `esg-collector` off the always-on GCE e2-micro VM onto Cloud Run Jobs — one container image, two job profiles (scheduled `daily` + manual `backfill`) — with the SQLite `articles.db` relocated to a single GCS blob that each run checks out and checks in under a lock.

**Architecture:** A thin new `runtime/` layer wraps the *unchanged* collection/match/enrich/export logic. Each run: acquire a GCS lock → download the DB blob → run the existing pipeline stages on a local SQLite file in `/tmp` → upload the blob back under an `if_generation_match` precondition → release the lock. Workers gain a `--drain` mode (exit when the queue is empty instead of sleeping forever) so the 24/7 producer/consumer model collapses into one batch job. `pipeline/export.py`'s GCS I/O moves from the `gsutil` CLI to the `google-cloud-storage` Python client so the image needs no Cloud SDK.

**Tech Stack:** Python 3.11, SQLite (WAL), `google-cloud-storage`, Cloud Run Jobs, Cloud Scheduler, Artifact Registry, Secret Manager, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-06-04-esg-collector-cloud-run-migration-design.md`

**Conventions for every task below:**
- All paths are relative to the repo's `esg-collector/` directory unless noted.
- Run tests from `esg-collector/` with: `python -m pytest tests/<file>.py -v` (each test inserts the package ROOT on `sys.path`, so `from core import storage` resolves).
- Commit messages follow the repo's `type(scope): subject` style (e.g. `feat(runtime): ...`).
- New runtime code lives under `esg-collector/runtime/`. GCS access is duck-typed over a `bucket` object so tests inject an in-memory fake — **no network, no Docker, no `fake-gcs-server` dependency.**

---

## File Structure

**New files:**
| File | Responsibility |
|---|---|
| `runtime/__init__.py` | package marker |
| `runtime/gcs.py` | low-level GCS helpers (duck-typed over a `bucket`): download/upload file, read/write/delete text, all with optional `if_generation_match` |
| `runtime/gcs_state.py` | DB-blob checkout/checkin: `download_db()` → generation, `upload_db(if_generation)` |
| `runtime/gcs_lock.py` | distributed mutex: `acquire`/`refresh`/`release`/stale-takeover via `if_generation_match=0` |
| `runtime/job.py` | orchestrator + container ENTRYPOINT: parse `--mode/--tickers`, sequence stages, own the lock+blob lifecycle |
| `Dockerfile` | package the app on `python:3.11-slim` |
| `tests/_fake_gcs.py` | in-memory `FakeBucket`/`FakeBlob` honoring generation-match semantics (shared test helper) |
| `tests/test_gcs.py`, `tests/test_gcs_state.py`, `tests/test_gcs_lock.py`, `tests/test_drain.py`, `tests/test_settings_env.py`, `tests/test_export_gcs.py`, `tests/test_job_orchestrator.py` | unit tests |
| `.github/workflows/deploy-esg-collector-cloudrun.yml` | build image → Artifact Registry → deploy both Cloud Run Jobs |
| `deploy/cloudrun/setup.sh` | one-time infra: APIs, Artifact Registry, Secret Manager, Scheduler, IAM (runbook, not run by CI) |
| `deploy/cloudrun/README.md` | cutover + decommission runbook |

**Modified files:**
| File | Change |
|---|---|
| `requirements.txt` | add `google-cloud-storage`; (de-dupe the duplicated lxml/feedparser lines) |
| `config/settings.py` | `ESG_DATA_DIR` env override for `DATA_DIR` (and derived paths); keep `_TODAY` as VN date |
| `core/storage.py` | add `has_remaining_tasks(conn, backend)` helper for the drain exit condition |
| `workers/runner.py` | add `--drain` mode |
| `workers/body_fetcher.py` | add `--drain` mode |
| `pipeline/export.py` | replace `gsutil` subprocess calls with `runtime/gcs.py` client calls |

---

## Chunk 1: GCS primitives (state blob + lock)

### Task 1: Add the GCS dependency and de-dupe requirements

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Edit `requirements.txt`** to the following exact content (adds `google-cloud-storage`, removes the duplicated lines):

```
beautifulsoup4>=4.12
googlenewsdecoder>=0.1.7
requests>=2.31
lxml>=5.0
feedparser>=6.0
google-cloud-storage>=2.16
```

- [ ] **Step 2: Install locally** so subsequent tests can import it.

Run: `python -m pip install google-cloud-storage>=2.16`
Expected: installs `google-cloud-storage` and `google-api-core` (which provides `google.api_core.exceptions.PreconditionFailed`).

- [ ] **Step 3: Commit**

```bash
git add esg-collector/requirements.txt
git commit -m "build(deps): add google-cloud-storage; de-dupe requirements"
```

---

### Task 2: In-memory fake GCS bucket (shared test helper)

A test double that mimics the subset of the `google-cloud-storage` `Bucket`/`Blob` API the runtime uses, **including generation-match semantics**, so lock/state logic is tested without network.

**Files:**
- Create: `tests/_fake_gcs.py`

- [ ] **Step 1: Write `tests/_fake_gcs.py`**

```python
"""In-memory fake of the google-cloud-storage Bucket/Blob subset we use.

Honors if_generation_match semantics so lock + state logic is unit-testable
with no network. Raises google.api_core.exceptions.PreconditionFailed on a
generation mismatch, exactly like the real client.
"""
from __future__ import annotations

from google.api_core.exceptions import NotFound, PreconditionFailed


class FakeBlob:
    def __init__(self, bucket: "FakeBucket", name: str):
        self._bucket = bucket
        self.name = name

    # --- generation helpers -------------------------------------------------
    @property
    def generation(self):
        rec = self._bucket._store.get(self.name)
        return rec[1] if rec else None

    def exists(self):
        return self.name in self._bucket._store

    def reload(self):
        if self.name not in self._bucket._store:
            raise NotFound(self.name)

    def _check_precondition(self, if_generation_match):
        if if_generation_match is None:
            return
        current = self.generation or 0
        if int(if_generation_match) != int(current):
            raise PreconditionFailed(
                f"generation mismatch: expected {if_generation_match}, have {current}"
            )

    def _write(self, data: bytes, if_generation_match):
        self._check_precondition(if_generation_match)
        self._bucket._gen += 1
        self._bucket._store[self.name] = (data, self._bucket._gen)

    # --- upload -------------------------------------------------------------
    def upload_from_string(self, data, if_generation_match=None, **_):
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._write(data, if_generation_match)

    def upload_from_filename(self, filename, if_generation_match=None, **_):
        with open(filename, "rb") as f:
            self._write(f.read(), if_generation_match)

    # --- download -----------------------------------------------------------
    def download_as_text(self):
        rec = self._bucket._store.get(self.name)
        if rec is None:
            raise NotFound(self.name)
        return rec[0].decode("utf-8")

    def download_to_filename(self, filename):
        rec = self._bucket._store.get(self.name)
        if rec is None:
            raise NotFound(self.name)
        with open(filename, "wb") as f:
            f.write(rec[0])

    # --- delete / acl -------------------------------------------------------
    def delete(self, if_generation_match=None):
        self._check_precondition(if_generation_match)
        self._bucket._store.pop(self.name, None)

    def make_public(self):
        self._bucket.public.add(self.name)


class FakeBucket:
    def __init__(self, name="esg-scan-data"):
        self.name = name
        self._store: dict[str, tuple[bytes, int]] = {}
        self._gen = 0
        self.public: set[str] = set()

    def blob(self, name: str) -> FakeBlob:
        return FakeBlob(self, name)
```

- [ ] **Step 2: Commit**

```bash
git add esg-collector/tests/_fake_gcs.py
git commit -m "test(runtime): in-memory fake GCS bucket with generation-match"
```

---

### Task 3: `runtime/gcs.py` — low-level GCS helpers

**Files:**
- Create: `runtime/__init__.py`
- Create: `runtime/gcs.py`
- Test: `tests/test_gcs.py`

- [ ] **Step 1: Write the failing test** `tests/test_gcs.py`

```python
import sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from google.api_core.exceptions import PreconditionFailed
from runtime import gcs
from tests._fake_gcs import FakeBucket


def test_upload_then_download_file_roundtrip(tmp_path):
    bucket = FakeBucket()
    src = tmp_path / "a.bin"; src.write_bytes(b"hello")
    gen = gcs.upload_file(bucket, "state/x.db", src)
    assert gen is not None
    dst = tmp_path / "b.bin"
    got_gen = gcs.download_file(bucket, "state/x.db", dst)
    assert got_gen == gen
    assert dst.read_bytes() == b"hello"


def test_download_missing_returns_none(tmp_path):
    bucket = FakeBucket()
    assert gcs.download_file(bucket, "state/missing.db", tmp_path / "out") is None


def test_upload_generation_match_conflict(tmp_path):
    bucket = FakeBucket()
    src = tmp_path / "a"; src.write_bytes(b"1")
    gen = gcs.upload_file(bucket, "k", src)
    # uploading again with the wrong expected generation must fail
    try:
        gcs.upload_file(bucket, "k", src, if_generation_match=gen + 99)
        assert False, "expected PreconditionFailed"
    except PreconditionFailed:
        pass


def test_text_roundtrip_and_public(tmp_path):
    bucket = FakeBucket()
    gcs.upload_text(bucket, "web/x.json", "[]", public=True)
    text, gen = gcs.read_text(bucket, "web/x.json")
    assert text == "[]" and gen is not None
    assert "web/x.json" in bucket.public
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_gcs.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'runtime'`.

- [ ] **Step 3: Write `runtime/__init__.py`** (empty file).

- [ ] **Step 4: Write `runtime/gcs.py`**

```python
"""Thin helpers over a google-cloud-storage Bucket, duck-typed so tests can
inject an in-memory fake. All functions take a `bucket` object whose `.blob(name)`
returns something with the google storage Blob API subset we use.
"""
from __future__ import annotations

import logging
from pathlib import Path

from google.api_core.exceptions import NotFound

log = logging.getLogger("runtime.gcs")

GCS_BUCKET_NAME = "esg-scan-data"


def get_bucket(name: str = GCS_BUCKET_NAME):
    """Real bucket from the default-credentials client (used outside tests)."""
    from google.cloud import storage  # imported lazily so tests need no creds
    return storage.Client().bucket(name)


def upload_file(bucket, blob_name: str, local_path, *,
                if_generation_match=None, public: bool = False) -> int:
    blob = bucket.blob(blob_name)
    blob.upload_from_filename(str(local_path), if_generation_match=if_generation_match)
    if public:
        blob.make_public()
    blob.reload()
    return blob.generation


def download_file(bucket, blob_name: str, local_path) -> int | None:
    """Download to local_path. Returns the blob generation, or None if absent."""
    blob = bucket.blob(blob_name)
    try:
        blob.download_to_filename(str(local_path))
        blob.reload()
        return blob.generation
    except NotFound:
        return None


def upload_text(bucket, blob_name: str, text: str, *,
                if_generation_match=None, public: bool = False) -> int:
    blob = bucket.blob(blob_name)
    blob.upload_from_string(text, if_generation_match=if_generation_match)
    if public:
        blob.make_public()
    blob.reload()
    return blob.generation


def read_text(bucket, blob_name: str) -> tuple[str, int] | None:
    blob = bucket.blob(blob_name)
    try:
        text = blob.download_as_text()
        blob.reload()
        return text, blob.generation
    except NotFound:
        return None


def delete(bucket, blob_name: str, *, if_generation_match=None) -> None:
    blob = bucket.blob(blob_name)
    try:
        blob.delete(if_generation_match=if_generation_match)
    except NotFound:
        pass
```

> Note: the fake's `make_public` records the name; the real client sets `AllUsers:READER` (the gsutil `acl ch -u AllUsers:R` equivalent — requires **UBLA OFF** on the bucket, per spec).

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_gcs.py -v`
Expected: PASS (4 tests).

- [ ] **Step 6: Commit**

```bash
git add esg-collector/runtime/__init__.py esg-collector/runtime/gcs.py esg-collector/tests/test_gcs.py
git commit -m "feat(runtime): google-cloud-storage helpers (duck-typed for tests)"
```

---

### Task 4: `runtime/gcs_state.py` — DB blob checkout/checkin

**Files:**
- Create: `runtime/gcs_state.py`
- Test: `tests/test_gcs_state.py`

- [ ] **Step 1: Write the failing test** `tests/test_gcs_state.py`

```python
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from google.api_core.exceptions import PreconditionFailed
from runtime import gcs_state
from tests._fake_gcs import FakeBucket


def test_first_download_returns_none_generation(tmp_path):
    bucket = FakeBucket()
    local = tmp_path / "articles.db"
    gen = gcs_state.download_db(bucket, local)
    assert gen is None
    assert not local.exists()  # nothing downloaded on a fresh bucket


def test_checkin_then_checkout_roundtrip(tmp_path):
    bucket = FakeBucket()
    local = tmp_path / "articles.db"; local.write_bytes(b"DBDATA")
    gen = gcs_state.upload_db(bucket, local, if_generation=0)  # create-only
    local2 = tmp_path / "articles2.db"
    got = gcs_state.download_db(bucket, local2)
    assert got == gen
    assert local2.read_bytes() == b"DBDATA"


def test_checkin_conflict_when_generation_moved(tmp_path):
    bucket = FakeBucket()
    local = tmp_path / "articles.db"; local.write_bytes(b"v1")
    gen = gcs_state.upload_db(bucket, local, if_generation=0)
    # someone else writes, moving the generation
    local.write_bytes(b"other")
    gcs_state.upload_db(bucket, local, if_generation=gen)
    # our stale checkin (still expecting `gen`) must fail
    local.write_bytes(b"v2")
    try:
        gcs_state.upload_db(bucket, local, if_generation=gen)
        assert False, "expected PreconditionFailed"
    except PreconditionFailed:
        pass
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_gcs_state.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'runtime.gcs_state'`.

- [ ] **Step 3: Write `runtime/gcs_state.py`**

```python
"""Checkout/checkin of the single SQLite DB blob on GCS.

download_db -> local file + its generation (None if the blob does not exist yet).
upload_db(if_generation) -> new generation; pass if_generation=0 for create-only,
or the generation returned by download_db for a safe overwrite. Raises
google.api_core.exceptions.PreconditionFailed if the blob moved underneath us.
"""
from __future__ import annotations

from runtime import gcs

DB_BLOB = "state/articles.db"


def download_db(bucket, local_path) -> int | None:
    return gcs.download_file(bucket, DB_BLOB, local_path)


def upload_db(bucket, local_path, *, if_generation) -> int:
    return gcs.upload_file(bucket, DB_BLOB, local_path,
                           if_generation_match=if_generation)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_gcs_state.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add esg-collector/runtime/gcs_state.py esg-collector/tests/test_gcs_state.py
git commit -m "feat(runtime): DB blob checkout/checkin with generation-match"
```

---

### Task 5: `runtime/gcs_lock.py` — distributed mutex

**Files:**
- Create: `runtime/gcs_lock.py`
- Test: `tests/test_gcs_lock.py`

The lock is a single blob `state/pipeline.lock` holding JSON `{owner, mode, started_at, ttl_seconds}`. `acquire` creates it with `if_generation_match=0` (create-only → atomic). If it already exists, the holder is alive unless `started_at + ttl_seconds` is in the past (stale → take over). `now`/`started_at` are passed in as ISO strings so tests are deterministic (no `Date.now`-style hidden clock).

- [ ] **Step 1: Write the failing test** `tests/test_gcs_lock.py`

```python
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime import gcs_lock
from tests._fake_gcs import FakeBucket


def test_acquire_on_empty_succeeds():
    bucket = FakeBucket()
    h = gcs_lock.acquire(bucket, owner="a", mode="daily",
                         now="2026-06-04T00:00:00Z", ttl_seconds=3600)
    assert h is not None and h.owner == "a"


def test_second_acquire_while_fresh_fails():
    bucket = FakeBucket()
    gcs_lock.acquire(bucket, owner="a", mode="daily",
                     now="2026-06-04T00:00:00Z", ttl_seconds=3600)
    h2 = gcs_lock.acquire(bucket, owner="b", mode="daily",
                          now="2026-06-04T00:10:00Z", ttl_seconds=3600)  # +10m < 1h TTL
    assert h2 is None


def test_stale_lock_is_taken_over():
    bucket = FakeBucket()
    gcs_lock.acquire(bucket, owner="a", mode="daily",
                     now="2026-06-04T00:00:00Z", ttl_seconds=3600)
    h2 = gcs_lock.acquire(bucket, owner="b", mode="backfill",
                          now="2026-06-04T02:00:00Z", ttl_seconds=3600)  # +2h > 1h TTL
    assert h2 is not None and h2.owner == "b"


def test_release_lets_next_acquire():
    bucket = FakeBucket()
    h = gcs_lock.acquire(bucket, owner="a", mode="daily",
                         now="2026-06-04T00:00:00Z", ttl_seconds=3600)
    gcs_lock.release(bucket, h)
    h2 = gcs_lock.acquire(bucket, owner="b", mode="daily",
                          now="2026-06-04T00:01:00Z", ttl_seconds=3600)
    assert h2 is not None and h2.owner == "b"


def test_refresh_extends_started_at():
    bucket = FakeBucket()
    h = gcs_lock.acquire(bucket, owner="a", mode="daily",
                         now="2026-06-04T00:00:00Z", ttl_seconds=3600)
    gcs_lock.refresh(bucket, h, now="2026-06-04T00:50:00Z")
    # a contender 30m after the REFRESH (but >1h after first acquire) is still blocked
    h2 = gcs_lock.acquire(bucket, owner="b", mode="daily",
                          now="2026-06-04T01:20:00Z", ttl_seconds=3600)
    assert h2 is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_gcs_lock.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'runtime.gcs_lock'`.

- [ ] **Step 3: Write `runtime/gcs_lock.py`**

```python
"""GCS-object mutex for the pipeline.

acquire(): create state/pipeline.lock with if_generation_match=0 (atomic create).
If it already exists, parse its body; take over only if started_at+ttl is past.
Returns a LockHandle (carries the blob generation we hold) or None if a live
owner holds it. release()/refresh() act through that handle's generation so two
jobs can't stomp each other.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from google.api_core.exceptions import PreconditionFailed
from runtime import gcs

log = logging.getLogger("runtime.gcs_lock")

LOCK_BLOB = "state/pipeline.lock"


@dataclass
class LockHandle:
    owner: str
    generation: int


def _parse(iso: str) -> datetime:
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _is_stale(body: dict, now: str) -> bool:
    try:
        started = _parse(body["started_at"])
        ttl = int(body.get("ttl_seconds", 3600))
    except Exception:
        return True  # unparseable lock → treat as abandoned
    return _parse(now) > started + timedelta(seconds=ttl)


def _write(bucket, *, owner, mode, now, ttl_seconds, if_generation) -> LockHandle | None:
    body = json.dumps({"owner": owner, "mode": mode,
                       "started_at": now, "ttl_seconds": ttl_seconds})
    try:
        gen = gcs.upload_text(bucket, LOCK_BLOB, body, if_generation_match=if_generation)
        return LockHandle(owner=owner, generation=gen)
    except PreconditionFailed:
        return None


def acquire(bucket, *, owner: str, mode: str, now: str,
            ttl_seconds: int = 3600) -> LockHandle | None:
    # 1. try create-only
    h = _write(bucket, owner=owner, mode=mode, now=now,
               ttl_seconds=ttl_seconds, if_generation=0)
    if h is not None:
        return h
    # 2. exists — read it; take over only if stale, using its generation as the
    #    precondition so a racing taker-over loses cleanly.
    cur = gcs.read_text(bucket, LOCK_BLOB)
    if cur is None:
        return None  # vanished between create-fail and read; let caller retry
    text, gen = cur
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        body = {}
    if not _is_stale(body, now):
        log.info("lock held by %s — skipping", body.get("owner"))
        return None
    log.warning("stale lock (owner=%s) — taking over", body.get("owner"))
    return _write(bucket, owner=owner, mode=mode, now=now,
                  ttl_seconds=ttl_seconds, if_generation=gen)


def refresh(bucket, handle: LockHandle, *, now: str, mode: str = "daily",
            ttl_seconds: int = 3600) -> LockHandle | None:
    h = _write(bucket, owner=handle.owner, mode=mode, now=now,
               ttl_seconds=ttl_seconds, if_generation=handle.generation)
    return h  # None if someone else moved the lock; caller should abort


def release(bucket, handle: LockHandle) -> None:
    try:
        gcs.delete(bucket, LOCK_BLOB, if_generation_match=handle.generation)
    except PreconditionFailed:
        log.warning("lock moved before release — not ours to delete")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_gcs_lock.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add esg-collector/runtime/gcs_lock.py esg-collector/tests/test_gcs_lock.py
git commit -m "feat(runtime): GCS object mutex with stale-takeover + refresh"
```

---

## Chunk 2: Drain mode + container-friendly config

### Task 6: `ESG_DATA_DIR` env override in settings

**Files:**
- Modify: `config/settings.py:13-21` and `:60-63`
- Test: `tests/test_settings_env.py`

- [ ] **Step 1: Write the failing test** `tests/test_settings_env.py`

```python
import importlib, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_data_dir_follows_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ESG_DATA_DIR", str(tmp_path / "esg"))
    from config import settings as s
    importlib.reload(s)
    try:
        assert s.DATA_DIR == tmp_path / "esg"
        assert s.DB_PATH == tmp_path / "esg" / "articles.db"
        assert s.PER_TICKER_DIR == tmp_path / "esg" / "per_ticker"
        assert s.WEB_DIR == tmp_path / "esg" / "web"
        assert s.DATA_DIR.exists()  # import-time mkdir still runs, under the env dir
    finally:
        monkeypatch.delenv("ESG_DATA_DIR", raising=False)
        importlib.reload(s)  # restore default for other tests


def test_default_data_dir_when_env_absent(monkeypatch):
    monkeypatch.delenv("ESG_DATA_DIR", raising=False)
    from config import settings as s
    importlib.reload(s)
    assert s.DATA_DIR == s.ROOT / "data"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_settings_env.py -v`
Expected: FAIL — `test_data_dir_follows_env` asserts `DATA_DIR == tmp/esg` but it is still `ROOT/data`.

- [ ] **Step 3: Edit `config/settings.py`** — change the `DATA_DIR` line (currently line 14) from:

```python
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
LOGS_DIR = ROOT / "logs"
```

to:

```python
ROOT = Path(__file__).resolve().parent.parent
# DATA_DIR is env-overridable so Cloud Run can point it at writable /tmp.
DATA_DIR = Path(os.environ["ESG_DATA_DIR"]) if os.environ.get("ESG_DATA_DIR") else ROOT / "data"
LOGS_DIR = ROOT / "logs"
```

(`PER_TICKER_DIR`, `WEB_DIR`, `DB_PATH` already derive from `DATA_DIR`, so they follow automatically. The import-time `mkdir` block at the bottom is unchanged — it now creates the env dir.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_settings_env.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Run the full suite** to confirm no regression from the reload.

Run: `python -m pytest tests/ -v`
Expected: PASS (all existing tests still pass).

- [ ] **Step 6: Commit**

```bash
git add esg-collector/config/settings.py esg-collector/tests/test_settings_env.py
git commit -m "feat(config): ESG_DATA_DIR env override for container /tmp"
```

---

### Task 7: `has_remaining_tasks` storage helper (drain exit condition)

**Files:**
- Modify: `core/storage.py` (add after `queue_stats`, ~line 510)
- Test: `tests/test_drain.py` (first half)

- [ ] **Step 1: Write the failing test** `tests/test_drain.py`

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_drain.py -v`
Expected: FAIL — `AttributeError: module 'core.storage' has no attribute 'has_remaining_tasks'`.

- [ ] **Step 3: Add `has_remaining_tasks` to `core/storage.py`** (after `queue_stats`):

```python
def has_remaining_tasks(conn: sqlite3.Connection, backend: str) -> bool:
    """True if `backend` still has work to drain — any pending/backoff/in_progress
    row, regardless of next_attempt. (A backed-off task with a future next_attempt
    counts: the drain loop must wait for it, not exit.) 'failed' and 'done' do not
    count, so the loop terminates once retries are exhausted (MAX_ATTEMPTS)."""
    row = conn.execute(
        "SELECT 1 FROM search_queue "
        "WHERE backend=? AND status IN ('pending','backoff','in_progress') LIMIT 1",
        (backend,),
    ).fetchone()
    return row is not None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_drain.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add esg-collector/core/storage.py esg-collector/tests/test_drain.py
git commit -m "feat(storage): has_remaining_tasks for drain exit condition"
```

---

### Task 8: `--drain` mode for the fetch worker

**Files:**
- Modify: `workers/runner.py:152-226` (the `run()` loop + `main()`)
- Test: `tests/test_drain.py` (append)

- [ ] **Step 1: Append the failing test** to `tests/test_drain.py`

```python
def test_runner_drain_processes_then_exits(monkeypatch):
    """In drain mode the runner drains one task then returns (does not block)."""
    import tempfile
    from workers import runner

    class _FakeBackend:
        name = "brave"
        @staticmethod
        def fetch(query, after, before):
            return []  # no items; we only assert the loop terminates

    monkeypatch.setattr(runner, "_load_backend", lambda name: _FakeBackend)

    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "q.db"
        monkeypatch.setenv("ESG_DATA_DIR", td)
        import importlib
        from config import settings as s; importlib.reload(s)
        importlib.reload(storage)
        storage.init_db()
        conn = storage.connect(); 
        storage.enqueue_task(conn, backend="brave", kind="keyword", ticker=None,
                             group_key="kw", sub_query_ix=0, query="x",
                             after="2026-01-01", before="2026-01-07")
        conn.close()
        # must return on its own (no SIGINT) because the queue drains
        runner.run("brave", drain=True, throttle_override=0)
        conn = storage.connect()
        assert storage.has_remaining_tasks(conn, "brave") is False
        conn.close()
    importlib.reload(s); importlib.reload(storage)
```

> Note: `throttle_override=0` keeps the test instant; without it the runner would sleep the configured `brave` throttle (1s) between tasks.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_drain.py::test_runner_drain_processes_then_exits -v`
Expected: FAIL — `run()` got an unexpected keyword `drain`.

- [ ] **Step 3: Edit `workers/runner.py`** — change the `run()` signature and the idle branch.

Change the signature (line 152) from `def run(backend_name: str) -> None:` to:

```python
def run(backend_name: str, *, drain: bool = False,
        throttle_override: float | None = None) -> None:
```

Inside `run()`, change the throttle line (currently `throttle = settings.THROTTLE[backend_name]`) to:

```python
    throttle = throttle_override if throttle_override is not None else settings.THROTTLE[backend_name]
```

Replace the idle branch (currently lines ~170-177):

```python
        task = storage.next_task(conn, backend_name)
        if task is None:
            log.info("no task ready — sleeping %ds", idle_sleep)
            for _ in range(idle_sleep):
                if _stop:
                    break
                time.sleep(1)
            continue
```

with:

```python
        task = storage.next_task(conn, backend_name)
        if task is None:
            if drain:
                if not storage.has_remaining_tasks(conn, backend_name):
                    log.info("drain: queue empty for %s — exiting", backend_name)
                    break
                # tasks exist but are backing off — wait briefly for next_attempt
                log.info("drain: only backed-off tasks left — idle %ds", idle_sleep)
            else:
                log.info("no task ready — sleeping %ds", idle_sleep)
            for _ in range(idle_sleep):
                if _stop:
                    break
                time.sleep(1)
            continue
```

Update `main()` (lines ~222-226) to parse `--drain`:

```python
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", required=True, choices=list(BACKEND_MODULES.keys()))
    ap.add_argument("--drain", action="store_true",
                    help="exit when the queue is fully drained instead of polling forever")
    args = ap.parse_args()
    run(args.backend, drain=args.drain)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_drain.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add esg-collector/workers/runner.py esg-collector/tests/test_drain.py
git commit -m "feat(workers): --drain mode for fetch worker (exit on empty queue)"
```

---

### Task 9: `--drain` mode for the body fetcher

**Files:**
- Modify: `workers/body_fetcher.py:78-151`
- Test: `tests/test_drain.py` (append)

- [ ] **Step 1: Append the failing test** to `tests/test_drain.py`

```python
def test_body_fetcher_drain_exits_when_no_pending(monkeypatch):
    import tempfile, importlib
    from workers import body_fetcher

    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setenv("ESG_DATA_DIR", td)
        from config import settings as s; importlib.reload(s); importlib.reload(storage)
        storage.init_db()
        # no body_status='pending' rows at all → drain must return immediately
        body_fetcher.run(workers=1, drain=True)
    importlib.reload(s); importlib.reload(storage)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_drain.py::test_body_fetcher_drain_exits_when_no_pending -v`
Expected: FAIL — `run()` got an unexpected keyword `drain`.

- [ ] **Step 3: Edit `workers/body_fetcher.py`** — change `run()` signature (line 78) from:

```python
def run(workers: int = 8, batch_limit: int = 500, idle_sleep: int = 60) -> None:
```

to:

```python
def run(workers: int = 8, batch_limit: int = 500, idle_sleep: int = 60,
        *, drain: bool = False) -> None:
```

Replace the empty-batch branch (lines ~91-98):

```python
        candidates = _candidate_articles(conn, batch_limit)
        if not candidates:
            log.info("no pending bodies — sleeping %ds", idle_sleep)
            for _ in range(idle_sleep):
                if _stop:
                    break
                time.sleep(1)
            continue
```

with:

```python
        candidates = _candidate_articles(conn, batch_limit)
        if not candidates:
            if drain:
                log.info("drain: no pending bodies — exiting")
                break
            log.info("no pending bodies — sleeping %ds", idle_sleep)
            for _ in range(idle_sleep):
                if _stop:
                    break
                time.sleep(1)
            continue
```

Update `main()` (lines ~146-151):

```python
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--batch-limit", type=int, default=500)
    ap.add_argument("--drain", action="store_true",
                    help="exit when no pending bodies remain instead of polling forever")
    args = ap.parse_args()
    run(workers=args.workers, batch_limit=args.batch_limit, drain=args.drain)
```

> Drain note: `_candidate_articles` selects `body_status='pending' AND match_status='pending'`. Rows the matcher already resolved are excluded, so "no candidates" is the correct terminal condition for body work.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_drain.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add esg-collector/workers/body_fetcher.py esg-collector/tests/test_drain.py
git commit -m "feat(workers): --drain mode for body fetcher"
```

---

## Chunk 3: Export GCS rewrite (gsutil → client)

### Task 10: Replace `gsutil` calls in `pipeline/export.py`

**Files:**
- Modify: `pipeline/export.py:24` (import), `:34` (bucket const), `:79-97` (`_gsutil_cp`/`_upload`), `:99` (`WEB_PREFIX`), `:192-197` (`_upload_web`)
- Test: `tests/test_export_gcs.py`

The functional logic of `build_esg_events` / `_write_web_files` is unchanged — only the *upload* path moves to the client. We inject a `bucket` so the test asserts blobs land at the right names with the right public flag.

- [ ] **Step 1: Write the failing test** `tests/test_export_gcs.py`

```python
import sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline import export
from tests._fake_gcs import FakeBucket


def test_upload_ndjson_lands_in_raw_esg(tmp_path):
    bucket = FakeBucket()
    nd = tmp_path / "articles_full_20260604.ndjson"; nd.write_text("{}\n")
    export._upload(nd, bucket=bucket)
    assert "raw_esg/articles_full_20260604.ndjson" in bucket._store


def test_upload_web_sets_public(tmp_path):
    bucket = FakeBucket()
    ev = tmp_path / "esg_events.json"; ev.write_text("[]")
    top = tmp_path / "top100.json"; top.write_text("[]")
    export._upload_web(ev, top, bucket=bucket)
    assert "web/esg_events.json" in bucket._store
    assert "web/top100.json" in bucket._store
    assert "web/esg_events.json" in bucket.public  # public-read re-applied
    assert "web/top100.json" in bucket.public
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_export_gcs.py -v`
Expected: FAIL — `_upload()` got an unexpected keyword `bucket` (still the gsutil version).

- [ ] **Step 3: Edit `pipeline/export.py`.**

Replace the `import subprocess` line (line 24) with:

```python
from runtime import gcs
```

Change the bucket constant (line 34) from `GCS_BUCKET = "gs://esg-scan-data"` to:

```python
GCS_BUCKET_NAME = gcs.GCS_BUCKET_NAME  # "esg-scan-data"
```

Replace `_gsutil_cp` and `_upload` (lines 79-97) with:

```python
def _upload(ndjson: Path, *, bucket=None) -> None:
    bucket = bucket if bucket is not None else gcs.get_bucket()
    gcs.upload_file(bucket, f"raw_esg/{ndjson.name}", ndjson)
    if settings.PER_TICKER_DIR.exists():
        for p in sorted(settings.PER_TICKER_DIR.glob("*.json")):
            gcs.upload_file(bucket, f"per_ticker/{p.name}", p)
```

Change `WEB_PREFIX` (line 99) from `WEB_PREFIX = f"{GCS_BUCKET}/web"` to:

```python
WEB_PREFIX = "web"
```

Replace `_upload_web` (lines 192-197) with:

```python
def _upload_web(ev_path: Path, top_path: Path, *, bucket=None) -> None:
    bucket = bucket if bucket is not None else gcs.get_bucket()
    for src in (ev_path, top_path):
        # objects are overwritten each run → re-apply public-read ACL each time
        # (requires UBLA OFF on the bucket).
        gcs.upload_file(bucket, f"{WEB_PREFIX}/{src.name}", src, public=True)
```

In `run()` (lines 200-225) the two call sites need no signature change — `_upload(ndjson_path)` and `_upload_web(ev_path, top_path)` still work (bucket defaults to the real one). Leave them as-is.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_export_gcs.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Run the full suite** (export is imported by other tests).

Run: `python -m pytest tests/ -v`
Expected: PASS.

- [ ] **Step 6: Update the module docstring** (lines 11-16) — replace the "requires gsutil + auth" line with "requires google-cloud-storage + ADC on the running host". Then commit.

```bash
git add esg-collector/pipeline/export.py esg-collector/tests/test_export_gcs.py
git commit -m "refactor(export): GCS I/O via google-cloud-storage client (drop gsutil)"
```

---

## Chunk 4: Orchestrator + container

### Task 11: `runtime/job.py` — the orchestrator / ENTRYPOINT

**Files:**
- Create: `runtime/job.py`
- Test: `tests/test_job_orchestrator.py`

The orchestrator runs each pipeline stage as a **subprocess** (`python -m workers.runner --backend X --drain`, etc.) so each reuses its existing entrypoint and isolated module-global state, all pointed at the same `ESG_DATA_DIR` SQLite file. It owns the lock+blob lifecycle around the stages. Stage commands are produced by a pure `stage_commands(mode)` function so they're asserted without spawning anything.

- [ ] **Step 1: Write the failing test** `tests/test_job_orchestrator.py`

```python
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime import job


def _joined(cmds):
    return [" ".join(c) for c in cmds]


def test_daily_stage_order_includes_enrich():
    cmds = _joined(job.stage_commands("daily", tickers=None))
    # fetch (3 backends, drained) → body → match → enrich → export
    assert any("workers.runner --backend google_rss --drain" in c for c in cmds)
    assert any("workers.runner --backend baomoi --drain" in c for c in cmds)
    assert any("workers.runner --backend brave --drain" in c for c in cmds)
    assert any("workers.body_fetcher --drain" in c for c in cmds)
    assert any("pipeline.match" in c for c in cmds)
    assert any("enrich.runner --limit 25" in c for c in cmds)
    assert any("pipeline.export --ndjson --web --upload" in c for c in cmds)
    # enrich must come after match and before export
    i_match = next(i for i, c in enumerate(cmds) if "pipeline.match" in c)
    i_enrich = next(i for i, c in enumerate(cmds) if "enrich.runner" in c)
    i_export = next(i for i, c in enumerate(cmds) if "pipeline.export" in c)
    assert i_match < i_enrich < i_export


def test_backfill_skips_enrich_and_uses_rematch():
    cmds = _joined(job.stage_commands("backfill", tickers=None))
    assert not any("enrich.runner" in c for c in cmds)
    assert any("queue_builder --mode backfill" in c for c in cmds)
    assert any("pipeline.match --rematch-all" in c for c in cmds)


def test_backfill_with_tickers_scopes_enqueue():
    cmds = _joined(job.stage_commands("backfill", tickers=["DBC", "HPG"]))
    assert any("queue_builder --mode backfill --tickers DBC HPG" in c for c in cmds)


def test_daily_enqueue_is_daily_mode():
    cmds = _joined(job.stage_commands("daily", tickers=None))
    assert any("queue_builder --mode daily" in c for c in cmds)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_job_orchestrator.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'runtime.job'`.

- [ ] **Step 3: Write `runtime/job.py`**

```python
"""Cloud Run Job entrypoint: own the lock + DB blob, run the pipeline stages.

  python -m runtime.job --mode daily
  python -m runtime.job --mode backfill [--tickers DBC HPG]

Lifecycle: acquire lock → download DB blob → init_db → run stages (each a
subprocess on the shared ESG_DATA_DIR SQLite) → upload blob (generation-match)
→ release lock. If the lock is held by a live owner, exit 0 ("skipped").
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone

from config import settings
from core import storage
from runtime import gcs, gcs_lock, gcs_state

log = logging.getLogger("runtime.job")

PY = sys.executable
BACKENDS = ("google_rss", "baomoi", "brave")
ENRICH_LIMIT = 25


def stage_commands(mode: str, tickers: list[str] | None) -> list[list[str]]:
    """Pure: the ordered argv lists for one run. Fetch backends are listed
    separately but the orchestrator runs the three concurrently (see run())."""
    enqueue = [PY, "-m", "core.queue_builder", "--mode", mode]
    if tickers:
        enqueue += ["--tickers", *tickers]

    cmds: list[list[str]] = [enqueue]
    for b in BACKENDS:
        cmds.append([PY, "-m", "workers.runner", "--backend", b, "--drain"])
    cmds.append([PY, "-m", "workers.body_fetcher", "--drain"])

    if mode == "backfill":
        cmds.append([PY, "-m", "pipeline.match", "--rematch-all"])
        # enrich intentionally skipped in backfill (daily catches up)
        cmds.append([PY, "-m", "pipeline.export", "--ndjson", "--web", "--upload"])
    else:  # daily
        cmds.append([PY, "-m", "pipeline.match"])
        cmds.append([PY, "-m", "enrich.runner", "--limit", str(ENRICH_LIMIT)])
        cmds.append([PY, "-m", "pipeline.export", "--ndjson", "--web", "--upload"])
    return cmds


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_fetch_concurrently(cmds: list[list[str]], env) -> None:
    """Run the three fetch-backend subprocesses in parallel, wait for all."""
    procs = [subprocess.Popen(c, env=env) for c in cmds]
    for p in procs:
        rc = p.wait()
        if rc != 0:
            log.warning("fetch subprocess exited rc=%d (continuing)", rc)


def run(mode: str, tickers: list[str] | None, *, ttl_seconds: int) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s/%(levelname)s] %(message)s")
    owner = os.environ.get("CLOUD_RUN_EXECUTION", _now())
    bucket = gcs.get_bucket()

    handle = gcs_lock.acquire(bucket, owner=owner, mode=mode,
                              now=_now(), ttl_seconds=ttl_seconds)
    if handle is None:
        log.info("another run holds the lock — skipping this execution")
        return 0
    try:
        gen = gcs_state.download_db(bucket, settings.DB_PATH)
        storage.init_db()  # apply migrations on the downloaded (or fresh) blob

        cmds = stage_commands(mode, tickers)
        env = dict(os.environ)
        fetch_cmds = [c for c in cmds if "workers.runner" in " ".join(c)]
        other_cmds = [c for c in cmds if c not in fetch_cmds]

        # enqueue (first non-fetch command) before fetching
        enqueue, *post_fetch = other_cmds
        subprocess.run(enqueue, env=env, check=False)
        gcs_lock.refresh(bucket, handle, now=_now(), mode=mode, ttl_seconds=ttl_seconds)

        _run_fetch_concurrently(fetch_cmds, env)
        gcs_lock.refresh(bucket, handle, now=_now(), mode=mode, ttl_seconds=ttl_seconds)

        for c in post_fetch:  # body → match → [enrich] → export
            subprocess.run(c, env=env, check=False)
            gcs_lock.refresh(bucket, handle, now=_now(), mode=mode, ttl_seconds=ttl_seconds)

        new_gen = 0 if gen is None else gen
        gcs_state.upload_db(bucket, settings.DB_PATH, if_generation=new_gen)
        log.info("checked in DB blob; run complete")
        return 0
    finally:
        gcs_lock.release(bucket, handle)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("daily", "backfill"), required=True)
    ap.add_argument("--tickers", nargs="+", default=None)
    ap.add_argument("--lock-ttl", type=int, default=int(os.environ.get("LOCK_TTL_SECONDS", "7200")))
    args = ap.parse_args()
    raise SystemExit(run(args.mode, args.tickers, ttl_seconds=args.lock_ttl))


if __name__ == "__main__":
    main()
```

> The `stage_commands` split in `run()` re-filters fetch vs non-fetch so the three backends run concurrently while everything else stays sequential. `enqueue` is the first non-fetch command by construction. The lock is refreshed between stages so a long backfill never trips its own TTL.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_job_orchestrator.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add esg-collector/runtime/job.py esg-collector/tests/test_job_orchestrator.py
git commit -m "feat(runtime): job orchestrator (lock+blob lifecycle, stage sequencing)"
```

---

### Task 12: Dockerfile

**Files:**
- Create: `Dockerfile`

- [ ] **Step 1: Write `esg-collector/Dockerfile`**

```dockerfile
FROM python:3.11-slim

# lxml needs libxml2/libxslt at runtime; build from wheels so no compiler needed.
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Writable SQLite + outputs live in /tmp on Cloud Run (only writable path).
ENV ESG_DATA_DIR=/tmp/esg-data
ENV PYTHONUNBUFFERED=1

# Default to a daily run; the backfill job overrides --args.
ENTRYPOINT ["python", "-m", "runtime.job"]
CMD ["--mode", "daily"]
```

- [ ] **Step 2: Verify the image builds via Cloud Build** (no local Docker). Requires the one-time setup in Task 14 to have created the Artifact Registry repo; if running this step early, just lint the Dockerfile syntax by eye.

Run (once infra exists): `gcloud builds submit esg-collector --tag us-central1-docker.pkg.dev/gen-lang-client-0020762472/esg/esg-collector:test`
Expected: `SUCCESS`, image pushed.

- [ ] **Step 3: Commit**

```bash
git add esg-collector/Dockerfile
git commit -m "build(docker): slim Python image, runtime.job entrypoint"
```

---

### Task 13: Full local suite green + smoke import

**Files:** none (verification task)

- [ ] **Step 1: Run the entire test suite from `esg-collector/`.**

Run: `python -m pytest tests/ -v`
Expected: PASS — all chunk-1..4 tests plus the pre-existing suite.

- [ ] **Step 2: Smoke-import the entrypoint** (catches import-time errors the container would hit).

Run: `python -c "import runtime.job, pipeline.export, workers.runner, workers.body_fetcher; print('imports OK')"`
Expected: `imports OK`.

- [ ] **Step 3: Commit** (no-op if nothing changed; otherwise any lint fixes).

```bash
git commit --allow-empty -m "test: full suite green before infra wiring"
```

---

## Chunk 5: CI + infra (deploy)

> These tasks produce infra config and a runbook. They are not unit-testable; each has an explicit verification command run against GCP. **Gate:** do not run the destructive cutover (Chunk 6) until Task 16's verification passes.

### Task 14: One-time infra setup script

**Files:**
- Create: `deploy/cloudrun/setup.sh`

- [ ] **Step 1: Write `deploy/cloudrun/setup.sh`** (a documented runbook script; run once from Cloud Shell):

```bash
#!/usr/bin/env bash
# One-time Cloud Run infra for esg-collector. Run from Cloud Shell as the
# project owner. Idempotent where the gcloud verb allows.
set -euo pipefail

PROJECT=gen-lang-client-0020762472
REGION=us-central1
REPO=esg
RUNTIME_SA=esg-collector@${PROJECT}.iam.gserviceaccount.com
DEPLOY_SA=github-actions-deploy@${PROJECT}.iam.gserviceaccount.com
BUCKET=esg-scan-data

gcloud config set project "$PROJECT"

# 1. APIs
gcloud services enable run.googleapis.com artifactregistry.googleapis.com \
  cloudbuild.googleapis.com cloudscheduler.googleapis.com secretmanager.googleapis.com

# 2. Artifact Registry repo
gcloud artifacts repositories create "$REPO" --repository-format=docker \
  --location="$REGION" --description="esg-collector images" || true

# 3. Secrets (paste values when prompted; --data-file=- reads stdin)
for S in BRAVE_API_KEY JINA_API_KEY GROQ_API_KEY; do
  gcloud secrets create "$S" --replication-policy=automatic || true
  echo "Set value for $S then Ctrl-D:"; gcloud secrets versions add "$S" --data-file=-
done

# 4. Runtime SA can read secrets (it already has storage.objectAdmin on the bucket)
for S in BRAVE_API_KEY JINA_API_KEY GROQ_API_KEY; do
  gcloud secrets add-iam-policy-binding "$S" \
    --member="serviceAccount:${RUNTIME_SA}" --role=roles/secretmanager.secretAccessor
done

# 5. Confirm the runtime SA actually has access to THIS bucket (spec open item)
gsutil iam get "gs://${BUCKET}" | grep -A2 "$RUNTIME_SA" || \
  echo "WARN: ${RUNTIME_SA} not found on gs://${BUCKET} — grant roles/storage.objectAdmin"

# 6. Deploy SA roles (swap compute roles for Cloud Run deploy roles)
for ROLE in roles/run.developer roles/artifactregistry.writer roles/iam.serviceAccountUser; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${DEPLOY_SA}" --role="$ROLE"
done

echo "Infra ready. Deploy jobs via the GitHub Actions workflow, then create the schedule (Task 15 step within deploy)."
```

- [ ] **Step 2: Run it** (Cloud Shell) and confirm step 5 does NOT print the WARN line (the SA must have bucket access — resolves the spec's open IAM item).

Expected: APIs enabled, repo + 3 secrets created, no bucket-access WARN.

- [ ] **Step 3: Commit**

```bash
git add esg-collector/deploy/cloudrun/setup.sh
git commit -m "build(cloudrun): one-time infra setup runbook script"
```

---

### Task 15: GitHub Actions — build, push, deploy both jobs

**Files:**
- Create: `.github/workflows/deploy-esg-collector-cloudrun.yml`

- [ ] **Step 1: Write `.github/workflows/deploy-esg-collector-cloudrun.yml`**

```yaml
name: Deploy esg-collector (Cloud Run)

on:
  push:
    branches: [main]
    paths:
      - "esg-collector/**"
      - ".github/workflows/deploy-esg-collector-cloudrun.yml"
  workflow_dispatch: {}

concurrency:
  group: deploy-esg-collector-cloudrun
  cancel-in-progress: false

env:
  PROJECT: gen-lang-client-0020762472
  REGION: us-central1
  IMAGE: us-central1-docker.pkg.dev/gen-lang-client-0020762472/esg/esg-collector
  RUNTIME_SA: esg-collector@gen-lang-client-0020762472.iam.gserviceaccount.com

jobs:
  deploy:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@v4

      - name: Authenticate to GCP
        uses: google-github-actions/auth@v2
        with:
          credentials_json: ${{ secrets.GCP_SA_KEY }}

      - name: Set up gcloud
        uses: google-github-actions/setup-gcloud@v2
        with:
          project_id: gen-lang-client-0020762472

      - name: Run tests
        working-directory: esg-collector
        run: |
          python -m pip install -r requirements.txt pytest
          python -m pytest tests/ -q

      - name: Build & push image (Cloud Build)
        run: |
          gcloud builds submit esg-collector \
            --tag "$IMAGE:${{ github.sha }}"
          gcloud artifacts docker tags add "$IMAGE:${{ github.sha }}" "$IMAGE:latest"

      - name: Deploy daily job
        run: |
          gcloud run jobs deploy esg-daily \
            --image "$IMAGE:${{ github.sha }}" --region "$REGION" \
            --service-account "$RUNTIME_SA" \
            --memory 2Gi --cpu 1 --task-timeout 3600 --max-retries 0 \
            --set-env-vars ESG_DATA_DIR=/tmp/esg-data,LOCK_TTL_SECONDS=7200 \
            --set-secrets BRAVE_API_KEY=BRAVE_API_KEY:latest,JINA_API_KEY=JINA_API_KEY:latest,GROQ_API_KEY=GROQ_API_KEY:latest \
            --args=--mode,daily

      - name: Deploy backfill job
        run: |
          gcloud run jobs deploy esg-backfill \
            --image "$IMAGE:${{ github.sha }}" --region "$REGION" \
            --service-account "$RUNTIME_SA" \
            --memory 4Gi --cpu 1 --task-timeout 86400 --max-retries 0 \
            --set-env-vars ESG_DATA_DIR=/tmp/esg-data,LOCK_TTL_SECONDS=86400 \
            --set-secrets BRAVE_API_KEY=BRAVE_API_KEY:latest,JINA_API_KEY=JINA_API_KEY:latest,GROQ_API_KEY=GROQ_API_KEY:latest \
            --args=--mode,backfill

      - name: Ensure daily schedule exists
        run: |
          gcloud scheduler jobs create http esg-daily-trigger \
            --location "$REGION" --schedule "0 2 * * *" --time-zone UTC \
            --uri "https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT}/jobs/esg-daily:run" \
            --http-method POST \
            --oauth-service-account-email "$RUNTIME_SA" \
            || echo "scheduler job already exists"
```

> Notes: `--task-timeout 3600` (1h) for daily, `86400` (24h) for backfill — both inside the 7-day cap. `--max-retries 0` so a failed run does not silently re-enter and fight the lock. The Scheduler step is create-or-skip (idempotent). The runtime SA needs `roles/run.invoker` on `esg-daily` for the scheduler call — add it in Task 14 setup if the create fails with a permission error.

- [ ] **Step 2: Add `roles/run.invoker` for the scheduler** (append to `setup.sh` or run once):

```bash
gcloud run jobs add-iam-policy-binding esg-daily --region us-central1 \
  --member="serviceAccount:esg-collector@gen-lang-client-0020762472.iam.gserviceaccount.com" \
  --role=roles/run.invoker
```

- [ ] **Step 3: Trigger the workflow** (push to a test branch with `workflow_dispatch`, or merge to main once cutover is ready). Verify the build + both `gcloud run jobs deploy` steps succeed.

Expected: GitHub Actions run green; `gcloud run jobs list --region us-central1` shows `esg-daily` and `esg-backfill`.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/deploy-esg-collector-cloudrun.yml
git commit -m "ci(cloudrun): build+push image, deploy daily/backfill jobs, schedule"
```

---

## Chunk 6: Cutover & decommission

### Task 16: Seed the blob from the VM and verify parity

**Files:**
- Create: `deploy/cloudrun/README.md` (runbook)

- [ ] **Step 1: Write `deploy/cloudrun/README.md`** documenting the cutover sequence (stop VM writers → WAL-checkpoint → seed blob → deploy → verify → decommission), including these exact commands:

```bash
# 1. Stop VM writers (so the DB is quiescent during copy)
gcloud compute ssh esg-collector --zone us-central1-a --tunnel-through-iap --command '
  sudo systemctl stop esg-collector-google esg-collector-baomoi \
    esg-collector-brave esg-collector-body esg-collector-match.timer \
    esg-collector-enrich.timer'

# 2. WAL-checkpoint, then seed the blob from the VM's articles.db
gcloud compute ssh esg-collector --zone us-central1-a --tunnel-through-iap --command '
  /opt/esg-collector/.venv/bin/python -c "import sqlite3; c=sqlite3.connect(\"/opt/esg-collector/esg-collector/data/articles.db\"); c.execute(\"PRAGMA wal_checkpoint(TRUNCATE)\"); c.close()"
  gsutil cp /opt/esg-collector/esg-collector/data/articles.db gs://esg-scan-data/state/articles.db'

# 3. Deploy jobs (merge to main → GitHub Actions) — see Task 15.

# 4. Verify: run the daily job once and confirm it completes + checks the blob back in
gcloud run jobs execute esg-daily --region us-central1 --wait
gcloud storage ls -L gs://esg-scan-data/state/articles.db        # generation advanced
gcloud storage ls gs://esg-scan-data/web/                        # esg_events.json fresh
```

- [ ] **Step 2: Execute steps 1–2** (stop writers, checkpoint, seed). Verify `gs://esg-scan-data/state/articles.db` exists.

Run: `gcloud storage ls -l gs://esg-scan-data/state/articles.db`
Expected: one object, size ≈ the VM DB size.

- [ ] **Step 3: Deploy + run the daily job once** (Task 15 workflow, then `gcloud run jobs execute esg-daily --region us-central1 --wait`).

Expected: execution `Succeeded`; logs show lock acquired → DB downloaded → stages → blob checked in.

- [ ] **Step 4: Parity check.** Confirm the web reads the same data path it already used (`gs://esg-scan-data/web/esg_events.json`) and the new run refreshed it. Spot-check a few tickers' `per_ticker/*.json` count against the pre-cutover VM output.

Expected: `web/esg_events.json` re-uploaded (public), per_ticker counts consistent (≥ pre-cutover, never wiped — daily does not `--rematch-all`).

- [ ] **Step 5: Commit the runbook**

```bash
git add esg-collector/deploy/cloudrun/README.md
git commit -m "docs(cloudrun): cutover + parity-verification runbook"
```

---

### Task 17: Decommission the VM and the old `esg_scan` function

**Files:**
- Modify: `esg-collector/CLAUDE.md` (update deploy notes to the Cloud Run model)
- Modify: deprecate `.github/workflows/deploy-esg-collector.yml`

**Only after Task 16 verification passes and the daily schedule has run cleanly ≥1 cycle.**

- [ ] **Step 1: Disable the old VM deploy workflow.** Rename `.github/workflows/deploy-esg-collector.yml` → `deploy-esg-collector.yml.disabled` (or delete it) so pushes no longer SSH the VM.

- [ ] **Step 2: Stop & delete the VM.**

```bash
gcloud compute instances stop esg-collector --zone us-central1-a
# after a few clean daily cycles:
gcloud compute instances delete esg-collector --zone us-central1-a
```

- [ ] **Step 3: Delete the old `esg_scan` Cloud Function** (project `ta-tracking-api`) once confirmed the web no longer reads its output.

```bash
gcloud functions delete esg_scan --region us-central1 --project ta-tracking-api
```

- [ ] **Step 4: Update `esg-collector/CLAUDE.md`** — replace the "Deploy is automated via IAP SSH" section with the Cloud Run model (push → build image → deploy jobs; backfill is `gcloud run jobs execute esg-backfill`; secrets in Secret Manager; no SSH/systemd).

- [ ] **Step 5: Commit**

```bash
git add esg-collector/CLAUDE.md .github/workflows/
git commit -m "chore(cloudrun): decommission VM + esg_scan; update deploy docs"
```

---

## Done criteria

- `python -m pytest tests/ -v` green in `esg-collector/`.
- `esg-daily` runs on schedule, checks the DB blob in/out under the lock, refreshes `web/*.json`.
- `esg-backfill` runs on manual `gcloud run jobs execute` (with optional `--args=--mode,backfill,--tickers,XXX`), never overlapping daily (lock-guarded).
- No VM, no systemd, no SSH in the deploy path; `git push` → image → jobs.
- Existing collected data preserved (blob seeded from VM; backfill is `INSERT OR IGNORE`).
