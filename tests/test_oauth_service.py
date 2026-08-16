"""Tests for the serialized OAuth credential lifecycle."""

from __future__ import annotations

import asyncio
import base64
import shutil
import sqlite3
import threading
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import TYPE_CHECKING, cast

import pytest

from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.credentials import (
    get_runtime_credentials_manager,
    save_scoped_credentials,
    scoped_credentials_path,
)
from mindroom.oauth import credential_lifecycle, credential_store, reset_execution
from mindroom.oauth.credential_lifecycle import (
    OAuthCredentialConflictError,
    OAuthCredentialContext,
    exchange_and_store_oauth_credentials,
    load_oauth_credentials_snapshot,
    oauth_connection_generation,
    refresh_oauth_credentials_sync,
    refresh_oauth_credentials_with_result,
)
from mindroom.oauth.credential_store import OAuthCredentialTransaction, _oauth_credential_database_path
from mindroom.oauth.providers import (
    OAuthClientConfig,
    OAuthProvider,
    OAuthProviderError,
    OAuthRefreshRejectedError,
    OAuthTokenResult,
)
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
    from pathlib import Path
    from typing import Any

    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

ACCESS_0 = "access-refresh-0"
CHAIN_0 = "refresh-0"
CHAIN_1 = "refresh-1"
INVALID_ROTATION = "invalid_refresh_token"
FUTURE_EXPIRES_AT = 4_102_444_800.0


class _CapturingLogger:
    def __init__(self) -> None:
        self.debug_calls: list[tuple[str, dict[str, object]]] = []
        self.info_calls: list[tuple[str, dict[str, object]]] = []
        self.warning_calls: list[tuple[str, dict[str, object]]] = []

    def debug(self, event: str, **kwargs: object) -> None:
        self.debug_calls.append((event, kwargs))

    def info(self, event: str, **kwargs: object) -> None:
        self.info_calls.append((event, kwargs))

    def warning(self, event: str, **kwargs: object) -> None:
        self.warning_calls.append((event, kwargs))


class _FakeOAuthProvider:
    id = "demo_provider"
    display_name = "Demo Provider"
    credential_service = "demo_oauth"
    requester_scoped_credentials = False
    scopes: tuple[str, ...] = ()
    claim_validator = None

    def __init__(self, refresh: Callable[[Mapping[str, Any]], Awaitable[dict[str, Any] | None]]) -> None:
        self._refresh = refresh

    def client_config(self, _runtime_paths: RuntimePaths) -> OAuthClientConfig:
        return OAuthClientConfig(
            client_id="public-client",
            client_secret=None,
            redirect_uri="http://localhost/callback",
        )

    def resolved_allowed_email_domains(self, _runtime_paths: RuntimePaths) -> tuple[str, ...]:
        return ()

    def resolved_allowed_hosted_domains(self, _runtime_paths: RuntimePaths) -> tuple[str, ...]:
        return ()

    async def refresh_token_data(
        self,
        token_data: Mapping[str, Any],
        _runtime_paths: RuntimePaths,
    ) -> dict[str, Any] | None:
        return await self._refresh(token_data)

    async def exchange_code(
        self,
        _code: str,
        _runtime_paths: RuntimePaths,
        *,
        code_verifier: str | None = None,
    ) -> OAuthTokenResult:
        assert code_verifier is None
        return OAuthTokenResult(
            token_data={
                "token": "callback-access",
                "client_id": "public-client",
                "scopes": [],
            },
            claims={"sub": "subject-1"},
            claims_verified=True,
        )

    def validate_claims(self, _result: OAuthTokenResult, _runtime_paths: RuntimePaths) -> None:
        return None

    def token_result_with_safe_claims(self, result: OAuthTokenResult) -> OAuthTokenResult:
        token_data = dict(result.token_data)
        token_data["_oauth_claims"] = dict(result.claims)
        token_data["_oauth_claims_verified"] = result.claims_verified
        return OAuthTokenResult(
            token_data=token_data,
            claims=dict(result.claims),
            claims_verified=result.claims_verified,
        )


def _runtime_paths(tmp_path: Path, *, process_env: Mapping[str, str] | None = None) -> RuntimePaths:
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env=process_env or {},
    )


def _worker_target(
    requester_id: str = "@alice:example.test",
    *,
    worker_scope: str = "shared",
) -> ResolvedWorkerTarget:
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id=requester_id,
        room_id="!room:example.test",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id=None,
        tenant_id="tenant",
        account_id=None,
    )
    return resolve_worker_target(worker_scope, "code", identity)


