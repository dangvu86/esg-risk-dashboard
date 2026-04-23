"""One-time backfill: classify controversy level for existing events
with severity == "Cao" that lack controversy_level.

Usage:
    python backfill_controversy.py              # default: sample 20 (local only)
    python backfill_controversy.py --sample 20  # sample N events, write local
    python backfill_controversy.py --full       # process all todo, upload to GCS

Sample mode writes to _sample_controversy.json locally for manual review.
Full mode requires explicit --full flag to upload results back to GCS.

Requires:
- gcloud auth (active account with access to bucket)
- GEMINI_API_KEY in .env
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from controversy_classifier import classify_events
from rss_fetcher import load_revenues

sys.stdout.reconfigure(encoding="utf-8")

BUCKET = "esg-risk-dashboard"
FILE = "esg_events.json"
TMP = Path(__file__).parent / "_backfill_controversy.json"
SAMPLE_OUT = Path(__file__).parent / "_sample_controversy.json"
CSV_PATH = Path(__file__).parent / "Top100.csv"


def load_env():
    p = Path(__file__).parent / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def gcs_download():
    print(f"Downloading gs://{BUCKET}/{FILE} ...")
    subprocess.run(
        ["gcloud", "storage", "cp", f"gs://{BUCKET}/{FILE}", str(TMP)],
        check=True, shell=True,
    )


def gcs_upload():
    print(f"Uploading to gs://{BUCKET}/{FILE} ...")
    subprocess.run(
        ["gcloud", "storage", "cp", str(TMP), f"gs://{BUCKET}/{FILE}",
         "--content-type=application/json; charset=utf-8"],
        check=True, shell=True,
    )


def apply_classifications(events, todo_idx, classifications):
    """Mutate events in place. Returns (success, fail, level_counts)."""
    now = datetime.now(timezone.utc).isoformat()
    success = 0
    fail = 0
    level_counts = {"Major": 0, "Minor": 0, "No": 0}

    for i, cls in zip(todo_idx, classifications):
        if cls is None:
            fail += 1
            continue
        events[i]["controversy_level"] = cls["level"]
        events[i]["controversy_justification"] = cls["justification"]
        events[i]["controversy_classified_at"] = now
        level_counts[cls["level"]] += 1
        success += 1

    return success, fail, level_counts


def build_sample_output(events, todo_idx, classifications):
    """Build review-friendly output: pair each event with its classification."""
    sample = []
    for i, cls in zip(todo_idx, classifications):
        evt = events[i]
        sample.append({
            "ticker": evt.get("ticker"),
            "company": evt.get("company"),
            "type": evt.get("type"),
            "date": evt.get("date"),
            "summary": evt.get("summary"),
            "summary_en": evt.get("summary_en"),
            "source": evt.get("source"),
            "url": evt.get("url"),
            "severity": evt.get("severity"),
            "classification": cls,  # None if failed
        })
    return sample


def main():
    parser = argparse.ArgumentParser(description="Backfill controversy classification")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--sample", type=int, default=None,
                       help="Process N events, write local file, no GCS upload")
    group.add_argument("--full", action="store_true",
                       help="Process all todo events, upload to GCS")
    args = parser.parse_args()

    # Default behavior: sample 20 (safety)
    if not args.full and args.sample is None:
        args.sample = 20

    load_env()
    if not os.environ.get("GEMINI_API_KEY"):
        print("ERROR: GEMINI_API_KEY missing in .env")
        sys.exit(1)

    gcs_download()
    events = json.loads(TMP.read_text(encoding="utf-8"))
    print(f"Loaded {len(events)} events")

    todo_idx = [
        i for i, e in enumerate(events)
        if e.get("severity") == "Cao" and not e.get("controversy_level")
    ]
    print(f"Cao events without classification: {len(todo_idx)}")

    if not todo_idx:
        print("Nothing to backfill.")
        TMP.unlink(missing_ok=True)
        return

    if args.sample is not None:
        todo_idx = todo_idx[:args.sample]
        print(f"SAMPLE mode: processing first {len(todo_idx)} events (no GCS upload)")
    else:
        print(f"FULL mode: processing all {len(todo_idx)} events (will upload to GCS)")

    todo_events = [events[i] for i in todo_idx]

    revenues = load_revenues(str(CSV_PATH))
    print(f"Loaded revenues for {len(revenues)} tickers from {CSV_PATH.name}")

    classifications = classify_events(todo_events, revenues=revenues)

    success, fail, level_counts = apply_classifications(events, todo_idx, classifications)
    print("\n--- Summary ---")
    print(f"Processed: {len(todo_idx)}")
    print(f"Success:   {success}")
    print(f"Failed:    {fail}")
    print(f"Levels:    Major={level_counts['Major']}  Minor={level_counts['Minor']}  No={level_counts['No']}")

    if args.sample is not None:
        sample = build_sample_output(events, todo_idx, classifications)
        SAMPLE_OUT.write_text(json.dumps(sample, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nSample written to: {SAMPLE_OUT}")
        print("Review this file, then run with --full to backfill all.")
        TMP.unlink(missing_ok=True)
        return

    # Full mode: upload back
    TMP.write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")
    gcs_upload()
    TMP.unlink()
    print("Done.")


if __name__ == "__main__":
    main()
