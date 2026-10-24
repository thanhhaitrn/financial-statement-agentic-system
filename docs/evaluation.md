# Đánh giá

## Nguyên tắc

- **Cổng chất lượng duy nhất** là factual recall xác định:
  `deterministic_factual_recall >= 0.95` trên hợp đồng fact đã duyệt. Mỗi fact có `entity`,
  `metric`, `period`, `value`, `unit`, `reference`, nên kết quả không phụ thuộc LLM judge.
- **RAGAS chỉ để chẩn đoán:**
  - judge free-tier nhiễu khoảng ±0.1;
  - không dùng chênh lệch RAGAS mean làm cổng.
- **Danh tính lần chạy:** mỗi lần chạy ghi run fingerprint gồm code, policy, model, dataset và
  seed. Chỉ so sánh các lần chạy có cùng fingerprint.

## Dữ liệu

| Loại | Ở đâu | Track |
|---|---|---|
| Seed, bộ câu hỏi, báo cáo APEC và báo cáo mẫu, fixture RAGAS | `backend/benchmarks/` ([danh sách](../backend/benchmarks/README.md)) | Có |
| Hợp đồng fact của cổng | `backend/tests/fixtures/apec_q211_250_factual_facts.json` | Có |
| Bộ câu hỏi đối chứng mà các bộ đo dùng mặc định | `backend/src/agentfinx/evaluation/data/` (đi kèm package) | Có |
| Prediction, điểm, báo cáo triage, run A/B | `artifacts/eval/` | Không |

Tên file tương đối (mặc định, hoặc ghi trong report/hợp đồng) được tìm theo thứ tự:
1. `backend/`;
2. `backend/benchmarks/`;
3. thư mục chứa file nêu tên nó;
4. dữ liệu đi kèm package;
5. cuối cùng mới đến thư mục đang đứng.

Vì vậy giá trị mặc định không phụ thuộc chỗ chạy lệnh. Đầu ra mặc định nằm trong
`artifacts/eval/`.

## Lệnh

```bash
# Prediction: câu trả lời + context truy xuất cho bộ APEC (seed mặc định)
agentfinx predict --dataset-id apec --limit 10 --output artifacts/eval/apec_smoke.json
agentfinx predict --dataset-id apec --full --output artifacts/eval/apec_clean_full.json
agentfinx predict --dataset-id apec --full --resume --output artifacts/eval/apec_full.json   # phát triển, không phải benchmark sạch

# Cổng factual recall (thoát với mã khác 0 khi dưới 0.95)
agentfinx recall --dataset-id apec --json-out artifacts/eval/apec_factual_recall.json [--predictions-file …]

# RAGAS (chẩn đoán), tổng hợp theo nhóm factual/analytical, phân loại lỗi
agentfinx score --predictions-file artifacts/eval/apec_full.json [--force]
agentfinx analyze artifacts/eval/apec_full.json [more.json …]
agentfinx triage artifacts/eval/apec_full.json      # báo cáo <stem>_error_triage.md/.json

# Batch: một bộ câu hỏi trên một hay nhiều báo cáo
agentfinx batch --queries-file backend/benchmarks/queries_test.json --output artifacts/eval/batch.json [--dataset-id …]
agentfinx batch --web-entry --queries-file …        # đi qua planner của chat như trên web
```

- **Benchmark sạch:** chỉ khi report có `run_complete=true` và `clean_full_run=true`, không lỗi
  prediction hay nhà cung cấp, và mọi câu cùng fingerprint code/KB/index. Chạy shard hay
  `--resume` vẫn dùng được để triage, nhưng không để chốt mức cải thiện.
- **`batch` và `predict`:**
  - dùng chung cách chạy một câu hỏi (`evaluation/runner.py`);
  - với `--web-entry`, câu hỏi bị planner web chuyển sang tìm báo cáo được ghi là từ chối trả
    lời;
  - batch report schema v2: mỗi query có `dataset_summaries`; `final_answer` cấp query chỉ được
    điền khi chạy đúng một báo cáo.
- **Judge RAGAS:** mặc định là model trên Ollama. Muốn dùng Claude thì đặt
  `RAGAS_JUDGE=anthropic`, `ANTHROPIC_API_KEY`, `RAGAS_JUDGE_MODEL` (extra `eval`).

### Chạy song song nhiều khóa

Pilot bảo mật có cơ chế đổi key riêng ngay trong runner. Lệnh dưới tiếp tục đúng
thư mục kết quả, ưu tiên key đang hoạt động rồi dùng các key dự phòng trong
`FINX_HOME/.env` khi Ollama báo quota:

```bash
agentfinx eval-security pilot --allow-live --out artifacts/eval/security/<pilot-id> --budget 300 --include-commented-keys
```

