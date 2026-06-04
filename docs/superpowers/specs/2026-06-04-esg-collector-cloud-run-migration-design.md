# ESG Collector → Cloud Run Migration — Design Spec

**Date:** 2026-06-04
**Status:** Approved (pending spec review)
**Scope:** Move the `esg-collector` runtime off the always-on GCE e2-micro VM onto
**Cloud Run Jobs**, keeping SQLite as the data model but relocating the database to a
**single blob on GCS** that each job checks out and checks in under a lock. Two job
profiles from one image: a scheduled **daily** job and an on-demand **backfill** job
(initial history + re-run per new ticker). The collector's collection/match/enrich/export
logic is reused unchanged; only the runtime, state location, and deploy mechanism change.

## Goal

The collector currently runs as 4 always-on systemd workers + 3 timers on a single
e2-micro VM (1 GB RAM), with SQLite `articles.db` on the VM's local disk as the source of
truth and GCS as an output/backup mirror. This runtime is **hard to operate**:

- The 1 GB box OOM-wedges on `match`/`enrich` and the ~50k `pending` backlog. A wedged VM
  **blocks the GitHub-Actions deploy** (deploy SSHes in, stops/starts workers — can't, if
  the box is thrashing).
- Deploy depends on IAP SSH + `git reset --hard` on a *live, stateful* box; state drift
  (leftover startup-scripts) has clobbered manual edits.
- SSH from the user's Windows box is broken (plink).

This spec replaces that with **Cloud Run Jobs**: each run is a fresh container with its own
RAM that does the work and exits — no permanent footprint, no box to wedge, deploy = push an
immutable image. Data lives in GCS, fully decoupled from compute.

**Hard constraint:** stay within the Cloud Run **free tier**. This rules out always-on
workers (one always-on instance ≈ 720 vCPU-h/month ≫ the 50 vCPU-h free) and forces the
**scheduled-batch** model below.

## Locked scope decisions

| Decision | Choice |
|---|---|
| Compute model | **Cloud Run Jobs** (batch, scale-to-zero). NOT services, NOT always-on workers. |
| State store | **SQLite blob on GCS** (`gs://esg-scan-data/state/articles.db`), checked out per run. NOT gcsfuse-mounted SQLite, NOT Firestore, NOT Cloud SQL. |
| Concurrency safety | **One GCS lock object** + `if-generation-match` preconditions → exactly one writer at a time. |
| Jobs | **2 profiles, 1 image**: `esg-daily` (scheduled) + `esg-backfill` (on-demand, `--ticker` aware). |
| Freshness / cadence | **1×/day**, 09:00 VN (= 02:00 UTC), via Cloud Scheduler. Cadence is a config knob (one Scheduler line). |
| Project / region | **`gen-lang-client-0020762472`**, **us-central1** (same as bucket + existing SA → free same-region egress). |
| Build | **GitHub Actions → build image → Artifact Registry → `gcloud run jobs deploy`**. No local Docker required. |
| Secrets | **Secret Manager** (`BRAVE_API_KEY`, `JINA_API_KEY`, `GROQ_API_KEY`), mounted via `--set-secrets`. |
| Data preservation | Backfill **only `INSERT OR IGNORE`** — never wipes. Cutover seeds the blob from the live VM `articles.db`. |
| Decommission | **Last step, after verification**: stop/delete VM + delete old `esg_scan` Cloud Function. |

## Background: what runs today

Per `esg-collector/ARCHITECTURE.md` — a producer/consumer system on one e2-micro VM:

- **Producer**: `daily.timer` (02:00 UTC) enqueues 72 tasks (24 ESG topics × 3 sources) into
  a SQLite `search_queue`, window = last 3 days.
- **Consumers**: 4 always-on workers (`google`, `baomoi`, `brave` fetchers + `body` fetcher
  via Jina) poll the queue 24/7, writing deduped rows into `articles` (SQLite WAL lets the
  separate processes share the one file).
- **Periodic**: `match.timer` (6h) runs alias match → `per_ticker/*.json` + NDJSON → GCS;
  `enrich.timer` (6h, offset) runs 3 LLM stages (`--limit 25`) + web export → GCS;
  `status.timer` (30m) bundles logs → GCS.
- **State**: `articles.db` (3 tables: `articles`, `search_queue`, `url_decode_cache`) on VM
  local disk = source of truth. GCS = output mirror + backup.
- **Deploy**: GitHub Actions, push to `main` touching `esg-collector/**` → IAP SSH → stop
  workers → backup db → `git reset --hard` → `init_db()` migrations → smoke-import → start
  workers.

**Two older generations also exist** (for cutover awareness):
- `cloud-function/` = the **oldest** pipeline, deployed as Cloud Function **`esg_scan`** in a
  *different* project (`ta-tracking-api`). 2nd-gen functions appear in the Cloud Run console —
  this is the "old thing on Cloud Run" to retire.