def _credentials(token: str, refresh_token: str, *, expires_at: float) -> dict[str, Any]:
    return {
        "token": token,
        "refresh_token": refresh_token,
        "client_id": "public-client",
        "scopes": [],
        "expires_at": expires_at,
        "_source": "oauth",
        "_oauth_provider": "demo_provider",
    }


def _context(
    tmp_path: Path,
    provider: _FakeOAuthProvider,
    *,
    worker_target: ResolvedWorkerTarget | None = None,
) -> OAuthCredentialContext:
    runtime_paths = _runtime_paths(tmp_path)
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    credentials_manager.save_credentials("demo_oauth_client", {"client_id": "public-client"})
    return OAuthCredentialContext(
        provider=cast("OAuthProvider", provider),
        runtime_paths=runtime_paths,
        credentials_manager=credentials_manager,
        worker_target=worker_target or _worker_target(),
    )


def _save(context: OAuthCredentialContext, credentials: dict[str, Any]) -> None:
    save_scoped_credentials(
        context.provider.credential_service,
        credentials,
        credentials_manager=context.credentials_manager,
        worker_target=context.worker_target,
    )


def _load(context: OAuthCredentialContext) -> dict[str, Any] | None:
    return credential_lifecycle.load_oauth_credentials(context)


def _assert_no_token_values_logged(logger: _CapturingLogger) -> None:
    logged_payload = repr(logger.debug_calls + logger.info_calls + logger.warning_calls)
    for token_value in (ACCESS_0, CHAIN_0, CHAIN_1, f"access-{CHAIN_1}"):
        assert token_value not in logged_payload


@pytest.mark.asyncio
async def test_same_scope_refresh_serializes_provider_rotation(tmp_path: Path) -> None:
    """A later same-scope refresh observes the first committed rotation."""
    first_started = threading.Event()
    release_first = threading.Event()
    seen: list[str] = []

    async def refresh(credentials: Mapping[str, Any]) -> dict[str, Any] | None:
        refresh_token = str(credentials["refresh_token"])
        seen.append(refresh_token)
        if refresh_token == CHAIN_0:
            first_started.set()
            await asyncio.to_thread(release_first.wait)
            return _credentials(f"access-{CHAIN_1}", CHAIN_1, expires_at=FUTURE_EXPIRES_AT)
        assert refresh_token == CHAIN_1
        return None

    context = _context(tmp_path, _FakeOAuthProvider(refresh))
    _save(context, _credentials(ACCESS_0, CHAIN_0, expires_at=1.0))
    original_generation = credential_lifecycle.oauth_credential_generation(context)
    first = asyncio.create_task(refresh_oauth_credentials_with_result(context))
    await asyncio.to_thread(first_started.wait)
    second = asyncio.create_task(refresh_oauth_credentials_with_result(context))
    await asyncio.sleep(0)
    assert seen == [CHAIN_0]

    release_first.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert first_result.refreshed is True
    assert first_result.generation != original_generation
    assert second_result.generation == first_result.generation
    assert second_result.credentials == first_result.credentials
    assert seen == [CHAIN_0, CHAIN_1]
    assert _load(context) == first_result.credentials


@pytest.mark.asyncio
async def test_different_scopes_refresh_concurrently(tmp_path: Path) -> None:
    """Independent credential scopes do not share one global transaction."""
    both_started = threading.Event()
    started: set[str] = set()

    async def refresh(credentials: Mapping[str, Any]) -> dict[str, Any]:
        started.add(str(credentials["token"]))
        if len(started) == 2:
            both_started.set()
        await asyncio.to_thread(both_started.wait)
        return _credentials("updated", CHAIN_1, expires_at=FUTURE_EXPIRES_AT)

    provider = _FakeOAuthProvider(refresh)
    alice = _context(
        tmp_path,
        provider,
        worker_target=_worker_target("@alice:example.test", worker_scope="user"),
    )
    bob = _context(
        tmp_path,
        provider,
        worker_target=_worker_target("@bob:example.test", worker_scope="user"),
    )
    assert scoped_credentials_path(
        alice.provider.credential_service,
        credentials_manager=alice.credentials_manager,
        worker_target=alice.worker_target,
    ) != scoped_credentials_path(
        bob.provider.credential_service,
        credentials_manager=bob.credentials_manager,
        worker_target=bob.worker_target,
    )
    assert _oauth_credential_database_path(alice) != _oauth_credential_database_path(bob)
    _save(alice, _credentials("alice", CHAIN_0, expires_at=1.0))
    _save(bob, _credentials("bob", CHAIN_0, expires_at=1.0))

    await asyncio.wait_for(
        asyncio.gather(
            refresh_oauth_credentials_with_result(alice),
            refresh_oauth_credentials_with_result(bob),
        ),
        timeout=2,
    )

    assert started == {"alice", "bob"}


