# Cloud Run Cutover + Decommission Runbook

> **SAFETY BANNER**
>
> **Nothing in Phase 5 (Decommission) runs until Phase 4 parity verification
> passes AND the daily schedule has completed at least one clean cycle.**
>
> **Phases 0 through 2 must execute in strict order: capture the baseline
> (Phase 0) BEFORE stopping VM writers (Phase 1), stop VM writers BEFORE
> seeding the blob (Phase 2). Running them out of order risks seeding a
> partial or stale DB.**

---

## Preconditions

Before beginning the cutover, confirm all three of the following are true:

1. **Code merged.** The `feature/esg-collector-cloud-run` branch has been
   merged to `main` (or a squash-merge equivalent). No uncommitted runtime
   changes remain.

2. **Cloud Run jobs deployed.** The GitHub Actions workflow
   `.github/workflows/deploy-esg-collector-cloudrun.yml` has run successfully
   on `main` and both jobs exist:

   ```bash
   gcloud run jobs list --region us-central1
   # Expected: esg-daily and esg-backfill both listed
   ```

3. **One-time infra complete.** `deploy/cloudrun/setup.sh` has been executed
   once from Cloud Shell (Artifact Registry repo, Secret Manager secrets,
   IAM bindings, Cloud Scheduler job all present). The runtime SA
   `esg-collector@gen-lang-client-0020762472.iam.gserviceaccount.com` has
   `roles/storage.objectAdmin` on `gs://esg-scan-data`.

---

## Phase 0 — Capture Parity Baseline (BEFORE touching the VM)

Run these commands from Cloud Shell **while the VM is still running** so that
Phase 4 has a pre-cutover snapshot to diff against.

```bash
# 0. CAPTURE A PARITY BASELINE *BEFORE* touching the VM (the gate for cutover).
#    Save the current live outputs so Phase 4 has something to diff against.
mkdir -p /tmp/baseline
gcloud storage cp gs://esg-scan-data/web/esg_events.json /tmp/baseline/
gcloud storage cp -r gs://esg-scan-data/per_ticker /tmp/baseline/    # or just record counts
python -c "import json,glob;print('baseline events:',len(json.load(open('/tmp/baseline/esg_events.json'))))"
```

Record the printed event count. This number is the lower-bound for Phase 4's
assertion.

---

## Phase 1 — Stop VM Writers

Stop every pipeline service on the VM so the SQLite database is quiescent
before the copy. Run from Cloud Shell (IAP tunnel).

```bash
# 1. Stop VM writers (so the DB is quiescent during copy)
gcloud compute ssh esg-collector --zone us-central1-a --tunnel-through-iap --command '
  sudo systemctl stop esg-collector-google esg-collector-baomoi \
    esg-collector-brave esg-collector-body esg-collector-match.timer \
    esg-collector-enrich.timer'
```

Confirm the services are stopped before proceeding to Phase 2:

```bash
gcloud compute ssh esg-collector --zone us-central1-a --tunnel-through-iap --command '
  sudo systemctl is-active esg-collector-google esg-collector-baomoi \
    esg-collector-brave esg-collector-body esg-collector-match.timer \
    esg-collector-enrich.timer'
# Each line should read "inactive" or "dead"
```

---

## Phase 2 — WAL-Checkpoint and Seed the Blob

Flush the SQLite WAL log so the database file is self-consistent, then upload
it as the Cloud Run state blob.

The path `/opt/esg-collector/esg-collector/data/articles.db` matches the
deploy workflow's `$DB=$APP_DIR/data/articles.db` where
`APP_DIR=/opt/esg-collector/esg-collector` (note the double nesting).

