"""Serialized ownership of MindRoom-managed OAuth credentials."""

from __future__ import annotations

import asyncio
import json
import math
import os
import secrets
import threading
import time
from concurrent.futures import Future as ConcurrentFuture
from contextlib import suppress
from contextvars import copy_context
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, NoReturn, cast

from mindroom.background_tasks import run_coroutine_until_complete
from mindroom.credentials import (
    delete_scoped_credentials,
    load_scoped_credentials,
    save_scoped_credentials,
    scoped_credentials_path,
)
from mindroom.durable_write import write_json_file_durable
from mindroom.file_locks import async_exclusive_file_lock
from mindroom.logging_config import get_logger
from mindroom.oauth.providers import (
    OAuthClaimValidationError,
    OAuthProviderError,
    OAuthRefreshRejectedError,
    OAuthTokenResult,
    is_terminal_oauth_refresh_error_code,
)
from mindroom.tool_system.worker_routing import resolve_worker_target

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Coroutine, Mapping
    from pathlib import Path

    from mindroom.config.auth import AuthorizationConfig
    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
    from mindroom.oauth.providers import OAuthClientConfig, OAuthProvider
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget, ToolExecutionIdentity

_OAUTH_ACCESS_TOKEN_EXPIRY_SKEW_SECONDS = 60
_OAUTH_REFRESH_FAILED_MESSAGE = "OAuth credential refresh failed"
_UNRECOGNIZED_OAUTH_ERROR_CODE = "unrecognized"
_LOGGABLE_OAUTH_ERROR_CODES = frozenset(
    {
        "access_denied",
        "authorization_pending",
        "expired_token",
        "invalid_client",
        "invalid_grant",
        "invalid_refresh_token",
        "invalid_request",
        "invalid_scope",
        "invalid_target",
        "invalid_token",
        "server_error",
        "slow_down",
        "temporarily_unavailable",
        "unauthorized_client",
        "unsupported_grant_type",
        "unsupported_token_type",
    },
)
_SCOPE_IMPLICATIONS = {
    "https://www.googleapis.com/auth/calendar": frozenset(
        {
            "https://www.googleapis.com/auth/calendar.readonly",
            "https://www.googleapis.com/auth/calendar.events",
            "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
            "https://www.googleapis.com/auth/calendar.freebusy",
            "https://www.googleapis.com/auth/calendar.settings.readonly",
        },
    ),
    "https://www.googleapis.com/auth/drive": frozenset(
        {
            "https://www.googleapis.com/auth/drive.file",
            "https://www.googleapis.com/auth/drive.readonly",
        },
    ),
    "https://www.googleapis.com/auth/gmail.modify": frozenset(
        {
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.compose",
        },
    ),
    "https://www.googleapis.com/auth/spreadsheets": frozenset(
        {"https://www.googleapis.com/auth/spreadsheets.readonly"},
    ),
}

logger = get_logger(__name__)
_INITIAL_CREDENTIAL_GENERATION = "initial"
_CREDENTIAL_GENERATION_KEY = "generation"


@dataclass(frozen=True, slots=True)
class _OAuthTransactionSubmission[Result]:
    """One transaction result plus cancellation routed to its owner loop."""

    future: ConcurrentFuture[Result]
    cancel: Callable[[], None]


