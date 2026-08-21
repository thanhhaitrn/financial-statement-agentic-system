"""
AgentFinX FastAPI backend.

Run locally from ``agentfinx-web/backend``:
    pip install -r requirements.txt
    pip install -e ../agent          # the bundled AI Agent package
    cp .env.example .env             # then fill in OLLAMA_*/QDRANT_* creds
    uvicorn main:app --reload --port 8000

Then open http://localhost:8000 (landing page). This app serves the whole
3-page frontend (landing / auth / chat) from ../frontend, so no separate
static server or CORS workaround is needed.

See agent_bridge.py for how the real AgentFinX LangGraph pipeline
(bundled at ../agent) is wired in, and what env vars it needs.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr, Field

import agent_bridge

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("agentfinx.api")

BASE_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = BASE_DIR.parent / "frontend"


class Settings:
    app_name = "AgentFinX API"
    version = "1.1.0"
    env = os.getenv("FINX_ENV", "development")
    secret_key = os.getenv("FINX_SECRET_KEY", "dev-only-insecure-key-change-me")
    algorithm = "HS256"
    access_token_expire_minutes = 60 * 24 * 7  # 7 days
    db_path = Path(os.getenv("FINX_DB_PATH", str(BASE_DIR / "finx.db")))
    upload_dir = Path(os.getenv("FINX_UPLOAD_DIR", str(BASE_DIR / "uploads")))
    cors_origins = [o.strip() for o in os.getenv("FINX_CORS_ORIGINS", "*").split(",") if o.strip()]


settings = Settings()

if settings.env == "production" and settings.secret_key == "dev-only-insecure-key-change-me":
    raise RuntimeError("Set FINX_SECRET_KEY before running with FINX_ENV=production.")

settings.upload_dir.mkdir(parents=True, exist_ok=True)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def db_cursor():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with db_cursor() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                title TEXT NOT NULL DEFAULT 'Cuộc trò chuyện mới',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK(role IN ('user','ai')),
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS uploaded_files (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                session_id TEXT,
                filename TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                content_type TEXT,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
            CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
            CREATE INDEX IF NOT EXISTS idx_files_user ON uploaded_files(user_id);
            """
        )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)


