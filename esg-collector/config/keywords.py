"""Single source of truth for ESG search and classification vocabulary.

ESG_KEYWORDS   — tagged master list (term, tag); search_terms() derives the L1
                 single-term search net, esg_terms() feeds body classification.
NOISE_KEYWORDS / HIGH_SEVERITY_KEYWORDS — classifier blacklists that suppress or boost article scores.
"""

# ---------------------------------------------------------------------------
# Unified ESG vocabulary — single source of truth for search AND classification
# Ported verbatim from cloud-function/keyword_classifier.py
# ---------------------------------------------------------------------------

# Master tagged list: (term, tag) where tag ∈ {"E", "S", "G"}
# ENV_KEYWORDS → "E", SOCIAL_KEYWORDS → "S", GOV_KEYWORDS → "G"
ESG_KEYWORDS: list[tuple[str, str]] = [
    # --- Environment (E) ---
    ("ô nhiễm", "E"),
    ("xả thải", "E"),
    ("khí thải", "E"),
    ("đổ thải", "E"),
    ("nước thải", "E"),
    ("mùi hôi", "E"),
    ("rác thải", "E"),
    ("chất thải", "E"),
    ("bụi mù", "E"),
    ("vi phạm môi trường", "E"),
    ("xử phạt môi trường", "E"),
    ("khai thác trái phép", "E"),
    ("quặng lậu", "E"),
    ("khoáng sản trái phép", "E"),
    ("không có ĐTM", "E"),
    ("đánh giá tác động môi trường", "E"),
    # --- Social (S) ---
    ("tai nạn lao động", "S"),
    ("tử vong", "S"),
    ("chết người", "S"),
    ("thương vong", "S"),
    ("cháy nhà máy", "S"),
    ("cháy xưởng", "S"),
    ("cháy lớn", "S"),
    ("đình công", "S"),
    ("biểu tình", "S"),
    ("an toàn lao động", "S"),
    ("ngộ độc", "S"),
    ("sập công trình", "S"),
    ("sập nhà xưởng", "S"),
    ("nổ nhà máy", "S"),
    # --- Governance (G) ---
    ("khởi tố", "G"),
    ("bắt giam", "G"),
    ("bắt tạm giam", "G"),
    ("truy tố", "G"),
    ("bị bắt", "G"),
    ("bị điều tra", "G"),
    ("xử phạt", "G"),
    ("bị phạt", "G"),
    ("liên tiếp bị xử phạt", "G"),
    ("vi phạm pháp luật", "G"),
    ("vi phạm quy định", "G"),
    ("UBCKNN", "G"),
    ("thanh tra", "G"),
    ("kiểm toán nhà nước", "G"),
    ("tham nhũng", "G"),
    ("hối lộ", "G"),
    ("thất thoát vốn", "G"),
    ("trốn thuế", "G"),
    ("sai phạm", "G"),
    ("kỷ luật", "G"),
    ("cách chức", "G"),
    ("thoái vốn", "G"),
    ("xung đột lợi ích", "G"),
    ("chậm nộp", "G"),
    ("không công bố", "G"),
    ("che giấu", "G"),
    ("bố con", "G"),
    ("gia đình trị", "G"),
    ("bố làm chủ tịch", "G"),
    ("quặng lậu", "G"),  # also G when related to illegal business
]

# Noise keywords — topics that are NOT ESG risk events
# Ported verbatim from cloud-function/keyword_classifier.py
NOISE_KEYWORDS: list[str] = [
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

# High-severity keywords — ported verbatim from cloud-function/keyword_classifier.py
HIGH_SEVERITY_KEYWORDS: list[str] = [
    "khởi tố", "bắt giam", "bắt tạm giam", "truy tố", "bị bắt",
    "tử vong", "chết người", "thiệt mạng",
    "đình chỉ hoạt động", "thu hồi giấy phép",
    "Bộ TN&MT", "Bộ Công an", "Tổng cục",
    "ô nhiễm nghiêm trọng", "triệu tấn", "hàng chục hecta",
    "khai thác trái phép", "quặng lậu",
    "cháy lớn", "nổ lớn", "sập",
    "liên tiếp bị xử phạt",
]


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def search_terms() -> list[str]:
    """Deduplicated ESG search terms (case-insensitive dedup, first occurrence wins).

    Preserves insertion order from ESG_KEYWORDS. A term's positional index here
    is used as sub_query_ix in the search queue and must stay stable across
    process restarts — reordering or inserting into ESG_KEYWORDS shifts those
    indices and changes already-enqueued task_ids. Append new terms at the end.
    """
    seen, out = set(), []
    for t, _ in ESG_KEYWORDS:
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def esg_terms() -> list[tuple[str, str]]:
    """Return full ESG_KEYWORDS list as (term, tag) tuples."""
    return list(ESG_KEYWORDS)


def noise_terms() -> list[str]:
    """Return NOISE_KEYWORDS list."""
    return list(NOISE_KEYWORDS)


def high_severity_terms() -> list[str]:
    """Return HIGH_SEVERITY_KEYWORDS list."""
    return list(HIGH_SEVERITY_KEYWORDS)
