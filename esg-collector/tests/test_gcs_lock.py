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
