# ESG Collector — Rematch Redesign (run safely on e2-micro)

**Date:** 2026-06-01
**Status:** Design approved, pending spec review
**Author:** session with Claude

## Problem

`pipeline.match --rematch-all` re-attributes the entire article corpus
(~50k rows) against every ticker's alias pool. It is invoked **inline,
synchronously**, inside the GitHub Actions deploy job's SSH call. On the
free-tier **e2-micro VM (0.25 vCPU baseline, 1 GB RAM)** this fails three
different ways, all observed in production on 2026-06-01:

1. **OOM.** `pipeline/match.py:109` does `pending = list(iter_articles(...))`,
   materialising all ~50k articles — including multi-KB `body` text — into a
   Python list at once. Estimated 0.5–1 GB+, exceeding the 1 GB VM. The serial
   console showed a real `Out of memory: Killed process (python)` event.
2. **CPU too slow → CI timeout.** The matcher
   (`core/alias_matcher.py:84-95`) loops every ticker × every alias regex for
   every article × every field — roughly `50_000 × 100 × ~6 × 4 ≈ 120M`
   `regex.search()` calls. On 0.25 vCPU this runs >40 min. Sustained CPU also
   exhausts the burstable instance's CPU credits, throttling it further. Deploy
   runs #6 (15 min cap) and #8 (40 min cap) were both **cancelled mid-rematch**.
3. **Cancellation → orphan → wedge.** When CI kills the job, the SSH session
   dies but the remote rematch python orphans and keeps pegging the CPU, leaving
   the VM unresponsive (SSH/IAP refused) until a hard reset. This happened twice.

### Why backfill works but rematch does not