```bash
# 2. WAL-checkpoint, then seed the blob from the VM's articles.db.
#    Path matches the deploy workflow's $DB=$APP_DIR/data/articles.db
#    (APP_DIR=/opt/esg-collector/esg-collector) — note the double nesting.
gcloud compute ssh esg-collector --zone us-central1-a --tunnel-through-iap --command '
  /opt/esg-collector/.venv/bin/python -c "import sqlite3; c=sqlite3.connect(\"/opt/esg-collector/esg-collector/data/articles.db\"); c.execute(\"PRAGMA wal_checkpoint(TRUNCATE)\"); c.close()"
  gsutil cp /opt/esg-collector/esg-collector/data/articles.db gs://esg-scan-data/state/articles.db'
```

Verify the blob landed:

```bash
gcloud storage ls -l gs://esg-scan-data/state/articles.db
# Expected: one object, size approximately equal to the VM DB size
```

---

## Phase 3 — Deploy Jobs

Merge the migration branch to `main` (if not already done as part of the
preconditions). The GitHub Actions workflow
`.github/workflows/deploy-esg-collector-cloudrun.yml` triggers automatically
on every push to `main` that touches `esg-collector/**`.

The workflow:
- Runs the full pytest suite
- Builds the container image via Cloud Build and pushes to Artifact Registry
- Deploys `esg-daily` (1h task-timeout, 2 Gi RAM) and `esg-backfill`
  (24h task-timeout, 4 Gi RAM) as Cloud Run Jobs
- Creates the Cloud Scheduler trigger `esg-daily-trigger` (fires daily at
  02:00 UTC) if it does not already exist

Monitor the workflow run in the GitHub Actions UI. Both `gcloud run jobs deploy`
steps must succeed before proceeding.

---

## Phase 4 — Verify

Run the daily job once manually and confirm it completes successfully, the
blob generation advances, and the event count is at least as large as the
Phase 0 baseline.

```bash
# Run the daily job and wait for completion. The 20-40 minute figure is a
# PROVISIONAL estimate — the first run's fetch wall-clock has not been measured
# and may run 2-3x higher. Measure it on this first execution. The lock is
# refreshed every 30 min during the fetch drain, so a long fetch will not trip
# the lock TTL even when it overruns this estimate.
gcloud run jobs execute esg-daily --region us-central1 --wait

# Confirm the DB blob generation advanced (metatdata should show a newer
# creation_time/generation than the Phase 2 seed)
gcloud storage ls -L gs://esg-scan-data/state/articles.db

# Confirm the web export was refreshed
gcloud storage ls gs://esg-scan-data/web/
```

Run the parity assertion against the Phase 0 baseline:

```bash
gcloud storage cp gs://esg-scan-data/web/esg_events.json /tmp/after.json && \
  python -c "import json;b=len(json.load(open('/tmp/baseline/esg_events.json')));a=len(json.load(open('/tmp/after.json')));print('baseline',b,'after',a);assert a>=b, 'event count dropped!'"
```

Expected output: `after >= baseline` — the daily run appends and re-enriches
articles but never deletes rows, so the count must be monotonically
non-decreasing.

> **Invariant caveat:** `assert a>=b` holds because the daily pipeline only
> appends new articles (`INSERT OR IGNORE`) and never runs `--rematch-all` or
> any pruning pass. If a future daily job introduces a pruning step that
> removes low-signal articles, this assertion would need to be revised to
> allow for intentional shrinkage.

Also confirm `web/esg_events.json` is publicly readable (UBLA must be OFF on
the bucket; see `runtime/gcs.py` note):

```bash
curl -sf "https://storage.googleapis.com/esg-scan-data/web/esg_events.json" | python -c "import sys,json;d=json.load(sys.stdin);print('public read OK, events:',len(d))"
```

**Do not proceed to Phase 5 until this verification passes AND the Cloud
Scheduler has successfully triggered at least one additional clean daily cycle
(`esg-daily-trigger` → execution `Succeeded` in logs).**

---

## Backfill Note

`esg-backfill` is configured with a 24-hour `--task-timeout`. A full 5-year
historical backfill may exceed this limit. The job checkpoints the fetch queue
into the GCS DB blob after each stage completes, so progress is preserved
across restarts.

