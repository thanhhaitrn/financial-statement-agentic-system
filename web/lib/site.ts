export const BRAND = "AgentFinX";

// Where "Mã nguồn" points.
export const SOURCE_URL = "https://github.com/thanhhaitrn/financial-statement-agentic-system";
// Where "Tài liệu" points. The repository's docs/ folder reaches main once this branch is merged.
export const DOCS_URL = `${SOURCE_URL}/tree/main/docs`;

export function docsPage(name: string) {
  return `${SOURCE_URL}/blob/main/docs/${name}.md`;
}
