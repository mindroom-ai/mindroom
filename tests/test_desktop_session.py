"""Tests for private local desktop Matrix sessions."""

# ruff: noqa: S106 - These are test-only credential values.

from __future__ import annotations

import json
import os
import stat
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, Event
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import aiohttp
import nio
import pytest

from mindroom.desktop.session import (
    DesktopLoginMethod,
    DesktopMatrixSession,
    DesktopSessionError,
    _open_owned_session,
    _transport_binding,
    desktop_transport_binding_path,
    load_desktop_http_headers,
    load_desktop_session,
    login_desktop_client,
    open_desktop_client,
    prepare_desktop_client,
    resolve_desktop_login_method,
    save_desktop_session,
)
from tests.conftest import test_runtime_paths as make_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


def _session() -> DesktopMatrixSession:
    return DesktopMatrixSession(
        homeserver="https://matrix.example.org",
        user_id="@desktop:example.org",
        device_id="DESKTOP",
        access_token="secret-access-token",
    )


def test_session_round_trip_uses_owner_only_permissions(tmp_path: Path) -> None:
    """The reusable Matrix token is never persisted with ambient read access."""
    path = tmp_path / "desktop" / "matrix_session.json"

    save_desktop_session(path, _session())

    assert load_desktop_session(path) == _session()
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_session_round_trip_remembers_interactive_access_transport(
    tmp_path: Path,
) -> None:
    """Bridge startup can renew Access without requiring the flag again."""
    path = tmp_path / "desktop" / "matrix_session.json"
    session = DesktopMatrixSession(
        homeserver="https://matrix.example.org",
        user_id="@desktop:example.org",
        device_id="DESKTOP",
        access_token="secret-access-token",
        cloudflare_access=True,
    )

    save_desktop_session(path, session)

    assert load_desktop_session(path) == session


def test_session_metadata_update_preserves_exact_saved_credentials(tmp_path: Path) -> None:
    """Successful setup can persist Access transport without replacing its login."""
    path = tmp_path / "desktop" / "matrix_session.json"
    original = _session()
    save_desktop_session(path, original)
    updated = replace(original, cloudflare_access=True)

    save_desktop_session(path, updated, expected_session=original)

    assert load_desktop_session(path) == updated
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "changed",
    [
        {"homeserver": "https://other.example.org"},
        {"user_id": "@other:example.org"},
        {"device_id": "NEW-DEVICE"},
        {"access_token": "replacement-token"},
        {"cloudflare_access": True},
    ],
)
def test_session_metadata_update_rejects_changed_saved_session(tmp_path: Path, changed: dict[str, object]) -> None:
    """Any concurrent login or metadata change invalidates the setup snapshot."""
    path = tmp_path / "matrix_session.json"
    original = _session()
    current = replace(original, **changed)
    save_desktop_session(path, current)
    before = path.read_bytes()

    with pytest.raises(DesktopSessionError, match="changed"):
        save_desktop_session(path, replace(original, cloudflare_access=True), expected_session=original)

    assert path.read_bytes() == before
    assert load_desktop_session(path) == current


