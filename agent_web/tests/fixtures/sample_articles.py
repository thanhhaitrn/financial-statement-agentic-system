"""Fixture tin tài chính tiếng Việt mẫu (mục 10: deliverables).

Bao gồm đủ các case yêu cầu ở mục 9:
- Tin tích cực & tiêu cực cho cùng doanh nghiệp.
- Nhiều bài cùng một sự kiện (bài đăng lại / nhiều nguồn đưa cùng tin).
- Bài có nhiều doanh nghiệp, sentiment trái chiều.
- Tin đồn / dự báo.
- Bài thiếu ngày xuất bản.
- Bài chỉ có snippet.
- Bài xuất bản sau as_of (phải bị loại).
"""

from datetime import datetime, timezone

from agent_web.providers.mock_provider import MockArticleFixture

FIXTURES = [
    MockArticleFixture(
        title="FPT công bố lợi nhuận quý 2 tăng trưởng mạnh nhờ mảng CNTT nước ngoài",
        url="https://example.com/fpt-loi-nhuan-q2",
        source="VietstockDemo",
        content=(
            "Công ty Cổ phần FPT vừa công bố kết quả kinh doanh quý 2 với "
            "doanh thu và lợi nhuận sau thuế đều tăng trưởng hai chữ số, chủ "
            "yếu nhờ mảng dịch vụ CNTT cho thị trường nước ngoài tiếp tục mở "
            "rộng đơn hàng lớn."
        ),
        published_at=datetime(2026, 8, 5, tzinfo=timezone.utc),
        content_scope="full_text",
    ),
    MockArticleFixture(
        # Bài đăng lại cùng sự kiện trên, nguồn khác -> test loại trùng theo sự kiện
        title="Lợi nhuận quý 2 của FPT tăng mạnh nhờ xuất khẩu phần mềm",
        url="https://example.com/fpt-loi-nhuan-q2-nguon-2",
        source="CafeFDemo",
        content=(
            "FPT ghi nhận lợi nhuận quý 2 tăng trưởng ấn tượng, động lực "
            "chính đến từ doanh thu chuyển đổi số và xuất khẩu phần mềm sang "
            "thị trường Nhật Bản, Mỹ."
        ),
        published_at=datetime(2026, 8, 6, tzinfo=timezone.utc),
        content_scope="full_text",
    ),
    MockArticleFixture(
        title="FPT bị phạt do vi phạm quy định công bố thông tin",
        url="https://example.com/fpt-bi-phat",
        source="VietstockDemo",
        content=(
            "Uỷ ban Chứng khoán Nhà nước ra quyết định xử phạt hành chính đối "
            "với FPT do chậm công bố thông tin về một giao dịch của người "
            "nội bộ. Mức phạt không đáng kể so với quy mô doanh nghiệp."
        ),
        published_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
        content_scope="full_text",
    ),
    MockArticleFixture(
        # Nhiều doanh nghiệp, sentiment trái chiều: tốt cho FPT, xấu cho đối thủ
        title="FPT giành hợp đồng lớn từ tay đối thủ CMC trong mảng chuyển đổi số",
        url="https://example.com/fpt-thang-thau-cmc",
        source="CafeFDemo",
        content=(
            "FPT vừa trúng gói thầu chuyển đổi số trị giá hàng trăm tỷ đồng, "
            "vượt qua đối thủ cạnh tranh trực tiếp là CMC. Đây là tin tích "
            "cực cho FPT trong khi CMC mất đi một hợp đồng lớn."
        ),
        published_at=datetime(2026, 8, 15, tzinfo=timezone.utc),
        content_scope="full_text",
    ),
    MockArticleFixture(
        # Tin đồn / dự báo
        title="Đồn đoán FPT có thể mua lại một startup AI trong nước",
        url="https://example.com/fpt-tin-don-mua-startup",
        source="ForumDemo",
        content=(
            "Trên một số diễn đàn đầu tư xuất hiện tin đồn chưa được xác "
            "nhận rằng FPT đang đàm phán mua lại một startup AI trong nước. "
            "FPT chưa có phát ngôn chính thức về thông tin này."
        ),
        published_at=datetime(2026, 8, 18, tzinfo=timezone.utc),
        content_scope="full_text",
    ),
    MockArticleFixture(
        # Thiếu ngày xuất bản
        title="Phân tích triển vọng ngành CNTT Việt Nam nửa cuối năm",
        url="https://example.com/trien-vong-nganh-cntt",
        source="AnalysisDemo",
        content=(
            "Báo cáo phân tích ngành CNTT Việt Nam nhận định nhóm doanh "
            "nghiệp đầu ngành như FPT có thể hưởng lợi từ xu hướng chuyển "
            "đổi số toàn cầu, tuy nhiên cạnh tranh nhân sự vẫn là rủi ro."
        ),
        published_at=None,
        content_scope="full_text",
    ),
    MockArticleFixture(
        # Chỉ có snippet
        title="FPT ký kết hợp tác chiến lược với đối tác Nhật Bản",
        url="https://example.com/fpt-hop-tac-nhat-ban",
        source="SnippetOnlyDemo",
        content="FPT vừa ký kết hợp tác chiến lược với một tập đoàn công nghệ Nhật Bản...",
        published_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
        content_scope="snippet",
    ),
    MockArticleFixture(
        # Xuất bản SAU as_of -> phải bị loại khỏi phân tích
        title="FPT công bố kế hoạch quý 3 (tin sau thời điểm as_of)",
        url="https://example.com/fpt-ke-hoach-q3",
        source="VietstockDemo",
        content="Nội dung này được công bố sau as_of nên không được sử dụng.",
        published_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
        content_scope="full_text",
    ),
]
