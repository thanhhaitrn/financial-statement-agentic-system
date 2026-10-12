# AgentFinX

Hỏi đáp báo cáo tài chính tiếng Việt theo hướng *evidence-first*: báo cáo được
chuẩn hóa vào SQLite và chỉ mục Qdrant, một graph agent (LangGraph) lập kế hoạch,
truy xuất bằng chứng và tổng hợp câu trả lời. Có giao diện web cho nhóm (đăng nhập,
hỏi đáp, tải PDF, tìm báo cáo trên CafeF, tin tức doanh nghiệp từ Vietstock).

```text
backend/     Package Python agentfinx (API, worker, agent, workflow, đánh giá) và test
web/         Giao diện Next.js; gọi API qua proxy /api
docs/        Kiến trúc, vận hành, bảo mật, đánh giá
scripts/     Công cụ vận hành không đóng gói
storage/     Dữ liệu runtime: tài khoản, báo cáo đã build, import, cache tin (không track)
artifacts/   Kết quả chạy: báo cáo đánh giá, log (không track)
```

## Cần có

- Python ≥ 3.11 và Node.js ≥ 20.9.
- Ollama cho model chat và embedding (`ollama serve`; mặc định embedding `bge-m3`).
- Qdrant dạng server (Qdrant Cloud hoặc chạy local) cho giao diện web.
- Tùy chọn: khóa LlamaParse để trích xuất PDF scan.

## Cài đặt

```bash
python -m venv .venv && source .venv/bin/activate
python -m pip install -e 'backend[web,dev]'
(cd web && npm ci)
cp .env.example .env        # rồi điền model, Qdrant và FINX_SECRET_KEY
```

Extra khác của `backend`: `eval` (RAGAS), `e2e` (Playwright cho test trình duyệt),
`smoke` (tạo PDF thử LlamaParse). Lệnh duy nhất là `agentfinx` (`agentfinx --help`),
chạy được từ bất kỳ thư mục nào.

## Lần đầu

```bash
agentfinx storage status      # đang dùng thư mục dữ liệu nào
agentfinx db init             # máy mới: tạo database tài khoản
# Máy còn dữ liệu ở dataset_store/ (bố cục cũ): agentfinx storage migrate, rồi --apply

# Build một báo cáo từ Markdown và hỏi thử
agentfinx ask --file-path backend/benchmarks/reports/document.md --dataset-id mau \
  --company "Công ty Mẫu" --fiscal-year 2025 --query "Tổng tài sản cuối kỳ là bao nhiêu?"

# Cho cả nhóm hỏi trên báo cáo đó: đặt FINX_SHARED_DATASET_IDS=mau trong .env
# Mời người dùng (đăng ký mặc định chỉ bằng mã mời)
agentfinx invites create --email ban@congty.vn
```

## Chạy

Bốn process, mỗi cái một terminal:

```bash
agentfinx serve                  # API, http://127.0.0.1:8000
agentfinx worker --queue chat    # trả lời câu hỏi, lần lượt từng câu
agentfinx worker --queue import  # xử lý PDF tải lên / báo cáo CafeF
cd web && npm run dev            # giao diện, http://127.0.0.1:3000
```

Bản build của giao diện: `npm run build && npm start`. API không ở cổng 8000 thì đặt
`FINX_API_URL` trong `web/.env.local` **trước khi build**.

## Kiểm thử

```bash
cd backend && python -m pytest            # không gọi model hay dịch vụ thật
cd web && npm run typecheck && npm run build
```

Test trình duyệt (`backend/tests/test_next_browser_smoke.py`) cần extra `e2e`,
`python -m playwright install chromium` và một bản build của `web/`; thiếu thì tự bỏ qua.

## Tài liệu

- [Kiến trúc](docs/architecture.md): các process, package, luồng trả lời, tin tức, nhập báo cáo.
- [Vận hành](docs/operations.md): dữ liệu và migrate, cấu hình, người dùng, báo cáo, nâng cấp.
- [Bảo mật](docs/security.md): đăng nhập, phiên, giới hạn, mở ra ngoài localhost, secret.
- [Đánh giá](docs/evaluation.md): cổng factual recall, RAGAS, benchmark, đo phân loại.