def test_session_login_writer_waits_for_pending_metadata_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An older setup write cannot overwrite a login published during its transaction."""
    from mindroom.desktop import session as session_module  # noqa: PLC0415

    path = tmp_path / "matrix_session.json"
    original = _session()
    updated = replace(original, cloudflare_access=True)
    new_login = replace(original, device_id="NEW-DEVICE", access_token="replacement-token")
    save_desktop_session(path, original)
    metadata_writing = Event()
    release_metadata = Event()
    login_started = Event()
    login_finished = Event()
    write = session_module.write_json_file_durable

    def pause_metadata_write(target: Path, payload: object, **kwargs: object) -> None:
        if payload == updated.to_payload():
            metadata_writing.set()
            assert release_metadata.wait(timeout=5)
        write(target, payload, **kwargs)

    def login() -> None:
        login_started.set()
        save_desktop_session(path, new_login)
        login_finished.set()

    monkeypatch.setattr(session_module, "write_json_file_durable", pause_metadata_write)
    with ThreadPoolExecutor(max_workers=2) as pool:
        metadata = pool.submit(save_desktop_session, path, updated, expected_session=original)
        try:
            assert metadata_writing.wait(timeout=2)
            latest = pool.submit(login)
            assert login_started.wait(timeout=2)
            login_finished.wait(timeout=0.05)
        finally:
            release_metadata.set()
        metadata.result(timeout=2)
        latest.result(timeout=2)

    assert load_desktop_session(path) == new_login


@pytest.mark.parametrize("kind", ["missing", "directory", "malformed", "exposed"])
def test_session_metadata_update_preserves_private_read_checks(tmp_path: Path, kind: str) -> None:
    """Conditional writes cannot repair or overwrite an unreadable session implicitly."""
    if kind == "exposed" and os.name == "nt":
        pytest.skip("Unix permission bits are not authoritative on Windows")
    path = tmp_path / "matrix_session.json"
    if kind == "directory":
        path.mkdir()
    elif kind == "malformed":
        path.write_text("{", encoding="utf-8")
        path.chmod(0o600)
    elif kind == "exposed":
        save_desktop_session(path, _session())
        path.chmod(0o640)
    before = path.read_bytes() if path.is_file() else None

    with pytest.raises(DesktopSessionError):
        save_desktop_session(path, replace(_session(), cloudflare_access=True), expected_session=_session())

    if before is not None:
        assert path.read_bytes() == before
    elif kind == "directory":
        assert path.is_dir()
    else:
        assert not path.exists()


@pytest.mark.skipif(
    os.name == "nt",
    reason="Unix permission bits are not authoritative on Windows",
)
def test_session_refuses_group_readable_token(tmp_path: Path) -> None:
    """An accidentally exposed token stops the bridge instead of being used."""
    path = tmp_path / "matrix_session.json"
    path.write_text(json.dumps(_session().to_payload()), encoding="utf-8")
    path.chmod(0o640)

    with pytest.raises(DesktopSessionError, match="must not be readable"):
        load_desktop_session(path)


def test_session_rejects_malformed_payload(tmp_path: Path) -> None:
    """Incomplete credentials never reach the Matrix client."""
    path = tmp_path / "matrix_session.json"
    path.write_text('{"v": 1, "user_id": "@desktop:example.org"}', encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(DesktopSessionError, match="field homeserver"):
        load_desktop_session(path)


def test_session_missing_path_has_setup_instruction(tmp_path: Path) -> None:
    """A genuinely absent session gets the actionable login instruction."""
    with pytest.raises(DesktopSessionError, match="desktop login"):
        load_desktop_session(tmp_path / "missing.json")


def test_session_rejects_directory_before_reading(tmp_path: Path) -> None:
    """Every consumer rejects non-regular session paths."""
    path = tmp_path / "matrix_session.json"
    path.mkdir(mode=0o700)

    with pytest.raises(DesktopSessionError, match="regular file"):
        load_desktop_session(path)


def test_session_save_never_copies_token_into_directory(tmp_path: Path) -> None:
    """A failed publication must not move the secret to an unintended path."""
    path = tmp_path / "matrix_session.json"
    path.mkdir(mode=0o700)
    try:
        with pytest.raises(IsADirectoryError):
            save_desktop_session(path, _session())
    finally:
        path.chmod(0o700)
    assert list(path.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="Unix descriptor and FIFO semantics")
def test_session_reads_validated_descriptor_when_path_is_swapped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A path swap cannot redirect the read after the opened file is checked."""
    path = tmp_path / "matrix_session.json"
    save_desktop_session(path, _session())
    fstat = os.fstat

    def swap_after_inspection(descriptor: int) -> os.stat_result:
        result = fstat(descriptor)
        path.unlink()
        os.mkfifo(path, 0o600)
        return result

    monkeypatch.setattr(os, "fstat", swap_after_inspection)

    assert load_desktop_session(path) == _session()
    assert stat.S_ISFIFO(path.stat().st_mode)


@pytest.mark.skipif(os.name == "nt", reason="Unix no-follow semantics")
def test_session_refuses_symlink(tmp_path: Path) -> None:
    """The saved session path must name the credential file itself."""
    target = tmp_path / "target.json"
    save_desktop_session(target, _session())
    path = tmp_path / "matrix_session.json"
    path.symlink_to(target)

    with pytest.raises(OSError, match="Too many levels of symbolic links"):
        load_desktop_session(path)


