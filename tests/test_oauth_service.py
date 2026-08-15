"""Tests for the serialized OAuth credential lifecycle."""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, cast

import pytest

from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.credentials import get_runtime_credentials_manager, load_scoped_credentials, save_scoped_credentials
from mindroom.oauth import credential_lifecycle
from mindroom.oauth.credential_lifecycle import (
    OAuthCredentialContext,
    exchange_and_store_oauth_credentials,
    refresh_oauth_credentials_sync,
    refresh_oauth_credentials_with_result,
)
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


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})


def _worker_target(requester_id: str = "@alice:example.test") -> ResolvedWorkerTarget:
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
    return resolve_worker_target("shared", "code", identity)


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
    return load_scoped_credentials(
        context.provider.credential_service,
        credentials_manager=context.credentials_manager,
        worker_target=context.worker_target,
    )


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
    first = asyncio.create_task(refresh_oauth_credentials_with_result(context))
    await asyncio.to_thread(first_started.wait)
    second = asyncio.create_task(refresh_oauth_credentials_with_result(context))
    await asyncio.sleep(0)
    assert seen == [CHAIN_0]

    release_first.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert first_result.refreshed is True
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
    alice = _context(tmp_path, provider, worker_target=_worker_target("@alice:example.test"))
    bob = _context(tmp_path, provider, worker_target=_worker_target("@bob:example.test"))
    _save(alice, _credentials("alice", CHAIN_0, expires_at=1.0))
    _save(bob, _credentials("bob", CHAIN_0, expires_at=1.0))

    await asyncio.gather(
        refresh_oauth_credentials_with_result(alice),
        refresh_oauth_credentials_with_result(bob),
    )

    assert started == {"alice", "bob"}


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

    refresh_task = asyncio.create_task(refresh_oauth_credentials_with_result(context))
    await asyncio.to_thread(refresh_started.wait)
    callback_task = asyncio.create_task(
        exchange_and_store_oauth_credentials(
            context,
            "code",
            None,
            expected_generation=credential_lifecycle.oauth_credential_generation(context),
        ),
    )
    await asyncio.sleep(0)
    assert not exchange_started.is_set()

    release_refresh.set()
    await refresh_task
    callback_credentials = await callback_task

    assert callback_credentials["token"] == "callback-access"  # noqa: S105
    assert callback_credentials["refresh_token"] == CHAIN_1
    assert _load(context) == callback_credentials


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
    stale_generation = credential_lifecycle.oauth_credential_generation(context)

    assert await credential_lifecycle.reset_oauth_credentials(context) is True
    with pytest.raises(OAuthProviderError, match="credential was reset"):
        await exchange_and_store_oauth_credentials(
            context,
            "stale-code",
            None,
            expected_generation=stale_generation,
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

    async def unused_refresh(_credentials: Mapping[str, Any]) -> None:
        return None

    @asynccontextmanager
    async def blocked_lock(_path: Path) -> AsyncIterator[None]:
        lock_waiting.set()
        await asyncio.to_thread(release_lock.wait)
        yield None

    context = _context(tmp_path, _FakeOAuthProvider(unused_refresh))
    original = _credentials(ACCESS_0, CHAIN_0, expires_at=FUTURE_EXPIRES_AT)
    _save(context, original)
    generation = credential_lifecycle.oauth_credential_generation(context)
    monkeypatch.setattr(credential_lifecycle, "async_exclusive_file_lock", blocked_lock)

    reset_task = asyncio.create_task(credential_lifecycle.reset_oauth_credentials(context))
    await asyncio.to_thread(lock_waiting.wait)
    reset_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reset_task
    release_lock.set()

    assert _load(context) == original
    assert credential_lifecycle.oauth_credential_generation(context) == generation


@pytest.mark.asyncio
async def test_reset_cancellation_after_delete_returns_committed_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A committed destructive reset must still return its receipt result when cancelled."""
    credential_deleted = threading.Event()
    release_delete = threading.Event()

    async def unused_refresh(_credentials: Mapping[str, Any]) -> None:
        return None

    context = _context(tmp_path, _FakeOAuthProvider(unused_refresh))
    _save(context, _credentials(ACCESS_0, CHAIN_0, expires_at=FUTURE_EXPIRES_AT))
    generation = credential_lifecycle.oauth_credential_generation(context)
    real_delete = credential_lifecycle.delete_scoped_credentials

    def blocked_delete(*args: object, **kwargs: object) -> None:
        real_delete(*args, **kwargs)
        credential_deleted.set()
        release_delete.wait()

    monkeypatch.setattr(credential_lifecycle, "delete_scoped_credentials", blocked_delete)

    reset_task = asyncio.create_task(credential_lifecycle.reset_oauth_credentials(context))
    await asyncio.to_thread(credential_deleted.wait)
    reset_task.cancel()
    release_delete.set()

    assert await reset_task is True
    assert _load(context) is None
    assert credential_lifecycle.oauth_credential_generation(context) != generation


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

    with pytest.raises(OAuthRefreshRejectedError):
        await refresh_oauth_credentials_with_result(context)

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

    with pytest.raises(OAuthProviderError):
        await refresh_oauth_credentials_with_result(context)

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
