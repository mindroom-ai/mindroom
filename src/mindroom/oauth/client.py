"""OAuth-backed toolkit client helpers."""

from __future__ import annotations

import json
import math
import threading
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum, auto
from functools import wraps
from typing import TYPE_CHECKING, Any, NoReturn, Protocol

from google.auth.exceptions import GoogleAuthError, RefreshError
from google.auth.transport import requests as google_requests
from google.oauth2.credentials import Credentials as GoogleOAuthCredentials

from mindroom.oauth.credential_lifecycle import (
    OAuthCredentialConflictError,
    OAuthCredentialContext,
    load_oauth_credentials_snapshot_sync,
    oauth_credential_generation,
    oauth_credentials_have_required_scopes,
    oauth_credentials_match_client_id,
    oauth_credentials_satisfy_identity_policy,
    refresh_oauth_credentials_sync,
    resolve_oauth_credential_context,
)
from mindroom.oauth.providers import (
    OAuthConnectionRequired,
    OAuthProvider,
    OAuthProviderError,
    OAuthRefreshRejectedError,
    is_terminal_oauth_refresh_error_code,
    oauth_connection_required_payload,
)
from mindroom.oauth.service import (
    OAUTH_ACCESS_REJECTED_REASON,
    OAUTH_REFRESH_REJECTED_REASON,
    oauth_connection_required,
)
from mindroom.tool_system.dependencies import ensure_tool_deps
from mindroom.tool_system.runtime_context import get_tool_runtime_context
from mindroom.tool_system.worker_routing import active_tool_execution_identity

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

    from structlog.stdlib import BoundLogger

    from mindroom.config.auth import AuthorizationConfig
    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

_GOOGLE_OAUTH_DEPS = ["google-auth", "google-auth-oauthlib"]
_SANITIZED_GOOGLE_REFRESH_ERROR_MESSAGE = "OAuth credential refresh failed"


class _SanitizedGoogleRefreshError(RefreshError):
    """Refresh failure safe to expose to wrapped toolkit code and logs."""


class _GoogleRefreshGrantMissingError(RuntimeError):
    """Signal that a forced provider retry has no refresh grant to use."""


class _OAuthAuthSource(Enum):
    """Credential source selected for one tool auth attempt."""

    PROVIDED_CREDENTIALS = auto()
    ORIGINAL_AUTH = auto()
    VALID_CREDENTIALS = auto()
    STORED_OAUTH = auto()


class _GoogleRefreshFailure(Enum):
    """Known refresh failures replayed to concurrent callers of one client."""

    TERMINAL = auto()
    PROVIDER = auto()
    MISSING = auto()


@dataclass(slots=True)
class _GoogleRefreshState:
    """Serialized local publication state for one Google credential object."""

    context: OAuthCredentialContext
    connection_generation: str
    snapshot: dict[str, Any]
    lock: threading.RLock = field(default_factory=threading.RLock)
    refresh_completion_generation: int = 0
    last_succeeded: bool = False
    last_failure: _GoogleRefreshFailure | None = None


class _OAuthClientThreadState(threading.local):
    """Per-worker OAuth result state."""

    def __init__(self) -> None:
        self.connection_required = False
        self.connection_reason: str | None = None
        self.entrypoint_depth = 0


class _AuthDescriptor(Protocol):
    """Descriptor contract for unbound tool auth methods."""

    def __get__(self, instance: object, owner: type[object] | None = None) -> Callable[[], None]:
        """Bind the auth method to one tool instance."""


