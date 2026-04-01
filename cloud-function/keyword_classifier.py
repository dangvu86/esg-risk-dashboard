"""
Keyword-based ESG classifier. No AI needed — uses pattern matching.
Free, unlimited, fast, no rate limits.
"""

import re
from datetime import datetime
from difflib import SequenceMatcher

# --- NOISE: topics that are NOT ESG risk events ---
NOISE_KEYWORDS = [
    # Sports / entertainment
    "bóng chuyền", "bóng đá", "VĐV", "giải đấu", "CLB", "huấn luyện",
    "thể thao", "vô địch", "huy chương", "cup", "giải vô địch",
    # Positive business
    "doanh thu", "lợi nhuận tăng", "kết quả kinh doanh", "mở rộng",
    "tuyển dụng", "khen thưởng", "giải thưởng", "top ", "bảng xếp hạng",
    "cổ tức", "tăng trưởng", "hợp tác", "ký kết", "ra mắt", "khánh thành",
    "khởi công", "động thổ", "IPO", "niêm yết", "phục hồi",
    "công nghệ xanh", "năng lượng tái tạo", "phát triển bền vững",
    "đạt chuẩn", "ESG tích cực",
    # Personal / lifestyle
    "du học", "thiếu gia", "nghìn tỷ", "tài sản", "giàu nhất",
    "đám cưới", "gia thế",
    # Stock market commentary (not ESG)
    "biến động ra sao", "cổ phiếu ra sao", "tương lai", "dự báo",
    "triển vọng", "mục tiêu giá", "khuyến nghị", "phân tích kỹ thuật",
    "hé lộ", "bí mật", "bất ngờ", "khát khao",
    # M&A (neutral business activity)
    "thâu tóm", "mua lại", "sáp nhập", "chi tỷ",
    # Analysis about others
    "đối thủ của",
]

# --- ESG NEGATIVE keywords ---

# E: Environment
ENV_KEYWORDS = [
    "ô nhiễm", "xả thải", "khí thải", "đổ thải", "nước thải",
    "mùi hôi", "rác thải", "chất thải", "bụi mù",
    "vi phạm môi trường", "xử phạt môi trường",
    "khai thác trái phép", "quặng lậu", "khoáng sản trái phép",
    "không có ĐTM", "đánh giá tác động môi trường",
]

# S: Social
SOCIAL_KEYWORDS = [
    "tai nạn lao động", "tử vong", "chết người", "thương vong",
    "cháy nhà máy", "cháy xưởng", "cháy lớn",
    "đình công", "biểu tình", "an toàn lao động", "ngộ độc",
    "sập công trình", "sập nhà xưởng", "nổ nhà máy",
]

# G: Governance
GOV_KEYWORDS = [
    "khởi tố", "bắt giam", "bắt tạm giam", "truy tố",
    "bị bắt", "bị điều tra",
    "xử phạt", "bị phạt", "liên tiếp bị xử phạt",
    "vi phạm pháp luật", "vi phạm quy định",
    "UBCKNN", "thanh tra", "kiểm toán nhà nước",
    "tham nhũng", "hối lộ", "thất thoát vốn", "trốn thuế",
    "sai phạm", "kỷ luật", "cách chức",
    "thoái vốn", "xung đột lợi ích",
    "chậm nộp", "không công bố", "che giấu",
    "bố con", "gia đình trị", "bố làm chủ tịch",
    "quặng lậu",  # also G when related to illegal business
]

# High severity
HIGH_SEVERITY_KEYWORDS = [
    "khởi tố", "bắt giam", "bắt tạm giam", "truy tố", "bị bắt",
    "tử vong", "chết người", "thiệt mạng",
    "đình chỉ hoạt động", "thu hồi giấy phép",
    "Bộ TN&MT", "Bộ Công an", "Tổng cục",
    "ô nhiễm nghiêm trọng", "triệu tấn", "hàng chục hecta",
    "khai thác trái phép", "quặng lậu",
    "cháy lớn", "nổ lớn", "sập",
    "liên tiếp bị xử phạt",
]

ALL_ESG_KEYWORDS = ENV_KEYWORDS + SOCIAL_KEYWORDS + GOV_KEYWORDS


def _normalize(text):
    return text.lower().strip()


def _contains_any(text, keywords):
    text_lower = _normalize(text)
    return any(kw.lower() in text_lower for kw in keywords)


def _is_about_company(title, company_name):
    """Check if title is actually about the target company."""
    title_lower = _normalize(title)
    name_lower = _normalize(company_name)

    if name_lower in title_lower:
        return True

    # Check bigrams for multi-word names
    name_parts = name_lower.split()
    if len(name_parts) >= 2:
        for i in range(len(name_parts) - 1):
            bigram = f"{name_parts[i]} {name_parts[i+1]}"
            if bigram in title_lower:
                return True

    return False


