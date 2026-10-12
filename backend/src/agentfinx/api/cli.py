"""``agentfinx serve``, ``agentfinx worker --queue chat|import``, ``agentfinx invites``
and ``agentfinx db init``.

None of them creates the account database on its own: a missing database
means the wrong storage, not a new install, so they stop instead of quietly
starting an empty one. A new install runs ``agentfinx db init`` once.
"""
# Code note: Web modules expose the pipeline to browsers; comments call out security boundaries.

from __future__ import annotations

import argparse
import ipaddress
import logging
import os
import signal
import sys
import threading
from typing import Optional


def _prepare_environment(host: Optional[str] = None, port: Optional[int] = None) -> dict:
    from dotenv import load_dotenv

    load_dotenv()
    env = dict(os.environ)
    if host:
        env["FINX_HOST"] = host
    if port:
        env["FINX_PORT"] = str(port)
    return env


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Per-request INFO lines from HTTP clients print provider/cluster hostnames
    # for every call; keep them out of routine logs.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _require_database(db_path, prog: str) -> bool:
    """The account database must already exist; say which one is used."""

    import sqlite3
    from pathlib import Path

    path = Path(db_path)
    if not path.is_file():
        print(
            f"{prog}: no web database at {path}. A new install creates it once with "
            "`agentfinx db init`; data still in dataset_store/ moves with `agentfinx storage migrate`.",
            file=sys.stderr,
        )
        return False
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        accounts = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    except sqlite3.Error:
        accounts = 0
    finally:
        conn.close()
    logging.getLogger("agentfinx.web").info("web database %s (%d accounts)", path, accounts)
    return True


def db_main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="agentfinx db", description="The web app's account and chat database.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Create the database for a new install (never overwrites one).")
    parser.parse_args(argv)

    from agentfinx.api import db as webdb
    from agentfinx.api.settings import resolve_db_path

    try:
        path = resolve_db_path(_prepare_environment())
    except RuntimeError as exc:
        print(f"agentfinx db: {exc}", file=sys.stderr)
        return 2
    if path.exists():
        print(f"agentfinx db: {path} already exists; nothing to do")
        return 0
    version = webdb.WebDB(path).migrate()
    print(f"created {path} (schema v{version})")
    return 0


def serve_main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="agentfinx serve", description="Run the local web API.")
    parser.add_argument("--host", default=None, help="Bind address (default 127.0.0.1).")
    parser.add_argument("--port", type=int, default=None, help="Port (default 8000).")
    args = parser.parse_args(argv)

    _configure_logging()
    from agentfinx.datasets.vectorstore.qdrant_store import ensure_multiprocess_qdrant_backend
    from agentfinx.api.settings import WebConfigError, load_settings

    try:
        settings = load_settings(_prepare_environment(args.host, args.port))
        ensure_multiprocess_qdrant_backend()
    except (WebConfigError, RuntimeError) as exc:
        print(f"agentfinx serve: {exc}", file=sys.stderr)
        return 2
    if not _require_database(settings.db_path, "agentfinx serve"):
        return 2
    if not _is_loopback(settings.host):
        print(
            f"agentfinx serve: WARNING binding to {settings.host}; this build is meant for "
            "local use and has no public-deployment hardening.",
            file=sys.stderr,
        )
        if settings.registration_mode == "open":
            print(
                "agentfinx serve: WARNING FINX_REGISTRATION=open lets anyone create accounts; "
                "use invite (the default) beyond localhost.",
                file=sys.stderr,
            )

    import uvicorn

    from agentfinx.api.app import create_app

    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level="info",
        server_header=False,
        proxy_headers=False,
    )
    return 0


