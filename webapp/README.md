# AgentFinX local web app

Giao diện web chạy **local cho nhóm**: đăng nhập, chọn báo cáo, hỏi đáp thật
và xem lại lịch sử. Không phải bản triển khai public.

Frontend lấy có chọn lọc từ branch `front` (commit `83486c8`): chỉ HTML/CSS/JS.
Database, file upload và bytecode trên branch đó **không** được đưa vào.

## Kiến trúc: ba process

| Lệnh | Vai trò |
|---|---|
| `agentfinx serve` | FastAPI phục vụ frontend và API cùng origin. **Không** chạy model, không parse, không build index. |
| `agentfinx worker --queue chat` | Xử lý câu hỏi tuần tự, mỗi lần một câu. |
| `agentfinx worker --queue import` | Import PDF tải lên: trích xuất, kiểm tra chất lượng, build index, gắn báo cáo vào cuộc trò chuyện. |

Mỗi queue chỉ có **một** worker (singleton lock). Chat và import chạy song song
với nhau; hai câu hỏi thì chạy lần lượt — một câu hard có thể mất vài phút, và
câu tiếp theo phải chờ. UI hiển thị trạng thái "đang chờ tới lượt" / "đang phân tích".

## Chạy local

```bash
pip install -e '.[web]'

# Bắt buộc, không có giá trị mặc định:
export FINX_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"

# Qdrant phải là server (Cloud hoặc local). Web từ chối QDRANT_LOCATION=':memory:'
# và đường dẫn embedded, vì ba process không dùng chung được.
export QDRANT_URL=...          # và QDRANT_API_KEY nếu là Qdrant Cloud

agentfinx serve                  # http://127.0.0.1:8000
agentfinx worker --queue chat    # terminal thứ hai
agentfinx worker --queue import  # terminal thứ ba
```

Các biến có thể đặt trong `.env` (được git ignore).

## Giao diện Next.js (`web/`)

`web/` là giao diện mới (Next.js 16, React 19), nối từ branch `namhai-web`. Nó
**chỉ là UI**: mọi request `/api/*` được Next proxy sang FastAPI ở trên, nên tài
khoản, cookie phiên, chat run, import và CafeF vẫn do FastAPI xử lý. Phần đăng
nhập riêng của branch đó (Prisma/SQLite, `AUTH_SECRET`) **không** được đưa vào.
Giao diện cũ `webapp/static` vẫn chạy ở cổng 8000 cho tới khi bản mới thay thế hẳn.

Cần Node.js ≥ 20.9. Chạy ba process ở trên, cộng thêm:

```bash
# FastAPI chỉ nhận request thay đổi trạng thái từ origin được phép:
export FINX_ALLOWED_ORIGINS=http://127.0.0.1:3000,http://localhost:3000,http://127.0.0.1:8000

cd web
npm ci
npm run dev        # http://127.0.0.1:3000 (bản build: npm run build && npm start)
```

FastAPI không ở `http://127.0.0.1:8000` thì đặt `FINX_API_URL` trong `web/.env.local`
(xem `web/.env.example`) **trước khi build**: địa chỉ này được biên dịch vào bản build
(`npm run build`/`npm run dev`); `next start` không đọc lại nó, muốn đổi phải build lại.
Câu hỏi trên báo cáo cần Ollama local cho embedding (`ollama serve`) và kết nối được tới Qdrant.

- Cuộc trò chuyện đang mở nằm trên địa chỉ (`/chat?session=<id>`): tải lại trang mở lại
  đúng cuộc trò chuyện và tiếp tục theo dõi câu hỏi/import đang chạy; nút Back/Forward chạy đúng.
- FastAPI không trả lời (lỗi 5xx, mất kết nối) thì trang chat báo "Không kết nối được máy chủ",
  **không** coi là đã đăng xuất.

- Câu trả lời Markdown render bằng `react-markdown` + `remark-gfm`, **không** bật HTML
  thô; ảnh bị bỏ; link chỉ giữ `http(s)`/`mailto`.
