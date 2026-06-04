# ESG Collector — Surgical removal of ambiguous bare-ticker aliases

**Date:** 2026-06-04
**Status:** Design approved, pending spec review
**Author:** session with Claude

## Problem

The alias matcher attributes news articles to companies. A large share of
those attributions are **false positives**, and a measurement on the live
corpus shows the dominant cause is **bare ticker codes used as match
aliases** — not, as initially hypothesised, sidebar/related-news noise in
fetched article bodies.

The alias builder deliberately injects the ticker symbol as the first
`names[]` entry for every company
([`alias_builder/fetch_vietstock.py:315`](../../../esg-collector/alias_builder/fetch_vietstock.py) —
`add(ticker.upper())`; the module docstring states *"names = full corporate
name + short brand + ticker"*). The matcher
([`core/alias_matcher.py`](../../../esg-collector/core/alias_matcher.py))
treats every `names[]` entry as a **strong alias, weight 1.0, equal to the
full company name**. So a 3-letter ticker like `GAS` is trusted exactly as
much as `Tổng Công ty Khí Việt Nam`. Roughly 40 of the ~100 tickers are
also common Vietnamese/English words, city names, currency codes, or generic
abbreviations, so the bare ticker matches text that is not about the company.

Notably, the builder spends real effort guarding *subsidiary* short tokens
(the `_is_brand_token` vowel-group heuristic and province/stopword lists in
`fetch_vietstock.py:100-197`) but applies **zero precision guard to the bare
ticker** — it is added unconditionally.

### Measured evidence

Measurements taken 2026-06-04 against the live GCS data
(`gs://esg-scan-data`), pulled with the `dangvule@gmail.com` account.

**Corpus** (full snapshot `raw_esg/articles_full_20260528_085453.ndjson`,
61,998 articles):

| body_status | count | note |
|---|---|---|
| failed   | 53,384 (86%) | Jina + bs4 fallback both failed — most articles have no body |
| fetched  | 7,105 (11%)  | the only rows with a real body to clean |
| skipped  | 1,186        | matched on title/desc/sapo, body not fetched |
| pending  | 323          | |

**Matches by field** (`per_ticker/*.json`, 3,617 matched articles):
title 1,567 (43%) · description 556 (15%) · **body 1,494 (41%)**.

**Root cause:** matches where `matched_alias == ticker code` = **1,427
(39.5%)**, spread across title 720 / description 281 / body 426. The worst
offenders are word/city/currency collisions:

- `HCM` — 330 matches, all in title+description (HCM = Ho Chi Minh City).
- `GAS` — 153 (GAS = the word "gas").
- `KDC` — 71 (KDC = "khu dân cư", residential area).
- `VND` — 27 (VND = the đồng currency; 26/27 are amounts like "...tỷ đồng").

**Sidebar/related-news noise is a weak lever**, contrary to the original
hypothesis: only **15.8%** of *fetched* bodies contain any related-news
marker ("Tin liên quan", "Xem thêm", …), and those markers sit **mid-body**
(median relative position 0.24–0.62), so a "cut after marker" heuristic both
covers little and risks truncating real article text. The 77% / `|` /
`>> Xem thêm` figures that motivated the original plan came from a Brave
search-snippet experiment file
(`experiments/vn_scrape/brave_local/DBC.json`), **not** from a stored Jina
body.

### Why not just remove all bare tickers

A simulation of "remove every bare ticker, keep name/brand/subsidiary
aliases" was run against the 05-28 snapshot (332 of the 1,427 bare-ticker
matches were present in that snapshot to simulate):

- **Survive** (a company name alias also appears, so still matched): 15 (4.5%)
- **Lost** (only the bare ticker hit → becomes unmatched): 317 (95.5%)

Inspecting the lost set shows it is **mixed**: about half are genuine junk
(word collisions like GAS/HCM), but the other half are **real ESG events for
companies that Vietnamese news refers to by ticker**, which would be wrongly
discarded:

- `ACV`: *"ACV bị phạt 270 triệu đồng do gây ô nhiễm môi trường … Long Thành"*
  (real environmental fine), *"Vì sao Chủ tịch ACV Vũ Thế Phiệt bị bắt?"*
  (real governance event).
- `BAF`: *"BAF nói gì khi trại nuôi heo bị phạt do xả thải ô nhiễm?"* (real
  environmental event).

