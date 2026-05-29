# Deploy esg-collector on GCE e2-micro

Tài khoản Google: `dangvule@gmail.com`. Chạy các lệnh dưới trong PowerShell hoặc WSL.

## 1. One-time GCP setup (local máy anh)

```bash
gcloud auth login                                # đăng nhập bằng dangvule@gmail.com
gcloud projects list                             # chọn project có sẵn hoặc tạo mới
gcloud config set project <PROJECT_ID>

# Provision bucket + service account + VM
bash esg-pipeline/esg-collector/deploy/provision_gcp.sh
```

Script tạo:
- `gs://esg-scan-data/` (us-central1, free tier 5GB)
- service account `esg-collector@<project>.iam.gserviceaccount.com` với role `storage.objectAdmin` trên bucket
- VM `esg-collector` (e2-micro Debian 12, attach SA + cloud-platform scope, 30GB pd-standard — đều trong free tier us-central1)

## 2. Install trên VM

```bash
# copy install.sh lên VM rồi chạy
gcloud compute scp esg-pipeline/esg-collector/deploy/install.sh esg-collector:/tmp/ \
  --zone us-central1-a
gcloud compute ssh esg-collector --zone us-central1-a \
  --command 'sudo bash /tmp/install.sh'
```

`install.sh` (chạy với sudo trên VM):
1. apt install python3 + lxml
2. tạo user `esg`
3. git clone `https://github.com/dangvule/esg-scan.git` vào `/opt/esg-collector` *(sửa REPO_URL trong script nếu repo của anh tên khác)*
4. tạo venv + pip install
5. copy 6 systemd unit + 1 timer vào `/etc/systemd/system/`
6. **tạo `/etc/esg-collector.env`** từ template (cần điền tay 2 key sau lần đầu)
7. chạy `queue_builder --mode backfill` (flow đầy đủ: alias từng công ty +
   keyword từng-từ, mọi backend) để fill queue lịch sử. **Trước đó** nên chạy
   `fetch_vietstock --all` để sinh alias cho ~100 mã (mã thiếu alias bị bỏ qua
   kèm cảnh báo, không lỗi).
8. enable + start 4 worker service + match.timer + daily.timer

## 3. Điền API keys

Lần đầu install.sh tạo `/etc/esg-collector.env` rỗng và 4 worker sẽ fail. SSH vào VM rồi:

```bash
sudo nano /etc/esg-collector.env
# điền BRAVE_API_KEY và JINA_API_KEY
sudo systemctl restart esg-collector-google esg-collector-baomoi esg-collector-brave esg-collector-body
```

## 4. Kiểm tra

```bash
# stream log
gcloud compute ssh esg-collector --zone us-central1-a \
  --command 'sudo journalctl -u esg-collector-google -f'

# queue stats
gcloud compute ssh esg-collector --zone us-central1-a --command \
  'sudo -u esg /opt/esg-collector/.venv/bin/python -c "from core import storage; print(storage.queue_stats(storage.connect()))"'

# kiểm tra NDJSON đã upload chưa (sau 6h match.timer chạy lần đầu)
gcloud storage ls gs://esg-scan-data/raw_esg/
gcloud storage ls gs://esg-scan-data/per_ticker/
```

## 5. Tắt VM khi xong backfill

Sau khi backfill lịch sử xong (2-3 ngày), tắt VM để khỏi tốn quota:

```bash
gcloud compute instances stop esg-collector --zone us-central1-a
```

Restart bất kỳ lúc nào — SQLite + queue persistent, worker resume nguyên trạng.

## File trong deploy/

```
provision_gcp.sh              # local — tạo bucket + SA + VM
install.sh                    # VM   — cài app + enable services
esg-collector.env.example     # template /etc/esg-collector.env
esg-collector-google.service  # systemd: workers.runner --backend google_rss
esg-collector-baomoi.service  # systemd: workers.runner --backend baomoi
esg-collector-brave.service   # systemd: workers.runner --backend brave
esg-collector-body.service    # systemd: workers.body_fetcher --workers 8
esg-collector-match.service   # oneshot: pipeline.match && pipeline.export --upload
esg-collector-match.timer     # mỗi 6h trigger match.service
```