def create_access_token(user_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    payload = {"sub": user_id, "exp": expire}
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


def decode_token(token: str) -> str:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
        user_id = payload.get("sub")
        if not user_id:
            raise JWTError("Missing subject")
        return user_id
    except JWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token không hợp lệ hoặc đã hết hạn") from exc


def get_current_user(authorization: Optional[str] = Header(default=None)) -> sqlite3.Row:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Thiếu token xác thực")
    token = authorization.split(" ", 1)[1].strip()
    user_id = decode_token(token)
    with db_cursor() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Tài khoản không tồn tại")
    return row


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class RegisterPayload(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    password: str = Field(min_length=6, max_length=128)


class LoginPayload(BaseModel):
    email: EmailStr
    password: str


class ChatPayload(BaseModel):
    session_id: Optional[str] = None
    message: str = Field(min_length=1, max_length=8000)
    file_id: Optional[str] = None


def user_out(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "name": row["name"], "email": row["email"]}


def session_out(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def message_out(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "role": row["role"],
        "content": row["content"],
        "created_at": row["created_at"],
    }


# ---------------------------------------------------------------------------
# AI reply generation
# ---------------------------------------------------------------------------

def _generate_demo_reply(question: str, context_file: Optional[str] = None) -> str:
    """Small canned fallback used only when the real AgentFinX pipeline
    (agent_bridge.py -> ../agent) isn't configured/reachable yet, so the UI
    stays demoable while you wire up Ollama/Qdrant."""
    q = question.lower()
    if "tổng tài sản" in q:
        return (
            "<i>(Câu trả lời demo — AI Agent thật chưa sẵn sàng, xem <code>/api/health</code>)</i><br><br>"
            "Tổng tài sản đạt <b>91.814,99 tỷ VND</b>, tăng <b>~12,3%</b> so với đầu kỳ."
        )
    if "roe" in q or "roa" in q:
        return (
            "<i>(Câu trả lời demo — AI Agent thật chưa sẵn sàng)</i><br><br>"
            "ROE ước tính <b>14,2%</b>, ROA ước tính <b>6,8%</b> trong kỳ báo cáo gần nhất."
        )
    prefix = f"Đã nhận file <b>{context_file}</b>. " if context_file else ""
    return (
        "<i>(Câu trả lời demo — AI Agent thật chưa sẵn sàng, xem hướng dẫn nối AI Agent trong README)</i><br><br>"
        f"{prefix}Tôi là bản demo giao diện của AgenFin-X. Câu hỏi của bạn: “{question}”. "
        "Khi AI Agent thật được cấu hình (Ollama + Qdrant), tôi sẽ phân tích số liệu thật từ báo cáo đã ingest."
    )


def generate_ai_reply(question: str, context_file: Optional[str] = None) -> str:
    status_info = agent_bridge.pipeline_status()
    if status_info["available"]:
        dataset_id = agent_bridge.resolve_dataset_id(context_file)
        try:
            return agent_bridge.run_real_pipeline(question, dataset_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("AgentFinX real pipeline failed, falling back to demo answers")
            return (
                f"AI Agent thật gặp lỗi khi xử lý câu hỏi này (<code>{type(exc).__name__}: {exc}</code>).<br><br>"
                + _generate_demo_reply(question, context_file)
            )
    logger.info("AgentFinX real pipeline not available (%s) — using demo answers", status_info["reason"])
    return _generate_demo_reply(question, context_file)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title=Settings.app_name, version=Settings.version)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    init_db()


# ---- auth ----

@app.post("/api/register", status_code=status.HTTP_201_CREATED)
def register(payload: RegisterPayload):
    with db_cursor() as conn:
        existing = conn.execute("SELECT id FROM users WHERE email = ?", (payload.email,)).fetchone()
        if existing:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email đã được sử dụng")
        user_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO users (id, name, email, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, payload.name.strip(), str(payload.email).lower(), hash_password(payload.password), now_iso()),
        )
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    token = create_access_token(user_id)
    return {"token": token, "user": user_out(row)}


@app.post("/api/login")
def login(payload: LoginPayload):
    with db_cursor() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (str(payload.email).lower(),)).fetchone()
    if row is None or not verify_password(payload.password, row["password_hash"]):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Email hoặc mật khẩu không đúng")
    token = create_access_token(row["id"])
    return {"token": token, "user": user_out(row)}


@app.get("/api/me")
def me(current_user: sqlite3.Row = Depends(get_current_user)):
    return user_out(current_user)


@app.get("/api/health")
def health():
    pipeline = agent_bridge.pipeline_status()
    return {
        "ok": True,
        "app": settings.app_name,
        "version": settings.version,
        "real_agent_available": pipeline["available"],
        "real_agent_note": pipeline["reason"] or None,
    }


# ---- sessions ----

@app.get("/api/sessions")
def list_sessions(current_user: sqlite3.Row = Depends(get_current_user)):
    with db_cursor() as conn:
        rows = conn.execute(
            "SELECT * FROM sessions WHERE user_id = ? ORDER BY updated_at DESC", (current_user["id"],)
        ).fetchall()
    return [session_out(r) for r in rows]


@app.post("/api/sessions", status_code=status.HTTP_201_CREATED)
def create_session(current_user: sqlite3.Row = Depends(get_current_user)):
    session_id = str(uuid.uuid4())
    ts = now_iso()
    with db_cursor() as conn:
        conn.execute(
            "INSERT INTO sessions (id, user_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (session_id, current_user["id"], "Cuộc trò chuyện mới", ts, ts),
        )
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return session_out(row)


def _get_owned_session(conn: sqlite3.Connection, session_id: str, user_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM sessions WHERE id = ? AND user_id = ?", (session_id, user_id)).fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Không tìm thấy cuộc trò chuyện")
    return row


@app.get("/api/sessions/{session_id}/messages")
def get_messages(session_id: str, current_user: sqlite3.Row = Depends(get_current_user)):
    with db_cursor() as conn:
        _get_owned_session(conn, session_id, current_user["id"])
        rows = conn.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at ASC", (session_id,)
        ).fetchall()
    return [message_out(r) for r in rows]


