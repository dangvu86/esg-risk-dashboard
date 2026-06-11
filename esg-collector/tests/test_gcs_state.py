import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from google.api_core.exceptions import PreconditionFailed
from runtime import gcs, gcs_state
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


def test_download_per_ticker_restores_only_json_under_prefix(tmp_path):
    bucket = FakeBucket()
    gcs.upload_text(bucket, "per_ticker/AAA.json", '{"ticker":"AAA"}')
    gcs.upload_text(bucket, "per_ticker/BBB.json", '{"ticker":"BBB"}')
    gcs.upload_text(bucket, "state/articles.db", "not-a-per-ticker")  # other prefix
    gcs.upload_text(bucket, "per_ticker/_index.txt", "skip-me")        # not .json
    dest = tmp_path / "per_ticker"
    n = gcs_state.download_per_ticker(bucket, dest)
    assert n == 2
    assert (dest / "AAA.json").read_text(encoding="utf-8") == '{"ticker":"AAA"}'
    assert (dest / "BBB.json").read_text(encoding="utf-8") == '{"ticker":"BBB"}'
    assert not (dest / "articles.db").exists()


def test_download_per_ticker_empty_bucket_is_noop(tmp_path):
    bucket = FakeBucket()
    dest = tmp_path / "per_ticker"
    assert gcs_state.download_per_ticker(bucket, dest) == 0
    assert dest.exists()  # dir created even when nothing to restore
