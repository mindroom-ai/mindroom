"""Signed OAuth state token helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from typing import TYPE_CHECKING, Any

from mindroom.oauth.providers import OAuthProviderError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.constants import RuntimePaths

_STATE_SECRET_ENV = "MINDROOM_OAUTH_STATE_SECRET"  # noqa: S105
_FALLBACK_SECRET = "mindroom-local-oauth-state-secret"  # noqa: S105
_consumed_nonce_lock = threading.Lock()
_consumed_state_nonces: dict[tuple[str, str], float] = {}


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _state_secret(runtime_paths: RuntimePaths) -> bytes:
    secret = (
        runtime_paths.env_value(_STATE_SECRET_ENV)
        or runtime_paths.env_value("MINDROOM_API_KEY")
        or runtime_paths.env_value("MINDROOM_LOCAL_CLIENT_SECRET")
        or _FALLBACK_SECRET
    )
    return secret.encode("utf-8")


def _signature(secret: bytes, payload: str) -> str:
    return _b64encode(hmac.new(secret, payload.encode("ascii"), hashlib.sha256).digest())


def _prune_consumed_nonces(now: float) -> None:
    expired = [key for key, expires_at in _consumed_state_nonces.items() if expires_at <= now]
    for key in expired:
        _consumed_state_nonces.pop(key, None)


def issue_signed_oauth_state(
    runtime_paths: RuntimePaths,
    *,
    kind: str,
    ttl_seconds: int,
    data: Mapping[str, Any],
) -> str:
    """Return one signed, time-limited OAuth state token."""
    now = time.time()
    payload = {
        "kind": kind,
        "iat": now,
        "exp": now + ttl_seconds,
        "nonce": secrets.token_urlsafe(18),
        "data": dict(data),
    }
    encoded_payload = _b64encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return f"{encoded_payload}.{_signature(_state_secret(runtime_paths), encoded_payload)}"


def read_signed_oauth_state(
    runtime_paths: RuntimePaths,
    *,
    kind: str,
    token: str,
) -> dict[str, Any]:
    """Validate one signed OAuth state token without marking it consumed."""
    try:
        encoded_payload, provided_signature = token.split(".", 1)
        expected_signature = _signature(_state_secret(runtime_paths), encoded_payload)
        if not hmac.compare_digest(provided_signature, expected_signature):
            msg = "OAuth state is invalid or expired"
            raise OAuthProviderError(msg)
        payload = json.loads(_b64decode(encoded_payload).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        msg = "OAuth state is invalid or expired"
        raise OAuthProviderError(msg) from exc

    if not isinstance(payload, dict) or payload.get("kind") != kind:
        msg = "OAuth state does not match this integration"
        raise OAuthProviderError(msg)
    expires_at = payload.get("exp")
    nonce = payload.get("nonce")
    if not isinstance(expires_at, int | float) or not isinstance(nonce, str) or not nonce:
        msg = "OAuth state is invalid or expired"
        raise OAuthProviderError(msg)
    now = time.time()
    if expires_at <= now:
        msg = "OAuth state is invalid or expired"
        raise OAuthProviderError(msg)

    data = payload.get("data")
    if not isinstance(data, dict):
        msg = "OAuth state is invalid or expired"
        raise OAuthProviderError(msg)
    return data


def consume_signed_oauth_state(
    runtime_paths: RuntimePaths,
    *,
    kind: str,
    token: str,
) -> dict[str, Any]:
    """Validate and consume one signed OAuth state token."""
    data = read_signed_oauth_state(runtime_paths, kind=kind, token=token)
    encoded_payload = token.split(".", 1)[0]
    payload = json.loads(_b64decode(encoded_payload).decode("utf-8"))
    nonce = payload["nonce"]
    expires_at = payload["exp"]
    now = time.time()
    nonce_key = (kind, nonce)
    with _consumed_nonce_lock:
        _prune_consumed_nonces(now)
        if nonce_key in _consumed_state_nonces:
            msg = "OAuth state is invalid or expired"
            raise OAuthProviderError(msg)
        _consumed_state_nonces[nonce_key] = float(expires_at)
    return data


def _reset_oauth_state_for_tests() -> None:
    """Clear replay-prevention memory for tests."""
    with _consumed_nonce_lock:
        _consumed_state_nonces.clear()