@pytest.mark.asyncio
async def test_credential_database_cannot_be_adopted_by_another_scope(tmp_path: Path) -> None:
    """A copied credential database cannot be adopted by another requester."""

    async def unused_refresh(_credentials: Mapping[str, Any]) -> None:
        return None

    provider = _FakeOAuthProvider(unused_refresh)
    alice = _context(
        tmp_path,
        provider,
        worker_target=_worker_target("@alice:example.test", worker_scope="user"),
    )
    bob = _context(
        tmp_path,
        provider,
        worker_target=_worker_target("@bob:example.test", worker_scope="user"),
    )
    _save(alice, _credentials("alice", CHAIN_0, expires_at=FUTURE_EXPIRES_AT))
    oauth_connection_generation(alice)
    alice_path = _oauth_credential_database_path(alice)
    bob_path = _oauth_credential_database_path(bob)
    bob_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(alice_path, bob_path)

    with pytest.raises(OAuthProviderError, match="different credential scope"):
        await load_oauth_credentials_snapshot(bob)


@pytest.mark.asyncio
async def test_user_scope_publication_is_shared_across_routing_agents(tmp_path: Path) -> None:
    """Routing-agent changes cannot split one requester-wide credential scope."""

    async def unused_refresh(_credentials: Mapping[str, Any]) -> None:
        return None

    provider = _FakeOAuthProvider(unused_refresh)
    code_target = _worker_target("@alice:example.test", worker_scope="user")
    assert code_target.execution_identity is not None
    research_target = replace(
        code_target,
        routing_agent_name="research",
        execution_identity=replace(code_target.execution_identity, agent_name="research"),
    )
    code = _context(tmp_path, provider, worker_target=code_target)
    research = _context(tmp_path, provider, worker_target=research_target)
    expected = _credentials("alice", CHAIN_0, expires_at=FUTURE_EXPIRES_AT)
    _save(code, expected)
    oauth_connection_generation(code)

    snapshot = await load_oauth_credentials_snapshot(research)

    assert snapshot.credentials == expected


def test_sqlite_state_requires_explicit_connection_generation(tmp_path: Path) -> None:
    """The SQLite schema cannot collapse its two revision fences."""

    async def unused_refresh(_credentials: Mapping[str, Any]) -> None:
        return None

    context = _context(tmp_path, _FakeOAuthProvider(unused_refresh))
    oauth_connection_generation(context)
    connection = sqlite3.connect(_oauth_credential_database_path(context))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE oauth_credential_state SET connection_generation = NULL WHERE singleton = 1",
            )
    finally:
        connection.close()


@pytest.mark.parametrize("encrypted", [False, True], ids=("plaintext", "encrypted"))
@pytest.mark.asyncio
async def test_reset_deletes_unreadable_scoped_file_and_allows_reconnect(
    tmp_path: Path,
    *,
    encrypted: bool,
) -> None:
    """Reset must remove the exact file even when credential decoding fails."""

    async def unused_refresh(_credentials: Mapping[str, Any]) -> None:
        return None

    process_env = (
        {
            "MINDROOM_CREDENTIALS_ENCRYPTION_KEY": base64.urlsafe_b64encode(b"x" * 32).decode(),
        }
        if encrypted
        else {}
    )
    runtime_paths = _runtime_paths(tmp_path, process_env=process_env)
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    credentials_manager.save_credentials("demo_oauth_client", {"client_id": "public-client"})
    context = OAuthCredentialContext(
        provider=cast("OAuthProvider", _FakeOAuthProvider(unused_refresh)),
        runtime_paths=runtime_paths,
        credentials_manager=credentials_manager,
        worker_target=_worker_target(),
    )
    credentials_path = scoped_credentials_path(
        context.provider.credential_service,
        credentials_manager=context.credentials_manager,
        worker_target=context.worker_target,
    )
    credentials_path.parent.mkdir(parents=True, exist_ok=True)
    credentials_path.write_bytes(b"not-a-readable-credential")

    assert await credential_lifecycle.reset_oauth_credentials(context) is True
    assert not credentials_path.exists()

    reconnected = await exchange_and_store_oauth_credentials(
        context,
        "replacement-code",
        None,
        expected_connection_generation=oauth_connection_generation(context),
    )
    assert reconnected["token"] == "callback-access"  # noqa: S105
    assert _load(context) == reconnected


