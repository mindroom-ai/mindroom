"""Signed, process-local sessions for the Connections portal."""

import hashlib
import hmac
import secrets

CONNECTIONS_SESSION_COOKIE = "__Host-mindroom_connections_session"
CONNECTIONS_SESSION_SECONDS = 3600
# A new key per process, so a restart signs every portal user out; reopening the portal from Chat signs them in again.
_SESSION_KEY = secrets.token_bytes(32)


def _signature(payload: str) -> str:
    return hmac.new(_SESSION_KEY, payload.encode(), hashlib.sha256).hexdigest()


def issue_connections_session(matrix_user_id: str, *, now: float) -> str:
    """Return a cookie value naming `matrix_user_id` for a fixed lifetime from `now`."""
    payload = f"{int(now) + CONNECTIONS_SESSION_SECONDS}|{matrix_user_id}".encode().hex()
    return f"{payload}.{_signature(payload)}"


def connections_session_user(token: str, *, now: float) -> str | None:
    """Return the Matrix user of a live cookie value this process issued, else `None`."""
    payload, _, signature = token.rpartition(".")
    if not hmac.compare_digest(signature.encode(), _signature(payload).encode()):
        return None
    expires_at, _, matrix_user_id = bytes.fromhex(payload).decode().partition("|")
    return matrix_user_id if now < int(expires_at) else None
