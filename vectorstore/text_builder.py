"""Convert financial fact rows into vector documents and metadata."""
# Code note: Vectorstore modules turn normalized facts into searchable text and metadata for retrieval.

from schemas.table_names import TABLE_NOTE, TABLE_REPORT_SECTION


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

    return " ".join(parts)


def build_documents_and_metadata(df):
    df = df.fillna("")
    documents = df.apply(build_combined_text, axis=1).astype(str).tolist()

    metadata_columns = [
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
        # Slot disambiguators for value-lookup rerank/answer.
        "period",
        "value_type",
        "unit",
    ]
    for column in metadata_columns:
        if column not in df.columns:
            df[column] = ""

    metadatas = df[metadata_columns].to_dict(orient="records")

    ids = df.index.astype(str).tolist()

    assert len(documents) == len(metadatas) == len(ids)

    return documents, metadatas, ids
