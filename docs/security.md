# Bảo mật

Phạm vi: chạy **local cho nhóm**, không phải bản triển khai public.

## Đăng nhập và phiên

- **Mật khẩu:** băm bằng Argon2.
- **Phiên:**
  - JWT trong cookie `HttpOnly`, `SameSite=Lax`; JavaScript không đọc được, không có gì nằm
    trong `localStorage`;
  - mỗi token gắn với một phiên lưu phía server (`auth_sessions`);
  - đăng xuất thu hồi phiên đó ngay, kể cả bản sao cookie.
- **Đăng ký:**
  - mặc định chỉ bằng mã mời (`FINX_REGISTRATION=invite`), gắn với đúng email, chỉ lưu hash;
  - mã sai, đã dùng, hết hạn hay không đúng email đều nhận cùng một thông báo, và mã được kiểm
    trước email, nên đăng ký không làm lộ email nào đã có tài khoản;
  - tên miền đặc biệt (`.invalid`, `.test`, `.example`, `.localhost`…) bị từ chối.
- **Giới hạn**, kiểm trước mọi phép tính Argon2 và lưu trong database (còn sau khi khởi động
  lại):
  - đăng nhập sai 5 lần/15 phút mỗi tài khoản (kể cả email không tồn tại);
  - 30 lần/15 phút mỗi IP;
  - đăng ký 10 lần/giờ mỗi IP;
  - quá giới hạn trả 429 kèm `Retry-After`; khóa và IP được lưu dạng hash.
- **Lỗi 422:** chỉ trả vị trí và loại lỗi, không lặp lại giá trị đã gửi (mật khẩu không vào log).
- **Chưa có:** đổi hay khôi phục mật khẩu, xác minh email ở chế độ `open` (vì vậy `open` chỉ
  dùng khi phát triển local), đăng xuất mọi thiết bị.

## API

- **Origin:** request thay đổi trạng thái phải có `Origin` (hoặc `Referer`) trong
  `FINX_ALLOWED_ORIGINS` (mặc định là origin của giao diện Next); CORS đóng.
- **Quyền sở hữu:** mọi cuộc trò chuyện, tin nhắn, chat run, import và báo cáo đều kiểm owner
  phía server. Owner luôn lấy từ phiên, không bao giờ từ dữ liệu client gửi lên; client không
  đặt được owner, đường dẫn, số credit hay mã job của nhà cung cấp.
- **Dữ liệu trả ra:** API không trả đường dẫn nội bộ, tên collection hay chuỗi lỗi thô của nhà
  cung cấp.
- **Hiển thị trong giao diện:**
  - câu trả lời AI render bằng `react-markdown` + `remark-gfm`: HTML thô bị bỏ, không render
    ảnh, link chỉ giữ `http(s)`/`mailto`;
  - tin nhắn người dùng và thẻ ngữ cảnh hiển thị dạng text.
- **Nhập báo cáo:**
  - API không nhận URL tùy ý;
  - tải về phải qua allowlist host và redirect, kiểm MIME, `%PDF-`, SHA-256 và giới hạn kích
    thước/số trang;
  - Giao diện web luôn cho phép trích xuất đám mây: PDF tải lên hoặc nhập từ CafeF mà không đọc được
    lớp văn bản sẽ được gửi lên LlamaParse (phát sinh chi phí). API vẫn nhận `allow_cloud_parse`; CLI
    chỉ gửi khi có `--allow-cloud-parse`.
- **Tin tức:** mọi URL và redirect của Vietstock phải thuộc vietstock.vn. Cache tin dùng chung
  không lưu câu hỏi hay tên công ty người dùng nhập.
- **Trace và log:** không chứa câu hỏi, fact hay câu trả lời ở chế độ mặc định; credential luôn
  bị che.

## Mở ra ngoài localhost

Next không tự ghi địa chỉ thật của client vào `X-Forwarded-For`; header client tự gửi được
chuyển nguyên sang API. Giới hạn theo IP chỉ đúng khi có đủ ba điều kiện:

1. Một reverse proxy (nginx, Caddy) đứng trước Next và **ghi đè** header, ví dụ nginx
   `proxy_set_header X-Forwarded-For $remote_addr;`, kèm HTTPS và `FINX_COOKIE_SECURE=1`.
2. Next và API chỉ bind `127.0.0.1`.
3. `FINX_TRUSTED_PROXIES=127.0.0.1,::1`, để API tin header do proxy đó ghi.

Đặt (3) mà thiếu (1) thì client tự chọn được IP của mình. Không đặt (3) thì mọi request qua Next
tính chung một IP: giới hạn theo tài khoản vẫn chặn dò mật khẩu, nhưng giới hạn theo IP dùng
chung cho mọi người. `agentfinx serve` in cảnh báo khi bind ra ngoài loopback, và cảnh báo thêm
nếu đăng ký đang để `open`.

## Secret và dữ liệu không được commit

- **Secret** (`FINX_SECRET_KEY`, `OLLAMA_API_KEY`, `QDRANT_API_KEY`, `LLAMA_CLOUD_API_KEY`,
  `ANTHROPIC_API_KEY`):
  - chỉ nằm trong `FINX_HOME/.env` hay môi trường; `.env` bị Git bỏ qua;
  - CLI và log không bao giờ in giá trị;
  - một khóa từng lộ phải được rotate ở nhà cung cấp; xóa khỏi file là chưa đủ.
- **Không bao giờ track:**
  - `storage/` (tài khoản, báo cáo, PDF, cache) và `artifacts/` (kết quả chạy);
  - mọi `*.db`, `*.sqlite*`, `.env*`, PDF;
  - dữ liệu cũ `dataset_store/`, `data/`.
- **Kiểm tra trong CI**, chỉ để phát hiện chứ không ngăn được (file đã push là đã công khai):
  - `scripts/check_tracked_artifacts.py` soát những gì Git đang track;
  - `detect-secrets` quét toàn repo.
- **Không upload qua giao diện web của GitHub:** cách đó bỏ qua mọi kiểm tra cục bộ.
- **Benchmark:** `backend/benchmarks/` chỉ được chứa báo cáo công khai và câu hỏi đã duyệt.
  Báo cáo riêng của người dùng chỉ nằm trong `storage/`.
