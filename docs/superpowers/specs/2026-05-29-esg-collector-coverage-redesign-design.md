# ESG Collector — Coverage Redesign (Collect-Broad, Filter-Later)

- **Date:** 2026-05-29
- **Status:** Draft for review (rev 2 — addresses spec-review findings)
- **Scope:** `esg-pipeline/esg-collector/` — search task generation, keyword config, backend fetch behavior, window settings, and the post-collection ESG filter. Does NOT touch the web/dashboard or GCS layout.

---

## 1. Problem

The current collector searches **broad ESG keyword groups with no company name**, then alias-matches the company in whatever comes back. Verified on 2026-05-29 against the full GCS snapshot (`articles_full_20260528`, 62k articles):

- The whole pool contains only **7 "Dabaco" articles** — the same 7 in `per_ticker/DBC.json`. The old per-company pipeline's dashboard showed many more, including a dense Nov-2025 cluster (DBC fined 385tr for disclosure violations, ~8 outlets). **None of that cluster is in the new pool.**
- It is **not a date-coverage gap**: 2025 is the densest year in the pool (11,906 articles).
- Root cause is the **per-query harvest limit** combined with broad, company-agnostic queries:
  - **Google News RSS** returns ~100 relevance-ranked results per (keyword × time-chunk). In busy months, generic keywords match thousands of articles → a mid-cap's specific fine is crowded out of the top 100. (~5.5% of Google tasks in the logs hit the ~100 cap.)
  - **BaoMoi** has no ~100 cap (returns 200–400/task) and a deep archive (live probe: `"Dabaco"` → 1,137 items spanning 2022-10 → 2026-05, incl. 56 in Nov–Dec 2025). But it paginates newest→oldest with `MAX_PAGES=50`; a **broad** keyword has hundreds of articles/day so 50 pages spans only a few days → old months return 0. A **narrow** per-company query reaches back years.

**Conclusion:** to guarantee a company's events are captured, the query must be narrow enough that its entire result set fits under each backend's harvest window. The narrowest natural axis is the **company name (alias)**, not the ESG keyword.

## 2. Goals / Non-Goals

**Goals**
- Capture (recall) every ESG-relevant article for each of the ~100 tracked companies, accepting collection may run several days.
- Keep collection robust against rate limits / timeouts (reuse the existing persistent queue + backoff).
- Move ESG relevance/precision out of the search step into a **rerunnable downstream filter**, so misclassification can be fixed without re-crawling.
- Eliminate current false positives (e.g. "cổ tức Dabaco") at the filter step.

**Non-Goals**
- No LLM classification here. LLM/sentiment refinement is Tier 3, on-demand, on the already-narrowed set — out of scope.
- No change to dedup keys, GCS export, web dashboard, or systemd/deploy topology.
- Not optimizing for speed — multi-day acceptable; **multi-week is not** (this constraint drives the granularity decisions in §4 and §11).

## 3. Design Overview — two beats

```
NHỊP 1 — THU (collect broad, store raw, no judgement)
  queue_builder emits two task kinds (both run through the existing workers/queue):
    L1 keyword : <single word>      × MONTHLY chunk, company-agnostic   (supplementary discovery net)
    L2 alias   : "<company alias>"  × per-backend granularity            (per-company coverage guarantee)
  workers fetch → INSERT OR IGNORE into `articles` (Tier 1 RAW)
  dedup (existing): article_id, then (title_hash, published_at); richer row wins

NHỊP 2 — LỌC (local, free, rerunnable; 0 API calls)
  pipeline.match, every 6h (and on demand via --rematch-all):
    ① ALIAS MATCH   — which ticker(s) the article mentions          (existing alias_matcher)
    ② ESG FILTER    — keep negative E/S/G, drop noise, tag type+severity  (NEW)
    → keep  → per_ticker/<TICKER>.json (Tier 2) with type + severity
    → drop  → mark non-ESG / noise, leave in pool for future re-filtering
```

The split decouples **coverage from precision**: fixing the filter is a local `--rematch-all` over the existing pool (zero queries), matching the existing Tier-2 "regenerate any time" property.

## 4. Component A — Search task generation (`core/queue_builder.py`, `core/storage.py`)

### A.1 Two task kinds + window settings

`search_queue` currently keys tasks as `{backend}:{group_key}:{sub_query_ix}:{after}`, and `enqueue_task()` is keyword-only (`def enqueue_task(conn, *, backend, group_key, sub_query_ix, query, after, before)`). Changes:

