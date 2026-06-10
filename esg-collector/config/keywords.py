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
    # --- Appended 2026-06-08: residential / real-estate fire coverage ---
    # APPEND-ONLY past this point — see search_terms() docstring: a term's index
    # is its sub_query_ix; inserting mid-list shifts already-enqueued task_ids.
    # Added because the industrial fire terms above ("cháy nhà máy/xưởng/lớn")
    # missed the Safira Khang Điền (KDH) apartment fire on 2026-05-19.
    ("hỏa hoạn", "S"),
    ("cháy chung cư", "S"),
    ("cháy căn hộ", "S"),
    ("cháy tòa nhà", "S"),
    # --- Appended 2026-06-08: single-path coverage gaps (still append-only) ---
    # Raw-stream audit showed these event types reach the DB almost ONLY via the
    # per-company alias search (low overlap with existing keyword terms), so they
    # currently have a single point of capture. Adding them as keyword terms
    # gives an independent second path. % = share already caught by an existing
    # ESG keyword (lower = bigger gap):  hủy niêm yết 18%, sa thải 19%,
    # đình chỉ giao dịch 25%, cưỡng chế 48%, thao túng 68%.
    ("hủy niêm yết", "G"),
    ("đình chỉ giao dịch", "G"),
    ("cưỡng chế", "G"),
    ("thao túng", "G"),
    ("sa thải", "S"),
    ("cắt giảm nhân sự", "S"),
    # Real-estate disputes / handover failures — no existing term covers them;
    # relevant for property names (KDH, NLG, DXG, VHM…). Reasoned, not measured.
    ("tranh chấp", "S"),
    ("chậm bàn giao", "S"),
]

# ---------------------------------------------------------------------------
# V2 classifier tiers (2026-06-10) — CLASSIFIER-ONLY vocabulary.
# Not part of ESG_KEYWORDS on purpose: search_terms() reads ESG_KEYWORDS, so
# nothing below creates new search-queue tasks or shifts sub_query_ix.
# Source: full-DB false-negative audit — the production filter suppressed
# ~350-540 real violation articles (noise-override + vocab gap). Every list
# was simulated on the full DB and checked against hand-labeled samples
# (recall 30/35 real events, junk resurrection ~14%) before deployment.
# ---------------------------------------------------------------------------

# Strong negative-event terms: override the noise blacklist the same way
# HIGH_SEVERITY_KEYWORDS do. Scanned in title+sapo ONLY — body text is too
# often polluted by sidebar/related-news fragments to be trusted here.
STRONG_EVENT_KEYWORDS: list[tuple[str, str]] = [
    ("xử phạt", "G"),
    ("bị phạt", "G"),
    ("tuyên phạt", "G"),
    ("phạt tù", "G"),
    ("án tù", "G"),
    ("phạt hành chính", "G"),
    ("thao túng", "G"),
    ("thất thoát", "G"),
    ("chiếm đoạt", "G"),
    ("bị điều tra", "G"),
    ("hủy niêm yết bắt buộc", "G"),
    ("gian lận", "G"),
    ("trục lợi", "G"),
    ("buôn lậu", "G"),
    ("hàng giả", "G"),
    ("vi phạm PCCC", "S"),
    ("chống bán phá giá", "G"),
    ("tấn công mạng", "S"),
    ("rò rỉ dữ liệu", "S"),
    ("lộ thông tin", "S"),
    ("thua kiện", "G"),
    ("bị kiện", "G"),
    ("khởi kiện", "G"),
    ("bị thu hồi", "G"),
    ("thu hồi đất", "G"),
    ("thu hồi dự án", "G"),
    ("bị kiểm soát", "G"),
    ("cháy cửa hàng", "S"),
    ("cháy kho", "S"),
    ("bị cháy", "S"),
]

# Violation-adjacent words too ambiguous for the full text: count as ESG
# evidence only when they appear in the TITLE, and never override noise.
WEAK_TITLE_KEYWORDS: list[tuple[str, str]] = [
    ("vi phạm", "G"),
    ("sự cố", "S"),
    ("điều tra", "G"),
    ("tố cáo", "G"),
    ("vụ cháy", "S"),
    ("nợ thuế", "G"),
    ("nợ lương", "S"),
    ("nợ bảo hiểm", "S"),
    ("âm vốn", "G"),
]

# Negated-noise pairs / product-quality phrases (title+sapo). These exist
# mostly so the overlap guard neutralizes their embedded noise substring:
# "KHÔNG đạt chuẩn" must not die to noise term "đạt chuẩn".
QUALITY_KEYWORDS: list[tuple[str, str]] = [
    ("không đạt chuẩn", "G"),
    ("chưa đạt chuẩn", "G"),
    ("không đảm bảo an toàn", "S"),
    ("kém chất lượng", "S"),
]

# A strong term preceded (≤25 chars) by one of these is GOOD news for the
# company and must not fire: "KHÔNG BỊ … áp thuế chống bán phá giá".
POSITIVE_GUARDS: list[str] = [
    "không bị", "ngoại trừ", "dỡ bỏ", "miễn ", "thoát ", "không áp",
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


def strong_event_terms() -> list[tuple[str, str]]:
    """Strong negative-event terms (override noise; title+sapo scope)."""
    return list(STRONG_EVENT_KEYWORDS)


def weak_title_terms() -> list[tuple[str, str]]:
    """Title-only violation terms (never override noise)."""
    return list(WEAK_TITLE_KEYWORDS)


def quality_terms() -> list[tuple[str, str]]:
    """Negated-noise / quality terms (title+sapo; never override noise)."""
    return list(QUALITY_KEYWORDS)


def positive_guards() -> list[str]:
    """Prefixes that disarm a strong term (good-news direction)."""
    return list(POSITIVE_GUARDS)


def noise_terms() -> list[str]:
    """Return NOISE_KEYWORDS list."""
    return list(NOISE_KEYWORDS)


def high_severity_terms() -> list[str]:
    """Return HIGH_SEVERITY_KEYWORDS list."""
    return list(HIGH_SEVERITY_KEYWORDS)
