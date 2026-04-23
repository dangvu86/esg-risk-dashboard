# Controversy Classification — Implementation Plan

> **Context for new session:** Đây là feature kế tiếp sau translation. Pipeline ESG hiện tại đã hoạt động: scan RSS → keyword filter → translate VI/EN → store GCS → display Vercel. Feature này thêm 1 bước classify mức độ controversy cho events có `severity == "Cao"`.

## Goal

Cho mỗi event có `severity == "Cao"` trong `esg_events.json`, gán **controversy level** (Major / Minor / No) + **justification 2 câu**, theo định nghĩa của 2 file PDF:
- `E&S controversy.pdf` — definition cho Environmental + Social
- `CG controversy.pdf` — 4 indicators cho Corporate Governance

## Pipeline hiện tại (đã deploy)

```
Cloud Scheduler → esg_scan (gen2)
  → fetch_company_news (RSS Google News VI)
  → classify_news (keyword filter, no AI) → events có type E/S/G + severity Cao/Trung bình
  → translate_summaries (Gemini Flash-Lite VI→EN) → thêm summary_en
  → write_events to GCS esg_events.json
```

Frontend: Vercel `esg-scan.vercel.app`, toggle VI/EN.

## State đã làm

- 234 events trên GCS, all có `summary_en` (backfilled)
- Cloud Function `esg_scan` gen2 (project `ta-tracking-api`, region `us-central1`)
- Bucket: `esg-risk-dashboard`
- Env: `GEMINI_API_KEY` + `GCS_BUCKET` set on function. Local có `.env` đồng bộ
- Code patterns to reuse:
  - `translator.py` — batch LLM call template (Gemini, retry 429/503, fallback to original)
  - `backfill_translations.py` — one-time GCS download + transform + upload pattern
  - `storage_writer.py` — write events with optimistic locking

## Decisions đã chốt

1. **Model**: Gemini 2.5 Flash-Lite (đã setup, free 1000 RPD). Có thể upgrade lên Flash (250 RPD) hoặc Pro (100 RPD) nếu accuracy yếu sau test 20 samples.
2. **Article fetch**: SKIP cho v1 — chỉ dùng title + summary + source. Material consequences assess từ thông tin hạn chế. Có thể add Jina Reader sau nếu cần.
3. **Company revenue cho 20% rule**: SKIP cho v1 — không apply scale adjustment. Major theo project = Major theo corporate. Add sau nếu connect được Beeslab MCP `get_financials`.
4. **Test approach**: Sample 20 events trước, manual review accuracy, mới backfill all 234.

## Schema additions

Thêm 3 fields vào mỗi event trong `esg_events.json`:

```
controversy_level         "Major" | "Minor" | "No" | ""
controversy_justification "<2-sentence English text>"
controversy_classified_at "<ISO timestamp>"
```

## Flow chi tiết — 4 phases

### Phase 1: Filter input

- Đọc `esg_events.json` từ GCS
- Lọc: chỉ events có `severity == "Cao"` AND `not event.get("controversy_level")` (chưa classify)
- Dataset hiện tại có ~30-50 events Cao trong 234 total

### Phase 2: Classification logic (LLM-driven)

Send to Gemini Flash-Lite với prompt structured. **Không chia thành multiple calls** — 1 call duy nhất per event với prompt rõ ràng để model thực hiện chuỗi reasoning.

**Decision tree (model phải follow trong reasoning):**

#### E + S branch (type == "E" or "S")

```
Q1: Có report trong 10 năm gần đây?
  → No: level = "No", justify = "no public reports in past 10 years"
  → Yes: continue

Q2: Có evidence of resolution?
  Resolution markers (VN): "đã đóng phạt", "đã khắc phục", "vụ án đình chỉ",
                          "Tòa kết luận", "tái bổ nhiệm", "đã giải quyết"
  → Yes: level = "No", justify = "evidence of resolution: <quote>"
  → No: continue

Q3: Trong 5 năm gần đây? (date >= today - 5 years)
  → No (5-10 years old): level = "Minor" (legacy major without resolution)
  → Yes: continue

Q4: Material consequences?
  Material markers:
    - Tử vong / chết người / thương vong (workers)
    - Ô nhiễm phạm vi rộng (community impact)
    - Đình chỉ hoạt động / thu hồi giấy phép
    - Tiền phạt > 1 tỷ VND
    - Cơ quan TW (Bộ TN&MT, Bộ Công an, Tổng cục) vào cuộc
    - Khởi tố hình sự
  → Yes: level = "Major"
  → No: level = "Minor" (legal action without material consequences, poor mgmt control)
```

