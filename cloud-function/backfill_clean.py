"""One-time backfill: run sentiment filter + Layer A dedup + Layer B dedup
on the existing GCS events. Used after adding these features to clean up
historical data that slipped through.

Usage:
    python backfill_clean.py --dry-run        # compute diff, write local, no upload
    python backfill_clean.py --apply          # apply + upload to GCS

Safe by default: requires --apply to upload.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from sentiment_filter import filter_negative
from semantic_dedup import dedupe_semantic
from storage_writer import event_hash

sys.stdout.reconfigure(encoding="utf-8")

BUCKET = "esg-risk-dashboard"
FILE = "esg_events.json"
TMP = Path(__file__).parent / "_backfill_clean.json"
OUT = Path(__file__).parent / "_backfill_clean_preview.json"


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


def layer_a_dedup(events):
    """Collapse events with identical normalized hash (Layer A).
    Keep the event with the earliest date (original report wins)."""
    by_hash = {}
    for e in events:
        h = event_hash(e.get("ticker", ""), e.get("date", ""), e.get("summary", ""))
        existing = by_hash.get(h)
        if existing is None:
            by_hash[h] = e
        else:
            # keep earlier date
            if (e.get("date") or "9999") < (existing.get("date") or "9999"):
                by_hash[h] = e
    return list(by_hash.values())


def main():
    parser = argparse.ArgumentParser(description="Backfill sentiment + dedup")
    parser.add_argument("--apply", action="store_true", help="Upload cleaned data to GCS")
    args = parser.parse_args()

    load_env()
    gcs_download()
    events = json.loads(TMP.read_text(encoding="utf-8"))
    n0 = len(events)
    print(f"Loaded {n0} events")

    # Layer A: normalize hash dedup (cheap, 0 quota)
    a = layer_a_dedup(events)
    n1 = len(a)
    print(f"\nLayer A (normalize hash): {n0} → {n1}  (dropped {n0 - n1})")

    # Sentiment filter: drop positive/neutral (batched LLM)
    b = filter_negative(a)
    n2 = len(b)
    print(f"\nSentiment filter:  {n1} → {n2}  (dropped {n1 - n2})")

    # Layer B: semantic cluster dedup (LLM, per-ticker windows)
    c = dedupe_semantic(b)
    n3 = len(c)
    print(f"\nLayer B (semantic): {n2} → {n3}  (dropped {n2 - n3})")

    # sort by date desc, same as storage_writer
    c.sort(key=lambda x: x.get("date", ""), reverse=True)

    print(f"\n=== SUMMARY ===")
    print(f"Input:        {n0}")
    print(f"Layer A out:  {n1}  (-{n0-n1})")
    print(f"Sentiment out:{n2}  (-{n1-n2})")
    print(f"Layer B out:  {n3}  (-{n2-n3})")
    print(f"Total dropped:{n0-n3}")

    OUT.write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nPreview written to {OUT}")

    if not args.apply:
        print("Dry-run mode — no upload. Rerun with --apply to push to GCS.")
        TMP.unlink(missing_ok=True)
        return

    TMP.write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")
    gcs_upload()
    TMP.unlink()
    print("Done.")


if __name__ == "__main__":
    main()