class ScopedOAuthClientMixin:
    """Shared scoped credential loading and refresh logic for OAuth-backed tools."""

    _oauth_provider: OAuthProvider
    _oauth_tool_name: str
    _oauth_logger: BoundLogger
    _runtime_paths: RuntimePaths
    _creds_manager: CredentialsManager
    _worker_target: ResolvedWorkerTarget | None
    _authorization: AuthorizationConfig | None
    _provided_creds: bool
    _provided_credentials: GoogleOAuthCredentials | None
    _provided_credentials_lock: threading.RLock
    _oauth_quota_project_id: str | None
    _defer_to_original_auth: bool
    _original_auth_completed: bool
    _original_auth: Callable[[], None]
    _oauth_call_state: _OAuthClientThreadState
    creds: Any | None
    service: Any | None
    _google_credential_key: object | None

    _adopt_google_credential_revision: Callable[[object], None]
    _reset_google_authorization_rejected: Callable[[], None]
    _consume_google_authorization_rejected: Callable[[], bool]

    def _apply_runtime_original_auth_kwargs(self, kwargs: dict[str, Any]) -> bool:
        """Populate upstream Google auth kwargs from the resolved runtime env."""
        if not kwargs.get("service_account_path"):
            service_account_path = self._runtime_paths.env_value("GOOGLE_SERVICE_ACCOUNT_FILE")
            if service_account_path:
                kwargs["service_account_path"] = service_account_path
        if not kwargs.get("delegated_user"):
            delegated_user = self._runtime_paths.env_value("GOOGLE_DELEGATED_USER")
            if delegated_user:
                kwargs["delegated_user"] = delegated_user
        return bool(kwargs.get("service_account_path"))

    def _initialize_oauth_client(
        self,
        *,
        worker_target: ResolvedWorkerTarget | None,
        authorization: AuthorizationConfig | None,
        provided_creds: Any,  # noqa: ANN401
        logger: BoundLogger,
        defer_to_original_auth: bool = False,
        quota_project_id: str | None = None,
    ) -> Any:  # noqa: ANN401
        """Prepare OAuth state and initial credentials for the tool."""
        self._worker_target = worker_target
        self._authorization = authorization
        self._provided_creds = provided_creds is not None
        self._provided_credentials_lock = threading.RLock()
        self._oauth_quota_project_id = quota_project_id
        self._provided_credentials = (
            self._copy_supplied_google_credentials(provided_creds) if provided_creds is not None else None
        )
        self._oauth_logger = logger
        self.functions = {}
        self._defer_to_original_auth = defer_to_original_auth
        self._original_auth_completed = False
        self._oauth_call_state = _OAuthClientThreadState()
        if self._provided_credentials is not None:
            return self._provided_credentials
        if defer_to_original_auth:
            return None
        return self._load_stored_credentials()

    def _set_original_auth(self, auth_method: _AuthDescriptor) -> None:
        """Store the bound parent auth callable for fallback."""
        self._original_auth = auth_method.__get__(self, type(self))

    def _wrap_oauth_function_entrypoints(self) -> None:
        """Return structured OAuth prompts from every registered toolkit function."""
        for function in self.functions.values():
            entrypoint = function.entrypoint
            if entrypoint is None:
                continue

            @wraps(entrypoint)
            def oauth_entrypoint(
                *args: object,
                _entrypoint: Callable[..., object] = entrypoint,
                **kwargs: object,
            ) -> object:
                if self._provided_creds:
                    with self._provided_credentials_lock:
                        return self._run_oauth_entrypoint(_entrypoint, args, kwargs)
                return self._run_oauth_entrypoint(_entrypoint, args, kwargs)

            function.entrypoint = oauth_entrypoint
            setattr(self, function.name, oauth_entrypoint)

    def _run_oauth_entrypoint(
        self,
        entrypoint: Callable[..., object],
        args: tuple[object, ...],
        kwargs: dict[str, object],
    ) -> object:
        """Run one wrapped call while retaining its thread-local auth outcome."""
        with self._oauth_entrypoint_scope() as outermost:
            return self._run_scoped_oauth_entrypoint(entrypoint, args, kwargs, outermost=outermost)

    @contextmanager
    def _oauth_entrypoint_scope(self) -> Iterator[bool]:
        """Let only the outermost nested tool entrypoint own shared auth outcomes."""
        state = self._oauth_call_state
        outermost = state.entrypoint_depth == 0
        if outermost:
            self._clear_oauth_connection_required()
            self._reset_google_authorization_rejected()
        state.entrypoint_depth += 1
        try:
            yield outermost
        finally:
            state.entrypoint_depth -= 1
            if outermost:
                self._clear_oauth_connection_required()
                self._reset_google_authorization_rejected()

    def _run_scoped_oauth_entrypoint(
        self,
        entrypoint: Callable[..., object],
        args: tuple[object, ...],
        kwargs: dict[str, object],
        *,
        outermost: bool,
    ) -> object:
        """Run one entrypoint and translate shared auth state at its outer boundary."""
        try:
            if result := self._ensure_structured_auth():
                return result
            result = entrypoint(*args, **kwargs)
        except RefreshError:
            if not outermost:
                raise
            if translated := self._outer_refresh_error_result():
                return translated
            raise
        except Exception:
            if not outermost:
                raise
            if translated := self._google_access_rejection_result():
                return translated
            raise
        else:
            if not outermost:
                return result
            return self._outer_oauth_entrypoint_result(result)

    def _outer_refresh_error_result(self) -> str | None:
        """Translate shared auth state after an outer RefreshError."""
        if rejected := self._google_access_rejection_result():
            return rejected
        required, reason = self._consume_oauth_connection_required()
        if not required:
            return None
        return self._structured_auth_failure(self._connection_required(reason=reason))

    def _outer_oauth_entrypoint_result(self, result: object) -> object:
        """Replace an outer tool result when nested auth state requires it."""
        if rejected := self._google_access_rejection_result():
            return rejected
        required, reason = self._consume_oauth_connection_required()
        if not required:
            return result
        return self._structured_auth_failure(self._connection_required(reason=reason))

    def _google_access_rejection_result(self) -> str | None:
        """Translate a final managed resource 401 after provider refresh retries."""
        rejected = self._consume_google_authorization_rejected()
        if not rejected or self._provided_creds or self._defer_to_original_auth:
            return None
        self.creds = None
        self.service = None
        return self._structured_auth_failure(
            self._connection_required(reason=OAUTH_ACCESS_REJECTED_REASON),
        )

    def _copy_supplied_google_credentials(self, credentials: Any) -> GoogleOAuthCredentials:  # noqa: ANN401
        """Copy supported caller credentials into private blocking credentials."""
        if type(credentials) is not GoogleOAuthCredentials:
            msg = "Google creds must be an exact google.oauth2.credentials.Credentials instance"
            raise TypeError(msg)
        if credentials.refresh_handler is not None:
            msg = "Google creds with refresh_handler are not supported"
            raise ValueError(msg)
        if credentials._enable_reauth_refresh:
            msg = "Google creds with reauth refresh are not supported"
            raise ValueError(msg)
        return GoogleOAuthCredentials(
            token=credentials.token,
            refresh_token=credentials.refresh_token,
            id_token=credentials.id_token,
            token_uri=credentials.token_uri,
            client_id=credentials.client_id,
            client_secret=credentials.client_secret,
            scopes=tuple(credentials.scopes) if credentials.scopes is not None else None,
            default_scopes=tuple(credentials.default_scopes) if credentials.default_scopes is not None else None,
            quota_project_id=self._oauth_quota_project_id or credentials.quota_project_id,
            expiry=credentials.expiry,
            rapt_token=credentials.rapt_token,
            refresh_handler=None,
            enable_reauth_refresh=False,
            granted_scopes=tuple(credentials.granted_scopes) if credentials.granted_scopes is not None else None,
            trust_boundary=deepcopy(credentials._trust_boundary),
            universe_domain=credentials.universe_domain,
            account=credentials.account,
        )

    def _load_token_data(self) -> dict[str, Any] | None:
        """Load OAuth credentials for the current execution scope."""
        return load_oauth_credentials_snapshot_sync(self._oauth_credential_context()).credentials

    def _oauth_credential_context(self) -> OAuthCredentialContext:
        execution_identity = active_tool_execution_identity(None)
        if execution_identity is None and self._worker_target is not None:
            execution_identity = self._worker_target.execution_identity
        runtime_context = get_tool_runtime_context()
        authorization = runtime_context.config.authorization if runtime_context is not None else self._authorization
        return resolve_oauth_credential_context(
            self._oauth_provider,
            self._runtime_paths,
            self._creds_manager,
            self._worker_target,
            execution_identity=execution_identity,
            authorization=authorization,
        )

    def _connection_required(self, *, reason: str | None = None) -> OAuthConnectionRequired:
        return oauth_connection_required(self._oauth_credential_context(), reason=reason)

    def _raise_connection_required(self) -> NoReturn:
        raise self._connection_required()

    def _structured_auth_failure(self, exc: OAuthConnectionRequired) -> str:
        return json.dumps(oauth_connection_required_payload(exc))

    def _ensure_structured_auth(self) -> str | None:
        try:
            auth_source = self._select_auth_source()
            if auth_source in {_OAuthAuthSource.PROVIDED_CREDENTIALS, _OAuthAuthSource.VALID_CREDENTIALS}:
                return None
            if auth_source is _OAuthAuthSource.ORIGINAL_AUTH:
                self._auth_with_original_fallback()
                return None
            self._auth_with_stored_oauth()
        except OAuthConnectionRequired as exc:
            self._mark_oauth_connection_required(reason=exc.reason)
            return self._structured_auth_failure(exc)
        return None

    def _token_expiry(self, token_data: dict[str, Any]) -> datetime | None:
        expires_at = token_data.get("expires_at")
        if isinstance(expires_at, bool) or not isinstance(expires_at, int | float) or not math.isfinite(expires_at):
            return None
        if expires_at <= 0:
            return None
        return datetime.fromtimestamp(float(expires_at), tz=UTC).replace(tzinfo=None)

    def _expires_at_from_credentials(self, credentials: Any) -> float | None:  # noqa: ANN401
        expiry = getattr(credentials, "expiry", None)
        if expiry is None:
            return None
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        return expiry.timestamp()

    def _raw_credentials_from_token_data(self, token_data: dict[str, Any]) -> Any:  # noqa: ANN401
        """Create an unwrapped Google credential adapter from stored token data."""
        ensure_tool_deps(_GOOGLE_OAUTH_DEPS, self._oauth_tool_name, self._runtime_paths)
        client_config = self._oauth_provider.client_config(self._runtime_paths)
        if client_config is None:
            msg = f"{self._oauth_provider.display_name} OAuth client config is missing."
            raise RuntimeError(msg)
        if not oauth_credentials_match_client_id(client_config, token_data):
            msg = f"{self._oauth_provider.display_name} OAuth token was issued for a different client ID."
            raise RuntimeError(msg)
        scopes = token_data.get("scopes")
        if not isinstance(scopes, list):
            scopes = list(self._oauth_provider.scopes)
        return GoogleOAuthCredentials(
            token=token_data.get("token"),
            refresh_token=token_data.get("refresh_token"),
            token_uri=token_data.get("token_uri") or self._oauth_provider.token_url,
            client_id=token_data.get("client_id") or client_config.client_id,
            client_secret=client_config.client_secret,
            scopes=scopes,
            quota_project_id=self._oauth_quota_project_id,
            expiry=self._token_expiry(token_data),
        )

    def _credentials_from_token_data(
        self,
        token_data: dict[str, Any],
        *,
        context: OAuthCredentialContext,
        connection_generation: str,
    ) -> Any:  # noqa: ANN401
        """Create Google credentials whose lazy refresh uses the lifecycle owner."""
        credentials = self._raw_credentials_from_token_data(token_data)
        refresh_state = _GoogleRefreshState(
            context=context,
            connection_generation=connection_generation,
            snapshot=dict(token_data),
        )

        def tracked_refresh(request: object) -> None:
            self._refresh_google_credentials(credentials, refresh_state, request)

        credentials.refresh = tracked_refresh
        original_before_request = credentials.before_request

        def tracked_before_request(*args: object, **kwargs: object) -> None:
            with refresh_state.lock:
                original_before_request(*args, **kwargs)

        credentials.before_request = tracked_before_request
        return credentials

    def _refresh_google_credentials(
        self,
        credentials: Any,  # noqa: ANN401
        state: _GoogleRefreshState,
        request: object,
    ) -> None:
        """Serialize one client's provider refresh and local credential publication."""
        observed_completion_generation = state.refresh_completion_generation
        if not state.lock.acquire(blocking=False):
            state.lock.acquire()
        try:
            if state.refresh_completion_generation != observed_completion_generation:
                if state.last_succeeded:
                    return
                if state.last_failure is not None:
                    self._raise_google_refresh_failure(state.last_failure)
            try:
                self._refresh_google_credentials_locked(credentials, state, request)
            finally:
                state.refresh_completion_generation += 1
        finally:
            state.lock.release()

    def _refresh_google_credentials_locked(
        self,
        credentials: Any,  # noqa: ANN401
        state: _GoogleRefreshState,
        request: object,
    ) -> None:
        """Refresh and publish while holding one materialized client's refresh lock."""
        state.last_succeeded = False
        state.last_failure = None
        triggering_snapshot = dict(state.snapshot)

        def refresh_if_unchanged(current: Mapping[str, Any]) -> dict[str, Any] | None:
            return self._refresh_google_token_if_snapshot_current(current, triggering_snapshot, request)

        context = state.context
        try:
            result = refresh_oauth_credentials_sync(
                context,
                refresh_if_unchanged,
                scope_validator=self._stored_credentials_have_required_scopes,
                expected_connection_generation=state.connection_generation,
            )
        except _GoogleRefreshGrantMissingError:
            state.last_failure = _GoogleRefreshFailure.MISSING
            self._raise_google_refresh_failure(state.last_failure)
        except OAuthCredentialConflictError:
            state.last_failure = _GoogleRefreshFailure.MISSING
            self._raise_google_refresh_failure(state.last_failure)
        except OAuthRefreshRejectedError:
            state.last_failure = _GoogleRefreshFailure.TERMINAL
            self._raise_google_refresh_failure(state.last_failure)
        except OAuthProviderError:
            state.last_failure = _GoogleRefreshFailure.PROVIDER
            self._raise_google_refresh_failure(state.last_failure)
        if result.credentials is None:
            state.last_failure = _GoogleRefreshFailure.MISSING
            self._raise_google_refresh_failure(state.last_failure)
        refreshed = self._raw_credentials_from_token_data(result.credentials)
        if not refreshed.valid:
            state.last_failure = _GoogleRefreshFailure.MISSING
            self._raise_google_refresh_failure(state.last_failure)
        credentials.token = refreshed.token
        credentials.expiry = refreshed.expiry
        credentials._refresh_token = refreshed.refresh_token
        state.snapshot.clear()
        state.snapshot.update(result.credentials)
        state.connection_generation = result.connection_generation
        self._adopt_google_credential_revision((context, result.generation))
        if result.refreshed and refreshed.token == (
            triggering_snapshot.get("token") or triggering_snapshot.get("access_token")
        ):
            state.last_failure = _GoogleRefreshFailure.PROVIDER
            self._raise_google_refresh_failure(state.last_failure)
        state.last_succeeded = True

    def _refresh_google_token_if_snapshot_current(
        self,
        current: Mapping[str, Any],
        triggering_snapshot: Mapping[str, Any],
        request: object,
    ) -> dict[str, Any] | None:
        """Refresh only the bearer that triggered this provider retry."""
        if dict(current) != dict(triggering_snapshot):
            return None
        if not current.get("refresh_token"):
            raise _GoogleRefreshGrantMissingError
        return self._refresh_google_token_data(current, request, force=True)

    def _raise_google_refresh_failure(self, failure: _GoogleRefreshFailure) -> NoReturn:
        """Replay one sanitized refresh outcome to the current caller thread."""
        if failure is _GoogleRefreshFailure.TERMINAL:
            self._mark_oauth_connection_required(reason=OAUTH_REFRESH_REJECTED_REASON)
            self.service = None
        elif failure is _GoogleRefreshFailure.MISSING:
            self._mark_oauth_connection_required()
        raise _SanitizedGoogleRefreshError(_SANITIZED_GOOGLE_REFRESH_ERROR_MESSAGE) from None

    def _refresh_google_token_data(
        self,
        token_data: dict[str, Any] | Mapping[str, Any],
        request: object,
        *,
        force: bool = False,
    ) -> dict[str, Any] | None:
        """Adapt one Google refresh call to the lifecycle provider contract."""
        current = dict(token_data)
        credentials = self._raw_credentials_from_token_data(current)
        if not force and not credentials.expired:
            return None
        if not credentials.refresh_token:
            return None
        try:
            credentials.refresh(request)
        except GoogleAuthError as exc:
            error_code = _google_refresh_error_code(exc) if isinstance(exc, RefreshError) else None
            if is_terminal_oauth_refresh_error_code(error_code):
                raise OAuthRefreshRejectedError(
                    _SANITIZED_GOOGLE_REFRESH_ERROR_MESSAGE,
                    oauth_error=error_code,
                ) from exc
            raise OAuthProviderError(
                _SANITIZED_GOOGLE_REFRESH_ERROR_MESSAGE,
                oauth_error=error_code,
            ) from exc
        refreshed = dict(current)
        refreshed["token"] = credentials.token
        refreshed_expires_at = self._expires_at_from_credentials(credentials)
        if refreshed_expires_at is not None:
            refreshed["expires_at"] = refreshed_expires_at
        if credentials.refresh_token:
            refreshed["refresh_token"] = credentials.refresh_token
        return refreshed

    def _consume_oauth_connection_required(self) -> tuple[bool, str | None]:
        required = self._oauth_call_state.connection_required
        reason = self._oauth_call_state.connection_reason
        self._clear_oauth_connection_required()
        if required:
            self.creds = None
            self.service = None
        return required, reason if isinstance(reason, str) else None

    def _mark_oauth_connection_required(self, *, reason: str | None = None) -> None:
        self._oauth_call_state.connection_required = True
        self._oauth_call_state.connection_reason = reason

    def _clear_oauth_connection_required(self) -> None:
        self._oauth_call_state.connection_required = False
        self._oauth_call_state.connection_reason = None

    def _load_stored_credentials(self) -> Any | None:  # noqa: ANN401
        """Load stored credentials for the current execution scope."""
        context = self._oauth_credential_context()
        snapshot = load_oauth_credentials_snapshot_sync(context)
        self._google_credential_key = (context, snapshot.generation)
        token_data = snapshot.credentials
        if not token_data:
            return None
        if not self._stored_credentials_have_required_scopes(token_data):
            self._oauth_logger.warning(
                "oauth_credentials_missing_required_scopes",
                tool_name=self._oauth_tool_name,
                provider_id=self._oauth_provider.id,
            )
            return None
        if not oauth_credentials_satisfy_identity_policy(self._oauth_provider, self._runtime_paths, token_data):
            self._oauth_logger.warning(
                "oauth_credentials_identity_policy_failed",
                tool_name=self._oauth_tool_name,
                provider_id=self._oauth_provider.id,
            )
            return None
        try:
            creds = self._credentials_from_token_data(
                token_data,
                context=context,
                connection_generation=snapshot.connection_generation,
            )
        except Exception:
            self._oauth_logger.exception("oauth_credentials_load_failed", tool_name=self._oauth_tool_name)
            return None
        self._oauth_logger.info("oauth_credentials_loaded", tool_name=self._oauth_tool_name)
        return creds

    def _stored_credentials_have_required_scopes(self, token_data: dict[str, Any]) -> bool:
        """Return whether stored credentials can authenticate this toolkit."""
        return oauth_credentials_have_required_scopes(self._oauth_provider, token_data)

    def _should_fallback_to_original_auth(self) -> bool:
        """Return whether the tool should defer to its original auth flow."""
        return self._defer_to_original_auth

    def _should_skip_auth(self) -> bool:
        """Return whether tool auth can return early with already-valid provided credentials."""
        if self._provided_credentials is None:
            return False
        self.creds = self._provided_credentials
        self._google_credential_key = None
        return True

    def _select_auth_source(self) -> _OAuthAuthSource:
        """Select the credential source according to the tool auth priority contract."""
        if self._should_skip_auth():
            return _OAuthAuthSource.PROVIDED_CREDENTIALS
        if self._should_fallback_to_original_auth():
            return _OAuthAuthSource.ORIGINAL_AUTH
        self._drop_stale_managed_oauth_credentials()
        if self.creds and self.creds.valid:
            return _OAuthAuthSource.VALID_CREDENTIALS
        return _OAuthAuthSource.STORED_OAUTH

    def _drop_stale_managed_oauth_credentials(self) -> None:
        """Discard this worker's credentials and service when their full key changes."""
        context = self._oauth_credential_context()
        try:
            revision = (context, oauth_credential_generation(context))
        except OAuthProviderError as exc:
            self.creds = None
            self.service = None
            raise self._connection_required() from exc
        if self._google_credential_key != revision:
            self.creds = None
            self.service = None
            self._google_credential_key = revision

    def _auth_with_original_fallback(self) -> None:
        """Authenticate through the wrapped tool's original auth flow."""
        if self._original_auth_completed and self.creds and self.creds.valid:
            return
        self.creds = None
        self._original_auth()
        self._original_auth_completed = True

    def _auth_with_stored_oauth(self) -> None:
        """Authenticate using MindRoom-scoped stored OAuth credentials."""
        try:
            ensure_tool_deps(_GOOGLE_OAUTH_DEPS, self._oauth_tool_name, self._runtime_paths)
            context = self._oauth_credential_context()
            result = refresh_oauth_credentials_sync(
                context,
                lambda current: self._refresh_google_token_data(current, google_requests.Request()),
                scope_validator=self._stored_credentials_have_required_scopes,
            )
            token_data = result.credentials
            if (
                not token_data
                or not self._stored_credentials_have_required_scopes(token_data)
                or not oauth_credentials_satisfy_identity_policy(
                    self._oauth_provider,
                    self._runtime_paths,
                    token_data,
                )
            ):
                self._raise_connection_required()
            credentials = self._credentials_from_token_data(
                token_data,
                context=context,
                connection_generation=result.connection_generation,
            )
            if not credentials.valid:
                self._raise_connection_required()
            self.creds = credentials
            self.service = None
            self._google_credential_key = (context, result.generation)
            self._oauth_logger.info("oauth_authentication_succeeded", tool_name=self._oauth_tool_name)
        except OAuthConnectionRequired:
            raise
        except OAuthRefreshRejectedError as exc:
            self.creds = None
            self.service = None
            raise self._connection_required(reason=OAUTH_REFRESH_REJECTED_REASON) from exc
        except OAuthProviderError:
            raise _SanitizedGoogleRefreshError(_SANITIZED_GOOGLE_REFRESH_ERROR_MESSAGE) from None
        except Exception as exc:
            self._oauth_logger.warning(
                "oauth_authentication_failed",
                tool_name=self._oauth_tool_name,
                error_type=type(exc).__name__,
            )
            raise self._connection_required() from exc

    def _auth(self) -> None:
        """Authenticate using the selected MindRoom or wrapped-tool credential source."""
        auth_source = self._select_auth_source()
        if auth_source in {_OAuthAuthSource.PROVIDED_CREDENTIALS, _OAuthAuthSource.VALID_CREDENTIALS}:
            return
        if auth_source is _OAuthAuthSource.ORIGINAL_AUTH:
            self._auth_with_original_fallback()
            return
        self._auth_with_stored_oauth()


def _google_refresh_error_code(exc: RefreshError) -> str | None:
    """Return Google's structured OAuth error code without inspecting free text."""
    for value in exc.args:
        if isinstance(value, dict):
            error = value.get("error")
            if isinstance(error, str) and error:
                return error
    return None
