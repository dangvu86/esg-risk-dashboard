# ESG Collector — Kiến trúc & Flow

Tài liệu giải thích app `esg-collector` đang chạy trên VM GCE `esg-collector`
(`gen-lang-client-0020762472` / us-central1-a / e2-micro), không có code,
dành cho người đọc muốn hiểu hệ thống mà không cần đọc Python.

---

## 1. Mental model: "Sếp + 4 thợ + 1 sổ cái"

```
[Sếp]               [Bảng giao việc]            [4 thợ thay nhau lấy việc]
daily.timer    →    search_queue           →    google / baomoi / brave
(09:00 VN)          (bảng SQLite)                       │
                                                        │ (insert bài tìm được)
                                                        ▼
                                              [Sổ cái bài báo]
                                              articles (SQLite)
                                                        │
                                              [Cache URL Google]
                                              url_decode_cache
                                              (tiết kiệm gọi Google)
                                                        │
                                                        ▼ match alias 6h/lần
                                              [File per ticker]
                                              data/per_ticker/*.json
                                                        │
                                                        ▼ gsutil cp
                                              gs://esg-scan-data/
                                                ├── per_ticker/  (cho web/dashboard)
                                                ├── raw_esg/     (NDJSON backup hằng ngày)
                                                └── _setup/      (log + queue stats mỗi 30')
```

### Sếp = cron `daily.timer`

- Chạy 1 lần/ngày lúc **09:00 VN** (= 02:00 UTC)
- Việc duy nhất: viết 72 task lên bảng `search_queue` (24 chủ đề ESG × 3 nguồn)
- Mỗi task = "tìm tin [chủ đề] trên [nguồn] trong khoảng [3 ngày qua]"
- Viết xong là về, không quay lại tới sáng mai

### 4 thợ = 4 systemd service chạy 24/7

- `esg-collector-google.service` — scrape Google News RSS
- `esg-collector-baomoi.service` — scrape BaoMoi (HMAC API)
- `esg-collector-brave.service` — scrape Brave Search API
- `esg-collector-body.service` — đọc nội dung body bài qua Jina Reader

Mỗi thợ làm theo cycle:
1. Lấy 1 task từ bảng (loại task của mình, chưa làm)
2. Gọi API/RSS → nhận list bài
3. Ghi từng bài vào `articles` (tự loại trùng)
4. Đánh dấu task "xong" trên bảng
5. Nghỉ X giây (mỗi nguồn quy định khác nhau, tránh bị ban)
6. Quay lại bước 1. Nếu bảng trống → sleep 60s rồi check lại.

### Sổ cái = SQLite file `data/articles.db`

3 bảng:
- `articles` — sổ cái bài báo (mỗi dòng 1 bài, mã ID duy nhất chống trùng)
- `search_queue` — bảng giao việc (mỗi dòng 1 task)
- `url_decode_cache` — cache URL Google News đã decode

---

## 2. Mọi thứ đang chạy trên VM (7 thứ)

| Tên | Loại | Lịch | Mục đích |
|---|---|---|---|
| `esg-collector-google.service` | worker 24/7 | luôn | scrape Google News |
| `esg-collector-baomoi.service` | worker 24/7 | luôn | scrape BaoMoi |
| `esg-collector-brave.service` | worker 24/7 | luôn | scrape Brave |
| `esg-collector-body.service` | worker 24/7 | luôn | fetch body bài qua Jina |
| `esg-collector-daily.timer` | hẹn giờ | 02:00 UTC = **09:00 VN** mỗi ngày | enqueue 72 task cho 3 ngày qua |
| `esg-collector-match.timer` | hẹn giờ | mỗi **6 giờ** | match alias, ghi `per_ticker/*.json`, dump NDJSON, upload GCS |
| `esg-collector-status.timer` | hẹn giờ | mỗi **30 phút** | bundle log + queue stats lên GCS để monitor từ xa |

**Lưu ý**: chỉ 4 worker là "luôn chạy". 3 timer chỉ là "đồng hồ hẹn giờ"
— tới giờ thì kích hoạt service tương ứng làm việc 5 giây tới vài phút,
xong là tắt.

---

## 3. Lịch giờ — UTC ↔ VN

VN = UTC+7. Quy đổi:

