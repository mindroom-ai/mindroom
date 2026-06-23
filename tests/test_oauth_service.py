"""Tests for shared OAuth service helpers."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest

import mindroom.oauth.service as oauth_service_module
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.credentials import get_runtime_credentials_manager, load_scoped_credentials, save_scoped_credentials
from mindroom.oauth.providers import OAuthClientConfig, OAuthRefreshRejectedError
from mindroom.oauth.service import refresh_scoped_oauth_credentials_with_result
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping
    from pathlib import Path
    from typing import Any

    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

ACCESS_0 = "access-refresh-0"
CHAIN_0 = "refresh-0"
CHAIN_1 = "refresh-1"
CHAIN_2 = "refresh-2"
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
