# Vận hành

## Thư mục dữ liệu

Mọi thứ app ghi ra nằm dưới `FINX_HOME`. Bản checkout mặc định dùng chính thư mục repo; bản cài
từ wheel **phải** đặt `FINX_HOME` (không đặt thì lệnh dừng, không tự tạo database rỗng ở chỗ
khác). Thư mục đang đứng không bao giờ ảnh hưởng tới chỗ lưu. `FINX_HOME` chỉ đọc từ môi trường;
các biến khác có thể để trong `FINX_HOME/.env` (giá trị đã đặt trong môi trường được ưu tiên).

| Đường dẫn | Nội dung |
|---|---|
| `storage/web/finx_web.sqlite3` | Tài khoản, phiên, cuộc trò chuyện, chat run, mã mời (`FINX_WEB_DB` để đổi) |
| `storage/datasets/` | `registry.json`, `manifests/`, `sqlite/` (KB), `raw_tables/`, `sources/` (Markdown của báo cáo nhập), `registry_backups/` |
| `storage/reports/` | File báo cáo Markdown thêm bằng tay cho `agentfinx ask --file-path` |
| `storage/acquisition/` | `acquisition.db` (job, quota, chi phí), PDF tải lên, cache LlamaParse |
| `storage/news/evidence.db` | Cache tin Vietstock dùng chung |
| `storage/catalog/cafef_symbols.json` | Danh mục mã CK của CafeF |
| `storage/locks/` | Khóa giữa các process (registry, dataset, một worker mỗi queue) |
| `artifacts/eval/` | Kết quả `predict`/`score`/`recall`/`triage`, có thể xóa |

```bash
agentfinx storage status    # FINX_HOME, database đang dùng, số tài khoản và báo cáo
```

**Sao lưu:**
- Dừng API và cả hai worker.
- Sao chép nguyên `storage/`; chỉ mục Qdrant nằm trên Qdrant server, sao lưu theo cách của nó.
- Không bao giờ xóa `storage/` để "dọn dẹp".

**Migrate từ bố cục cũ:** máy từng chạy bản trước có `dataset_store/` ở repo; app từ chối chạy
cho tới khi dữ liệu được chuyển.

```bash
agentfinx storage migrate           # xem kế hoạch, không đổi gì
agentfinx storage migrate --apply   # sao chép và kiểm tra
```

Lệnh `--apply`:
- **Từ chối chạy** khi có khóa đang bị giữ, hoặc một worker vừa báo nhịp tim (dưới 60 giây).
- **Sao chép:**
  - SQLite sao chép bằng backup API, kiểm tra `integrity_check` và số dòng từng bảng;
  - file khác kiểm bằng SHA-256;
  - đường dẫn trong registry và manifest được viết lại.
- **Kiểm tra bằng chính code của app:**
  - registry mở được mà không cần sửa;
  - mọi báo cáo `ready` qua được phép kiểm mà chat dùng.
- **Ghi kết quả:** chỉ sau tất cả các bước trên mới ghi `storage/migration.json`.

`dataset_store/` và `data/` không bị đụng tới; xóa bằng tay sau khi đã chạy thử app trên dữ liệu
mới (`agentfinx storage status` báo "migrated, can be deleted").

## Chạy, dừng, nâng cấp

```bash
agentfinx db init                  # máy mới, một lần; serve/worker/invites không tự tạo database
agentfinx serve [--host --port]    # API, mặc định 127.0.0.1:8000
agentfinx worker --queue chat
agentfinx worker --queue import
cd web && npm run build && npm start   # giao diện, 127.0.0.1:3000
```

- **Khi khởi động:** serve và worker ghi log database đang dùng cùng số tài khoản. Không tìm thấy
  database thì dừng và chỉ cách xử lý.
- **Khởi động lại worker:**
  - câu hỏi đang chờ: tiếp tục bình thường;
  - câu hỏi đang chạy dở: thành `interrupted`, không tự chạy lại (tránh gọi model hai lần);
  - job import dở: tiếp tục theo mã job đã lưu, không gửi lại.
- **Nâng cấp database web:**
  - lần khởi động đầu sau khi cập nhật code tự nâng schema;
  - trước khi nâng, nó sao lưu thành `finx_web.sqlite3.backup-v<cũ>-<thời điểm>` cạnh file;
  - nó từ chối nâng nếu một worker vừa báo nhịp tim (< 45 giây), nên hãy dừng API và cả hai
    worker trước;
  - lên schema v6 thì mọi người phải đăng nhập lại một lần;
  - không chạy code cũ trên database đã nâng.
