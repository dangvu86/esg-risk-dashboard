# ESG Collector — Match precision overhaul (4 fixes)

**Date:** 2026-06-04
**Status:** Design approved, pending spec review
**Author:** session with Claude
**Supersedes:** `2026-06-04-bare-ticker-precision-design.md` (Fix 1 below is that
spec, now one part of a four-part program).

## Problem

A large share of company↔article attributions produced by the matcher are
**false positives**. A measurement on the live corpus (2026-06-04,
`gs://esg-scan-data`, pulled with `dangvule@gmail.com`) — full snapshot
`raw_esg/articles_full_20260528_085453.ndjson` (61,998 articles) and
`per_ticker/*.json` (3,617 matches) — shows **three distinct causes**, each
fixed by a separate, independently-testable change:

1. **Collision aliases.** Bare ticker codes are matched as if as distinctive
   as the full company name. `matched_alias == ticker code` = **1,427
   matches (39.5%)** across title 720 / desc 281 / body 426. The same flaw
   exists for **generic name/subsidiary fragments** ("Delta", "Apatit",
   "Phát triển Đô thị", "BH"). → **Fix 1** (tickers) + **Fix A** (fragments).

2. **Noisy body.** Jina stores the **full-page markdown**, including the
   trailing "related news / recommendations" widget (rendered as a markdown
   image/link list). Aliases inside *other articles' headlines* in that block
   match the current article. Concrete: the article *"Ô nhiễm nghiêm trọng
   tại kênh hào thành cổ Vinh"* (baotintuc.vn, body 51,529 chars) was
   attributed to **BCM, DXG, HVN, VRE** — all matched at body positions
   0.79–0.90 inside `* [![Image …](url)` link-list lines, none present in the
   actual article. **47%** of resolved body matches sit in such markdown
   link/image lines. → **Fix C** (clean body).

3. **Roundup / listicle articles.** One article lists many companies (vaccine
   donor lists, bank rankings, "which bank is best?"). **22%** of all matches
   are in articles attributed to **≥3 distinct companies**, and **774 of those
   800** are not in the title (the company is merely listed in body/desc). →
   **Fix B** (roundup/aboutness gate).

### Key measurements (for reference)

Corpus body_status: fetched 7,105 (11%) · **failed 53,384 (86%)** · skipped
1,186 · pending 323. Matches by field: title 43% · desc 15% · **body 41%**.

Body-match noise (598 resolved body matches): **47% in markdown link/image
lines** (nav/related), 42% in the trailing 40% of the body, **30% in
prose & not-trailing** (the real matches a cleaner must keep).

Companies-per-article: 1 co → 2,333 articles; 2 co → 242; **≥3 co → ~172
articles = 800 matches (22%)**, of which **774 not in title**. The ≥4-company
articles are, by inspection, all roundups (vaccine donor list = 22 companies;
"richest private firms" ranking = 14; "which bank is best" listicles).

### Why not the originally-assumed single fix

- "Remove all bare tickers" would unmatch real events for ticker-named firms
  (ACV pollution fine, BAF waste discharge) — a simulation showed 95.5% of
  bare-ticker matches would unmatch and ~half are real. Removal must be
  **surgical** (Fix 1).
- A pure text-marker body cut ("Tin liên quan"/"Xem thêm") only sees 15.8% of
  bodies, because Jina renders the related block as a **markdown link list**,
  not a text header. The right body signal is the link/image-list structure
  (Fix C), not the text markers.

## Goal

Raise precision by removing the collision, body-noise, and roundup false
positives, while **keeping recall flat** — no dropping of real ESG events.
Each fix is independently shippable and testable; they compose.

---

## Fix 1 — Drop ambiguous bare-ticker aliases (collision)

The builder injects the ticker as `names[0]` for every company
([`alias_builder/fetch_vietstock.py:315`](../../../esg-collector/alias_builder/fetch_vietstock.py)),
and the matcher treats every `names[]` entry as a strong alias (weight 1.0).
~40 of ~100 tickers collide with common words/cities/currency (GAS=gas,
HCM=city, KDC="khu dân cư", VND=đồng). Drop the bare ticker **only** for those.

**Classification: lexical + audit** (not frequency — a junk-proxy over-flagged
distinctive tickers like FPT/SHB/DIG/REE). Audited drop list:

