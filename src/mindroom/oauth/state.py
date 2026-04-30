"""Opaque server-side OAuth state token helpers."""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import threading
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from mindroom.oauth.providers import OAuthProviderError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths

_oauth_state_lock = threading.Lock()
_OAUTH_STATE_FILE_NAME = "oauth_state.json"


def _state_file(runtime_paths: RuntimePaths) -> Path:
    return runtime_paths.storage_root / _OAUTH_STATE_FILE_NAME


def _state_lock_file(runtime_paths: RuntimePaths) -> Path:
    return runtime_paths.storage_root / f"{_OAUTH_STATE_FILE_NAME}.lock"


@contextmanager
def _locked_state_store(runtime_paths: RuntimePaths, *, now: float) -> Iterator[dict[str, dict[str, Any]]]:
    with _oauth_state_lock:
        lock_path = _state_lock_file(runtime_paths)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                states = _load_state_store(runtime_paths, now=now)
                yield states
                _save_state_store(runtime_paths, states)
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _load_state_store(runtime_paths: RuntimePaths, *, now: float) -> dict[str, dict[str, Any]]:
    path = _state_file(runtime_paths)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    states = raw.get("states")
    if not isinstance(states, dict):
        return {}
    pruned: dict[str, dict[str, Any]] = {}
    for token, record in states.items():
        if not isinstance(token, str) or not isinstance(record, dict):
            continue
        expires_at = record.get("exp")
        if isinstance(expires_at, int | float) and expires_at > now:
            pruned[token] = record
    return pruned


def _save_state_store(runtime_paths: RuntimePaths, states: dict[str, dict[str, Any]]) -> None:
    path = _state_file(runtime_paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp-{os.getpid()}-{uuid4().hex}")
    tmp_path.write_text(json.dumps({"states": states}, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    tmp_path.replace(path)


def issue_opaque_oauth_state(
    runtime_paths: RuntimePaths,
    *,
    kind: str,
    ttl_seconds: int,
    data: dict[str, Any],
) -> str:
    """Return one opaque, time-limited OAuth state token."""
    now = time.time()
    token = secrets.token_urlsafe(32)
    record = {
        "kind": kind,
        "iat": now,
        "exp": now + ttl_seconds,
        "data": dict(data),
    }
    with _locked_state_store(runtime_paths, now=now) as states:
        states[token] = record
    return token


def read_opaque_oauth_state(
    runtime_paths: RuntimePaths,
    *,
    kind: str,
    token: str,
) -> dict[str, Any]:
    """Return one server-side OAuth state payload without consuming it."""
    now = time.time()
    with _locked_state_store(runtime_paths, now=now) as states:
        record = states.get(token)

    if not isinstance(record, dict):
        msg = "OAuth state is invalid or expired"
        raise OAuthProviderError(msg)
    if record.get("kind") != kind:
        msg = "OAuth state does not match this integration"
        raise OAuthProviderError(msg)
    expires_at = record.get("exp")
    if not isinstance(expires_at, int | float):
        msg = "OAuth state is invalid or expired"
        raise OAuthProviderError(msg)
    if expires_at <= now:
        msg = "OAuth state is invalid or expired"
        raise OAuthProviderError(msg)

    data = record.get("data")
    if not isinstance(data, dict):
        msg = "OAuth state is invalid or expired"
        raise OAuthProviderError(msg)
    return data


def consume_opaque_oauth_state(
    runtime_paths: RuntimePaths,
    *,
    kind: str,
    token: str,
) -> dict[str, Any]:
    """Return and remove one server-side OAuth state payload."""
    now = time.time()
    with _locked_state_store(runtime_paths, now=now) as states:
        record = states.pop(token, None)
    if not isinstance(record, dict):
        msg = "OAuth state is invalid or expired"
        raise OAuthProviderError(msg)
    if record.get("kind") != kind:
        msg = "OAuth state does not match this integration"
        raise OAuthProviderError(msg)
    expires_at = record.get("exp")
    if not isinstance(expires_at, int | float) or expires_at <= now:
        msg = "OAuth state is invalid or expired"
        raise OAuthProviderError(msg)
    data = record.get("data")
    if not isinstance(data, dict):
        msg = "OAuth state is invalid or expired"
        raise OAuthProviderError(msg)
    return data