@pytest.mark.asyncio
async def test_wrong_encryption_key_does_not_poison_legacy_oauth_adoption(tmp_path: Path) -> None:
    """An unreadable legacy credential must remain adoptable after the correct key is restored."""

    async def unused_refresh(_credentials: Mapping[str, Any]) -> None:
        return None

    correct_key = base64.urlsafe_b64encode(b"a" * 32).decode()
    wrong_key = base64.urlsafe_b64encode(b"b" * 32).decode()
    provider = cast("OAuthProvider", _FakeOAuthProvider(unused_refresh))
    worker_target = _worker_target()
    correct_runtime_paths = _runtime_paths(
        tmp_path,
        process_env={"MINDROOM_CREDENTIALS_ENCRYPTION_KEY": correct_key},
    )
    correct_context = OAuthCredentialContext(
        provider=provider,
        runtime_paths=correct_runtime_paths,
        credentials_manager=get_runtime_credentials_manager(correct_runtime_paths),
        worker_target=worker_target,
    )
    legacy_credentials = _credentials(ACCESS_0, CHAIN_0, expires_at=FUTURE_EXPIRES_AT)
    _save(correct_context, legacy_credentials)
    credentials_path = scoped_credentials_path(
        provider.credential_service,
        credentials_manager=correct_context.credentials_manager,
        worker_target=worker_target,
    )
    wrong_runtime_paths = _runtime_paths(
        tmp_path,
        process_env={"MINDROOM_CREDENTIALS_ENCRYPTION_KEY": wrong_key},
    )
    wrong_context = OAuthCredentialContext(
        provider=provider,
        runtime_paths=wrong_runtime_paths,
        credentials_manager=get_runtime_credentials_manager(wrong_runtime_paths),
        worker_target=worker_target,
    )
    with pytest.raises(OAuthProviderError, match="credentials could not be loaded"):
        await load_oauth_credentials_snapshot(wrong_context)

    assert not credentials_path.exists()
    assert ACCESS_0.encode() not in _oauth_credential_database_path(wrong_context).read_bytes()
    snapshot = await load_oauth_credentials_snapshot(correct_context)
    assert snapshot.credentials == legacy_credentials
    assert _oauth_credential_database_path(correct_context).exists()


@pytest.mark.asyncio
async def test_completed_reset_operation_cannot_delete_later_callback_credentials(tmp_path: Path) -> None:
    """A stale recovery retry must return its original result without resetting a newer account."""

    async def unused_refresh(_credentials: Mapping[str, Any]) -> None:
        return None

    context = _context(tmp_path, _FakeOAuthProvider(unused_refresh))
    _save(context, _credentials(ACCESS_0, CHAIN_0, expires_at=FUTURE_EXPIRES_AT))
    operation_id = "browser:reset-operation-1"

    assert await credential_lifecycle.reset_oauth_credentials(context, operation_id=operation_id) is True
    replacement = await exchange_and_store_oauth_credentials(
        context,
        "replacement-code",
        None,
        expected_connection_generation=oauth_connection_generation(context),
    )

    assert await credential_lifecycle.reset_oauth_credentials(context, operation_id=operation_id) is True
    assert _load(context) == replacement


