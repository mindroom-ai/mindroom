"""Persistent Matrix session lifecycle for the local desktop bridge."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast
from uuid import UUID, uuid4

import aiohttp
import nio
from nio.durable import DurableSync, DurableSyncConfig, open_durable_sync
from nio.store import DefaultStore

from mindroom.desktop.login_method import DesktopLoginMethod
from mindroom.durable_write import write_json_file_durable
from mindroom.file_locks import advisory_file_lock
from mindroom.matrix.client_session import (
    MindRoomAsyncClient,
    PermanentMatrixStartupError,
    create_matrix_http_client,
    login_flows,
    matrix_client_config,
    maybe_ssl_context,
    olm_store_dir,
    olm_store_exists,
)
from mindroom.matrix.cross_signing import ensure_agent_cross_signing
from mindroom.matrix.users import AgentMatrixUser

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from mindroom.constants import RuntimePaths


class DesktopSessionError(RuntimeError):
    """Desktop Matrix session state is missing, exposed, or invalid."""


class DesktopSessionNotFoundError(DesktopSessionError):
    """No saved desktop Matrix session exists at the configured path."""


@dataclass
class DesktopOwnedSession:
    """Own the exact crypto store, durable source, and HTTP client together."""

    client: nio.AsyncClient
    source: DurableSync

    async def close(self) -> None:
        """Release the source lease before closing its HTTP connection."""
        try:
            await self.source.close()
        finally:
            await self.client.close()


@dataclass(frozen=True, slots=True)
class DesktopMatrixSession:
    """Restorable Matrix device session without a persisted password."""

    homeserver: str
    user_id: str
    device_id: str
    access_token: str
    cloudflare_access: bool = False

    def to_payload(self) -> dict[str, str | int | bool]:
        """Serialize the minimum restorable device state."""
        payload: dict[str, str | int | bool] = {
            "v": 1,
            "homeserver": self.homeserver,
            "user_id": self.user_id,
            "device_id": self.device_id,
            "access_token": self.access_token,
        }
        if self.cloudflare_access:
            payload["cloudflare_access"] = True
        return payload

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
        cloudflare_access = payload.get("cloudflare_access", False)
        if not isinstance(cloudflare_access, bool):
            msg = "Desktop Matrix session field cloudflare_access must be a boolean."
            raise DesktopSessionError(msg)
        return cls(**values, cloudflare_access=cloudflare_access)


def desktop_session_path(runtime_paths: RuntimePaths) -> Path:
    """Return the private session path for the lightweight desktop client."""
    return runtime_paths.storage_root / "desktop_bridge" / "matrix_session.json"


def save_desktop_session(path: Path, session: DesktopMatrixSession) -> None:
    """Durably persist a Matrix access token with owner-only permissions."""
    write_json_file_durable(
        path,
        session.to_payload(),
        strict_atomic_replace=True,
        indent=2,
        sort_keys=True,
        trailing_newline=True,
    )
    path.chmod(0o600)


def load_desktop_session(path: Path) -> DesktopMatrixSession:
    """Validate and read one private regular file, refusing links on Unix."""
    flags = os.O_RDONLY
    if os.name != "nt":
        flags |= os.O_NONBLOCK | os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        msg = f"Desktop Matrix session not found at {path}. Run 'mindroom desktop login' first."
        raise DesktopSessionNotFoundError(msg) from exc
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            msg = f"Desktop Matrix session {path} must be a regular file."
            raise DesktopSessionError(msg)
        if os.name != "nt" and stat.S_IMODE(file_stat.st_mode) & 0o077:
            msg = f"Desktop Matrix session {path} must not be readable by group or other users."
            raise DesktopSessionError(msg)
        try:
            with os.fdopen(descriptor, encoding="utf-8", closefd=False) as stream:
                raw = json.load(stream)
        except (UnicodeError, json.JSONDecodeError) as exc:
            msg = f"Desktop Matrix session {path} is unreadable or malformed."
            raise DesktopSessionError(msg) from exc
    finally:
        os.close(descriptor)
    return DesktopMatrixSession.from_payload(raw)


def load_desktop_http_headers(path: Path | None) -> dict[str, str] | None:
    """Load optional secret HTTP headers for the desktop Matrix transport."""
    if path is None:
        return None
    path = path.expanduser()
    try:
        file_stat = path.stat()
    except FileNotFoundError as exc:
        msg = f"Desktop Matrix HTTP headers file not found at {path}."
        raise DesktopSessionError(msg) from exc
    if os.name != "nt" and stat.S_IMODE(file_stat.st_mode) & 0o077:
        msg = f"Desktop Matrix HTTP headers file {path} must not be readable by group or other users."
        raise DesktopSessionError(msg)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        msg = f"Desktop Matrix HTTP headers file {path} is unreadable or malformed."
        raise DesktopSessionError(msg) from exc
    if not isinstance(raw, dict) or any(
        not isinstance(name, str) or not name or not isinstance(value, str) for name, value in raw.items()
    ):
        msg = f"Desktop Matrix HTTP headers file {path} must contain one JSON object of string values."
        raise DesktopSessionError(msg)
    return cast("dict[str, str]", raw)


async def resolve_desktop_login_method(
    requested: DesktopLoginMethod,
    *,
    homeserver: str,
    runtime_paths: RuntimePaths,
    http_headers: Mapping[str, str] | None = None,
) -> DesktopLoginMethod:
    """Resolve automatic desktop login from methods advertised by Matrix."""
    if requested is not DesktopLoginMethod.AUTO:
        return requested
    try:
        flows = await login_flows(
            homeserver,
            runtime_paths,
            http_headers=http_headers,
        )
    except (
        PermanentMatrixStartupError,
        aiohttp.ClientError,
        OSError,
        TimeoutError,
        ValueError,
    ) as exc:
        msg = f"Could not discover Matrix login methods: {exc}"
        raise DesktopSessionError(msg) from exc
    if "m.login.sso" in flows:
        return DesktopLoginMethod.SSO
    if "m.login.password" in flows:
        return DesktopLoginMethod.PASSWORD
    advertised = ", ".join(sorted(flows)) or "none"
    msg = f"Matrix homeserver offers no supported desktop login method (advertised: {advertised})."
    raise DesktopSessionError(msg)


async def login_desktop_client(
    *,
    homeserver: str,
    user_id: str | None,
    runtime_paths: RuntimePaths,
    password: str | None = None,
    login_token: str | None = None,
    http_headers: Mapping[str, str] | None = None,
    cloudflare_access: bool = False,
) -> tuple[DesktopOwnedSession, DesktopMatrixSession]:
    """Exchange credentials without crypto, then open their single durable owner."""
    if (password is None) == (login_token is None):
        msg = "Desktop Matrix login requires exactly one password or SSO login token."
        raise DesktopSessionError(msg)
    if password is not None and user_id is None:
        msg = "Desktop Matrix password login requires --user-id."
        raise DesktopSessionError(msg)
    credentials = create_matrix_http_client(homeserver, runtime_paths, user_id or "", http_headers=http_headers)
    try:
        try:
            response = await credentials.login(
                password=password,
                token=login_token,
                device_name="MindRoom Desktop Bridge",
            )
        except (aiohttp.ClientError, OSError, TimeoutError, ValueError) as exc:
            msg = f"Desktop Matrix login failed: {exc}"
            raise DesktopSessionError(msg) from exc
        if not isinstance(response, nio.LoginResponse):
            msg = f"Desktop Matrix login failed: {response}"
            raise DesktopSessionError(msg)
        if user_id is not None and response.user_id != user_id:
            try:
                await credentials.logout()
            finally:
                msg = "Matrix login returned a different user than requested."
                raise DesktopSessionError(msg)
        session = DesktopMatrixSession(
            homeserver,
            response.user_id,
            response.device_id,
            response.access_token,
            cloudflare_access,
        )
    finally:
        await credentials.close()
    owner = await _open_owned_session(
        session,
        runtime_paths=runtime_paths,
        http_headers=http_headers,
        allow_create=True,
    )
    try:
        await prepare_desktop_client(owner.client)
        if password is not None:
            await ensure_agent_cross_signing(
                owner.client,
                AgentMatrixUser(
                    agent_name="desktop_bridge",
                    user_id=session.user_id,
                    display_name="MindRoom Desktop Bridge",
                    password=password,
                    device_id=session.device_id,
                    access_token=session.access_token,
                ),
            )
    except BaseException:
        await owner.close()
        raise
    return owner, session


async def open_desktop_client(
    session: DesktopMatrixSession,
    *,
    runtime_paths: RuntimePaths,
    http_headers: Mapping[str, str] | None = None,
) -> DesktopOwnedSession:
    """Adopt the exact existing crypto store before polling or key use."""
    return await _open_owned_session(
        session,
        runtime_paths=runtime_paths,
        http_headers=http_headers,
        allow_create=False,
    )


def desktop_transport_binding_path(runtime_paths: RuntimePaths, session: DesktopMatrixSession) -> Path:
    """Separate durable consumer identity for each exact account and device."""
    identity = json.dumps([session.homeserver, session.user_id, session.device_id]).encode()
    return runtime_paths.storage_root / "desktop_bridge" / "transport" / f"{hashlib.sha256(identity).hexdigest()}.json"


def _transport_binding(path: Path, session: DesktopMatrixSession) -> dict[str, object]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        path.parent.chmod(0o700)
    lock_path = path.with_suffix(".lock")
    with advisory_file_lock(lock_path):
        if os.name != "nt":
            lock_path.chmod(0o600)
        return _load_or_create_transport_binding(path, session)


def _load_or_create_transport_binding(path: Path, session: DesktopMatrixSession) -> dict[str, object]:
    identity = {"homeserver": session.homeserver, "user_id": session.user_id, "device_id": session.device_id}
    if path.exists():
        if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077:
            msg = "Desktop transport binding must be owner-only."
            raise DesktopSessionError(msg)
        try:
            binding = _parse_transport_binding(json.loads(path.read_text(encoding="utf-8")), identity)
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            msg = "Desktop transport binding is malformed or identifies a different device."
            raise DesktopSessionError(msg) from exc
        return binding
    # Persist consumer identity before adoption. A crash here leaves an unfinished
    # binding that the same consumer can complete on its next open.
    binding: dict[str, object] = {"v": 1, **identity, "consumer_id": str(uuid4()), "stream_id": None}
    write_json_file_durable(path, binding, strict_atomic_replace=True)
    return binding


def _parse_transport_binding(raw: object, identity: dict[str, str]) -> dict[str, object]:
    if not isinstance(raw, dict):
        msg = "Transport binding must be an object."
        raise TypeError(msg)
    binding = cast("dict[str, object]", raw)
    if binding.get("v") != 1 or any(binding.get(key) != value for key, value in identity.items()):
        msg = "Transport binding identity does not match."
        raise ValueError(msg)
    for key in ("consumer_id", "stream_id"):
        value = binding[key]
        if key == "stream_id" and value is None:
            continue
        if not isinstance(value, str):
            msg = "Transport binding identifiers must be strings."
            raise TypeError(msg)
        UUID(value)
    return binding


def _bind_stream(path: Path, binding: dict[str, object], source: DurableSync) -> None:
    if binding["stream_id"] is not None and binding["stream_id"] != str(source.stream_id):
        msg = "Desktop transport binding identifies a different durable stream."
        raise DesktopSessionError(msg)
    if binding["stream_id"] is None:
        binding["stream_id"] = str(source.stream_id)
        write_json_file_durable(path, binding, strict_atomic_replace=True)


async def _open_owned_session(
    session: DesktopMatrixSession,
    *,
    runtime_paths: RuntimePaths,
    allow_create: bool,
    http_headers: Mapping[str, str] | None = None,
) -> DesktopOwnedSession:
    if not allow_create and not olm_store_exists(session.user_id, session.device_id, runtime_paths):
        msg = "Desktop Matrix encryption store is missing; run 'mindroom desktop login --replace' for a fresh device."
        raise DesktopSessionError(msg)
    path = desktop_transport_binding_path(runtime_paths, session)
    binding = _transport_binding(path, session)
    store_path = olm_store_dir(session.user_id, runtime_paths)
    store_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        store_path.chmod(0o700)
    # MindRoomAsyncClient prepares dynamic Cloudflare Access headers before every request.
    client = MindRoomAsyncClient(
        session.homeserver,
        session.user_id,
        device_id=session.device_id,
        config=matrix_client_config(http_headers=http_headers),
        ssl=maybe_ssl_context(session.homeserver, runtime_paths=runtime_paths),
    )
    # restore_login would eagerly open an ordinary store. Durable ownership must
    # attach to an authenticated client whose store has never been loaded.
    client.user_id, client.device_id, client.access_token = session.user_id, session.device_id, session.access_token
    source: DurableSync | None = None
    try:
        source = open_durable_sync(
            client,
            consumer_id=UUID(str(binding["consumer_id"])),
            store_path=store_path,
            source_store_class=DefaultStore,
            config=DurableSyncConfig(to_device_only=True),
        )
        _bind_stream(path, binding, source)
    except BaseException:
        try:
            if source is not None:
                await source.close()
        finally:
            await client.close()
        raise
    return DesktopOwnedSession(client, source)


async def prepare_desktop_client(client: nio.AsyncClient) -> None:
    """Publish encryption keys without polling or acknowledging any commands."""
    if client.olm is None:
        msg = "Desktop Matrix client started without Olm encryption support."
        raise DesktopSessionError(msg)
    if client.should_upload_keys:
        upload = await client.keys_upload()
        if isinstance(upload, nio.KeysUploadError):
            msg = f"Desktop Matrix encryption-key upload failed: {upload}"
            raise DesktopSessionError(msg)


def client_ed25519_fingerprint(client: nio.AsyncClient) -> str:
    """Return the local device fingerprint sent through chat pairing."""
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
    "DesktopOwnedSession",
    "DesktopSessionError",
    "DesktopSessionNotFoundError",
    "client_ed25519_fingerprint",
    "desktop_session_path",
    "desktop_transport_binding_path",
    "load_desktop_http_headers",
    "load_desktop_session",
    "login_desktop_client",
    "open_desktop_client",
    "prepare_desktop_client",
    "resolve_desktop_login_method",
    "save_desktop_session",
]