class _OAuthTransactionLoop:
    """Process-local event loop that owns every OAuth credential transaction."""

    def __init__(self) -> None:
        self.pid = os.getpid()
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="mindroom-oauth-transactions",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    def submit[Result](self, coroutine: Coroutine[Any, Any, Result]) -> ConcurrentFuture[Result]:
        """Submit one transaction while preserving the caller's context variables."""
        if threading.get_ident() == self._thread.ident:
            msg = "OAuth transactions cannot synchronously submit to their own event loop"
            raise RuntimeError(msg)
        return cast(
            "ConcurrentFuture[Result]",
            copy_context().run(asyncio.run_coroutine_threadsafe, coroutine, self._loop),
        )

    def submit_cancellable[Result](
        self,
        coroutine: Coroutine[Any, Any, Result],
    ) -> _OAuthTransactionSubmission[Result]:
        """Submit work whose source task can be cancelled without losing its final outcome."""
        if threading.get_ident() == self._thread.ident:
            msg = "OAuth transactions cannot synchronously submit to their own event loop"
            raise RuntimeError(msg)
        result: ConcurrentFuture[Result] = ConcurrentFuture()
        task: asyncio.Task[Result] | None = None
        cancel_requested = False

        def complete(completed: asyncio.Task[Result]) -> None:
            if completed.cancelled():
                result.cancel()
                return
            error = completed.exception()
            if error is not None:
                result.set_exception(error)
                return
            result.set_result(completed.result())

        def start() -> None:
            nonlocal task
            task = self._loop.create_task(coroutine)
            task.add_done_callback(complete)
            if cancel_requested:
                task.cancel()

        def cancel() -> None:
            def cancel_on_owner() -> None:
                nonlocal cancel_requested
                cancel_requested = True
                if task is not None:
                    task.cancel()

            self._loop.call_soon_threadsafe(cancel_on_owner)

        self._loop.call_soon_threadsafe(start, context=copy_context())
        return _OAuthTransactionSubmission(future=result, cancel=cancel)

    @property
    def alive(self) -> bool:
        """Return whether this process still has a usable transaction owner."""
        return self.pid == os.getpid() and self._thread.is_alive()


_oauth_transaction_loop: _OAuthTransactionLoop | None = None
_oauth_transaction_loop_guard = threading.Lock()


def _reset_oauth_transaction_loop_after_fork() -> None:
    """Discard parent-process thread state in a forked child."""
    global _oauth_transaction_loop, _oauth_transaction_loop_guard
    _oauth_transaction_loop = None
    _oauth_transaction_loop_guard = threading.Lock()


if os.name == "posix":
    os.register_at_fork(after_in_child=_reset_oauth_transaction_loop_after_fork)


def _get_oauth_transaction_loop() -> _OAuthTransactionLoop:
    """Return the lazy process-lifetime OAuth transaction owner."""
    global _oauth_transaction_loop
    with _oauth_transaction_loop_guard:
        if _oauth_transaction_loop is None or not _oauth_transaction_loop.alive:
            _oauth_transaction_loop = _OAuthTransactionLoop()
        return _oauth_transaction_loop


async def _run_oauth_transaction[Result](coroutine: Coroutine[Any, Any, Result]) -> Result:
    """Await one transaction without allowing caller cancellation to interrupt its commit."""

    async def wait_for_transaction() -> Result:
        transaction_loop = await asyncio.to_thread(_get_oauth_transaction_loop)
        future = transaction_loop.submit(coroutine)
        return await asyncio.wrap_future(future)

    return await run_coroutine_until_complete(wait_for_transaction())


def _run_oauth_transaction_sync[Result](coroutine: Coroutine[Any, Any, Result]) -> Result:
    """Block a synchronous tool on work owned entirely by the transaction loop."""
    return _get_oauth_transaction_loop().submit(coroutine).result()


@dataclass(frozen=True, slots=True)
class OAuthCredentialContext:
    """Canonical runtime identity for one OAuth credential scope."""

    provider: OAuthProvider
    runtime_paths: RuntimePaths
    credentials_manager: CredentialsManager
    worker_target: ResolvedWorkerTarget | None
    allowed_shared_services: frozenset[str] | None = None


@dataclass(frozen=True, slots=True)
class OAuthCredentialsRefreshResult:
    """Result of one serialized OAuth credential refresh attempt."""

    credentials: dict[str, Any] | None
    refreshed: bool


@dataclass(slots=True)
class _OAuthCredentialResetOutcome:
    """Cross-thread reset commit state used to resolve caller cancellation."""

    committed: bool = False
    deleted: bool = False
    error: BaseException | None = None