- **Schema additions** (idempotent `ALTER TABLE` in `storage.init_db()`, guarded by `PRAGMA table_info`):
  - `search_queue.kind TEXT DEFAULT 'keyword'` — `'keyword'` (L1) or `'alias'` (L2)
  - `search_queue.ticker TEXT` — set for alias tasks, NULL for keyword tasks
- **`enqueue_task()` signature** gains `kind='keyword'` and `ticker=None` keyword args (defaulted so existing callers are unaffected).
- **task_id schemes** (globally unique, idempotent under `INSERT OR IGNORE`):
  - L1 keyword: `{backend}:kw:{word_ix}:{after}`
  - L2 alias: `{backend}:alias:{ticker}:{alias_ix}:{after}`
- **Window settings (`config/settings.py`) must be extended.** Today `BACKFILL_END=2024-12-31`, `BAOMOI_WINDOW_END=2024-12-31`, `BRAVE_WINDOW_END=2021-12-31` — all ~17 months behind "today", so the design's motivating example (Nov-2025) is unreachable without this change. Replace the hardcoded end dates with a computed rolling end = today (VN date), keeping per-backend **start** floors. After this change the backfill reaches the present; the existing `daily` mode continues to top up recent days.

`workers/runner.py` reads `query/after/before/group_key/sub_query_ix` generically; it gains: read `kind`/`ticker`, and for alias tasks stamp `articles.ticker_hint` (see §8) as provenance.

### A.2 L1 — keyword discovery net (company-agnostic, supplementary)

