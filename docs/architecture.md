# Kiến trúc

## Các process

| Process | Lệnh | Việc làm |
|---|---|---|
| API | `agentfinx serve` | FastAPI, chỉ có `/api/*`: tài khoản, phiên, cuộc trò chuyện, nhận câu hỏi và job import. **Không** chạy model, không parse, không build index. |
| Chat worker | `agentfinx worker --queue chat` | Trả lời câu hỏi lần lượt từng câu; giữa các câu hoàn tất phần tin tức còn dở. |
| Import worker | `agentfinx worker --queue import` | Tải/trích xuất PDF, kiểm tra chất lượng, build KB và index, gắn báo cáo vào cuộc trò chuyện. |
| Giao diện | `web/` (Next.js) | Chỉ là UI; mọi request `/api/*` được Next proxy sang API, nên trình duyệt chỉ thấy một origin. |
| CLI | `agentfinx ask`, `batch`, `reports`, … | Dùng cùng package và cùng `storage/`, không qua API. |

Mỗi queue chỉ có một worker (khóa singleton trong `storage/locks/`). Chat và import chạy song
song với nhau; hai câu hỏi thì chạy lần lượt, giao diện hiện "đang chờ tới lượt".

API và worker dùng chung database tài khoản (`storage/web/finx_web.sqlite3`), registry báo cáo
và một Qdrant server; vì vậy web từ chối Qdrant `:memory:` hay đường dẫn embedded.

## Package `agentfinx` (`backend/src/agentfinx`)

| Nhóm | Nội dung |
|---|---|
| `api/` | FastAPI app, đăng nhập và phiên, chat run, worker, import, tìm báo cáo trong chat, cập nhật tin sau |
| `agents/` | Planner, router/keyworder, các agent phân tích, Synth; `agents/tools/` là công cụ truy xuất |
| `workflow/` | Graph LangGraph (`graph.py`), routing, gói bằng chứng, nhánh tin (`news_node.py`, `news_branch.py`), `pipeline.py` (chạy một câu hỏi, lệnh `ask`) |
| `ingestion/` | Parser Markdown/bảng/thuyết minh, dựng fact |
| `datasets/` | Registry báo cáo, khóa, kích hoạt, dọn index cũ, migrate storage; `kb/` (SQLite), `vectorstore/` (Qdrant) |
| `acquisition/` | Tìm báo cáo trên CafeF, tải và kiểm tra PDF, LlamaParse, quota, danh mục mã CK (`symbols.py`) |
| `news/` | `collect/` lấy và cache tin Vietstock; `analysis/` phân tích tin; `service.py` là lối vào duy nhất cho phân tích; `dates.py`, `topics.py` dùng chung |
| `evaluation/` | `batch`, `predict`, `score` (RAGAS), `recall` (cổng), `metrics_report`, `triage`, `task_routing`, `chat_context`; `runner.py` chạy một câu hỏi cho batch và predict |
| `core/` | Cấu hình (`config/`, gồm `paths.py`), schema Pydantic, client LLM, tiện ích chung |
| `cli/` | Lệnh `agentfinx` duy nhất, chuyển sang `main` của từng module |

Ranh giới được kiểm bằng test (`backend/tests/test_package_boundaries.py`):

- `news/collect` không gọi model; `news/analysis` không tự lấy tin (không thư viện mạng, không
  import `news/collect`; chỉ đọc danh mục mã CK cục bộ);
- `evaluation/predict` và `evaluation/batch` không import lẫn nhau, cùng dùng `runner.py`;
- code chỉ import `agentfinx.*`; không còn module ở thư mục gốc.

Chữ "web" trong tên khóa state (`web_bundle`, `web_analysis`, `need_web`, `web_intent`) là tên
lịch sử của phần tin tức; được giữ vì đã nằm trong database chat, kết quả lưu và bộ đánh giá.

## Luồng trả lời một câu hỏi

```text
                       ┌─ start_web ── (luồng riêng: phân tích tin) ───────────────┐
Planner → Router → build_evidence ─┤                                                    ├→ join_web → Synth
                       └─ agent BCTC → tool → … → collect_analysis ─────────────────┘
(agent_web chạy trước agent BCTC chỉ khi một agent BCTC cần đọc tin)
```

- **Planner** phân loại câu hỏi (`task_kind`: hỏi báo cáo hay tìm báo cáo; `need_web`,
  `web_intent`) và lập kế hoạch. Trong chat có bật tìm báo cáo, planner web gọi một lần; hỏi đáp
  dùng lại đúng kế hoạch đó (graph bắt đầu từ router).
- **Router** chọn agent phân tích và yêu cầu truy xuất; **build_evidence** dựng gói bằng chứng từ
  SQLite (fact có kiểu) và Qdrant.
- **Agent phân tích** gọi tool truy xuất; **Synth** tổng hợp đúng một câu trả lời Markdown, không
  nhãn vận chuyển kiểu `=== FINAL ANSWER ===`.
