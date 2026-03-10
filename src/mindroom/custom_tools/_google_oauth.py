"""Shared Google OAuth helpers for custom Google-backed tools."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mindroom.credentials import load_scoped_credentials, save_scoped_credentials
from mindroom.tool_system.dependencies import ensure_tool_deps
from mindroom.tool_system.worker_routing import (
    unsupported_shared_only_integration_message,
    worker_scope_allows_shared_only_integrations,
)

if TYPE_CHECKING:
    from loguru import Logger

    from mindroom.credentials import CredentialsManager
    from mindroom.tool_system.worker_routing import WorkerScope

GOOGLE_OAUTH_DEPS = ["google-auth", "google-auth-oauthlib"]


class ScopedGoogleOAuthMixin:
    """Shared scoped credential loading and refresh logic for Google-backed tools."""

    _oauth_tool_name: str
    _oauth_log_name: str
    DEFAULT_SCOPES: list[str] | dict[str, str]
    _oauth_logger: Logger
    _creds_manager: CredentialsManager
    _worker_scope: WorkerScope | None
    _routing_agent_name: str | None
    _provided_creds: bool

    def _validate_google_oauth_contract(self) -> None:
        """Fail fast when a subclass does not provide the required OAuth metadata."""
        missing = [
            name
            for name in ("_oauth_tool_name", "_oauth_log_name", "DEFAULT_SCOPES", "_creds_manager")
            if not hasattr(self, name)
        ]
        if missing:
            msg = f"{type(self).__name__} is missing required Google OAuth attributes: {', '.join(missing)}"
            raise TypeError(msg)

    def _initialize_google_oauth(
        self,
        *,
        worker_scope: WorkerScope | None,
        routing_agent_name: str | None,
        provided_creds: Any,  # noqa: ANN401
        logger: Logger,
    ) -> Any:  # noqa: ANN401
        """Validate scope and prepare initial Google credentials for the tool."""
        self._validate_google_oauth_contract()
        if not worker_scope_allows_shared_only_integrations(worker_scope):
            msg = unsupported_shared_only_integration_message(
                self._oauth_tool_name,
                worker_scope,
                agent_name=routing_agent_name,
                subject="Tool",
            )
            raise ValueError(msg)

        self._worker_scope = worker_scope
        self._routing_agent_name = routing_agent_name
        self._provided_creds = provided_creds is not None
        self._oauth_logger = logger
        return provided_creds or self._load_stored_credentials()

    def _load_token_data(self) -> dict[str, Any] | None:
        """Load Google OAuth credentials for the current execution scope."""
        return load_scoped_credentials(
            "google",
            worker_scope=self._worker_scope,
            routing_agent_name=self._routing_agent_name,
            credentials_manager=self._creds_manager,
        )

    def _save_token_data(self, token_data: dict[str, Any]) -> None:
        """Persist refreshed Google OAuth credentials to the current execution scope."""
        save_scoped_credentials(
            "google",
            token_data,
            worker_scope=self._worker_scope,
            routing_agent_name=self._routing_agent_name,
            credentials_manager=self._creds_manager,
        )

    def _build_credentials(self, token_data: dict[str, Any]) -> Any:  # noqa: ANN401
        """Create a Google Credentials object from stored token data."""
        ensure_tool_deps(GOOGLE_OAUTH_DEPS, self._oauth_tool_name)
        from google.oauth2.credentials import Credentials  # noqa: PLC0415

        scopes = token_data.get("scopes")
        if not isinstance(scopes, list):
            default_scopes = self.DEFAULT_SCOPES
            scopes = list(default_scopes.values()) if isinstance(default_scopes, dict) else default_scopes

        return Credentials(
            token=token_data.get("token"),
            refresh_token=token_data.get("refresh_token"),
            token_uri=token_data.get("token_uri"),
            client_id=token_data.get("client_id"),
            client_secret=token_data.get("client_secret"),
            scopes=scopes,
        )

    def _load_stored_credentials(self) -> Any:  # noqa: ANN401
        """Load stored credentials for the current execution scope."""
        token_data = self._load_token_data()
        if not token_data:
            return None

        try:
            creds = self._build_credentials(token_data)
        except Exception:
            self._oauth_logger.exception(f"Failed to load {self._oauth_log_name} credentials")
            return None

        self._oauth_logger.info(f"Loaded {self._oauth_log_name} credentials from MindRoom storage")
        return creds

    def _should_fallback_to_original_auth(self) -> bool:
        """Return whether the tool should defer to its original OAuth flow."""
        return False

    def _set_original_auth(self, auth_method: Any) -> None:  # noqa: ANN401
        """Store the parent auth callable, binding descriptors when needed."""
        binder = getattr(auth_method, "__get__", None)
        self._original_auth = binder(self, type(self)) if callable(binder) else auth_method

    def _should_skip_auth(self) -> bool:
        """Return whether tool auth can return early with already-valid provided credentials."""
        return bool(self._provided_creds and self.creds and self.creds.valid)

    def _auth(self) -> None:
        """Authenticate using MindRoom-scoped Google credentials."""
        if self._should_skip_auth():
            return

        if self._should_fallback_to_original_auth():
            self.creds = None
            self._original_auth()
            return

        token_data = self._load_token_data()
        if token_data:
            try:
                ensure_tool_deps(GOOGLE_OAUTH_DEPS, self._oauth_tool_name)
                from google.auth.transport.requests import Request  # noqa: PLC0415

                self.creds = self._build_credentials(token_data)

                if self.creds.expired and self.creds.refresh_token:
                    self.creds.refresh(Request())
                    token_data["token"] = self.creds.token
                    self._save_token_data(token_data)

                self._oauth_logger.info(f"{self._oauth_log_name} authentication successful")
            except Exception:
                self._oauth_logger.exception(f"Failed to authenticate with {self._oauth_log_name}")
                raise
            return

        self.creds = None
        self._oauth_logger.warning("No stored credentials found, initiating OAuth flow")
        self._original_auth()