- Flatten the master keyword list (§5) to **single terms**, de-duplicated (several terms repeat across the current OR-groups, e.g. `xử phạt`/`vi phạm`). Estimate ≈ 80–96 unique terms after dedup.
- **MONTHLY** chunks over the full window `2020-01-01 → today`, on **Google RSS + Brave** (not BaoMoi — a broad term cannot paginate back on BaoMoi; BaoMoi's value is the L2 deep pass).
- **Why monthly, not weekly (changed from the original idea):** once L2 (§A.3) guarantees the tracked 100 companies, L1's only residual job is discovering events at entities *outside* the tracked list. The ~100 cap on a monthly single-term query therefore no longer threatens the core goal, and monthly keeps Google bounded (≈ 90 terms × 77 months ≈ 6.9k tasks ≈ ~2 days at 25s) instead of weekly (~9 days, which breaches the no-multi-week constraint). L1 is explicitly a **supplementary** net and may be deferred to a later phase without affecting tracked-company coverage.

### A.3 L2 — per-company alias search (the coverage guarantee)

Each ticker has two alias tiers (both auto-built — see §10): **names** (the parent brand variants) and **subsidiaries** (the differently-named children, e.g. Nasaco/Dacovet for DBC). They are used differently to keep runtime bounded:

- **Names** → search axis on **all** backends.
- **Subsidiaries** → search axis on **BaoMoi only** (its deep pass is cheap and un-chunked), plus they are **always** used as match aliases downstream (§7) regardless of whether they were searched. Subsidiaries are NOT searched on Google/Brave (would explode the chunked tail).
- All aliases are emitted as bare `"<alias>"` queries (no ESG keyword attached).
- **Per-backend granularity** (chosen to keep total runtime in days):
  - **BaoMoi:** one **deep pass per (ticker, alias)** over names + subsidiaries — no date chunk; the worker paginates to the window start (see §6). Low per-query volume → ~100 tickers × ~15–20 aliases ≈ 1.5–2k deep passes ≈ a few hours. Covers ~2022 → today.
  - **Google RSS / Brave:** **names only**, × **monthly** chunk, restricted to the **2020–2021 tail** BaoMoi cannot reach. ≈ 100 tickers × ~2 name aliases × 24 months ≈ 4.8k Google tasks ≈ ~1.5 days. Differently-named subsidiaries in the 2020–2021 tail are best-effort (caught if the article also names the parent, or via L1).
  - **High-volume weekly-split fallback (distinct new capability):** if an alias×month Google task returns ≥ 90 items (truncation risk, mostly large blue-chips), the **worker re-enqueues** that month as 4 weekly child tasks. This requires `workers/runner.py` to gain the ability to write child tasks into `search_queue` (it currently never enqueues) — treat as its own implementation unit, not a config tweak.

### A.4 Backend × period roles

| Period | Primary | Secondary |
|---|---|---|
| 2022 → today | BaoMoi (deep alias pass; has sapo/body) | Google RSS (L1 monthly) |
| 2020–2021 (tail) | Google RSS (alias + L1 monthly) | Brave (alias + L1 monthly) |

Depends on the §A.1 settings change so all windows reach `today`.

## 5. Component B — Unified keyword config (`config/keywords.py`)

Today `keywords.py` holds 24 OR-groups (search only); legacy `cloud-function/keyword_classifier.py` holds a separate narrower ESG list **plus a NOISE blacklist**. Unify to one source of truth:

- **`ESG_KEYWORDS`** — one master list, each entry tagged `E`/`S`/`G`. Serves **both**:
  - **Search (L1):** each term emitted as a single-word query (after dedup).
  - **Classify (§7):** the same terms are the "is this ESG-negative?" whitelist and supply the type tag.
- **`NOISE_KEYWORDS`** — separate blacklist (`cổ tức`, `lợi nhuận tăng`, `thâu tóm/sáp nhập`, sports, PR…). Used **only** by the classifier. Ported from `keyword_classifier.NOISE_KEYWORDS`.
- **`HIGH_SEVERITY_KEYWORDS`** — ported; drives severity (§7).

Helpers: `search_terms()` → deduped flat list for L1; `esg_terms()` → `[(term, type)]`; `noise_terms()`; `high_severity_terms()`.

Rationale: one place to edit ESG vocabulary keeps search and classify in sync; the NOISE list is the part that removes neutral/positive news and has no search-side equivalent.

## 6. Component C — Backend fetch behavior (`backends/baomoi.py`)

- **BaoMoi alias deep pass:** `fetch(query, after, before)` already paginates newest→oldest and stops when `oldest_ts < after_ts`. Raise `MAX_PAGES` from 50 → **200** so low-volume alias queries reach the 2022 floor (probe: ~120 pages reached 2022-10 for "Dabaco"); the existing early-stop keeps quiet queries fast. No interface change.
- **Google RSS / Brave:** fetch logic unchanged — they already accept `after/before`; they just receive more, narrower tasks. The ~100 cap is handled by the §A.3 weekly-split fallback.
- No change to the shared item shape in `backends/base.py`.

## 7. Component D — ESG filter (NEW: `pipeline/esg_filter.py`, integrated into `pipeline/match.py`)

Port the four-part logic of `cloud-function/keyword_classifier.py` into the collector, operating on `title + sapo + body`:

1. **About-company attribution** — authoritative source is the collector's `core/alias_matcher` (regex with Unicode word-boundaries `(?<!\w)…(?!\w)`). This already prevents the substring false-positives (e.g. "Khánh Hòa phát" ≠ "Hòa Phát") that the legacy `FALSE_POSITIVE_CONTEXTS` existed to patch in the substring-based legacy matcher — so **do NOT port `FALSE_POSITIVE_CONTEXTS`**; `alias_matcher` is sufficient. `ticker_hint` (from an L2 search) is **advisory only**: it records provenance and lets the body-fetcher prioritize alias-sourced articles; it does **not** force attribution. Attribution is whatever `alias_matcher` returns.
2. **Noise check** — if a `NOISE_KEYWORDS` term hits and no `HIGH_SEVERITY_KEYWORDS` term is present → mark `esg_status='noise'`, do not write to per_ticker.
3. **ESG check** — require ≥1 `ESG_KEYWORDS` term → otherwise `esg_status='non_esg'`.
4. **Tag** — `esg_type` = E/S/G by per-type term count (tie-break G > E > S, as in legacy); `severity` = `Cao`/`Trung bình` via `HIGH_SEVERITY_KEYWORDS` and the fine-amount regex (`≥ 500 triệu` or any `tỷ` → Cao).

**Body-pending handling (mirrors the existing two-stage match in `pipeline/match.py`):**
- Run alias-match + ESG filter on `title + sapo` first. If it yields a keep → finalize.
- If no keep and `body_status` is still `pending` → leave `esg_status='pending'` (the body-fetcher will fetch it, and the next match cycle re-runs the filter with the body).
- If no keep and `body_status` is terminal (`fetched|skipped|failed`) → finalize as `noise`/`non_esg`. This matches how `match.py` currently defers `match_status`.

**On keep:** write to `per_ticker/<TICKER>.json` with `type`, `severity`, `match_source` (title|sapo|body); set `articles.esg_status='esg'`, `esg_type`, `severity`. Dropped rows stay in the pool so a future vocabulary fix + `--rematch-all` can reclassify them.

## 8. Data model changes (`core/storage.py`)

Idempotent `ALTER TABLE` additions guarded by `PRAGMA table_info` (existing migration style):

- `search_queue.kind TEXT DEFAULT 'keyword'`, `search_queue.ticker TEXT`
- `articles.ticker_hint TEXT` — ticker an L2 alias-search targeted (NULL for L1); advisory/provenance only (§7). To actually persist it, also add `ticker_hint` to `_ARTICLE_COLS` in `storage.insert_article` and to the rec dict built in `runner._process_task` — the migration alone is not enough.
- `articles.esg_status TEXT DEFAULT 'pending'` — `pending|esg|noise|non_esg`
- `articles.esg_type TEXT` — `E|S|G`
- `articles.severity TEXT` — `Cao|Trung bình`

`match_status` keeps its current meaning (company matched vs not); `esg_status` is the new ESG verdict layer so the two concerns stay separable.

## 9. Backfill & rerun strategy

- **Augment, do not wipe.** Enqueue new L1 + L2 tasks alongside the existing pool. `INSERT OR IGNORE` on `article_id`/`task_id` makes re-runs free; dedup merges new richer rows (sapo/body) into existing ones.
- **Filter is rerunnable.** `pipeline/match.py --rematch-all` must reset **both** `match_status` AND `esg_status` to `pending` (today it resets only `match_status` — extend it) and rebuild per_ticker from the existing pool — zero API calls. This is the path for any keyword/NOISE-list change.
- Legacy keyword-pool rows already collected remain valid inputs to the new filter.

## 10. Alias coverage (auto-built, including subsidiaries)

The filter and L2 search need names + subsidiaries for all tracked tickers. Today only DBC/KDH/DGC exist (hand-curated). The remaining ~97 are auto-built by extending `alias_builder/fetch_vietstock.py` to fetch **two** Vietstock pages per ticker:

1. `…/ho-so-doanh-nghiep.htm` → corporate name + short brand + HQ province (already implemented).
2. `…/cong-ty-con-lien-doanh-lien-ket.htm` → **subsidiaries** (NEW). Verified 2026-05-29: this dedicated page renders the full subsidiary table **in static HTML, NOT blurred** (`******` paywall absent here, unlike the profile page's embedded widget). Parse rows by the capital marker — `(CTCP|Công ty|Tổng [Cc]ông ty|Tập đoàn)…  <number> ( Tr. VND )`. Yields full lists: DBC 29 (incl. "Dabaco Thanh Hóa", Nasaco, Dacovet, Nutreco…), KDH 24, HPG 5. The earlier docstring claim that subsidiaries are AJAX-only is **wrong** — no login, token, or `/view` POST needed.

- **Short-form derivation (alias quality):** the page gives full legal names ("Công ty TNHH Thức ăn chăn nuôi Nasaco Hà Nam"). Store the full name as a (safe, specific) alias, and derive distinctive short tokens via two sources in `short_aliases()`: (1) strip the legal prefix and keep the remainder when it's ≤4 words and not in the generic blacklist ("Dabaco Thanh Hóa"); (2) extract an **interior coined brand token** ("Nasaco") that source (1) can't reach because the remainder is too long. **Decision (review 2026-05-29): auto-extract interior tokens** rather than leaving them to hand-curation, since press writes "Nasaco"/"Dacovet", not the legal name. `_is_brand_token` accepts a no-diacritic (pure-ASCII) capitalised word that (a) is **not** a legal form / generic English business noun (`_ASCII_STOP`), (b) is **not** a province, and (c) has **≥2 vowel groups** — the last guard is what separates a fused coined brand (Na-sa-co, Da-co-vet) from a lone Vietnamese syllable whose diacritic sits on its neighbour ("Thanh" in "Thanh Hóa", "Ninh" in "Quảng Ninh", "Minh" in "Minh Phát"), which must not become standalone aliases. The older generic blacklist (`_GENERIC`: "Minh Phát", "Song Lập") still guards the prefix-strip path. Subsidiaries whose name already contains the parent brand ("Dabaco Thanh Hóa") are redundant with the parent alias but harmless.
- `projects` remain empty/out of scope.

## 11. Rate-limit & runtime

No new rate-limit machinery — existing per-backend throttle, exponential backoff (5m→30m→2h), jitter, UA rotation, crash-safe queue carry over. The 4 workers run **in parallel** (one process per backend), so wall-clock ≈ the slowest single backend's queue, not the sum. Budget on Google (the 25s bottleneck):

- L2 Google 2020–2021 tail: ~100 × ~2 aliases × 24 months ≈ **4.8k tasks ≈ ~1.5 days**.
- L1 Google monthly: ~90 terms × 77 months ≈ **6.9k tasks ≈ ~2 days**.
- Google total ≈ **~3.5 days** (names-only tail + L1); BaoMoi L2 deep pass (names + subsidiaries, ~1.5–2k passes × 15s) ≈ **~6–8 hours**, runs in parallel; Brave parallel too (watch its quota — logs showed ~192 failed tasks, so treat Brave as best-effort secondary).

Total wall-clock stays within "several days," meeting the constraint. (Weekly L1 was rejected here precisely because it pushed Google to ~9 days.)

## 12. Testing

Extend `tests/test_smoke.py` (no network):
- `queue_builder` emits both task kinds with correct unique `task_id`s and `kind`/`ticker` fields; monthly chunk boundaries correct; L1 term list is de-duplicated.
- `esg_filter`: keep case ("Xử phạt Dabaco 300 triệu vì vi phạm môi trường" → keep, type=E, severity Trung bình); noise case ("Cổ đông Dabaco nhận cổ tức" → `noise`); severity case (fine ≥ 500 triệu / `tỷ` → Cao); boundary case ("Khánh Hòa phát hiện…" → not attributed to HPG, via `alias_matcher` regex).
- **Body-only attribution:** an article whose alias appears only in `body` (not title/sapo) with `body_status='fetched'` is attributed and ESG-classified correctly — this is the core recall path.
- **Body-pending deferral:** same article with `body_status='pending'` stays `esg_status='pending'` (not prematurely dropped).
- Migration idempotency: `init_db()` twice is a no-op; new columns present. `--rematch-all` resets both `match_status` and `esg_status`.

A small **live** verification (manual, not CI), mirroring the existing 1-month verify step: run L2 alias for DBC over a known window and confirm the Nov-2025 fine cluster lands in `per_ticker/DBC.json`.

## 13. Rollout sequence

1. **Schema + settings:** additive `ALTER TABLE`s in `storage.init_db()`; extend `config/settings.py` window ends to rolling `today`.
2. **Filter first (precision on existing data):** unify `config/keywords.py` (master tagged list + NOISE + severity); add `pipeline/esg_filter.py`; wire into `pipeline/match.py` (incl. body-pending deferral and `--rematch-all` resetting `esg_status`). Verify via `--rematch-all` on the existing pool — the "cổ tức" item should drop, DBC's real events keep. **No new crawling yet.**
3. **Recall — task generation:** `queue_builder` two-kind support + monthly chunking + L1 single-term flattening; `enqueue_task()` new kwargs; `backends/baomoi.py` `MAX_PAGES`→200; worker `kind/ticker` read + `ticker_hint` stamp.
4. **Weekly-split fallback** in `workers/runner.py` (distinct unit: worker emits child tasks).
5. **Aliases:** extend `fetch_vietstock.py` to also parse `cong-ty-con-lien-doanh-lien-ket.htm` for subsidiaries (with short-form derivation + generic-token guard), then run `--all` for the ~97.
6. **Enqueue** L2 (alias) then L1 (single-term monthly); let workers drain over several days.

Step 2 alone fixes precision on already-collected data; steps 3–6 add the missing recall.

## 14. Decisions captured (defaults; open at review)

- L1 = single **terms** (deduped, ≈ 80–96), **monthly**, Google + Brave only; **supplementary** (L2 is the coverage guarantee). Weekly was rejected on the runtime budget (§11). L1 may be deferred to a later phase.
- L2 aliases = `names` + `subsidiaries`, **both auto-built** from Vietstock's unblurred `cong-ty-con-lien-doanh-lien-ket.htm` (§10). Names searched on all backends; subsidiaries searched on BaoMoi only but always used as match aliases (§A.3). Short-form derivation skips over-generic tokens.
- Weak/location aliases excluded from L2 search (cross-company false positives; consistent with `alias_matcher` weighting).
- L2 high-volume weekly-split threshold = 90 items/month (Google only).
- Window: `2020-01-01 → today` after the §A.1 `settings.py` change; BaoMoi effective floor ~2022 (tail to Google/Brave).
- `ticker_hint` is advisory/provenance only; `alias_matcher` is authoritative for attribution. `FALSE_POSITIVE_CONTEXTS` not ported.
- ESG filter is keyword/rule only here; LLM/sentiment deferred to Tier 3.
