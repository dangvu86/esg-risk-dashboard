# ESG Enrich Pipeline — Design Spec

**Date:** 2026-06-03
**Status:** Approved (pending spec review)
**Scope:** Port the 3 LLM stages of the old `cloud-function/` pipeline (sentiment filter →
title translation → controversy classification) into the new `esg-collector/`, then export a
web-shaped `esg_events.json` and repoint the web at it.

## Goal

The new `esg-collector` already does collection (multi-source) and rule-based E/S/G + severity
classification, but lacks the three LLM stages the web depends on. This spec adds an **`enrich`
stage** to esg-collector that runs those three stages on the VM, writes results into the SQLite
`articles.db`, and exports `esg_events.json` (the shape the web reads) to a public path in
`gs://esg-scan-data`. The web's two API routes are repointed to the new bucket.

After this ships, the new pipeline produces everything the web needs and the old cloud-function
becomes redundant — but **retiring the old cloud-function/bucket is a follow-up, not this spec.**

## Locked scope decisions

| Decision | Choice |
|---|---|
| Where enrichment runs | **New systemd `enrich` timer on the VM** (not Cloud Function, not manual batch) |
| Controversy depth | **Full parity with old** — severity=Cao only, body + revenue + E&S/CG definitions + 20% rule |
| Translation scope | **Display title only** (`summary` → `summary_en`). No body/snippet translation. |
| Output location | **`gs://esg-scan-data`** (new bucket), web prefix `web/`; repoint web's 2 API routes |
| LLM provider | **Reuse old registry** — default Groq `meta-llama/llama-4-scout-17b-16e-instruct`, swap via `.env` |

## Background: the two pipelines

- **OLD** (`cloud-function/`): Google-RSS only → keyword classify → **sentiment (LLM)** →
  **translate (LLM)** → **controversy (LLM, Cao only)** → hash-dedup write to
  `gs://esg-risk-dashboard/esg_events.json`. The web reads this bucket today.
- **NEW** (`esg-collector/`): VM + SQLite + 4 workers (Google RSS + BaoMoi + Brave + body/Jina),
  daily queue, `match.timer` runs `esg_filter.classify()` (E/S/G type — tie-break already fixed
  to prefer E/S over generic-governance — + severity), exports `per_ticker/*.json` + `raw_esg`
  NDJSON to `gs://esg-scan-data`. No LLM stages. The web does NOT read this bucket.

This spec closes the gap: the missing LLM stages + a web-shaped export.

## Architecture & data flow

```
match.timer (6h)  →  articles rows: esg_status='matched', type/severity set, enrich_status='pending'
        │
        ▼
enrich.timer (NEW, offset from match)  ── drains a bounded chunk of enrich_status='pending'
        │   1. sentiment (LLM, batch 5)   → not_risk ⇒ enrich_status='dropped' (stop)
        │   2. translate title (LLM, 30)  → summary_en          (display title only)
        │   3. controversy (LLM, Cao only)→ level + justification (body from DB, revenue from Top100.csv)
        │   write columns, enrich_status='done'
        ▼
export.build_esg_events()  → flatten matched+risk+enriched → web EsgEvent shape → dedup
        ▼
gsutil cp → gs://esg-scan-data/web/esg_events.json  (+ web/top100.json)   [public-read prefix]
        ▼
web /api/events, /api/tickers  →  repointed to esg-scan-data/web/
```

Key properties:
- Enrich **reuses the body already fetched** by the `body` worker and stored in the DB — no extra
  Jina calls.
- Sentiment is a **gate**: dropped articles skip translation and controversy (saves LLM calls).
- Idempotent: only `enrich_status='pending'` rows are processed; an LLM failure leaves the row
  `pending` for the next cycle.

## Components / files

New package `enrich/` (each stage is a pure function: input → verdict, no DB access; `runner.py`
owns all I/O and state):

| File | Responsibility | Ported from |
|---|---|---|
| `enrich/llm.py` | Provider registry: `resolve_provider`, request build, call, rate-limit sleep | `cloud-function/controversy_classifier.py` |
| `enrich/sentiment.py` | Risk vs CSR/positive verdict (batch 5, "analyze independently", drop/keep rules) | `cloud-function/sentiment_filter.py` |
| `enrich/translate.py` | VN→EN of the **title only** (batch 30, transliterate names, strip source suffix) | `cloud-function/translator.py` |
| `enrich/controversy.py` | Major/Minor/No + justification; E&S vs CG branch; 20% revenue downgrade | `cloud-function/controversy_classifier.py` |
| `enrich/runner.py` | Read pending chunk → sentiment gate → translate → controversy (Cao) → write back | (new) |

Modified existing files:
- `core/storage.py` — add `articles` columns (below) via guarded `init_db()` ALTERs; add queries
  `get_pending_enrich(limit)`, `mark_enriched(...)`, `mark_dropped(...)`.
- `pipeline/export.py` — add `build_esg_events()` producing the web shape + `top100.json`; upload
  to `gs://esg-scan-data/web/`.
- `config/` — add `Top100.csv` (revenue per ticker/year for the 20% rule) and the E&S/CG
  controversy definition text (ported from the old prompts).
- `deploy/` — `esg-collector-enrich.{service,timer}` units (run after `match`, with a memory cap).

Web:
- `web/app/api/events/route.ts`, `web/app/api/tickers/route.ts` — change the GCS URL to
  `https://storage.googleapis.com/esg-scan-data/web/esg_events.json` (and `web/top100.json`).

## Data model & state machine

