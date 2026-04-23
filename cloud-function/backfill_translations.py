"""One-time backfill: translate existing events on GCS that lack summary_en.

Workflow:
1. Download esg_events.json from GCS via gcloud storage cp
2. Translate events missing summary_en (batched 30 per request)
3. Upload back to GCS

Requires:
- gcloud auth (active account with access to bucket)
- GEMINI_API_KEY in .env
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from translator import translate_summaries

sys.stdout.reconfigure(encoding="utf-8")

BUCKET = "esg-risk-dashboard"
FILE = "esg_events.json"
TMP = Path(__file__).parent / "_backfill_events.json"


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


def main():
    load_env()
    if not os.environ.get("GEMINI_API_KEY"):
        print("ERROR: GEMINI_API_KEY missing in .env")
        sys.exit(1)

    gcs_download()
    events = json.loads(TMP.read_text(encoding="utf-8"))
    print(f"Loaded {len(events)} events")

    todo_idx = [i for i, e in enumerate(events) if not e.get("summary_en")]
    print(f"Need translation: {len(todo_idx)}")

    if not todo_idx:
        print("Nothing to backfill.")
        return

    summaries = [events[i]["summary"] for i in todo_idx]
    translated = translate_summaries(summaries)

    written = 0
    for i, en in zip(todo_idx, translated):
        if en and en != events[i]["summary"]:
            events[i]["summary_en"] = en
            written += 1
    print(f"Wrote {written} translations (skipped {len(todo_idx) - written} unchanged/failed)")

    TMP.write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")
    gcs_upload()
    TMP.unlink()
    print("Done.")


if __name__ == "__main__":
    main()