- **Drop (confirmed; real events covered by the company name):** GAS, KDC, VND,
  PAN, BID. (`HCM` already removed from `HCM.json` by a prior fix — keep it on
  the list as belt-and-suspenders, 0 incremental.)
- **Drop (borderline, low volume):** BMP (military "BMP-1"), POW, SIP.
- **Keep (audit shows bare matches are real, not collisions):** REE, DIG, GEX,
  EIB, SAB, CII, ANV.

Incremental impact ≈ **268 matches** (GAS 153 + KDC 71 + VND 27 + small), HCM's
330 already config-removed.

## Fix A — Drop generic name/subsidiary fragments (collision)

**Same mechanism as Fix 1.** Some `names`/`subsidiaries`/`projects` aliases are
generic words that match unrelated text: e.g. `VHM <- "Delta"` (×21),
`DGC <- "Apatit"` (×14), `BCM <- "Phát triển Đô thị"`, `SBT <- "BH"` (×26).
Distinctive brand names (Vinhomes, Novaland, Sacombank, BIDV, VEAM…) are the
**majority of non-ticker matches and must be kept** — only the generic tokens
are dropped. Built by the same lexical + per-ticker audit; the audit must scan
all strong aliases for generic surface forms.

### Mechanism (Fix 1 + A share one stoplist)

1. **One central stoplist** `esg-collector/config/ambiguous_aliases.json` — a
   JSON array of upper-cased surface forms holding **both** collision tickers
   **and** generic fragments, e.g.
   `["GAS","KDC","VND","PAN","BID","BMP","POW","SIP","HCM","DELTA","APATIT","BH","PHÁT TRIỂN ĐÔ THỊ"]`.
   Add a settings constant following the `ROOT / "config" / ...` convention:
   `settings.AMBIGUOUS_ALIASES_PATH = ROOT / "config" / "ambiguous_aliases.json"`.
