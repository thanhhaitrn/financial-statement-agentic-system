"""
AgentFinX FastAPI backend.

Run locally from ``web/backend``:
    uvicorn main:app --reload --port 8000

Production notes:
    - Set FINX_SECRET_KEY to a long random value.
    - Set FINX_CORS_ORIGINS to comma-separated frontend origins.
    - Set FINX_DB_PATH and FINX_UPLOAD_DIR for persistent storage.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr, Field


logger = logging.getLogger("agentfinx.api")

BASE_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = BASE_DIR.parent


# =============================================================================
# Settings
# =============================================================================
@dataclass(frozen=True)
class Settings:
    app_name: str = "AgentFinX API"
    version: str = "1.0.0"
    environment: str = "development"
    secret_key: str = "finx-dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    token_expire_hours: int = 72
    db_path: Path = BASE_DIR / "finx.db"
    upload_dir: Path = BASE_DIR / "uploads"
    frontend_dir: Path = FRONTEND_DIR
    allowed_upload_exts: frozenset[str] = frozenset({".pdf", ".xlsx", ".csv"})
    max_upload_bytes: int = 25 * 1024 * 1024
    cors_origins: tuple[str, ...] = (
        "http://localhost:3000",
        "http://localhost:5500",
        "http://localhost:8000",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5500",
        "http://127.0.0.1:8000",
        "null",
    )


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def load_settings() -> Settings:
    cors_origins = _split_csv(os.getenv("FINX_CORS_ORIGINS", ""))
    allowed_exts = _split_csv(os.getenv("FINX_ALLOWED_UPLOAD_EXTS", ""))

    return Settings(
        environment=os.getenv("FINX_ENV", Settings.environment),
        secret_key=os.getenv("FINX_SECRET_KEY", Settings.secret_key),
        token_expire_hours=int(os.getenv("FINX_TOKEN_EXPIRE_HOURS", Settings.token_expire_hours)),
        db_path=Path(os.getenv("FINX_DB_PATH", str(Settings.db_path))),
        upload_dir=Path(os.getenv("FINX_UPLOAD_DIR", str(Settings.upload_dir))),
        frontend_dir=Path(os.getenv("FINX_FRONTEND_DIR", str(Settings.frontend_dir))),
        max_upload_bytes=int(os.getenv("FINX_MAX_UPLOAD_BYTES", Settings.max_upload_bytes)),
        cors_origins=cors_origins or Settings.cors_origins,
        allowed_upload_exts=frozenset(allowed_exts or Settings.allowed_upload_exts),
    )


settings = load_settings()
if settings.environment == "production" and settings.secret_key == Settings.secret_key:
    raise RuntimeError("FINX_SECRET_KEY must be set in production")

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


# =============================================================================
# Database
# =============================================================================
def get_db() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(settings.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def _connect_db() -> sqlite3.Connection:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with _connect_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id         TEXT PRIMARY KEY,
                name       TEXT NOT NULL,
                email      TEXT UNIQUE NOT NULL,
                password   TEXT NOT NULL,
                avatar     TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id         TEXT PRIMARY KEY,
                user_id    TEXT NOT NULL,
                title      TEXT DEFAULT 'Cuộc trò chuyện mới',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS messages (
                id         TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                role       TEXT NOT NULL CHECK(role IN ('user', 'ai')),
                content    TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS uploaded_files (
                id          TEXT PRIMARY KEY,
                user_id     TEXT NOT NULL,
                session_id  TEXT,
                filename    TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                size        INTEGER,
                uploaded_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE SET NULL
            );
            """
        )
        conn.commit()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


# =============================================================================
# Schemas
# =============================================================================
class RegisterBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    password: str = Field(min_length=6, max_length=128)


class LoginBody(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class ChatBody(BaseModel):
    session_id: Optional[str] = None
    message: str = Field(min_length=1, max_length=20_000)
    file_id: Optional[str] = None


class UserOut(BaseModel):
    id: str
    name: str
    email: EmailStr
    avatar: Optional[str] = None
    created_at: Optional[str] = None


class AuthResponse(BaseModel):
    token: str
    user: UserOut


class SessionOut(BaseModel):
    id: str
    user_id: Optional[str] = None
    title: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class MessageOut(BaseModel):
    id: str
    session_id: str
    role: str
    content: str
    created_at: Optional[str] = None


class ChatResponse(BaseModel):
    session_id: str
    reply: str


class UploadResponse(BaseModel):
    file_id: str
    filename: str
    size: int
    message: str


class FileOut(BaseModel):
    id: str
    filename: str
    size: Optional[int] = None
    uploaded_at: Optional[str] = None
    session_id: Optional[str] = None


# =============================================================================
# Repository helpers
# =============================================================================
def get_user_by_email(db: sqlite3.Connection, email: str) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM users WHERE email = ?", (email.lower(),)).fetchone()


def get_user_by_id(db: sqlite3.Connection, user_id: str) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_owned_session(db: sqlite3.Connection, session_id: str, user_id: str) -> sqlite3.Row | None:
    return db.execute(
        "SELECT * FROM sessions WHERE id = ? AND user_id = ?",
        (session_id, user_id),
    ).fetchone()


def get_owned_file(db: sqlite3.Connection, file_id: str, user_id: str) -> sqlite3.Row | None:
    return db.execute(
        "SELECT * FROM uploaded_files WHERE id = ? AND user_id = ?",
        (file_id, user_id),
    ).fetchone()


def create_user(db: sqlite3.Connection, name: str, email: str, password: str) -> dict[str, Any]:
    user_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO users(id, name, email, password) VALUES (?, ?, ?, ?)",
        (user_id, name.strip(), email.lower(), hash_password(password)),
    )
    db.commit()
    return {"id": user_id, "name": name.strip(), "email": email.lower(), "avatar": None}


