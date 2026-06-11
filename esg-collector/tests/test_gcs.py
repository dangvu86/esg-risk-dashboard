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
