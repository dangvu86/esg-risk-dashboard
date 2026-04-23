"""
Semantic dedup (Layer B): catches same-story events that differ beyond
string normalization — e.g., same incident covered by several outlets with
slightly different wording across days.

Runs AFTER Layer A (normalize_title hash in storage_writer.event_hash).

Approach:
  1. Bucket events by ticker (cheap; different tickers can't be same story).
  2. For each ticker bucket with >=2 events, group events into 30-day windows.
  3. If a window has >=2 events, ask LLM to cluster them by incident.
  4. Keep the earliest event from each cluster, drop the rest.

Single LLM call per ticker-window, batched with all events in that window.
Quota: typically 0-10 calls/week since only active tickers need clustering.
"""

import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta

from controversy_classifier import resolve_provider, _build_request


WINDOW_DAYS = 30
MIN_EVENTS_IN_WINDOW = 2

CLUSTER_PROMPT = """You are deduplicating Vietnamese ESG news events for a single company.

Below are {n} event titles from the same ticker, within a {days}-day window. Some titles describe THE SAME underlying incident (republished by multiple outlets or updated over several days); others are genuinely distinct events.

Group each event into clusters where all events in a cluster describe the SAME underlying story/incident. Use cluster id 1, 2, 3, ... Each event belongs to exactly one cluster.

Be strict: only cluster events that share the SAME specific incident (same people, same action, same subject).
- Same: "3 HĐQT bị khởi tố" on 2026-04-20 (outlet A) and 2026-04-18 (outlet B) → same cluster
- Different: "Phạt 500M vì xả thải" and "Phạt 1 tỷ vì xả thải sau đó tiếp tục vi phạm" → different clusters
- Different: two separate tax violations on different dates → different clusters even if wording similar

Events (index. title):
{events}

Return a JSON object with key "clusters" containing an array of integers (same length as input), where each integer is the cluster id for that event. Only the JSON, no explanation.
"""


def _call_llm(provider, prompt, retries=3):
    url, payload, headers, extract = _build_request(provider, prompt)
    label = f"{provider['name']}/{provider['model']}"

    for attempt in range(retries):
        req = urllib.request.Request(url, data=payload, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            return json.loads(extract(result))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if e.code in (429, 503) and attempt < retries - 1:
                wait = 30 * (attempt + 1)
                print(f"  Semantic {label} {e.code}, retry in {wait}s...")
                time.sleep(wait)
                continue
            print(f"  Semantic {label} API error {e.code}: {body[:200]}")
            return None
        except Exception as e:
            if attempt < retries - 1:
                print(f"  Semantic {label} error: {e}, retry in 10s...")
                time.sleep(10)
                continue
            print(f"  Semantic {label} failed: {type(e).__name__}: {e}")
            return None
    return None


def _parse_date(s):
    try:
        return datetime.strptime((s or "")[:10], "%Y-%m-%d")
    except Exception:
        return None


def _extract_clusters(parsed, expected_len):
    if isinstance(parsed, list):
        lst = parsed
    elif isinstance(parsed, dict):
        lst = parsed.get("clusters") or next(
            (v for v in parsed.values() if isinstance(v, list)), None)
    else:
        return None
    if not isinstance(lst, list) or len(lst) != expected_len:
        return None
    out = []
    for v in lst:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            return None
    return out


def _group_windows(events_with_idx):
    """events_with_idx: list of (orig_idx, event). Sort by date, produce sliding
    windows each of length WINDOW_DAYS. Events within a window are candidates
    for clustering together. Returns list of lists of (orig_idx, event).
    """
    dated = [(i, e, _parse_date(e.get("date"))) for i, e in events_with_idx]
    dated = [x for x in dated if x[2] is not None]
    dated.sort(key=lambda x: x[2])

    windows = []
    if not dated:
        return windows

    current = [dated[0]]
    for x in dated[1:]:
        if (x[2] - current[0][2]).days <= WINDOW_DAYS:
            current.append(x)
        else:
            if len(current) >= MIN_EVENTS_IN_WINDOW:
                windows.append([(i, e) for i, e, _ in current])
            current = [x]
    if len(current) >= MIN_EVENTS_IN_WINDOW:
        windows.append([(i, e) for i, e, _ in current])
    return windows


def dedupe_semantic(events, provider=None):
    """Return a deduped copy of events. Keeps the earliest event from each
    LLM-identified cluster; drops the rest. Events that can't be LLM-clustered
    (provider missing, call fails) are kept as-is.
    """
    if not events:
        return []

    provider = provider or resolve_provider()
    if not provider:
        print("  Semantic dedup: no LLM provider configured, skipping")
        return list(events)

    # Bucket by ticker
    by_ticker = {}
    for i, e in enumerate(events):
        by_ticker.setdefault(e.get("ticker", ""), []).append((i, e))

    drop = set()
    sleep_s = provider["sleep"]
    calls = 0

    for ticker, items in by_ticker.items():
        if len(items) < MIN_EVENTS_IN_WINDOW:
            continue
        windows = _group_windows(items)
        for window in windows:
            n = len(window)
            events_text = "\n".join(
                f"{k+1}. {w[1].get('summary','')}" for k, w in enumerate(window)
            )
            prompt = CLUSTER_PROMPT.format(n=n, days=WINDOW_DAYS, events=events_text)

            print(f"  Semantic: {ticker} window of {n} events ...")
            if calls > 0:
                time.sleep(sleep_s)
            parsed = _call_llm(provider, prompt)
            calls += 1
            clusters = _extract_clusters(parsed, n) if parsed else None
            if clusters is None:
                print(f"    cluster parse failed, keeping all {n}")
                continue

            # For each cluster, keep earliest event (by date); drop others
            cluster_map = {}  # cid -> list of (orig_idx, event)
            for (orig_idx, e), cid in zip(window, clusters):
                cluster_map.setdefault(cid, []).append((orig_idx, e))

            for cid, group in cluster_map.items():
                if len(group) < 2:
                    continue
                group.sort(key=lambda x: (x[1].get("date") or "9999"))
                keeper = group[0]
                losers = group[1:]
                for orig_idx, e in losers:
                    drop.add(orig_idx)
                    print(f"    DROP [{ticker}] {e.get('date')}: {e.get('summary','')[:80]}")
                print(f"    KEEP [{ticker}] {keeper[1].get('date')}: {keeper[1].get('summary','')[:80]}  (cluster of {len(group)})")

    kept = [e for i, e in enumerate(events) if i not in drop]
    print(f"  Semantic dedup: {len(kept)}/{len(events)} events kept (dropped {len(drop)}); {calls} LLM calls")
    return kept