- **Hợp đồng runtime:**
  - thuyết minh lấy tối đa `NOTE_FACTS_LIMIT=12` fact (24 cho câu dạng danh sách);
  - báo cáo chính lấy 10 (16 cho danh sách);
  - mọi dữ liệu do người dùng, tool hay web cung cấp nằm trong human message, đánh dấu
    untrusted;
  - cache truy xuất có khóa gồm dataset, câu hỏi và generation của index;
  - handle `collection` không bao giờ được serialize ra trace hay report.
- **Trace** mặc định chỉ có metadata vận hành (trạng thái, số lượng, thời gian, token; tối đa
  4 KiB/event). `--debug-trace` cho payload chi tiết (tối đa 32 KiB/event); secret luôn bị che.

## Báo cáo và dataset

- **Ingestion:** Markdown → bảng/thuyết minh → fact có kiểu trong SQLite → document trong Qdrant.
  KB và index build ở bản staging rồi mới kích hoạt; build lỗi không phá bản đang dùng.
- **Danh tính:**
  - KB được tái sử dụng theo **nội dung** nguồn (SHA-256), phiên bản parser và schema, không
    theo đường dẫn file;
  - đổi parser phải tăng `PARSER_CONTRACT_VERSION` (digest các module parser được ghim bằng test).
- **Generation:** mỗi cuộc trò chuyện gắn với một báo cáo **và** phiên bản build của nó. Nếu báo
  cáo bị build lại, bị xóa hay mất artifact, cuộc trò chuyện cũ chuyển sang **chỉ đọc** (lịch sử
  còn, phải mở cuộc trò chuyện mới để hỏi tiếp). Worker chat giữ dataset lock suốt một câu hỏi.
- **Quyền:**
  - người dùng chỉ thấy báo cáo của mình;
  - báo cáo không có owner có thể được chia sẻ cho mọi tài khoản qua `FINX_SHARED_DATASET_IDS`;
  - báo cáo có owner thì không bao giờ được chia sẻ.
- **`agentfinx ask` chọn dataset theo thứ tự:**
  1. `--file-path` (tạo hoặc cập nhật);
  2. `--dataset-id`;
  3. bộ lọc `--company`/`--ticker`/`--fiscal-year`… (hỏi chọn nếu nhiều kết quả);
  4. báo cáo mẫu mặc định.

## Tin tức doanh nghiệp (`WEB_EVIDENCE_ENABLED`, `WEB_ANALYSIS_ENABLED`)

Khi câu hỏi có `need_web=true`, `web_intent=company_news`:

- **Song song:** nhánh tin chạy ngoài các bước của LangGraph (`news_branch.py`) để không chặn
  vòng gọi tool của agent BCTC; `join_web` chờ nó ngay trước Synth. Chỉ khi một agent BCTC cần
  đọc tin (câu hỏi gắn tin với số liệu) thì nhánh tin chạy trước.
- **Một ngân sách thời gian:** cho cả nhánh (`WEB_BRANCH_BUDGET_SECONDS`, mặc định 75 giây).
  Lấy tin dùng trước, phân tích nhận phần còn lại (tối đa `WEB_ANALYSIS_BUDGET_SECONDS`). Hết
  giờ thì câu trả lời BCTC vẫn đi, phần tin ghi "đang cập nhật".
- **Tự bổ sung:**
  - tin chưa xong khi trả lời thì để lại một dòng `news_updates`;
  - chat worker hoàn tất nó giữa các câu hỏi và thêm tin nhắn "Cập nhật tin tức";
  - giao diện tự hiện tin nhắn đó, kể cả sau khi tải lại trang;
  - sau 10 phút không có tin thì báo rồi dừng.
- **Nguồn:**
  - Vietstock: danh sách tin JSON (`POST /data/GetNews`) rồi từng trang bài;
  - mọi URL và redirect phải thuộc vietstock.vn, có giới hạn dung lượng, timeout từng request
    và deadline;
  - danh sách chỉ coi là đọc hết khi đạt `TotalRow`; "không có tin" chỉ kết luận khi đã đọc hết
    khoảng hỏi.
- **Cache dùng chung:**
  - chỉ chứa trường lấy từ nguồn;
  - câu hỏi và tên công ty người hỏi nhập không bao giờ vào cache;
  - kết quả phân tích theo câu hỏi chỉ sống trong lần chạy.
- **Doanh nghiệp và thời gian:**
  - mã CK lấy từ báo cáo đang mở; câu hỏi nêu doanh nghiệp khác thì yêu cầu mở báo cáo đó;
  - chỉ đọc mốc thời gian trong phần câu hỏi nói về tin; mặc định 30 ngày tới `as_of`;
  - khoảng đảo ngược, ở tương lai hay không đọc được thì hỏi lại; ngày theo giờ Việt Nam.
- **Phân tích (`news/service.py` → `news/analysis`):**
  - hai lượt model theo lô: chọn bài và gom sự kiện, rồi bằng chứng;
  - mỗi lượt thử lại một lần, tối đa 4 lần gọi.
