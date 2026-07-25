"""Schedule-aware evidence fact caps for list/superlative questions."""

from graph.evidence import (
    EVIDENCE_FACTS_LIMIT,
    NOTE_EVIDENCE_FACTS_LIMIT,
    NOTE_FACTS_LIMIT,
    NOTE_LLM_FACTS_LIMIT,
    REPORT_SECTION_FACTS_LIMIT,
    SCHEDULE_FACTS_LIMIT,
    SCHEDULE_MAIN_FACTS_LIMIT,
    _facts_limit_for_table,
    _llm_facts_limit_for_table,
)
from schemas.table_names import TABLE_BS, TABLE_NOTE, TABLE_REPORT_SECTION
from tools.tools import needs_full_schedule


def test_needs_full_schedule_markers():
    assert needs_full_schedule("Khoản cho vay ngắn hạn nào đã được thu hồi hoàn toàn trong kỳ?")
    assert needs_full_schedule("Liệt kê các công ty bị lỗ trong năm.")
    assert needs_full_schedule("Dự án nào có số dư giảm nhiều nhất?")
    assert needs_full_schedule("So sánh dự phòng giảm giá đầu tư cuối kỳ và đầu năm.")
    assert not needs_full_schedule("Tổng giá trị hàng tồn kho cuối kỳ là bao nhiêu VND?")


def test_schedule_question_widens_note_caps():
    state = {"user_query": "Khoản cho vay ngắn hạn nào đã được thu hồi hoàn toàn trong kỳ?"}
    assert _facts_limit_for_table(state, {}, TABLE_NOTE) == SCHEDULE_FACTS_LIMIT
    assert _llm_facts_limit_for_table(state, {}, TABLE_NOTE) == SCHEDULE_FACTS_LIMIT
    assert _facts_limit_for_table(state, {}, TABLE_BS) == SCHEDULE_MAIN_FACTS_LIMIT
    assert _llm_facts_limit_for_table(state, {}, TABLE_BS) == SCHEDULE_MAIN_FACTS_LIMIT
    # Front-matter prose has a separate cap and never inherits schedule widening.
    assert _facts_limit_for_table(state, {}, TABLE_REPORT_SECTION) == REPORT_SECTION_FACTS_LIMIT
    assert _llm_facts_limit_for_table(state, {}, TABLE_REPORT_SECTION) == REPORT_SECTION_FACTS_LIMIT


def test_plain_lookup_keeps_default_note_cap():
    state = {"user_query": "Tổng giá trị hàng tồn kho cuối kỳ là bao nhiêu VND?"}
    assert NOTE_FACTS_LIMIT == NOTE_EVIDENCE_FACTS_LIMIT == NOTE_LLM_FACTS_LIMIT == 12
    assert _facts_limit_for_table(state, {}, TABLE_NOTE) == NOTE_EVIDENCE_FACTS_LIMIT
    assert _llm_facts_limit_for_table(state, {}, TABLE_NOTE) == NOTE_EVIDENCE_FACTS_LIMIT
    assert _facts_limit_for_table(state, {}, TABLE_BS) == EVIDENCE_FACTS_LIMIT


def test_easy_note_route_does_not_inherit_report_section_cap():
    state = {
        "user_query": "Thuyết minh hàng tồn kho là gì?",
        "planner_plan": {"difficulty_level": "easy"},
    }
    worker_plan = {
        "evidence_plan": [
            {"table": TABLE_NOTE, "query": "thuyết minh hàng tồn kho"}
        ]
    }

    assert _facts_limit_for_table(state, worker_plan, TABLE_NOTE) == NOTE_EVIDENCE_FACTS_LIMIT
    assert _llm_facts_limit_for_table(state, worker_plan, TABLE_NOTE) == NOTE_EVIDENCE_FACTS_LIMIT