Backfill (the heavy 5-year historical collection) runs fine on the same VM
because it uses a fundamentally different pattern: the deploy step only
**enqueues** tasks (`queue_builder --mode backfill`, returns in ~1 s — deploy
run #4 took 1 m 01 s total) and the **persistent worker services** drain the
queue gradually, throttled (15–25 s/request), network-bound, CPU mostly idle.

Rematch is the one batch operation shoehorned into a synchronous, CPU-bound,
inline CI call. The redesign makes rematch behave like backfill: bounded
memory, lighter CPU, and detached from the CI wait.

## Goals / Non-goals

**Goals**
- Rematch completes on the current e2-micro without OOM, without wedging the
  VM, and without depending on the CI job staying connected.
- No change to *which* articles match which tickers (behaviour-preserving
  performance work), verified by an automated equivalence test.
- No new runtime dependency on the VM.

**Non-goals**
- Resizing or replacing the VM.
- Changing alias content, the ESG filter, or the per-ticker output schema.
- Reworking the steady-state incremental `match.timer` path beyond what the
  shared code changes naturally give it (it benefits for free).

## Design

Three independent changes plus test/observability scaffolding.

### 1. Single-pass matcher (`core/alias_matcher.py`)

Replace the per-ticker-per-alias `regex.search()` loop with **one combined
alternation pattern** built once at load time:

- Build `(?<!\w)(esc_alias_1|esc_alias_2|…)(?!\w)` over all strong aliases
  (and weak, when `include_weak`), reusing the **exact same lookaround and
  `re.IGNORECASE | re.UNICODE` flags** as today — so Vietnamese-diacritic word
  boundaries and case behaviour are byte-for-byte preserved.
- Keep a `dict[str_lower → list[(ticker, alias, weight)]]` to map each match
  back to its owning ticker(s).
- `match_text` / `match_article` run `finditer` once per text field, collect
  the set of matched alias strings, resolve tickers, and return the same
  `AliasHit` objects with the same "≤1 hit per ticker, location = first field
  it appeared in" semantics.
- **Public API (`match_article`, `match_text`, `reload`, `loaded_tickers`,
  `AliasHit`) is unchanged**, so `pipeline/match.py` and any other caller need
  no edits.

**Overlap edge case:** alternation returns one match per position, so if two
aliases for *different* tickers overlap at the same position (one a substring
of the other) the old per-alias loop could report both while alternation
reports one. Mitigation: order alternatives longest-first so the longer alias
wins, and let the equivalence test (below) surface any residual discrepancy; if
found, fall back to a secondary scan only for the affected overlapping alias
set. This is expected to be rare-to-nonexistent in the current alias data.

### 2. Chunked / streamed rematch (`pipeline/match.py`)

- Remove `pending = list(iter_articles(...))`. Iterate the generator directly,
  processing in **batches of ~2000 articles**: match → mark → accumulate
  per-ticker docs → `conn.commit()` per batch → release the batch from memory.
- Per-ticker accumulation stays in memory (it holds only the *matched* subset,
  a few thousand entries) and is written once at the end as today. The OOM
  source is the *input* list, which streaming eliminates → peak RAM drops to
  tens of MB.
- `--rematch-all`'s "reset all to pending + wipe per_ticker/" prelude is
  unchanged. `--since` / `--limit` keep working.

### 3. Detached rematch deploy step (`.github/workflows/deploy-esg-collector.yml`)

Replace the inline `if [ "$REMATCH" = "1" ]; then … pipeline.match
--rematch-all … fi` with a **fire-and-return** launch:

```bash
systemd-run --no-block --unit=esg-rematch --uid=esg \
  --setenv=PYTHONUNBUFFERED=1 \
  /opt/esg-collector/.venv/bin/python -m pipeline.match --rematch-all --managed
```

A new `--managed` flag (or a small wrapper) makes the run own its lifecycle on
the VM: stop the four workers → rematch (chunked) → export+upload → restart
workers → write the status file. The deploy job returns in seconds (like
backfill), so the CI step no longer holds a long SSH wait and can never be
killed mid-rematch. The `trap restart_workers` safety net added earlier stays
as defence in depth for the *deploy* script itself.

### 4. Observability (status file on GCS)

The managed rematch writes `gs://esg-scan-data/_setup/rematch_status.json` at
start and end:

```json
{"state":"running|done|failed","started_at":"…","finished_at":"…",
 "counts":{"matched":N,"unmatched":N,"deferred":N},"error":"…"}
```

Polling this file from the local box (already has GCS access via
`dangvule@gmail.com`) gives progress and completion without SSH.

## Testing

- **Matcher equivalence (local, pytest):** run the OLD matcher and the NEW
  matcher over a representative corpus (sample real titles/snippets/bodies,
  including the HCM/FRT/DGC cases already captured under `_health/`) and assert
  **identical** `{ticker, alias, location}` results for every input. This is the
  gate that proves the rewrite changes nothing about *what* matches.
- **Chunked rematch (local, pytest):** build a small temp SQLite with a known
  set of articles, run `run(rematch_all=True)` against it, assert correct
  matched/unmatched/deferred counts and correct per-ticker JSON output, and
  that it streams (no full-corpus list).
- **Deferred (needs live VM):** the `systemd-run` detached launch and the
  status-file round-trip can only be verified once the VM is back up. This is
  the single remaining manual verification, performed on the first real
  detached rematch after deploy.

## Rollout (collector currently down)

1. Implement + test + commit on branch `rematch-redesign` — no VM dependency.
2. User resets the VM via GCP Console (current `reset` permission is denied to
   the CLI account) → collector back online via systemd-enabled workers.
3. Merge / push → auto-deploy: `git reset --hard` restores the committed clean
   aliases (incl. the HCM/FRT fixes) and ships the new code.
4. Trigger the now-safe detached rematch (`run_rematch_all`) → historical data
   cleaned; watch the GCS status file.

## Risks

- **Matcher behaviour drift** — mitigated by the equivalence test gate; we do
  not ship the new matcher unless it matches the old one exactly.
- **systemd-run availability / uid** — `systemd-run --uid=esg` must be allowed
  on the VM; verify on first live run. Fallback: a dedicated oneshot service
  unit shipped in `deploy/`.
- **Detached job ↔ deploy race** — the managed rematch stops/starts workers; a
  concurrent push-deploy also touches workers. The existing `concurrency` group
  on the workflow plus the rematch unit name (`esg-rematch`, single instance)
  bound this; document "don't trigger a deploy while a rematch is running".