@pytest.mark.asyncio
async def test_completed_browser_reset_replay_skips_mcp_retirement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retried browser confirmation cannot retire a later reconnected session."""

    async def unused_refresh(_credentials: Mapping[str, Any]) -> None:
        return None

    context = _context(tmp_path, _FakeOAuthProvider(unused_refresh))
    _save(context, _credentials(ACCESS_0, CHAIN_0, expires_at=FUTURE_EXPIRES_AT))
    operation_id = "browser:stable-reset"
    assert await credential_lifecycle.reset_oauth_credentials(context, operation_id=operation_id) is True
    replacement = await exchange_and_store_oauth_credentials(
        context,
        "replacement-code",
        None,
        expected_connection_generation=oauth_connection_generation(context),
    )
    retirement_entered = False

    @asynccontextmanager
    async def retirement(*_args: object, **_kwargs: object) -> AsyncIterator[None]:
        nonlocal retirement_entered
        retirement_entered = True
        yield

    monkeypatch.setattr(reset_execution, "retire_mcp_oauth_request_session", retirement)

    deleted = await reset_execution.retire_and_reset_oauth_credentials(
        context,
        mcp_servers={},
        operation_id=operation_id,
        expected_connection_generation="stale-generation",
    )

    assert deleted is True
    assert retirement_entered is False
    assert _load(context) == replacement


@pytest.mark.asyncio
async def test_stale_approved_reset_cannot_delete_reconnected_credentials(tmp_path: Path) -> None:
    """A browser reset intent is valid only for the credential generation shown at confirmation."""

    async def unused_refresh(_credentials: Mapping[str, Any]) -> None:
        return None

    context = _context(tmp_path, _FakeOAuthProvider(unused_refresh))
    _save(context, _credentials(ACCESS_0, CHAIN_0, expires_at=FUTURE_EXPIRES_AT))
    approved_generation = oauth_connection_generation(context)
    replacement = await exchange_and_store_oauth_credentials(
        context,
        "replacement-code",
        None,
        expected_connection_generation=approved_generation,
    )

    with pytest.raises(OAuthProviderError, match="credential changed"):
        await credential_lifecycle.reset_oauth_credentials(
            context,
            operation_id="browser:reset-operation-1",
            expected_connection_generation=approved_generation,
        )

    assert _load(context) == replacement
    assert await credential_lifecycle.oauth_reset_operation_result(context, "browser:reset-operation-1") is None


@pytest.mark.asyncio
async def test_direct_resets_do_not_retain_replay_tombstones(tmp_path: Path) -> None:
    """Unkeyed API resets recover pending deletion without growing permanent operation state."""

    async def unused_refresh(_credentials: Mapping[str, Any]) -> None:
        return None

    context = _context(tmp_path, _FakeOAuthProvider(unused_refresh))
    _save(context, _credentials(ACCESS_0, CHAIN_0, expires_at=FUTURE_EXPIRES_AT))

    assert await credential_lifecycle.reset_oauth_credentials(context) is True
    assert await credential_lifecycle.reset_oauth_credentials(context) is False
    async with credential_store.oauth_credential_transaction(context) as transaction:
        assert transaction.reset_operation_result("direct:unknown") is None
        await transaction.commit()


@pytest.mark.asyncio
async def test_callback_waits_for_refresh_and_preserves_rotated_refresh_token(tmp_path: Path) -> None:
    """Callback publication preserves the latest committed token chain, not its stale predecessor."""
    refresh_started = threading.Event()
    release_refresh = threading.Event()
    exchange_started = threading.Event()

    async def refresh(credentials: Mapping[str, Any]) -> dict[str, Any]:
        assert credentials["refresh_token"] == CHAIN_0
        refresh_started.set()
        await asyncio.to_thread(release_refresh.wait)
        rotated = _credentials(f"access-{CHAIN_1}", CHAIN_1, expires_at=FUTURE_EXPIRES_AT)
        rotated["_oauth_claims"] = {"sub": "subject-1"}
        rotated["_oauth_claims_verified"] = True
        return rotated

    provider = _FakeOAuthProvider(refresh)
    original_exchange = provider.exchange_code

    async def observed_exchange(*args: object, **kwargs: object) -> OAuthTokenResult:
        exchange_started.set()
        return await original_exchange(*args, **kwargs)

    provider.exchange_code = observed_exchange  # type: ignore[method-assign]
    context = _context(tmp_path, provider)
    original = _credentials(ACCESS_0, CHAIN_0, expires_at=1.0)
    original["_oauth_claims"] = {"sub": "subject-1"}
    original["_oauth_claims_verified"] = True
    _save(context, original)
    issued_connection_generation = oauth_connection_generation(context)
    issued_credential_generation = credential_lifecycle.oauth_credential_generation(context)

    refresh_task = asyncio.create_task(refresh_oauth_credentials_with_result(context))
    await asyncio.to_thread(refresh_started.wait)
    callback_task = asyncio.create_task(
        exchange_and_store_oauth_credentials(
            context,
            "code",
            None,
            expected_connection_generation=issued_connection_generation,
        ),
    )
    await asyncio.sleep(0)
    assert not exchange_started.is_set()

    release_refresh.set()
    refresh_result = await refresh_task
    callback_credentials = await callback_task

    assert refresh_result.generation != issued_credential_generation
    assert refresh_result.connection_generation == issued_connection_generation
    assert callback_credentials["token"] == "callback-access"  # noqa: S105
    assert callback_credentials["refresh_token"] == CHAIN_1
    assert _load(context) == callback_credentials


@pytest.mark.asyncio
async def test_callback_advances_connection_generation_and_rejects_second_callback(tmp_path: Path) -> None:
    """One issued connection generation authorizes exactly one credential replacement."""

    async def unused_refresh(_credentials: Mapping[str, Any]) -> None:
        return None

    context = _context(tmp_path, _FakeOAuthProvider(unused_refresh))
    issued_connection_generation = oauth_connection_generation(context)

    stored = await exchange_and_store_oauth_credentials(
        context,
        "first-code",
        None,
        expected_connection_generation=issued_connection_generation,
    )

    assert stored["token"] == "callback-access"  # noqa: S105
    assert oauth_connection_generation(context) != issued_connection_generation
    with pytest.raises(OAuthProviderError, match="credential changed"):
        await exchange_and_store_oauth_credentials(
            context,
            "second-code",
            None,
            expected_connection_generation=issued_connection_generation,
        )


@pytest.mark.asyncio
async def test_refresh_cancellation_while_waiting_for_lock_does_not_call_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cancellation before lock ownership must abandon an unaccepted refresh."""
    lock_waiting = threading.Event()
    release_lock = threading.Event()
    provider_called = threading.Event()
    real_begin = credential_store._begin_immediate

    async def refresh(_credentials: Mapping[str, Any]) -> None:
        provider_called.set()

    async def blocked_begin(_connection: sqlite3.Connection) -> None:
        lock_waiting.set()
        await asyncio.to_thread(release_lock.wait)

    context = _context(tmp_path, _FakeOAuthProvider(refresh))
    _save(context, _credentials(ACCESS_0, CHAIN_0, expires_at=1.0))
    monkeypatch.setattr(credential_store, "_begin_immediate", blocked_begin)

    refresh_task = asyncio.create_task(refresh_oauth_credentials_with_result(context))
    await asyncio.to_thread(lock_waiting.wait)
    refresh_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await refresh_task
    release_lock.set()
    monkeypatch.setattr(credential_store, "_begin_immediate", real_begin)

    assert not provider_called.is_set()
    assert _load(context) == _credentials(ACCESS_0, CHAIN_0, expires_at=1.0)