```
00:00 UTC  =  07:00 VN
02:00 UTC  =  09:00 VN   ← daily.timer
05:00 UTC  =  12:00 VN
12:00 UTC  =  19:00 VN
17:00 UTC  =  00:00 VN (ngày hôm sau)
```

### Vì sao daily chạy 09:00 VN, không phải 00:00 VN
- 00:00 VN: tin "hôm qua" chưa được Google News index xong (báo điện tử đăng muộn 1-2h)
- 09:00 VN: ngày hôm trước đã kết thúc 9 tiếng → tin đã ổn định trên Google index
- Lúc 09:00 sáng VN bên Mỹ là 2h sáng = giờ ít traffic → ít risk bị Google chặn

### Vì sao window "3 ngày" thay vì "1 ngày"
- Lệch timezone UTC↔VN = 7 tiếng → 1 ngày UTC không khớp 1 ngày VN
- Google News index trễ 12-48h là chuyện thường
- Cron đôi khi fail (VM reboot, network) → cần phủ rộng để bù
- Dedup tự lo: tin đã có trong DB sẽ bị `INSERT OR IGNORE` từ chối
- Cost: chỉ tăng số item Google trả về/task, không tăng số API call

---

## 4. Producer-Consumer pattern — vì sao tách sếp riêng + thợ riêng

Có thể đơn giản hơn bằng cách: **1 cron duy nhất** chạy 09:00 VN, làm cả search lẫn lưu, xong tắt.

Nhưng có 3 vấn đề khiến mô hình "sếp + thợ" tốt hơn:

### Vấn đề 1: Bị rate-limit giữa chừng
- Mô hình 1-cron: cron đang chạy, Google chặn → cron fail → mất luôn ngày đó, đợi 24h sau
- Mô hình thợ 24/7: thợ Google bị chặn → tự sleep backoff (5 phút → 30 phút → 2 giờ) → tự retry. Tasks còn nguyên trên bảng.

### Vấn đề 2: Backfill 5 năm = 1.500 task/người
- Mô hình 1-cron: chạy 1 lượt cả 1.500 task quá dài (>12 giờ) → cron timeout
- Mô hình thợ 24/7: thợ cứ nhai dần 1-2 ngày, không cần ai nhắc

### Vấn đề 3: Mỗi nguồn tốc độ khác nhau
- BaoMoi cho ~4 task/phút, Brave cho ~60 task/phút
- Mô hình 1-cron: phải code logic chờ giữa các nguồn → phức tạp
- Mô hình thợ: mỗi thợ tự quản tốc độ mình, song song, không ai chờ ai

### Cái giá phải trả
- 4 process Python chạy 24/7 ngồi sleep → tốn ~40-60MB RAM
- Trên e2-micro (1GB RAM) vẫn dư

---

## 5. 2 lớp lưu trữ: VM + GCS

### Lớp 1: VM — nơi làm việc thật

`/opt/esg-collector/esg-collector/data/`
- `articles.db` (SQLite, source of truth)
- `per_ticker/*.json` (output cho web)

→ 4 worker chỉ đọc/ghi ở đây. Nhanh, free, đơn giản.

### Lớp 2: GCS `gs://esg-scan-data/` — bản sao + giao tiếp ngoài

3 thư mục:

**`per_ticker/<TICKER>.json`** — snapshot từng ticker
- Update 6h/lần (sau khi match.timer chạy)
- **Tác dụng**: web app/dashboard đọc từ đây không cần SSH VM. Nếu sau này có front-end thì front-end đọc thẳng GCS.

**`raw_esg/articles_YYYYMMDD.ndjson`** — dump toàn bộ DB
- Update 6h/lần, mỗi ngày 1 file riêng theo timestamp
- **Tác dụng**: backup, phân tích offline trên máy local, khôi phục nếu VM crash.

**`_setup/logs.tar.gz`** — gói log monitoring
- Update 30 phút/lần (status.timer)
- **Tác dụng**: check tình trạng VM từ máy local mà không cần SSH.

### Vì sao cần cả 2 lớp

| Tình huống | Chỉ VM | Chỉ GCS | Cả 2 (đang dùng) |
|---|---|---|---|
| Web đọc data | Phải mở port + auth | Đọc trực tiếp | ✓ Đọc GCS |
| VM crash | **Mất hết** | Còn data | ✓ Khôi phục từ GCS |
| 4 worker ghi liên tục | Nhanh (local) | Chậm (API call/insert) | ✓ Ghi VM, sync GCS định kỳ |
| Phân tích batch máy local | SSH + download | Download trực tiếp | ✓ Download GCS |
| Cost | ~0 | ~$0.02/GB/tháng | ✓ Đủ free tier |