def oauth_credentials_worker_target(
    provider: OAuthProvider,
    worker_target: ResolvedWorkerTarget | None,
    *,
    execution_identity: ToolExecutionIdentity | None = None,
    authorization: AuthorizationConfig | None = None,
) -> ResolvedWorkerTarget | None:
    """Return one OAuth-only canonical target under the provider identity policy."""
    identity = execution_identity or (worker_target.execution_identity if worker_target is not None else None)
    if identity is not None and identity.requester_id and authorization is not None:
        identity = replace(identity, requester_id=authorization.resolve_alias(identity.requester_id))
    if provider.requester_scoped_credentials:
        if identity is None or not identity.requester_id:
            return None
        worker_scope = "user"
    elif worker_target is None or identity is None or identity == worker_target.execution_identity:
        return worker_target
    else:
        worker_scope = worker_target.worker_scope
    return resolve_worker_target(
        worker_scope,
        worker_target.routing_agent_name if worker_target is not None else identity.agent_name,
        execution_identity=identity,
        tenant_id=worker_target.tenant_id if worker_target is not None else None,
        account_id=worker_target.account_id if worker_target is not None else None,
        private_agent_names=worker_target.private_agent_names if worker_target is not None else None,
    )


def resolve_oauth_credential_context(
    provider: OAuthProvider,
    runtime_paths: RuntimePaths,
    credentials_manager: CredentialsManager,
    worker_target: ResolvedWorkerTarget | None,
    *,
    execution_identity: ToolExecutionIdentity | None = None,
    authorization: AuthorizationConfig | None = None,
    allowed_shared_services: frozenset[str] | None = None,
) -> OAuthCredentialContext:
    """Resolve the canonical identity and storage target for one OAuth credential scope."""
    return OAuthCredentialContext(
        provider=provider,
        runtime_paths=runtime_paths,
        credentials_manager=credentials_manager,
        worker_target=oauth_credentials_worker_target(
            provider,
            worker_target,
            execution_identity=execution_identity,
            authorization=authorization,
        ),
        allowed_shared_services=allowed_shared_services,
    )


def load_oauth_credentials(context: OAuthCredentialContext) -> dict[str, Any] | None:
    """Load credentials for one canonical scope."""
    return load_scoped_credentials(
        context.provider.credential_service,
        credentials_manager=context.credentials_manager,
        worker_target=context.worker_target,
        allowed_shared_services=context.allowed_shared_services,
    )


def oauth_credential_generation(context: OAuthCredentialContext) -> str:
    """Return the durable generation that fences callbacks across credential resets."""
    path = _credential_generation_path(context)
    if not path.exists():
        return _INITIAL_CREDENTIAL_GENERATION
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        msg = "OAuth credential generation state is invalid"
        raise OAuthProviderError(msg) from exc
    generation = payload.get(_CREDENTIAL_GENERATION_KEY) if isinstance(payload, dict) else None
    if not isinstance(generation, str) or not generation:
        msg = "OAuth credential generation state is invalid"
        raise OAuthProviderError(msg)
    return generation


async def refresh_oauth_credentials(context: OAuthCredentialContext) -> dict[str, Any] | None:
    """Refresh one credential scope and return its committed snapshot."""
    return (await refresh_oauth_credentials_with_result(context)).credentials


async def refresh_oauth_credentials_with_result(
    context: OAuthCredentialContext,
) -> OAuthCredentialsRefreshResult:
    """Serialize provider refresh and publication for one credential scope."""
    return await _run_oauth_transaction(_refresh_oauth_credentials_transaction(context))


async def _refresh_oauth_credentials_transaction(
    context: OAuthCredentialContext,
) -> OAuthCredentialsRefreshResult:
    async with async_exclusive_file_lock(_operation_lock_path(context)):
        return await _refresh_oauth_credentials_locked(context)


async def _refresh_oauth_credentials_locked(
    context: OAuthCredentialContext,
) -> OAuthCredentialsRefreshResult:
    credentials = load_oauth_credentials(context)
    if credentials is None:
        _log_oauth_refresh_skipped(context, None, reason="missing_credentials")
        return OAuthCredentialsRefreshResult(credentials=None, refreshed=False)
    if not oauth_credentials_usable(context.provider, context.runtime_paths, credentials):
        _log_oauth_refresh_skipped(context, credentials, reason="unusable_credentials")
        return OAuthCredentialsRefreshResult(credentials=credentials, refreshed=False)
    try:
        refreshed_credentials = await context.provider.refresh_token_data(credentials, context.runtime_paths)
    except OAuthProviderError as exc:
        _raise_normalized_refresh_error(context, credentials, exc)
    return _publish_refresh_result(context, credentials, refreshed_credentials)


