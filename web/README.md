Hướng dẫn chạy dự án
agentfinx/
├── main.py           ← FastAPI backend (chạy ở đây)
├── requirements.txt
├── finx.db           ← SQLite tự tạo khi chạy lần đầu
├── uploads/          ← Thư mục lưu file upload, tự tạo
│
├── index.html        ← Trang đăng nhập / đăng ký
├── style.css
├── chatbox.html      ← Trang chat chính
├── chatbox.js
└── chatstyle.css
```

---

## 1. Cài Python dependencies

```bash
pip install -r requirements.txt
```

---

## 2. Chạy backend

```bash
uvicorn main:app --reload --port 8000
```

Backend sẽ chạy tại: **http://localhost:8000**

Kiểm tra API docs (Swagger): **http://localhost:8000/docs**

---

## 3. Mở frontend

Mở `index.html` bằng trình duyệt (double-click hoặc dùng Live Server trong VS Code).

> **Lưu ý CORS**: Nếu mở file HTML trực tiếp (`file://`) thì CORS có thể bị chặn.  
> Dùng Live Server của VS Code hoặc chạy thêm:
> ```bash
> python -m http.server 3000
> ```
> rồi truy cập `http://localhost:3000`

---

## 4. Tài khoản demo có sẵn

| Email | Mật khẩu |
|-------|----------|
| demo@ftu.vn | 123456 |

Tài khoản này được tạo tự động qua API `/api/register` — hoặc tự đăng ký tài khoản mới.

---

## 5. API Endpoints chính

| Method | Endpoint | Mô tả |
|--------|----------|-------|
| POST | `/api/register` | Đăng ký tài khoản |
| POST | `/api/login` | Đăng nhập, nhận JWT token |
| GET | `/api/me` | Thông tin user hiện tại |
| GET | `/api/sessions` | Danh sách lịch sử chat |
| POST | `/api/sessions` | Tạo session mới |
| GET | `/api/sessions/{id}/messages` | Tin nhắn của 1 session |
| DELETE | `/api/sessions/{id}` | Xoá session |
| POST | `/api/chat` | Gửi tin nhắn, nhận AI reply |
| POST | `/api/upload` | Upload file PDF/Excel/CSV |
| GET | `/api/files` | Danh sách file đã upload |

Tất cả endpoint (trừ `/api/login` và `/api/register`) yêu cầu header:
```
Authorization: Bearer <token>
```

---

## 6. Nối AI Agent thật (của nhóm)

Trong `main.py`, tìm hàm `generate_ai_reply()` và thay phần placeholder bằng logic thật:

```python
def generate_ai_reply(question: str, context_file: Optional[str] = None) -> str:
    # TODO: Gọi AI Agent của nhóm ở đây
    # Ví dụ: dùng LangChain, OpenAI, Gemini, v.v.
    # context_file là đường dẫn tới file đã upload (nếu user có đính kèm)
    
    file_path = UPLOAD_DIR / context_file if context_file else None
    result = your_agent.run(question=question, file=file_path)
    return result
```

---

## 7. Database

Dữ liệu được lưu trong file `finx.db` (SQLite), bao gồm:
- **users** — tài khoản người dùng (mật khẩu được hash bcrypt)
- **sessions** — lịch sử các cuộc hội thoại
- **messages** — từng tin nhắn trong mỗi session
- **uploaded_files** — metadata file đã upload

File vật lý được lưu trong thư mục `uploads/`.

---

## 8. Deploy production (gợi ý)

- Backend: Render, Railway, hoặc VPS với `uvicorn main:app --host 0.0.0.0 --port 8000`
- Đổi `SECRET_KEY` trong `main.py` thành chuỗi ngẫu nhiên dài
- Đổi `API` trong `index.html` và `chatbox.js` thành URL server thật
- Đổi CORS `allow_origins` thành domain cụ thể của frontend
