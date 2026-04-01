"""
Write classified ESG events to Google Cloud Storage as JSON.
Reads existing data, merges new events (dedup), writes back.
"""

import hashlib
import json
import os
from datetime import datetime, timezone
from google.cloud import storage


BUCKET_NAME = os.environ.get("GCS_BUCKET", "esg-risk-dashboard")
EVENTS_FILE = "esg_events.json"
SCANS_FILE = "esg_scans.json"
TICKERS_FILE = "esg_tickers.json"  # tracks last scan date per ticker


def get_bucket():
    client = storage.Client()
    return client.bucket(BUCKET_NAME)


def read_json(filename):
    """Read JSON file from GCS. Returns [] if not found."""
    bucket = get_bucket()
    blob = bucket.blob(filename)
    if not blob.exists():
        return []
    data = blob.download_as_text(encoding="utf-8")
    return json.loads(data)


def write_json(filename, data):
    """Write JSON to GCS with public read access."""
    bucket = get_bucket()
    blob = bucket.blob(filename)
    blob.upload_from_string(
        json.dumps(data, ensure_ascii=False, indent=2),
        content_type="application/json; charset=utf-8",
    )


def event_hash(ticker, date, summary):
    """Create a hash for dedup."""
    key = f"{ticker}|{date}|{summary}"
    return hashlib.md5(key.encode("utf-8")).hexdigest()


def write_events(company, ticker, events):
    """
    Merge new ESG events into existing JSON on GCS. Skip duplicates.
    Returns count of new events written.
    """
    existing = read_json(EVENTS_FILE)
    existing_hashes = {
        event_hash(e["ticker"], e["date"], e["summary"])
        for e in existing
    }

    new_count = 0
    now = datetime.now(timezone.utc).isoformat()

    for evt in events:
        h = event_hash(ticker, evt.get("date", ""), evt.get("summary", ""))
        if h in existing_hashes:
            continue

        existing.append({
            "ticker": ticker,
            "company": company,
            "type": evt.get("type", ""),
            "date": evt.get("date", ""),
            "summary": evt.get("summary", ""),
            "severity": evt.get("severity", ""),
            "source": evt.get("source", ""),
            "url": evt.get("url", ""),
            "created_at": now,
        })
        existing_hashes.add(h)
        new_count += 1

    if new_count > 0:
        # Sort by date descending
        existing.sort(key=lambda x: x.get("date", ""), reverse=True)
        write_json(EVENTS_FILE, existing)

    return new_count


def get_last_scan_date(ticker):
    """Get the last scan date for a ticker.
    Returns date string 'YYYY-MM-DD' or None if never scanned.
    Checks both: last event date AND ticker scan history.
    """
    # Check ticker scan history first
    tickers_data = read_json(TICKERS_FILE)
    ticker_info = {t["ticker"]: t for t in tickers_data}
    if ticker in ticker_info:
        return ticker_info[ticker].get("last_scan_date")

    # Fallback: check events
    existing = read_json(EVENTS_FILE)
    dates = [e["date"] for e in existing if e.get("ticker") == ticker and e.get("date")]
    if dates:
        return max(dates)

    return None


def mark_ticker_scanned(ticker, company, scan_date):
    """Record that a ticker has been scanned up to scan_date."""
    tickers_data = read_json(TICKERS_FILE)
    ticker_map = {t["ticker"]: t for t in tickers_data}

    ticker_map[ticker] = {
        "ticker": ticker,
        "company": company,
        "last_scan_date": scan_date,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
    }

    write_json(TICKERS_FILE, list(ticker_map.values()))


def get_next_batch(total_companies):
    """Get next batch number to process. Auto-rotates through all batches.
    Stored in GCS as simple JSON: {"next_batch": 1}
    """
    bucket = get_bucket()
    blob = bucket.blob("esg_batch_counter.json")
    if blob.exists():
        data = json.loads(blob.download_as_text(encoding="utf-8"))
        next_batch = data.get("next_batch", 1)
    else:
        next_batch = 1

    max_batch = (total_companies + BATCH_SIZE - 1) // BATCH_SIZE
    if next_batch > max_batch:
        next_batch = 1

    return next_batch


def advance_batch(total_companies):
    """Increment batch counter for next run."""
    current = get_next_batch(total_companies)
    max_batch = (total_companies + BATCH_SIZE - 1) // BATCH_SIZE
    next_val = current + 1 if current < max_batch else 1

    bucket = get_bucket()
    blob = bucket.blob("esg_batch_counter.json")
    blob.upload_from_string(
        json.dumps({"next_batch": next_val}),
        content_type="application/json",
    )
    return current


BATCH_SIZE = 5


def write_scan_log(tickers_scanned, new_events_found, status="completed", error_msg=""):
    """Append scan result to scans log."""
    scans = read_json(SCANS_FILE)
    scans.append({
        "scan_date": datetime.now(timezone.utc).isoformat(),
        "tickers_scanned": tickers_scanned,
        "new_events_found": new_events_found,
        "status": status,
        "error": error_msg,
    })
    # Keep last 100 scans
    scans = scans[-100:]
    write_json(SCANS_FILE, scans)