For these companies the bare ticker is the **only** way the article matches,
because the article says "ACV", not "Tổng công ty Cảng hàng không Việt Nam".
Removing all bare tickers therefore costs real recall. Removal must be
**surgical**.

## Goal

Eliminate the false positives caused by word/city/currency-collision tickers
while **keeping recall flat** — in particular without dropping real ESG
events for companies that are commonly named by their ticker.

Precision ↑ on the ~600 collision-driven bare-ticker matches; recall ≈
unchanged (real events for collision tickers are independently covered by the
company name; real events for distinctive tickers keep their bare ticker).

## Non-goals (out of scope — see Follow-ups)

This spec deliberately does **not** address:

- **Listicle / aboutness false positives** (~15% of matches — e.g. *"ngân
  hàng nào tốt nhất?"* matching ACB; a V.League football article matching
  ACB in body). These are an aboutness problem, not a ticker problem.
- **Subsidiary short-token fragments** (e.g. `Delta`, `Apatit`).
- **Body sidebar/related-news cleaning** (the original hypothesis; measured
  to be low-ROI and risky).
- **The 86% body-fetch failure rate** (a separate, larger reliability issue).
- **Fixing rematch infrastructure** (a hard dependency — see Rollout).

## Approach — surgical removal

Remove the bare ticker from the matchable alias set **only for tickers whose
surface form collides** with a common Vietnamese/English word, a city/
province, a currency/unit, or a generic abbreviation. Keep the bare ticker
for distinctive acronym tickers.

### Classification: lexical, then audited

