# ESG Collector — Session Handoff

> ⚠️ **LỖI THỜI (2026-05-29):** tài liệu này mô tả thiết kế gốc (24 sub-query
> keyword OR-group). Hệ thống đã chuyển sang **collect-broad / filter-later**:
> 1 flow duy nhất = alias từng công ty (L2) + keyword từng-từ (L1), dùng cho cả
> backfill lẫn daily. Pool keyword OR cũ (`KEYWORD_GROUPS`/`build_queue`) đã bị
> gỡ. Xem `docs/superpowers/specs/2026-05-29-esg-collector-coverage-redesign-design.md`.

Dùng file này để mở session mới và tiếp tục build app `esg-collector/`.

## Bối cảnh

Production hiện tại (`esg-pipeline/cloud-function/main.py`) dùng Google News RSS với query `intitle:"<name>" <esg_keywords>` — coverage thấp (test trên DBC: 2/7 known events). App mới `esg-collector/` đảo logic: search keyword ESG không kèm tên cty → pool dùng chung cho 100 cty → match alias trên title/desc/sapo/body.

Đã test PoC trên DBC 5y (file `experiments/vn_scrape/_test_alias_vs_keyword_rss.py`):
- Baseline (production): 2/7 events
- Method A (alias-only search, gồm subsidiary): 3/7 events
- Method B cũ (keyword + body match qua googlenewsdecoder): fail vì rate-limit 503
- → Method B với architecture mới (Jina Reader thay decode+fetch, queue persistent, chạy 24/24) là hướng đi.

## Quyết định đã chốt

### Architecture 3 layer
- **Layer 1 (Tier 1 RAW)**: pool tin ESG universal cho cả 100 cty, scrape 1 lần dùng mãi
- **Layer 2 (Tier 2 CLASSIFIED)**: per-ticker matched, re-generate từ Tier 1 + aliases bất kỳ lúc nào
- **Layer 3 (AI REVIEWED)**: local Claude session refine, on-demand

### Search backends
| Period | Primary | Secondary |
|---|---|---|
| 0-4y (2022-2024) | Báo Mới (HMAC API, có sapo body) | Google RSS |
| 4-5y (2020-2021) | Google RSS | Brave Search API |

### Storage
- SQLite local trong lúc scrape (`data/articles.db`) — atomic dedup qua `INSERT OR IGNORE` trên `article_id`
- Export NDJSON upload GCS `gs://esg-scan-data/raw_esg/` khi xong
- Tier 2 per-ticker JSON trong `data/per_ticker/<TICKER>.json`
- GCS free tier 5GB đủ (estimate 150-300MB)

### Dedup
- **`article_id`** = `<domain>::<id_extracted>` — primary key. Regex per domain (vnexpress, dantri, tuoitre, cafef, baodautu, baomoi, soha, vietnamindex, daibieunhandan + generic fallback)
- **`url_canonical`** — fallback key. Normalize: https, strip www/m/amp, strip tracking params (utm_*, fbclid...), strip fragment
- Google News encoded link → decode qua `googlenewsdecoder` TRƯỚC khi canonicalize

### Anti rate-limit (chạy 24/24, không gấp)
- Throttle per backend: Google RSS 25s, Báo Mới 15s, Brave 1s — đều ± jitter 33%
- Backoff exponential khi 503/429: Google 5m→30m→2h, Báo Mới 3m→15m→1h
- Persistent queue SQLite — crash-safe resume
- Rotate User-Agent
- Deploy: GCE e2-micro free tier + systemd
- Có thể chạy 2-3 ngày, không gấp

### Aliases
- **Chỉ Vietstock**: `finance.vietstock.vn/<TICKER>/ho-so-doanh-nghiep.htm` → parse "Công ty con" + "Trụ sở" + tên
- Không Báo Mới corpus, không Claude curation (over-engineered)
- 3 cty đã có sẵn: DBC, KDH, DGC (đã copy sang `config/aliases/`)
- 97 cty còn lại: chạy `alias_builder/fetch_vietstock.py` 1 lần (~5 phút)

### Body fetching
- **Jina Reader** (`https://r.jina.ai/<url>`) thay cho `googlenewsdecoder + bs4` — 1 HTTP call, follow redirect, extract clean markdown
- Free tier ~20 RPM. Đăng ký API key (free) lên 200 RPM
- Chỉ fetch cho tin **chưa match alias** qua title/snippet/sapo — tiết kiệm 30-40% calls
- Fallback: bs4 generic extractor nếu Jina fail

### Keywords (24 sub-queries, ~96 từ khoá)
Final list trong `config/keywords.py`:
- E: 6 sub-queries (ô nhiễm, xả thải, phá rừng, cá chết, biến đổi khí hậu, ...)
- S: 6 sub-queries (tai nạn, cháy nổ, GPMB, nợ lương/BHXH, lao động trẻ em, thu hồi đất)
- G: 12 sub-queries (vi phạm, tham nhũng, trốn thuế, thao túng giá, chậm công bố, vỡ nợ, truy nã, ...)

Mỗi sub-query ≤4 OR clauses (RSS limit).

