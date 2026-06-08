# ESG Collector — Event clustering / de-duplication

**Date:** 2026-06-08
**Status:** Design approved, pending spec review
**Author:** session with Claude
**Related:** `2026-06-03-esg-enrich-pipeline-design.md` (the enrich + export
pipeline this modifies), `2026-06-04-match-precision-overhaul-design.md`
(upstream match precision).

## Problem

The web dashboard shows **one card per article**, not one card per real-world
event. A single ESG event — e.g. the arrest of ACV chairman Vũ Thế Phiệt, or
the prosecution of Đức Giang Chemicals (DGC) chairman Đào Hữu Huyền — is
reported by dozens of outlets, each with a slightly reworded headline. The
dashboard therefore lists the *same event* 25–50 times.

**Measured (2026-06-08, `C:/Users/dangvu/AppData/Local/Temp/esg-cleanup/articles.db`):**
- 93 web-visible events (`match_status='matched' AND enrich_status='done' AND
  sentiment='risk'`) carry **93 distinct `title_hash`es** — i.e. the existing
  dedup collapses *nothing*.
- The next applied batch (`verdicts_149`, indices 149–247, 96 kept) is
  essentially **three events**: ACV (~25 articles), DGC (~50), Vinaconex/VCG
  (~10).

### Root cause

Dedup today lives in two places, both keyed on **`title_hash`** — a sha1 of the
diacritic-stripped, suffix-stripped, lowercased headline (`core/title.py`):

1. `core/storage.py::insert_article` — `(title_hash, published_at)` dedup at
   insert time.
2. `pipeline/export.py::build_esg_events` — `(ticker, title_hash)` dedup at
   export time (earliest kept).

