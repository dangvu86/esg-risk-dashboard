"""ESG keyword sub-queries.

Each sub-query has <= 4 OR clauses (Google News RSS silently returns 0 items
when intitle:"..." is combined with more OR clauses, and even keyword-only
queries get unreliable past ~6 OR). Keep it at 4 to be safe across all backends.
"""

KEYWORD_GROUPS = {
    "E": [
        "ô nhiễm OR xả thải OR môi trường OR khí thải",
        "nước thải OR mùi hôi OR rác thải OR chất thải",
        "sự cố môi trường OR tràn dầu OR rò rỉ OR giấy phép môi trường",
        "phá rừng OR khai thác trái phép OR khai thác lậu OR hủy hoại môi trường",
        "hóa chất độc OR cá chết OR thủy sản chết OR bụi mịn",
        "biến đổi khí hậu OR khí nhà kính OR phát thải carbon OR net zero",
    ],
    "S": [
        "tai nạn OR tử vong OR đình công OR an toàn lao động",
        "cháy nổ OR sập OR ngộ độc OR thương vong",
        "giải phóng mặt bằng OR bồi thường OR dân phản đối OR dân kêu cứu",
        "nợ lương OR nợ BHXH OR nợ bảo hiểm OR chậm lương",
        "lao động trẻ em OR bệnh nghề nghiệp OR quấy rối OR lừa đảo khách hàng",
        "thu hồi đất OR cưỡng chế OR tranh chấp đất OR dân tộc thiểu số",
    ],
    "G": [
        "vi phạm OR xử phạt OR khởi tố OR thanh tra",
        "sai phạm OR bị phạt OR truy thu OR đấu thầu",
        "bêu tên OR tầm ngắm OR danh sách đen OR UBCKNN",
        "khiếu kiện OR khiếu nại OR giám sát OR chậm tiến độ",
        "gian lận OR hàng giả OR kém chất lượng OR thu hồi sản phẩm",
        "tham nhũng OR hối lộ OR đưa hối lộ OR nhận hối lộ",
        "trốn thuế OR gian lận thuế OR biển thủ OR tham ô",
        "thao túng giá OR thao túng cổ phiếu OR nội gián OR giao dịch nội bộ",
        "chậm công bố OR sai lệch công bố OR đình chỉ giao dịch OR cảnh cáo UBCKNN",
        "vỡ nợ OR mất thanh khoản OR phá sản OR nợ xấu",
        "rửa tiền OR lừa đảo nhà đầu tư OR chiếm đoạt OR cổ đông kiện",
        "truy nã OR bỏ trốn OR tạm giam OR khám xét",
    ],
}

def all_subqueries():
    """Yield (group_key, sub_query_index, query_text) for every sub-query."""
    for grp, subs in KEYWORD_GROUPS.items():
        for i, q in enumerate(subs):
            yield grp, i, q

def count_subqueries():
    return sum(len(v) for v in KEYWORD_GROUPS.values())
