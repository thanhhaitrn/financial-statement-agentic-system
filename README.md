# AgentFinX Financial QA

AgentFinX là pipeline evidence-first cho hỏi đáp báo cáo tài chính tiếng Việt. Project đọc báo cáo Markdown, chuẩn hóa bảng và thuyết minh vào SQLite, xây chỉ mục **Qdrant**, sau đó dùng graph agent (LangGraph) để lập kế hoạch, truy xuất bằng chứng và tổng hợp một câu trả lời cuối. Vector store duy nhất là Qdrant; không dùng vector DB nào khác.

## Entry Point

Sau khi cài (`pip install -e .`), lệnh hợp nhất `agentfinx <command>` route sang module tương ứng. Mỗi module cũng chạy trực tiếp bằng `python <module>.py` và có console script riêng.

| Lệnh hợp nhất | Console script | Module | Vai trò |
|---|---|---|---|
| `agentfinx ask …` | — | `test.py` | Đăng ký/build dataset và hỏi một câu |
| `agentfinx batch …` | `agentfinx-batch` | `dataset_batch_runner.py` | Chạy nhiều câu hỏi trên một/nhiều dataset |
| `agentfinx predict …` | `agentfinx-predict` | `dataset_batch_result.py` | Tạo prediction/context cho evaluation |
| `agentfinx score …` | `agentfinx-ragas` | `ragas_eval_runner.py` | Chấm RAGAS (diagnostic) |
| `agentfinx recall …` | `agentfinx-recall` | `eval_retrieval_recall.py` | Hard gate deterministic factual recall |
| `agentfinx analyze …` | — | `analyze_batch_metrics.py` | Báo cáo metric theo bucket + latency baseline |

Output trình bày chỉ in kết quả synth đúng một lần với header `=== FINAL ANSWER ===` (thân câu trả lời có tiền tố `ANSWER:`). Kết quả trung gian của worker không được lặp lại trong final answer.

## Cấu Trúc

```text
src/agentfinx/          Package cài đặt: CLI dispatcher hợp nhất (agentfinx)
agents/                 Planner, router/keyworder, analysis agents, synth
common.py               Tiện ích dùng chung (dedupe_keep_order, prediction_key)
config/                 Cấu hình mặc định và keyword whitelist
data/                   Báo cáo Markdown đầu vào
dataset_catalog/        Registry API cho dataset
dataset_store/          Registry, manifest, SQLite/raw-table sinh ra khi build
evaluation/             Contract report v2 + fingerprint (contracts, run_identity),
                        RAGAS judge/metric setup (ragas_evaluator),
                        chuẩn hóa số/kỳ cho gate (financial_text)
graph/                  LangGraph workflow, routing, evidence pack
ingestion/              Parser Markdown/table/frontmatter/note, build KB
kb/                     SQLite repository
llm/                    LangChain Ollama client và invoke helper
ragas_runs/             Artifact evaluation cục bộ; chỉ manifest/fixture nhỏ được track
schemas/                Pydantic models và normalizer
scripts/                Tiện ích vận hành (run_rotate, diagnostics)
tests/                  Unit tests (pytest)
tools/                  Retrieval tools cho agents
vectorstore/            Qdrant adapter, lexical index, reranker
```

Các file dữ liệu/evaluation ở root:

- `dau_tu_APEC_ragas_seed.json`: seed chuẩn cho evaluation bộ APEC.
- `queries_test.json`: câu hỏi batch thông thường.
- `batch_test_results.json`: output batch query (sinh ra khi chạy).

## Cài Đặt

Yêu cầu Python 3.11 trở lên (khuyến nghị 3.12) trong virtual environment. `pyproject.toml` là nguồn dependency duy nhất.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .                 # runtime core
```

Các dependency không thuộc runtime core tách thành optional groups:

```bash
python -m pip install -e '.[eval]'         # RAGAS, HF datasets, Claude judge
python -m pip install -e '.[dev]'          # pytest, ruff, mypy, detect-secrets
python -m pip install -e '.[eval,dev]'     # cả hai (dùng cho CI/dev)
```

Core install **không** kéo theo các dependency nặng của evaluation (RAGAS/datasets) — chúng chỉ có trong extra `eval`.

## Biến Môi Trường

Sao chép `.env.example` thành `.env`, rồi điền credential ở local. Không commit `.env`.

```bash
OLLAMA_API_KEY=<ollama-api-key>
OLLAMA_BASE_URL=https://ollama.com
OLLAMA_MODEL=gpt-oss:120b-cloud

OLLAMA_EMBEDDING_MODEL=bge-m3
OLLAMA_EMBEDDING_BASE_URL=http://127.0.0.1:11434

