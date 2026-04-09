def build_combined_text(row) -> str:
    parts = []
    value = row.get("normalized_value") or row.get("value") or ""

    if row.get("company"):
        parts.append(f"Công ty {row['company']}.")

    if row.get("heading"):
        parts.append(f"Bảng {row['heading']}.")

    if row.get("item_name"):
        parts.append(f"{row['item_name']}.")

    if value:
        parts.append(f"Giá trị {value}.")

    return " ".join(parts)


def build_documents_and_metadata(df):
    df = df.fillna("")
    documents = df.apply(build_combined_text, axis=1).astype(str).tolist()

    metadatas = df[
        ["company", "heading", "item_name", "source", "raw_value", "normalized_value"]
    ].to_dict(orient="records")

    ids = df.index.astype(str).tolist()

    assert len(documents) == len(metadatas) == len(ids)

    return documents, metadatas, ids
