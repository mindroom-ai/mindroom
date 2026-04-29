"""Google Drive tools backed by MindRoom-scoped OAuth credentials."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, NoReturn

from agno.tools.google.drive import GoogleDriveTools as AgnoGoogleDriveTools
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials

from mindroom.constants import RuntimePaths  # noqa: TC001
from mindroom.credentials import CredentialsManager, load_scoped_credentials, save_scoped_credentials
from mindroom.logging_config import get_logger
from mindroom.oauth.google_drive import google_drive_oauth_provider
from mindroom.oauth.providers import OAuthConnectionRequired
from mindroom.oauth.service import build_oauth_connect_instruction
from mindroom.tool_system.dependencies import ensure_tool_deps
from mindroom.tool_system.worker_routing import ResolvedWorkerTarget  # noqa: TC001

logger = get_logger(__name__)
_GOOGLE_OAUTH_DEPS = ["google-auth", "google-auth-oauthlib"]


class GoogleDriveTools(AgnoGoogleDriveTools):
    """Google Drive toolkit that reads OAuth tokens from MindRoom's credential scopes."""

    _oauth_provider = google_drive_oauth_provider()

    def __init__(
        self,
        *,
        runtime_paths: RuntimePaths,
        credentials_manager: CredentialsManager | None = None,
        worker_target: ResolvedWorkerTarget | None = None,
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        provided_creds = kwargs.pop("creds", None)
        if credentials_manager is None:
            msg = "GoogleDriveTools requires an explicit credentials_manager"
            raise RuntimeError(msg)
        if "max_read_size" in kwargs:
            kwargs["max_read_size"] = self._coerce_max_read_size(kwargs["max_read_size"])
        self._runtime_paths = runtime_paths
        self._creds_manager = credentials_manager
        self._worker_target = worker_target
        super().__init__(creds=provided_creds, **kwargs)

    def _coerce_max_read_size(self, value: object) -> int | float | None:
        if value is None:
            return None
        if isinstance(value, bool):
            msg = "Google Drive max_read_size must be a number"
            raise TypeError(msg)
        if isinstance(value, int | float):
            return value
        if isinstance(value, str):
            raw_value = value.strip()
            if not raw_value:
                return None
            try:
                parsed = float(raw_value)
            except ValueError as exc:
                msg = "Google Drive max_read_size must be a number"
                raise ValueError(msg) from exc
            return int(parsed) if parsed.is_integer() else parsed
        msg = "Google Drive max_read_size must be a number"
        raise TypeError(msg)

    def _load_token_data(self) -> dict[str, Any] | None:
        return load_scoped_credentials(
            self._oauth_provider.credential_service,
            credentials_manager=self._creds_manager,
            worker_target=self._worker_target,
        )

    def _save_token_data(self, token_data: dict[str, Any]) -> None:
        save_scoped_credentials(
            self._oauth_provider.credential_service,
            token_data,
            credentials_manager=self._creds_manager,
            worker_target=self._worker_target,
        )

    def _connection_required(self) -> OAuthConnectionRequired:
        return OAuthConnectionRequired(
            build_oauth_connect_instruction(
                self._oauth_provider,
                self._runtime_paths,
                worker_target=self._worker_target,
            ),
        )

    def _raise_connection_required(self) -> NoReturn:
        raise self._connection_required()

    def _expiry_from_token_data(self, token_data: dict[str, Any]) -> datetime | None:
        expires_at = token_data.get("expires_at")
        if not isinstance(expires_at, int | float) or expires_at <= 0:
            return None
        return datetime.fromtimestamp(float(expires_at), tz=UTC).replace(tzinfo=None)

    def _expires_at_from_expiry(self, credentials: Credentials) -> float | None:
        expiry = credentials.expiry
        if expiry is None:
            return None
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        return expiry.timestamp()

    def _credentials_from_token_data(self, token_data: dict[str, Any]) -> Credentials:
        ensure_tool_deps(_GOOGLE_OAUTH_DEPS, "google_drive", self._runtime_paths)

        client_config = self._oauth_provider.client_config(self._runtime_paths)
        if client_config is None:
            raise self._connection_required()
        return Credentials(
            token=token_data.get("token"),
            refresh_token=token_data.get("refresh_token"),
            token_uri=token_data.get("token_uri") or self._oauth_provider.token_url,
            client_id=token_data.get("client_id") or client_config.client_id,
            client_secret=client_config.client_secret,
            scopes=token_data.get("scopes") or list(self._oauth_provider.scopes),
            expiry=self._expiry_from_token_data(token_data),
        )

    def _auth(self) -> None:
        """Authenticate with scoped MindRoom credentials or return a connect instruction."""
        if self.creds and self.creds.valid:
            return

        if self.service_account_path:
            super()._auth()
            return

        token_data = self._load_token_data()
        if not token_data:
            raise self._connection_required()

        try:
            ensure_tool_deps(_GOOGLE_OAUTH_DEPS, "google_drive", self._runtime_paths)

            credentials = self._credentials_from_token_data(token_data)
            self.creds = credentials
            if credentials.expired and credentials.refresh_token:
                credentials.refresh(GoogleRequest())
                refreshed_token_data = dict(token_data)
                refreshed_token_data["token"] = credentials.token
                refreshed_expires_at = self._expires_at_from_expiry(credentials)
                if refreshed_expires_at is not None:
                    refreshed_token_data["expires_at"] = refreshed_expires_at
                self._save_token_data(refreshed_token_data)
            if not self.creds.valid:
                self._raise_connection_required()
        except OAuthConnectionRequired:
            raise
        except Exception as exc:
            logger.warning("google_drive_oauth_authentication_failed", error_type=type(exc).__name__)
            raise self._connection_required() from exc
