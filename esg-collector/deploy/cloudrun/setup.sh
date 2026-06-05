#!/usr/bin/env bash
# One-time Cloud Run infra for esg-collector. Run from Cloud Shell as the
# project owner. Idempotent where the gcloud verb allows.
set -euo pipefail

PROJECT=gen-lang-client-0020762472
REGION=us-central1
REPO=esg
RUNTIME_SA=esg-collector@${PROJECT}.iam.gserviceaccount.com
DEPLOY_SA=github-actions-deploy@${PROJECT}.iam.gserviceaccount.com
BUCKET=esg-scan-data

gcloud config set project "$PROJECT"

# 1. APIs
gcloud services enable run.googleapis.com artifactregistry.googleapis.com \
  cloudbuild.googleapis.com cloudscheduler.googleapis.com secretmanager.googleapis.com

# 2. Artifact Registry repo
gcloud artifacts repositories create "$REPO" --repository-format=docker \
  --location="$REGION" --description="esg-collector images" || true

# 3. Secrets (paste values when prompted; --data-file=- reads stdin)
for S in BRAVE_API_KEY JINA_API_KEY GROQ_API_KEY; do
  gcloud secrets create "$S" --replication-policy=automatic || true
  echo "Set value for $S then Ctrl-D:"; gcloud secrets versions add "$S" --data-file=-
done

# 4. Runtime SA can read secrets (it already has storage.objectAdmin on the bucket)
for S in BRAVE_API_KEY JINA_API_KEY GROQ_API_KEY; do
  gcloud secrets add-iam-policy-binding "$S" \
    --member="serviceAccount:${RUNTIME_SA}" --role=roles/secretmanager.secretAccessor
done

# 5. Confirm the runtime SA actually has access to THIS bucket (spec open item)
gsutil iam get "gs://${BUCKET}" | grep -A2 "$RUNTIME_SA" || \
  echo "WARN: ${RUNTIME_SA} not found on gs://${BUCKET} — grant roles/storage.objectAdmin"

# 6. Deploy SA roles (swap compute roles for Cloud Run deploy roles).
#    Every role here backs a concrete step in deploy-esg-collector-cloudrun.yml:
#    - cloudbuild.builds.editor : `gcloud builds submit` (build the image)
#    - artifactregistry.writer  : `gcloud artifacts docker tags add` (tag :latest)
#    - run.admin (NOT developer): `gcloud run jobs deploy` AND the
#        `run jobs add-iam-policy-binding` invoker grant — setIamPolicy is only
#        in run.admin; run.developer would 403 on the invoker step.
#    - cloudscheduler.admin     : `gcloud scheduler jobs create http`
#    - iam.serviceAccountUser   : deploy job/scheduler acting-as the runtime SA
for ROLE in roles/cloudbuild.builds.editor roles/artifactregistry.writer \
            roles/run.admin roles/cloudscheduler.admin roles/iam.serviceAccountUser; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${DEPLOY_SA}" --role="$ROLE"
done

# 7. `gcloud builds submit` runs the build under the Cloud Build service agent
#    (PROJECT_NUMBER@cloudbuild.gserviceaccount.com), NOT the deploy SA. On a
#    fresh project that agent may lack push/log perms. If the build step fails
#    on "permission denied" pushing to Artifact Registry or writing logs, grant:
#      CB_SA=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')@cloudbuild.gserviceaccount.com
#      gcloud projects add-iam-policy-binding "$PROJECT" \
#        --member="serviceAccount:${CB_SA}" --role=roles/artifactregistry.writer
#      gcloud projects add-iam-policy-binding "$PROJECT" \
#        --member="serviceAccount:${CB_SA}" --role=roles/logging.logWriter

echo "Infra ready. Deploy jobs via the GitHub Actions workflow (it builds the"
echo "image, deploys both jobs, grants run.invoker, and creates the schedule)."
