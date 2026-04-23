# ESG Scan Pipeline

Chi tiết 7 stage xử lý từ Google News RSS → events cuối cùng trên dashboard.

## Overview

```
Google News RSS
    ↓
[1] RSS Fetch        ← rss_fetcher.py
    ↓
[2] Keyword Filter   ← keyword_classifier.py
    ↓
[3] Sentiment Filter ← sentiment_filter.py    (LLM)
    ↓
[4] Semantic Dedup   ← semantic_dedup.py      (LLM)
    ↓
[5] Translate        ← translator.py          (LLM)
    ↓
[6] Controversy      ← controversy_classifier.py (LLM, chỉ cho Cao)
    ↓
[7] Write GCS        ← storage_writer.py      (normalized hash dedup)
    ↓
Dashboard (Vercel)
```

---

## [1] RSS Fetch

**Làm gì:** Với mỗi ticker, search Google News RSS theo 3 nhóm keyword (E, S, G). Mỗi nhóm split thành nhiều sub-query (≤4 OR keywords / sub-query) — bắt buộc vì Google News RSS silently trả 0 results khi `intitle:"phrase"` kết hợp >6 OR clauses (gặp trên prod với G query 7-OR cũ → luôn trả 0).

**KEYWORD_GROUPS** ([rss_fetcher.py](rss_fetcher.py)) — 8 sub-queries tổng:
```python
"E": [
    "ô nhiễm OR xả thải OR môi trường OR khí thải",
    "nước thải OR mùi hôi OR rác thải OR chất thải",
],
"S": [
    "tai nạn OR tử vong OR đình công OR an toàn lao động",
    "cháy nổ OR sập OR ngộ độc OR thương vong",
],
"G": [
    "vi phạm OR xử phạt OR khởi tố OR thanh tra",
    "sai phạm OR bị phạt OR truy thu OR đấu thầu",
    "bêu tên OR tầm ngắm OR danh sách đen OR UBCKNN",
    "khiếu kiện OR khiếu nại OR giám sát OR chậm tiến độ",
],
```

**Dedup:** Sau khi merge tất cả sub-query, dedup theo exact title (lower-case).

**Output:** RSS items thô với title + source + date + google_news_url + keyword_group (E/S/G).

**Lượng:** Weekly scan ~100 companies × 8 sub-queries × 1 chunk = **~3000-5000 raw items** (sau dedup intra-company).

---

## [2] Keyword Filter (rule-based, 0 LLM)

**Làm gì:** Phân loại title bằng keyword matching:
- Có keyword E / S / G nào → gán `type`
- Check title có thực sự về công ty đó không (`_is_about_company`) — VD "Hòa Phát" phải xuất hiện, không chỉ ngành "thép"
- Check `NOISE_KEYWORDS` (promotion, celebrity, sports, stock tips...) → skip
- Gán `severity`:
  - "Cao" nếu match keyword nặng (khởi tố, phạt tiền lớn, tử vong, ô nhiễm rộng)
  - "Trung bình" nếu nhẹ hơn

**Kết quả:** ~100-200 events qualified per scan.

**Giới hạn:** Chỉ nhìn keyword, không hiểu context. VD:
- "Quỹ Thiện Tâm hỗ trợ gia đình nạn nhân **tử vong**" → match "tử vong" → S event (SAI — từ thiện)
- "Chủ tịch Vingroup: Tận dụng tái tạo thay vì gia tăng **ô nhiễm**" → match "ô nhiễm" → E event (SAI — cam kết sustainability)

→ Cần stage [3] để sửa.

---

## [3] Sentiment Filter (LLM, batch=5)

**Làm gì:** Gửi từng nhóm 5 events qua LLM, hỏi: "Mỗi event này có phải RISK thật sự, hay chỉ là tin positive/neutral vô tình dính keyword?"

**Drop (`not_risk`):**
- **CSR/từ thiện**: "Quỹ Thiện Tâm hỗ trợ...", "tài trợ", "ủng hộ gia đình nạn nhân"
- **Phát biểu CSR của lãnh đạo**: "Chủ tịch: Tận dụng tái tạo thay vì ô nhiễm"
- **Đầu tư vào phòng chống**: "Công ty đầu tư hệ thống xử lý khí thải mới"
- **Cty nhận giải thưởng** về xanh, bền vững
- **Cướp/trộm mà cty là nơi bị hại**: "Nhóm cướp Sacombank bị bắt" (ngân hàng là victim)
- **Generic advisory**: "BIDV cảnh báo người dùng..." (không phải BIDV vi phạm, chỉ tư vấn)
- **M&A/thoái vốn không có vi phạm**: "Vinpearl giảm cổ phiếu VIC", "Gemadept thoái vốn cảng"

**Keep (`risk`):** Mặc định khi mơ hồ. Chỉ drop khi CHẮC chắn là positive/unrelated.

**Explicit kept (không được drop):**
- Bất kỳ "bị xử phạt", "bị phạt", "truy thu thuế", "bị thanh tra", "bị khởi tố", "vi phạm", "sai phạm"
- Kết luận thanh tra có "tồn tại", "chưa tuân thủ"
- Tai nạn lao động, tử vong công nhân AT nhà máy
- Ô nhiễm, xả thải BY công ty
- Governance: HĐQT bị bắt, gian lận, xung đột lợi ích