@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: str, current_user: sqlite3.Row = Depends(get_current_user)):
    with db_cursor() as conn:
        _get_owned_session(conn, session_id, current_user["id"])
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    return {"deleted": True}


# ---- chat ----

def _derive_title(message: str) -> str:
    title = message.strip().replace("\n", " ")
    return (title[:60] + "…") if len(title) > 60 else (title or "Cuộc trò chuyện mới")


@app.post("/api/chat")
def chat(payload: ChatPayload, current_user: sqlite3.Row = Depends(get_current_user)):
    ts = now_iso()
    context_filename = None

    with db_cursor() as conn:
        if payload.file_id:
            file_row = conn.execute(
                "SELECT * FROM uploaded_files WHERE id = ? AND user_id = ?",
                (payload.file_id, current_user["id"]),
            ).fetchone()
            if file_row is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Không tìm thấy file đã upload")
            context_filename = file_row["filename"]

        if payload.session_id:
            session_row = _get_owned_session(conn, payload.session_id, current_user["id"])
            session_id = session_row["id"]
        else:
            session_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO sessions (id, user_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (session_id, current_user["id"], _derive_title(payload.message), ts, ts),
            )

        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, created_at) VALUES (?, ?, 'user', ?, ?)",
            (str(uuid.uuid4()), session_id, payload.message, ts),
        )

    reply = generate_ai_reply(payload.message, context_filename)
    reply_ts = now_iso()

    with db_cursor() as conn:
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, created_at) VALUES (?, ?, 'ai', ?, ?)",
            (str(uuid.uuid4()), session_id, reply, reply_ts),
        )
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (reply_ts, session_id))

    return {"session_id": session_id, "reply": reply, "created_at": reply_ts}


# ---- uploads ----

ALLOWED_UPLOAD_EXTENSIONS = {".pdf", ".xlsx", ".csv"}


@app.post("/api/upload", status_code=status.HTTP_201_CREATED)
async def upload_file(
    file: UploadFile = File(...),
    session_id: Optional[str] = Form(default=None),
    current_user: sqlite3.Row = Depends(get_current_user),
):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_UPLOAD_EXTENSIONS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Chỉ hỗ trợ file PDF, Excel (.xlsx) hoặc CSV")

    if session_id:
        with db_cursor() as conn:
            _get_owned_session(conn, session_id, current_user["id"])

    file_id = str(uuid.uuid4())
    stored_name = f"{file_id}{ext}"
    stored_path = settings.upload_dir / stored_name
    with stored_path.open("wb") as out:
        shutil.copyfileobj(file.file, out)

    with db_cursor() as conn:
        conn.execute(
            "INSERT INTO uploaded_files (id, user_id, session_id, filename, stored_path, content_type, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (file_id, current_user["id"], session_id, file.filename, str(stored_path), file.content_type, now_iso()),
        )

    return {"file_id": file_id, "filename": file.filename}


@app.get("/api/files")
def list_files(current_user: sqlite3.Row = Depends(get_current_user)):
    with db_cursor() as conn:
        rows = conn.execute(
            "SELECT * FROM uploaded_files WHERE user_id = ? ORDER BY created_at DESC", (current_user["id"],)
        ).fetchall()
    return [
        {"id": r["id"], "filename": r["filename"], "created_at": r["created_at"], "session_id": r["session_id"]}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Frontend serving
# ---------------------------------------------------------------------------

def frontend_file(name: str) -> FileResponse:
    path = FRONTEND_DIR / name
    if not path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Không tìm thấy tài nguyên frontend")
    return FileResponse(path)


@app.get("/")
def root():
    return frontend_file("landing.html")


@app.get("/login")
@app.get("/register")
def login_page():
    return frontend_file("auth.html")


@app.get("/chat")
def chat_page():
    return frontend_file("chat.html")


@app.get("/{asset_name}")
def frontend_asset(asset_name: str):
    allowed_assets = {
        "landing.html", "landing.css", "landing.js",
        "auth.html", "auth.css", "auth.js",
        "chat.html", "chatstyle.css", "chatbox.js",
        "tokens.css",
        "favicon.ico",
    }
    if asset_name not in allowed_assets:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Không tìm thấy tài nguyên")
    return frontend_file(asset_name)