def refresh_oauth_credentials_sync(
    context: OAuthCredentialContext,
    refresh: Callable[[Mapping[str, Any]], dict[str, Any] | None],
) -> OAuthCredentialsRefreshResult:
    """Run one synchronous provider adapter on the OAuth transaction owner."""
    return _run_oauth_transaction_sync(_refresh_oauth_credentials_sync_transaction(context, refresh))


async def _refresh_oauth_credentials_sync_transaction(
    context: OAuthCredentialContext,
    refresh: Callable[[Mapping[str, Any]], dict[str, Any] | None],
) -> OAuthCredentialsRefreshResult:
    async with async_exclusive_file_lock(_operation_lock_path(context)):
        credentials = load_oauth_credentials(context)
        if credentials is None:
            _log_oauth_refresh_skipped(context, None, reason="missing_credentials")
            return OAuthCredentialsRefreshResult(credentials=None, refreshed=False)
        if not oauth_credentials_usable(context.provider, context.runtime_paths, credentials):
            _log_oauth_refresh_skipped(context, credentials, reason="unusable_credentials")
            return OAuthCredentialsRefreshResult(credentials=credentials, refreshed=False)
        try:
            refreshed_credentials = await asyncio.to_thread(refresh, credentials)
        except OAuthProviderError as exc:
            _raise_normalized_refresh_error(context, credentials, exc)
        return _publish_refresh_result(context, credentials, refreshed_credentials)


def refresh_oauth_credentials_blocking(context: OAuthCredentialContext) -> dict[str, Any] | None:
    """Refresh through the async provider contract for one synchronous tool call."""
    return _run_oauth_transaction_sync(_refresh_oauth_credentials_transaction(context)).credentials


async def exchange_and_store_oauth_credentials(
    context: OAuthCredentialContext,
    code: str,
    code_verifier: str | None,
    *,
    expected_generation: str,
) -> dict[str, Any]:
    """Exchange one code and publish its credential snapshot atomically."""
    return await _run_oauth_transaction(
        _exchange_and_store_oauth_credentials_transaction(
            context,
            code,
            code_verifier,
            expected_generation=expected_generation,
        ),
    )


async def _exchange_and_store_oauth_credentials_transaction(
    context: OAuthCredentialContext,
    code: str,
    code_verifier: str | None,
    *,
    expected_generation: str,
) -> dict[str, Any]:
    async with async_exclusive_file_lock(_operation_lock_path(context)):
        if oauth_credential_generation(context) != expected_generation:
            msg = "OAuth connection state is stale because this credential was reset"
            raise OAuthProviderError(msg)
        return await _exchange_and_store_oauth_credentials_locked(context, code, code_verifier)


async def _exchange_and_store_oauth_credentials_locked(
    context: OAuthCredentialContext,
    code: str,
    code_verifier: str | None,
) -> dict[str, Any]:
    result = await context.provider.exchange_code(
        code,
        context.runtime_paths,
        code_verifier=code_verifier,
    )
    context.provider.validate_claims(result, context.runtime_paths)
    safe_result = sanitized_oauth_token_result(context.provider, result)
    token_data = _token_data_preserving_refresh_token(
        load_oauth_credentials(context),
        safe_result.token_data,
    )
    save_scoped_credentials(
        context.provider.credential_service,
        token_data,
        credentials_manager=context.credentials_manager,
        worker_target=context.worker_target,
    )
    return token_data


async def reset_oauth_credentials(
    context: OAuthCredentialContext,
) -> bool:
    """Delete one credential, cancelling before its lock or returning after its commit."""
    transaction_loop = await asyncio.to_thread(_get_oauth_transaction_loop)
    outcome = _OAuthCredentialResetOutcome()
    submission = transaction_loop.submit_cancellable(_reset_oauth_credentials_transaction(context, outcome))
    wrapped_future = asyncio.wrap_future(submission.future)
    try:
        return await asyncio.shield(wrapped_future)
    except asyncio.CancelledError as cancellation:
        submission.cancel()
        while not wrapped_future.done():
            try:
                await asyncio.shield(wrapped_future)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        with suppress(BaseException):
            wrapped_future.result()
        if outcome.committed:
            return outcome.deleted
        if outcome.error is not None and not isinstance(outcome.error, asyncio.CancelledError):
            raise cancellation from outcome.error
        raise