- Đổi tên cuộc trò chuyện: `PATCH /api/sessions/{id}`.
- Không có tính năng chia sẻ cuộc trò chuyện (backend chưa có quyền xem cho người khác).
- CI job `web`: `npm ci`, `npm run typecheck`, `npm run build`, `npm audit --audit-level=high`.
  Job `browser-smoke` build Next với `FINX_API_URL=http://127.0.0.1:8765` rồi chạy
  `tests/test_next_browser_smoke.py` (đăng ký bằng mã mời, câu trả lời không lọt sang cuộc
  trò chuyện khác, tải lại trang, máy chủ lỗi). Chạy local: build `web/` rồi chạy test;
  test tự đọc cổng API từ bản build và bỏ qua nếu chưa build.

### Mở ra ngoài localhost

Next không tự ghi địa chỉ thật của client vào `X-Forwarded-For`; header client tự gửi
được chuyển nguyên sang FastAPI. Vì vậy giới hạn đăng nhập theo IP chỉ đúng khi:

1. Có reverse proxy (nginx/Caddy) đứng trước Next và **ghi đè** header, ví dụ nginx
   `proxy_set_header X-Forwarded-For $remote_addr;`, có HTTPS (`FINX_COOKIE_SECURE=1`).
2. Next và FastAPI chỉ bind `127.0.0.1`, không mở trực tiếp ra ngoài.
3. `FINX_TRUSTED_PROXIES=127.0.0.1,::1` để FastAPI tin header do proxy đó ghi.

Thiếu (1) mà vẫn đặt (3) thì client tự chọn được IP của mình. Không đặt (3) thì mọi request
qua Next tính chung một IP: giới hạn theo tài khoản vẫn chặn dò mật khẩu, nhưng giới hạn theo IP
dùng chung cho mọi người. Đăng ký mặc định chỉ bằng mã mời (`FINX_REGISTRATION=invite`).

## Cấu hình

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `FINX_SECRET_KEY` | — (bắt buộc, ≥ 32 ký tự) | Ký cookie phiên đăng nhập. |
| `FINX_HOST` / `FINX_PORT` | `127.0.0.1` / `8000` | Địa chỉ bind. Bind ra ngoài loopback sẽ in cảnh báo. |
| `FINX_ALLOWED_ORIGINS` | suy ra từ host/port | Origin được phép gửi request thay đổi trạng thái. |
| `FINX_WEB_DB` | `dataset_store/web/finx_web.sqlite3` | Database web (user, session, lịch sử, chat run). |
| `FINX_SHARED_DATASET_IDS` | rỗng | Dataset mẫu dùng chung, phân tách bằng dấu phẩy. Chỉ áp dụng cho dataset **không có owner**. |
| `FINX_COOKIE_SECURE` | `0` | Đặt `1` khi chạy sau HTTPS. |
| `FINX_CHAT_CONTEXT` | `0` trong giai đoạn chờ gate live | Resolver có guard từ lịch sử câu hỏi hiệu lực; `0` là rollback. Chỉ bật sau nghiệm thu bên dưới. |
| `FINX_REPORT_DISCOVERY_ENABLED` | `0` | Đặt `1` để tìm báo cáo trên CafeF bằng form và trong chat (xem bên dưới). |
| `FINX_REGISTRATION` | `invite` | `invite`: chỉ đăng ký bằng mã mời gắn với đúng email; `open`: ai cũng đăng ký được (chỉ dùng khi phát triển local); `closed`: không nhận tài khoản mới. |
| `FINX_TRUSTED_PROXIES` | rỗng | IP/CIDR của proxy được tin `X-Forwarded-For` (xem "Mở ra ngoài localhost"). Rỗng: dùng IP của kết nối. |

### Mời người dùng

