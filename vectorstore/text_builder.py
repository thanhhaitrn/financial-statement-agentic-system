"""Convert financial fact rows into vector documents and metadata."""
# Code note: Vectorstore modules turn normalized facts into searchable text and metadata for retrieval.

from kb.sqlite_repo import TYPED_FACT_METADATA_FIELDS
from schemas.table_names import TABLE_NOTE, TABLE_REPORT_SECTION


VECTOR_METADATA_FIELDS = (
    "company",
    "fiscal_year",
    "heading",
    "item_code",
    "note_ref",
    "subheading",
    "item_name",
    "source",
    "raw_value",
    "normalized_value",
    "period",
    "value_type",
    "unit",
    *TYPED_FACT_METADATA_FIELDS,
    "fact_id",
)


def _row_value(row) -> str:
    return str(row.get("normalized_value") or row.get("value") or "").strip()


def _note_heading_from_item_name(item_name: str) -> str:
    text = str(item_name or "").strip()
    if not text:
        return "Thuyết minh báo cáo tài chính"
    return text.split("|", 1)[0].strip()


def _note_content_from_row(row) -> str:
    value = _row_value(row)
    subheading = str(row.get("subheading", "") or "").strip()
    item_name = str(row.get("item_name", "") or "").strip()
    if subheading and subheading in value:
        return value
    if subheading and "|" not in item_name:
        return f"{subheading}. {value}" if value else subheading
    if "|" not in item_name:
        return value

    row_label = item_name.split("|", 1)[1].strip()
    if row_label and row_label not in value:
        return f"{row_label}. {value}" if value else row_label
    return value


def _typed_context_parts(row) -> list[str]:
    parts: list[str] = []
    row_label = str(row.get("row_label", "") or "").strip()
    column_label = str(row.get("column_label", "") or "").strip()
    if row_label and row_label not in str(row.get("item_name", "") or ""):
        parts.append(f"Dòng: {row_label}")
    if column_label and column_label not in str(row.get("item_name", "") or ""):
        parts.append(f"Cột: {column_label}")
    if row.get("period_role"):
        parts.append(f"Vai trò kỳ: {row['period_role']}")
    if row.get("aggregation_level") == "total":
        parts.append("Mức tổng hợp: tổng")
    if row.get("metric_label"):
        parts.append(f"Chỉ tiêu ngữ nghĩa: {row['metric_label']}")
    if row.get("entity_label"):
        parts.append(f"Đối tượng ngữ nghĩa: {row['entity_label']}")
    if row.get("scope_label"):
        parts.append(f"Phạm vi bảng: {row['scope_label']}")
    if row.get("counterparty"):
        parts.append(f"Đối tác giao dịch: {row['counterparty']}")
    if row.get("transaction_type"):
        parts.append(f"Loại giao dịch: {row['transaction_type']}")
    if row.get("movement_type"):
        parts.append(f"Loại biến động: {row['movement_type']}")
    if row.get("geography"):
        parts.append(f"Khu vực địa lý: {row['geography']}")
    if row.get("policy_topic"):
        parts.append(f"Chủ đề chính sách: {row['policy_topic']}")
    if row.get("section_key"):
        parts.append(f"Khóa phần báo cáo: {row['section_key']}")
    return parts


def _build_note_text(row) -> str:
    value = _note_content_from_row(row)
    item_name = str(row.get("item_name", "") or "").strip()
    note_heading = _note_heading_from_item_name(item_name)

    parts = []
    if row.get("company"):
        parts.append(f"Công ty: {row['company']}")
    if row.get("fiscal_year"):
        parts.append(f"Năm: {row['fiscal_year']}")

    parts.append(f"Báo cáo: {TABLE_NOTE}")
    if note_heading:
        parts.append(note_heading)
    if row.get("subheading"):
        parts.append(f"Subheading: {row['subheading']}")
    if value:
        parts.append(f"Nội dung: {value}")
    parts.extend(_typed_context_parts(row))

    return "\n".join(parts)


def _build_report_section_text(row) -> str:
    value = _row_value(row)
    parts = []
    if row.get("company"):
        parts.append(f"Công ty: {row['company']}")
    if row.get("fiscal_year"):
        parts.append(f"Năm: {row['fiscal_year']}")

    parts.append(f"Phần báo cáo: {TABLE_REPORT_SECTION}")
    if row.get("item_name"):
        parts.append(str(row.get("item_name", "") or "").strip())
    if row.get("subheading"):
        parts.append(f"Subheading: {row['subheading']}")
    if value:
        parts.append(f"Nội dung: {value}")
    parts.extend(_typed_context_parts(row))

    return "\n".join(parts)


def build_combined_text(row) -> str:
    if str(row.get("heading", "") or "").strip() == TABLE_NOTE:
        return _build_note_text(row)
    if str(row.get("heading", "") or "").strip() == TABLE_REPORT_SECTION:
        return _build_report_section_text(row)

    parts = []
    value = _row_value(row)

    if row.get("company"):
        parts.append(f"Công ty {row['company']}.")

    if row.get("heading"):
        parts.append(f"Bảng {row['heading']}.")

    if row.get("note_ref"):
        parts.append(f"Thuyết minh {row['note_ref']}.")

    if row.get("subheading"):
        parts.append(f"{row['subheading']}.")

    if row.get("item_name"):
        parts.append(f"{row['item_name']}.")

    if value:
        parts.append(f"Giá trị {value}.")
    parts.extend(f"{part}." for part in _typed_context_parts(row))

    return " ".join(parts)


def build_documents_and_metadata(df):
    df = df.fillna("")
    documents = df.apply(build_combined_text, axis=1).astype(str).tolist()

    for column in VECTOR_METADATA_FIELDS:
        if column not in df.columns:
            df[column] = ""

    metadatas = df[list(VECTOR_METADATA_FIELDS)].to_dict(orient="records")

    ids = df.index.astype(str).tolist()

    assert len(documents) == len(metadatas) == len(ids)

    return documents, metadatas, ids


def validate_canonical_vector_metadata(metadatas: list[dict]) -> None:
    """Require exact SQLite-to-vector metadata parity for canonical builds."""

    expected = set(VECTOR_METADATA_FIELDS)
    for index, metadata in enumerate(metadatas):
        if not isinstance(metadata, dict):
            raise ValueError(f"vector metadata at index {index} must be a mapping")
        missing = sorted(expected.difference(metadata))
        if missing:
            raise ValueError(
                f"vector metadata at index {index} is missing: {', '.join(missing)}"
            )
        if not str(metadata.get("fact_id", "") or "").strip():
            raise ValueError(f"vector metadata at index {index} has no fact_id")
        typed_present = any(
            str(metadata.get(field, "") or "").strip()
            for field in TYPED_FACT_METADATA_FIELDS
            if field != "aggregation_level"
        )
        if not typed_present:
            continue
        for field in (
            "row_label",
            "column_label",
            "value_kind",
            "parsed_value",
            "aggregation_level",
            "section_path",
            "block_id",
        ):
            if not str(metadata.get(field, "") or "").strip():
                raise ValueError(
                    f"vector metadata at index {index} has empty {field}"
                )
