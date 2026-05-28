# esg-collector — deploy notes

## Deploy is automated. Do not SSH manually.

Push to `main` touching `esg-collector/**` (or the workflow file)
triggers `.github/workflows/deploy-esg-collector.yml`. The workflow SSHs
to the GCE VM `esg-collector` (us-central1-a, project
`gen-lang-client-0020762472`) via IAP and runs:

1. stop 4 workers
2. rolling 7-day backup of `articles.db`
3. `git reset --hard origin/main`
4. `storage.init_db()` — picks up any new `ALTER TABLE` migrations
5. legacy timestamp normalize (idempotent; no-op once clean)
6. smoke-import the new modules before restarting workers
7. start workers, fail the run if any aren't `active`

So: **commit + push** is the whole deploy. Don't write one-off bash
scripts to SSH and `git pull` — that's what the old `_deploy_fix.sh`
pattern did and it left state drift.

## Schema migrations

Add `ALTER TABLE` statements to `core/storage.py::init_db()` guarded by
an `IF NOT EXISTS` / `PRAGMA table_info` check. The deploy will apply
them. For data backfills that aren't idempotent, gate them behind a
metadata flag in `export_state` so reruns are safe.

## Manual triggers

- **Full rematch**: Actions UI → "Deploy esg-collector" → "Run
  workflow" → tick `run_rematch_all`. Use after alias edits.
- **No path-filter trigger**: same UI, leave checkbox off.

## CI auth

- Service account: `github-actions-deploy@gen-lang-client-0020762472.iam.gserviceaccount.com`
- GitHub secret: `GCP_SA_KEY` (JSON key, in repo settings)
- SA roles: `compute.instanceAdmin.v1`, `iap.tunnelResourceAccessor`,
  `iam.serviceAccountUser`, `compute.osAdminLogin`

If auth breaks, the failed step is `Authenticate to GCP` — usually
secret expired or SA roles were stripped.

## VM facts that aren't in code

- App dir: `/opt/esg-collector/esg-collector` (git checkout)
- Venv: `/opt/esg-collector/.venv/bin/python`
- DB: `/opt/esg-collector/esg-collector/data/articles.db`
- Service user: `esg`
- **No `sqlite3` CLI** on the VM — use the venv's Python sqlite3 module
  in deploy scripts.
