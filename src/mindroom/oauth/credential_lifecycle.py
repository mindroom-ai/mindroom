"""Serialized ownership of MindRoom-managed OAuth credentials."""

from __future__ import annotations

import asyncio
import math
import os
import threading
import time
from concurrent.futures import Future as ConcurrentFuture
from contextvars import ContextVar, copy_context
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, NoReturn, cast

from mindroom.background_tasks import run_coroutine_until_complete
from mindroom.logging_config import get_logger
from mindroom.oauth.credential_store import (
    OAuthCredentialTransaction,
    OAuthCredentialUnreadableError,
    oauth_credential_reader,
    oauth_credential_transaction,
)
from mindroom.oauth.providers import (
    OAuthClaimValidationError,
    OAuthProviderError,
    OAuthRefreshRejectedError,
    OAuthTokenResult,
    is_terminal_oauth_refresh_error_code,
)
from mindroom.tool_system.worker_routing import resolve_worker_target

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection, Coroutine, Mapping

    from mindroom.config.auth import AuthorizationConfig
    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
    from mindroom.oauth.providers import OAuthClientConfig, OAuthProvider
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget, ToolExecutionIdentity

_OAUTH_ACCESS_TOKEN_EXPIRY_SKEW_SECONDS = 60
_OAUTH_REFRESH_FAILED_MESSAGE = "OAuth credential refresh failed"
_OAUTH_TRANSACTION_REENTRY_MESSAGE = "OAuth provider adapters cannot re-enter the credential lifecycle"
_UNRECOGNIZED_OAUTH_ERROR_CODE = "unrecognized"
_LOGGABLE_OAUTH_ERROR_CODES = frozenset(
    {
        "access_denied",
        "authorization_pending",
        "bad_refresh_token",
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
            coroutine.close()
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
_oauth_provider_adapter_active: ContextVar[bool] = ContextVar("oauth_provider_adapter_active", default=False)


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
    if _oauth_provider_adapter_active.get():
        coroutine.close()
        raise RuntimeError(_OAUTH_TRANSACTION_REENTRY_MESSAGE)

    async def wait_for_transaction() -> Result:
        transaction_loop = await asyncio.to_thread(_get_oauth_transaction_loop)
        future = transaction_loop.submit(coroutine)
        return await asyncio.wrap_future(future)

    return await run_coroutine_until_complete(wait_for_transaction())


async def _run_cancellable_oauth_transaction[Result](
    coroutine_factory: Callable[[], Coroutine[Any, Any, Result]],
) -> Result:
    """Submit work whose transaction defines its own cancellation-safe commit boundary."""
    if _oauth_provider_adapter_active.get():
        raise RuntimeError(_OAUTH_TRANSACTION_REENTRY_MESSAGE)
    transaction_loop = await asyncio.to_thread(_get_oauth_transaction_loop)
    submission = transaction_loop.submit_cancellable(coroutine_factory())
    wrapped_future = asyncio.wrap_future(submission.future)
    try:
        return await asyncio.shield(wrapped_future)
    except asyncio.CancelledError as cancellation:
        submission.cancel()
        source_error: BaseException | None = None
        while not wrapped_future.done():
            try:
                await asyncio.shield(wrapped_future)
            except asyncio.CancelledError:
                continue
            except BaseException as exc:
                source_error = exc
                break
        if source_error is None:
            try:
                wrapped_future.result()
            except BaseException as exc:
                source_error = exc
        if source_error is not None and not isinstance(source_error, asyncio.CancelledError):
            raise cancellation from source_error
        raise


def _run_oauth_transaction_sync[Result](coroutine: Coroutine[Any, Any, Result]) -> Result:
    """Block a synchronous tool on work owned entirely by the transaction loop."""
    if _oauth_provider_adapter_active.get():
        coroutine.close()
        raise RuntimeError(_OAUTH_TRANSACTION_REENTRY_MESSAGE)
    return _get_oauth_transaction_loop().submit(coroutine).result()


@dataclass(frozen=True, slots=True)
class OAuthCredentialContext:
    """Canonical runtime identity for one OAuth credential scope."""

    provider: OAuthProvider
    runtime_paths: RuntimePaths
    credentials_manager: CredentialsManager
    worker_target: ResolvedWorkerTarget | None


@dataclass(frozen=True, slots=True)
class OAuthCredentialsRefreshResult:
    """Result of one serialized OAuth credential refresh attempt."""

    credentials: dict[str, Any] | None
    refreshed: bool
    generation: str
    connection_generation: str


@dataclass(frozen=True, slots=True)
class OAuthCredentialsSnapshot:
    """Credential data and durable revision read under one operation lock."""

    credentials: dict[str, Any] | None
    generation: str
    connection_generation: str


@dataclass(frozen=True, slots=True)
class OAuthCredentialsStatus:
    """Credential data plus whether recovery requires a decode-free reset."""

    credentials: dict[str, Any] | None
    reset_required: bool


type _OAuthRefreshAdapter = Callable[
    [Mapping[str, Any]],
    Awaitable[dict[str, Any] | None],
]


class OAuthCredentialConflictError(OAuthProviderError):
    """Signal that an OAuth mutation lost its connection-lineage compare-and-swap."""


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
    )


def oauth_credential_generation(context: OAuthCredentialContext) -> str:
    """Return the authoritative durable revision fencing materialized clients."""
    return load_oauth_credentials_snapshot_sync(context).generation


async def oauth_reset_operation_result(context: OAuthCredentialContext, operation_id: str) -> bool | None:
    """Return one completed replayable reset result without starting or finishing work."""
    return await _run_cancellable_oauth_transaction(
        lambda: _oauth_reset_operation_result_read(context, operation_id),
    )


async def _oauth_reset_operation_result_read(
    context: OAuthCredentialContext,
    operation_id: str,
) -> bool | None:
    async with oauth_credential_reader(context) as reader:
        return reader.reset_operation_result(operation_id)


def load_oauth_credentials_snapshot_sync(context: OAuthCredentialContext) -> OAuthCredentialsSnapshot:
    """Load the last committed credentials and durable revision."""
    return _run_oauth_transaction_sync(_load_oauth_credentials_snapshot_read(context))


def load_oauth_credentials_snapshot_if_readable_sync(
    context: OAuthCredentialContext,
) -> OAuthCredentialsSnapshot | None:
    """Load a client-construction snapshot, treating unreadable state as unavailable."""
    try:
        return load_oauth_credentials_snapshot_sync(context)
    except OAuthCredentialUnreadableError:
        return None


async def load_oauth_credentials_snapshot(context: OAuthCredentialContext) -> OAuthCredentialsSnapshot:
    """Load the last committed credentials without waiting for provider I/O."""
    return await _run_cancellable_oauth_transaction(lambda: _load_oauth_credentials_snapshot_read(context))


async def load_oauth_credentials_status(context: OAuthCredentialContext) -> OAuthCredentialsStatus:
    """Load credentials or classify an unreadable payload as requiring reset."""
    try:
        snapshot = await load_oauth_credentials_snapshot(context)
    except OAuthCredentialUnreadableError:
        return OAuthCredentialsStatus(credentials=None, reset_required=True)
    return OAuthCredentialsStatus(credentials=snapshot.credentials, reset_required=False)


async def load_oauth_reset_connection_generation(context: OAuthCredentialContext) -> str:
    """Load the reset CAS generation without requiring readable credentials."""
    return await _run_cancellable_oauth_transaction(
        lambda: _load_oauth_reset_connection_generation_read(context),
    )


async def _load_oauth_reset_connection_generation_read(context: OAuthCredentialContext) -> str:
    async with oauth_credential_reader(context) as reader:
        return reader.generations().connection_generation


async def _load_oauth_credentials_snapshot_read(
    context: OAuthCredentialContext,
) -> OAuthCredentialsSnapshot:
    async with oauth_credential_reader(context) as reader:
        stored = reader.snapshot()
        return OAuthCredentialsSnapshot(
            credentials=stored.credentials,
            generation=stored.generation,
            connection_generation=stored.connection_generation,
        )


async def refresh_oauth_credentials(context: OAuthCredentialContext) -> dict[str, Any] | None:
    """Refresh one credential scope and return its committed snapshot."""
    return (await refresh_oauth_credentials_with_result(context)).credentials


async def refresh_oauth_credentials_with_result(
    context: OAuthCredentialContext,
) -> OAuthCredentialsRefreshResult:
    """Serialize provider refresh and publication for one credential scope."""
    return await _run_cancellable_oauth_transaction(lambda: _refresh_oauth_credentials_transaction(context))


async def _refresh_oauth_credentials_transaction(
    context: OAuthCredentialContext,
) -> OAuthCredentialsRefreshResult:
    async def refresh(credentials: Mapping[str, Any]) -> dict[str, Any] | None:
        return await context.provider.refresh_token_data(credentials, context.runtime_paths)

    async with oauth_credential_transaction(context) as transaction:
        operation = _refresh_oauth_credentials_locked(context, transaction, refresh=refresh)
        return await run_coroutine_until_complete(operation)


async def _refresh_oauth_credentials_locked(
    context: OAuthCredentialContext,
    transaction: OAuthCredentialTransaction,
    *,
    refresh: _OAuthRefreshAdapter,
    scope_validator: Callable[[dict[str, Any]], bool] | None = None,
    expected_connection_generation: str | None = None,
) -> OAuthCredentialsRefreshResult:
    snapshot = transaction.snapshot()
    if expected_connection_generation is not None and snapshot.connection_generation != expected_connection_generation:
        msg = "OAuth connection state is stale because this credential changed"
        raise OAuthCredentialConflictError(msg)
    credentials = snapshot.credentials
    if credentials is None:
        skipped_reason = "missing_credentials"
    elif not oauth_credentials_usable(
        context.provider,
        context.runtime_paths,
        credentials,
        scope_validator=scope_validator,
    ):
        skipped_reason = "unusable_credentials"
    else:
        adapter_scope = _oauth_provider_adapter_active.set(True)
        try:
            refreshed_credentials = await refresh(credentials)
        except OAuthProviderError as exc:
            await _raise_normalized_refresh_error(context, credentials, exc, transaction=transaction)
        finally:
            _oauth_provider_adapter_active.reset(adapter_scope)
        result = _publish_refresh_result(
            context,
            credentials,
            refreshed_credentials,
            transaction=transaction,
        )
        await transaction.commit()
        return result
    _log_oauth_refresh_skipped(context, credentials, reason=skipped_reason)
    result = OAuthCredentialsRefreshResult(
        credentials=credentials,
        refreshed=False,
        generation=snapshot.generation,
        connection_generation=snapshot.connection_generation,
    )
    await transaction.commit()
    return result


def refresh_oauth_credentials_sync(
    context: OAuthCredentialContext,
    refresh: Callable[[Mapping[str, Any]], dict[str, Any] | None],
    *,
    scope_validator: Callable[[dict[str, Any]], bool] | None = None,
    expected_connection_generation: str | None = None,
) -> OAuthCredentialsRefreshResult:
    """Run one synchronous provider adapter on the OAuth transaction owner."""

    async def refresh_transaction() -> OAuthCredentialsRefreshResult:
        async def refresh_adapter(credentials: Mapping[str, Any]) -> dict[str, Any] | None:
            return await asyncio.to_thread(refresh, credentials)

        async with oauth_credential_transaction(context) as transaction:
            return await _refresh_oauth_credentials_locked(
                context,
                transaction,
                refresh=refresh_adapter,
                scope_validator=scope_validator,
                expected_connection_generation=expected_connection_generation,
            )

    return _run_oauth_transaction_sync(refresh_transaction())


def refresh_oauth_credentials_blocking(context: OAuthCredentialContext) -> dict[str, Any] | None:
    """Refresh through the async provider contract for one synchronous tool call."""
    return _run_oauth_transaction_sync(_refresh_oauth_credentials_transaction(context)).credentials


async def exchange_and_store_oauth_credentials(
    context: OAuthCredentialContext,
    code: str,
    code_verifier: str | None,
    *,
    expected_connection_generation: str,
) -> dict[str, Any]:
    """Exchange one code and publish its credential snapshot atomically."""
    return await _run_oauth_transaction(
        _exchange_and_store_oauth_credentials_transaction(
            context,
            code,
            code_verifier,
            expected_connection_generation=expected_connection_generation,
        ),
    )


async def _exchange_and_store_oauth_credentials_transaction(
    context: OAuthCredentialContext,
    code: str,
    code_verifier: str | None,
    *,
    expected_connection_generation: str,
) -> dict[str, Any]:
    async with oauth_credential_transaction(context) as transaction:
        if transaction.generations().connection_generation != expected_connection_generation:
            msg = "OAuth connection state is stale because this credential changed"
            raise OAuthCredentialConflictError(msg)
        return await _exchange_and_store_oauth_credentials_locked(
            context,
            code,
            code_verifier,
            transaction=transaction,
        )


async def _exchange_and_store_oauth_credentials_locked(
    context: OAuthCredentialContext,
    code: str,
    code_verifier: str | None,
    *,
    transaction: OAuthCredentialTransaction,
) -> dict[str, Any]:
    adapter_scope = _oauth_provider_adapter_active.set(True)
    try:
        result = await context.provider.exchange_code(
            code,
            context.runtime_paths,
            code_verifier=code_verifier,
        )
        context.provider.validate_claims(result, context.runtime_paths)
        safe_result = context.provider.token_result_with_safe_claims(result)
    finally:
        _oauth_provider_adapter_active.reset(adapter_scope)
    token_data = _token_data_preserving_refresh_token(
        transaction.snapshot().credentials,
        safe_result.token_data,
    )
    published = transaction.publish(
        token_data,
        advance_connection_generation=True,
    )
    await transaction.commit()
    return published.credentials or {}


async def reset_oauth_credentials(
    context: OAuthCredentialContext,
    *,
    operation_id: str | None = None,
    expected_connection_generation: str | None = None,
) -> bool:
    """Delete one credential, propagating cancellation after transaction settlement."""
    return await _run_cancellable_oauth_transaction(
        lambda: _reset_oauth_credentials_transaction(
            context,
            operation_id=operation_id,
            expected_connection_generation=expected_connection_generation,
        ),
    )


async def _reset_oauth_credentials_transaction(
    context: OAuthCredentialContext,
    *,
    operation_id: str | None,
    expected_connection_generation: str | None,
) -> bool:
    async with oauth_credential_transaction(context) as transaction:
        completed = transaction.reset_operation_result(operation_id) if operation_id is not None else None
        if completed is not None:
            await transaction.commit()
            return completed
        generations = transaction.generations()
        if (
            expected_connection_generation is not None
            and generations.connection_generation != expected_connection_generation
        ):
            msg = "OAuth connection state is stale because this credential changed"
            raise OAuthCredentialConflictError(msg)
        deleted = transaction.reset(operation_id)
        await transaction.commit()
        return deleted


def _publish_refresh_result(
    context: OAuthCredentialContext,
    credentials: dict[str, Any],
    refreshed_credentials: dict[str, Any] | None,
    *,
    transaction: OAuthCredentialTransaction,
) -> OAuthCredentialsRefreshResult:
    if refreshed_credentials is None:
        generations = transaction.generations()
        _log_oauth_refresh_skipped(context, credentials, reason="not_needed")
        return OAuthCredentialsRefreshResult(
            credentials=credentials,
            refreshed=False,
            generation=generations.generation,
            connection_generation=generations.connection_generation,
        )
    published = transaction.publish(
        refreshed_credentials,
        advance_connection_generation=False,
    )
    logger.info(
        "oauth_credentials_refreshed",
        **_oauth_refresh_log_context(context, published.credentials),
        reason="refreshed",
    )
    return OAuthCredentialsRefreshResult(
        credentials=published.credentials,
        refreshed=True,
        generation=published.generation,
        connection_generation=published.connection_generation,
    )


async def _invalidate_rejected_credentials(
    context: OAuthCredentialContext,
    credentials: dict[str, Any],
    exc: OAuthRefreshRejectedError,
    *,
    transaction: OAuthCredentialTransaction,
) -> None:
    _attach_oauth_refresh_failure_context(exc, credentials)
    transaction.reset(None)
    await transaction.commit()
    _log_oauth_refresh_failed(context, credentials, exc, reason="refresh_rejected")


async def _raise_normalized_refresh_error(
    context: OAuthCredentialContext,
    credentials: dict[str, Any],
    exc: OAuthProviderError,
    *,
    transaction: OAuthCredentialTransaction,
) -> NoReturn:
    normalized_error = _normalized_refresh_error(exc)
    if isinstance(normalized_error, OAuthRefreshRejectedError):
        await _invalidate_rejected_credentials(
            context,
            credentials,
            normalized_error,
            transaction=transaction,
        )
    else:
        await transaction.commit()
        _log_oauth_refresh_failed(context, credentials, normalized_error, reason="provider_refresh_failed")
    if normalized_error is exc:
        raise exc
    raise normalized_error from exc


def _normalized_refresh_error(exc: OAuthProviderError) -> OAuthProviderError:
    """Classify refresh failure only from its structured OAuth error code."""
    if is_terminal_oauth_refresh_error_code(exc.oauth_error):
        return OAuthRefreshRejectedError(
            _OAUTH_REFRESH_FAILED_MESSAGE,
            oauth_error=exc.oauth_error,
        )
    return OAuthProviderError(
        _OAUTH_REFRESH_FAILED_MESSAGE,
        oauth_error=exc.oauth_error,
    )


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


def _oauth_credentials_expires_at(credentials: Mapping[str, object] | None) -> float | None:
    """Return one finite stored access-token expiry timestamp."""
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


def oauth_credentials_usable(
    provider: OAuthProvider,
    runtime_paths: RuntimePaths,
    credentials: dict[str, object] | None,
    *,
    now: float | None = None,
    scope_validator: Callable[[dict[str, Any]], bool] | None = None,
) -> bool:
    """Return whether stored OAuth credentials can currently authenticate provider calls."""
    client_config = provider.client_config(runtime_paths)
    if not credentials or client_config is None:
        return False
    if not oauth_credentials_match_client_id(client_config, credentials):
        return False
    if not (
        scope_validator(credentials)
        if scope_validator is not None
        else oauth_credentials_have_required_scopes(provider, credentials)
    ):
        return False
    if not oauth_credentials_satisfy_identity_policy(provider, runtime_paths, credentials):
        return False

    token = credentials.get("token") or credentials.get("access_token")
    has_refresh_token = _refresh_token_value(credentials) is not None
    expires_at = _oauth_credentials_expires_at(credentials)
    if isinstance(token, str) and token:
        return (
            expires_at is None
            or expires_at > (now if now is not None else time.time()) + _OAUTH_ACCESS_TOKEN_EXPIRY_SKEW_SECONDS
            or has_refresh_token
        )
    return expires_at is not None and has_refresh_token


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


def oauth_verified_claim(credentials: dict[str, Any], key: str) -> str | None:
    """Return one non-empty string claim only after provider verification."""
    if credentials.get("_oauth_claims_verified") is not True:
        return None
    claims = credentials.get("_oauth_claims")
    if not isinstance(claims, dict):
        return None
    value = claims.get(key)
    return value if isinstance(value, str) and value else None


def _same_external_identity(existing_credentials: dict[str, Any] | None, token_data: dict[str, Any]) -> bool:
    existing_sub = oauth_verified_claim(existing_credentials or {}, "sub")
    new_sub = oauth_verified_claim(token_data, "sub")
    if existing_sub is not None or new_sub is not None:
        return existing_sub == new_sub

    existing_email = oauth_verified_claim(existing_credentials or {}, "email")
    new_email = oauth_verified_claim(token_data, "email")
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
