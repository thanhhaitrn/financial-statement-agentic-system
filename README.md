# AgentFinX Financial QA

AgentFinX là pipeline hỏi đáp báo cáo tài chính tiếng Việt. Project đọc báo cáo dạng Markdown, chuẩn hóa bảng/thuyết minh vào SQLite, build chỉ mục tìm kiếm bằng Qdrant + embedding, rồi dùng graph agent để lập kế hoạch, lấy bằng chứng và tổng hợp câu trả lời.

Repo hiện có 4 luồng sử dụng chính:

- `test.py`: CLI chính để đăng ký/build dataset và hỏi một câu.
- `dataset_batch_runner.py`: chạy một hoặc nhiều câu hỏi trên các dataset đã đăng ký, tạo `batch_test_results.json`.
- `dataset_batch_result.py` + `ragas_eval_runner.py`: tạo prediction/context và chấm RAGAS.
- `web/`: frontend tĩnh + FastAPI backend cho demo chat/upload.

## Cấu Trúc

```text
agents/                 Planner, router/keyworder, analysis agents, synth
config/                 Cấu hình mặc định và keyword whitelist
data/                   Báo cáo Markdown đầu vào
datasets/               Registry API cho dataset
dataset_store/          Registry, manifest, SQLite/raw-table sinh ra khi build
graph/                  LangGraph workflow, routing, evidence pack
ingestion/              Parser Markdown/table/frontmatter/note, build KB
kb/                     SQLite repository
llm/                    LangChain Ollama client và invoke helper
ragas_runs/             Kết quả prediction/RAGAS JSON + CSV
schemas/                Pydantic models và normalizer
tests/                  Unit tests
tools/                  Retrieval tools cho agents
vectorstore/            Qdrant adapter, lexical index, reranker
web/                    Demo frontend/backend
```

Các file dữ liệu/evaluation thường dùng:

- `dau_tu_APEC.json`: bộ câu hỏi/tham chiếu gốc.
- `dau_tu_APEC_ragas_seed.json`: seed cho RAGAS.
- `queries_test.json`: câu hỏi batch thông thường.
- `batch_test_results.json`: output batch query.

## Cài Đặt

Khuyến nghị dùng Python 3.12 trong virtual environment.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

`requirements.txt` là file dependency chính của repo. Nó bao gồm pipeline, RAGAS, Streamlit viewer, FastAPI backend, optional neural reranker và pytest.

## Biến Môi Trường

Tạo file `.env` ở root repo. Các biến tối thiểu thường cần:

```bash
OLLAMA_API_KEY=<ollama-api-key>
OLLAMA_BASE_URL=https://ollama.com
OLLAMA_MODEL=gpt-oss:120b-cloud

OLLAMA_EMBEDDING_MODEL=bge-m3
OLLAMA_EMBEDDING_BASE_URL=http://127.0.0.1:11434

QDRANT_URL=https://<cluster-url>.qdrant.io:6333
QDRANT_API_KEY=<qdrant-api-key>
```

Nếu dùng Qdrant local/in-memory, có thể cấu hình `QDRANT_LOCATION` thay cho `QDRANT_URL`. Nếu dùng RAGAS với Claude judge, thêm:

```bash
RAGAS_JUDGE=anthropic
ANTHROPIC_API_KEY=<anthropic-api-key>
RAGAS_JUDGE_MODEL=claude-opus-4-8
```

Optional neural reranker chỉ chạy khi bật:

```bash
NEURAL_RERANK=1
NEURAL_RERANK_MODEL=keepitreal/vietnamese-sbert
```

## Chạy Một Câu Hỏi

Liệt kê dataset đã đăng ký:

```bash
python test.py --list-datasets
```

Chạy một câu hỏi trên dataset mặc định hoặc dataset đã match:

```bash
python test.py --query "ROE quý 2/2025 là bao nhiêu?"
```

Chạy theo dataset id:

```bash
python test.py \
  --dataset-id apec \
  --query "Tổng tài sản cuối kỳ là bao nhiêu?"
```

Đăng ký hoặc build dataset mới từ file Markdown:

```bash
python test.py \
  --file-path data/dau_tu_APEC.md \
  --dataset-id apec \
  --company "Công ty Cổ phần Chứng khoán Châu Á Thái Bình Dương" \
  --ticker APEC \
  --fiscal-year 2025 \
  --fiscal-quarter 2 \
  --scope standalone \
  --audit-status unaudited \
  --query "Tổng tài sản cuối kỳ là bao nhiêu?"
```

Khi dataset chưa được build hoặc ingestion version thay đổi, CLI sẽ tự tạo/cập nhật SQLite KB, raw table files, manifest và Qdrant collection.

## Chọn Dataset

`test.py` resolve dataset theo thứ tự:

1. Có `--file-path`: tạo hoặc cập nhật dataset từ file đó.
2. Có `--dataset-id`: lấy đúng dataset id.
3. Có filter như `--company`, `--ticker`, `--fiscal-year`: tìm trong registry.
4. Chạy interactive và có nhiều match: yêu cầu chọn dataset.
5. Không có filter: dùng dataset mặc định trong `config/settings.py`.

