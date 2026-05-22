#!/usr/bin/env bash
# Provision GCP resources for esg-collector. Run LOCALLY (needs gcloud auth).
#
# Prerequisites:
#   gcloud auth login          # use dangvule@gmail.com
#   gcloud config set project <PROJECT_ID>
#
# What this script creates:
#   - GCS bucket            : gs://esg-scan-data/
#   - Service account       : esg-collector@<project>.iam.gserviceaccount.com
#     with role roles/storage.objectAdmin on the bucket
#   - GCE VM                : esg-collector  (e2-micro, Debian 12, us-central1-a)
#     with the SA attached + cloud-platform scope
#
# After this finishes, SSH into the VM and run install.sh.
set -euo pipefail

PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-central1}"          # free-tier eligible
ZONE="${ZONE:-us-central1-a}"
VM_NAME="${VM_NAME:-esg-collector}"
SA_NAME="${SA_NAME:-esg-collector}"
BUCKET="${BUCKET:-esg-scan-data}"
MACHINE="${MACHINE:-e2-micro}"

if [[ -z "$PROJECT" ]]; then
  echo "ERROR: project not set. Run: gcloud config set project <PROJECT_ID>" >&2
  exit 1
fi
SA_EMAIL="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"

echo "==> Project: $PROJECT"
echo "==> Zone:    $ZONE"
echo "==> Bucket:  gs://$BUCKET"
echo "==> VM:      $VM_NAME ($MACHINE)"
echo

# 1. Bucket
if ! gcloud storage buckets describe "gs://$BUCKET" --project "$PROJECT" >/dev/null 2>&1; then
  echo "Creating bucket gs://$BUCKET …"
  gcloud storage buckets create "gs://$BUCKET" \
    --project "$PROJECT" --location "$REGION" --uniform-bucket-level-access
else
  echo "Bucket gs://$BUCKET already exists."
fi

# 2. Service account
if ! gcloud iam service-accounts describe "$SA_EMAIL" --project "$PROJECT" >/dev/null 2>&1; then
  echo "Creating service account $SA_EMAIL …"
  gcloud iam service-accounts create "$SA_NAME" \
    --project "$PROJECT" --display-name "ESG collector worker"
else
  echo "Service account $SA_EMAIL already exists."
fi

# 3. Bucket-scoped IAM
echo "Granting storage.objectAdmin on gs://$BUCKET to $SA_EMAIL …"
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" \
  --member="serviceAccount:$SA_EMAIL" \
  --role="roles/storage.objectAdmin" \
  --project "$PROJECT" >/dev/null

# 4. VM
if ! gcloud compute instances describe "$VM_NAME" --zone "$ZONE" --project "$PROJECT" >/dev/null 2>&1; then
  echo "Creating VM $VM_NAME …"
  gcloud compute instances create "$VM_NAME" \
    --project "$PROJECT" \
    --zone "$ZONE" \
    --machine-type "$MACHINE" \
    --image-family debian-12 --image-project debian-cloud \
    --boot-disk-size 30GB --boot-disk-type pd-standard \
    --service-account "$SA_EMAIL" \
    --scopes cloud-platform \
    --tags esg-collector
else
  echo "VM $VM_NAME already exists."
fi

echo
echo "Done. Next:"
echo "  gcloud compute ssh $VM_NAME --zone $ZONE --project $PROJECT"
echo "  # then on the VM:"
echo "  sudo bash /tmp/install.sh   # after scp-ing deploy/install.sh"
echo
echo "Or one-liner to copy + run install.sh:"
echo "  gcloud compute scp deploy/install.sh $VM_NAME:/tmp/ --zone $ZONE --project $PROJECT"
echo "  gcloud compute ssh $VM_NAME --zone $ZONE --project $PROJECT --command 'sudo bash /tmp/install.sh'"
