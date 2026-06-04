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

# 6. Deploy SA roles (swap compute roles for Cloud Run deploy roles)
for ROLE in roles/run.developer roles/artifactregistry.writer roles/iam.serviceAccountUser; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${DEPLOY_SA}" --role="$ROLE"
done

echo "Infra ready. Deploy jobs via the GitHub Actions workflow, then create the schedule (Task 15 step within deploy)."