**Tại sao batch=5:** Trước dùng batch=20, model bị "context mixing" — thấy 19 events là risk thì vote event thứ 20 cũng risk, dù nên drop. Batch=5 + instruction "analyze INDEPENDENTLY" → model xét mỗi title riêng.

**Quota:** ~145 events ÷ 5 = ~30 calls/backfill. Weekly ~20 calls.

---

## [4] Semantic Dedup (LLM, Layer B)

**Làm gì:** Với mỗi ticker, chia events vào window 30 ngày. Window có ≥2 events → gọi LLM: "Group vào clusters nếu cùng incident". Keep earliest của mỗi cluster, drop rest.

**Ví dụ cluster DGC (9 events → 1):**
```
2026-03-17: Khởi tố tại Tập đoàn Hóa chất Đức Giang...
2026-03-19: Cha con Chủ tịch bị bắt, triệu tập cổ đông...
2026-03-24: DGC công bố tin bất thường sau bắt...
2026-04-02: Khởi tố loạt lãnh đạo, cổ phiếu sàn...
2026-04-06: DGC phát thông báo sau vụ khởi tố...
2026-04-07: Công ty con trong vụ án sai phạm 2700 tỷ...
2026-04-09: Lãnh đạo bị khởi tố, DGC vẫn muốn làm pin xe điện...
```
→ Cùng một "DGC leadership prosecution crisis" → keep 1, drop 8.

**Strict criteria:** Chỉ cluster events CÙNG specific incident (same people, same action, same subject). Không cluster events khác bản chất kể cả wording gần giống.

**Quota:** ~30-40 calls/backfill, weekly ~0-5 calls.

---

## [5] Translate (LLM)

**Làm gì:** Dịch tất cả events còn lại VN → EN, Google Translate style:
- Tên riêng VN transliterate bỏ dấu: "Hóa chất Đức Giang" → "Duc Giang Chemicals"
- Tên người: "Phạm Nhật Vượng" → "Pham Nhat Vuong"
- Cơ quan nhà nước dịch nghĩa: "Bộ TN&MT" → "Ministry of Natural Resources and Environment"
- Ticker/số/đơn vị tiền tệ giữ nguyên (DGC, 500 triệu VND, tỷ VND → billion VND)
- Strip source suffix ("- CafeF", "- Znews")

Batch 30 titles/call.

**Quota:** ~5 calls/scan.

---

## [6] Controversy Classifier (LLM, chỉ cho Cao)

**Làm gì:** Với mỗi event có `severity == "Cao"`:

1. **Fetch article body** qua Jina Reader (`https://r.jina.ai/<real_url>`) sau khi decode Google News URL qua `googlenewsdecoder`
2. **Look up company revenue** cho năm của event từ [Top100.csv](Top100.csv) — `get_revenue_for_year()` chọn năm gần nhất nếu không có exact match
3. **Gọi LLM** để gán `controversy_level` ∈ {Major, Minor, No} + justification. Logic phân nhánh theo `event.type`:

   **E hoặc S event** → dùng trực tiếp định nghĩa Major/Minor/No trong [E&S controversy.pdf](../../E&S%20controversy.pdf). Tiêu chí: có report trong 5y/10y? có evidence resolution? có material consequence cho community/worker/environment?

   **G event** → 2 bước:
   1. **Justification**: map sự việc vào 1 trong 4 indicators trong [CG controversy.pdf](../../CG%20controversy.pdf) — (1) bribery/corruption/business ethics, (2) accurate financial reporting, (3) tax behavior, (4) shareholder rights / governance breach — dùng indicator này để viết justification + ghi vào `cg_indicator`.
   2. **Level**: quay về thang Major/Minor/No của E&S controversy.pdf để gán level (CG file không có thang riêng).

   **Corporate-level consolidation (20% rule)** — áp dụng SAU khi đã có base level, cho cả E/S/G:
   - Nếu article cho thấy event chỉ scope ở 1 subsidiary/plant/project (không phải toàn corporate) **VÀ** một trong:
     - revenue của unit đó < 20% annual revenue của mẹ, **HOẶC**
     - parent ownership ở affected entity < 20%
   - → downgrade **Major → Minor**. Không downgrade Minor → No.
   - Nếu scope/ownership không rõ → giữ nguyên base level.
4. **Output**: `level` (Major/Minor/No) + 2-sentence English justification + `cg_indicator` (chỉ cho G)

**Ghi vào event:**
```json
{
  "controversy_level": "Major",
  "controversy_justification": "Three board members were prosecuted...",
  "controversy_classified_at": "2026-04-23T03:10:57Z"
}
```

**Quota:** 1 call/Cao event. Weekly ~5 calls. Backfill đã chạy 75 events.

---

## [7] Write GCS (rule-based dedup, Layer A)