```bash
agentfinx invites create --email ban@congty.vn          # in mã và link /signup?invite=… (một lần)
agentfinx invites create --email ban@congty.vn --days 3 --base-url https://finx.example.vn
agentfinx invites list                                  # trạng thái; không in lại mã
agentfinx invites revoke <invite-id>
```

Mỗi mã dùng một lần, gắn với một email, mặc định hết hạn sau 7 ngày; database chỉ lưu hash
của mã. Mã mời sai, đã dùng, hết hạn hay không đúng email đều nhận cùng một thông báo, và mã
được kiểm tra trước email, nên đăng ký không cho người lạ biết email nào đã có tài khoản.

### Dataset dùng chung

Mặc định người dùng chỉ thấy báo cáo **của chính mình**. Để cho cả nhóm hỏi trên
một báo cáo mẫu đã build sẵn bằng CLI:

```bash
agentfinx ask --list-datasets                 # lấy dataset_id
export FINX_SHARED_DATASET_IDS=suavietnam
```

Dataset có owner không bao giờ được chia sẻ qua biến này.

### Giới hạn câu hỏi

Mỗi câu hỏi được tính quota **một lần**, lúc API nhận câu hỏi — gửi lặp cùng
idempotency key hay polling đều không tính thêm. Giới hạn trial mặc định
(20 câu/ngày, 100 câu tổng, 14 ngày) có thể nới cho nhóm nội bộ:

```bash
export TRIAL_DAYS=365 TRIAL_QUESTIONS_PER_DAY=500 TRIAL_QUESTIONS_TOTAL=100000
```

Mọi thay đổi policy làm đổi run fingerprint của batch, xem `config/runtime_policy.py`.

## Tải báo cáo PDF

Trên màn hình chào, **Tải báo cáo PDF của bạn**: chỉ PDF, tối đa 25 MB và 200 trang,
kèm tên công ty (bắt buộc), mã CK, năm, kỳ, phạm vi, trạng thái kiểm toán. Báo cáo tải
lên là **riêng** của tài khoản đó; không có kho dùng chung cho upload.

Trình tự: upload file → tạo cuộc trò chuyện → tạo import job → worker import xử lý →
báo cáo tự gắn vào cuộc trò chuyện đó. Tải lại trang vẫn tiếp tục theo dõi trạng thái.
Client không gửi được owner, đường dẫn, số credit hay mã job của nhà cung cấp.

**Đồng ý gửi lên đám mây** mặc định **tắt** và lưu theo từng import. Tắt thì chỉ dùng
lớp văn bản của PDF; PDF scan sẽ báo lỗi và gợi ý tải lại kèm đồng ý. Bật thì PDF có
thể được gửi tới LlamaParse (phát sinh chi phí).

UI hiển thị **hai trạng thái riêng**:

| Xử lý | Chi phí trích xuất |
|---|---|
| `queued → parsing → validating → building → ready` hoặc `failed` | `not_submitted` · `pending` · `confirmed` · `estimated_legacy` |

Báo cáo `ready` là hỏi được ngay, kể cả khi chi phí còn `pending` (nhà cung cấp chưa
báo usage). Khi đó phần dự trữ ngân sách được giữ nguyên và **lần tải tiếp theo của
tài khoản bị chặn** kèm thông báo, cho tới khi chi phí được xác nhận.

### Đối soát chi phí (admin)

```bash
agentfinx reports reconcile JOB_ID
agentfinx reports reconcile --all-pending
```

Chỉ tra cứu nhà cung cấp theo **mã job đã lưu** — không bao giờ gửi parse mới, chạy
lại nhiều lần không tính phí hai lần, và thiếu usage không bị ghi thành 0. Một lần
gửi bị ngắt trước khi kịp lưu mã job (`remote_id_lost`) vẫn ở trạng thái `pending` và
giữ một slot parse toàn cục: cần xử lý thủ công (chưa có công cụ).

