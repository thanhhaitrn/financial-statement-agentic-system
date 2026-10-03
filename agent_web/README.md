# agent_web

Agent độc lập: nhận câu hỏi về một doanh nghiệp Việt Nam → tìm tin liên
quan → lọc trùng/relevance → đánh giá sentiment theo từng **sự kiện** →
trả kết quả có nguồn (evidence-backed). Xem đầy đủ spec gốc tại file
requirement đính kèm.

Module này **độc lập**, chưa tích hợp vào workflow phân tích BCTC.

## Cấu trúc project

```
agent_web/
  agent_web/
    __init__.py          # export run_agent_web, WebAnalysisRequest, WebAnalysisResult
    models.py             # Pydantic schema input/output (mục 3, 5, 6, 7)
    interfaces.py          # Protocol: SearchProvider, ArticleFetcher, ModelClient, WebAgentServices
    config.py               # AgentWebConfig — mọi tham số vận hành (mục 8)
    pipeline.py              # run_agent_web — orchestration chính (mục 4)
    dedup.py                  # loại trùng lớp 1 (URL/content)
    aggregate.py               # công thức điểm tổng hợp (mục 6), có version
    render.py                   # render Markdown để demo
    cli.py                        # CLI demo, xuất JSON/Markdown
    prompts/                       # system prompt cho từng bước model quyết định
    providers/
      mock_provider.py             # provider giả cho unit test (không cần network)
      vietstock_provider.py         # ADAPTER STUB — nối vào crawler Vietstock hiện có
    clients/
      anthropic_client.py           # ModelClient dùng Anthropic API thật
      mock_model_client.py           # ModelClient giả cho unit test
  tests/
    fixtures/sample_articles.py      # fixture tin tài chính tiếng Việt mẫu
    test_dedup.py, test_aggregate.py, test_pipeline_mock.py, test_needs_clarification.py
  requirements.txt
  pytest.ini
  .env.example
```

### Phân chia trách nhiệm (đúng mục 4)

| Thành phần | Vai trò |
|---|---|
| `ModelClient` (LLM) | Quyết định query, relevance, nhóm sự kiện, sentiment |
| `SearchProvider` / `ArticleFetcher` | Thực hiện search/fetch thô |
| `pipeline.py` (code) | Validate schema, giới hạn tài nguyên, provenance, tính điểm tổng hợp |

Không có danh sách từ khóa tích cực/tiêu cực hardcode nào — sentiment do
model chấm, code chỉ ép điểm số khớp với nhãn (mục 6) và validate schema.

## Cài đặt

```bash
cd agent_web
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # rồi điền ANTHROPIC_API_KEY
```

Mở project bằng VS Code (`.vscode/settings.json` đã trỏ interpreter về
`.venv`), chọn interpreter đó nếu VS Code chưa tự nhận.

## Biến môi trường

Xem `.env.example`. Bắt buộc: `ANTHROPIC_API_KEY` (chỉ cần khi gọi model
thật — unit test dùng mock, không cần key). Các tham số còn lại có default
hợp lý trong `agent_web/config.py`, có thể override qua env hoặc truyền
thẳng `AgentWebConfig(...)`.

## Chạy test

```bash
pytest
```

Toàn bộ test hiện có chạy bằng fixture/mock (`MockSearchProvider`,
`MockFetcher`, `MockModelClient`) — **không cần API key, không cần
network**. Live test gọi Anthropic thật chưa có sẵn trong repo này (theo
mục 9, live test phải tách riêng và chỉ chạy khi bật rõ ràng) — khi cần,
thêm file `tests/live/test_live_*.py`, skip mặc định bằng
`@pytest.mark.skipif(not os.environ.get("AGENT_WEB_RUN_LIVE"), ...)`.

## Chạy demo CLI

```bash
python -m agent_web.cli \
  --question "Các tin trong tháng 8 ảnh hưởng tích cực hay tiêu cực đến FPT?" \
  --ticker FPT --company-name "Công ty Cổ phần FPT" \
  --date-from 2026-08-01 --date-to 2026-08-31 \
  --fixtures tests/fixtures/sample_articles.py \
  --format markdown
```

Đổi `--format json` để lấy structured result đầy đủ (output chính theo
mục 7 — Markdown chỉ để đọc nhanh khi demo).

CLI hiện dùng `MockSearchProvider`/`MockFetcher` với fixture tĩnh mặc định.
Để chạy với **3 crawler thật** (Cafef BCTC + Cafef danh mục mã CK +
Vietstock news — xem `ARCHITECTURE.md`), dùng:

```python
from agent_web.providers.composite_provider import build_real_services_providers

search_provider, fetcher = build_real_services_providers(headless=True)
```

rồi truyền `search_provider`/`fetcher` này vào `WebAgentServices` thay cho
mock. Yêu cầu thêm: `pip install selenium` + Chrome/chromedriver cài sẵn
trên máy chạy (chỉ cần cho phần Vietstock news; phần Cafef chỉ cần
`requests`, không cần Selenium). `ModelClient` (Anthropic) là thật, nên cần
`ANTHROPIC_API_KEY` để chạy CLI.

Xem **`ARCHITECTURE.md`** để biết sơ đồ luồng dữ liệu đầy đủ và giải thích
vì sao "agent" (phần ra quyết định ngữ nghĩa) nằm ở `pipeline.py` + LLM,
tách biệt khỏi 3 hàm crawl thuần trong `providers/real_crawlers.py`.

## Việc còn thiếu / cần bạn cung cấp

1. ~~Code crawler Vietstock~~ — **đã nhận và tích hợp** trong
   `providers/real_crawlers.py` (`crawl_vietstock_news`,
   `crawl_vietstock_article`, `crawl_financial_reports`,
   `crawl_stock_directory`) + adapter thật trong `vietstock_provider.py`,
   `cafef_reports_provider.py`, `composite_provider.py`.
2. **Live test** gọi Anthropic API thật (chưa thêm, theo mục 9 phải tách
   riêng khỏi unit test) và **live test crawl thật** (selenium cần Chrome
   thật, chưa chạy được trong sandbox phát triển — xem giới hạn ở
   `ARCHITECTURE.md` mục 4).
3. Rubric sentiment hiện là bản mô tả trong prompt (`prompts/templates.py`)
   — nếu cần rubric chi tiết hơn (ví dụ theo ngành), cập nhật ở đó và tăng
   `RUBRIC_VERSION` trong `aggregate.py`.

## Giới hạn đã biết

- `event_date` trong `Evidence` hiện luôn `None` — model chưa được yêu cầu
  trích xuất ngày sự kiện riêng biệt với ngày xuất bản; có thể bổ sung
  bước này vào `EVENT_GROUPING_SYSTEM` nếu cần.
- Việc lọc theo `date_from/date_to` hiện áp dụng ở tầng search provider
  (provider tự quyết định cách lọc); việc loại bài sau `as_of` được đảm
  bảo ở tầng `pipeline.py` bất kể provider có lọc hay không.
- Retry hiện là backoff tuyến tính đơn giản (0.5s * lần thử); chưa phân
  biệt lỗi 429 (rate limit) với lỗi timeout — có thể tinh chỉnh trong
  `pipeline.py::_collect_evidence._fetch_one` nếu provider thật cần xử lý
  khác nhau.
- Chưa có cơ chế cache evidence giữa các lần gọi (mỗi lần gọi
  `run_agent_web` crawl lại từ đầu).