2. **Enforce at alias load** — in
   [`core/alias_matcher.py` `reload()`](../../../esg-collector/core/alias_matcher.py).
   `reload()` does not build a per-ticker structure; it flattens every alias
   into the global `_OWNERS` map + `strong`/`alla` sets in a per-file loop
   where `ticker` is in scope (alias_matcher.py:63-82). At the top of
   `reload()` load the stoplist into a module-level `set[str]` (upper-cased),
   guarded by try/except so a missing/malformed file → empty set + a logged
   warning (the module auto-calls `reload()` at import, line 135). Then, in the
   per-field loop, **before** the alias is added to `_OWNERS`/`alla`/`strong`
   (line 77), skip any `names`/`subsidiaries`/`projects` value whose
   `.strip().upper()` is in the stoplist.
   - **`_NESTED` invariant:** the overlapping-substring recovery is built by
     iterating `alla` (alias_matcher.py:83) and reading `_OWNERS.get(bl, ())`
     (line 86). Because the stoplisted surface is skipped before it is added to
     `alla` (and hence absent from `_OWNERS` and from the `_NESTED` build), it
     contributes nothing to nested recovery — a stoplisted token embedded in a
     longer alias is suppressed as the bare token while the longer alias still
     matches. No extra code needed; stated for auditability.
   - **Global (non-per-ticker) stoplist:** a surface is dropped for **every**
     company that holds it, not per-ticker. The audit must confirm each
     stoplisted surface is generic across every company using it. (Today each
     listed surface lives in a single company's JSON, so this is moot.)
3. **Builder consistency (OPTIONAL — loader is authoritative)** — optionally
   make `fetch_vietstock.py` consult the same stoplist so a future `--all`
   regeneration does not re-introduce a dropped surface. Provenance for the
   ticker is preserved by the top-level `"ticker"` JSON key (line 340).

## Fix C — Clean article body (noise)

Stop matching (and enriching on) the related-news/sidebar/footer block.

1. **New fetches — fetch clean.** In
   [`body_fetcher/jina.py`](../../../esg-collector/body_fetcher/jina.py) add
   Jina `X-Target-Selector` (the article-content selectors already curated in
   [`body_fetcher/fallback.py:19-28`](../../../esg-collector/body_fetcher/fallback.py):
   `div.detail-content, div.entry-content, div.fck_detail, …`) and/or
   `X-Remove-Selector` for known related/sidebar/footer containers, so the
   stored body is the article text only. **Fallback:** `jina.fetch` today
   returns `(None,"failed")` on an empty body (jina.py:77-79) with no retry.
   Add: if the target-selector response is empty or short (`len(body) < 200`,
   matching `fallback.py:41`), re-issue the request **once** with the selector
   headers removed and return that result — so coverage does not regress on
   sites the selectors don't cover.
2. **Old bodies — clean in place, no re-fetch.** A **one-shot backfill**
   (standalone script, or a pre-step of the rematch) over stored bodies with
   `body_status='fetched'`. Delete, **line-by-line over the whole body** (not a
   positional cut — related blocks also appear mid-body), any line that, after
   `lstrip`, matches a markdown link/image list item:
   `^[\*\-]\s*\[?!?\[?Image` **or** `^[\*\-]\s+\[.*\]\(https?://`
   (covers `* [![Image N: …](url)` and bare `* [text](http…)` items). Re-store
   via `storage.mark_body(conn, aid, "fetched", cleaned)` (storage.py:242-247).
   Because this is a **non-idempotent data backfill**, gate it behind an
   `export_state` flag (`storage.get_meta`/`set_meta`, storage.py:462-474) so a
   redeploy/re-run does not re-strip — per `esg-collector/CLAUDE.md` ("data
   backfills … gated behind a metadata flag in `export_state`"). Acceptance
   bar: ~47% link-line body matches removed, ~30% prose body matches kept.
3. **Benefit:** a clean stored body improves **both** matching (Fix C target)
   **and** enrichment (the LLM controversy classifier reads `body[:6000]` —
   [`enrich/controversy.py:27`](../../../esg-collector/enrich/controversy.py);
   today sidebar can eat that budget).

**Coverage & caveats:** Fix C removes the **~47%** of body matches that live in
link/image lines (the Vinh-moat class). It does **not** remove prose-listed
companies (handled by Fix B) — keep the **~30% prose** body matches. Per-site
selector variance is handled by the empty-response fallback.

## Fix B — Roundup / aboutness gate (listicle)

In [`pipeline/match.py` `_process_article`](../../../esg-collector/pipeline/match.py):
**if the article matched ≥3 distinct companies, drop any hit whose
`location != "title"`.**

- Within one article, `len(hits)` already equals the number of distinct
  companies (match_article returns ≤1 hit per ticker). So the rule is local:
  `if len(hits) >= 3: hits = [h for h in hits if h.location == "title"]`.
- **Placement:** insert immediately **after** `verdict = esg_filter.classify(...)`
  (match.py:107) and **before** the `if hits and verdict.keep` test (line 108),
  so that if the filter empties `hits` the article correctly routes to the
  `unmatched`/`deferred` branch instead of being a matched row with zero hits.
- **Works on cached hits too:** `cached_hits` (Stage-1 hits from the body
  fetcher pre-check) preserve `location` (serialized via `asdict`, rehydrated at
  match.py:78), so the title-guard applies to them as well. **No guard is
  needed in `body_fetcher._prefetch_hits`** — pre-check hits flow through
  `_process_article`; do not duplicate the rule there.
- Removes ~**774** roundup/donation/ranking false positives (vaccine donor
  lists, bank rankings, "which bank is best?"). Recall risk is low: a real
  single-company ESG event rarely co-names 3+ *other tracked* companies, and a
  company that is the article's subject is usually in the title (26 such
  in-title matches in ≥3-company articles are kept).
- **Threshold (≥3) and the not-in-title guard are tunable** — start
  conservative; revisit ≥2 only after checking recall.
- **Ordering / what the ≥3 count reflects:** Fix 1+A act at alias load, so the
  count is always on a ticker/fragment-cleaned hit set. Fix C: for
  **body-matched** articles the count also reflects the cleaned body; for
  **cached-hit (pre-check, `skipped`-body)** articles the body was never matched
  (match.py:96-104 skips Stage-2 when cached hits exist), so Fix C is a no-op
  there and the ≥3 count comes from already-clean title/desc/sapo hits. Either
  way the count is on a Fix-1/A-cleaned set — the earlier "reflects Fix C on the
  body" shorthand only holds for live body-matched articles.

## Rollout (shared dependency: rematch)

The code/config changes are small, but their effect on **already-stored**
matches requires a **rematch** — re-running the matcher over the stored corpus
(reading the stored body from `articles.db`, **no re-fetch**). Fix C
additionally needs its **one-time in-place body clean** (also no re-fetch)
before/with the rematch.

Rematch depends on the **chunked/detached rematch**
([`2026-06-01-esg-collector-rematch-redesign-design.md`](2026-06-01-esg-collector-rematch-redesign-design.md))
being operational; per memory `rematch_redesign_deploy_pending` it is merged
but not yet live (old inline `match.timer` OOM-wedges the e2-micro VM). **This
spec does not fix rematch.** Deploy is push-to-`main` (automated; do not SSH —
see `esg-collector/CLAUDE.md`). Suggested sequence:

1. Ship **Fix 1 + A + C** (all low recall-risk) + tests.
2. Run the in-place body clean + a rematch once the chunked rematch is runnable.
3. Re-measure the residual, then ship **Fix B** (also low-risk per measurement;
   may ship together with 1+A+C if preferred).

## Verification & success criteria

Re-run the measurement scripts before/after on the local snapshot.

- **Fix 1/A:** stoplisted surfaces produce 0 matches; total drops ≈ the
  stoplisted bare/fragment count (~268 for tickers + the audited fragments),
  concentrated in GAS/KDC/VND/Delta/Apatit. Keep checks: distinctive tickers
  (ACV/BAF) and distinctive names (Vinhomes/Novaland) still match.
- **Fix C:** the Vinh-moat article no longer attributes to BCM/DXG/HVN/VRE; the
  ~47% link-line body matches disappear; the ~30% prose body matches remain.
- **Fix B:** the ≥3-company roundup articles (vaccine donor list, bank
  rankings) lose their non-title attributions; single-company articles are
  untouched; in-title attributions in multi-company articles are kept.
- **Overall:** total matches drop; spot-check a random sample of *dropped*
  matches are junk and a sample of *kept* matches are real (no recall loss).
- **Unit tests** (hermetic, in the style of
  [`tests/test_rematch.py`](../../../esg-collector/tests/test_rematch.py) which
  monkeypatches `settings.PER_TICKER_DIR`):
  - 1/A: monkeypatch `settings.AMBIGUOUS_ALIASES_PATH` to a temp file; a
    stoplisted surface (KDC / "Delta") doesn't match while the company name
    ("Kido" / "Vinhomes") does; a non-stoplisted ticker (ACV) still matches.
  - C: a body containing only a `* [![Image …](url)` related block + a short
    prose lead → the cleaner keeps the prose, drops the link block; a prose
    company mention survives.
  - B: an article hitting 3 companies in body only → all dropped; an article
    hitting 3 companies with one in the title → only the title one kept; a
    2-company article → untouched.

## Out of scope / residual

- **Same-name, different entity** (e.g. *Viettel Post* `VTP` vs *Viettel FC*
  football vs *Viettel Group*) — a shared distinctive name; none of the four
  fixes resolves it. Needs entity disambiguation; tracked as residual.
- **86% body-fetch failure** — a separate reliability issue (rate limits, dead
  2020–2021 links); not addressed here.
- **2-company articles** — Fix B is conservatively ≥3; revisit after recall
  checks.
- **Fixing rematch infrastructure** — a hard dependency, owned by the rematch
  redesign spec.

## Appendix — measurement artifacts

Read-only scripts run locally against downloaded GCS data:
`analyze.py` (corpus/body-noise), `analyze_pt3.py` (match-field + bare-ticker
breakdown), `sim.py` (bare-ticker recall simulation), `audit.py` (per-ticker
audit), `nonticker.py` (fragment/aboutness scan), `diag.py` (the Vinh-moat
case), `measure_body.py` (body link-line vs prose), `measure_b.py`
(companies-per-article / roundup signal). Source:
`gs://esg-scan-data/raw_esg/articles_full_20260528_085453.ndjson` and
`gs://esg-scan-data/per_ticker/*.json`.

These were **throwaway local scripts** (run under a temp dir, not committed).
The "re-run the measurement scripts before/after" success criteria mean
re-pulling the same GCS data and re-running equivalent checks — not invoking a
committed harness. If reproducibility matters, commit them under
`esg-collector/scripts/` during implementation.