def create_chat_session(db: sqlite3.Connection, user_id: str, title: str) -> str:
    session_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO sessions(id, user_id, title) VALUES (?, ?, ?)",
        (session_id, user_id, title),
    )
    return session_id


def add_message(db: sqlite3.Connection, session_id: str, role: str, content: str) -> None:
    db.execute(
        "INSERT INTO messages(id, session_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), session_id, role, content, utc_now_iso()),
    )


# =============================================================================
# Auth service
# =============================================================================
def hash_password(password: str) -> str:
    return pwd_ctx.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_ctx.verify(plain, hashed)


def create_token(user_id: str) -> str:
    exp = datetime.now(timezone.utc) + timedelta(hours=settings.token_expire_hours)
    return jwt.encode({"sub": user_id, "exp": exp}, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> str:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token không hợp lệ hoặc đã hết hạn",
        ) from exc

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token thiếu thông tin người dùng")
    return str(user_id)


def get_current_user(
    authorization: Optional[str] = Header(None),
    db: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Chưa đăng nhập")

    user = row_to_dict(get_user_by_id(db, decode_token(authorization.removeprefix("Bearer ").strip())))
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Người dùng không tồn tại")
    return user


def public_user(user: dict[str, Any] | sqlite3.Row) -> dict[str, Any]:
    data = dict(user)
    data.pop("password", None)
    return data


# =============================================================================
# Chat service
# =============================================================================
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_session_title(message: str) -> str:
    compact = " ".join(message.strip().split())
    return compact[:40] + ("..." if len(compact) > 40 else "") if compact else "Cuộc trò chuyện mới"


def generate_ai_reply(question: str, context_file: Optional[str] = None) -> str:
    """
    Placeholder for the real AgentFinX reasoning pipeline.

    Keep this function as a narrow integration point: later it can call LangChain,
    OpenAI, Gemini, or the repository's graph workflow without changing routes.
    """
    q = question.lower()

    canned_answers = [
        (
            ("tổng tài sản", "total asset"),
            (
                "<b>Tổng tài sản</b> của Tập đoàn Hòa Phát tại ngày 30/06/2025:<br>"
                "• Tài sản ngắn hạn: <b>42.136.500.000.000 VND</b><br>"
                "• Tài sản dài hạn: <b>49.678.494.942.317 VND</b><br>"
                "• <b>Tổng cộng: 91.814.994.942.317 VND</b><br><br>"
                "So với đầu kỳ, tổng tài sản tăng <b>~12,3%</b>."
            ),
        ),
        (
            ("chi phí tài chính",),
            (
                "<b>Chi phí tài chính</b> 6 tháng đầu năm 2025:<br>"
                "• Lãi vay: <b>58.210.000.000 VND</b><br>"
                "• Khác: <b>3.808.961.390 VND</b><br>"
                "• <b>Tổng: 62.018.961.390 VND</b> (~1,15% doanh thu thuần)"
            ),
        ),
        (
            ("hệ số nợ", "nợ phải trả"),
            (
                "<b>Hệ số Nợ / Tổng Tài sản:</b><br>"
                "6.078.241.901.235 ÷ 91.814.994.942.317 ≈ <b>0,066 (6,6%)</b><br>"
                "Cấu trúc tài chính <b>lành mạnh</b>, ít phụ thuộc nợ vay."
            ),
        ),
        (
            ("roa",),
            (
                "<b>ROA bình quân 6T/2025:</b><br>"
                "5.379.996.188.030 ÷ 86.804.035.728.981 ≈ <b>6,19%</b><br>"
                "Mức <b>khá tốt</b> trong ngành thép."
            ),
        ),
        (
            ("roe",),
            (
                "<b>ROE</b> = Lợi nhuận sau thuế ÷ Vốn chủ sở hữu bình quân.<br>"
                "Vui lòng tải lên BCTC để tôi trích xuất số liệu tự động."
            ),
        ),
        (
            ("dòng tiền", "cash flow"),
            (
                "<b>Phân tích dòng tiền:</b> CFO / CFI / CFF<br>"
                "Tải lên Báo cáo LCTT để tôi phân tích Free Cash Flow, OCF Margin..."
            ),
        ),
        (
            ("dự báo", "doanh thu"),
            (
                "<b>Dự báo doanh thu</b> cần dữ liệu 3-5 năm + kế hoạch mở rộng.<br>"
                "Tải lên BCTC để áp dụng mô hình trendline / hồi quy."
            ),
        ),
        (
            ("rủi ro", "risk"),
            (
                "<b>Rủi ro cần lưu ý:</b><br>"
                "• Thanh khoản: Current Ratio < 1<br>"
                "• Đòn bẩy: Debt/Equity > 2<br>"
                "• Sinh lời: Biên giảm liên tục >= 3 kỳ<br>"
                "Tải lên BCTC để quét toàn bộ chỉ số."
            ),
        ),
        (
            ("phân tích", "bctc"),
            (
                "Tôi có thể phân tích BCTC theo:<br>"
                "1. Sinh lời: ROA, ROE, biên LN<br>"
                "2. Thanh khoản: Current/Quick Ratio<br>"
                "3. Đòn bẩy: D/A, D/E, ICR<br>"
                "4. Hiệu quả: vòng quay HTK, khoản phải thu<br>"
                "Bạn muốn tập trung vào khía cạnh nào?"
            ),
        ),
    ]

    for keywords, answer in canned_answers:
        if any(keyword in q for keyword in keywords):
            return answer

    if context_file:
        return (
            f"Tôi đã nhận và xử lý file <b>{context_file}</b>.<br>"
            "Bạn muốn phân tích chỉ số nào? (ROA, ROE, dòng tiền, cơ cấu nợ...)"
        )

    return (
        f"Tôi hiểu câu hỏi: <b>{question}</b>.<br>"
        "Vui lòng cung cấp thêm ngữ cảnh hoặc tải lên BCTC để tôi phân tích số liệu thực tế."
    )


# =============================================================================
# File service
# =============================================================================
_FILENAME_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_filename(filename: str) -> str:
    basename = Path(filename or "upload").name.strip()
    sanitized = _FILENAME_SAFE_CHARS.sub("_", basename)
    return sanitized or "upload"


async def save_upload(file: UploadFile, file_id: str, ext: str) -> tuple[str, int]:
    stored_name = f"{file_id}{ext}"
    file_path = settings.upload_dir / stored_name
    total = 0

    with file_path.open("wb") as out:
        while chunk := await file.read(1024 * 1024):
            total += len(chunk)
            if total > settings.max_upload_bytes:
                file_path.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"File vượt quá giới hạn {settings.max_upload_bytes // (1024 * 1024)} MB",
                )
            await asyncio.to_thread(out.write, chunk)

    return stored_name, total