@pytest.mark.asyncio
async def test_snapshot_cancellation_while_waiting_for_lock_returns_promptly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A read-only snapshot must remain cancellable before it owns the operation lock."""
    lock_waiting = threading.Event()
    release_lock = threading.Event()

    async def unused_refresh(_credentials: Mapping[str, Any]) -> None:
        return None

    async def blocked_begin(_connection: sqlite3.Connection) -> None:
        lock_waiting.set()
        await asyncio.to_thread(release_lock.wait)

    context = _context(tmp_path, _FakeOAuthProvider(unused_refresh))
    _save(context, _credentials(ACCESS_0, CHAIN_0, expires_at=FUTURE_EXPIRES_AT))
    monkeypatch.setattr(credential_store, "_begin_immediate", blocked_begin)
    snapshot_task = asyncio.create_task(load_oauth_credentials_snapshot(context))
    await asyncio.to_thread(lock_waiting.wait)
    snapshot_task.cancel()

    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(snapshot_task, timeout=1)
    finally:
        release_lock.set()


@pytest.mark.asyncio
async def test_refresh_publishes_rotation_before_propagating_cancellation(tmp_path: Path) -> None:
    """A remotely rotated refresh grant is committed before cancellation escapes."""
    provider_rotated = threading.Event()
    release_provider_result = threading.Event()

    async def refresh(credentials: Mapping[str, Any]) -> dict[str, Any]:
        assert credentials["refresh_token"] == CHAIN_0
        provider_rotated.set()
        await asyncio.to_thread(release_provider_result.wait)
        return _credentials(f"access-{CHAIN_1}", CHAIN_1, expires_at=FUTURE_EXPIRES_AT)

    context = _context(tmp_path, _FakeOAuthProvider(refresh))
    _save(context, _credentials(ACCESS_0, CHAIN_0, expires_at=1.0))
    refresh_task = asyncio.create_task(refresh_oauth_credentials_with_result(context))
    await asyncio.to_thread(provider_rotated.wait)

    refresh_task.cancel()
    await asyncio.sleep(0)
    assert not refresh_task.done()
    release_provider_result.set()

    with pytest.raises(asyncio.CancelledError):
        await refresh_task
    stored = _load(context)
    assert stored is not None
    assert stored["refresh_token"] == CHAIN_1


@pytest.mark.asyncio
async def test_reset_generation_rejects_a_callback_that_was_issued_before_reset(tmp_path: Path) -> None:
    """A callback cannot republish credentials after its target generation is reset."""

    async def unused_refresh(_credentials: Mapping[str, Any]) -> None:
        return None

    context = _context(tmp_path, _FakeOAuthProvider(unused_refresh))
    _save(context, _credentials(ACCESS_0, CHAIN_0, expires_at=FUTURE_EXPIRES_AT))
    stale_generation = oauth_connection_generation(context)

    assert await credential_lifecycle.reset_oauth_credentials(context) is True
    with pytest.raises(OAuthProviderError, match="credential changed"):
        await exchange_and_store_oauth_credentials(
            context,
            "stale-code",
            None,
            expected_connection_generation=stale_generation,
        )

    assert _load(context) is None


@pytest.mark.asyncio
async def test_reset_cancellation_while_waiting_for_lock_preserves_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cancellation before the reset transaction owns its lock must abort deletion."""
    lock_waiting = threading.Event()
    release_lock = threading.Event()
    real_begin = credential_store._begin_immediate

    async def unused_refresh(_credentials: Mapping[str, Any]) -> None:
        return None

    async def blocked_begin(_connection: sqlite3.Connection) -> None:
        lock_waiting.set()
        await asyncio.to_thread(release_lock.wait)

    context = _context(tmp_path, _FakeOAuthProvider(unused_refresh))
    original = _credentials(ACCESS_0, CHAIN_0, expires_at=FUTURE_EXPIRES_AT)
    _save(context, original)
    generation = credential_lifecycle.oauth_credential_generation(context)
    monkeypatch.setattr(credential_store, "_begin_immediate", blocked_begin)

    reset_task = asyncio.create_task(credential_lifecycle.reset_oauth_credentials(context))
    await asyncio.to_thread(lock_waiting.wait)
    reset_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reset_task
    release_lock.set()
    monkeypatch.setattr(credential_store, "_begin_immediate", real_begin)

    assert _load(context) == original
    assert credential_lifecycle.oauth_credential_generation(context) == generation