#### G branch (type == "G")

**Step 1: Map vào 1 trong 4 CG indicators:**

| Indicator | Trigger keywords (VN) |
|---|---|
| 1. Bribery / corruption / business ethics | hối lộ, tham nhũng, vi phạm Luật chống tham nhũng |
| 2. Accurate Reporting | sai lệch BCTC, opinion qualified, audit issued với reservation, late disclosure UBCKNN |
| 3. Tax Behavior | trốn thuế, truy thu thuế, vi phạm quy định thuế, tax dispute |
| 4. Shareholder rights / governance breach | xung đột lợi ích, gia đình trị, cách chức trái thẩm quyền, vi phạm UBCKNN, blocked shareholder voting |

**Step 2: Apply E&S 3-level scheme với G-specific material markers:**

- Material in G = khởi tố hình sự, mất giấy phép, đại diện pháp luật bị bắt, phạt UBCKNN > 500 triệu, dispossession of shareholder rights
- Resolution in G = đã đóng phạt, vụ án bị đình chỉ, đã re-issue financial statement đã sửa, đã được tái bổ nhiệm

#### Justification format (2 sentences English)

```
Sentence 1: WHAT happened + WHICH definition/indicator matched
Sentence 2: WHY this level (cite material consequences, resolution status, scale)
```

Example outputs:

> Major: "Hoa Phat's Dung Quat steel plant was fined 500M VND by the Ministry of Natural Resources for excess wastewater discharge in 2024, matching the E&S 'major pollution event' criterion. Classified as Major because the violation involves material environmental consequences with no public evidence of remediation or resolution."

> Minor: "Vinamilk received an administrative warning from local authorities in 2022 for delayed waste reporting at one bottling plant, fitting the 'legal action without material consequences' minor case. Classified as Minor because the violation reflects poor management control but caused no documented harm to community or workers."

> No: "PNJ was investigated for tax irregularities in 2019 but the case was formally closed by tax authorities in 2021 after settlement. Classified as No controversy because there is documented evidence of resolution within the 10-year window."

### Phase 3: Confidence + flagging

Thêm vào prompt: yêu cầu model output thêm `confidence` (0-100) trong JSON response. Field này KHÔNG lưu vào events.json, chỉ dùng ở classifier output:

- `confidence >= 80` → auto-accept
- `confidence < 80` → log to `_review_needed.json` cho analyst review thủ công

Mục đích: track quality, không block backfill.

### Phase 4: Write back

- Update events.json với 3 new fields
- Skip event nếu LLM trả về invalid level (defensive — keep event unchanged)
- Same optimistic locking pattern như `storage_writer.py:write_events()`

## Prompt template draft

```
You are an ESG analyst specializing in Vietnamese corporate controversies.

EVENT TO CLASSIFY:
- Ticker: {ticker}
- Company: {company}
- Type: {type}  ("E" = Environmental, "S" = Social, "G" = Governance)
- Date: {date}
- Title (VI): {summary}
- Title (EN): {summary_en}
- Source: {source}

DEFINITIONS (apply strictly):

For E and S events:
- Major = public report in past 5 years + NO evidence of resolution + material
  consequences for community/worker/environment
- Minor = (i) legal action / non-compliance WITHOUT material consequences (poor
  mgmt control), OR (ii) legacy major (5-10 years old) without resolution
- No = (i) no reports in past 10 years, OR (ii) evidence of resolution exists

For G events, first map to one of 4 CG indicators:
1. Bribery, corruption, business ethics
2. Accurate financial reporting
3. Tax behavior
4. Shareholder rights / governance breach

Then apply the same 3-level scheme. Material in G context = criminal
prosecution, license revocation, executive arrest, fine > 500M VND, etc.

TODAY'S DATE: {today}  (use to compute past 5y / 10y windows)

REASONING STEPS (perform internally before answering):
1. Determine event recency (past 5y? 5-10y? >10y?)
2. Search for resolution markers in title/source
3. Assess material consequences from title scope
4. For G: identify which of 4 CG indicators
5. Decide level
6. Compute confidence (high if clear-cut, low if borderline)

OUTPUT JSON ONLY (no other text):
{{
  "level": "Major" | "Minor" | "No",
  "cg_indicator": 1 | 2 | 3 | 4 | null,  // only if type == "G"
  "justification": "<exactly 2 sentences in English>",
  "confidence": <integer 0-100>
}}
```