→ **VM là kho làm việc (động), GCS là tủ trưng bày + két sắt (tĩnh)**.

---

## 6. Hành trình 1 bài báo

1. **09:00 sáng VN** — Sếp viết task "tìm tin ô nhiễm 23-25/5 trên Google News" vào `search_queue`.
2. **Ông Google** đọc task, gọi Google News RSS → nhận 30 bài.
3. Mỗi bài, ông Google:
   - Lấy URL Google News encoded (chuỗi base64 dài)
   - Hỏi `url_decode_cache`: URL này có chưa? Có thì dùng, chưa thì decode rồi cache lại
   - Tính mã ID duy nhất từ URL publisher đã decode (vd `laodong.vn::12345`)
   - Strip HTML khỏi description (mới fix)
   - Ghi dòng mới vào `articles` với `match_status='pending'`, `body_status='pending'`
4. **Match.timer** 6 tiếng/lần đọc các dòng pending, so với 100 alias ticker:
   - Match (vd "Dabaco" xuất hiện trong title) → ghi vào `per_ticker/DBC.json`, đổi `match_status='matched'`
   - Không match nhưng body chưa fetch → để pending, đợi body-fetcher
   - Không match và body đã fetch → `match_status='unmatched'` (loại bỏ)
5. **Ông Body** song song quét các dòng `body_status='pending'`, gọi Jina đọc nội dung, ghi vào cột `body`, đổi `body_status='fetched'`. Lần match sau sẽ quét lại với body → có thể bắt match mới (tên ticker xuất hiện ở giữa bài).
6. **Match.timer** sau khi update xong → upload `per_ticker/*.json` và NDJSON lên GCS.
7. **Status.timer** 30 phút/lần bundle log + queue stats lên `_setup/logs.tar.gz` để monitor từ xa.

---

## 7. Thêm nguồn mới (vd: CafeF, VnExpress)

Mất khoảng 1-2 giờ code + test. 4 việc:

1. **Viết "phiên dịch"**: 1 file Python ~50 dòng dịch format API của nguồn mới về chuẩn chung `{title, url, description, published_at, source}`.
2. **Khai báo cho sếp**: thêm 1 dòng config "nguồn-mới cần làm 24 chủ đề" → sếp tự enqueue 24 task mới mỗi sáng.
3. **Set quy tắc nghỉ**: thêm 1 dòng `"cafef": X giây` trong settings — X tuỳ tài liệu rate-limit của nguồn.
4. **Tạo "ông thợ mới"**: copy file systemd service của ông Google, đổi tên, `systemctl enable`.

### Những thứ KHÔNG cần đụng tới
- Dedup logic
- Alias matching
- Body fetcher
- Export NDJSON / per_ticker JSON / upload GCS
- Status timer

### Khi nào kiến trúc này KHÔNG hợp
- Nguồn đòi đăng nhập + captcha + thao tác trình duyệt thật → cần Playwright, không fit e2-micro
- Nguồn quá nhanh (>1000 RPS) → cần Redis queue, không hợp SQLite
- Cả 2 trường hợp này hiếm với data ESG VN

---

## 8. Files & ánh xạ

| Component | File code |
|---|---|
| Sếp | `core/queue_builder.py` |
| Bảng giao việc + sổ cái | `core/storage.py` |
| Cache URL Google | `core/url_cache.py` |
| 4 thợ | `workers/runner.py` + `workers/body_fetcher.py` |
| Phiên dịch theo nguồn | `backends/google_rss.py`, `backends/baomoi.py`, `backends/brave.py` |
| Body reader | `body_fetcher/jina.py` + `body_fetcher/fallback.py` |
| Alias matcher | `core/alias_matcher.py` |
| Match + export | `pipeline/match.py` + `pipeline/export.py` |
| Alias builder (1 lần) | `alias_builder/fetch_vietstock.py` |
| Reprocess Google URL cũ (1 lần) | `pipeline/redecode_google.py` |
| systemd units | `deploy/esg-collector-*.{service,timer}` |
| Status bundle script | `deploy/_status.sh` |
| Install fresh VM | `deploy/install.sh` + `deploy/provision_gcp.sh` |
