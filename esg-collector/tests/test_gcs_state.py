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