- The VM esg-collector (above) is the **current** generation and feeds the live web.

### Why this migration is non-trivial

The whole system is built around **one SQLite file shared by multiple processes via WAL on a
single host**. Cloud Run instances are ephemeral and do **not** share local disk. So the
central problem is not "can the code run on Cloud Run" — it is **"where does the queue +
article DB live once there is no single VM holding the file?"** Everything else maps cleanly:
timers → Cloud Scheduler, workers → stages inside a Job.

## Architecture & data flow

**One container image, two Cloud Run Job profiles.** Both follow the same checkout/checkin
lifecycle against a single GCS blob.

```
                 ┌──────────────────────────────────────────┐
 1 image (shared)│  esg-collector image (Artifact Registry)  │  = current code + thin GCS wrapper
                 └───────────────┬──────────────────────────-┘
                     ┌───────────┴────────────┐
                     ▼                         ▼
       ┌────────────────────────┐  ┌──────────────────────────────┐
       │ JOB esg-daily          │  │ JOB esg-backfill             │
       │ --mode daily           │  │ --mode backfill [--ticker X] │
       │ 1–2 GiB, timeout 1h    │  │ 4 GiB, timeout up to 7 days  │
       │ Cloud Scheduler 09:00VN│  │ run manually (gcloud/Console)│
       └───────────┬────────────┘  └──────────────┬───────────────┘
                   └───────────────┬───────────────┘  share blob + lock
                                   ▼
                    ┌──────────────────────────────────────┐
                    │  GCS gs://esg-scan-data/ (us-central1)│
                    │   state/articles.db    (the blob)     │
                    │   state/pipeline.lock  (mutex)        │
                    │   per_ticker/*.json    (web/dashboard)│
                    │   raw_esg/*.ndjson     (backup)       │
                    │   web/*.json           (web reads)    │
                    └──────────────────────────────────────┘
```

**Old → new mapping:**

| Old (VM) | New (Cloud Run) |
|---|---|
| `daily.timer` | Cloud Scheduler → `esg-daily` |
| 4 always-on workers | stages *inside* `esg-daily`, run with `--drain` |
| `match.timer` + `enrich.timer` | final stages of `esg-daily` |
| `status.timer` | dropped — logs go straight to Cloud Logging |
| backfill run by hand on VM | `esg-backfill` job |

## State model & lock protocol (the crux)

Every run — daily **and** backfill — follows this lifecycle. The work in the middle is the
**unchanged** existing code operating on a local SQLite file in the container's `/tmp`.

```
1. ACQUIRE LOCK  create state/pipeline.lock with x-goog-if-generation-match: 0
                 (succeeds only if it does not yet exist → atomic mutex).
                 If it exists and is fresh → log "skipped, lock held" → EXIT 0.
                 If it exists but is STALE (TTL exceeded, see below) → take it over.
2. DOWNLOAD      copy state/articles.db → /tmp/articles.db; record its generation G.
                 (If absent — first ever run — start from empty schema.)
3. WORK          run the pipeline on /tmp/articles.db (WAL, existing queries, unchanged).
4. UPLOAD        upload /tmp/articles.db with x-goog-if-generation-match: G
                 (fails if anyone modified it meanwhile → abort, keep lock logic safe).
                 Also upload per_ticker / raw_esg / web outputs.
5. RELEASE LOCK  delete state/pipeline.lock.
```

**Why safe:** the lock guarantees exactly one job owns the blob at a time → never two writers
→ no SQLite corruption. This is what lets daily and backfill *coexist without running
concurrently*: if backfill holds the lock for hours, a daily firing sees the lock and skips
that day; the 3-day fetch window + next day's run recovers, so no articles are lost.

**Crash safety:** if a job dies during step 3, the GCS blob is still at generation G (the
previous good state). The next run re-downloads G and redoes the lost run's work — one wasted
run, **zero data loss**.

**Stale-lock recovery:** `pipeline.lock` carries a JSON body `{owner, started_at, mode}`. A
starting job that finds a lock whose `started_at` is older than a TTL (e.g. daily TTL = 2h;
backfill writes a long/refreshed TTL) treats it as abandoned and takes it over. The lock body
is refreshed periodically during a long backfill (see below) so it is not falsely reaped.

**Lock body schema** (`state/pipeline.lock`, JSON):
```json
{ "owner": "<execution-id>", "mode": "daily|backfill", "started_at": "<iso8601 UTC>", "ttl_seconds": 7200 }
```

## Daily job flow (`--mode daily`)

Stages run sequentially inside one execution, in this required order (match needs body;
enrich needs match; export needs all):