Worker import khởi động lại: job dở dang được xếp hàng lại và tiếp tục **theo mã job
đã có** (không gửi lại); job mất mã chuyển `failed` với chi phí `pending`.

## Tìm báo cáo trên CafeF (`FINX_REPORT_DISCOVERY_ENABLED=1`)

Tắt (mặc định): không gọi CafeF, prompt và schema Planner giữ nguyên như CLI/batch,
upload vẫn hoạt động. Bật: người dùng tìm bằng nút **Tìm báo cáo** hoặc gõ trong chat
("tìm BCTC VNM năm 2025"). Tìm kiếm **chỉ lấy metadata**; không tải PDF hay gọi
LlamaParse cho tới khi người dùng chọn một báo cáo và bấm **Xác nhận tải và xử lý**.

**Chuẩn bị danh mục mã CK** (tên công ty, sàn niêm yết):

```bash
agentfinx-refresh-symbols      # tạo dataset_store/web/cafef_symbols.json; nên chạy hằng ngày
```

Danh mục mới dưới 24 giờ dùng trực tiếp; 24 giờ–7 ngày dùng kèm cảnh báo và worker import
tự làm mới (tối đa 6 giờ/lần); quá 7 ngày hoặc thiếu file thì không suy tên/sàn từ danh
mục — vẫn tìm được nếu người dùng nhập mã CK. Refresh có lock, ghi nguyên tử và **không
ghi đè** danh mục tốt bằng kết quả rỗng, thiếu sàn hoặc hụt quá nửa. Tên thương hiệu không
có trong tên chính thức (vd "Vinamilk") chỉ nhận khi có trong `config/company_aliases.json`
kèm nguồn đã kiểm chứng; mã CK do model đoán luôn được hỏi lại người dùng trước khi tìm.

**Sàn không còn là tham số tìm kiếm.** Đã kiểm chứng trên CafeF (09/2026): tham số `Type`
của endpoint báo cáo là loại tài liệu (`1` = BCTC cho mọi sàn), không phải sàn; code cũ
gửi `Type=2` cho mã HNX nên luôn ra rỗng. Sàn chỉ còn để hiển thị, không mặc định HOSE.

**Kết quả** được lưu thành card trong lịch sử (tải lại trang không tìm lại):

- tối đa 30 báo cáo mới nhất, báo cáo năm/quý/bán niên tách theo loại kỳ đã chọn;
- kỳ chỉ **đã xác minh** khi `Year`/`Quarter`/`Time` của CafeF có đủ, nhất quán và không
  mâu thuẫn với tiêu đề; còn lại vào nhóm **Chưa xác minh kỳ** (không bị loại, kỳ để trống);
  bán niên chỉ nhận từ tiêu đề, không suy từ Q2 hay "soát xét";
- lỗi CafeF (timeout, bị chặn, `Success=false`, dữ liệu hỏng) hiện là **lỗi**, không bao giờ
  là "không có báo cáo", kèm nút tải PDF lên;
- mỗi tài khoản chỉ một yêu cầu tìm chạy cùng lúc (form và chat dùng chung khoá; quá thì 429).

**Xác nhận**: báo cáo được chọn còn hạn 30 phút. Nếu cuộc trò chuyện hiện tại chưa có báo
cáo thì import vào chính nó; nếu đã có, **chỉ khi xác nhận** mới tạo cuộc trò chuyện mới —
tìm rồi bỏ qua không để lại cuộc trò chuyện rỗng. Ô đồng ý gửi LlamaParse mặc định tắt.
Báo cáo CafeF công khai dùng chung kết quả trích xuất giữa các tài khoản; báo cáo, quyền
truy cập và lịch sử vẫn riêng. Người dùng lại cache không trả phí lần hai, giữ phần dự trữ
tới khi phí của lần trích xuất gốc được xác nhận, nhưng **không bị chặn** import tiếp chỉ vì
lần gốc (của người khác) chưa chốt phí.

