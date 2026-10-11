"""Environment-backed settings for the local web app and its workers."""
# Code note: Web modules expose the pipeline to browsers; comments call out security boundaries.

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Union

from dataset_catalog.access import shared_dataset_ids_from_env

MIN_SECRET_LENGTH = 32
# invite: only with a code from `agentfinx invites create` (default);
# open: anyone, for local development only; closed: no new accounts.
REGISTRATION_MODES = ("invite", "open", "closed")

IPNetwork = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]


class WebConfigError(RuntimeError):
    """The web app cannot start with the current environment."""


@dataclass(frozen=True)
class WebSettings:
    secret_key: str
    db_path: Path
    host: str = "127.0.0.1"
    port: int = 8000
    allowed_origins: frozenset[str] = frozenset()
    shared_dataset_ids: frozenset[str] = frozenset()
    cookie_secure: bool = False
    remember_ttl_seconds: int = 30 * 24 * 3600
    session_ttl_seconds: int = 12 * 3600
    worker_poll_seconds: float = 1.0
    heartbeat_interval_seconds: float = 5.0
    heartbeat_stale_seconds: float = 30.0
    # Staged rollout: keep OFF until the live context + QA gates are approved.
    # Target after acceptance is ON for web; explicit FINX_CHAT_CONTEXT=0 rolls back.
    chat_context: bool = False
    # CafeF report discovery in the form and chat (FINX_REPORT_DISCOVERY_ENABLED).
    report_discovery: bool = False
    registration_mode: str = "invite"
    # Proxies whose X-Forwarded-For is believed (FINX_TRUSTED_PROXIES). Empty:
    # the socket peer is the client. Next.js passes a client-sent header through
    # unchanged, so list it only behind a proxy that sets the header itself.
    trusted_proxies: tuple[IPNetwork, ...] = ()
    # Brute-force and sign-up limits, checked before any Argon2 work. Login
    # counts failures only; registration counts every attempt.
    login_account_limit: int = 5
    login_ip_limit: int = 30
    login_window_seconds: int = 15 * 60
    register_ip_limit: int = 10
    register_window_seconds: int = 3600


def default_db_path() -> Path:
    # storage/web/, the git-ignored runtime storage (config.paths).
    from config import paths

    return paths.default_web_db_path()


def resolve_db_path(env: Mapping[str, str]) -> Path:
    db_raw = str(env.get("FINX_WEB_DB", "") or "").strip()
    return Path(db_raw).expanduser() if db_raw else default_db_path()


def _flag(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def load_settings(environ: Optional[Mapping[str, str]] = None) -> WebSettings:
    env = os.environ if environ is None else environ

    # No default secret: a guessable key would let anyone mint session cookies.
    secret = str(env.get("FINX_SECRET_KEY", "") or "")
    if len(secret) < MIN_SECRET_LENGTH:
        raise WebConfigError(
            "FINX_SECRET_KEY must be a random value of at least "
            f"{MIN_SECRET_LENGTH} characters; there is no default. "
            "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(48))\""
        )

    host = str(env.get("FINX_HOST", "") or "127.0.0.1").strip()
    try:
        port = int(str(env.get("FINX_PORT", "") or "8000"))
    except ValueError as exc:
        raise WebConfigError("FINX_PORT must be an integer") from exc

    raw_origins = str(env.get("FINX_ALLOWED_ORIGINS", "") or "")
    origins = {item.strip().rstrip("/") for item in raw_origins.split(",") if item.strip()}
    if not origins:
        origins = {f"http://{host}:{port}"}
        if host in {"127.0.0.1", "localhost"}:
            origins |= {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    db_path = resolve_db_path(env)

    try:
        shared = shared_dataset_ids_from_env(env)
    except ValueError as exc:
        raise WebConfigError(f"FINX_SHARED_DATASET_IDS is invalid: {exc}") from exc

    registration = str(env.get("FINX_REGISTRATION", "") or "invite").strip().lower()
    if registration not in REGISTRATION_MODES:
        raise WebConfigError(f"FINX_REGISTRATION must be one of {', '.join(REGISTRATION_MODES)}")

    proxies = []
    for item in str(env.get("FINX_TRUSTED_PROXIES", "") or "").split(","):
        if not item.strip():
            continue
        try:
            proxies.append(ipaddress.ip_network(item.strip(), strict=False))
        except ValueError as exc:
            raise WebConfigError(f"FINX_TRUSTED_PROXIES has an invalid entry: {item.strip()!r}") from exc

    return WebSettings(
        secret_key=secret,
        db_path=db_path,
        host=host,
        port=port,
        allowed_origins=frozenset(origins),
        shared_dataset_ids=shared,
        cookie_secure=_flag(env.get("FINX_COOKIE_SECURE")),
        chat_context=_flag(env.get("FINX_CHAT_CONTEXT")),
        report_discovery=_flag(env.get("FINX_REPORT_DISCOVERY_ENABLED")),
        registration_mode=registration,
        trusted_proxies=tuple(proxies),
    )
