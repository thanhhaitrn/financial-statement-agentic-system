"""Login and sign-up rate limits, and the client address they are keyed on.

Limits are sliding windows over an attempt log in the web database, so they
hold across API restarts. A request is checked and recorded in one write
transaction *before* any Argon2 work: a burst of parallel requests cannot all
slip under a limit, and a refused request costs one indexed query. Keys are
stored hashed; the log never holds an email address or an IP in clear.
"""
# Code note: Web modules expose the pipeline to browsers; comments call out security boundaries.

from __future__ import annotations

import hashlib
import ipaddress
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from agentfinx.api import db as webdb

# Attempts older than this are deleted; every window is shorter.
_LOG_RETENTION_SECONDS = 24 * 3600


@dataclass(frozen=True)
class Limit:
    scope: str
    max_attempts: int
    window_seconds: int


class Throttled(Exception):
    def __init__(self, retry_after: int):
        super().__init__(f"retry after {retry_after}s")
        self.retry_after = retry_after


def key_hash(scope: str, value: str) -> str:
    return hashlib.sha256(f"{scope}\0{value}".encode("utf-8")).hexdigest()


def admit(conn, checks: Sequence[tuple[Limit, str]], *, now: float) -> list[int]:
    """Inside a write transaction: refuse if any key is at its limit, otherwise
    record one attempt per key and return the attempt ids."""

    webdb.prune_attempts(conn, before=now - _LOG_RETENTION_SECONDS)
    retry_after = 0
    for limit, value in checks:
        times = webdb.count_attempts(conn, limit.scope, key_hash(limit.scope, value),
                                     since=now - limit.window_seconds)
        if len(times) >= limit.max_attempts:
            # Allowed again once enough of the oldest attempts leave the window.
            frees_at = times[len(times) - limit.max_attempts] + limit.window_seconds
            retry_after = max(retry_after, int(frees_at - now) + 1)
    if retry_after:
        raise Throttled(retry_after)
    return [
        webdb.add_attempt(conn, limit.scope, key_hash(limit.scope, value), at=now)
        for limit, value in checks
    ]


def forget(conn, attempt_ids: Iterable[int]) -> None:
    webdb.delete_attempts(conn, list(attempt_ids))


def clear(conn, limit: Limit, value: str) -> None:
    webdb.clear_attempts(conn, limit.scope, key_hash(limit.scope, value))


def client_address(
    peer: Optional[str],
    forwarded_for: Optional[str],
    trusted: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> str:
    """The client as seen by the nearest proxy we do not trust.

    Without trusted proxies the socket peer is the client and X-Forwarded-For
    is ignored (any client can write it). With them, walk the chain from the
    right, skipping trusted hops.
    """

    peer = (peer or "").strip() or "unknown"
    if not trusted:
        return peer
    hops = [item.strip() for item in (forwarded_for or "").split(",") if item.strip()]
    hops.append(peer)
    for hop in reversed(hops):
        try:
            address = ipaddress.ip_address(hop)
        except ValueError:
            return hop[:64]
        if not any(address in network for network in trusted):
            return str(address)
    return hops[0]