**Ngân sách**: chưa biết số trang trước khi tải, nên mỗi import CafeF giữ tối đa
200 × 13 = **2.600 credit**, điều chỉnh sau khi kiểm tra PDF. Với ngân sách 10.000 và ngưỡng
95%, khi chưa có chi tiêu khác chỉ giữ được **ba** mức dự trữ đầy đủ cùng lúc.

**Trong chat**: Planner (một lần gọi) phân loại tìm báo cáo hay hỏi đáp. Hỏi đáp dùng lại
đúng kế hoạch đó (graph bắt đầu từ router, không lập kế hoạch lần hai). Câu hỏi tài chính
trong cuộc trò chuyện chưa có báo cáo nhận card "chọn/tìm/tải báo cáo", không có câu trả lời.
Nếu một câu hỏi bị hiểu nhầm thành tìm báo cáo, card có nút **Trả lời trên báo cáo đang mở**
(không trừ thêm lượt hỏi, vẫn ghi usage).

**Nâng cấp database web** (schema v4): dừng API và cả hai worker trước. Lần khởi động đầu tự
sao lưu `finx_web.sqlite3.backup-v3-<thời điểm>` cạnh file database và từ chối nâng cấp nếu
thấy worker vừa báo nhịp tim (< 45 giây).

**Đo phân loại** (gọi LLM thật — chạy chủ động, không trong CI):

```bash
python -m scripts.eval_task_routing --questions evaluation/task_routing_contrastive.json
python -m scripts.eval_task_routing --questions queries_test.json --expect report_qa   # mục tiêu: 0 câu bị chuyển nhầm
agentfinx batch --web-entry --queries-file queries_test.json ...   # factual recall/WRONG_REFUSAL qua đường web
```

## Ngữ cảnh hội thoại: guard trước, bật sau

Resolver dùng **câu hỏi hiệu lực của các QA run hoàn tất thành công**, không dùng
riêng các câu gốc rời rạc và không dùng câu trả lời Synth. History cùng owner,
session, dataset và generation, tối đa 10 lượt/12.000 ký tự (tính cả snapshot).
Ví dụ "Vốn chủ sở hữu?" → "vào đầu năm thì sao" phải thành câu độc lập vẫn hỏi
vốn chủ sở hữu đầu năm, không chuyển sang tổng tài sản.

Lượt đầu không gọi model bổ sung (`context_status=skipped`, `context_reason=no_history`).
Khi có history, model trả schema rõ ràng trong **một invocation, không prose fallback**:

- `standalone`: giữ nguyên câu hiện tại; không ép kế thừa topic/kỳ cũ.
- `resolved`: câu hiệu lực mới có các đoạn giữ/đổi hợp lệ.
- `needs_clarification`: lưu câu hỏi làm rõ; run có thể succeeded nhưng không phải QA hoàn tất.
- `failed`: lỗi provider/schema/guard; lưu thông báo an toàn và không chạy QA bằng câu mơ hồ.

Đoạn giữ phải có trong câu hiệu lực nguồn và câu mới; đoạn đổi phải có trong câu
người dùng hiện tại và câu mới. Giữ nguyên cụm thời gian, số, dấu và đơn vị; không
bỏ dấu phân cách (`1.13` khác `113`). Guard kiểm chứng nguồn đoạn chữ, không phải
bằng chứng tuyệt đối rằng model đã hiểu đúng mọi ý định — vẫn cần đánh giá live.

Sau QA, snapshot lấy công ty/kỳ từ `planner_plan`, chỉ tiêu/kỳ/phép toán từ typed
slots của `evidence_ledger.retrieval_requirement`. Chỉ lưu trường allowlist và hash
tham chiếu requirement, không copy raw requirement có thể chứa số, facts hay câu
trả lời. Snapshot không cho phép chèn năm/ngày mới vào rewrite trái numeric guard.
Nhiều requirement không bị tự chọn mục đầu tiên. Snapshot thiếu/lỗi chỉ bị bỏ qua;
lịch sử câu hiệu lực vẫn dùng được. Không có thay đổi Synth trong đợt này.