- **Đổi cờ:** sau khi đổi `WEB_EVIDENCE_ENABLED`, `WEB_ANALYSIS_ENABLED` hay `FINX_CHAT_CONTEXT`,
  khởi động lại **cả** API lẫn worker.
- **Giao diện:**
  - `FINX_API_URL` (trong `web/.env.local`) được biên dịch vào bản build, nên đổi thì phải build
    lại;
  - cuộc trò chuyện đang mở nằm trên URL (`/chat?session=<id>`);
  - API lỗi thì trang báo "Không kết nối được máy chủ", không đăng xuất người dùng.

## Cấu hình

Mẫu đầy đủ: `.env.example`.

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `FINX_HOME` | thư mục repo (checkout) | Gốc của `storage/` và `artifacts/`; chỉ đọc từ môi trường |
| `FINX_SECRET_KEY` | — (bắt buộc, ≥ 32 ký tự) | Ký cookie phiên |
| `FINX_HOST` / `FINX_PORT` | `127.0.0.1` / `8000` | Địa chỉ của API |
| `FINX_ALLOWED_ORIGINS` | `http://127.0.0.1:3000,http://localhost:3000` | Origin của giao diện được gửi request thay đổi trạng thái |
| `FINX_WEB_DB` | `storage/web/finx_web.sqlite3` | Database tài khoản |
| `FINX_SHARED_DATASET_IDS` | rỗng | Báo cáo không có owner mà mọi tài khoản được hỏi |
| `FINX_REGISTRATION` | `invite` | `invite`, `open` (chỉ khi phát triển local), `closed` |
| `FINX_REPORT_DISCOVERY_ENABLED` | `0` | Tìm báo cáo trên CafeF (form và chat) |
| `FINX_CHAT_CONTEXT` | `0` | Resolver câu hỏi tiếp theo; xem bên dưới |
| `FINX_COOKIE_SECURE` / `FINX_TRUSTED_PROXIES` | `0` / rỗng | Khi chạy sau HTTPS/reverse proxy, xem [Bảo mật](security.md) |
| `OLLAMA_*`, `LLM_REQUEST_TIMEOUT_SECONDS` | | Model chat và embedding |
| `QDRANT_URL`, `QDRANT_API_KEY` | | Qdrant server (web bắt buộc) |
| `WEB_EVIDENCE_ENABLED`, `WEB_ANALYSIS_ENABLED` | `0` | Thu thập và phân tích tin doanh nghiệp |
| `WEB_BRANCH_BUDGET_SECONDS`, `WEB_ANALYSIS_*`, `WEB_CACHE_TTL_SECONDS` | 75 s, … | Ngân sách và giới hạn của nhánh tin |
| `LLAMA_CLOUD_API_KEY`, `LLAMAPARSE_TIER`, `LLAMAPARSE_VERSION` | | Trích xuất PDF trên đám mây; version phải cố định |
| `TRIAL_DAYS`, `TRIAL_QUESTIONS_PER_DAY`, `TRIAL_QUESTIONS_TOTAL` | 14, 20, 100 | Giới hạn câu hỏi mỗi tài khoản |

Mọi thay đổi policy runtime làm đổi run fingerprint của đánh giá
(`agentfinx/core/config/runtime_policy.py`).

## Người dùng

```bash
agentfinx invites create --email ban@congty.vn [--days 3] [--base-url https://finx.example.vn]
agentfinx invites list
agentfinx invites revoke <invite-id>
```

- **Mã mời:**
  - dùng một lần, gắn với một email, mặc định hết hạn sau 7 ngày;
  - mã được in một lần, database chỉ lưu hash.
- **Giới hạn câu hỏi:**
  - mỗi câu hỏi tính quota **một lần**, lúc API nhận;
  - gửi lại cùng idempotency key hay polling không tính thêm;
  - nhóm nội bộ nới bằng ví dụ `TRIAL_DAYS=365 TRIAL_QUESTIONS_PER_DAY=500 TRIAL_QUESTIONS_TOTAL=100000`.

## Báo cáo

```bash
agentfinx ask --list-datasets
agentfinx ask --file-path storage/reports/X.md --dataset-id x --company "…" --fiscal-year 2025 \
  [--fiscal-quarter 2 --scope separate --audit-status audited] --query "…"
agentfinx ask --dataset-id x --query "…" [--debug-trace]
agentfinx ask --delete-dataset --dataset-id x [--yes]   # bỏ KB, index, manifest; giữ file nguồn
agentfinx datasets gc --dataset-id x [--apply]          # dọn bản build Qdrant cũ
```

- **Build:** chưa build, hoặc nguồn, parser hay ingestion version thay đổi thì `ask` tự build lại
  KB và index.