async def _reset_oauth_credentials_transaction(
    context: OAuthCredentialContext,
    outcome: _OAuthCredentialResetOutcome,
) -> bool:
    try:
        async with async_exclusive_file_lock(_operation_lock_path(context)):
            credentials = load_oauth_credentials(context)
            _advance_oauth_credential_generation(context)
            if credentials is not None:
                delete_scoped_credentials(
                    context.provider.credential_service,
                    credentials_manager=context.credentials_manager,
                    worker_target=context.worker_target,
                )
            outcome.deleted = credentials is not None
            outcome.committed = True
            return outcome.deleted
    except BaseException as exc:
        outcome.error = exc
        raise


def _operation_lock_path(context: OAuthCredentialContext) -> Path:
    credentials_path = scoped_credentials_path(
        context.provider.credential_service,
        credentials_manager=context.credentials_manager,
        worker_target=context.worker_target,
    )
    return credentials_path.with_name(f"{credentials_path.name}.oauth-operation.lock")


def _credential_generation_path(context: OAuthCredentialContext) -> Path:
    credentials_path = scoped_credentials_path(
        context.provider.credential_service,
        credentials_manager=context.credentials_manager,
        worker_target=context.worker_target,
    )
    return credentials_path.with_name(f"{credentials_path.name}.oauth-generation.json")


def _advance_oauth_credential_generation(context: OAuthCredentialContext) -> None:
    write_json_file_durable(
        _credential_generation_path(context),
        {_CREDENTIAL_GENERATION_KEY: secrets.token_hex(32)},
        strict_atomic_replace=True,
    )


def _publish_refresh_result(
    context: OAuthCredentialContext,
    credentials: dict[str, Any],
    refreshed_credentials: dict[str, Any] | None,
) -> OAuthCredentialsRefreshResult:
    if refreshed_credentials is None:
        _log_oauth_refresh_skipped(context, credentials, reason="not_needed")
        return OAuthCredentialsRefreshResult(credentials=credentials, refreshed=False)
    save_scoped_credentials(
        context.provider.credential_service,
        refreshed_credentials,
        credentials_manager=context.credentials_manager,
        worker_target=context.worker_target,
    )
    logger.info(
        "oauth_credentials_refreshed",
        **_oauth_refresh_log_context(context, refreshed_credentials),
        reason="refreshed",
    )
    return OAuthCredentialsRefreshResult(credentials=refreshed_credentials, refreshed=True)


def _invalidate_rejected_credentials(
    context: OAuthCredentialContext,
    credentials: dict[str, Any],
    exc: OAuthRefreshRejectedError,
) -> None:
    _attach_oauth_refresh_failure_context(exc, credentials)
    delete_scoped_credentials(
        context.provider.credential_service,
        credentials_manager=context.credentials_manager,
        worker_target=context.worker_target,
    )
    _log_oauth_refresh_failed(context, credentials, exc, reason="refresh_rejected")


def _raise_normalized_refresh_error(
    context: OAuthCredentialContext,
    credentials: dict[str, Any],
    exc: OAuthProviderError,
) -> NoReturn:
    normalized_error = _normalized_refresh_error(exc)
    if isinstance(normalized_error, OAuthRefreshRejectedError):
        _invalidate_rejected_credentials(context, credentials, normalized_error)
    else:
        _log_oauth_refresh_failed(context, credentials, normalized_error, reason="provider_refresh_failed")
    if normalized_error is exc:
        raise exc
    raise normalized_error from exc


def _normalized_refresh_error(exc: OAuthProviderError) -> OAuthProviderError:
    """Classify refresh failure only from its structured OAuth error code."""
    if is_terminal_oauth_refresh_error_code(exc.oauth_error):
        if isinstance(exc, OAuthRefreshRejectedError):
            return exc
        return OAuthRefreshRejectedError(
            _OAUTH_REFRESH_FAILED_MESSAGE,
            oauth_error=exc.oauth_error,
        )
    if isinstance(exc, OAuthRefreshRejectedError):
        return OAuthProviderError(
            _OAUTH_REFRESH_FAILED_MESSAGE,
            oauth_error=exc.oauth_error,
        )
    return exc


