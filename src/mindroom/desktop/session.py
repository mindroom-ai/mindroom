"""Persistent Matrix session lifecycle for the local desktop bridge."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import nio

from mindroom.durable_write import write_json_file_durable
from mindroom.matrix.client_session import login, olm_store_exists, restore_login
from mindroom.matrix.cross_signing import ensure_agent_cross_signing
from mindroom.matrix.users import AgentMatrixUser

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


class DesktopSessionError(RuntimeError):
    """Desktop Matrix session state is missing, exposed, or invalid."""


@dataclass(frozen=True, slots=True)
class DesktopMatrixSession:
    """Restorable Matrix device session without a persisted password."""

    homeserver: str
    user_id: str
    device_id: str
    access_token: str

    def to_payload(self) -> dict[str, str | int]:
        """Serialize the minimum restorable device state."""
        return {
            "v": 1,
            "homeserver": self.homeserver,
            "user_id": self.user_id,
            "device_id": self.device_id,
            "access_token": self.access_token,
        }

    @classmethod
    def from_payload(cls, raw: object) -> DesktopMatrixSession:
        """Parse one strict persisted session payload."""
        if not isinstance(raw, dict):
            msg = "Desktop Matrix session has an unsupported format."
            raise DesktopSessionError(msg)
        payload = cast("dict[str, object]", raw)
        if payload.get("v") != 1:
            msg = "Desktop Matrix session has an unsupported format."
            raise DesktopSessionError(msg)
        values: dict[str, str] = {}
        for key in ("homeserver", "user_id", "device_id", "access_token"):
            value = payload.get(key)
            if not isinstance(value, str) or not value:
                msg = f"Desktop Matrix session field {key} is missing."
                raise DesktopSessionError(msg)
            values[key] = value
        return cls(**values)


def desktop_session_path(runtime_paths: RuntimePaths) -> Path:
    """Return the private session path for the lightweight desktop client."""
    return runtime_paths.storage_root / "desktop_bridge" / "matrix_session.json"


def save_desktop_session(path: Path, session: DesktopMatrixSession) -> None:
    """Durably persist a Matrix access token with owner-only permissions."""
    write_json_file_durable(path, session.to_payload(), indent=2, sort_keys=True, trailing_newline=True)
    path.chmod(0o600)


def load_desktop_session(path: Path) -> DesktopMatrixSession:
    """Load one private Matrix session, refusing permissive Unix modes."""
    try:
        file_stat = path.stat()
    except FileNotFoundError as exc:
        msg = f"Desktop Matrix session not found at {path}. Run 'mindroom desktop login' first."
        raise DesktopSessionError(msg) from exc
    if os.name != "nt" and stat.S_IMODE(file_stat.st_mode) & 0o077:
        msg = f"Desktop Matrix session {path} must not be readable by group or other users."
        raise DesktopSessionError(msg)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        msg = f"Desktop Matrix session {path} is unreadable or malformed."
        raise DesktopSessionError(msg) from exc
    return DesktopMatrixSession.from_payload(raw)


async def login_desktop_client(
    *,
    homeserver: str,
    user_id: str,
    password: str,
    runtime_paths: RuntimePaths,
) -> tuple[nio.AsyncClient, DesktopMatrixSession]:
    """Create a fresh cross-signed Matrix desktop device and its restorable session."""
    client = await login(homeserver, user_id, password, runtime_paths)
    try:
        await _prepare_crypto(client)
        authenticated_session = _session_from_authenticated_client(client, homeserver=homeserver)
        await ensure_agent_cross_signing(
            client,
            AgentMatrixUser(
                agent_name="desktop_bridge",
                user_id=authenticated_session.user_id,
                display_name="MindRoom Desktop Bridge",
                password=password,
                device_id=authenticated_session.device_id,
                access_token=authenticated_session.access_token,
            ),
        )
    except Exception:
        await client.close()
        raise
    return client, authenticated_session


def _session_from_authenticated_client(client: nio.AsyncClient, *, homeserver: str) -> DesktopMatrixSession:
    if client.user_id is None or client.device_id is None or client.access_token is None:
        msg = "Matrix login did not return complete desktop device credentials."
        raise DesktopSessionError(msg)
    return DesktopMatrixSession(
        homeserver=homeserver,
        user_id=client.user_id,
        device_id=client.device_id,
        access_token=client.access_token,
    )


async def restore_desktop_client(
    session: DesktopMatrixSession,
    *,
    runtime_paths: RuntimePaths,
) -> nio.AsyncClient:
    """Restore the exact desktop Matrix device and its Olm identity."""
    client = await open_desktop_client(session, runtime_paths=runtime_paths)
    try:
        await prepare_desktop_client(client)
    except Exception:
        await client.close()
        raise
    return client


async def open_desktop_client(
    session: DesktopMatrixSession,
    *,
    runtime_paths: RuntimePaths,
) -> nio.AsyncClient:
    """Restore a desktop Matrix client before consuming queued sync events."""
    if not olm_store_exists(session.user_id, session.device_id, runtime_paths):
        msg = "Desktop Matrix encryption store is missing; run 'mindroom desktop login --replace' for a fresh device."
        raise DesktopSessionError(msg)
    return await restore_login(
        session.homeserver,
        session.user_id,
        session.device_id,
        session.access_token,
        runtime_paths,
    )


async def prepare_desktop_client(client: nio.AsyncClient) -> None:
    """Prepare Olm only after the caller has registered command callbacks."""
    await _prepare_crypto(client)


async def _prepare_crypto(client: nio.AsyncClient) -> None:
    """Load the crypto store, publish keys, and establish a current sync token."""
    response = await client.sync(timeout=0, full_state=False, set_presence="offline")
    if isinstance(response, nio.SyncError):
        msg = f"Desktop Matrix initial sync failed: {response}"
        raise DesktopSessionError(msg)
    if client.should_upload_keys:
        upload = await client.keys_upload()
        if isinstance(upload, nio.KeysUploadError):
            msg = f"Desktop Matrix encryption-key upload failed: {upload}"
            raise DesktopSessionError(msg)
    if client.olm is None:
        msg = "Desktop Matrix client started without Olm encryption support."
        raise DesktopSessionError(msg)


def client_ed25519_fingerprint(client: nio.AsyncClient) -> str:
    """Return the local device fingerprint users pin in cloud tool config."""
    if client.olm is None:
        msg = "Desktop Matrix client has no Olm identity."
        raise DesktopSessionError(msg)
    fingerprint = client.olm.account.identity_keys.get("ed25519")
    if not isinstance(fingerprint, str) or not fingerprint:
        msg = "Desktop Matrix client has no ed25519 identity key."
        raise DesktopSessionError(msg)
    return fingerprint


__all__ = [
    "DesktopMatrixSession",
    "DesktopSessionError",
    "client_ed25519_fingerprint",
    "desktop_session_path",
    "load_desktop_session",
    "login_desktop_client",
    "open_desktop_client",
    "prepare_desktop_client",
    "restore_desktop_client",
    "save_desktop_session",
]
