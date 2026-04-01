"""
Cloud Function entry point for ESG RSS scan pipeline.

Query params:
  ?mode=auto         — auto-rotate through batches (for Cloud Scheduler)
  ?batch=1..N        — manual: which batch of 5 companies
  ?tickers=HPG,VNM   — manual: scan specific tickers only

Logic (same as ESG skill):
  - Company NEVER scanned → 5-year historical scan (batch mode)
  - Company ALREADY scanned → scan from last scan date to today (update mode)
  - 0 events found → still mark as scanned
"""

import json
import os
from datetime import datetime
import functions_framework
from rss_fetcher import load_companies, fetch_company_news
from link_resolver import resolve_links
from keyword_classifier import classify_news
from storage_writer import (
    write_events, write_scan_log, get_last_scan_date,
    mark_ticker_scanned, get_next_batch, advance_batch,
)


BATCH_SIZE = 5


@functions_framework.http
def esg_scan(request):
    """HTTP Cloud Function entry point."""
    # CORS
    if request.method == "OPTIONS":
        return ("", 204, {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST",
            "Access-Control-Allow-Headers": "Content-Type",
        })

    headers = {
        "Access-Control-Allow-Origin": "*",
        "Content-Type": "application/json; charset=utf-8",
    }

    # Parse params
    args = request.args or {}
    mode = args.get("mode", "")
    tickers_param = args.get("tickers", "")

    # Load companies
    companies = load_companies(os.path.join(os.path.dirname(__file__), "Top100.csv"))
    if not companies:
        return json.dumps({"error": "No companies found in CSV"}), 500, headers

    # Determine which companies to scan
    if tickers_param:
        # Manual: specific tickers
        ticker_list = [t.strip().upper() for t in tickers_param.split(",")]
        companies = {k: v for k, v in companies.items() if k.upper() in ticker_list}
        batch = 0
    elif mode == "auto":
        # Auto: get next batch from counter, then advance
        batch = advance_batch(len(companies))
        all_tickers = list(companies.keys())
        start = (batch - 1) * BATCH_SIZE
        end = start + BATCH_SIZE
        companies = {k: companies[k] for k in all_tickers[start:end] if k in companies}
    else:
        # Manual: specific batch number
        batch = int(args.get("batch", 1))
        all_tickers = list(companies.keys())
        start = (batch - 1) * BATCH_SIZE
        end = start + BATCH_SIZE
        companies = {k: companies[k] for k in all_tickers[start:end] if k in companies}

    if not companies:
        return json.dumps({"error": f"No companies in batch {batch}"}), 404, headers

    api_key = os.environ.get("GEMINI_API_KEY", "")
    total_new = 0
    results = []
    today = datetime.now().strftime("%Y-%m-%d")

    print(f"=== ESG Scan: mode={mode or 'manual'}, batch={batch}, companies={list(companies.keys())} ===")

    for ticker, company_name in companies.items():
        print(f"\nProcessing {ticker} ({company_name})...")

        # Determine scan range
        last_date = get_last_scan_date(ticker)
        if last_date:
            days_back = (datetime.now() - datetime.strptime(last_date, "%Y-%m-%d")).days + 1
            days_back = max(days_back, 1)
            scan_mode = f"update (from {last_date}, {days_back} days)"
        else:
            days_back = 1825  # 5 years
            scan_mode = "batch 5 years"

        print(f"  Mode: {scan_mode}")

        # Step 1: Fetch RSS
        items = fetch_company_news(company_name, days_back=days_back, delay=1.0)
        print(f"  RSS: {len(items)} items")

        if not items:
            mark_ticker_scanned(ticker, company_name, today)
            results.append({"ticker": ticker, "mode": scan_mode, "rss": 0, "events": 0, "new": 0})
            print(f"  No RSS results. Marked as scanned.")
            continue

        # Step 2: Pass through links
        items = resolve_links(items)

        # Step 3: Classify with Gemini
        events = classify_news(company_name, ticker, items, api_key)
        print(f"  Classified: {len(events)} ESG events")

        # Step 4: Write to Cloud Storage
        new_count = write_events(company_name, ticker, events)
        total_new += new_count
        print(f"  New events written: {new_count}")

        mark_ticker_scanned(ticker, company_name, today)
        results.append({"ticker": ticker, "mode": scan_mode, "rss": len(items), "events": len(events), "new": new_count})

    # Log scan
    write_scan_log(
        tickers_scanned=len(companies),
        new_events_found=total_new,
        status="completed",
    )

    response = {
        "status": "completed",
        "batch": batch,
        "tickers_scanned": len(companies),
        "new_events": total_new,
        "details": results,
    }
    return json.dumps(response, ensure_ascii=False), 200, headers
