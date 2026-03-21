import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from agents.planner_hints import infer_table_keywords, infer_table_query_hints


TABLE_BS = "BẢNG CÂN ĐỐI KẾ TOÁN"


def test_infer_table_keywords_keeps_exact_total_assets_without_noisy_neighbors():
    analysis_axes = [
        {
            "axis": "total_assets",
            "tables": [TABLE_BS],
            "objective": "Xác định giá trị tổng tài sản tại thời điểm được yêu cầu",
        }
    ]
    user_query = "Tổng tài sản của Hòa Phát tại ngày 30/06/2025 là bao nhiêu?"

    keywords = infer_table_keywords(TABLE_BS, user_query, analysis_axes)

    assert keywords == ["tổng cộng tài sản"]


def test_infer_table_query_hints_returns_planner_texts_not_table_keywords():
    analysis_axes = [
        {
            "axis": "total_assets",
            "tables": [TABLE_BS],
            "objective": "Xác định giá trị tổng tài sản tại thời điểm được yêu cầu",
        }
    ]
    user_query = "Tổng tài sản của Hòa Phát tại ngày 30/06/2025 là bao nhiêu?"

    hints = infer_table_query_hints(TABLE_BS, user_query, analysis_axes)

    assert hints == [
        "Xác định giá trị tổng tài sản tại thời điểm được yêu cầu",
        "total_assets: Xác định giá trị tổng tài sản tại thời điểm được yêu cầu",
        user_query,
    ]