QDRANT_URL=https://<cluster-url>.qdrant.io:6333
QDRANT_API_KEY=<qdrant-api-key>
```

Dùng Qdrant local/in-memory thì cấu hình `QDRANT_LOCATION` thay cho `QDRANT_URL`. Judge RAGAS mặc định là `minimax-m3` trên Ollama Cloud; muốn dùng Claude judge thì thêm:

```bash
RAGAS_JUDGE=anthropic
ANTHROPIC_API_KEY=<anthropic-api-key>
RAGAS_JUDGE_MODEL=claude-opus-4-8
```

## Chạy Một Câu Hỏi

Liệt kê dataset đã đăng ký:

```bash
agentfinx ask --list-datasets
```

Chạy một câu hỏi trên dataset mặc định hoặc dataset đã match:

```bash
agentfinx ask --query "ROE quý 2/2025 là bao nhiêu?"
```

Chạy theo dataset id:

```bash
agentfinx ask --dataset-id apec --query "Tổng tài sản cuối kỳ là bao nhiêu?"
```

Đăng ký hoặc build dataset mới từ file Markdown:

```bash
agentfinx ask \
  --file-path data/dau_tu_APEC.md \
  --dataset-id apec \
  --company "Công ty Cổ phần Đầu tư Châu Á Thái Bình Dương" \
  --ticker APEC \
  --fiscal-year 2025 \
  --fiscal-quarter 2 \
  --scope standalone \
  --audit-status unaudited \
  --query "Tổng tài sản cuối kỳ là bao nhiêu?"
```

Khi dataset chưa build hoặc ingestion version thay đổi, CLI tự tạo/cập nhật SQLite KB, raw table files, manifest và Qdrant collection. (Mọi ví dụ `agentfinx ask …` tương đương `python test.py …`.)

## Chọn Dataset

`ask`/`test.py` resolve dataset theo thứ tự:

1. Có `--file-path`: tạo hoặc cập nhật dataset từ file đó.
2. Có `--dataset-id`: lấy đúng dataset id.
3. Có filter `--company` / `--ticker` / `--fiscal-year`…: tìm trong registry.
4. Interactive và có nhiều match: yêu cầu chọn.
5. Không filter: dùng dataset mặc định trong `config/settings.py`.

Registry nằm ở `dataset_store/registry.json`.

## Xóa Dataset

```bash
agentfinx ask --delete-dataset --dataset-id apec           # thêm --yes để non-interactive
```

Lệnh xóa chỉ bỏ registry/manifest/SQLite/raw tables/Qdrant collection của dataset, không xóa source document gốc.

## Batch Query

`agentfinx batch` (= `dataset_batch_runner.py`) chạy cùng một bộ câu hỏi trên tất cả dataset hoặc một nhóm được chọn.

```bash
agentfinx batch --query "ROE là bao nhiêu?" --output batch_test_results.json

agentfinx batch --queries-file queries_test.json --output batch_test_results.json

agentfinx batch --dataset-id apec --queries-file queries_test.json \
  --output batch_test_results.json --include-trace
```

Mặc định merge kết quả mới vào output cũ theo từng query/dataset; thêm `--overwrite-output` để ghi đè hoàn toàn.

Batch report dùng schema v2: mỗi query có `dataset_summaries`; scalar `final_answer` chỉ được điền khi query chạy đúng một dataset, còn nhiều dataset thì để rỗng thay vì lấy index 0.

```json
{
  "query": "ROE là bao nhiêu?",
  "final_answer": "",
  "dataset_summaries": [
    {"dataset_id": "dataset-a", "final_answer": "ROE A là 6%."},
    {"dataset_id": "dataset-b", "final_answer": "ROE B là 8%."}
  ]
}
```

Reader cũ vẫn được hỗ trợ khi merge report v1; output ghi mới luôn có `schema_version`, `run_fingerprint`, `queries` và `query_reports`.

## Evaluation

Seed chuẩn của bộ APEC là [`dau_tu_APEC_ragas_seed.json`](dau_tu_APEC_ragas_seed.json). Population chính thức của hard gate nằm trong [`tests/fixtures/apec_q211_250_factual_facts.json`](tests/fixtures/apec_q211_250_factual_facts.json); mỗi fact bắt buộc có đủ `entity`, `metric`, `period`, `value`, `unit` và `reference` để kết quả không phụ thuộc LLM judge. Seed, query fixture và facts contract được giữ trong repo để clean clone dựng lại được benchmark; credential từng xuất hiện trong `.env` phải rotate ở nhà cung cấp.

### Tạo prediction

```bash
# smoke run 10 câu
agentfinx predict --dataset-id apec --seed-file dau_tu_APEC_ragas_seed.json \
  --limit 10 --output ragas_runs/apec_smoke_predictions.json

# full seed (thêm --resume để tiếp tục khi bị gián đoạn)
agentfinx predict --dataset-id apec --seed-file dau_tu_APEC_ragas_seed.json \
  --full --resume --output ragas_runs/apec_full_predictions.json
```

### Chấm RAGAS (diagnostic)

```bash
agentfinx score --predictions-file ragas_runs/apec_full_predictions.json \
  --output ragas_runs/apec_full_scored.json          # thêm --force để chấm lại toàn bộ