def _log_oauth_refresh_skipped(
    context: OAuthCredentialContext,
    credentials: dict[str, Any] | None,
    *,
    reason: str,
) -> None:
    logger.debug(
        "oauth_credentials_refresh_skipped",
        **_oauth_refresh_log_context(context, credentials),
        reason=reason,
    )


def _log_oauth_refresh_failed(
    context: OAuthCredentialContext,
    credentials: dict[str, Any],
    exc: OAuthProviderError,
    *,
    reason: str,
) -> None:
    logger.warning(
        "oauth_credentials_refresh_failed",
        **_oauth_refresh_log_context(context, credentials),
        reason=reason,
        error_type=type(exc).__name__,
        oauth_error=_safe_oauth_error_code_for_logging(exc.oauth_error),
    )


def _oauth_refresh_log_context(
    context: OAuthCredentialContext,
    credentials: dict[str, Any] | None,
) -> dict[str, object]:
    return {
        "provider_id": context.provider.id,
        "credential_service": context.provider.credential_service,
        "has_refresh_token": _refresh_token_value(credentials) is not None,
        "expires_at": _oauth_credentials_expires_at(credentials),
    }


def _oauth_credentials_expires_at(credentials: dict[str, Any] | None) -> float | None:
    if credentials is None:
        return None
    expires_at = credentials.get("expires_at")
    if isinstance(expires_at, bool) or not isinstance(expires_at, int | float) or not math.isfinite(expires_at):
        return None
    return float(expires_at)


def _attach_oauth_refresh_failure_context(
    exc: OAuthRefreshRejectedError,
    credentials: dict[str, Any],
) -> None:
    exc.refresh_had_token = _refresh_token_value(credentials) is not None
    exc.refresh_expires_at = _oauth_credentials_expires_at(credentials)


