"""Tests for shared OAuth service helpers."""

from __future__ import annotations

import asyncio
import threading
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import pytest

import mindroom.oauth.service as oauth_service_module
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.credentials import get_runtime_credentials_manager, load_scoped_credentials, save_scoped_credentials
from mindroom.file_locks import async_exclusive_file_lock, file_lock_is_held
from mindroom.oauth.providers import OAuthClientConfig, OAuthProviderError, OAuthRefreshRejectedError
from mindroom.oauth.service import (
    refresh_scoped_oauth_credentials_with_result,
    scoped_oauth_credentials_refresh_lock_path,
    scoped_oauth_credentials_singleflight_lock_path,
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
CHAIN_2 = "refresh-2"
INVALID_ROTATION = "invalid_refresh_token"
FUTURE_EXPIRES_AT = 4_102_444_800.0
RECONNECTED_ACCESS = "reconnected-access"


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


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env={})


def _worker_target() -> ResolvedWorkerTarget:
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id="@alice:example.test",
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


def _save_credentials(
    runtime_paths: RuntimePaths,
    worker_target: ResolvedWorkerTarget,
    credentials: dict[str, Any],
) -> None:
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    credentials_manager.save_credentials("demo_oauth_client", {"client_id": "public-client"})
    save_scoped_credentials(
        "demo_oauth",
        credentials,
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )


def _assert_no_token_values_logged(logger: _CapturingLogger) -> None:
    logged_payload = repr(logger.debug_calls + logger.info_calls + logger.warning_calls)
    for token_value in (ACCESS_0, CHAIN_0, CHAIN_1, CHAIN_2, f"access-{CHAIN_1}", f"access-{CHAIN_2}"):
        assert token_value not in logged_payload


@pytest.mark.asyncio
async def test_scoped_oauth_refresh_releases_file_lock_during_provider_io(tmp_path: Path) -> None:
    """Provider I/O must not retain the file lock needed by synchronous OAuth callers."""
    runtime_paths = _runtime_paths(tmp_path)
    worker_target = _worker_target()
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    _save_credentials(runtime_paths, worker_target, _credentials(ACCESS_0, CHAIN_0, expires_at=1.0))
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()

    async def refresh(credentials: Mapping[str, Any]) -> dict[str, Any]:
        assert credentials["refresh_token"] == CHAIN_0
        refresh_started.set()
        await release_refresh.wait()
        return _credentials(f"access-{CHAIN_1}", CHAIN_1, expires_at=FUTURE_EXPIRES_AT)

    refresh_task = asyncio.create_task(
        refresh_scoped_oauth_credentials_with_result(
            _FakeOAuthProvider(refresh),
            runtime_paths,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
        ),
    )
    await refresh_started.wait()

    lock_path = scoped_oauth_credentials_refresh_lock_path(
        "demo_oauth",
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )
    assert file_lock_is_held(lock_path) is False

    release_refresh.set()
    result = await refresh_task
    assert result.refreshed is True


@pytest.mark.asyncio
async def test_scoped_oauth_refresh_preserves_reconnect_during_successful_provider_io(tmp_path: Path) -> None:
    """A reconnect committed during provider I/O must supersede its stale successful result."""
    runtime_paths = _runtime_paths(tmp_path)
    worker_target = _worker_target()
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    _save_credentials(runtime_paths, worker_target, _credentials(ACCESS_0, CHAIN_0, expires_at=1.0))
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()

    async def refresh(credentials: Mapping[str, Any]) -> dict[str, Any]:
        assert credentials["refresh_token"] == CHAIN_0
        refresh_started.set()
        await release_refresh.wait()
        return _credentials(f"access-{CHAIN_1}", CHAIN_1, expires_at=FUTURE_EXPIRES_AT)

    refresh_task = asyncio.create_task(
        refresh_scoped_oauth_credentials_with_result(
            _FakeOAuthProvider(refresh),
            runtime_paths,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
        ),
    )
    await refresh_started.wait()
    reconnected_credentials = _credentials(RECONNECTED_ACCESS, CHAIN_0, expires_at=FUTURE_EXPIRES_AT)
    lock_path = scoped_oauth_credentials_refresh_lock_path(
        "demo_oauth",
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )
    async with async_exclusive_file_lock(lock_path):
        save_scoped_credentials(
            "demo_oauth",
            reconnected_credentials,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
        )

    release_refresh.set()
    result = await refresh_task

    assert result == oauth_service_module.OAuthCredentialsRefreshResult(
        credentials=reconnected_credentials,
        refreshed=False,
    )
    assert (
        load_scoped_credentials(
            "demo_oauth",
            credentials_manager=credentials_manager,
            worker_target=worker_target,
        )
        == reconnected_credentials
    )


