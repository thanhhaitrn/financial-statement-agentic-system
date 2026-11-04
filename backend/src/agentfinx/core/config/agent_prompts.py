"""Account preferences appended to private prompts for one chat-run snapshot."""

from contextlib import contextmanager
from contextvars import ContextVar

MAX_PROMPT_LENGTH = 60000
AGENT_LABELS = {
    "agent_planner": ("Lập kế hoạch", "Hiểu câu hỏi và xác định hướng xử lý."),
    "agent_router": ("Điều phối bằng chứng", "Chọn dữ liệu và khía cạnh cần phân tích."),
    "agent_profitability": ("Khả năng sinh lời", "Phân tích lợi nhuận và các tỷ suất sinh lời."),
    "agent_liquidity_solvency": ("Thanh khoản và an toàn tài chính", "Đánh giá khả năng thanh toán và nghĩa vụ nợ."),
    "agent_cashflow_analysis": ("Dòng tiền", "Phân tích dòng tiền và chất lượng tạo tiền."),
    "agent_efficiency": ("Hiệu quả hoạt động", "Đánh giá hiệu quả sử dụng tài sản và vốn."),
    "agent_synth": ("Tổng hợp câu trả lời", "Kết hợp kết quả phân tích thành câu trả lời cuối cùng."),
    "news_select": ("Tin tức · Chọn và gom tin", "Chọn bài liên quan và nhóm theo sự kiện."),
    "news_score": ("Tin tức · Phân tích bằng chứng", "Trích dẫn và đánh giá các sự kiện doanh nghiệp."),
}
_ACTIVE: ContextVar[dict[str, str]] = ContextVar("agent_preferences", default={})


@contextmanager
def activate(overrides: dict[str, str]):
    token = _ACTIVE.set(dict(overrides))
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def resolve(agent_id: str, default: str) -> str:
    preference = _ACTIVE.get().get(agent_id, "").strip()
    if not preference:
        return default
    return (
        default
        + "\n\n=== CỰC KỲ QUAN TRỌNG: YÊU CẦU CÁ NHÂN HÓA CỦA NGƯỜI DÙNG ===\n"
        + "Ưu tiên áp dụng yêu cầu dưới đây vào cách phân tích, trọng tâm và cách trình bày "
        + "của agent này. Vẫn tuân thủ nhiệm vụ, giới hạn bằng chứng, quyền sử dụng công cụ "
        + "và định dạng đầu ra đã quy định ở trên. Không bịa số liệu để đáp ứng yêu cầu.\n"
        + preference
        + "\n=== KẾT THÚC YÊU CẦU CÁ NHÂN HÓA ==="
    )


def catalog() -> list[dict]:
    return [
        {"id": key, "label": label, "description": description}
        for key, (label, description) in AGENT_LABELS.items()
    ]
