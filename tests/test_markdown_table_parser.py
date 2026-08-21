import sys
import unittest
from pathlib import Path

import pandas as pd


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from ingestion.table_parser import markdown_table_to_df


class MarkdownTableParserTests(unittest.TestCase):
    def test_escaped_pipe_remains_inside_cell(self):
        df = markdown_table_to_df([
            "| Khoản mục | Mã | Giá trị |",
            "| :--- | :---: | ---: |",
            r"| Doanh thu \| thu nhập | A\|B | 100 |",
        ])

        self.assertEqual(list(df.columns), ["Khoản mục", "Mã", "Giá trị"])
        self.assertEqual(df.iloc[0].tolist(), ["Doanh thu | thu nhập", "A|B", "100"])
        self.assertEqual(df.attrs["markdown_parser_warnings"], [])

    def test_wrapped_physical_lines_form_one_logical_row(self):
        df = markdown_table_to_df([
            "| Khoản mục | Diễn giải | Giá trị |",
            "| --- | --- | ---: |",
            "| Tiền gửi | Dòng diễn giải thứ nhất",
            "dòng diễn giải thứ hai | 1.000 |",
        ])

        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0, 0], "Tiền gửi")
        self.assertEqual(df.iloc[0, 1], "Dòng diễn giải thứ nhất\ndòng diễn giải thứ hai")
        self.assertEqual(df.iloc[0, 2], "1.000")

    def test_alignment_row_is_optional_and_never_becomes_data(self):
        aligned = markdown_table_to_df([
            "| A | B |",
            "| :--- | ---: |",
            "| x | 1 |",
        ])
        without_alignment = markdown_table_to_df([
            "| A | B |",
            "| x | 1 |",
        ])

        self.assertEqual(aligned.to_dict("records"), [{"A": "x", "B": "1"}])
        self.assertEqual(without_alignment.to_dict("records"), [{"A": "x", "B": "1"}])

    def test_ragged_rows_do_not_shift_or_drop_cells(self):
        df = markdown_table_to_df([
            "| A | B | C |",
            "| --- | --- | --- |",
            "| short | 10 |",
            "| wide | 20 | 30 | source fragment |",
        ])

        self.assertEqual(df.iloc[0, 0], "short")
        self.assertEqual(df.iloc[0, 1], "10")
        self.assertTrue(pd.isna(df.iloc[0, 2]))
        self.assertEqual(df.iloc[1].tolist(), ["wide", "20", "30 | source fragment"])
        self.assertEqual(
            [warning["kind"] for warning in df.attrs["markdown_parser_warnings"]],
            ["short_row_padded", "wide_row_merged"],
        )

    def test_new_leading_boundary_does_not_get_consumed_by_short_row(self):
        df = markdown_table_to_df([
            "| A | B | C |",
            "| --- | --- | --- |",
            "| first | 10",
            "| second | 20 | 30 |",
        ])

        self.assertEqual(len(df), 2)
        self.assertEqual(df.iloc[0, :2].tolist(), ["first", "10"])
        self.assertTrue(pd.isna(df.iloc[0, 2]))
        self.assertEqual(df.iloc[1].tolist(), ["second", "20", "30"])


if __name__ == "__main__":
    unittest.main()
