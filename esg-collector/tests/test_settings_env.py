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