`--keys-file` chọn file key khác. Mặc định chỉ đọc dòng `OLLAMA_API_KEY=` đang bật;
`--include-commented-keys` cho phép dùng cả dòng `# OLLAMA_API_KEY=` làm dự phòng.
Runner dùng chung bộ đọc key với `scripts/run_rotate.py`, loại key trùng, không sửa
`.env` và chỉ truyền secret qua môi trường worker. Key được giữ cố định trong mỗi
lượt QA. Quota tạo attempt mới trên key kế tiếp; kết quả thành công không chạy lại.
Lỗi mạng thông thường chỉ thử lại một lần trên cùng key, không kích hoạt đổi key.

Mọi attempt vẫn tính vào cùng ngân sách 300 lần gọi. Hết key thì dừng với
`all_keys_exhausted` (exit code 3), giữ kết quả và trạng thái key để resume. Khi quota
đã được nhà cung cấp cấp lại, dùng `--retry-exhausted-keys` để thử lại các key cũ;
cờ này không đặt lại ngân sách. Có thể thêm key mới vào file rồi resume mà không
thử lại key đã hết quota. Không chạy đồng thời hai runner trong cùng thư mục.
Thay đổi chính sách điều phối được ghi ở `orchestration.jsonl`, tách khỏi fingerprint
model/prompt để không làm mất hiệu lực các câu trả lời đã hoàn tất.

Mỗi `OLLAMA_API_KEY` có quota riêng, nên có thể chia seed thành nhiều shard, mỗi shard một khóa
và một file output riêng.

- **Điều kiện:**
  - Qdrant dùng chung (không `:memory:`);
  - báo cáo đã build sẵn.
- **Không dùng cho latency baseline.**
- **Một shard hết quota:** chạy lại đúng lệnh của shard đó với `--resume`.
- **Xoay khóa tự động:** `scripts/run_rotate.py` chạy lại lệnh và đổi khóa khi hết quota.

### Latency baseline

Latency chỉ tính phần prediction (không gồm khởi động, build index hay judge).

- **Cách chạy:** ba lần chạy đầy đủ APEC q181–250 trong cùng môi trường:

  ```bash
  agentfinx predict --dataset-id apec --offset 180 --limit 70 --output artifacts/eval/apec_q181_250_run1.json
  agentfinx score --predictions-file artifacts/eval/apec_q181_250_run1.json --attest-clean-latency-run
  agentfinx analyze artifacts/eval/apec_q181_250_run{1,2,3}.json
  ```

- **Điều kiện attest:** chỉ attest khi không có `session_limit`, `usage_limit`, HTTP 429 hay
  quota backoff. Lần chạy có các tín hiệu đó bị đánh dấu latency-invalid.
- **Baseline:** median của p50/p95 trên ít nhất ba lần chạy sạch có cùng fingerprint.

## Đo phân loại của planner (gọi model thật, không chạy trong CI)

```bash
agentfinx eval-routing --questions web_intent_contrastive.json --planner both --runs 5
agentfinx eval-routing --questions task_routing_contrastive.json --runs 3 --dataset-id suavietnam
agentfinx eval-routing --questions queries_test.json --expect report_qa   # mục tiêu: 0 câu bị chuyển nhầm
```

- **Kiểm tra:** `task_kind`, `need_web` và `web_intent`, với báo cáo đang mở làm ngữ cảnh như
  lúc chạy thật.
- **Mã thoát 1** khi một câu `clear` sai ở bất kỳ lượt nào, hoặc một câu hỏi báo cáo bị chuyển
  sang tìm báo cáo.
- **Quy tắc phân loại:**
  - tin hay sự kiện của một doanh nghiệp là `company_news`;
  - vĩ mô, thị trường chung hay giá cổ phiếu là `unsupported_external`;
  - trả lời được bằng BCTC thì `need_web=false`.
- **Kết quả 11/10/2026:**
  - prompt cũ đúng 142/165 lượt (7/31 ca rõ nghĩa không ổn định);
  - prompt mới đúng 165/165;
  - bộ cũ đúng 51/51.

## Gate ngữ cảnh hội thoại

```bash
agentfinx eval-context --questions queries_test.json --scenarios context_contrastive.json --dry-run
agentfinx eval-context --questions queries_test.json --scenarios context_contrastive.json \
  --allow-live --max-calls 200 --output artifacts/eval/context-gate.json
```

- **Cách đo:**
  - mỗi câu độc lập được thử ở lượt thứ hai với hai lịch sử khác chủ đề hoặc khác kỳ;
  - gate yêu cầu 0 câu độc lập bị hỏi lại hay đổi nghĩa;
  - lỗi guard, schema và nhà cung cấp được tách riêng.
- **`--limit`** chỉ để chẩn đoán.
- **Phạm vi:** gate này chỉ đo resolver. Factual recall và `WRONG_REFUSAL ≤ 21` vẫn cần đánh giá
  QA riêng, kể cả qua worker nhiều lượt.
- **Gọi model thật** cần được cho phép trước.