def test_session_preserves_unexpected_filesystem_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Permission and device errors retain their native exception and traceback."""
    path = tmp_path / "matrix_session.json"
    path.write_text(json.dumps(_session().to_payload()), encoding="utf-8")
    path.chmod(0o600)

    def denied(*_args: object, **_kwargs: object) -> str:
        message = "test permission failure"
        raise PermissionError(message)

    monkeypatch.setattr(os, "open", denied)

    with pytest.raises(PermissionError, match="test permission failure"):
        load_desktop_session(path)


def test_http_headers_file_loads_string_mapping(tmp_path: Path) -> None:
    """Proxy credentials remain in a separate private file instead of session state."""
    path = tmp_path / "matrix-http-headers.json"
    path.write_text('{"X-Access-Client": "test-secret"}', encoding="utf-8")
    path.chmod(0o600)

    assert load_desktop_http_headers(path) == {"X-Access-Client": "test-secret"}


@pytest.mark.skipif(
    os.name == "nt",
    reason="Unix permission bits are not authoritative on Windows",
)
def test_http_headers_file_refuses_group_readable_secrets(tmp_path: Path) -> None:
    """An exposed proxy credential file stops before any Matrix request."""
    path = tmp_path / "matrix-http-headers.json"
    path.write_text('{"X-Access-Client": "test-secret"}', encoding="utf-8")
    path.chmod(0o640)

    with pytest.raises(DesktopSessionError, match="must not be readable"):
        load_desktop_http_headers(path)


@pytest.mark.parametrize("payload", ['["not-an-object"]', '{"X-Access-Client": 1}'])
def test_http_headers_file_requires_string_mapping(
    tmp_path: Path,
    payload: str,
) -> None:
    """Malformed header configuration fails before nio receives it."""
    path = tmp_path / "matrix-http-headers.json"
    path.write_text(payload, encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(DesktopSessionError, match="JSON object of string values"):
        load_desktop_http_headers(path)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("flows", "expected"),
    [
        (("m.login.password", "m.login.sso"), DesktopLoginMethod.SSO),
        (("m.login.password",), DesktopLoginMethod.PASSWORD),
        (("m.login.token", "m.login.sso"), DesktopLoginMethod.SSO),
    ],
)
async def test_auto_login_method_uses_advertised_matrix_flows(
    monkeypatch: pytest.MonkeyPatch,
    flows: tuple[str, ...],
    expected: DesktopLoginMethod,
) -> None:
    """Auto prefers browser SSO and falls back to password-only homeservers."""
    query = AsyncMock(return_value=flows)
    monkeypatch.setattr("mindroom.desktop.session.login_flows", query)
    runtime_paths = SimpleNamespace()

    resolved = await resolve_desktop_login_method(
        DesktopLoginMethod.AUTO,
        homeserver="https://matrix.example.org",
        runtime_paths=runtime_paths,
        http_headers={"X-Access-Client": "test-secret"},
    )

    assert resolved is expected
    query.assert_awaited_once_with(
        "https://matrix.example.org",
        runtime_paths,
        http_headers={"X-Access-Client": "test-secret"},
    )


@pytest.mark.asyncio
async def test_explicit_login_method_skips_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operators can force SSO when a homeserver also advertises password login."""
    query = AsyncMock()
    monkeypatch.setattr("mindroom.desktop.session.login_flows", query)

    resolved = await resolve_desktop_login_method(
        DesktopLoginMethod.SSO,
        homeserver="https://matrix.example.org",
        runtime_paths=SimpleNamespace(),
    )

    assert resolved is DesktopLoginMethod.SSO
    query.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_login_method_rejects_unsupported_flows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Application-service-only servers produce a clear local setup error."""
    monkeypatch.setattr(
        "mindroom.desktop.session.login_flows",
        AsyncMock(return_value=("m.login.application_service",)),
    )

    with pytest.raises(DesktopSessionError, match=r"m\.login\.application_service"):
        await resolve_desktop_login_method(
            DesktopLoginMethod.AUTO,
            homeserver="https://matrix.example.org",
            runtime_paths=SimpleNamespace(),
        )


@pytest.mark.asyncio
async def test_auto_login_method_translates_network_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Login discovery reports transport failure as one actionable desktop error."""
    monkeypatch.setattr(
        "mindroom.desktop.session.login_flows",
        AsyncMock(side_effect=aiohttp.ClientConnectionError("homeserver unavailable")),
    )

    with pytest.raises(
        DesktopSessionError,
        match=r"Could not discover.*homeserver unavailable",
    ):
        await resolve_desktop_login_method(
            DesktopLoginMethod.AUTO,
            homeserver="https://matrix.example.org",
            runtime_paths=SimpleNamespace(),
        )


