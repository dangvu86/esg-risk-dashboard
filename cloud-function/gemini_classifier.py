"""
Gemini 2.5 Flash classifier for ESG news filtering and classification.
Free tier: 15 RPM, 1000 RPD.
"""

import json
import os
import time
import urllib.request


GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"

CLASSIFY_PROMPT = """Bạn là chuyên gia phân tích rủi ro ESG cho thị trường chứng khoán Việt Nam. Nhiệm vụ: tìm TẤT CẢ sự kiện ESG tiêu cực liên quan đến công ty.

Công ty: {company} (mã: {ticker})

## Bước 1: LỌC — chỉ bỏ khi CHẮC CHẮN không liên quan
- BỎ nếu tiêu đề KHÔNG nhắc đến "{company}" hoặc "{ticker}" (lưu ý tên VN hay trùng từ, vd "Hòa Phát" ≠ "Khánh Hòa phát hiện")
- BỎ nếu hoàn toàn tích cực/trung tính (doanh thu, mở rộng, tuyển dụng, khen thưởng)
- GIỮ LẠI nếu có BẤT KỲ yếu tố tiêu cực nào: ô nhiễm, vi phạm, xử phạt, khởi tố, bắt giam, tai nạn, đình công, thanh tra, kiểm toán phát hiện sai phạm, khai thác trái phép, quặng lậu, thất thoát vốn, vi phạm quản trị (bố-con cùng lãnh đạo, vv)
- KHI NGHI NGỜ → GIỮ LẠI. Thà thừa còn hơn thiếu.

## Bước 2: DEDUP — nhóm tin cùng sự kiện
Hai tiêu đề là CÙNG sự kiện khi:
- Cùng vụ việc cụ thể (cùng vụ khởi tố, cùng lần phạt, cùng vụ tai nạn)
- Cùng thời điểm (cách nhau ≤ 3 ngày) VÀ cùng nội dung

Hai tiêu đề là KHÁC sự kiện khi:
- Khác thời điểm (cách nhau > 1 tháng) → chắc chắn khác sự kiện
- Cùng chủ đề nhưng khác vụ việc cụ thể (vd: phạt 2021 ≠ phạt 2023)
- Khác khía cạnh hoàn toàn (vd: ô nhiễm ≠ quặng lậu ≠ quản trị gia đình)

Mỗi sự kiện duy nhất → giữ 1 entry, ưu tiên nguồn: VnExpress > Tuổi Trẻ > Thanh Niên > Lao Động > CafeF > Dân Trí > khác.

## Bước 3: PHÂN LOẠI
- type: "E" (ô nhiễm, xả thải, khí thải, đổ thải, vi phạm môi trường), "S" (tai nạn lao động, tử vong, đình công, an toàn), "G" (khởi tố, xử phạt hành chính, vi phạm quản trị, khai thác trái phép, thất thoát vốn, thanh tra)
- severity:
  * "Cao": tử vong, khởi tố/bắt giam, phạt >500 triệu, đình chỉ hoạt động, bộ/ngành/thanh tra vào cuộc, ô nhiễm nghiêm trọng ảnh hưởng dân cư, khai thác trái phép quy mô lớn
  * "Trung bình": phạt <500 triệu, khiếu nại, vi phạm hành chính nhẹ, cảnh cáo
- summary: tóm tắt 1 dòng tiếng Việt, nêu rõ CHI TIẾT cụ thể (số tiền phạt, địa điểm, cơ quan xử lý)

Trả về JSON array. Nếu không có tin ESG tiêu cực nào, trả về [].
Chỉ trả JSON, không giải thích.

Format:
[
  {{"index": 0, "type": "E", "severity": "Cao", "summary": "Bị phạt 500 triệu do xả thải vượt chuẩn tại nhà máy Dung Quất"}}
]

Danh sách tiêu đề:
{titles}"""


MAX_TITLES_PER_CALL = 80  # Gemini can handle ~80 titles per call reliably


def _call_gemini(prompt, api_key, retries=3):
    """Single Gemini API call with retry for 429/503. Returns parsed JSON list or []."""
    url = f"{GEMINI_API_URL}?key={api_key}"
    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }).encode("utf-8")

    for attempt in range(retries):
        req = urllib.request.Request(url, data=payload, headers={
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            text = result["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(text)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if e.code in (429, 503) and attempt < retries - 1:
                wait = 30 * (attempt + 1)
                print(f"Gemini {e.code}, retry {attempt+1}/{retries} in {wait}s...")
                time.sleep(wait)
                continue
            print(f"Gemini API error {e.code}: {body[:300]}")
            return []
        except Exception as e:
            if attempt < retries - 1:
                print(f"Gemini error: {e}, retry {attempt+1}/{retries} in 15s...")
                time.sleep(15)
                continue
            print(f"Gemini call error: {type(e).__name__}: {e}")
            return []
    return []


def classify_news(company, ticker, items, api_key=None):
    """
    Send titles to Gemini for filtering and classification.
    Splits large lists into chunks of MAX_TITLES_PER_CALL.
    Returns list of classified ESG events (deduped by event).
    """
    if not items:
        return []

    api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        print("ERROR: GEMINI_API_KEY not set")
        return []

    all_classified = []

    # Process in chunks
    for chunk_start in range(0, len(items), MAX_TITLES_PER_CALL):
        chunk = items[chunk_start:chunk_start + MAX_TITLES_PER_CALL]

        titles_text = "\n".join(
            f"{i}. [{item['date']}] [{item['source']}] {item['title']}"
            for i, item in enumerate(chunk)
        )

        prompt = CLASSIFY_PROMPT.format(
            company=company,
            ticker=ticker,
            titles=titles_text,
        )

        print(f"  Gemini call: {len(chunk)} titles (offset {chunk_start})...")
        events = _call_gemini(prompt, api_key)

        # Map back to original items
        for evt in events:
            idx = evt.get("index", -1)
            if 0 <= idx < len(chunk):
                original = chunk[idx]
                all_classified.append({
                    "type": evt.get("type", ""),
                    "date": original.get("date", ""),
                    "summary": evt.get("summary", ""),
                    "severity": evt.get("severity", ""),
                    "source": original.get("source", ""),
                    "url": original.get("url", ""),
                })

        # Rate limit: wait between chunks
        if chunk_start + MAX_TITLES_PER_CALL < len(items):
            time.sleep(5)

    return all_classified
