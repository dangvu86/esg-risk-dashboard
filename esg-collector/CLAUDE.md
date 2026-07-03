# esg-collector — deploy notes (Cloud Run era)

The old GCE VM is GONE (deleted 2026-06-08). Everything runs as two Cloud
Run Jobs in **us-central1**, project `gen-lang-client-0020762472`:

| Job | Trigger | Mode |
|---|---|---|
| `esg-daily` | Cloud Scheduler `esg-daily-trigger`, 01:00 Asia/Ho_Chi_Minh daily | `--mode daily` |
| `esg-backfill` | manual | `--mode backfill` / `rematch` / `enrich` |

## Deploy is automated. Push to main.

Push to `main` touching `esg-collector/**` triggers
`.github/workflows/deploy-esg-collector-cloudrun.yml`: pytest → Cloud Build
image `:<sha>` → deploy both jobs → ensure the scheduler exists. 5–8 min.
(`deploy-esg-collector.yml` is the dead VM workflow — workflow_dispatch only,
do not use.)

CI identity: `github-actions-deploy@…iam.gserviceaccount.com` (granted
2026-06-12: cloudbuild.builds.editor, artifactregistry.writer, run.admin,
cloudscheduler.admin, serviceusage.serviceUsageConsumer +
iam.serviceAccountUser, and storage.admin scoped to the
`…_cloudbuild` staging bucket — `gcloud builds submit` uploads source
there first). If the build step 403s, check these roles first.

## Manual operations

Always pass `--account dangvule@gmail.com --project gen-lang-client-0020762472`
on EVERY gcloud call (local config flips to other accounts mid-session).

- **Rematch after alias/filter changes** (NOT automated on purpose):
  `gcloud run jobs execute esg-backfill --region us-central1 --args="--mode,rematch"`
  (~20 min; resets match/esg columns, PRESERVES enrich columns).
- **Manual deploy fallback**:
  `gcloud builds submit esg-collector --tag us-central1-docker.pkg.dev/gen-lang-client-0020762472/esg/esg-collector:<sha>`
  then `gcloud run jobs update esg-daily|esg-backfill --region us-central1 --image …`.

## Adding a company

1. `python -m alias_builder.fetch_vietstock --ticker XYZ`
2. Vet the aliases against the corpus BEFORE deploying (FP aliases like
   NLG "Southgate" = 531 Gareth-Southgate articles are silent until they
   hit the dashboard):
   `python -m alias_builder.alias_vet --tickers XYZ --db <local articles.db>`
   → read flagged samples → `--apply` removes FAILs.
3. Commit, push main (auto-deploy), then run a rematch.

## State layout on GCS (gs://esg-scan-data)

- `state/articles.db` — THE database (gen-matched upload; lock first)
- `state/pipeline.lock` — GCS mutex; daily skips itself if held
- `per_ticker/*.json` — live per-company matches (**bucket root**;
  `state/per_ticker/` is a STALE VM-era copy — never export from it)
- `web/*.json` — public dashboard data (publicRead, cache 300s)

## Schema migrations

Add guarded `ALTER TABLE` to `core/storage.py::init_db()` — every job run
calls it on start.