```

Metrics: `faithfulness`, `answer_relevancy`, `context_precision`, `context_recall`. RAGAS chỉ mang tính diagnostic — báo mean/distribution/missing metrics theo bucket factual và analytical. **Không** có gate dựa trên chênh lệch RAGAS mean `0.03` (judge free-tier nhiễu ~±0.1, dưới ngưỡng đó). Dùng `agentfinx analyze` để tách bucket và tính factual-recall xác định.

### Hard gate deterministic factual recall

Gate chất lượng bắt buộc:

```text
deterministic_factual_recall >= 0.95
```

```bash
agentfinx recall --dataset-id apec --json-out ragas_runs/apec_factual_recall.json
```

`--predictions-file` là tùy chọn để enrich report; file này không thêm identity hay đổi denominator/expected facts của contract. Một expected fact chỉ match khi sáu field cùng xuất hiện trong một fact/context sau normalization; hệ thống không ghép giá trị từ nhiều dòng độc lập để tạo false positive. Process trả exit code khác 0 khi recall dưới `0.95`.

### Clean latency baseline

Latency sản phẩm chỉ tính prediction, không gồm startup/index build, warm-up hay RAGAS judge. Tạo ba prediction run hoàn chỉnh trên cùng tập APEC q181–250, cùng model/embedding/Qdrant index/hardware/network:

```bash
agentfinx predict --dataset-id apec --seed-file dau_tu_APEC_ragas_seed.json \
  --offset 180 --limit 70 --output ragas_runs/apec_q181_250_run1.json
# ... run2, run3 tương tự
```

Chỉ attest sau khi xác nhận không có Ollama `session_limit`/`usage_limit`/HTTP 429/quota backoff:

```bash
agentfinx score --predictions-file ragas_runs/apec_q181_250_run1.json \
  --attest-clean-latency-run

agentfinx analyze \
  ragas_runs/apec_q181_250_run1.json \
  ragas_runs/apec_q181_250_run2.json \
  ragas_runs/apec_q181_250_run3.json
```

Một run có provider limit/quota backoff/prediction error hoặc thiếu clean-environment attestation thì cả run đó bị đánh dấu latency-invalid (không loại riêng sample chậm). Baseline chỉ thiết lập khi có ≥ 3 run sạch, dùng median của p50/p95 và mean token. Các số cũ nhiễu Ollama limit không phải baseline.

## Debug

```bash
agentfinx ask --dataset-id apec --query "Tổng tài sản cuối kỳ là bao nhiêu?" --debug-trace
```

Trace gồm event planner/router/evidence/synth và `run:done` với runtime/token summary. Khi bật `--debug-trace`, một số event tool có thêm preview context để điều tra retrieval.

## Runtime Contracts

- `NOTE_FACTS_LIMIT=12` là contract mặc định cho thuyết minh; câu schedule/list dùng 24. Main statement dùng 10 (hoặc 16 cho schedule). Cap 5 của một tool observation không được dùng để hạ NOTE evidence xuống 5.
- `get_report_section_info` chạy ở evidence stage, không nằm trong allowlist tool mặc định của analysis agents.
- `message` là field canonical cho trạng thái not-found; reader vẫn đọc `interpretation_hint` của report v1.
- User query, planner plan, retrieved evidence, worker results, web data, previous response và tool observations đều nằm trong human messages và đánh dấu untrusted. Chỉ role, system instruction và danh sách bound tools nằm trong system messages.
- Collection handle là context-local. Cache retrieval có key gồm dataset, query/intent và index generation để không dùng evidence của dataset/index cũ.
- Internal object như `collection` không được serialize vào trace/report/tool arguments công khai.

## Test

```bash
python -m pytest tests                     # toàn bộ (287 test)

python -m pytest \
  tests/test_dataset_batch_runner.py \
  tests/test_ragas_eval_runner.py \
  tests/test_runtime_contracts.py
```

Unit test đã mock phần LLM và không gọi API thật. CI (`.github/workflows/ci.yml`) chạy `pip install -e '.[eval,dev]'`, pytest, `compileall`, build wheel và `detect-secrets`.

## Ghi Chú Vận Hành

- `dataset_store/`, `ragas_runs/`, `batch_test_results.json` là output sinh ra khi chạy.
- `data/` chứa source Markdown đầu vào; không xóa khi chỉ muốn reset dataset đã build.
- `test.py` là CLI chính (`agentfinx ask`), không phải unit test.
- `dataset_batch_runner.py` (batch answer) và `dataset_batch_result.py` (prediction/context cho RAGAS) là hai nhiệm vụ khác nhau.
- KB/SQLite và Qdrant index build ở staging rồi mới activate; build lỗi không phá artifact đang hoạt động.
- Artifact lớn trong `ragas_runs/`, cache và database sinh ra khi chạy không commit. Chỉ seed, manifest và fixture nhỏ đã review được version-control.
