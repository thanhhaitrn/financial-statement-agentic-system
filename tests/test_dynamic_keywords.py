"""Dataset-derived routing keywords: derivation from the KB and payload merge."""

import json
import sqlite3

import pytest

from config.allowed_keywords import (
    TABLE_BS,
    TABLE_NOTE,
    build_allowed_keywords_payload,
    iter_keyword_table_pairs,
    set_dynamic_keywords,
)
from kb.sqlite_repo import derive_keyword_augmentation, init_db, insert_financial_facts


@pytest.fixture(autouse=True)
def _reset_dynamic_keywords():
    yield
    set_dynamic_keywords({})


@pytest.fixture()
def conn():
    connection = init_db(":memory:")
    rows = [
        # Primary line carrying a note_ref -> keyword for BS and NOTE.
        ("APEC", "2024", TABLE_BS, "135", "V.5", "", "Phải thu về cho vay ngắn hạn | Số cuối năm",
         "85.566.500.000", "85.566.500.000", "85566500000", "r.md", "cuối", "", "VND"),
        # Numbered note schedule -> NOTE keyword from the subheading title.
        ("APEC", "2024", TABLE_NOTE, "note_row", "V.8", "8. Chi phí trả trước dài hạn",
         "Chi phí công cụ, dụng cụ | Số cuối kỳ",
         "1.141.547.635", "1.141.547.635", "1141547635", "r.md", "cuối", "", "VND"),
        # Matrix-section suffix must be stripped from the title.
        ("APEC", "2024", TABLE_NOTE, "note_row", "V.9", "9. Tài sản cố định hữu hình — Nguyên giá",
         "Máy móc thiết bị | Số cuối kỳ", "1.000", "1.000", "1000", "r.md", "cuối", "nguyên giá", "VND"),
        # Descriptive subheading (no leading number) contributes nothing.
        ("APEC", "2024", TABLE_NOTE, "note_row", "V.5", "Là các khoản cho vay Bên liên quan, bao gồm:",
         "Công ty X | Số cuối kỳ", "2.000", "2.000", "2000", "r.md", "cuối", "", "VND"),
    ]
    insert_financial_facts(connection, rows)
    return connection


def test_derive_keyword_augmentation(conn):
    augmented = derive_keyword_augmentation(conn)

    assert "phải thu về cho vay ngắn hạn" in augmented[TABLE_BS]
    assert "phải thu về cho vay ngắn hạn" in augmented[TABLE_NOTE]
    assert "chi phí trả trước dài hạn" in augmented[TABLE_NOTE]
    assert "tài sản cố định hữu hình" in augmented[TABLE_NOTE]
    # Descriptive subheadings and section suffixes never become keywords.
    for keyword in augmented[TABLE_NOTE]:
        assert not keyword.startswith("là các khoản")
        assert "—" not in keyword


def test_dynamic_keywords_merge_into_payload_and_pairs(conn):
    set_dynamic_keywords(derive_keyword_augmentation(conn))

    payload = json.loads(build_allowed_keywords_payload())
    assert "phải thu về cho vay ngắn hạn" in payload[TABLE_BS]
    assert "chi phí trả trước dài hạn" in payload[TABLE_NOTE]
    # Static vocabulary remains.
    assert "tổng cộng tài sản" in payload[TABLE_BS]

    pairs = iter_keyword_table_pairs()
    assert ("chi phí trả trước dài hạn", TABLE_NOTE) in pairs


def test_unknown_table_and_empty_keywords_ignored():
    set_dynamic_keywords({"BẢNG LẠ": {"x"}, TABLE_BS: {"", "  "}})
    payload = json.loads(build_allowed_keywords_payload())
    assert "BẢNG LẠ" not in payload
    assert "" not in payload[TABLE_BS]