@pytest.mark.asyncio
async def test_reset_cancellation_before_sqlite_commit_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A cancelled reset remains non-destructive until its SQLite commit succeeds."""
    commit_waiting = threading.Event()
    commit_cancelled = threading.Event()
    release_commit = threading.Event()

    async def unused_refresh(_credentials: Mapping[str, Any]) -> None:
        return None

    context = _context(tmp_path, _FakeOAuthProvider(unused_refresh))
    original = _credentials(ACCESS_0, CHAIN_0, expires_at=FUTURE_EXPIRES_AT)
    _save(context, original)
    generation = credential_lifecycle.oauth_credential_generation(context)
    real_commit = OAuthCredentialTransaction.commit

    async def blocked_commit(transaction: OAuthCredentialTransaction) -> None:
        commit_waiting.set()
        try:
            await asyncio.to_thread(release_commit.wait)
        except asyncio.CancelledError:
            commit_cancelled.set()
            raise
        await real_commit(transaction)

    monkeypatch.setattr(OAuthCredentialTransaction, "commit", blocked_commit)

    reset_task = asyncio.create_task(credential_lifecycle.reset_oauth_credentials(context))
    await asyncio.to_thread(commit_waiting.wait)
    reset_task.cancel()
    await asyncio.to_thread(commit_cancelled.wait)
    release_commit.set()

    with pytest.raises(asyncio.CancelledError):
        await reset_task
    assert _load(context) == original
    assert credential_lifecycle.oauth_credential_generation(context) == generation


@pytest.mark.asyncio
async def test_terminal_refresh_rejection_deletes_locked_credentials_without_logging_tokens(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A structured terminal rejection invalidates without exposing token data."""
    logger = _CapturingLogger()
    monkeypatch.setattr(credential_lifecycle, "logger", logger)

    async def refresh(credentials: Mapping[str, Any]) -> dict[str, Any]:
        assert credentials["refresh_token"] == CHAIN_0
        message = "dead refresh grant"
        raise OAuthRefreshRejectedError(
            message,
            oauth_error=INVALID_ROTATION,
            oauth_error_description=f"provider detail must not log {CHAIN_0}",
        )

    context = _context(tmp_path, _FakeOAuthProvider(refresh))
    _save(context, _credentials(ACCESS_0, CHAIN_0, expires_at=1.0))

    with pytest.raises(OAuthRefreshRejectedError) as exc_info:
        await refresh_oauth_credentials_with_result(context)

    assert str(exc_info.value) == "OAuth credential refresh failed"
    assert exc_info.value.oauth_error == INVALID_ROTATION
    assert exc_info.value.oauth_error_description is None
    assert _load(context) is None
    assert logger.warning_calls == [
        (
            "oauth_credentials_refresh_failed",
            {
                "provider_id": "demo_provider",
                "credential_service": "demo_oauth",
                "reason": "refresh_rejected",
                "has_refresh_token": True,
                "expires_at": 1.0,
                "error_type": "OAuthRefreshRejectedError",
                "oauth_error": INVALID_ROTATION,
            },
        ),
    ]
    _assert_no_token_values_logged(logger)


