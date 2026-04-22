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
from translator import translate_summaries
from storage_writer import (
    write_events, write_scan_log, get_last_scan_date,
    mark_ticker_scanned, get_next_batch, advance_batch,
)


BATCH_SIZE = 5


def _scan_companies(companies, api_key):
    """Scan a dict of {ticker: company_name}. Returns (total_new, results)."""
    total_new = 0
    results = []
    today = datetime.now().strftime("%Y-%m-%d")

    for ticker, company_name in companies.items():
        print(f"\nProcessing {ticker} ({company_name})...")

        last_date = get_last_scan_date(ticker)
        if last_date:
            days_back = (datetime.now() - datetime.strptime(last_date, "%Y-%m-%d")).days + 1
            days_back = max(days_back, 1)
            scan_mode = f"update (from {last_date}, {days_back} days)"
        else:
            days_back = 1825
            scan_mode = "batch 5 years"

        print(f"  Mode: {scan_mode}")

        items = fetch_company_news(company_name, days_back=days_back, delay=1.0)
        print(f"  RSS: {len(items)} items")

        if not items:
            mark_ticker_scanned(ticker, company_name, today)
            results.append({"ticker": ticker, "mode": scan_mode, "rss": 0, "events": 0, "new": 0})
            print(f"  No RSS results. Marked as scanned.")
            continue

        items = resolve_links(items)
        events = classify_news(company_name, ticker, items, api_key)
        print(f"  Classified: {len(events)} ESG events")

        if events:
            summaries_en = translate_summaries([e["summary"] for e in events], api_key)
            for e, en in zip(events, summaries_en):
                e["summary_en"] = en

        new_count = write_events(company_name, ticker, events)
        total_new += new_count
        print(f"  New events written: {new_count}")

        mark_ticker_scanned(ticker, company_name, today)
        results.append({"ticker": ticker, "mode": scan_mode, "rss": len(items), "events": len(events), "new": new_count})

    return total_new, results


@functions_framework.http
def esg_scan(request):
    """HTTP Cloud Function entry point."""
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

    args = request.args or {}
    mode = args.get("mode", "")
    tickers_param = args.get("tickers", "")

    all_companies = load_companies(os.path.join(os.path.dirname(__file__), "Top100.csv"))
    if not all_companies:
        return json.dumps({"error": "No companies found in CSV"}), 500, headers

    api_key = os.environ.get("GEMINI_API_KEY", "")

    if tickers_param:
        # Manual: specific tickers
        ticker_list = [t.strip().upper() for t in tickers_param.split(",")]
        companies = {k: v for k, v in all_companies.items() if k.upper() in ticker_list}
        if not companies:
            return json.dumps({"error": f"Tickers not found: {tickers_param}"}), 404, headers

        print(f"=== ESG Scan: manual tickers={list(companies.keys())} ===")
        total_new, results = _scan_companies(companies, api_key)

        write_scan_log(tickers_scanned=len(companies), new_events_found=total_new)
        response = {
            "status": "completed",
            "tickers_scanned": len(companies),
            "new_events": total_new,
            "details": results,
        }
        return json.dumps(response, ensure_ascii=False), 200, headers

    elif mode == "auto":
        # Auto: loop through ALL batches sequentially in one request
        max_batch = (len(all_companies) + BATCH_SIZE - 1) // BATCH_SIZE
        all_tickers = list(all_companies.keys())
        all_results = []
        grand_total = 0

        print(f"=== ESG Scan: auto mode, {max_batch} batches, {len(all_companies)} companies ===")

        for b in range(1, max_batch + 1):
            start = (b - 1) * BATCH_SIZE
            end = start + BATCH_SIZE
            batch_companies = {k: all_companies[k] for k in all_tickers[start:end] if k in all_companies}

            print(f"\n--- Batch {b}/{max_batch}: {list(batch_companies.keys())} ---")
            total_new, results = _scan_companies(batch_companies, api_key)
            grand_total += total_new
            all_results.extend(results)

            write_scan_log(tickers_scanned=len(batch_companies), new_events_found=total_new)

        response = {
            "status": "completed",
            "batches": max_batch,
            "tickers_scanned": len(all_companies),
            "new_events": grand_total,
            "details": all_results,
        }
        return json.dumps(response, ensure_ascii=False), 200, headers

    else:
        # Manual: specific batch number
        batch = int(args.get("batch", 1))
        all_tickers = list(all_companies.keys())
        start = (batch - 1) * BATCH_SIZE
        end = start + BATCH_SIZE
        companies = {k: all_companies[k] for k in all_tickers[start:end] if k in all_companies}

        if not companies:
            return json.dumps({"error": f"No companies in batch {batch}"}), 404, headers

        print(f"=== ESG Scan: batch={batch}, companies={list(companies.keys())} ===")
        total_new, results = _scan_companies(companies, api_key)

        write_scan_log(tickers_scanned=len(companies), new_events_found=total_new)
        response = {
            "status": "completed",
            "batch": batch,
            "tickers_scanned": len(companies),
            "new_events": total_new,
            "details": results,
        }
        return json.dumps(response, ensure_ascii=False), 200, headers