Registry nằm ở `dataset_store/registry.json`.

## Xóa Dataset

```bash
python test.py --delete-dataset --dataset-id apec
```

Chạy non-interactive thì thêm `--yes`:

```bash
python test.py --delete-dataset --dataset-id apec --yes
```

Lệnh xóa chỉ xóa registry/manifest/SQLite/raw tables/Qdrant collection của dataset, không xóa source document gốc.

## Batch Query

`dataset_batch_runner.py` chạy cùng một bộ câu hỏi trên tất cả dataset hoặc một nhóm dataset được chọn.

Chạy một câu:

```bash
python dataset_batch_runner.py \
  --query "ROE là bao nhiêu?" \
  --output batch_test_results.json
```

Chạy từ file JSON:

```bash
python dataset_batch_runner.py \
  --queries-file queries_test.json \
  --output batch_test_results.json
```

Chạy một dataset cụ thể và lưu trace:

```bash
python dataset_batch_runner.py \
  --dataset-id apec \
  --queries-file queries_test.json \
  --output batch_test_results.json \
  --include-trace
```

Mặc định script merge kết quả mới vào output cũ theo từng query/dataset. Muốn ghi đè hoàn toàn, thêm `--overwrite-output`.

Xem kết quả batch bằng Streamlit:

```bash
streamlit run streamlit_batch_results_viewer.py
```

## RAGAS Evaluation

Luồng RAGAS có 2 bước: tạo prediction/retrieved contexts, rồi chấm điểm.

Smoke run 10 câu:

```bash
python dataset_batch_result.py \
  --dataset-id apec \
  --seed-file dau_tu_APEC_ragas_seed.json \
  --limit 10 \
  --output ragas_runs/apec_smoke_predictions.json
```

Chấm RAGAS:

```bash
python ragas_eval_runner.py \
  --predictions-file ragas_runs/apec_smoke_predictions.json \
  --output ragas_runs/apec_smoke_scored.json
```

Chạy full seed:

```bash
python dataset_batch_result.py \
  --dataset-id apec \
  --seed-file dau_tu_APEC_ragas_seed.json \
  --full \
  --output ragas_runs/apec_full_predictions.json
```

Resume khi bị gián đoạn:

```bash
python dataset_batch_result.py \
  --dataset-id apec \
  --seed-file dau_tu_APEC_ragas_seed.json \
  --full \
  --resume \
  --output ragas_runs/apec_full_predictions.json
```

Chấm lại toàn bộ scores, bỏ qua điểm cũ:

```bash
python ragas_eval_runner.py \
  --predictions-file ragas_runs/apec_full_predictions.json \
  --output ragas_runs/apec_full_scored.json \
  --force
```

Metrics hiện dùng: `faithfulness`, `answer_relevancy`, `context_precision`, `context_recall`.

## Web Demo

Backend nằm trong `web/backend`, frontend tĩnh nằm trong `web`.

Chạy backend:

```bash
cd web/backend
python -m pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Mở frontend:

```bash
cd web
python -m http.server 3000
```

Sau đó vào `http://localhost:3000`. API docs ở `http://localhost:8000/docs`.

Các biến backend hữu ích:

- `FINX_SECRET_KEY`
- `FINX_CORS_ORIGINS`
- `FINX_DB_PATH`
- `FINX_UPLOAD_DIR`
- `FINX_ALLOWED_UPLOAD_EXTS`

## Debug

Bật trace debug cho một câu hỏi:

```bash
python test.py \
  --dataset-id apec \
  --query "Tổng tài sản cuối kỳ là bao nhiêu?" \
  --debug-trace
```

Trace chính gồm các event planner/router/evidence/synth và `run:done` với runtime/token summary. Khi bật `--debug-trace`, một số event tool có thêm preview context để điều tra retrieval.

## Test

Chạy toàn bộ test:

```bash
python -m pytest tests
```

Chạy nhóm liên quan batch/RAGAS:

```bash
python -m pytest tests/test_dataset_batch_runner.py tests/test_ragas_eval_runner.py
```

Lưu ý: một số test/luồng có thể cần biến môi trường hoặc dependency LLM/Qdrant nếu chạm đến workflow thật. Các unit test đã mock phần LLM thường không cần gọi API thật.

## Ghi Chú Vận Hành

- `dataset_store/`, `ragas_runs/`, `batch_test_results.json` là output sinh ra trong quá trình chạy.
- `data/` chứa source Markdown đầu vào; không nên xóa khi chỉ muốn reset dataset đã build.
- `test.py` là CLI chính của project, không phải unit test.
- `dataset_batch_runner.py` phục vụ batch answer thông thường.
- `dataset_batch_result.py` phục vụ tạo prediction/context cho RAGAS, không trùng nhiệm vụ với `dataset_batch_runner.py`.