@pytest.mark.asyncio
async def test_nonterminal_refresh_failure_preserves_credentials_and_bounds_logs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Transient and unknown errors keep credentials and produce bounded logs."""
    logger = _CapturingLogger()
    monkeypatch.setattr(credential_lifecycle, "logger", logger)
    provider_error = f"invalid_grant appears only in provider text with {CHAIN_0} " + ("x" * 10_000)

    async def refresh(credentials: Mapping[str, Any]) -> dict[str, Any]:
        assert credentials["refresh_token"] == CHAIN_0
        message = "provider unavailable"
        raise OAuthProviderError(message, oauth_error=provider_error)

    context = _context(tmp_path, _FakeOAuthProvider(refresh))
    original = _credentials(ACCESS_0, CHAIN_0, expires_at=1.0)
    _save(context, original)

    with pytest.raises(OAuthProviderError) as exc_info:
        await refresh_oauth_credentials_with_result(context)

    assert type(exc_info.value) is OAuthProviderError
    assert str(exc_info.value) == "OAuth credential refresh failed"
    assert provider_error not in str(exc_info.value)
    assert _load(context) == original
    assert logger.warning_calls[0][1]["reason"] == "provider_refresh_failed"
    assert logger.warning_calls[0][1]["oauth_error"] == "unrecognized"
    assert provider_error not in repr(logger.warning_calls)
    _assert_no_token_values_logged(logger)


def test_sync_refresh_uses_same_scope_transaction(tmp_path: Path) -> None:
    """The synchronous provider adapter delegates persistence to the lifecycle."""
    caller_thread = threading.get_ident()
    observed: list[str] = []

    async def unused_refresh(_credentials: Mapping[str, Any]) -> None:
        return None

    context = _context(tmp_path, _FakeOAuthProvider(unused_refresh))
    _save(context, _credentials(ACCESS_0, CHAIN_0, expires_at=1.0))

    def refresh(credentials: Mapping[str, Any]) -> dict[str, Any]:
        assert threading.get_ident() != caller_thread
        observed.append(str(credentials["refresh_token"]))
        return _credentials(f"access-{CHAIN_1}", CHAIN_1, expires_at=FUTURE_EXPIRES_AT)

    result = refresh_oauth_credentials_sync(context, refresh)

    assert result.refreshed is True
    assert observed == [CHAIN_0]
    assert _load(context) == result.credentials


@pytest.mark.asyncio
async def test_sync_refresh_rejects_changed_connection_generation_before_adapter(tmp_path: Path) -> None:
    """A stale materialized client cannot adopt credentials from a replacement account."""

    async def unused_refresh(_credentials: Mapping[str, Any]) -> None:
        return None

    context = _context(tmp_path, _FakeOAuthProvider(unused_refresh))
    _save(context, _credentials(ACCESS_0, CHAIN_0, expires_at=1.0))
    account_a_generation = oauth_connection_generation(context)
    await exchange_and_store_oauth_credentials(
        context,
        "account-b-code",
        None,
        expected_connection_generation=account_a_generation,
    )
    adapter_called = False

    def stale_adapter(_credentials: Mapping[str, Any]) -> None:
        nonlocal adapter_called
        adapter_called = True

    with pytest.raises(OAuthCredentialConflictError):
        refresh_oauth_credentials_sync(
            context,
            stale_adapter,
            expected_connection_generation=account_a_generation,
        )

    assert not adapter_called
    current = _load(context)
    assert current is not None
    assert current["token"] == "callback-access"  # noqa: S105


@pytest.mark.asyncio
async def test_sync_refresh_on_event_loop_cannot_deadlock_behind_async_same_scope_transaction(
    tmp_path: Path,
) -> None:
    """Sync tools wait on an independent transaction loop, never on their blocked caller loop."""
    async_refresh_started = threading.Event()
    release_async_refresh = threading.Event()
    caller_thread = threading.get_ident()
    sync_refresh_thread: list[int] = []

    async def refresh(credentials: Mapping[str, Any]) -> dict[str, Any] | None:
        if credentials["refresh_token"] != CHAIN_0:
            return None
        async_refresh_started.set()
        await asyncio.to_thread(release_async_refresh.wait)
        return _credentials(f"access-{CHAIN_1}", CHAIN_1, expires_at=1.0)

    context = _context(tmp_path, _FakeOAuthProvider(refresh))
    _save(context, _credentials(ACCESS_0, CHAIN_0, expires_at=1.0))
    async_task = asyncio.create_task(refresh_oauth_credentials_with_result(context))
    await asyncio.to_thread(async_refresh_started.wait)

    releaser = threading.Thread(target=release_async_refresh.set)
    releaser.start()

    def sync_refresh(credentials: Mapping[str, Any]) -> None:
        sync_refresh_thread.append(threading.get_ident())
        assert credentials["refresh_token"] == CHAIN_1

    sync_result = refresh_oauth_credentials_sync(context, sync_refresh)
    releaser.join()
    async_result = await async_task

    assert async_result.refreshed is True
    assert sync_result.credentials == async_result.credentials
    assert sync_refresh_thread
    assert sync_refresh_thread[0] != caller_thread
