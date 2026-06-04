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