`title_hash` only matches **verbatim republications** (same headline, char for
char after normalisation). Real outlets reword the headline ("ACV bắt chủ tịch
Vũ Thế Phiệt" vs "Vì sao chủ tịch ACV bị bắt?" vs "ACV thông báo chủ tịch bị
tạm giam"), so every report is a distinct hash → a distinct dashboard card.

### Cost of the status quo

- **Dashboard** is cluttered: the same event repeated dozens of times buries
  the diversity of events.
- **Enrich effort wasted**: the manual backlog grind (Claude judging sentiment
  + writing an English title) and the nightly Groq daily both spend one LLM
  call per *article*. ~1,400 pending articles are dominated by a handful of big
  events, so the bulk of that work is redundant. Groq free tier (100k tok/day)
  is the binding constraint.

## Goals / non-goals

**Goals**
1. The dashboard shows **one card per event** (cluster of articles), with a
   `sources_count` and the list of member sources available for later display.
2. Enrich (manual backlog + nightly daily) judges **one representative per
   event**, not one per article — saving Groq quota and manual effort.
3. Deterministic, in-code clustering so it works identically for the daily and
   any future re-export. No reliance on a human reading titles.
4. Immediate payoff: re-export the already-enriched 189 events → dashboard
   collapses from ~189 cards to an estimated ~40–60.

**Non-goals**
- No frontend redesign. Fewer cards is visible immediately; rendering
  "[+24 nguồn]" is an additive, optional follow-up.
- No cross-ticker clustering. An event is always scoped to a single ticker
  (`ticker` is a required clustering key).
- No semantic/embedding model. A token-overlap heuristic is sufficient for
  Vietnamese news headlines, which share substantial vocabulary within an
  event.
- No schema migration if avoidable (see Open question O1).

## Definition of "same event"

Two matched articles belong to the same event iff **all** of:
- same `ticker` (hard requirement), AND
- published within **`window_days = 10`** days of each other, AND
- (Jaccard similarity of their normalised title token-sets **≥ `jaccard_min =
  0.5`**) **OR** they share a **rare significant token** (a proper-noun-like
  token — length ≥ 4, not a stopword, appearing in few of the ticker's titles —
  intended to catch a shared person/project name like "Huyền" / "Đào Hữu
  Huyền" when one headline is too short to clear the Jaccard bar).

Clustering is the **connected components** (union-find) of the pairwise
"same-event" graph within each ticker. The **representative** of a cluster is
the **earliest-published** article.

Thresholds (`window_days=10`, `jaccard_min=0.5`) are the design defaults and
will be **empirically tuned against the live DB** during implementation:
the known ACV (~25) and DGC (~50) clusters must each collapse to a single
cluster, while any two genuinely distinct same-ticker events must stay
separate. The chosen final values are recorded in the implementation.

## Architecture

### Component 1 — `core/events.py` (new, pure, unit-tested)

```
cluster_events(articles, window_days=10, jaccard_min=0.5) -> list[list[dict]]
```
- Input: an iterable of article dicts that each carry at least
  `article_id`, `ticker`, `title`, `published_at`.
- Groups by `ticker`; within a ticker builds the same-event graph and returns
  connected components as lists of the original dicts, each list sorted
  earliest-first (so `cluster[0]` is the representative).
- Reuses `core/title.normalise()` for tokenisation (already strips diacritics,
  punctuation, and the trailing publisher suffix). A small Vietnamese stopword
  set lives in this module.
- Pure and deterministic: no DB, no network, no clock. This is what makes it
  testable and reusable by both export and enrich.

Helper, same module:
```
event_key(cluster) -> str          # stable id = f"{ticker}:{representative_article_id}"
```
Used to label members of a cluster with their representative.

### Component 2 — export collapse (`pipeline/export.py::build_esg_events`)

- After gathering the **matched** articles per ticker from `per_ticker/*.json`
  (regardless of enrich status — needed so `sources_count` reflects all
  outlets, not only the enriched ones), run `cluster_events` per ticker.
- For each cluster that contains **≥ 1 enriched `sentiment='risk'`** member,
  emit **one** event:
  - representative = earliest **risk** member (falls back to earliest member
    for the displayed date/title), carrying the existing fields plus
  - `sources_count` = number of matched articles in the cluster, and
  - `sources` = list of `{date, source, url}` for each matched member,
    earliest-first.
- A cluster with zero risk members emits nothing (unchanged behaviour: pending
  or dropped events don't show).
- The old `(ticker, title_hash)` `seen` set is **removed** — clustering
  supersedes it. (Verbatim republications fall into the same cluster anyway.)

### Component 3 — enrich skip (`enrich` runner)

- Before judging an article, compute its cluster within the candidate pool for
  its ticker. If a **same-cluster** article is already
  `enrich_status='done'` (risk or dropped), **inherit** that verdict
  (`sentiment` + reuse the representative's `summary_en`) and mark this article
  done **without an LLM call**.
- Otherwise judge normally; this article becomes the cluster representative and
  its verdict is what later members inherit.
- Net effect: both the nightly daily and the manual backlog spend one LLM call
  per event instead of per article.

### Component 4 — manual backlog helper (one-off, not deployed)

- A script (in the temp working dir, alongside `apply_enrich.py`) that clusters
  the pending matched pool and prints **one representative per cluster** for
  Claude to judge, plus the member `article_id`s so the verdict can be fanned
  out to the whole cluster. Reduces the remaining manual grind from ~1,400
  articles to an estimated ~200 events.

## Data flow

```
per_ticker/*.json (matched articles: id, ticker, title, date, url, source)
        │
        ▼
core/events.cluster_events  ──────────────┐
        │                                 │
        ▼ (export)                        ▼ (enrich)
build_esg_events:                   runner: representative →
  1 card / cluster w/ risk            LLM judge; members →
  + sources_count + sources           inherit verdict (no LLM)
        │
        ▼
web/esg_events.json  →  gs://esg-scan-data/web/  →  dashboard (fewer cards)
```

## Error handling / edge cases

- **Missing/empty `published_at`**: treated as "no date" → cannot satisfy the
  ≤10-day window with a dated article, so it only clusters with other dateless
  articles of the same ticker that meet the title test. It is never silently
  merged across a wide time gap.
- **Empty/very short title**: `normalise()` yields a tiny token set; Jaccard
  with anything is low and it has no rare token → it stays a singleton cluster
  (correct: we can't prove it's the same event). Matches the existing
  `title_hash` guard that returns `''` for titles `< 8` chars.
- **Singletons**: a one-article event is a valid cluster of size 1; it emits as
  one card with `sources_count = 1`.
- **Inheritance correctness**: a member inherits the representative's verdict
  only within the same `(ticker, cluster)`. Because clustering requires same
  ticker + tight time window + title overlap, an inherited "risk/drop" applies
  to genuinely the same story. Over-merge risk is bounded by the tuned
  thresholds and verified by tests.
- **Determinism under incremental enrich**: the runner clusters within the
  current candidate pool. As long as cluster membership is stable for a given
  set of articles (it is — pure function of their fields), order of processing
  does not change the final per-cluster verdict; the first-judged article is
  the representative and the rest inherit.

## Testing strategy

- **Unit (`core/events.py`)**: synthetic articles — verify (a) two reworded
  same-event headlines within window cluster together; (b) two different events
  same ticker/week stay separate; (c) the window boundary (11 days apart →
  separate); (d) the rare-token OR-clause merges a short headline that shares a
  name but fails Jaccard; (e) singletons and empty-title guards.
- **Export (`pipeline/export.py`)**: extend `tests/test_enrich.py`-style
  fixtures — a cluster of 3 reworded risk articles emits **one** event with
  `sources_count = 3`; a non-risk cluster emits none.
- **Empirical tuning on live data**: run `cluster_events` over the current
  `articles.db` + `per_ticker`; assert ACV → 1 cluster, DGC → 1 cluster, and
  spot-check that no two distinct events were merged. Record the final
  thresholds.
- **Regression**: existing `tests/test_enrich.py` title_hash collapse case must
  still pass (verbatim dups land in one cluster).

## Rollout

1. Land `core/events.py` + export change + enrich change + tests on the current
   feature branch; run the suite.
2. **Immediate cleanup**: locally `python -m pipeline.export --web` against the
   enriched temp DB → `esg_events.json` shrinks (~189 → ~40–60) → `gcloud
   storage cp … gs://esg-scan-data/web/esg_events.json --predefined-acl=publicRead`.
   (Re-upload `articles.db` first per the enrich-backlog resume note so a daily
   run re-exports from the enriched DB.)
3. Merge to `main` to deploy the clustered export + enrich-skip to the nightly
   daily (per `esg-collector/CLAUDE.md`: push to main = deploy).
4. Continue the manual backlog using Component 4's representative list.

## Open questions

- **O1 — store `event_key` or compute on the fly?** Default: compute on the fly
  (no schema migration; the spec assumes this). If the enrich runner's
  per-article re-clustering proves too slow on the full pool, fall back to
  persisting an `event_key` column (guarded `ALTER TABLE` in
  `storage.init_db()` per `CLAUDE.md`) populated once and maintained on insert.
  Decide during implementation based on measured runner cost.
- **O2 — frontend source count.** Out of scope here; `sources_count` /`sources`
  are emitted so the web UI can render "[+N nguồn]" in a later, additive change.