@pytest.mark.asyncio
async def test_crypto_preparation_uploads_keys_without_sync() -> None:
    """Preparing encryption must not consume commands outside durable admission."""
    client = SimpleNamespace(sync=AsyncMock(), should_upload_keys=True, keys_upload=AsyncMock(), olm=object())

    await prepare_desktop_client(client)

    client.sync.assert_not_awaited()
    client.keys_upload.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("token_login", [False, True])
async def test_login_acquires_credentials_without_crypto_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    token_login: bool,
) -> None:
    """Credential exchange cannot adopt a store before the exact device is known."""
    calls = []

    async def credential_login(
        client: nio.AsyncClient,
        password: str | None = None,
        **kwargs: object,
    ) -> nio.LoginResponse:
        assert not client.config.encryption_enabled
        assert client.store is None
        assert client.store_path is None
        assert client.config.custom_headers == {"X-Access-Client": "test-secret"}
        calls.append({**kwargs, "password": password})
        return nio.LoginResponse(user_id="@desktop:example.org", device_id="DESKTOP", access_token="issued-token")

    async def reject_sync(*_args: object, **_kwargs: object) -> None:
        msg = "ordinary sync consumed desktop input"
        raise AssertionError(msg)

    monkeypatch.setattr(nio.AsyncClient, "login", credential_login)
    monkeypatch.setattr(nio.AsyncClient, "sync", reject_sync)
    monkeypatch.setattr(nio.AsyncClient, "keys_upload", AsyncMock())
    monkeypatch.setattr("mindroom.desktop.session.ensure_agent_cross_signing", AsyncMock())
    owner, session = await login_desktop_client(
        homeserver="https://matrix.example.org",
        user_id=None if token_login else "@desktop:example.org",
        password=None if token_login else "password",
        login_token="sso-token" if token_login else None,
        http_headers={"X-Access-Client": "test-secret"},
        runtime_paths=make_runtime_paths(tmp_path),
    )
    try:
        assert session.user_id == "@desktop:example.org"
        assert session.device_id == "DESKTOP"
        assert owner.client.olm is not None
        assert owner.client.store is not None
        assert owner.client.config.custom_headers == {"X-Access-Client": "test-secret"}
        assert owner.source.cursor is None
        assert calls[0]["token" if token_login else "password"] == ("sso-token" if token_login else "password")
    finally:
        await owner.close()


@pytest.mark.asyncio
async def test_login_translates_expected_authentication_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Credential rejection becomes an actionable session error without opening crypto."""
    monkeypatch.setattr(
        nio.AsyncClient,
        "login",
        AsyncMock(return_value=nio.LoginError("invalid credentials", "M_FORBIDDEN")),
    )
    with pytest.raises(DesktopSessionError, match="invalid credentials"):
        await login_desktop_client(
            homeserver="https://matrix.example.org",
            user_id="@desktop:example.org",
            password="wrong",
            runtime_paths=make_runtime_paths(tmp_path),
        )


@pytest.mark.asyncio
async def test_login_translates_network_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Credential network failures are reported without opening a persistent store."""
    monkeypatch.setattr(
        nio.AsyncClient,
        "login",
        AsyncMock(side_effect=aiohttp.ClientConnectionError("connection refused")),
    )
    with pytest.raises(DesktopSessionError, match="connection refused"):
        await login_desktop_client(
            homeserver="https://matrix.example.org",
            user_id=None,
            login_token="sso-token",
            runtime_paths=make_runtime_paths(tmp_path),
        )


