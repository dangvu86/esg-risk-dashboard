# Body extraction overhaul — trafilatura

**Date:** 2026-06-08 · **Status:** approved (user) · **Scope:** `esg-collector/body_fetcher` + worker

## Problem

Tier-2 alias matching runs on the article `body`. The body is captured by Jina as
**full-page markdown** (nav, ad banners, "related-news" lists, footer). The current
cleaner (`body_clean.strip_related_blocks`) only drops bullet-list lines, so 60–80%
of every body is junk. Stray company names in that junk get matched → false positives.

**Measured (dump `articles_full_20260528`, 61,998 rows, current code replayed):**
- 47% of all attributions come from `body`; Brave-sourced = 88% body-only.
- 6/6 sampled web FPs (HDB←"tai nạn lao động", VNM←"trốn thuế", DGW←"cháy nhà dầu khí")
  **still match today** — all on `body`, where the brand sits in an ad/related/nav block
  (e.g. "Thế giới số" = laodong.vn's tech-section nav link, not Digiworld).

## Decision

Replace "Jina markdown + line-strip" with **raw HTML → `trafilatura.extract`**.
trafilatura is a purpose-built main-content extractor: it drops boilerplate
(nav/ads/related/footer) across arbitrary VN sites with no per-site config.

**Validated on 11/12 top VN domains (live):** direct `requests` (browser headers)
fetched 11/12; trafilatura shrank 80–760K-char pages to 1–9K-char articles and
removed stray company mentions (tuoitre/cand/dantri/znews: 1 ticker in raw HTML → 0
in extracted article). Only laodong.vn blocked direct → needs the Jina fallback.

## Changes (`esg-collector/`)

| File | Change |
|---|---|
| `requirements.txt` | add `trafilatura>=2.0` |
| `body_fetcher/extract.py` | **new** — `extract_main(html) -> str\|None` (trafilatura, `favor_precision=True`, min 200 chars) |
| `body_fetcher/fallback.py` | rewrite → **direct HTML fetcher** (primary): requests + browser headers, resolves Google links, returns raw HTML (drop bs4 `_extract`) |
| `body_fetcher/jina.py` | rewrite → **Jina HTML fetcher** (fallback): `X-Return-Format: html`, keep token-bucket pacing (drop selector/markdown retry) |
| `workers/body_fetcher.py` | `_fetch_one`: direct → Jina fallback → `extract_main`; drop `strip_related_blocks` |

**Fetch order flips:** direct requests is now **primary** (free, no rate limit, works
11/12); Jina is the **fallback** for blocked sites / undecoded Google links.

`body_clean.py` + `pipeline/clean_bodies.py` are left in place (dead, harmless) — their
tests still pass; remove in a later cleanup.

## Tests (plain functions, `python -m tests.test_*`)

- `test_extract.py` **new** — HTML with an article + a nav block naming a company →
  `extract_main` returns the article, drops the nav company.
- `test_fetch_one_clean.py` — rewrite: mock `fallback.fetch`→HTML + `extract.extract_main`
  → `_fetch_one` returns extracted text; on direct fail it falls back to `jina.fetch`.
- `test_jina_headers.py` — rewrite: `jina.fetch` sends `X-Return-Format: html`, returns HTML.

## Rollout

New fetches are clean automatically. Old `body` rows are still junky markdown
(trafilatura can't re-clean markdown) — to fix existing FPs, a one-time **re-fetch**
of `body_status='fetched'` rows + **rematch** is needed. That is a separate operational
step, out of scope for this code change.

## Out of scope

- Dropping body from attribution entirely (alternative fix ①) — we keep body matching,
  just on clean text.
- "Aboutness" FPs where a company is genuinely named in the real article body but the
  event isn't about it — a separate precision concern.
