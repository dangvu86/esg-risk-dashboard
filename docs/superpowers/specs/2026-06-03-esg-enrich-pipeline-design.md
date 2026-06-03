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
match.timer (6h)  →  kept articles: esg_status='esg', esg_type/severity set; per_ticker/*.json updated
                     (NEW column enrich_status defaults 'pending' on these rows)
        │
        ▼
enrich.timer (NEW, offset from match)  ── drains a bounded chunk of (esg_status='esg' AND enrich_status='pending')
        │   1. sentiment (LLM, batch 5)   → not_risk ⇒ enrich_status='dropped' (stop)
        │   2. translate title (LLM, 30)  → summary_en          (display title only)
        │   3. controversy (LLM, Cao only)→ level + justification (body from articles.body, revenue per matched ticker)
        │   write columns, enrich_status='done'
        ▼
export.build_esg_events()  → per_ticker/*.json  ⨝(article_id)  articles enrich cols → filter risk → dedup(title_hash) → web shape
        ▼
gsutil cp → gs://esg-scan-data/web/esg_events.json  (+ web/top100.json)   [objects made public]
        ▼
web /api/events, /api/tickers  →  repointed to esg-scan-data/web/
```

Naming note (verified against `core/storage.py` + `pipeline/match.py`): `match.py` writes
`esg_status='esg'` for kept articles (via `mark_esg`), NOT `'matched'`. `enrich_status` is a **new**
column this spec adds — distinct from the existing `esg_status`. The ticker↔article association is
stored in `per_ticker/*.json` (an article may match several tickers via `for hit in hits`), not as a
column on `articles`.

Key properties:
- Enrich **reuses `articles.body`** already fetched by the `body` worker — controversy must read body
  from the DB and **must NOT re-port the old Jina `fetch_article_body()` / Google-URL-decode path**.
- Sentiment is a **gate**: dropped articles skip translation and controversy (saves LLM calls).
- Idempotent: only `esg_status='esg' AND enrich_status='pending'` rows are processed; an LLM failure
  leaves the row `pending` for the next cycle.

## Components / files

New package `enrich/` (each stage is a pure function: input → verdict, no DB access; `runner.py`
owns all I/O and state):

| File | Responsibility | Ported from |
|---|---|---|
| `enrich/llm.py` | Provider registry: `resolve_provider`, request build, call, rate-limit sleep | `cloud-function/controversy_classifier.py` |
| `enrich/sentiment.py` | Risk vs CSR/positive verdict (batch 5, "analyze independently", drop/keep rules) | `cloud-function/sentiment_filter.py` |
| `enrich/translate.py` | VN→EN of the **title only** (batch 30, transliterate names, strip source suffix) | `cloud-function/translator.py` |
| `enrich/controversy.py` | Major/Minor/No + justification; E&S vs CG branch; 20% revenue downgrade. Reads `articles.body`; **reimplements** `get_revenue_for_year()` reading `config/Top100.csv` (the old one is entangled with `cloud-function/rss_fetcher.py` — do not import it) | `cloud-function/controversy_classifier.py` |
| `enrich/runner.py` | Read pending chunk → resolve matched ticker (via `alias_matcher`) → sentiment gate → translate → controversy (Cao) → write back | (new) |

The E&S/CG controversy definitions stay **inline in the controversy prompt** (ported verbatim from
the old `CLASSIFY_PROMPT`), not separate config files.

Modified existing files:
- `core/storage.py` — add `articles` columns (below) via guarded `init_db()` ALTERs (same pattern as
  the existing `esg_status`/`esg_type` ALTERs); add queries `get_pending_enrich(limit)`,
  `mark_enriched(...)`, `mark_dropped(...)`.
- `pipeline/export.py` — add `build_esg_events()` (see Export & dedup) + `top100.json`; upload to
  `gs://esg-scan-data/web/`.
- `config/` — add `Top100.csv` (revenue per ticker/year for the 20% rule). Top100.csv already exists
  at the repo root / cloud-function; copy it into `esg-collector/config/`.
- `deploy/` — `esg-collector-enrich.{service,timer}` units (run after `match`, with a memory cap).

Web:
- `web/app/api/events/route.ts`, `web/app/api/tickers/route.ts` — change the GCS URL to
  `https://storage.googleapis.com/esg-scan-data/web/esg_events.json` (and `web/top100.json`).

## Data model & state machine

New columns on `articles` (all nullable; guarded ALTER in `init_db()`, same pattern as the existing
`esg_status`/`esg_type`/`severity` ALTERs): `summary_en`, `sentiment` (`risk`|`not_risk`),
`controversy_level` (`Major`|`Minor`|`No`), `controversy_justification`,
`controversy_classified_at`, `enrich_status` (default `'pending'`).

```
match keeps article (esg_status='esg')  →  enrich_status='pending'
   pending → sentiment: not_risk → 'dropped'  (excluded from export)
   pending → sentiment: risk → translate title → controversy (if severity='Cao') → 'done'
   LLM error at any step → stays 'pending' → retried next cycle
```

**Backfill of existing kept rows:** a one-time, idempotent migration sets `enrich_status='pending'`
on rows where `esg_status='esg'`, gated behind the `export_state` key `enrich_backfill_done` so
reruns are safe. The timer then drains the backlog in bounded chunks over many cycles.

**Field-name caveat:** in the collector, the per_ticker `location` field is the **matched field
name** (`title`|`description`|`sapo`|`body`), NOT geography. Do not surface it as a place. (The
mockup's "location = Bắc Ninh" geography does not exist in this data and is out of scope.)

## OOM safety (e2-micro, 1 GB RAM)

The existing VM OOM comes from the old `match` loading the entire pending backlog at once. Enrich
is designed chunked from the start, with a hard cgroup cap so it can never take down the VM:

1. **Bounded chunk** — `get_pending_enrich(limit=K)` with `LIMIT` (K ≈ 20–30). Each timer run
   processes one small chunk, then exits and frees all memory. Peak RAM is independent of backlog
   size.
2. **systemd cap** — the `esg-collector-enrich.service` sets `MemoryMax=250M` and
   `Restart=on-failure`; if it ever exceeds the cap, the cgroup kills only enrich, not other
   workers (no kernel OOM of random processes).
3. **No concurrency with `match`** — order the enrich unit after match in systemd
   (`After=esg-collector-match.service`) rather than a fixed clock offset (match runtime is
   backlog-dependent), plus a simple file lock (skip the run if the lock is held), so their peak
   memory never adds up even if a match run is slow.
4. **Bounded body** — controversy loads body only for the Cao rows in the current chunk and
   truncates to ~6–8k chars before the LLM call (bounds RAM and tokens).
5. **Streaming** — cursor `fetchmany` over the chunk, not `fetchall`; process → write → release.
6. **Idempotent/resumable** — a mid-chunk kill leaves rows `pending`; the next run retries. No
   in-memory state to lose.

Result: per-run peak RAM is a fixed few tens of MB regardless of backlog, under a cgroup ceiling,
scheduled off-peak from `match`.

## Export & dedup

`build_esg_events()` builds one array of web `EsgEvent` objects by reading every `per_ticker/*.json`
(which carries `ticker`, `type`, `severity`, `title`, `published_at`, `source`, `url`, `backend`,
`matched_alias`, `article_id`) and **joining the enrich columns from `articles` by `article_id`**
(`summary_en`, `sentiment`, `controversy_*`). Only entries whose article has `sentiment='risk'`
(i.e. survived the sentiment gate; dropped/pending excluded) are emitted.

Web `EsgEvent` shape (verified against `web/lib/esg.ts`):
`{ ticker, company, type, date, summary, summary_en, severity, source, url, controversy_level,
controversy_justification, controversy_classified_at, created_at }` where:
- `summary` = the article `title` (VN headline); `summary_en` = enriched EN title.
- `date` = `published_at[:10]` (truncate ISO timestamp to `YYYY-MM-DD`).
- `created_at` = `articles.fetched_at` (used only as the web's sort tie-breaker).
- `company` = canonical name for the ticker (from `config/aliases/<TICKER>.json` / `Top100.csv`).

**Dedup:** use the existing `articles.title_hash` column (already computed; the collector dedups
cross-backend on `(title_hash, published_at)`). Within each ticker, collapse rows sharing the same
`title_hash` to a **single representative**, keeping the earliest `published_at`. (NOTE: `group_key`
is the keyword-search slot like `E_0`/`S_2`, NOT an incident cluster — do not use it for dedup.)
Sort the final array by `date` descending, matching what the web expects today.

`backend` and `matched_alias` are **optional passthrough fields** for later web use (out of scope to
display now); they don't affect dedup or the required shape. `cg_indicator` (G-only) is folded into
`controversy_justification` and is **not** a separate web field.

## Output bucket access

`gs://esg-scan-data` is currently **private** (anonymous fetch returns 403). The web fetches over
public HTTPS with no auth, so the two web files must be publicly readable. GCS has **no
prefix-scoped public IAM**; the correct, narrowly-scoped option is a **per-object public ACL** on
just those two objects (keeps the rest of the bucket private):

```
gsutil acl ch -u AllUsers:R gs://esg-scan-data/web/esg_events.json
gsutil acl ch -u AllUsers:R gs://esg-scan-data/web/top100.json
```

(Per-object ACLs require the bucket to allow fine-grained ACLs, i.e. not Uniform Bucket-Level
Access; if UBLA is on, the fallback is a dedicated public sub-bucket or accepting bucket-level
`allUsers:objectViewer`. The plan must check the bucket's access-control mode first.) The export
re-applies the ACL after each upload (a fresh object loses prior ACLs). `top100.json` is built from
the collector's ticker list / `Top100.csv`. Web reads
`https://storage.googleapis.com/esg-scan-data/web/esg_events.json`.

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
- **Multi-ticker articles** (one article matched to >1 ticker — rare, since aliases are
  company-specific): enrich computes one controversy verdict per article, using the **primary
  (first) matched ticker's** revenue for the 20% rule (first = `alias_matcher` hit order, i.e.
  aliases-file insertion order — deterministic). Documented limitation; acceptable because the old
  pipeline had no global dedup at all and the case is uncommon.

## Testing

- Unit tests with the **LLM mocked** (no real API calls): sentiment verdict parsing, title-translate
  parsing/batching, controversy parsing + the 20% downgrade rule, the `build_esg_events()` output
  shape, and the `title_hash` dedup (same-incident rows collapse to one, earliest `published_at` kept).
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