@pytest.mark.asyncio
async def test_saved_session_requires_existing_crypto_store(tmp_path: Path) -> None:
    """Restore cannot silently replace the established encryption identity."""
    with pytest.raises(DesktopSessionError, match="encryption store is missing"):
        await open_desktop_client(_session(), runtime_paths=make_runtime_paths(tmp_path))


@pytest.mark.asyncio
async def test_owned_session_preserves_binding_and_identity_across_reopen(tmp_path: Path) -> None:
    """The same consumer, stream, and Olm account survive the owner lifecycle."""
    runtime_paths = make_runtime_paths(tmp_path)
    owner = await _open_owned_session(_session(), runtime_paths=runtime_paths, allow_create=True)
    fingerprint = owner.client.olm.account.identity_keys["ed25519"]
    stream = owner.source.stream_id
    assert owner.source.config.to_device_only is True
    assert owner.source.config.sync_filter is None
    await owner.close()
    restored = await open_desktop_client(_session(), runtime_paths=runtime_paths)
    try:
        assert restored.source.stream_id == stream
        assert restored.client.olm.account.identity_keys["ed25519"] == fingerprint
    finally:
        await restored.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["user_id", "device_id", "stream_id"])
async def test_owned_session_rejects_mismatched_binding(tmp_path: Path, field: str) -> None:
    """Account, device, and stream substitutions fail before desktop consumption."""
    runtime_paths = make_runtime_paths(tmp_path)
    owner = await _open_owned_session(_session(), runtime_paths=runtime_paths, allow_create=True)
    await owner.close()
    path = desktop_transport_binding_path(runtime_paths, _session())
    payload = json.loads(path.read_text())
    payload[field] = "00000000-0000-0000-0000-000000000001" if field == "stream_id" else "different"
    path.write_text(json.dumps(payload))
    with pytest.raises(DesktopSessionError, match="binding"):
        await open_desktop_client(_session(), runtime_paths=runtime_paths)


def test_concurrent_first_open_preserves_one_consumer_binding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Concurrent processes must not persist competing consumer identities before adoption."""
    from mindroom.desktop import session as session_module  # noqa: PLC0415

    write = session_module.write_json_file_durable
    start = Barrier(8)

    def delayed_write(*args: object, **kwargs: object) -> None:
        # Enlarge the competing read/write window without replacing filesystem persistence.
        time.sleep(0.01)
        write(*args, **kwargs)

    def create() -> dict[str, object]:
        start.wait()
        return _transport_binding(tmp_path / "binding.json", _session())

    monkeypatch.setattr(session_module, "write_json_file_durable", delayed_write)
    with ThreadPoolExecutor(max_workers=8) as executor:
        bindings = list(executor.map(lambda _index: create(), range(8)))
    assert len({binding["consumer_id"] for binding in bindings}) == 1
    assert json.loads((tmp_path / "binding.json").read_text()) == bindings[0]


@pytest.mark.asyncio
async def test_token_login_revokes_unexpected_account_before_adoption(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The owned login path cannot enroll an account other than the requested one."""
    revoked = []

    async def login(client: nio.AsyncClient, **_kwargs: object) -> nio.LoginResponse:
        response = nio.LoginResponse("@wrong:example.org", "WRONG", "issued-token")
        await client.receive_response(response)
        return response

    async def logout(client: nio.AsyncClient) -> nio.LogoutResponse:
        revoked.append((client.user_id, client.device_id))
        return nio.LogoutResponse()

    monkeypatch.setattr(nio.AsyncClient, "login", login)
    monkeypatch.setattr(nio.AsyncClient, "logout", logout)
    monkeypatch.setattr(nio.AsyncClient, "keys_upload", AsyncMock())
    runtime_paths = make_runtime_paths(tmp_path)
    wrong_session = DesktopMatrixSession("https://matrix.example.org", "@wrong:example.org", "WRONG", "issued-token")
    with pytest.raises(DesktopSessionError, match="different user"):
        await login_desktop_client(
            homeserver="https://matrix.example.org",
            user_id="@desktop:example.org",
            login_token="sso-token",
            runtime_paths=runtime_paths,
        )
    assert revoked == [("@wrong:example.org", "WRONG")]
    assert not desktop_transport_binding_path(runtime_paths, wrong_session).exists()
