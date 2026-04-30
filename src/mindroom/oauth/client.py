"""OAuth-backed toolkit client helpers."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from functools import wraps
from typing import TYPE_CHECKING, Any, NoReturn, Protocol

from google.auth.transport import requests as google_requests
from google.oauth2 import credentials as google_credentials

from mindroom.credentials import load_scoped_credentials, save_scoped_credentials
from mindroom.oauth.providers import OAuthConnectionRequired, OAuthProvider
from mindroom.oauth.service import (
    oauth_connect_url,
    oauth_credentials_have_required_scopes,
    oauth_credentials_satisfy_identity_policy,
)
from mindroom.tool_system.dependencies import ensure_tool_deps

if TYPE_CHECKING:
    from collections.abc import Callable

    from structlog.stdlib import BoundLogger

    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

_GOOGLE_OAUTH_DEPS = ["google-auth", "google-auth-oauthlib"]


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
    _provided_creds: bool
    _defer_to_original_auth: bool
    _original_auth_completed: bool
    _original_auth: Callable[[], None]
    creds: Any | None

    def _has_initial_service_account_auth(self, kwargs: dict[str, Any]) -> bool:
        """Return whether construction should skip stored OAuth credentials."""
        return bool(kwargs.get("service_account_path") or self._runtime_paths.env_value("GOOGLE_SERVICE_ACCOUNT_FILE"))

    def _initialize_oauth_client(
        self,
        *,
        worker_target: ResolvedWorkerTarget | None,
        provided_creds: Any,  # noqa: ANN401
        logger: BoundLogger,
        defer_to_original_auth: bool = False,
    ) -> Any:  # noqa: ANN401
        """Prepare OAuth state and initial credentials for the tool."""
        self._worker_target = worker_target
        self._provided_creds = provided_creds is not None
        self._oauth_logger = logger
        self.functions = {}
        self._defer_to_original_auth = defer_to_original_auth
        self._original_auth_completed = False
        if provided_creds is not None:
            return provided_creds
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
                if result := self._ensure_structured_auth():
                    return result
                return _entrypoint(*args, **kwargs)

            function.entrypoint = oauth_entrypoint
            setattr(self, function.name, oauth_entrypoint)

    def _load_token_data(self) -> dict[str, Any] | None:
        """Load OAuth credentials for the current execution scope."""
        return load_scoped_credentials(
            self._oauth_provider.credential_service,
            credentials_manager=self._creds_manager,
            worker_target=self._worker_target,
        )

    def _save_token_data(self, token_data: dict[str, Any]) -> None:
        """Persist refreshed OAuth credentials to the current execution scope."""
        save_scoped_credentials(
            self._oauth_provider.credential_service,
            token_data,
            credentials_manager=self._creds_manager,
            worker_target=self._worker_target,
        )

    def _connection_required(self) -> OAuthConnectionRequired:
        connect_url = oauth_connect_url(
            self._oauth_provider,
            self._runtime_paths,
            worker_target=self._worker_target,
        )
        message = (
            f"{self._oauth_provider.display_name} is not connected for this agent. "
            f"Open this MindRoom link to connect it, then retry the request: {connect_url}"
        )
        return OAuthConnectionRequired(
            message,
            provider_id=self._oauth_provider.id,
            connect_url=connect_url,
        )

    def _raise_connection_required(self) -> NoReturn:
        raise self._connection_required()

    def _structured_auth_failure(self, exc: OAuthConnectionRequired) -> str:
        return json.dumps(
            {
                "error": str(exc),
                "oauth_connection_required": True,
                "provider": exc.provider_id,
                "connect_url": exc.connect_url,
            },
        )

    def _ensure_structured_auth(self) -> str | None:
        if self._should_skip_auth():
            return None
        if self._should_fallback_to_original_auth():
            self._auth()
            return None
        try:
            self._auth()
        except OAuthConnectionRequired as exc:
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

    def _credentials_from_token_data(self, token_data: dict[str, Any]) -> Any:  # noqa: ANN401
        """Create a Google Credentials object from stored token data."""
        ensure_tool_deps(_GOOGLE_OAUTH_DEPS, self._oauth_tool_name, self._runtime_paths)

        client_config = self._oauth_provider.client_config(self._runtime_paths)
        if client_config is None:
            msg = f"{self._oauth_provider.display_name} OAuth client config is missing."
            raise RuntimeError(msg)
        scopes = token_data.get("scopes")
        if not isinstance(scopes, list):
            scopes = list(self._oauth_provider.scopes)
        return google_credentials.Credentials(
            token=token_data.get("token"),
            refresh_token=token_data.get("refresh_token"),
            token_uri=token_data.get("token_uri") or self._oauth_provider.token_url,
            client_id=token_data.get("client_id") or client_config.client_id,
            client_secret=client_config.client_secret,
            scopes=scopes,
            expiry=self._token_expiry(token_data),
        )

    def _load_stored_credentials(self) -> Any | None:  # noqa: ANN401
        """Load stored credentials for the current execution scope."""
        token_data = self._load_token_data()
        if not token_data:
            return None
        if not oauth_credentials_have_required_scopes(self._oauth_provider, token_data):
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
            creds = self._credentials_from_token_data(token_data)
        except Exception:
            self._oauth_logger.exception("oauth_credentials_load_failed", tool_name=self._oauth_tool_name)
            return None
        self._oauth_logger.info("oauth_credentials_loaded", tool_name=self._oauth_tool_name)
        return creds

    def _should_fallback_to_original_auth(self) -> bool:
        """Return whether the tool should defer to its original auth flow."""
        return self._defer_to_original_auth

    def _should_skip_auth(self) -> bool:
        """Return whether tool auth can return early with already-valid provided credentials."""
        return bool(self._provided_creds and self.creds and self.creds.valid)

    def _auth_with_original_fallback(self) -> None:
        """Authenticate through the wrapped tool's original auth flow."""
        if self._original_auth_completed and self.creds and self.creds.valid:
            return
        self.creds = None
        self._original_auth()
        self._original_auth_completed = True

    def _auth(self) -> None:
        """Authenticate using MindRoom-scoped OAuth credentials."""
        if self._should_skip_auth():
            return

        if self._should_fallback_to_original_auth():
            self._auth_with_original_fallback()
            return

        if self.creds and self.creds.valid:
            return

        token_data = self._load_token_data()
        if (
            not token_data
            or not oauth_credentials_have_required_scopes(self._oauth_provider, token_data)
            or not oauth_credentials_satisfy_identity_policy(self._oauth_provider, self._runtime_paths, token_data)
        ):
            raise self._connection_required()

        try:
            ensure_tool_deps(_GOOGLE_OAUTH_DEPS, self._oauth_tool_name, self._runtime_paths)

            self.creds = self._credentials_from_token_data(token_data)
            if self.creds.expired and self.creds.refresh_token:
                self.creds.refresh(google_requests.Request())
                refreshed = dict(token_data)
                refreshed["token"] = self.creds.token
                refreshed_expires_at = self._expires_at_from_credentials(self.creds)
                if refreshed_expires_at is not None:
                    refreshed["expires_at"] = refreshed_expires_at
                self._save_token_data(refreshed)
            if not self.creds.valid:
                self._raise_connection_required()
            self._oauth_logger.info("oauth_authentication_succeeded", tool_name=self._oauth_tool_name)
        except OAuthConnectionRequired:
            raise
        except Exception as exc:
            self._oauth_logger.warning(
                "oauth_authentication_failed",
                tool_name=self._oauth_tool_name,
                error_type=type(exc).__name__,
            )
            raise self._connection_required() from exc
