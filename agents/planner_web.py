"""Planner instruction for the web chat with report discovery enabled.

The QA planner profile in ``agents.profiles`` is untouched: with
``FINX_REPORT_DISCOVERY_ENABLED`` off, the planner prompt and schema are exactly
the ones the CLI and batch evaluation use. This module only appends the task
classification the web entry needs.
"""
# Code note: Agent modules coordinate LLM prompts, tool calls, and structured outputs; comments here call out control-flow constraints.

from agents.profiles import AGENT_PROFILES

DISCOVERY_ADDENDUM = """

            TÁC VỤ (chỉ áp dụng cho giao diện web có tìm báo cáo)
            - Thêm field task_kind: "report_qa" (mặc định) hoặc "report_discovery".
            - task_kind="report_discovery" CHỈ khi người dùng yêu cầu TÌM, TẢI hoặc LẤY một báo cáo tài chính (vd "tìm BCTC VNM năm 2025", "tải báo cáo quý 2 của FPT"), không phải khi hỏi số liệu hay phân tích trong báo cáo.
            - Câu hỏi về số liệu, chỉ tiêu, tính toán hay phân tích — kể cả khi có nhắc tên báo cáo, vd "báo cáo VNM 2025 lợi nhuận bao nhiêu?" — là report_qa.
            - Với report_discovery, điền report_search:
              + subject: tên công ty hoặc mã CK ĐÚNG NHƯ người dùng viết.
              + ticker_guess: mã CK bạn đoán khi người dùng không tự viết mã; để "" nếu không chắc.
              + fiscal_year chỉ khi người dùng nêu một năm cụ thể; không đổi "năm trước"/"năm nay" thành năm cụ thể và không lấy năm từ báo cáo đang mở — để null.
              + period: "annual" (báo cáo năm), "quarter" (báo cáo quý), "half_year" (bán niên) hoặc "any"; fiscal_quarter/fiscal_half chỉ khi được nêu.
              + scope ("consolidated"|"separate"|"unknown") và audit_status ("audited"|"reviewed"|"unaudited"|"unknown") chỉ khi được nêu.
            - Không suy công ty từ báo cáo đang mở khi người dùng hỏi tìm một doanh nghiệp khác.
            - Với report_discovery, các field phân tích giữ mặc định (analysis_axes=[], premise_requirements=[], need_web=false).
            """


def web_planner_instruction() -> str:
    return AGENT_PROFILES["agent_planner"]["system_instruction"] + DISCOVERY_ADDENDUM