def _is_noise(title):
    """Check if title is noise (not ESG risk)."""
    return _contains_any(title, NOISE_KEYWORDS)


def _classify_type(title):
    """Classify: E, S, or G."""
    title_lower = _normalize(title)

    e_score = sum(1 for kw in ENV_KEYWORDS if kw.lower() in title_lower)
    s_score = sum(1 for kw in SOCIAL_KEYWORDS if kw.lower() in title_lower)
    g_score = sum(1 for kw in GOV_KEYWORDS if kw.lower() in title_lower)

    if e_score > g_score and e_score > s_score:
        return "E"
    if s_score > e_score and s_score > g_score:
        return "S"
    if g_score > 0:
        return "G"
    if e_score > 0:
        return "E"
    if s_score > 0:
        return "S"
    return "G"


def _classify_severity(title):
    """Classify: Cao or Trung bình."""
    if _contains_any(title, HIGH_SEVERITY_KEYWORDS):
        return "Cao"

    title_lower = _normalize(title)
    fine_match = re.search(r'(\d+[\.,]?\d*)\s*(tỷ|triệu)', title_lower)
    if fine_match:
        amount = float(fine_match.group(1).replace(",", "."))
        unit = fine_match.group(2)
        if unit == "tỷ":
            return "Cao"
        if unit == "triệu" and amount >= 500:
            return "Cao"

    return "Trung bình"


def _extract_key_phrases(title):
    """Extract key phrases for better dedup comparison."""
    title_lower = _normalize(title)
    # Remove common filler words
    fillers = ["của", "tại", "và", "cho", "với", "được", "bị", "là",
               "có", "từ", "trong", "theo", "về", "đã", "sẽ", "đang"]
    words = title_lower.split()
    key_words = [w for w in words if w not in fillers and len(w) > 1]
    return " ".join(key_words)


def _dedup_events(events, preferred_sources=None):
    """Remove duplicates: same event within ±7 days + similar content.
    Prefer articles from major sources.
    """
    if not events:
        return events

    if preferred_sources is None:
        preferred_sources = ["vnexpress", "tuổi trẻ", "thanh niên",
                             "lao động", "cafef", "dân trí", "báo thanh tra"]

    def source_rank(source):
        s = _normalize(source)
        for i, pref in enumerate(preferred_sources):
            if pref in s:
                return i
        return 100

    # Sort by date desc, then by source preference
    events.sort(key=lambda e: (e.get("date", ""), -source_rank(e.get("source", ""))), reverse=True)

    unique = []
    for evt in events:
        is_dup = False
        evt_key = _extract_key_phrases(evt["summary"])

        for existing in unique:
            # Check date proximity
            date_close = False
            if evt["date"] and existing["date"]:
                try:
                    d1 = datetime.strptime(evt["date"], "%Y-%m-%d")
                    d2 = datetime.strptime(existing["date"], "%Y-%m-%d")
                    date_close = abs((d1 - d2).days) <= 7
                except ValueError:
                    date_close = evt["date"] == existing["date"]
            else:
                date_close = evt["date"] == existing["date"]

            if not date_close:
                continue

            # Check content similarity
            existing_key = _extract_key_phrases(existing["summary"])
            similarity = SequenceMatcher(None, evt_key, existing_key).ratio()

            # Also check if they share many key ESG words
            evt_esg = set(kw.lower() for kw in ALL_ESG_KEYWORDS if kw.lower() in evt_key)
            exist_esg = set(kw.lower() for kw in ALL_ESG_KEYWORDS if kw.lower() in existing_key)
            shared_esg = evt_esg & exist_esg

            if similarity > 0.35 or len(shared_esg) >= 2:
                is_dup = True
                break

        if not is_dup:
            unique.append(evt)

    return unique


def classify_news(company, ticker, items, api_key=None):
    """
    Keyword-based ESG classification. Same interface as gemini_classifier.
    api_key param kept for compatibility but not used.
    """
    if not items:
        return []

    events = []
    for item in items:
        title = item.get("title", "")

        # Step 1: Is it about this company?
        if not _is_about_company(title, company):
            continue

        # Step 2: Is it noise?
        if _is_noise(title):
            # Exception: if it also contains strong ESG keywords, keep it
            if not _contains_any(title, HIGH_SEVERITY_KEYWORDS):
                continue

        # Step 3: Does it contain ESG keywords?
        if not _contains_any(title, ALL_ESG_KEYWORDS):
            continue

        # Step 4: Classify
        events.append({
            "type": _classify_type(title),
            "date": item.get("date", ""),
            "summary": title,
            "severity": _classify_severity(title),
            "source": item.get("source", ""),
            "url": item.get("url", ""),
        })

    # Step 5: Dedup
    events = _dedup_events(events)

    print(f"  Keyword classifier: {len(items)} titles → {len(events)} events")
    return events
