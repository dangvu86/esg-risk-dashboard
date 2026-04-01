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
```

## Bước 2: Tạo Cloud Storage Bucket

```bash
# Tạo bucket (chọn tên unique)
gcloud storage buckets create gs://esg-risk-dashboard --location=us-central1

# Cho phép public read
gcloud storage buckets add-iam-policy-binding gs://esg-risk-dashboard \
  --member=allUsers --role=roles/storage.objectViewer
```

## Bước 3: Deploy Cloud Function

```bash
cd cloud-function

gcloud functions deploy esg_scan \
  --runtime python312 \
  --trigger-http \
  --allow-unauthenticated \
  --memory 512MB \
  --timeout 540s \
  --region us-central1 \
  --set-env-vars GEMINI_API_KEY=your_key_here,GCS_BUCKET=esg-risk-dashboard
```

## Bước 4: Test Cloud Function

```bash
# Test scan 1 ticker
curl "https://us-central1-YOUR_PROJECT.cloudfunctions.net/esg_scan?tickers=HPG"

# Test batch 1 (50 công ty đầu)
curl "https://us-central1-YOUR_PROJECT.cloudfunctions.net/esg_scan?batch=1"
```

## Bước 5: Setup Cloud Scheduler (Weekly Cron)

```bash
# Batch 1: 50 công ty đầu - Thứ 2 lúc 7:00
gcloud scheduler jobs create http esg-scan-batch1 \
  --schedule="0 7 * * 1" \
  --uri="https://us-central1-YOUR_PROJECT.cloudfunctions.net/esg_scan?batch=1" \
  --http-method=GET \
  --time-zone="Asia/Ho_Chi_Minh"

# Batch 2: 50 công ty sau - Thứ 2 lúc 7:15
gcloud scheduler jobs create http esg-scan-batch2 \
  --schedule="15 7 * * 1" \
  --uri="https://us-central1-YOUR_PROJECT.cloudfunctions.net/esg_scan?batch=2" \
  --http-method=GET \
  --time-zone="Asia/Ho_Chi_Minh"
```

## Bước 6: Deploy Vercel

1. Sửa `web/.env.local`:
```
NEXT_PUBLIC_DATA_URL=https://storage.googleapis.com/esg-risk-dashboard/esg_events.json
NEXT_PUBLIC_FUNCTION_URL=https://us-central1-YOUR_PROJECT.cloudfunctions.net/esg_scan
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
| Cloud Functions | 2M calls/tháng | ~8 calls/tháng |
| Cloud Storage | 5GB | < 1MB |
| Cloud Scheduler | 3 jobs | 2 jobs |
| Gemini API | 1500 RPD | ~100 calls/tuần |
| Vercel | 100GB bandwidth | < 1GB |