DB **v5** chỉ thêm các cột context trên chat run và cấu hình heartbeat. Migration
không viết lại các câu cũ; effective question legacy rỗng được đọc từ question gốc
và đánh dấu legacy. Dừng API và cả hai worker trước khi migrate; cơ chế backup
SQLite nhất quán và kiểm tra heartbeat vẫn áp dụng. Không downgrade DB bằng code cũ.

API chat-run trả `context_status`, `context_reason`, `interpreted_question`. UI lưu
caption câu đã hiểu, card hỏi lại và lỗi context qua reload; context card render
bằng text, không coi nội dung model là HTML. `/api/health` công bố feature thực tế
trong heartbeat chat worker và `context_configuration_in_sync` (null khi chưa biết
hoặc worker stale). Không dùng heartbeat này làm bằng chứng provider đang hoạt động.

### Gate và rollout

**Chưa bật mặc định chỉ từ kết quả mock.** Mặc định đích sau nghiệm thu là bật cho
web; bản đang chờ gate live vẫn để `FINX_CHAT_CONTEXT=0`. CLI/batch thông thường
không gọi resolver, không đổi schema/prompt QA. Evaluator mới là đường opt-in riêng:

```bash
# Không gọi mạng/model: kiểm tra số call dự kiến.
python -m scripts.eval_chat_context --questions queries_test.json --scenarios evaluation/context_contrastive.json --dry-run

# CHỈ khi được cho phép gọi model cấu hình hiện tại. Không gửi đáp án reference/PDF.
python -m scripts.eval_chat_context --questions queries_test.json --scenarios evaluation/context_contrastive.json --allow-live --max-calls 200 --output dataset_store/evaluation/context-gate.json
```

Mỗi câu độc lập được thử ở lượt thứ hai với hai history khác topic/kỳ, không đo
trên lượt đầu bypass resolver. Gate yêu cầu 0 câu độc lập bị hỏi lại hoặc đổi nghĩa,
đồng thời tách guard rejection/schema/provider errors và các bước chuỗi bị chặn.
`--limit` chỉ là chẩn đoán, không được ghi nhận release gate. Report có fingerprint
context/model/cases; không tái sử dụng gate sau khi sửa contract/prompt/guard.

Gate trên chỉ đo resolver. Factual recall và WRONG_REFUSAL ≤ 21 cần đánh giá QA
riêng theo quy trình hiện có, cộng các lượt multi-turn qua worker; report context
không tuyên bố hai gate này pass. Cần được cho phép riêng trước các lần gọi live.

Sau khi mọi gate đạt: bật mặc định web, cấu hình `FINX_CHAT_CONTEXT=1` cho rollout,
**khởi động lại API và worker** rồi kiểm tra health/các câu mẫu. Rollback bằng
`FINX_CHAT_CONTEXT=0` và restart cả API lẫn worker; dữ liệu v5 vẫn được giữ nguyên.

## Dọn bản build Qdrant cũ

```bash
agentfinx datasets gc --dataset-id ID          # chỉ in kế hoạch
agentfinx datasets gc --dataset-id ID --apply  # xoá, giữ dataset lock
```

Giữ bản đang active, bản trước đó và mọi bản dưới 48 giờ; bản build cũ không có dấu
thời gian (tạo trước khi có tính năng này) không bao giờ bị xoá.

## Bảo mật (phạm vi local)

- Mật khẩu băm Argon2. Phiên đăng nhập là JWT trong cookie `HttpOnly`, `SameSite=Lax`;
  JavaScript không đọc được token và không có gì lưu trong `localStorage`.
- Mỗi token gắn với một phiên lưu phía server (`auth_sessions`). Đăng xuất thu hồi phiên đó:
  bản sao của cookie cũng hết hiệu lực ngay; các thiết bị khác vẫn đăng nhập.