To resume an interrupted backfill, simply re-run the job:

```bash
gcloud run jobs execute esg-backfill --region us-central1
```

Each re-run picks up from where the previous one left off (pending tasks
remain in the queue). Because `--max-retries 0` is set, the job never
auto-retries — all re-runs are operator-initiated. To backfill only specific
tickers, pass overriding args:

```bash
gcloud run jobs execute esg-backfill --region us-central1 \
  --args="--mode,backfill,--tickers,DBC,HPG"
```

---

## Phase 5 — Decommission

> **Gate: Phase 4 parity must pass AND the daily schedule must have run
> cleanly for at least 1 cycle before executing any step in this phase.**

### Step 5a — Disable the old VM deploy workflow

Rename the old workflow file so that future pushes to `main` no longer
attempt to SSH-deploy to the VM. Do this in a local checkout and commit the
change:

```bash
# In a local checkout of the repo
git mv .github/workflows/deploy-esg-collector.yml \
       .github/workflows/deploy-esg-collector.yml.disabled
git add .github/workflows/
git commit -m "chore(cloudrun): disable VM deploy workflow (superseded by Cloud Run)"
git push origin main
```

### Step 5b — Stop and delete the VM

```bash
# Stop the instance first; confirm nothing is running before deleting
gcloud compute instances stop esg-collector --zone us-central1-a

# After confirming a further clean daily cycle from Cloud Run:
gcloud compute instances delete esg-collector --zone us-central1-a
```

### Step 5c — Delete the old `esg_scan` Cloud Function

The old `esg_scan` function lives in project `ta-tracking-api`. Locate it
first to confirm the region and generation before deleting.

```bash
# locate it first — confirms project, region, and generation
gcloud functions list --project ta-tracking-api

# 2nd-gen function (shows up under Cloud Run) → needs --gen2; adjust --region to the listed one
gcloud functions delete esg_scan --gen2 --region us-central1 --project ta-tracking-api
```

Only delete the function once confirmed the frontend/web layer no longer
reads its output (the web export is now served from
`gs://esg-scan-data/web/` via the Cloud Run pipeline).

### Step 5d — Update `esg-collector/CLAUDE.md`

Replace the "Deploy is automated via IAP SSH" section with the Cloud Run
deploy model:

- Deploy path: `git push` to `main` → GitHub Actions builds image → deploys
  both Cloud Run Jobs.
- Daily schedule: Cloud Scheduler `esg-daily-trigger` fires `esg-daily` at
  02:00 UTC.
- Manual backfill: `gcloud run jobs execute esg-backfill --region us-central1`
  (optional `--args=--mode,backfill,--tickers,XXX`).
- Secrets: managed in Secret Manager (`BRAVE_API_KEY`, `JINA_API_KEY`,
  `GROQ_API_KEY`); injected at runtime via `--set-secrets` in the deploy
  workflow.
- No SSH, no systemd, no VM.

Commit the CLAUDE.md update along with any remaining workflow file changes:

```bash
git add esg-collector/CLAUDE.md .github/workflows/
git commit -m "chore(cloudrun): decommission VM + esg_scan; update deploy docs"
```

---

## Quick Reference

| Action | Command |
|---|---|
| Trigger daily job manually | `gcloud run jobs execute esg-daily --region us-central1 --wait` |
| Trigger backfill manually | `gcloud run jobs execute esg-backfill --region us-central1` |
| View job executions | `gcloud run jobs executions list --job esg-daily --region us-central1` |
| View job logs | `gcloud logging read 'resource.type=cloud_run_job AND resource.labels.job_name=esg-daily' --limit 200 --format json` |
| Check DB blob | `gcloud storage ls -L gs://esg-scan-data/state/articles.db` |
| Check web export | `gcloud storage ls gs://esg-scan-data/web/` |
| Fire scheduler manually | `gcloud scheduler jobs run esg-daily-trigger --location us-central1` |
