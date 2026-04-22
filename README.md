# ESG Risk Dashboard - Setup Guide

## Architecture
```
Cloud Scheduler (weekly + manual trigger)
  → Cloud Function (Python): RSS → Gemini → JSON
    → Google Cloud Storage (esg_events.json)
      → Vercel (Next.js dashboard)
```

## Bước 1: Tạo Google Cloud Project

1. Vào https://console.cloud.google.com
2. Tạo project mới (hoặc dùng project có sẵn)
3. Enable APIs:
```bash
gcloud services enable cloudfunctions.googleapis.com
gcloud services enable cloudscheduler.googleapis.com
gcloud services enable storage.googleapis.com
gcloud services enable run.googleapis.com
gcloud services enable eventarc.googleapis.com
gcloud services enable cloudbuild.googleapis.com
```

## Bước 2: Tạo Cloud Storage Bucket

```bash
# Tạo bucket (chọn tên unique)
gcloud storage buckets create gs://esg-risk-dashboard --location=us-central1

# Cho phép public read
gcloud storage buckets add-iam-policy-binding gs://esg-risk-dashboard \
  --member=allUsers --role=roles/storage.objectViewer
```

## Bước 3: Deploy Cloud Function (gen2)

```bash
cd cloud-function

gcloud functions deploy esg_scan \
  --gen2 \
  --runtime python312 \
  --trigger-http \
  --allow-unauthenticated \
  --memory 512MB \
  --timeout 3600s \
  --region us-central1 \
  --set-env-vars GEMINI_API_KEY=your_key_here,GCS_BUCKET=esg-risk-dashboard
```

Gen2 cho phép timeout tối đa 60 phút (gen1 chỉ 9 phút) — cần thiết cho weekly scan top100.

## Bước 4: Test Cloud Function

Lấy URL gen2 (khác format gen1):
```bash
gcloud functions describe esg_scan --gen2 --region us-central1 --format="value(serviceConfig.uri)"
```

URL có dạng: `https://esg-scan-<hash>-uc.a.run.app`

```bash
# Test scan 1 ticker
curl "<FUNCTION_URL>?tickers=HPG"

# Test batch 1 (5 công ty đầu)
curl "<FUNCTION_URL>?batch=1"

# Auto mode (toàn bộ top100, dùng full timeout 60 phút)
curl "<FUNCTION_URL>?mode=auto"
```

## Bước 5: Setup Cloud Scheduler (Weekly Cron)

Auto mode chạy toàn bộ top100 trong 1 lần (gen2 timeout 60 phút đủ cho weekly update):

```bash
# Auto mode - Thứ 2 lúc 7:00 sáng VN
gcloud scheduler jobs create http esg-scan-weekly \
  --schedule="0 7 * * 1" \
  --uri="<FUNCTION_URL>?mode=auto" \
  --http-method=GET \
  --time-zone="Asia/Ho_Chi_Minh" \
  --attempt-deadline=1800s
```

**Lưu ý `--attempt-deadline=1800s`** (30 phút, max của Cloud Scheduler):
- Function vẫn chạy ở backend nếu vượt 30 phút (gen2 cho phép tới 60 phút)
- Scheduler sẽ log "failed" nhưng function vẫn complete
- Để tránh log nhiễu, có thể chia thành 2 jobs cách nhau 30 phút:

```bash
# Job 1: batch 1-10 (50 công ty đầu)
gcloud scheduler jobs create http esg-scan-batch1 \
  --schedule="0 7 * * 1" \
  --uri="<FUNCTION_URL>?batch=1" \
  --http-method=GET \
  --time-zone="Asia/Ho_Chi_Minh" \
  --attempt-deadline=1800s

# (lặp lại cho batch=2..20 nếu muốn fine-grained)
```

## Bước 6: Deploy Vercel

1. Sửa `web/.env.local`:
```
NEXT_PUBLIC_DATA_URL=https://storage.googleapis.com/esg-risk-dashboard/esg_events.json
NEXT_PUBLIC_FUNCTION_URL=<FUNCTION_URL từ Bước 4>
```

2. Deploy:
```bash
cd web
npx vercel --prod
```

Hoặc push lên GitHub → connect Vercel → auto deploy.

## Chi phí: $0

| Service | Free Tier | Sử dụng |
|---------|-----------|---------|
| Cloud Functions gen2 | 2M invocations + 360K vCPU-s + 180K GiB-s | ~5 calls/tháng, < 2% compute |
| Cloud Run (gen2 backend) | Cùng pool với Cloud Functions gen2 | — |
| Cloud Build (deploy gen2) | 120 phút build/ngày | ~3 phút/deploy |
| Cloud Storage | 5GB | < 1MB |
| Cloud Scheduler | 3 jobs/tháng | 1-2 jobs |
| Gemini API (Flash-Lite) | 1000 RPD | ~70 calls/tuần |
| Vercel | 100GB bandwidth | < 1GB |