@pytest.mark.asyncio
async def test_scoped_oauth_refresh_singleflights_error_behind_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A rejected concurrent refresh must observe a successful rotation instead of deleting its input."""
    runtime_paths = _runtime_paths(tmp_path)
    worker_target = _worker_target()
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    _save_credentials(runtime_paths, worker_target, _credentials(ACCESS_0, CHAIN_0, expires_at=1.0))
    singleflight_path = scoped_oauth_credentials_singleflight_lock_path(
        "demo_oauth",
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )
    first_refresh_started = asyncio.Event()
    second_lock_attempted = asyncio.Event()
    release_first_refresh = asyncio.Event()
    refresh_tokens: list[str] = []
    lock_attempts = 0
    original_async_exclusive_file_lock = oauth_service_module.async_exclusive_file_lock

    @asynccontextmanager
    async def observed_async_exclusive_file_lock(lock_path: Path) -> AsyncIterator[object]:
        nonlocal lock_attempts
        if lock_path == singleflight_path:
            lock_attempts += 1
            if lock_attempts == 2:
                second_lock_attempted.set()
        async with original_async_exclusive_file_lock(lock_path) as lock_file:
            yield lock_file

    monkeypatch.setattr(
        oauth_service_module,
        "async_exclusive_file_lock",
        observed_async_exclusive_file_lock,
    )

    async def refresh(credentials: Mapping[str, Any]) -> dict[str, Any] | None:
        refresh_token = str(credentials["refresh_token"])
        refresh_tokens.append(refresh_token)
        if refresh_token == CHAIN_0:
            if not first_refresh_started.is_set():
                first_refresh_started.set()
                await release_first_refresh.wait()
                return _credentials(f"access-{CHAIN_1}", CHAIN_1, expires_at=FUTURE_EXPIRES_AT)
            raise OAuthRefreshRejectedError(INVALID_ROTATION, oauth_error=INVALID_ROTATION)
        assert refresh_token == CHAIN_1
        return None

    provider = _FakeOAuthProvider(refresh)
    first_task = asyncio.create_task(
        refresh_scoped_oauth_credentials_with_result(
            provider,
            runtime_paths,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
        ),
    )
    await first_refresh_started.wait()
    second_task = asyncio.create_task(
        refresh_scoped_oauth_credentials_with_result(
            provider,
            runtime_paths,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
        ),
    )
    await second_lock_attempted.wait()
    assert refresh_tokens == [CHAIN_0]

    release_first_refresh.set()
    first_result, second_result = await asyncio.gather(first_task, second_task)

    assert first_result.credentials is not None
    assert first_result.credentials["refresh_token"] == CHAIN_1
    assert second_result.credentials == first_result.credentials
    assert refresh_tokens == [CHAIN_0, CHAIN_1]
    assert (
        load_scoped_credentials(
            "demo_oauth",
            credentials_manager=credentials_manager,
            worker_target=worker_target,
        )
        == first_result.credentials
    )


@pytest.mark.asyncio
async def test_scoped_oauth_refresh_runs_provider_on_callers_event_loop(tmp_path: Path) -> None:
    """Provider I/O and same-scope waiting must not occupy default-executor workers."""
    runtime_paths = _runtime_paths(tmp_path)
    worker_target = _worker_target()
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    _save_credentials(runtime_paths, worker_target, _credentials(ACCESS_0, CHAIN_0, expires_at=1.0))
    caller_loop = asyncio.get_running_loop()
    caller_thread = threading.get_ident()

    async def refresh(credentials: Mapping[str, Any]) -> dict[str, Any]:
        assert credentials["refresh_token"] == CHAIN_0
        assert asyncio.get_running_loop() is caller_loop
        assert threading.get_ident() == caller_thread
        return _credentials(f"access-{CHAIN_1}", CHAIN_1, expires_at=FUTURE_EXPIRES_AT)

    result = await refresh_scoped_oauth_credentials_with_result(
        _FakeOAuthProvider(refresh),
        runtime_paths,
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )

    assert result.refreshed is True


@pytest.mark.asyncio
async def test_scoped_oauth_refresh_publishes_rotation_before_propagating_cancellation(tmp_path: Path) -> None:
    """Cancellation after provider rotation must wait for local credential publication."""
    runtime_paths = _runtime_paths(tmp_path)
    worker_target = _worker_target()
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    _save_credentials(runtime_paths, worker_target, _credentials(ACCESS_0, CHAIN_0, expires_at=1.0))
    provider_rotated = asyncio.Event()
    release_provider_result = asyncio.Event()

    async def refresh(credentials: Mapping[str, Any]) -> dict[str, Any]:
        assert credentials["refresh_token"] == CHAIN_0
        provider_rotated.set()
        await release_provider_result.wait()
        return _credentials(f"access-{CHAIN_1}", CHAIN_1, expires_at=FUTURE_EXPIRES_AT)

    refresh_task = asyncio.create_task(
        refresh_scoped_oauth_credentials_with_result(
            _FakeOAuthProvider(refresh),
            runtime_paths,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
        ),
    )
    await provider_rotated.wait()

    refresh_task.cancel()
    await asyncio.sleep(0)
    assert not refresh_task.done()
    refresh_task.cancel()
    release_provider_result.set()

    with pytest.raises(asyncio.CancelledError):
        await refresh_task
    stored = load_scoped_credentials(
        "demo_oauth",
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )
    assert stored is not None
    assert stored["refresh_token"] == CHAIN_1


@pytest.mark.asyncio
async def test_scoped_oauth_refresh_logs_success_without_tokens(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A normal refresh should emit one structured success log without token values."""
    runtime_paths = _runtime_paths(tmp_path)
    worker_target = _worker_target()
    _save_credentials(runtime_paths, worker_target, _credentials(ACCESS_0, CHAIN_0, expires_at=1.0))
    logger = _CapturingLogger()
    monkeypatch.setattr(oauth_service_module, "logger", logger, raising=False)

    async def refresh(credentials: Mapping[str, Any]) -> dict[str, Any]:
        assert credentials["refresh_token"] == CHAIN_0
        return _credentials(f"access-{CHAIN_1}", CHAIN_1, expires_at=FUTURE_EXPIRES_AT)

    provider = _FakeOAuthProvider(refresh)
    result = await refresh_scoped_oauth_credentials_with_result(
        provider,
        runtime_paths,
        credentials_manager=get_runtime_credentials_manager(runtime_paths),
        worker_target=worker_target,
    )

    assert result.refreshed is True
    assert result.credentials is not None
    assert result.credentials["refresh_token"] == CHAIN_1
    assert logger.info_calls == [
        (
            "oauth_credentials_refreshed",
            {
                "provider_id": "demo_provider",
                "credential_service": "demo_oauth",
                "reason": "refreshed",
                "stale_retry_used": False,
                "has_refresh_token": True,
                "expires_at": FUTURE_EXPIRES_AT,
            },
        ),
    ]
    assert logger.warning_calls == []
    _assert_no_token_values_logged(logger)