def worker_main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="agentfinx worker", description="Run one queue worker.")
    parser.add_argument("--queue", required=True, choices=["chat", "import"])
    args = parser.parse_args(argv)

    _configure_logging()
    from agentfinx.datasets.vectorstore.qdrant_store import ensure_multiprocess_qdrant_backend
    from agentfinx.api.settings import WebConfigError, load_settings
    from agentfinx.api.worker import WorkerAlreadyRunning, run_worker

    try:
        settings = load_settings(_prepare_environment())
        ensure_multiprocess_qdrant_backend()
    except (WebConfigError, RuntimeError) as exc:
        print(f"agentfinx worker: {exc}", file=sys.stderr)
        return 2
    if not _require_database(settings.db_path, "agentfinx worker"):
        return 2

    stop = threading.Event()

    def request_stop(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    try:
        return run_worker(args.queue, settings, stop_event=stop)
    except WorkerAlreadyRunning as exc:
        print(f"agentfinx worker: {exc}", file=sys.stderr)
        return 3


def invites_main(argv: Optional[list[str]] = None) -> int:
    """``agentfinx invites create|list|revoke``: single-use sign-up invites.

    With ``FINX_REGISTRATION=invite`` (the default) an account can only be
    created with an invite bound to its email. The code is printed once and
    only its hash is stored.
    """

    parser = argparse.ArgumentParser(prog="agentfinx invites", description="Manage sign-up invites.")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="Create an invite for one email address.")
    create.add_argument("--email", required=True)
    create.add_argument("--days", type=int, default=7, help="Days until the invite expires (default 7).")
    create.add_argument("--base-url", default="http://127.0.0.1:3000",
                        help="Address of the web UI, for the printed sign-up link.")
    commands.add_parser("list", help="List invites (codes are never shown again).")
    revoke = commands.add_parser("revoke", help="Revoke an unused invite.")
    revoke.add_argument("invite_id")
    args = parser.parse_args(argv)

    from datetime import datetime, timedelta, timezone
    from urllib.parse import quote

    from agentfinx.api import auth
    from agentfinx.api import db as webdb
    from agentfinx.api.settings import WebConfigError, load_settings

    try:
        settings = load_settings(_prepare_environment())
    except (WebConfigError, RuntimeError) as exc:
        print(f"agentfinx invites: {exc}", file=sys.stderr)
        return 2
    if not _require_database(settings.db_path, "agentfinx invites"):
        return 2
    database = webdb.WebDB(settings.db_path)
    database.migrate()

    if args.command == "create":
        try:
            email = auth.normalize_email(args.email)
        except ValueError:
            print("agentfinx invites: invalid email address", file=sys.stderr)
            return 2
        if not auth.is_deliverable_email(email):
            print("agentfinx invites: that email domain cannot receive mail", file=sys.stderr)
            return 2
        if not 1 <= args.days <= 90:
            print("agentfinx invites: --days must be between 1 and 90", file=sys.stderr)
            return 2
        code = auth.new_invite_code()
        expires = datetime.now(timezone.utc) + timedelta(days=args.days)
        with database.transaction() as conn:
            if webdb.get_user_by_email(conn, email) is not None:
                print("agentfinx invites: that email already has an account", file=sys.stderr)
                return 2
            invite = webdb.create_invite(
                conn, code_hash=auth.hash_invite_code(code), email=email, expires_at=webdb.iso(expires)
            )
        print(f"invite {invite['id']} for {email}, expires {invite['expires_at']}")
        print(f"code: {code}")
        print(f"link: {args.base_url.rstrip('/')}/signup?invite={quote(code)}")
        return 0

    if args.command == "list":
        with database.read() as conn:
            rows = webdb.list_invites(conn)
        now = webdb.utc_now()
        for row in rows:
            state = ("used" if row["used_at"] else "revoked" if row["revoked_at"]
                     else "expired" if row["expires_at"] <= now else "open")
            print(f"{row['id']}  {state:<8} {row['email']}  expires {row['expires_at']}")
        if not rows:
            print("no invites")
        return 0

    with database.transaction() as conn:
        revoked = webdb.revoke_invite(conn, args.invite_id)
    if not revoked:
        print("agentfinx invites: no open invite with that id", file=sys.stderr)
        return 1
    print(f"revoked {args.invite_id}")
    return 0
