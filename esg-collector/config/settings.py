"""Global runtime settings for esg-collector."""
import os
from datetime import datetime, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    _VN = ZoneInfo("Asia/Ho_Chi_Minh")
except Exception:
    _VN = None
_TODAY = (datetime.now(_VN) if _VN else datetime.now(timezone.utc)).date().isoformat()

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
LOGS_DIR = ROOT / "logs"
DB_PATH = DATA_DIR / "articles.db"
PER_TICKER_DIR = DATA_DIR / "per_ticker"
WEB_DIR = DATA_DIR / "web"
ALIASES_DIR = ROOT / "config" / "aliases"
COMPANIES_CSV = ROOT / "config" / "companies.csv"

# Backfill window
BACKFILL_START = "2020-01-01"
BACKFILL_END   = _TODAY
CHUNK_MONTHS   = 1

# Per-backend throttle (seconds, base delay before each call). Jitter ±33%.
THROTTLE = {
    "google_rss": 25.0,
    "baomoi":     15.0,
    "brave":       1.0,
}

# Backoff schedule (seconds) when a backend reports rate-limit/5xx.
BACKOFF = {
    "google_rss": [300, 1800, 7200],     # 5m -> 30m -> 2h
    "baomoi":     [180, 900, 3600],      # 3m -> 15m -> 1h
    "brave":      [60, 600, 3600],       # 1m -> 10m -> 1h
}
MAX_ATTEMPTS = 5

# Brave API window: only fetch for the older portion of the backfill (Báo Mới
# archive runs out around 4y). Brave queries the rest.
BRAVE_WINDOW_START = "2020-01-01"
BRAVE_WINDOW_END   = "2021-12-31"
BAOMOI_WINDOW_START = "2022-01-01"
BAOMOI_WINDOW_END   = _TODAY

BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY", "")
JINA_API_KEY  = os.environ.get("JINA_API_KEY", "")  # optional; raises RPM cap

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)
PER_TICKER_DIR.mkdir(parents=True, exist_ok=True)
WEB_DIR.mkdir(parents=True, exist_ok=True)