@pytest.mark.asyncio
async def test_scoped_oauth_refresh_logs_stale_retry_recovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A recovered stale-token retry should be visible in the central success log."""
    runtime_paths = _runtime_paths(tmp_path)
    worker_target = _worker_target()
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    _save_credentials(runtime_paths, worker_target, _credentials(ACCESS_0, CHAIN_0, expires_at=1.0))
    logger = _CapturingLogger()
    monkeypatch.setattr(oauth_service_module, "logger", logger, raising=False)
    seen_refresh_tokens: list[str] = []

    async def refresh(credentials: Mapping[str, Any]) -> dict[str, Any]:
        refresh_token = str(credentials["refresh_token"])
        seen_refresh_tokens.append(refresh_token)
        if refresh_token == CHAIN_0:
            save_scoped_credentials(
                "demo_oauth",
                _credentials(f"access-{CHAIN_1}", CHAIN_1, expires_at=time.time() + 3600),
                credentials_manager=credentials_manager,
                worker_target=worker_target,
            )
            raise OAuthRefreshRejectedError(INVALID_ROTATION, oauth_error=INVALID_ROTATION)
        assert refresh_token == CHAIN_1
        return _credentials(f"access-{CHAIN_2}", CHAIN_2, expires_at=FUTURE_EXPIRES_AT)

    provider = _FakeOAuthProvider(refresh)
    result = await refresh_scoped_oauth_credentials_with_result(
        provider,
        runtime_paths,
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )

    stored_credentials = load_scoped_credentials(
        "demo_oauth",
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )
    assert result.refreshed is True
    assert result.credentials is not None
    assert result.credentials["refresh_token"] == CHAIN_2
    assert stored_credentials is not None
    assert stored_credentials["refresh_token"] == CHAIN_2
    assert seen_refresh_tokens == [CHAIN_0, CHAIN_1]
    assert logger.info_calls == [
        (
            "oauth_credentials_refreshed",
            {
                "provider_id": "demo_provider",
                "credential_service": "demo_oauth",
                "reason": "stale_retry_refreshed",
                "stale_retry_used": True,
                "has_refresh_token": True,
                "expires_at": FUTURE_EXPIRES_AT,
            },
        ),
    ]
    assert logger.warning_calls == []
    _assert_no_token_values_logged(logger)


@pytest.mark.asyncio
async def test_scoped_oauth_refresh_preserves_reconnect_with_same_refresh_token(tmp_path: Path) -> None:
    """A reconnect that reuses the refresh token must supersede the attempted credential snapshot."""
    runtime_paths = _runtime_paths(tmp_path)
    worker_target = _worker_target()
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    _save_credentials(runtime_paths, worker_target, _credentials(ACCESS_0, CHAIN_0, expires_at=1.0))
    seen_access_tokens: list[str] = []

    async def refresh(credentials: Mapping[str, Any]) -> dict[str, Any] | None:
        access_token = str(credentials["token"])
        seen_access_tokens.append(access_token)
        if access_token == ACCESS_0:
            save_scoped_credentials(
                "demo_oauth",
                _credentials(RECONNECTED_ACCESS, CHAIN_0, expires_at=FUTURE_EXPIRES_AT),
                credentials_manager=credentials_manager,
                worker_target=worker_target,
            )
            raise OAuthRefreshRejectedError(INVALID_ROTATION, oauth_error=INVALID_ROTATION)
        assert access_token == RECONNECTED_ACCESS
        return None

    result = await refresh_scoped_oauth_credentials_with_result(
        _FakeOAuthProvider(refresh),
        runtime_paths,
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )

    stored_credentials = load_scoped_credentials(
        "demo_oauth",
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )
    assert result.credentials is not None
    assert result.credentials["token"] == RECONNECTED_ACCESS
    assert result.refreshed is False
    assert result.stale_retry_used is True
    assert stored_credentials is not None
    assert stored_credentials["token"] == RECONNECTED_ACCESS
    assert seen_access_tokens == [ACCESS_0, RECONNECTED_ACCESS]


@pytest.mark.asyncio
async def test_scoped_oauth_refresh_accepts_access_token_only_reconnect_after_stale_rejection(tmp_path: Path) -> None:
    """A usable reconnect must supersede stale rejection even without another refresh token."""
    runtime_paths = _runtime_paths(tmp_path)
    worker_target = _worker_target()
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    _save_credentials(runtime_paths, worker_target, _credentials(ACCESS_0, CHAIN_0, expires_at=1.0))
    reconnected_credentials = _credentials(RECONNECTED_ACCESS, CHAIN_1, expires_at=FUTURE_EXPIRES_AT)
    del reconnected_credentials["refresh_token"]
    refresh_calls = 0

    async def refresh(credentials: Mapping[str, Any]) -> dict[str, Any] | None:
        nonlocal refresh_calls
        refresh_calls += 1
        assert credentials["refresh_token"] == CHAIN_0
        save_scoped_credentials(
            "demo_oauth",
            reconnected_credentials,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
        )
        raise OAuthRefreshRejectedError(INVALID_ROTATION, oauth_error=INVALID_ROTATION)

    result = await refresh_scoped_oauth_credentials_with_result(
        _FakeOAuthProvider(refresh),
        runtime_paths,
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )

    assert result == oauth_service_module.OAuthCredentialsRefreshResult(
        credentials=reconnected_credentials,
        refreshed=False,
    )
    assert refresh_calls == 1
    assert (
        load_scoped_credentials(
            "demo_oauth",
            credentials_manager=credentials_manager,
            worker_target=worker_target,
        )
        == reconnected_credentials
    )


@pytest.mark.asyncio
async def test_scoped_oauth_refresh_accepts_reconnect_during_stale_retry(tmp_path: Path) -> None:
    """A usable reconnect committed during the retry must supersede its stale rejection."""
    runtime_paths = _runtime_paths(tmp_path)
    worker_target = _worker_target()
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    _save_credentials(runtime_paths, worker_target, _credentials(ACCESS_0, CHAIN_0, expires_at=1.0))
    reconnected_credentials = _credentials(RECONNECTED_ACCESS, CHAIN_2, expires_at=FUTURE_EXPIRES_AT)
    del reconnected_credentials["refresh_token"]
    seen_refresh_tokens: list[str] = []

    async def refresh(credentials: Mapping[str, Any]) -> dict[str, Any]:
        refresh_token = str(credentials["refresh_token"])
        seen_refresh_tokens.append(refresh_token)
        if refresh_token == CHAIN_0:
            save_scoped_credentials(
                "demo_oauth",
                _credentials(f"access-{CHAIN_1}", CHAIN_1, expires_at=FUTURE_EXPIRES_AT),
                credentials_manager=credentials_manager,
                worker_target=worker_target,
            )
        else:
            assert refresh_token == CHAIN_1
            save_scoped_credentials(
                "demo_oauth",
                reconnected_credentials,
                credentials_manager=credentials_manager,
                worker_target=worker_target,
            )
        raise OAuthRefreshRejectedError(INVALID_ROTATION, oauth_error=INVALID_ROTATION)

    result = await refresh_scoped_oauth_credentials_with_result(
        _FakeOAuthProvider(refresh),
        runtime_paths,
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )

    assert result == oauth_service_module.OAuthCredentialsRefreshResult(
        credentials=reconnected_credentials,
        refreshed=False,
        stale_retry_used=True,
    )
    assert seen_refresh_tokens == [CHAIN_0, CHAIN_1]
    assert (
        load_scoped_credentials(
            "demo_oauth",
            credentials_manager=credentials_manager,
            worker_target=worker_target,
        )
        == reconnected_credentials
    )


@pytest.mark.asyncio
async def test_scoped_oauth_refresh_logs_terminal_failure_without_tokens(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A terminal refresh rejection should emit one structured warning without token values."""
    runtime_paths = _runtime_paths(tmp_path)
    worker_target = _worker_target()
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    _save_credentials(runtime_paths, worker_target, _credentials(ACCESS_0, CHAIN_0, expires_at=1.0))
    logger = _CapturingLogger()
    monkeypatch.setattr(oauth_service_module, "logger", logger, raising=False)

    async def refresh(credentials: Mapping[str, Any]) -> dict[str, Any]:
        assert credentials["refresh_token"] == CHAIN_0
        message = "dead refresh grant"
        description = f"provider detail must not log {CHAIN_0}"
        raise OAuthRefreshRejectedError(
            message,
            oauth_error=INVALID_ROTATION,
            oauth_error_description=description,
        )

    provider = _FakeOAuthProvider(refresh)

    with pytest.raises(OAuthRefreshRejectedError):
        await refresh_scoped_oauth_credentials_with_result(
            provider,
            runtime_paths,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
        )

    assert (
        load_scoped_credentials(
            "demo_oauth",
            credentials_manager=credentials_manager,
            worker_target=worker_target,
        )
        is None
    )
    assert logger.info_calls == []
    assert logger.warning_calls == [
        (
            "oauth_credentials_refresh_failed",
            {
                "provider_id": "demo_provider",
                "credential_service": "demo_oauth",
                "reason": "stale_retry_unavailable",
                "stale_retry_used": False,
                "has_refresh_token": True,
                "expires_at": 1.0,
                "error_type": "OAuthRefreshRejectedError",
                "oauth_error": INVALID_ROTATION,
            },
        ),
    ]
    _assert_no_token_values_logged(logger)