def _safe_oauth_error_code_for_logging(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    if len(value) > 64:
        return _UNRECOGNIZED_OAUTH_ERROR_CODE
    normalized = value.strip().lower()
    if not normalized:
        return None
    if normalized in _LOGGABLE_OAUTH_ERROR_CODES:
        return normalized
    return _UNRECOGNIZED_OAUTH_ERROR_CODE


def _refresh_token_value(credentials: Mapping[str, Any] | None) -> str | None:
    if credentials is None:
        return None
    refresh_token = credentials.get("refresh_token")
    return refresh_token if isinstance(refresh_token, str) and refresh_token else None


def oauth_credentials_usable(  # noqa: PLR0911
    provider: OAuthProvider,
    runtime_paths: RuntimePaths,
    credentials: dict[str, object] | None,
    *,
    now: float | None = None,
) -> bool:
    """Return whether stored OAuth credentials can currently authenticate provider calls."""
    client_config = provider.client_config(runtime_paths)
    if not credentials or client_config is None:
        return False
    if not oauth_credentials_match_client_id(client_config, credentials):
        return False
    if not oauth_credentials_have_required_scopes(provider, credentials):
        return False
    if not oauth_credentials_satisfy_identity_policy(provider, runtime_paths, credentials):
        return False

    token = credentials.get("token") or credentials.get("access_token")
    refresh_token = credentials.get("refresh_token")
    has_refresh_token = isinstance(refresh_token, str) and bool(refresh_token)
    if isinstance(token, str) and token:
        expires_at = credentials.get("expires_at")
        if isinstance(expires_at, bool) or not isinstance(expires_at, int | float) or not math.isfinite(expires_at):
            return True
        return (
            float(expires_at) > (now if now is not None else time.time()) + _OAUTH_ACCESS_TOKEN_EXPIRY_SKEW_SECONDS
            or has_refresh_token
        )

    expires_at = credentials.get("expires_at")
    if isinstance(expires_at, bool) or not isinstance(expires_at, int | float) or not math.isfinite(expires_at):
        return False
    return has_refresh_token


def oauth_credentials_match_client_id(
    client_config: OAuthClientConfig,
    credentials: dict[str, object],
) -> bool:
    """Return whether token credentials belong to the active OAuth app client."""
    stored_client_id = credentials.get("client_id")
    return isinstance(stored_client_id, str) and stored_client_id.strip() == client_config.client_id


def oauth_credentials_have_scopes(
    credentials: Mapping[str, object],
    required_scopes: Collection[str],
) -> bool:
    """Return whether stored credentials include every requested scope."""
    granted_scopes: set[str] = set()
    raw_scopes = credentials.get("scopes")
    if isinstance(raw_scopes, list):
        granted_scopes.update(scope for scope in raw_scopes if isinstance(scope, str) and scope)
    raw_scope = credentials.get("scope")
    if isinstance(raw_scope, str):
        granted_scopes.update(scope for scope in raw_scope.split() if scope)
    expanded_granted_scopes = set(granted_scopes)
    for scope in granted_scopes:
        expanded_granted_scopes.update(_SCOPE_IMPLICATIONS.get(scope, ()))
    return set(required_scopes).issubset(expanded_granted_scopes)


def oauth_credentials_have_required_scopes(provider: OAuthProvider, credentials: dict[str, object]) -> bool:
    """Return whether stored credentials include every provider-required scope."""
    required_scopes = set(provider.scopes)
    if _refresh_token_value(credentials) is not None:
        required_scopes.discard("offline_access")
    return oauth_credentials_have_scopes(credentials, required_scopes)


def oauth_credentials_satisfy_identity_policy(
    provider: OAuthProvider,
    runtime_paths: RuntimePaths,
    credentials: dict[str, object],
) -> bool:
    """Return whether stored credentials still satisfy configured identity policy."""
    has_identity_policy = (
        bool(provider.resolved_allowed_email_domains(runtime_paths))
        or bool(provider.resolved_allowed_hosted_domains(runtime_paths))
        or provider.claim_validator is not None
    )
    if not has_identity_policy:
        return True

    raw_claims = credentials.get("_oauth_claims")
    if not isinstance(raw_claims, dict) or not raw_claims:
        return False
    if credentials.get("_oauth_claims_verified") is not True:
        return False
    claims = cast("dict[str, Any]", raw_claims)
    try:
        provider.validate_claims(
            OAuthTokenResult(
                token_data=dict(credentials),
                claims=claims,
                claims_verified=True,
            ),
            runtime_paths,
        )
    except OAuthClaimValidationError:
        return False
    return True


def sanitized_oauth_token_result(provider: OAuthProvider, result: OAuthTokenResult) -> OAuthTokenResult:
    """Return a token result with only safe claim metadata persisted."""
    return provider.token_result_with_safe_claims(result)


def _claim_str(credentials: dict[str, Any], key: str) -> str | None:
    if credentials.get("_oauth_claims_verified") is not True:
        return None
    claims = credentials.get("_oauth_claims")
    if not isinstance(claims, dict):
        return None
    value = claims.get(key)
    return value if isinstance(value, str) and value else None


def _same_external_identity(existing_credentials: dict[str, Any] | None, token_data: dict[str, Any]) -> bool:
    existing_sub = _claim_str(existing_credentials or {}, "sub")
    new_sub = _claim_str(token_data, "sub")
    if existing_sub is not None or new_sub is not None:
        return existing_sub == new_sub

    existing_email = _claim_str(existing_credentials or {}, "email")
    new_email = _claim_str(token_data, "email")
    return existing_email is not None and existing_email == new_email


def _same_oauth_client(existing_credentials: dict[str, Any] | None, token_data: dict[str, Any]) -> bool:
    existing_client_id = (existing_credentials or {}).get("client_id")
    if not isinstance(existing_client_id, str) or not existing_client_id.strip():
        return False
    token_client_id = token_data.get("client_id")
    return isinstance(token_client_id, str) and token_client_id.strip() == existing_client_id.strip()


def _token_data_preserving_refresh_token(
    existing_credentials: dict[str, Any] | None,
    safe_token_data: dict[str, Any],
) -> dict[str, Any]:
    token_data = dict(safe_token_data)
    existing_refresh_token = (existing_credentials or {}).get("refresh_token")
    if (
        "refresh_token" not in token_data
        and isinstance(existing_refresh_token, str)
        and existing_refresh_token
        and _same_external_identity(existing_credentials, token_data)
        and _same_oauth_client(existing_credentials, token_data)
    ):
        token_data["refresh_token"] = existing_refresh_token
    return token_data