def validate_upload(file: UploadFile) -> str:
    filename = sanitize_filename(file.filename or "")
    ext = Path(filename).suffix.lower()
    if ext not in settings.allowed_upload_exts:
        allowed = ", ".join(sorted(settings.allowed_upload_exts))
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Chỉ hỗ trợ: {allowed}")
    return ext


# =============================================================================
# Application factory
# =============================================================================
@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    init_db()
    logger.info("AgentFinX API started with db=%s uploads=%s", settings.db_path, settings.upload_dir)
    yield


def create_app() -> FastAPI:
    app = FastAPI(title=settings.app_name, version=settings.version, lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    if settings.frontend_dir.exists():
        app.mount("/static", StaticFiles(directory=settings.frontend_dir), name="static")

    register_routes(app)
    return app


def register_routes(app: FastAPI) -> None:
    def frontend_file(filename: str) -> FileResponse:
        path = settings.frontend_dir / filename
        if not path.exists() or not path.is_file():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{filename} không tồn tại")
        return FileResponse(path)

    @app.post("/api/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
    def register(body: RegisterBody, db: sqlite3.Connection = Depends(get_db)):
        if get_user_by_email(db, body.email):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email đã được đăng ký")

        user = create_user(db, body.name, body.email, body.password)
        return {"token": create_token(user["id"]), "user": user}

    @app.post("/api/login", response_model=AuthResponse)
    def login(body: LoginBody, db: sqlite3.Connection = Depends(get_db)):
        user = get_user_by_email(db, body.email)
        if not user or not verify_password(body.password, user["password"]):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Email hoặc mật khẩu không đúng")

        return {"token": create_token(user["id"]), "user": public_user(user)}

    @app.get("/api/me", response_model=UserOut)
    def me(current_user: dict[str, Any] = Depends(get_current_user)):
        return public_user(current_user)

    @app.get("/api/sessions", response_model=list[SessionOut])
    def list_sessions(
        current_user: dict[str, Any] = Depends(get_current_user),
        db: sqlite3.Connection = Depends(get_db),
    ):
        rows = db.execute(
            "SELECT * FROM sessions WHERE user_id = ? ORDER BY updated_at DESC",
            (current_user["id"],),
        ).fetchall()
        return [dict(row) for row in rows]

    @app.post("/api/sessions", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
    def create_session(
        current_user: dict[str, Any] = Depends(get_current_user),
        db: sqlite3.Connection = Depends(get_db),
    ):
        session_id = create_chat_session(db, current_user["id"], "Cuộc trò chuyện mới")
        db.commit()
        return row_to_dict(get_owned_session(db, session_id, current_user["id"]))

    @app.get("/api/sessions/{session_id}/messages", response_model=list[MessageOut])
    def get_messages(
        session_id: str,
        current_user: dict[str, Any] = Depends(get_current_user),
        db: sqlite3.Connection = Depends(get_db),
    ):
        if not get_owned_session(db, session_id, current_user["id"]):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session không tồn tại")

        rows = db.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at",
            (session_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    @app.delete("/api/sessions/{session_id}")
    def delete_session(
        session_id: str,
        current_user: dict[str, Any] = Depends(get_current_user),
        db: sqlite3.Connection = Depends(get_db),
    ):
        if not get_owned_session(db, session_id, current_user["id"]):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session không tồn tại")

        db.execute("DELETE FROM sessions WHERE id = ? AND user_id = ?", (session_id, current_user["id"]))
        db.commit()
        return {"ok": True}

    @app.post("/api/chat", response_model=ChatResponse)
    def chat(
        body: ChatBody,
        current_user: dict[str, Any] = Depends(get_current_user),
        db: sqlite3.Connection = Depends(get_db),
    ):
        if body.session_id:
            if not get_owned_session(db, body.session_id, current_user["id"]):
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session không tồn tại")
            session_id = body.session_id
        else:
            session_id = create_chat_session(db, current_user["id"], make_session_title(body.message))

        context_file = None
        if body.file_id:
            uploaded_file = get_owned_file(db, body.file_id, current_user["id"])
            if not uploaded_file:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File không tồn tại")
            context_file = uploaded_file["filename"]

        add_message(db, session_id, "user", body.message)
        reply = generate_ai_reply(body.message, context_file=context_file)
        add_message(db, session_id, "ai", reply)
        db.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (utc_now_iso(), session_id))
        db.commit()

        return {"session_id": session_id, "reply": reply}

    @app.post("/api/upload", response_model=UploadResponse, status_code=status.HTTP_201_CREATED)
    async def upload_file(
        file: UploadFile = File(...),
        session_id: Optional[str] = Form(None),
        current_user: dict[str, Any] = Depends(get_current_user),
        db: sqlite3.Connection = Depends(get_db),
    ):
        if session_id and not get_owned_session(db, session_id, current_user["id"]):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session không tồn tại")

        ext = validate_upload(file)
        file_id = str(uuid.uuid4())
        stored_name, size = await save_upload(file, file_id, ext)
        original_name = sanitize_filename(file.filename or stored_name)

        db.execute(
            """
            INSERT INTO uploaded_files(id, user_id, session_id, filename, stored_name, size)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (file_id, current_user["id"], session_id, original_name, stored_name, size),
        )
        db.commit()

        return {
            "file_id": file_id,
            "filename": original_name,
            "size": size,
            "message": f"Đã tải lên thành công: {original_name}",
        }

    @app.get("/api/files", response_model=list[FileOut])
    def list_files(
        current_user: dict[str, Any] = Depends(get_current_user),
        db: sqlite3.Connection = Depends(get_db),
    ):
        rows = db.execute(
            """
            SELECT id, filename, size, uploaded_at, session_id
            FROM uploaded_files
            WHERE user_id = ?
            ORDER BY uploaded_at DESC
            """,
            (current_user["id"],),
        ).fetchall()
        return [dict(row) for row in rows]

    @app.get("/")
    def root():
        return frontend_file("index.html")

    @app.get("/chat")
    def chat_page():
        return frontend_file("chatbox.html")

    @app.get("/{asset_name}")
    def frontend_asset(asset_name: str):
        allowed_assets = {"style.css", "chatstyle.css", "chatbox.js", "ảnh.jpg", "ảnh2.jpg", "favicon.ico"}
        if asset_name not in allowed_assets:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Không tìm thấy tài nguyên")
        return frontend_file(asset_name)


app = create_app()