The classification criterion is **lexical** ("does the ticker's surface form
collide with a non-company word?"), **not frequency-based**. A purely
data-driven junk-proxy (fraction of a ticker's bare matches that land in
body/description/listicle) was tried and **rejected** — it over-flags
distinctive tickers that simply appear in many bodies (it wrongly flagged
`FPT`, `SHB`, `DIG`, `REE`). Every candidate must be confirmed by auditing
its real matched articles.

The audit (2026-06-04) produced this starting list:

**Drop bare ticker (confirmed collisions; real events independently covered
by the company name):**

| Ticker | Collides with | Bare matches | Real events keep via |
|---|---|---|---|
| HCM | Ho Chi Minh City | 330 | "HSC", "Chứng khoán HSC" |
| GAS | word "gas" | 153 | "PV Gas", "Tổng Công ty Khí…" |
| KDC | "khu dân cư" | 71 | "Kido" |
| VND | đồng currency | 27 | "VNDirect" |
| PAN | place names ("La Pan Tẩn"), "pan" | 6 | "PAN Group", "Tập đoàn PAN" |
| BID | "bid"; listicles | 2 | "BIDV" |

**Drop bare ticker (borderline collisions, low volume; real events covered by
name):**

| Ticker | Collides with | Bare matches |
|---|---|---|
| BMP | military vehicle "BMP-1/BMP-2" | 4 |
| POW | "pow"/"power" | 2 |
| SIP | "sip" | 3 |

**Keep bare ticker (audit confirmed the bare matches are real, not word
collisions):** `REE` (Cơ Điện Lạnh REE), `DIG` (DIC Corp — all 9 real),
`GEX` (Gelex), `EIB` (Eximbank), `SAB` (Sabeco), `CII`, `ANV`. Their residual
false positives (a few body/listicle hits) belong to the out-of-scope
aboutness problem, not to this spec.

This list is the **initial** blocklist; the criterion and the audit method
are the durable part. New tickers are classified by the same lexical +
audit process.

### Mechanism

1. **Central blocklist file** — `esg-collector/config/ambiguous_tickers.json`
   (a JSON list of ticker strings). One file to audit and maintain; avoids
   editing ~100 alias files and avoids re-running the Vietstock builder.
2. **Enforce at alias load** — in
   [`core/alias_matcher.py` `reload()`](../../../esg-collector/core/alias_matcher.py),
   when building the per-ticker alias set, drop any `names[]` entry that
   (case-insensitively) equals the ticker code **iff** the ticker is in the
   blocklist. All other aliases (full name, brand, subsidiaries, projects)
   are unaffected. This is data-independent: it works on the existing alias
   JSONs with no regeneration, and naturally takes effect on the next match
   run.
3. **Keep the builder consistent (defense in depth)** — update
   `alias_builder/fetch_vietstock.py` to consult the same blocklist so that a
   future `--all` regeneration does not silently re-introduce a dropped bare
   ticker. (The builder still *writes* the ticker into the JSON for
   provenance; the loader is the single enforcement point. Decide during
   implementation whether the builder should also omit it — the load-time
   guard is authoritative either way.)

This keeps a single source of truth (the blocklist) and a single enforcement
point (the loader), so the rule cannot be partially applied.

## Rollout (hard dependency on rematch)

The code/config change is small, but its **visible effect requires a
rematch**: the ~600 stale bare-ticker matches already written to
`per_ticker/*.json` and `articles.db` are only purged when the matcher
re-runs over the stored corpus (reading the already-stored body — **no
re-fetch**). A bare-ticker match that has no other alias support must flip to
`unmatched`.

This depends on the **chunked/detached rematch** described in
[`2026-06-01-esg-collector-rematch-redesign-design.md`](2026-06-01-esg-collector-rematch-redesign-design.md)
being operational. Per the project's memory (`rematch_redesign_deploy_pending`),
that rematch is merged but not yet live, and the old inline `match.timer`
OOM-wedges the e2-micro VM on the pending backlog. **This spec does not
attempt to fix rematch.** The rollout sequence is:

1. Ship the blocklist + loader change (and tests) via the normal
   push-to-`main` deploy (per `esg-collector/CLAUDE.md`, deploy is automated;
   do not SSH manually).
2. Once the chunked/detached rematch is confirmed runnable, trigger a full
   rematch (Actions UI → "Deploy esg-collector" → tick `run_rematch_all`).
3. Re-export `per_ticker/*.json` and the web bucket so the dashboard reflects
   the purge.

Until step 2 runs, **new** articles already get the improved precision; the
**backlog** purge waits on the rematch deploy.

## Verification & success criteria

- **Before/after on the local snapshot.** Re-run the measurement scripts: for
  every blocklisted ticker, bare-ticker matches drop to ~0; total matches
  drop by approximately the blocklisted bare-match count (~600), concentrated
  in HCM/GAS/KDC/VND.
- **Recall safety check.** Confirm the audited real events for **distinctive**
  tickers still match (ACV pollution fine, BAF waste discharge) — these
  tickers are *not* blocklisted, so they must be unaffected.
- **No collateral loss for blocklisted tickers.** Spot-check that real events
  for blocklisted tickers still match via the company name (e.g. a genuine
  Kido story still attributes to KDC via "Kido"; a genuine HSC story via
  "HSC").
- **Unit test** (in the style of
  [`tests/test_rematch.py`](../../../esg-collector/tests/test_rematch.py)):
  given a blocklisted ticker, its bare code does **not** produce a match in
  arbitrary text, while its company-name alias **does**; given a
  non-blocklisted ticker, its bare code **still** matches. Include a
  regression case for `KDC` ("khu dân cư" text must not match KDC) and `ACV`
  ("ACV bị phạt" must still match ACV).

## Risks

- **Blocklist drift / incompleteness.** New tickers (or universe changes) can
  introduce new collisions not on the list. Mitigated by documenting the
  lexical+audit criterion and keeping the list in one auditable file; a
  periodic re-audit using the measurement scripts catches new offenders.
- **A blocklisted ticker's real event lacks the company name.** If a genuine
  ESG article about, say, PV Gas refers to it *only* as "GAS" with no
  name/brand present, it would be lost. The audit found these are rare
  (real events for collision tickers carry the name), but it is a residual,
  asymmetric risk accepted in exchange for removing the much larger junk
  volume.
- **Rollout gating.** The precision win on the backlog is invisible until the
  (separately-blocked) rematch runs; triggering a full rematch before the
  chunked version is live could re-wedge the VM. Sequencing above mitigates
  this.
- **Borderline tickers (BMP/POW/SIP).** Low volume; if a later audit shows
  they carry real ticker-named events, remove them from the blocklist — the
  central file makes this a one-line change.

## Appendix — measurement artifacts

Scripts used (run locally against downloaded GCS data, read-only):
corpus/body-noise analysis, per-ticker match-field breakdown, bare-ticker
recall simulation, and per-ticker audit. Source data:
`gs://esg-scan-data/raw_esg/articles_full_20260528_085453.ndjson` and
`gs://esg-scan-data/per_ticker/*.json`.
