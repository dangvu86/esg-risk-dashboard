"""
Keyword-based ESG classifier. No AI needed — uses pattern matching.
Free, unlimited, fast, no rate limits.
"""

import re

from rss_fetcher import derive_aliases

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
    # Positive ESG / CSR
    "hưởng ứng", "đảm bảo an toàn", "tiên phong triển khai",
    "chiến dịch cao điểm", "hỗ trợ", "giải pháp giúp",
    "dấu ấn đặc sắc", "thành tựu", "duy trì hoạt động ổn định",
    "khung quản lý rủi ro", "triển khai thanh toán",
    "phiên bản mới", "tính năng mới", "chuyển đổi số",
    "thành công vì",
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
    # Positive statements / PR
    "hãy coi", "phủ nhận thông tin", "khẳng định",
    "cam kết", "nỗ lực", "đồng hành",
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


def _strip_source_suffix(title):
    """Strip trailing source name from RSS title: '...content - Báo Thanh tra' → '...content'"""
    # Google News RSS format: "Title - Source Name"
    idx = title.rfind(" - ")
    if idx > 0:
        return title[:idx].strip()
    return title


def _contains_any(text, keywords):
    text_lower = _normalize(text)
    return any(kw.lower() in text_lower for kw in keywords)


# Known Vietnamese place names and phrases that cause false positives
# Map: "confusing bigram" → list of preceding words that indicate NOT the company
FALSE_POSITIVE_CONTEXTS = {
    "hòa phát": ["khánh"],          # "Khánh Hòa phát hiện/sinh..."
    "hòa phát hiện": ["khánh"],
}


def _is_about_company(title, company):
    """Check if title is actually about the target company.

    `company` accepts either a single name string or a list of alias strings.
    Match if any alias appears as a substring of the title. Each alias goes
    through FALSE_POSITIVE_CONTEXTS to filter out cases like
    'Khánh Hòa phát hiện' != 'Hòa Phát'.

    No bigram fallback — the previous bigram approach matched too eagerly
    (e.g. bigram "tập đoàn" of "Tập đoàn X" would accept any article about
    any other "Tập đoàn Y"). Aliases from derive_aliases() are sufficient.
    """
    if isinstance(company, str):
        aliases = derive_aliases(company)
    else:
        aliases = list(company)

    title_lower = _normalize(title)

    for alias in aliases:
        alias_lower = _normalize(alias)
        if alias_lower not in title_lower:
            continue
        # Check FALSE_POSITIVE_CONTEXTS for this alias
        is_false_positive = False
        for phrase, bad_prefixes in FALSE_POSITIVE_CONTEXTS.items():
            if phrase.startswith(alias_lower) or alias_lower == phrase:
                for prefix in bad_prefixes:
                    pattern = re.escape(prefix) + r'\s+' + re.escape(alias_lower)
                    if re.search(pattern, title_lower):
                        cleaned = re.sub(pattern, '', title_lower)
                        if alias_lower not in cleaned:
                            is_false_positive = True
        if not is_false_positive:
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
        # Strip source suffix to avoid matching keywords in source name
        # e.g. "BIDV duy trì ổn định - Báo Thanh tra" → "thanh tra" was false positive
        content = _strip_source_suffix(title)

        # Step 1: Is it about this company?
        if not _is_about_company(content, company):
            continue

        # Step 2: Is it noise?
        if _is_noise(content):
            # Exception: if it also contains strong ESG keywords, keep it
            if not _contains_any(content, HIGH_SEVERITY_KEYWORDS):
                continue

        # Step 3: Does it contain ESG keywords?
        if not _contains_any(content, ALL_ESG_KEYWORDS):
            continue

        # Step 4: Classify
        events.append({
            "type": _classify_type(content),
            "date": item.get("date", ""),
            "summary": title,
            "severity": _classify_severity(content),
            "source": item.get("source", ""),
            "url": item.get("url", ""),
        })

    print(f"  Keyword classifier: {len(items)} titles → {len(events)} events")
    return events
