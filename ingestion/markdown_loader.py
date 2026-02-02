def load_markdown(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()
    
def detect_tables(md_text: str) -> list[list[str]]:
    tables = []
    current = []

    for line in md_text.splitlines():
        if line.strip().startswith("|"):
            current.append(line)
        else:
            if current:
                tables.append(current)
                current = []

    if current:
        tables.append(current)

    return tables