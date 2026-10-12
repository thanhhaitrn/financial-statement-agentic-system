"""Password hashing, session tokens, invite codes and email normalization.

Passwords are hashed with Argon2 (argon2-cffi). Sessions are short JWTs
signed with ``FINX_SECRET_KEY`` and carried only in an ``HttpOnly``,
``SameSite=Lax`` cookie — never in ``localStorage`` where page scripts, and
therefore any XSS, could read them. Each token names a server-side login
session (``sid``), so logout revokes the token itself, not just the cookie.
"""
# Code note: Web modules expose the pipeline to browsers; comments call out security boundaries.

from __future__ import annotations

import hashlib
import re
import secrets
import time
from dataclasses import dataclass
from typing import Optional

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError

try:
    from argon2.exceptions import InvalidHashError
except ImportError:  # argon2-cffi < 23.1 names it InvalidHash
    from argon2.exceptions import InvalidHash as InvalidHashError

SESSION_COOKIE = "finx_session"
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 256
_JWT_ALGORITHM = "HS256"
_HASHER = PasswordHasher()
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,253}\.[^@\s.]{2,}$")
# Verifying against a real hash when the account does not exist keeps login
# timing from revealing which emails are registered.
_TIMING_EQUALIZER_HASH = _HASHER.hash("agentfinx-timing-equalizer")


# Special-use names (RFC 2606/6761) never receive mail; refused at sign-up.
_UNDELIVERABLE_TLDS = frozenset({"invalid", "test", "example", "localhost", "local", "internal"})


@dataclass(frozen=True)
class SessionClaims:
    user_id: str
    session_id: str


def normalize_email(value: object) -> str:
    text = str(value or "").strip().lower()
    if len(text) > 254 or not _EMAIL_RE.fullmatch(text):
        raise ValueError("invalid email address")
    return text


def is_deliverable_email(email: str) -> bool:
    return email.rsplit(".", 1)[-1] not in _UNDELIVERABLE_TLDS


def new_invite_code() -> str:
    return secrets.token_urlsafe(24)


def hash_invite_code(code: str) -> str:
    # Only the hash is stored: a copy of the database cannot be used to sign up.
    return hashlib.sha256(code.strip().encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    return _HASHER.hash(password)


def verify_password(password: str, password_hash: Optional[str]) -> bool:
    try:
        return _HASHER.verify(password_hash or _TIMING_EQUALIZER_HASH, password) and bool(
            password_hash
        )
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def issue_token(
    user_id: str,
    session_id: str,
    secret: str,
    ttl_seconds: int,
    *,
    now: Optional[int] = None,
) -> str:
    issued = int(time.time() if now is None else now)
    claims = {
        "sub": user_id,
        "sid": session_id,
        "typ": "session",
        "iat": issued,
        "exp": issued + int(ttl_seconds),
    }
    return jwt.encode(claims, secret, algorithm=_JWT_ALGORITHM)


def decode_token(token: Optional[str], secret: str) -> Optional[SessionClaims]:
    """The claims of a validly signed, unexpired session token, otherwise
    ``None``. The caller must still check that the session is not revoked."""

    if not token:
        return None
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=[_JWT_ALGORITHM],
            options={"require": ["exp", "iat", "sub", "sid"]},
        )
    except jwt.PyJWTError:
        return None
    if claims.get("typ") != "session":
        return None
    subject = str(claims.get("sub") or "").strip()
    session_id = str(claims.get("sid") or "").strip()
    if not subject or not session_id:
        return None
    return SessionClaims(user_id=subject, session_id=session_id)