### Estimate runtime
- Google RSS: 24 × 60 chunk × 25s = ~10h
- Báo Mới: 24 × 36 chunk × 15s = ~3.6h
- Brave: 24 × 24 chunk × 1s = ~10 phút
- Body fetch Jina parallel 8 thread: ~30-60 phút cho ~5-10K candidate
- **Tổng 1 lần backfill 5y**: 1 đêm + nửa ngày, OK chạy 2-3 ngày

## Tình trạng đã build (~30% folder skeleton)

Đã tạo:
```
esg-collector/
├── .gitignore                    ✓
├── requirements.txt              ✓
├── HANDOFF.md                    ✓ (file này)
├── config/
│   ├── settings.py               ✓ (paths, throttle, backoff, brave window)
│   ├── keywords.py               ✓ (24 sub-queries final)
│   ├── companies.csv             ✓ (copy từ cloud-function/Top100.csv)
│   └── aliases/
│       ├── DBC.json              ✓
│       ├── KDH.json              ✓
│       └── DGC.json              ✓
├── core/
│   └── canonicalize.py           ✓ (URL normalize + article_id + decode_google_url)
├── backends/                     (empty)
├── body_fetcher/                 (empty)
├── workers/                      (empty)
├── pipeline/                     (empty)
├── alias_builder/                (empty)
├── tests/                        (empty)
├── data/                         (gitignored)
└── logs/                         (gitignored)
```

## Build order session sau

1. `core/storage.py` — SQLite schema (`articles` + `search_queue`), INSERT OR IGNORE, update merge sapo/body
2. `core/queue_builder.py` — sinh task cho 3 backend × 24 keyword × N chunk
3. `backends/base.py` — interface chung: `fetch(query, after, before) -> list[dict]`
4. `backends/google_rss.py` — tham khảo `cloud-function/rss_fetcher.py` (chỉ `build_rss_url`, `parse_rss_xml`)
5. `backends/baomoi.py` — tham khảo `experiments/vn_scrape/baomoi.py` (HMAC signature)
6. `backends/brave.py` — tham khảo `cloud-function/brave_fetcher.py` (API key, freshness param)
7. `body_fetcher/jina.py` — `GET https://r.jina.ai/{url}`, header `X-Return-Format: markdown`, optional `Authorization: Bearer <JINA_API_KEY>`
8. `body_fetcher/fallback.py` — `googlenewsdecoder` + bs4 extract `<article>` + og:description
9. `core/alias_matcher.py` — load `config/aliases/<TICKER>.json`, build regex pool, match text → return list of (ticker, matched_alias, location∈{title|desc|sapo|body})
10. `workers/runner.py` — main loop: pick pending task with `next_attempt<=now`, fetch via backend, save articles, mark done; backoff on rate-limit; 3 process song song (1 per backend)
11. `pipeline/match.py` — quét bảng `articles` chưa match → match alias → ghi Tier 2 JSON
12. `pipeline/export.py` — SQLite → NDJSON → upload GCS
13. `alias_builder/fetch_vietstock.py` — fetch + parse → ghi `config/aliases/<TICKER>.json`

## Verify trước khi deploy

- Test 1 tháng window (`2024-06-01` → `2024-06-30`): 24 query × 3 backend = 72 task → ~30 phút
- Match alias DBC trên pool nhỏ này → verify schema + pipeline đúng
- Sau đó full 5y on GCE

## Files reference từ legacy (chỉ đọc, KHÔNG import)

- `esg-pipeline/cloud-function/rss_fetcher.py` — build_rss_url, parse_rss_xml, generate_date_chunks, KEYWORD_GROUPS cũ (11 sub-queries)
- `esg-pipeline/cloud-function/brave_fetcher.py` — Brave API client
- `esg-pipeline/cloud-function/controversy_classifier.py:103-110` — pattern dùng googlenewsdecoder
- `esg-pipeline/experiments/vn_scrape/baomoi.py` — HMAC signature
- `esg-pipeline/experiments/vn_scrape/collect_alias_sources.py` — Vietstock fetch pattern
- `esg-pipeline/experiments/vn_scrape/_test_alias_vs_keyword_rss.py` — PoC test 3 method (baseline/A/B), known events DBC

## Câu hỏi mở (chưa quyết)

1. **Brave API key**: anh đã có chưa? Nếu chưa → skip Brave backend, chỉ Google + Báo Mới ban đầu (vẫn cover được 0-4y tốt, 4-5y sparse).
2. **Jina API key**: optional, free tier 20 RPM đủ test, đăng ký lên 200 RPM cho production. Đăng ký 1 phút tại jina.ai.
3. **Báo Mới rate limit thực tế**: chưa probe. Em set default 15s — có thể tune sau khi chạy thử 100 query đầu.
4. **GCS bucket**: `gs://esg-scan-data/` — đã tồn tại chưa? Nếu chưa cần `gsutil mb` trước khi deploy.
5. **GCE e2-micro deploy**: có thể defer — chạy local máy anh trước cho lần backfill đầu, deploy sau.

## Lệnh start session mới

```
cd "d:/Claude/ESG scan/esg-pipeline/esg-collector"
cat HANDOFF.md
# Tiếp tục từ step 1 trong "Build order session sau"
```
