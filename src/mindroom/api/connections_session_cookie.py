"""Signed, process-local sessions for the Connections portal."""

import hashlib
import hmac
import secrets
from typing import NamedTuple

CONNECTIONS_SESSION_COOKIE = "__Host-mindroom_connections_session"
CONNECTIONS_SESSION_SECONDS = 3600
# A new key per process, so a restart signs every portal user out; reopening the portal from Chat signs them in again.
_SESSION_KEY = secrets.token_bytes(32)


class _ConnectionsSession(NamedTuple):
    """A live portal session: who signed in, and whether the sign-in may open the dashboard."""

    matrix_user_id: str
    dashboard: bool


def _signature(payload: str) -> str:
    return hmac.new(_SESSION_KEY, payload.encode(), hashlib.sha256).hexdigest()


def issue_connections_session(matrix_user_id: str, *, now: float, dashboard: bool) -> str:
    """Return a cookie value naming `matrix_user_id` for a fixed lifetime from `now`."""
    payload = f"{int(now) + CONNECTIONS_SESSION_SECONDS}|{int(dashboard)}|{matrix_user_id}".encode().hex()
    return f"{payload}.{_signature(payload)}"


def read_connections_session(token: str, *, now: float) -> _ConnectionsSession | None:
    """Return the session of a live cookie value this process issued, else `None`."""
    payload, _, signature = token.rpartition(".")
    if not hmac.compare_digest(signature.encode(), _signature(payload).encode()):
        return None
    expires_at, _, rest = bytes.fromhex(payload).decode().partition("|")
    dashboard, _, matrix_user_id = rest.partition("|")
    if dashboard not in {"0", "1"} or not matrix_user_id or now >= int(expires_at):
        return None
    return _ConnectionsSession(matrix_user_id, dashboard=dashboard == "1")