New columns on `articles` (all nullable; guarded ALTER in `init_db()`):
`summary_en`, `sentiment` (`risk`|`not_risk`), `controversy_level` (`Major`|`Minor`|`No`),
`controversy_justification`, `controversy_classified_at`, `enrich_status`.

```
esg_filter keeps article  →  enrich_status='pending'
   pending → sentiment: not_risk → 'dropped'  (excluded from export)
   pending → sentiment: risk → translate title → controversy (if severity='Cao') → 'done'
   LLM error at any step → stays 'pending' → retried next cycle
```

**Backfill of existing matched rows:** a one-time, idempotent migration sets
`enrich_status='pending'` on already-matched rows, gated behind a flag in `export_state` so reruns
are safe. The timer then drains the backlog over many cycles.

## OOM safety (e2-micro, 1 GB RAM)

The existing VM OOM comes from the old `match` loading the entire pending backlog at once. Enrich
is designed chunked from the start, with a hard cgroup cap so it can never take down the VM:

1. **Bounded chunk** — `get_pending_enrich(limit=K)` with `LIMIT` (K ≈ 20–30). Each timer run
   processes one small chunk, then exits and frees all memory. Peak RAM is independent of backlog
   size.
2. **systemd cap** — the `esg-collector-enrich.service` sets `MemoryMax=250M` and
   `Restart=on-failure`; if it ever exceeds the cap, the cgroup kills only enrich, not other
   workers (no kernel OOM of random processes).
3. **No concurrency with `match`** — enrich timer is offset from `match.timer`, plus a simple file
   lock, so their peak memory never adds up.
4. **Bounded body** — controversy loads body only for the Cao rows in the current chunk and
   truncates to ~6–8k chars before the LLM call (bounds RAM and tokens).
5. **Streaming** — cursor `fetchmany` over the chunk, not `fetchall`; process → write → release.
6. **Idempotent/resumable** — a mid-chunk kill leaves rows `pending`; the next run retries. No
   in-memory state to lose.

Result: per-run peak RAM is a fixed few tens of MB regardless of backlog, under a cgroup ceiling,
scheduled off-peak from `match`.

## Export & dedup

`build_esg_events()` flattens articles that are `esg_status='matched'` AND `sentiment='risk'`
(i.e. not dropped) into the web `EsgEvent` shape:
`{ ticker, company, type, date, summary, summary_en, severity, source, url, controversy_level,
controversy_justification, controversy_classified_at, created_at }`.

**Dedup:** the collector already computes `group_key` (clusters the same incident across multiple
sources) plus `dedup_titles`. The export collapses each `group_key` to a **single representative
event** — preferring the earliest-dated article that has a body/translation — which is stronger
than the old exact-normalized-title hash. The resulting array is sorted by date descending, matching
what the web expects today.

The richer per-article fields (`backend`, `matched_alias`, `location`) are **carried through as
optional fields** in `esg_events.json` so the web can surface them later (out of scope to display
now), but they do not change the dedup or the required shape.

## Output bucket access

`gs://esg-scan-data` is currently **private** (anonymous fetch returns 403). The web fetches over
public HTTPS with no auth, so the two web files must be publicly readable:

- Write `esg_events.json` and `top100.json` under a `web/` prefix.
- Grant `allUsers:objectViewer` on the `web/` prefix (or on those two objects) so
  `https://storage.googleapis.com/esg-scan-data/web/esg_events.json` is fetchable.

This is a one-time ops step (IAM), documented in the implementation plan. `top100.json` is built
from the collector's ticker list / `Top100.csv`.

## LLM provider

Port the old provider registry verbatim. Active provider is whichever `.env` selects (or auto-pick:
`groq > cerebras > openrouter > deepseek > openai > mistral > gemini`). Production stays on Groq
`meta-llama/llama-4-scout-17b-16e-instruct`; switching is `.env`-only. The VM `.env` must carry
`GROQ_API_KEY` (and `LLM_MODEL` override). Exactly one provider is active at a time.

## Error handling

- LLM / network failure on any stage → the row stays `enrich_status='pending'`; the next cycle
  retries. A failed chunk never corrupts data.
- Malformed LLM output → treated as a failure for that row (stays pending), logged; other rows in
  the chunk proceed.
- Controversy with missing body or missing revenue → classify with what's available (old behavior:
  no downgrade when scope/ownership is unknown); never block the row.

## Testing

- Unit tests with the **LLM mocked** (no real API calls): sentiment verdict parsing, title-translate
  parsing/batching, controversy parsing + the 20% downgrade rule, the `build_esg_events()` output
  shape, and the `group_key` dedup (one event per incident, earliest representative).
- A smoke test that the new `articles` columns and queries exist after `init_db()`.

## Out of scope (follow-up)

- Retiring the old `cloud-function/` and `gs://esg-risk-dashboard` bucket.
- Displaying the richer fields (`backend`, `matched_alias`, `location`) in the web UI.
- Unblocking the separate `match`/rematch OOM backlog (tracked elsewhere); enrich is designed not to
  add to it.

## Success criteria

- A new `enrich` timer runs on the VM, draining `enrich_status='pending'` in bounded chunks under a
  250M cgroup cap, without OOM-ing the VM.
- Sentiment drops CSR/positive false positives; surviving events get an EN title; Cao events get a
  controversy level + justification — matching the old pipeline's behavior on the same inputs.
- `gs://esg-scan-data/web/esg_events.json` is produced in the web `EsgEvent` shape, deduped by
  incident, publicly fetchable.
- The web, repointed to the new bucket, shows correctly-classified DBC events (E for the wastewater
  fines, not all-G) with EN titles and controversy where applicable.
- All unit tests pass; no real LLM calls in tests.
