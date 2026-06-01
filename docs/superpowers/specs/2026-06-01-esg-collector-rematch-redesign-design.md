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

**Pattern + scan strategy — consuming `finditer`, longest-first.** Build a
single `re.compile((?<!\w)(?:alias_1|alias_2|…)(?!\w), re.IGNORECASE |
re.UNICODE)` with alternatives ordered **longest-first**, and scan each field
with a normal **consuming `finditer`**. `match.group()` gives the matched alias
text; a `dict[str_lower → list[(ticker, alias, weight)]]` maps it back to its
owning ticker(s). This is the fast path: one pass per field, and CPython's `re`
first-character optimisation skips non-matching positions, so it avoids the
600-separate-`search()` overhead.

**Why not the zero-width lookahead.** An earlier design used
`(?=((?<!\w)(?:…)(?!\w)))` to catch *every* overlapping occurrence (exactly
reproducing independent per-alias search). But a zero-width match forces the
engine to advance one character at a time and **disables the first-char
skip-ahead**, making it as slow as the old loop — defeating the whole point.
We deliberately use the consuming scan instead.

**Known, bounded behaviour difference.** A consuming scan is non-overlapping
(leftmost-longest), so it diverges from the old per-alias search in exactly one
rare case: a short alias of ticker A that appears *only* nested inside a longer
alias of ticker B at the same position (and never standalone elsewhere in the
text). Longest-first ordering makes the longer alias win there. In practice the
short alias (usually a company's own short name) also occurs standalone, so its
ticker still matches. The equivalence test (below) **measures** this divergence
on the fixture and on a larger real sample; the gate is "zero or
explainable-as-more-correct divergences," not byte-identity.

**Performance expectation (honest).** The algorithmic win from single-pass
matching is **modest (~2–5×)**, not an order of magnitude — true 10×+ would need
an Aho-Corasick/trie engine, which was rejected to avoid a new VM dependency.
The changes that actually make rematch *complete* on the e2-micro are the
chunking (§2, removes OOM) and the detached run (§3, removes the CI-timeout
kill + orphan wedge). Expect a full rematch to take ~10–20 min running detached
on the VM — fine, because nothing is waiting on it synchronously.

### 2. Chunked / streamed rematch (`pipeline/match.py`)

**Cursor strategy (must not stream-while-mutating).** `storage.connect()` opens
with `isolation_level=None` (autocommit) + `PRAGMA journal_mode=WAL`. The match
loop UPDATEs `match_status` — the very column the input query filters on — so we
must **not** iterate a live `SELECT … WHERE match_status='pending'` cursor while
flipping those rows. Instead:

1. **Snapshot the work list first:** `SELECT article_id FROM articles WHERE
   match_status='pending' [AND published_at >= since] [LIMIT n]` into a list of
   ids. Ids are tiny (~50k × ~40 B ≈ 2 MB) — safe to hold fully.
2. **Paginate by id:** walk the id list in slices of `BATCH_SIZE` (a named
   constant, default 2000). For each slice, fetch the *full* rows, match them,
   and `mark_match`/`mark_esg` each. Only one batch of full rows (with bodies)
   is in memory at a time → peak RAM tens of MB.
   - `storage.iter_articles` today filters only by `match_status`/`body_status`/
     `since`/`limit` and has no by-ids mode — the plan adds a
     `storage.fetch_articles_by_ids(conn, ids)` helper (preferred) rather than
     inlining SQL in `match.py`.
   - **SQLite variable cap:** a single `WHERE article_id IN (?,?,…)` is limited
     to ~999 (older) / 32766 (newer) bound params. Since `BATCH_SIZE`=2000 can
     exceed the old cap, the helper must sub-chunk the `IN` list (e.g. 500/query)
     or join against a temp table — do not assume one `IN` per batch.

**Transactions.** Because the connection is autocommit, each `mark_*` UPDATE
commits on its own; there is no separate per-batch `conn.commit()` to add (it
would be a no-op / "no transaction" error). If batch-level durability/throughput
tuning is wanted, wrap each slice in an explicit `BEGIN`/`COMMIT` via
`conn.execute("BEGIN")` … `conn.execute("COMMIT")`; this is optional and the
plan should pick one and state it — the default is "rely on autocommit, no
explicit transaction."

- Per-ticker accumulation stays in memory (only the *matched* subset, a few
  thousand entries) and is written once at the end, as today.
- `--rematch-all`'s "reset all to pending + wipe per_ticker/" prelude runs
  **before** the id snapshot, unchanged. `--since` / `--limit` keep working
  (applied to the snapshot query).

### 3. Detached rematch deploy step (`.github/workflows/deploy-esg-collector.yml`)

Replace the inline `if [ "$REMATCH" = "1" ]; then … pipeline.match
--rematch-all … fi` with a **fire-and-return** launch of a transient unit that
owns the whole rematch lifecycle on the VM. A committed wrapper script
`deploy/rematch_managed.sh` (shipped in the repo, runnable on the VM) does:

```
1. systemctl stop the 4 worker services        # needs root
2. sudo -u esg <venv> -m pipeline.match --rematch-all   # heavy work as esg
3. sudo -u esg <venv> -m pipeline.export --ndjson --upload
4. systemctl start the 4 worker services        # needs root
5. write gs://esg-scan-data/_setup/rematch_status.json (state/counts)
```

**Status counts.** `run()` returns its `{matched,unmatched,deferred}` dict in
Python (match.py:183), not on stdout, so the shell wrapper cannot read it
directly. The plan adds a small `--status-json <path>` option to
`pipeline.match` that writes those counts to a local file on completion; the
wrapper reads that file and folds it into `rematch_status.json` (with
`state=done`). On any failure the wrapper writes `state=failed` + the error and
still restarts the workers. The wrapper writes `state=running` at step 1.

The deploy launches it detached and returns immediately:

```bash
systemd-run --no-block --unit=esg-rematch --collect \
  /opt/esg-collector/esg-collector/deploy/rematch_managed.sh
```

**Run as root, not `--uid=esg`.** The wrapper must `systemctl stop/start` the
worker units, which the unprivileged `esg` user cannot do; so the transient unit
runs as root (the deploy SSH user is already sudo-capable) and drops to `esg`
via `sudo -u esg` only for the python steps (matching how the existing
deploy/leftover scripts invoke the venv). GCS upload uses the **VM's attached
service account** (already proven by the daily exports), not a user credential.

**Control-flow split in the deploy script.** With the rematch detached, the
deploy must not also restart the workers itself — otherwise its step-7 restart
and the wrapper's step-1 stop race over the same units. So:
- `REMATCH=0` (normal deploy/push): unchanged — stop (step 1) … restart +
  active-check (step 7).
- `REMATCH=1`: skip the deploy's own step-1 stop and step-7 restart; instead
  launch `esg-rematch` (above) and let the wrapper own stop → rematch → restart.
  The deploy job still smoke-imports modules (step 6) before launching.

The deploy job returns in seconds (like backfill run #4), so the CI step never
holds a long SSH wait and can never be killed mid-rematch. The
`trap restart_workers` safety net stays as defence in depth for the deploy
script's own (REMATCH=0) path. Single-instance enforcement: the fixed unit name
`esg-rematch` means a second launch while one is running fails fast, so a stray
concurrent deploy cannot start a second rematch.

### 4. Observability (status file on GCS)

The managed rematch writes `gs://esg-scan-data/_setup/rematch_status.json` at
start and end:

```json
{"state":"running|done|failed","started_at":"…","finished_at":"…",
 "counts":{"matched":N,"unmatched":N,"deferred":N},"error":"…"}
```

**Identities.** The VM **writes** the status file (and the per-ticker exports)
with its **attached instance service account** — already proven by the working
daily `gsutil cp … gs://esg-scan-data/…` uploads. The **local box reads** it
with the `dangvule@gmail.com` gcloud account, which is the local identity
confirmed to have list/read on the bucket (the other local account
`alphax2signal@gmail.com` does not). Two different identities, both with bucket
access; no user credential is needed on the VM. Polling this file from the local
box gives progress and completion without SSH.

## Testing

- **Matcher equivalence (local, pytest):** keep the current per-alias matcher as
  a reference implementation (a small `_legacy_match_text` kept in the test file,
  copied from today's `alias_matcher.py`), then run both the reference and the
  new single-pass matcher over a **committed fixture corpus** at
  `tests/fixtures/matcher_corpus.jsonl` and assert the **`{(ticker, location)}`
  set is identical** for every input — i.e. the same tickers match in the same
  fields (the load-bearing invariant). The matched-alias *label* is allowed to
  differ and is not asserted. The test also **counts** any `(ticker, location)`
  divergence and prints offenders; the gate is **zero** divergences on the
  fixture (and the test is reused as a one-off over a larger real sample pulled
  from GCS before the live rematch). The fixture is a new
  curated file (≈40–60 short Vietnamese texts) deliberately covering: the HCM
  city false-positive strings (`TP.HCM`, `CSGT TPHCM`), real HSC/`Chứng khoán
  HSC` hits, FRT brand strings (`FPT Shop`, `Nhà thuốc Long Châu`, `FPT Long
  Châu`), DGC full-brand vs bare-`Đức Giang`, diacritic word-boundary cases, and
  at least one cross-ticker substring-overlap case. It does **not** depend on any
  out-of-repo scratch data. This is the gate that proves the rewrite changes
  nothing about *what* matches.
- **Chunked rematch (local, pytest):** build a small temp SQLite (via
  `storage.init_db` on a temp path) with a known set of articles, run
  `run(rematch_all=True, db_path=…)` against it, assert correct
  matched/unmatched/deferred counts and correct per-ticker JSON output. Set
  `BATCH_SIZE` small (e.g. 2) in the test so the multi-batch pagination path is
  exercised. Note: this proves *correctness and that pagination works*, not the
  e2-micro memory ceiling — the OOM fix is validated by code inspection +
  bounded-batch design, and confirmed only on the live VM (see deferred).
- **Deferred (needs live VM):** the `systemd-run` detached launch, the
  root→`esg` privilege drop, the worker stop/start by the wrapper, and the
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
- **systemd-run availability** — the transient `esg-rematch` unit runs as
  **root** (per §3) and drops to `esg` via `sudo -u esg` for the python steps;
  verify on first live run that `systemd-run --no-block --collect` is available
  and the wrapper can `systemctl stop/start` the worker units. Fallback: ship a
  dedicated oneshot service unit in `deploy/` instead of `systemd-run`.
- **Detached job ↔ deploy race** — the managed rematch stops/starts workers; a
  concurrent push-deploy also touches workers. The existing `concurrency` group
  on the workflow plus the rematch unit name (`esg-rematch`, single instance)
  bound this; document "don't trigger a deploy while a rematch is running".