@pytest.mark.asyncio
async def test_scoped_oauth_refresh_logs_non_recoverable_provider_failure_without_tokens(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A non-recoverable provider error should emit the provider_refresh_failed warning."""
    runtime_paths = _runtime_paths(tmp_path)
    worker_target = _worker_target()
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    _save_credentials(runtime_paths, worker_target, _credentials(ACCESS_0, CHAIN_0, expires_at=1.0))
    logger = _CapturingLogger()
    monkeypatch.setattr(oauth_service_module, "logger", logger, raising=False)

    async def refresh(credentials: Mapping[str, Any]) -> dict[str, Any]:
        assert credentials["refresh_token"] == CHAIN_0
        message = "provider unavailable"
        description = f"provider detail must not log {CHAIN_0}"
        raise OAuthProviderError(
            message,
            oauth_error="temporarily_unavailable",
            oauth_error_description=description,
        )

    provider = _FakeOAuthProvider(refresh)

    with pytest.raises(OAuthProviderError):
        await refresh_scoped_oauth_credentials_with_result(
            provider,
            runtime_paths,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
        )

    assert (
        load_scoped_credentials(
            "demo_oauth",
            credentials_manager=credentials_manager,
            worker_target=worker_target,
        )
        is not None
    )
    assert logger.info_calls == []
    assert logger.warning_calls == [
        (
            "oauth_credentials_refresh_failed",
            {
                "provider_id": "demo_provider",
                "credential_service": "demo_oauth",
                "reason": "provider_refresh_failed",
                "stale_retry_used": False,
                "has_refresh_token": True,
                "expires_at": 1.0,
                "error_type": "OAuthProviderError",
                "oauth_error": "temporarily_unavailable",
            },
        ),
    ]
    _assert_no_token_values_logged(logger)


@pytest.mark.asyncio
async def test_scoped_oauth_refresh_does_not_log_unrecognized_provider_error_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Provider-controlled error values must collapse to a bounded log sentinel."""
    runtime_paths = _runtime_paths(tmp_path)
    worker_target = _worker_target()
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    _save_credentials(runtime_paths, worker_target, _credentials(ACCESS_0, CHAIN_0, expires_at=1.0))
    logger = _CapturingLogger()
    monkeypatch.setattr(oauth_service_module, "logger", logger, raising=False)
    provider_error = f"provider echoed {CHAIN_0} " + ("x" * 10_000)

    async def refresh(credentials: Mapping[str, Any]) -> dict[str, Any]:
        assert credentials["refresh_token"] == CHAIN_0
        message = "provider unavailable"
        raise OAuthProviderError(message, oauth_error=provider_error)

    with pytest.raises(OAuthProviderError):
        await refresh_scoped_oauth_credentials_with_result(
            _FakeOAuthProvider(refresh),
            runtime_paths,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
        )

    assert logger.warning_calls[0][1]["oauth_error"] == "unrecognized"
    assert provider_error not in repr(logger.warning_calls)
    _assert_no_token_values_logged(logger)


@pytest.mark.asyncio
async def test_scoped_oauth_refresh_logs_stale_retry_failure_without_tokens(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed stale-token retry should emit the stale_retry_failed warning."""
    runtime_paths = _runtime_paths(tmp_path)
    worker_target = _worker_target()
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    _save_credentials(runtime_paths, worker_target, _credentials(ACCESS_0, CHAIN_0, expires_at=1.0))
    logger = _CapturingLogger()
    monkeypatch.setattr(oauth_service_module, "logger", logger, raising=False)
    seen_refresh_tokens: list[str] = []

    async def refresh(credentials: Mapping[str, Any]) -> dict[str, Any]:
        refresh_token = str(credentials["refresh_token"])
        seen_refresh_tokens.append(refresh_token)
        if refresh_token == CHAIN_0:
            save_scoped_credentials(
                "demo_oauth",
                _credentials(f"access-{CHAIN_1}", CHAIN_1, expires_at=FUTURE_EXPIRES_AT),
                credentials_manager=credentials_manager,
                worker_target=worker_target,
            )
            raise OAuthRefreshRejectedError(INVALID_ROTATION, oauth_error=INVALID_ROTATION)
        assert refresh_token == CHAIN_1
        message = "latest refresh grant rejected"
        description = f"provider detail must not log {CHAIN_1}"
        raise OAuthRefreshRejectedError(
            message,
            oauth_error=INVALID_ROTATION,
            oauth_error_description=description,
        )

    provider = _FakeOAuthProvider(refresh)

    with pytest.raises(OAuthRefreshRejectedError):
        await refresh_scoped_oauth_credentials_with_result(
            provider,
            runtime_paths,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
        )

    assert seen_refresh_tokens == [CHAIN_0, CHAIN_1]
    assert logger.info_calls == []
    assert logger.warning_calls == [
        (
            "oauth_credentials_refresh_failed",
            {
                "provider_id": "demo_provider",
                "credential_service": "demo_oauth",
                "reason": "stale_retry_failed",
                "stale_retry_used": True,
                "has_refresh_token": True,
                "expires_at": FUTURE_EXPIRES_AT,
                "error_type": "OAuthRefreshRejectedError",
                "oauth_error": INVALID_ROTATION,
            },
        ),
    ]
    _assert_no_token_values_logged(logger)
