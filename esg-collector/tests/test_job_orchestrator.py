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
    # two export stages so the raw_esg NDJSON actually uploads (see job.py note)
    assert any("pipeline.export --ndjson --upload" in c for c in cmds)
    assert any("pipeline.export --web --upload" in c for c in cmds)
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


def test_run_acquires_does_stages_checks_in_and_releases(monkeypatch, tmp_path):
    import importlib
    monkeypatch.setenv("ESG_DATA_DIR", str(tmp_path))
    from config import settings as s; importlib.reload(s)
    from core import storage; importlib.reload(storage)
    importlib.reload(job)  # rebind job.settings/job.storage to the reloaded modules

    from tests._fake_gcs import FakeBucket
    bucket = FakeBucket()
    ran = []
    # pin the clock so lock acquire/refresh/release are deterministic regardless
    # of wall time (each refresh stays well within the TTL window)
    monkeypatch.setattr(job, "_now", lambda: "2026-06-04T00:00:00Z")
    monkeypatch.setattr(job, "_run_stage", lambda cmd, env: ran.append(cmd))
    monkeypatch.setattr(job, "_run_fetch_concurrently", lambda cmds, env: ran.append("fetch"))

    rc = job.run("daily", None, ttl_seconds=3600, bucket=bucket)
    assert rc == 0
    assert "fetch" in ran                      # fetch stage ran
    assert "state/articles.db" in bucket._store  # DB checked in
    assert "state/pipeline.lock" not in bucket._store  # lock released
    importlib.reload(s); importlib.reload(storage); importlib.reload(job)


def test_run_skips_when_lock_already_held(monkeypatch, tmp_path):
    import importlib
    monkeypatch.setenv("ESG_DATA_DIR", str(tmp_path))
    from config import settings as s; importlib.reload(s)
    from core import storage; importlib.reload(storage)
    importlib.reload(job)
    from tests._fake_gcs import FakeBucket
    from runtime import gcs_lock
    bucket = FakeBucket()
    gcs_lock.acquire(bucket, owner="other", mode="daily",
                     now="2026-06-04T00:00:00Z", ttl_seconds=3600)  # fresh lock held
    # pin the clock inside the held lock's TTL window so our acquire sees it as
    # live (not stale) regardless of wall time → run must skip
    monkeypatch.setattr(job, "_now", lambda: "2026-06-04T00:10:00Z")
    ran = []
    monkeypatch.setattr(job, "_run_stage", lambda cmd, env: ran.append(cmd))
    monkeypatch.setattr(job, "_run_fetch_concurrently", lambda cmds, env: ran.append("fetch"))
    rc = job.run("daily", None, ttl_seconds=3600, bucket=bucket)
    assert rc == 0
    assert ran == []                            # nothing ran — skipped
    importlib.reload(s); importlib.reload(storage); importlib.reload(job)