- **Kiểm chứng nhận định:**
  - trích dẫn phải khớp nguyên văn với bài thuộc đúng sự kiện;
  - tiêu đề và câu giải thích phải được trích dẫn chứng minh, không thì bị thay bằng tiêu đề bài
    gốc hoặc bỏ;
  - mức xác nhận do code quyết theo nguồn: `official_disclosure`, `attributed_report`,
    `forecast`, `unconfirmed`, `unattributed`.
- **Ai nhận tin:**
  - Synth nhận dữ liệu có cấu trúc (sự kiện, ngày, chỉ tiêu liên quan, trích dẫn và nguồn, mức
    xác nhận, điều chưa chắc, giới hạn thu thập), không URL, không toàn văn;
  - agent phân tích chỉ nhận thêm khi được router ghi trong `needby` **và** câu hỏi gắn tin với
    số liệu;
  - tin không bao giờ là toán hạng hay căn cứ BCTC.
- **Trạng thái:** `ok`, `partial`, `pending`, `insufficient_evidence`, `needs_clarification`,
  `unsupported`, `error`. Lỗi model hay lỗi nguồn không bao giờ được báo thành "không có tin".

## Nhập báo cáo PDF

Hai đầu vào: tải PDF lên, hoặc tìm trên CafeF rồi người dùng xác nhận một ứng viên.

- **Tìm (`FINX_REPORT_DISCOVERY_ENABLED=1`):**
  - chỉ lấy metadata; không tải hay trích xuất gì trước khi người dùng xác nhận;
  - kết quả lưu thành thẻ trong lịch sử (tải lại trang không tìm lại);
  - kỳ báo cáo chỉ "đã xác minh" khi `Year`/`Quarter`/`Time` của CafeF nhất quán với tiêu đề;
  - lỗi CafeF là lỗi, không bao giờ là "không có báo cáo";
  - tham số `Type` của CafeF là loại tài liệu (`1` = BCTC), không phải sàn.
- **Tải và kiểm tra:**
  - allowlist host và redirect, MIME, `%PDF-`, SHA-256;
  - tối đa 25 MB và 200 trang;
  - API không nhận URL tùy ý.
- **Trích xuất:**
  - lớp văn bản được kiểm tra cục bộ trước;
  - trang chất lượng kém mới gửi LlamaParse, và chỉ khi người dùng **đồng ý** (mặc định tắt);
  - phiên bản LlamaParse phải cố định;
  - cache theo SHA + cấu hình; upload riêng gửi `disable_cache`.
- **Job:**
  - đi qua `queued → downloading → parsing → validating → building → ready|failed`;
  - trạng thái xử lý và trạng thái chi phí (`not_submitted`/`pending`/`confirmed`) tách riêng:
    báo cáo `ready` hỏi được ngay dù chi phí còn `pending`, khi đó lần tải tiếp của tài khoản bị
    chặn tới khi chi phí được xác nhận;
  - quota được kiểm, giữ và xếp hàng trong một transaction;
  - báo cáo chỉ gắn vào cuộc trò chuyện sau trạng thái `ready`.
- **Quyền sở hữu:** báo cáo tải lên là riêng của tài khoản; báo cáo CafeF công khai dùng chung
  kết quả trích xuất, nhưng báo cáo, quyền và lịch sử vẫn riêng.

## Ngữ cảnh hội thoại (`FINX_CHAT_CONTEXT`)

Resolver viết lại câu hỏi tiếp theo thành câu độc lập ("vào đầu năm thì sao" → "Vốn chủ sở hữu
đầu năm là bao nhiêu?").

- **Đầu vào:** chỉ dùng **câu hỏi hiệu lực** của các lượt QA thành công (cùng owner, cuộc trò
  chuyện, báo cáo và generation; tối đa 10 lượt), không dùng câu trả lời.
- **Kết quả**, trả theo schema trong một lần gọi model:
  - `standalone`;
  - `resolved`;
  - `needs_clarification` (lưu câu hỏi làm rõ);
  - `failed` (không chạy QA bằng câu mơ hồ).
- **Guard:**
  - đoạn được giữ phải có trong câu nguồn;
  - đoạn được đổi phải có trong câu người dùng vừa nhập;
  - số, dấu và đơn vị giữ nguyên.
- **Snapshot sau QA:** chỉ lưu trường allowlist (công ty, kỳ, chỉ tiêu, phép toán) và hash tham
  chiếu, không lưu số liệu hay câu trả lời.
- **Trạng thái:** mặc định tắt cho tới khi gate live đạt, xem [Đánh giá](evaluation.md) và
  [Vận hành](operations.md).

## Lưu trữ

Mọi dữ liệu ghi ra nằm dưới `FINX_HOME`:

- `storage/`: dữ liệu người dùng cần, gồm tài khoản, báo cáo đã build, import, cache tin, danh
  mục mã CK;
- `artifacts/`: kết quả chạy, gồm báo cáo đánh giá và log.

Registry, manifest và đường dẫn file của job import lưu tương đối với `FINX_HOME`, nên có thể
chuyển cả thư mục.
Chi tiết, cách migrate và sao lưu: [Vận hành](operations.md).