- **Dọn index (`gc`):** giữ bản đang dùng, bản trước đó và mọi bản dưới 48 giờ; mặc định chỉ in
  kế hoạch.

**Nhập PDF bằng CLI** (cùng service với web, chạy đồng bộ):

```bash
agentfinx reports discover --ticker VNM --year 2025 --period annual --session-id demo
agentfinx reports import --confirmed-candidate-id ID --session-id demo [--allow-cloud-parse]
agentfinx reports import --pdf /path/report.pdf --ticker VNM --year 2025 [--allow-cloud-parse]
agentfinx reports status JOB_ID
agentfinx reports budget
agentfinx reports reconcile JOB_ID | --all-pending     # đối soát chi phí theo mã job đã lưu
```

- **Đồng ý gửi đám mây:** không có `--allow-cloud-parse` thì chỉ trích xuất cục bộ. Có khóa
  LlamaParse trong môi trường không tự cấp quyền.
- **Đối soát (`reconcile`):**
  - không bao giờ gửi parse mới; chạy lại không tính phí hai lần;
  - job mất mã nhà cung cấp (`remote_id_lost`) vẫn `pending` và giữ một slot parse, hiện phải xử
    lý tay.
- **Ngân sách:**
  - mỗi import CafeF giữ tạm tối đa 200 trang × 13 = 2.600 credit, rồi điều chỉnh sau khi biết
    số trang;
  - ước tính chỉ để giữ chỗ, không bao giờ ghi thành chi phí thật.
- **Khóa LlamaParse:** `LLAMA_CLOUD_API_KEY`; các tên cũ `LLAMAPARSE_API_KEY`,
  `LLAMA_PARSE_API_KEY`, `LLAMA_API_KEY` vẫn được nhận.

**Danh mục mã CK** (cho tìm báo cáo và nhận diện doanh nghiệp trong tin):

```bash
agentfinx refresh-symbols      # storage/catalog/cafef_symbols.json; nên chạy hằng ngày
```

- **Độ mới:**
  - dưới 24 giờ: dùng thẳng;
  - 24 giờ đến 7 ngày: dùng kèm cảnh báo, worker import tự làm mới (tối đa 6 giờ/lần);
  - quá 7 ngày: không suy tên hay sàn từ danh mục.
- **An toàn khi làm mới:** không ghi đè danh mục tốt bằng kết quả rỗng hay hụt quá nửa.
- **Tên thương hiệu** (vd "Vinamilk"): chỉ nhận khi có trong
  `backend/src/agentfinx/core/config/company_aliases.json`.

## Tin tức doanh nghiệp

```bash
WEB_EVIDENCE_ENABLED=1
WEB_ANALYSIS_ENABLED=1
```

Tắt `WEB_ANALYSIS_ENABLED` thì tin chỉ đi tới agent phân tích theo `needby`, không có phần
"Tin tức doanh nghiệp".

Kiểm tra với Vietstock thật (đọc toàn văn bài, lật trang danh sách; không chạy trong CI):

```bash
cd backend && FINX_LIVE_WEB_SMOKE=1 python -m pytest tests/test_vietstock_live.py
```

## Ngữ cảnh hội thoại

`FINX_CHAT_CONTEXT=0` cho tới khi gate live đạt ([Đánh giá](evaluation.md)). Sau đó:

1. đặt `FINX_CHAT_CONTEXT=1`;
2. khởi động lại API và cả hai worker;
3. kiểm tra `/api/health` (`context_configuration_in_sync`) và vài câu mẫu.

Rollback: đặt lại `0` rồi khởi động lại; dữ liệu đã lưu vẫn giữ nguyên.

## Công cụ trong `scripts/`

| Lệnh | Việc |
|---|---|
| `python scripts/check_tracked_artifacts.py` | Báo lỗi nếu Git đang track database, upload, `.env`, `storage/`, `artifacts/` (CI cũng chạy) |
| `python scripts/run_rotate.py COUNT_FILE predictions\|scores LIMIT -- <lệnh>` | Chạy lại một lệnh resumable, xoay các `OLLAMA_API_KEY` trong `.env` khi hết quota |
| `python scripts/smoke_llamaparse.py --work-dir … --font … [--create-only \| --allow-cloud-parse]` | Thử LlamaParse thật với một PDF tổng hợp (cần extra `web,smoke`) |
| `streamlit run scripts/ab_viewer.py` | Xem các run A/B routing trong `artifacts/eval/` |
| `python scripts/tool_call_probe.py` | Kiểm tra model có trả `tool_calls` hợp lệ cho schema tool của từng agent |