```
1. ACQUIRE LOCK + DOWNLOAD articles.db (generation G)
2. init_db()                          ← apply ALTER TABLE migrations on the downloaded blob
3. ENQUEUE   queue_builder --mode daily   → 72 tasks, 3-day window
4. FETCH     run google + baomoi + brave workers in --drain mode, concurrently
             → exit when the queue is empty (NOT sleep-forever)
5. BODY      body_fetcher --drain, CAPPED (e.g. newest 200 pending / time-box 10 min)
             → remainder carries to next day (body fetch is the long pole; ~86% miss today)
6. MATCH     pipeline.match            → per_ticker, match_status
7. ENRICH    enrich.runner --limit 25  (sentiment → translate → controversy, capped)
8. EXPORT    pipeline.export --web --upload → per_ticker + web/*.json + raw_esg NDJSON
9. UPLOAD articles.db (if-generation-match G) + RELEASE LOCK
```

Caps (body time-box, enrich `--limit 25`) keep a daily run to ~20–40 min wall-clock → well
inside free tier.

## Backfill job flow + new-ticker workflow (`--mode backfill`)

**Full backfill (initial):** `--mode backfill`
```
1. ACQUIRE LOCK + DOWNLOAD blob
2. init_db()
3. ENQUEUE  queue_builder --mode backfill  → per-company alias + per-keyword passes,
            weekly chunks over 5 years (the "hybrid per-company pass" coverage-gap fix)
4. DRAIN in CHUNKS: fetch workers --drain; every ~2h checkpoint-upload the blob and
            refresh the lock TTL → a crash never loses more than the last chunk
            (queue state persists in the blob). Reuses the merged chunked-rematch primitive.
5. BODY (chunked / time-boxed) → 6. MATCH (full rematch) → 7. EXPORT
8. UPLOAD blob + RELEASE LOCK
   ENRICH is SKIPPED in backfill — the daily job's --limit 25 chews through new events over
   subsequent days, avoiding an LLM rate-limit/cost blowup in one run.
```

**Add a new ticker later:** `--mode backfill --ticker XXX`
```
1. Add XXX to config/companies.csv → git push (ships a new image with the updated config)
2. Run esg-backfill --ticker XXX manually:
     - ensure aliases for XXX exist (fetch_vietstock --ticker XXX if missing)
     - enqueue ONLY XXX's historical tasks
     - drain → match → export
3. From the next day, esg-daily automatically includes XXX (it is in companies.csv)
```

This satisfies "backfill runs initially AND re-runs per new ticker," touching only what is
needed and **never overwriting existing data** (`INSERT OR IGNORE` on `article_id`).

## Code changes (what actually gets written)

The collector's domain logic is reused as-is. New/changed code is the runtime shell:

1. **`--drain` mode for workers** (`workers/runner.py`, `workers/body_fetcher.py`): exit when
   no pending tasks remain for the backend, instead of sleeping 60s forever. This is the key
   behavioral change that turns 24/7 workers into batch stages.
2. **GCS blob wrapper / orchestrator** (new module, e.g. `runtime/job.py`): implements the
   lock + download/upload lifecycle, parses `--mode`/`--ticker`, sequences the stages, and is
   the container `ENTRYPOINT`.
3. **Lock helper** (new, e.g. `runtime/gcs_lock.py`): acquire/release/refresh/stale-takeover
   via `if-generation-match`.
4. **Config**: read DB path from env (`/tmp/articles.db` in-container), secrets from env
   (Secret Manager), cadence/caps as env knobs.
5. **`Dockerfile`** (~12 lines: python:3.x-slim + lxml + pip install + ENTRYPOINT).
6. **GitHub Actions workflow**: replace the IAP-SSH deploy with build → Artifact Registry →
   `gcloud run jobs deploy` for both job profiles.
7. **Deprecate** VM-only deploy scripts (`install.sh`, systemd units, `_deploy_fix.sh`, etc.)
   after cutover — keep until the VM is decommissioned.

## Deploy (no local Docker required)

`Dockerfile` is just a text file (authored here); the *build* runs in the cloud/CI, so the
user's Windows box never installs Docker:

- **One-time setup** (a few `gcloud` commands, runnable in Cloud Shell): enable
  `run.googleapis.com` + `artifactregistry.googleapis.com` + `cloudbuild.googleapis.com`;
  create an Artifact Registry repo; create the 3 Secret Manager secrets; create the daily
  Cloud Scheduler job; grant IAM (below).
- **Ongoing deploy** = `git push`: GitHub Actions runner builds the image, pushes to Artifact
  Registry, runs `gcloud run jobs deploy esg-daily ...` and `gcloud run jobs deploy
  esg-backfill ...` (same image, different `--args`/`--memory`/`--task-timeout`).
- **Rollback** = point a job at the previous image tag (one command).

Artifact Registry (one small image ≤ 0.5 GB) and Cloud Build (≈ 2 min/build, free 120
min/day) both stay in free tier.