## Implementation files

### New file: `controversy_classifier.py`

Pattern: clone `translator.py` structure
- `classify_event(event_dict, api_key) -> dict`
- `classify_events(events_list, api_key) -> list[dict]`
- 1 event per request (NOT batch — reasoning task, batch hurts accuracy)
- Sleep 4s between calls (15 RPM limit)
- Retry 429/503 same as translator

### New file: `backfill_controversy.py`

Pattern: clone `backfill_translations.py`
- Load events from GCS
- Filter `severity == "Cao"` AND no existing `controversy_level`
- Sample 20 first, write to `_sample_controversy.json` for review
- After review approval, run full

### Modify: `main.py`

After translate step, add classify step for events with `severity == "Cao"`:

```python
if events:
    summaries_en = translate_summaries(...)
    cao_events = [e for e in events if e.get("severity") == "Cao"]
    if cao_events:
        results = classify_events(cao_events, api_key)
        # merge results back into events
```

### Modify: `storage_writer.py`

Add 3 fields in `write_events()` event dict construction (line ~73-84):

```python
"controversy_level": evt.get("controversy_level", ""),
"controversy_justification": evt.get("controversy_justification", ""),
"controversy_classified_at": evt.get("controversy_classified_at", ""),
```

### Modify: `web/app/page.tsx`

- Add `controversy_level` field to `EsgEvent` interface
- Add filter dropdown: "Controversy: All / Major / Minor / No"
- Add column "Controversy" with colored badge (red Major / orange Minor / green No / gray empty)
- Tooltip on hover shows justification

## Validation plan

1. Implement `controversy_classifier.py` + `backfill_controversy.py`
2. Sample 20 events → write to `_sample_controversy.json`
3. **MANUAL REVIEW** with user before full backfill:
   - Spot check accuracy
   - Check edge cases (legacy events, ambiguous resolution)
   - Verify justification quality
4. If accuracy < 70% → upgrade model to Gemini 2.5 Flash → retry sample
5. If accuracy >= 70% → backfill all qualifying events
6. Deploy `main.py` change so production scans get classification

## Open questions / future iterations

- **Resolution detection**: hiện tại dựa vào keywords trong title. Tương lai có thể search Google News với query "ticker + 'đã giải quyết' OR 'đóng phạt' after:event_date" để confirm resolution
- **20% rule**: cần Beeslab MCP `get_financials` để get revenue. V2 enhancement
- **Article body fetch**: Jina Reader (`https://r.jina.ai/<url>`) free tier có thể parse Google News redirect. V2 enhancement
- **G indicator confidence**: model có thể nhầm giữa indicator 1 (corruption) vs 4 (governance breach). Có thể cần few-shot examples trong prompt

## Quota estimate

- 234 events total, ~30-50 có severity Cao
- 1 LLM call per event, no batching
- Initial backfill: ~50 calls × 1 request each = 50 RPD, dùng 5% Flash-Lite quota
- Weekly: ~5 events Cao mới/tuần × 1 call = 5 RPD, < 1% quota
- Combined with translation (already using ~10/tuần) — vẫn thừa thãi free tier

## Reference files

- `cloud-function/translator.py` — template cho LLM batch call pattern
- `cloud-function/backfill_translations.py` — template cho one-time GCS update
- `cloud-function/main.py:31-71` — `_scan_companies()` to integrate
- `cloud-function/storage_writer.py:73-84` — event dict construction
- `web/app/page.tsx:11-19` — EsgEvent interface
- `web/app/page.tsx:155-170` — table cell rendering pattern
- E&S controversy.pdf — primary definition source
- CG controversy.pdf — 4 governance indicators