**Làm gì:**
- Read current `esg_events.json` từ GCS (với optimistic lock via `if_generation_match`)
- Merge new events, dedup by hash
- **Hash = MD5(`ticker | normalize_title(summary)`)**
  - `normalize_title`: strip " - Source" suffix, strip diacritics, lowercase, remove punctuation
  - **Không include date** → "xxx - Znews" 2026-04-18 và "xxx - CafeF" 2026-04-20 có cùng hash → drop 1 (keep earliest)
- Sort by date desc
- Upload back to GCS với optimistic lock (tránh race condition khi scan chạy concurrent)

**Quota:** 0 LLM calls. Pure string processing.

---

## Tóm tắt impact

| Stage | Method | Input | Output | Drop rate |
|-------|--------|-------|--------|-----------|
| [1] RSS | Google News API | — | ~1500 raw | — |
| [2] Keyword | Regex + company match | 1500 | ~200 | -87% |
| [3] Sentiment | LLM batch=5 | 200 | ~180 | -10% |
| [4] Semantic dedup | LLM per-ticker window | 180 | ~145 | -20% |
| [5] Translate | LLM batch=30 | 145 | 145 | 0% |
| [6] Controversy | LLM per Cao event | 30 Cao | 30 (labeled) | 0% |
| [7] Write + Layer A hash | rule | 145 | 145 | -few% |

**Backfill thực tế (lần đầu):** 237 → 145 (-39%).

**Weekly scan ongoing:** ~5-15 new events/tuần sau khi qua tất cả filter.

---

## LLM Provider

Tất cả LLM calls qua **registry provider** trong [controversy_classifier.py](controversy_classifier.py):

| Provider | Key env | Default model | Sleep (RPM) |
|----------|---------|---------------|-------------|
| groq | `GROQ_API_KEY` | `llama-3.3-70b-versatile` | 2s (30 RPM) |
| cerebras | `CEREBRAS_API_KEY` | `llama-3.3-70b` | 2s |
| openrouter | `OPENROUTER_API_KEY` | `deepseek/deepseek-r1:free` | 3s |
| deepseek | `DEEPSEEK_API_KEY` | `deepseek-chat` | 2s |
| openai | `OPENAI_API_KEY` | `gpt-4o-mini` | 1s |
| mistral | `MISTRAL_API_KEY` | `mistral-small-latest` | 1s |
| gemini | `GEMINI_API_KEY` | `gemini-2.5-flash-lite` | 4s (15 RPM) |

**Switch provider:** chỉ sửa `.env`:
```bash
LLM_PROVIDER=cerebras     # optional, auto-pick first key available
LLM_MODEL=<override>      # optional, dùng default_model nếu không set
CEREBRAS_API_KEY=csk-...
```

**Hiện production dùng:**
```
LLM_PROVIDER=groq (auto-pick)
LLM_MODEL=meta-llama/llama-4-scout-17b-16e-instruct
GROQ_API_KEY=gsk_...
```

**Total LLM calls per weekly scan:** ~30-40 (dư trong 1000 RPD Groq Llama 4 Scout).

---

## Files

| File | Role |
|------|------|
| [main.py](main.py) | Cloud Function entry + orchestrator |
| [rss_fetcher.py](rss_fetcher.py) | Stage 1 + load_companies + load_revenues |
| [link_resolver.py](link_resolver.py) | Pass-through Google News URL (real resolve in controversy_classifier via gnewsdecoder) |
| [keyword_classifier.py](keyword_classifier.py) | Stage 2 |
| [sentiment_filter.py](sentiment_filter.py) | Stage 3 |
| [semantic_dedup.py](semantic_dedup.py) | Stage 4 |
| [translator.py](translator.py) | Stage 5 |
| [controversy_classifier.py](controversy_classifier.py) | Stage 6 + LLM provider registry |
| [storage_writer.py](storage_writer.py) | Stage 7 + optimistic lock write |
| [backfill_translations.py](backfill_translations.py) | One-time backfill chỉ cho translation |
| [backfill_controversy.py](backfill_controversy.py) | One-time backfill chỉ cho classify Cao |
| [backfill_clean.py](backfill_clean.py) | One-time backfill sentiment + 2-layer dedup |

---

## Chạy backfill thủ công

```bash
cd cloud-function

# Sample 20 events (local, không upload)
python backfill_controversy.py
# Full: 75+ Cao events lên GCS
python backfill_controversy.py --full

# Sentiment + dedup (dry run xem diff)
python backfill_clean.py
# Apply lên GCS
python backfill_clean.py --apply
```

## Deploy

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
  --set-env-vars "GEMINI_API_KEY=...,GCS_BUCKET=esg-risk-dashboard,GROQ_API_KEY=gsk_...,LLM_MODEL=meta-llama/llama-4-scout-17b-16e-instruct"
```

## Trigger scan

```bash
# Toàn bộ top100 (1 lần, gen2 timeout 60 phút)
curl "https://us-central1-ta-tracking-api.cloudfunctions.net/esg_scan?mode=auto"

# 1 ticker
curl "https://us-central1-ta-tracking-api.cloudfunctions.net/esg_scan?tickers=HPG"

# Batch 5 companies (1..20)
curl "https://us-central1-ta-tracking-api.cloudfunctions.net/esg_scan?batch=3"
```