## Secrets & IAM

- **Secrets** → Secret Manager: `BRAVE_API_KEY`, `JINA_API_KEY`, `GROQ_API_KEY`, mounted with
  `--set-secrets` (replaces `/etc/esg-collector.env`).
- **Runtime SA**: reuse `esg-collector@gen-lang-client-0020762472` (already has
  `storage.objectAdmin` on the bucket) + add `secretmanager.secretAccessor`. Both jobs run
  as this SA.
- **Deploy SA** (`github-actions-deploy@...`): swap compute roles for `run.developer` +
  `artifactregistry.writer` + `iam.serviceAccountUser`.
- **Scheduler SA**: `run.invoker` on `esg-daily` to trigger executions via OIDC.

## Cutover & decommission

Sequenced so nothing live breaks and the VM is the **last** thing turned off:

1. **Seed the blob**: copy the live VM `articles.db` → `gs://esg-scan-data/state/articles.db`.
   Do this via a **one-off CI step** (`gsutil cp` over the working IAP SSH path) — the user's
   local SSH is broken, but GitHub Actions IAP SSH works. All existing articles preserved.
2. **Stop VM writers** during the seed (so no double-write while copying).
3. **Deploy** both Cloud Run Jobs from the new image.
4. **Verify**: run `esg-daily` once; diff its `per_ticker/*.json` + `web/*.json` outputs
   against what the VM was producing — confirm parity. Confirm the web reads only the new
   pipeline's output (not the old `esg_scan` bucket).
5. **Decommission (only after verify passes)**: stop/delete the GCE VM; delete the old
   `esg_scan` Cloud Function in `ta-tracking-api`. Deleting compute does **not** touch GCS.

## Free-tier budget

| Item | Config | Monthly use | Free tier | Verdict |
|---|---|---|---|---|
| Daily vCPU | 1 vCPU × ~30 min × 30 | ~54k vCPU-s | 180k | ✅ ~30% |
| Daily RAM | 2 GiB | ~108k GiB-s | 360k | ✅ ~30% |
| Cloud Scheduler | 1 job | 30 fires | 3 jobs free | ✅ |
| Secret Manager | 3 secrets | a few/day | 6 secrets + 10k ops | ✅ |
| Artifact Registry | 1 image ~300 MB | 0.3 GB | 0.5 GB | ✅ |
| Cloud Build | ~2 min/push | low | 120 min/day | ✅ |
| **Backfill (one-time)** | 4 GiB × ~40 h | may exceed if same month as daily | — | ⚠️ **≈ $0.40 once** |

**Free-tier guardrails:** keep jobs + bucket in the same region (us-central1) → GCS↔Run
egress is free and fast; the daily blob round-trip (~a few hundred MB) costs ~20 s/run.
Sources: [Cloud Run pricing](https://cloud.google.com/run/pricing),
[Cloud Run task timeout](https://docs.cloud.google.com/run/docs/configuring/task-timeout).

## Testing

- **Reuse** all existing pytest (match / enrich / body / stoplist / roundup) — domain logic
  is unchanged.
- **New tests**:
  - GCS blob checkout/checkin + lock: acquire/release, `if-generation-match` conflict,
    stale-lock takeover — using `fake-gcs-server` or mocks.
  - Worker `--drain`: exits when the queue is empty.
- **Post-deploy smoke**: a small-fetch run that verifies blob upload + per_ticker/web outputs.
- **Cutover verify**: parity diff of outputs vs the VM before the VM is turned off.

## Out of scope / future (YAGNI now)

- **Splitting body out of the blob**: if `articles.db` grows to multiple GB (the `body`
  column stores full text), the daily blob round-trip gets wasteful → offload bodies to
  separate GCS objects. Not needed at current size.
- **Per-stage separate jobs** (fetch vs process on staggered schedules) — rejected: at 1×/day
  it adds clobber risk + a real lock requirement without benefit.
- **Firestore / Cloud SQL** for true concurrent jobs — rejected: SQL→NoSQL rewrite / not free
  / contradicts the GCS-state decision. Revisit only if the batch model is outgrown.

## Risks & open questions

- **Backfill holds the lock for hours** → daily skips while it runs. Acceptable for an
  occasional operation (3-day window recovers). Alternative if it becomes a problem: run
  backfill as repeated short executions that each acquire/drain-a-chunk/release so daily can
  interleave.
- **Blob size growth** over time (see Future). Monitor; act only if round-trip time bites.
- **Exact daily wall-clock** depends on body-fetch behavior (currently failing ~86%); the
  time-box cap bounds it regardless.
- **`queue_builder --ticker` support**: needs verifying/adding a ticker-scoped enqueue path
  for the new-ticker workflow (the implementation plan must confirm the current
  `queue_builder` API).