- Giới hạn trước mọi phép tính Argon2, lưu trong database (giữ qua khi khởi động lại):
  đăng nhập sai 5 lần/15 phút cho mỗi tài khoản (kể cả email không tồn tại, để không lộ email)
  và 30 lần/15 phút cho mỗi IP; đăng ký 10 lần/giờ cho mỗi IP. Quá giới hạn trả 429 kèm
  `Retry-After`. Khoá và IP được lưu dạng hash.
- Lỗi 422 chỉ trả vị trí và loại lỗi, không lặp lại giá trị đã gửi (mật khẩu không vào log).
- Email có tên miền đặc biệt (`.invalid`, `.test`, `.example`, `.localhost`, …) không đăng ký được.
- Request thay đổi trạng thái phải có `Origin` (hoặc `Referer`) nằm trong danh sách cho phép; CORS đóng.
- Mọi session, tin nhắn và chat run đều kiểm tra owner phía server.
- API không trả đường dẫn nội bộ, tên collection hay chuỗi lỗi của provider.
- Câu trả lời AI là Markdown, được render bằng Marked rồi lọc bằng DOMPurify
  (`webapp/static/vendor/`, marked 12.0.2 và DOMPurify 3.1.6, ghim SHA-256 bên dưới).
  Tin nhắn người dùng luôn hiển thị dạng text.
- Không còn câu trả lời demo: pipeline lỗi thì trả thông báo lỗi cố định.

Chưa có: đổi/khôi phục mật khẩu, xác minh email khi đăng ký mở (vì vậy chế độ `open` chỉ dùng
local), đăng xuất mọi thiết bị.

**Nâng cấp lên schema v6** (phiên phía server, mã mời, giới hạn đăng nhập): dừng API và cả
hai worker trước; lần khởi động đầu tự sao lưu database như các lần nâng cấp trước. Token đăng
nhập cũ không có phiên phía server nên mọi người phải đăng nhập lại một lần.

**Schema v7** thêm bảng `news_updates`: phần tin doanh nghiệp chưa sẵn sàng lúc trả lời được chat
worker hoàn tất sau (giữa các câu hỏi) và thêm thành tin nhắn `news_update`. API: mỗi chat run có
`news_update` (`pending`/`done`/`expired`/`failed` kèm tin nhắn), mỗi session có
`news_update_run_id` để trang tiếp tục chờ sau khi tải lại. Cả giao diện Next và giao diện cũ tự hiện
cập nhật. Nâng cấp như các bản trước: dừng API và cả hai worker.

## Phiên bản báo cáo (generation)

Mỗi cuộc trò chuyện gắn với một báo cáo **và** phiên bản build của nó. Nếu báo cáo
được build lại, bị xoá hoặc mất artifact, cuộc trò chuyện cũ chuyển sang **chỉ đọc**:
lịch sử vẫn còn, nhưng phải tạo cuộc trò chuyện mới để hỏi tiếp. Không có chuyện
tự chuyển sang bản build mới giữa chừng.

Worker chat giữ dataset lock suốt một câu hỏi, nên build lại cùng báo cáo đó
phải chờ câu hỏi đang chạy xong.

## Khởi động lại worker

- Câu hỏi đang chờ: tiếp tục bình thường.
- Câu hỏi đang chạy dở: đánh dấu **interrupted**, không tự chạy lại (tránh gọi model
  và tính phí hai lần). Người dùng gửi lại nếu cần.

## Vendor libraries

| File | Phiên bản | SHA-256 |
|---|---|---|
| `static/vendor/marked.min.js` | marked 12.0.2 | `15fabce5b65898b32b03f5ed25e9f891a729ad4c0d6d877110a7744aa847a894` |
| `static/vendor/purify.min.js` | DOMPurify 3.1.6 | `c0845096a7c4a6741f362ac506c94c1c7d27dc603bcc1bf64a587f76f2dbe3a1` |
