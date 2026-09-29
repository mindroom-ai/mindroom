"""Shared helpers for Google API-backed tools."""

from __future__ import annotations

import json
import threading
from typing import TYPE_CHECKING, Any, cast

from agno.tools import Toolkit
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from googleapiclient.http import build_http

from mindroom.logging_config import get_logger
from mindroom.oauth.client import ScopedOAuthClientMixin

if TYPE_CHECKING:
    from collections.abc import Callable

    from googleapiclient.errors import HttpError

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

logger = get_logger(__name__)

_SANITIZED_GOOGLE_AUTHORIZATION_REJECTION = b'{"error":{"code":401,"message":"Google authorization rejected"}}'


class _GoogleServiceThreadState(threading.local):
    def __init__(self) -> None:
        self.creds: Any | None = None
        self.service: Any | None = None
        self.credential_key: object | None = None
        self.label_cache: dict[str, str] | None = None
        self.user_email: str | None = None
        self.authorization_rejected = False


def _clear_google_account_state(state: _GoogleServiceThreadState) -> None:
    """Invalidate service and account-derived caches after an identity change."""
    state.service = None
    state.label_cache = None
    state.user_email = None


class _TrackedGoogleAuthorizedHttp(AuthorizedHttp):
    """Latch only a final HTTP 401 after AuthorizedHttp finishes its retries."""

    def __init__(self, credentials: Any, state: _GoogleServiceThreadState) -> None:  # noqa: ANN401
        super().__init__(credentials, http=build_http())
        self._mindroom_state = state

    def request(self, *args: Any, **kwargs: Any) -> tuple[Any, Any]:  # noqa: ANN401
        response, content = super().request(*args, **kwargs)
        if response.status == 401:
            self._mindroom_state.authorization_rejected = True
            content = _SANITIZED_GOOGLE_AUTHORIZATION_REJECTION
        return response, content


def google_http_error_result(service_name: str, operation: str, exc: HttpError) -> str:
    """Return a tool error exposing only the HTTP status, never provider-controlled text."""
    status = exc.resp.status
    logger.warning(
        "google_api_request_failed",
        service=service_name,
        operation=operation,
        error_type=type(exc).__name__,
        status=status,
    )
    message = f"{service_name} request failed"
    if not isinstance(status, bool) and isinstance(status, int):
        message = f"{message} (HTTP {status})"
    return json.dumps({"error": message})


class ThreadLocalGoogleServiceMixin:
    """Own Google credentials and service objects in one worker thread."""

    def _google_service_state(self) -> _GoogleServiceThreadState:
        state = self.__dict__.setdefault("_google_service_thread_state", _GoogleServiceThreadState())
        return cast("_GoogleServiceThreadState", state)

    @property
    def creds(self) -> Any | None:  # noqa: ANN401
        """Return credentials owned by the current worker thread."""
        return self._google_service_state().creds

    @creds.setter
    def creds(self, value: Any | None) -> None:  # noqa: ANN401
        state = self._google_service_state()
        if state.creds is not value:
            _clear_google_account_state(state)
        state.creds = value

    @property
    def service(self) -> Any | None:  # noqa: ANN401
        """Return the Google API service cached for the current worker thread."""
        return self._google_service_state().service

    @service.setter
    def service(self, value: Any | None) -> None:  # noqa: ANN401
        self._google_service_state().service = value

    @property
    def _service(self) -> Any | None:  # noqa: ANN401
        """Expose the same per-thread service under the name Agno's auth decorator reads."""
        return self._google_service_state().service

    @_service.setter
    def _service(self, value: Any | None) -> None:  # noqa: ANN401
        self._google_service_state().service = value

    @property
    def _google_credential_key(self) -> object | None:
        """Return canonical scope and revision backing this thread's credentials."""
        return self._google_service_state().credential_key

    @_google_credential_key.setter
    def _google_credential_key(self, value: object | None) -> None:
        state = self._google_service_state()
        if state.credential_key != value:
            _clear_google_account_state(state)
        state.credential_key = value

    def _adopt_google_credential_revision(self, value: object) -> None:
        """Advance one same-account revision without invalidating an active service call."""
        self._google_service_state().credential_key = value

    def _google_authorized_http(self, credentials: Any) -> AuthorizedHttp:  # noqa: ANN401
        """Build an HTTP client that records final managed OAuth rejection."""
        return _TrackedGoogleAuthorizedHttp(credentials, self._google_service_state())

    def _reset_google_authorization_rejected(self) -> None:
        self._google_service_state().authorization_rejected = False

    def _mark_google_authorization_rejected(self) -> None:
        self._google_service_state().authorization_rejected = True

    def _consume_google_authorization_rejected(self) -> bool:
        state = self._google_service_state()
        rejected = state.authorization_rejected
        state.authorization_rejected = False
        return rejected

    @property
    def _label_cache(self) -> dict[str, str] | None:
        """Return Gmail label identities owned by the current account thread."""
        return self._google_service_state().label_cache

    @_label_cache.setter
    def _label_cache(self, value: dict[str, str] | None) -> None:
        self._google_service_state().label_cache = value

    @property
    def _user_email(self) -> str | None:
        """Return the Calendar principal owned by the current account thread."""
        return self._google_service_state().user_email

    @_user_email.setter
    def _user_email(self, value: str | None) -> None:
        self._google_service_state().user_email = value


class GoogleApiToolkit(ScopedOAuthClientMixin, ThreadLocalGoogleServiceMixin, Toolkit):
    """Native Google API toolkit with scoped OAuth credentials and optional service-account fallback."""

    _google_api_name: str
    _google_api_version: str

    def __init__(
        self,
        *,
        name: str,
        tools: list[Callable[..., str]],
        runtime_paths: RuntimePaths,
        credentials_manager: CredentialsManager | None,
        worker_target: ResolvedWorkerTarget | None,
        runtime_config: Config | None,
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        provided_creds = kwargs.pop("creds", None)
        if credentials_manager is None:
            msg = f"{type(self).__name__} requires an explicit credentials_manager"
            raise RuntimeError(msg)

        self._runtime_paths = runtime_paths
        self._creds_manager = credentials_manager
        defer_to_original_auth = self._apply_runtime_original_auth_kwargs(kwargs)
        self.service_account_path = cast("str | None", kwargs.pop("service_account_path", None))
        self.delegated_user = cast("str | None", kwargs.pop("delegated_user", None))
        if kwargs:
            unexpected = ", ".join(sorted(kwargs))
            msg = f"{self._oauth_provider.display_name} received unsupported constructor arguments: {unexpected}"
            raise TypeError(msg)

        self.creds = self._initialize_oauth_client(
            worker_target=worker_target,
            config=runtime_config,
            provided_creds=provided_creds,
            logger=logger,
            defer_to_original_auth=defer_to_original_auth,
        )
        super().__init__(name=name, tools=tools)
        self._set_original_auth(GoogleApiToolkit._service_account_auth)
        self._wrap_oauth_function_entrypoints()

    def _should_fallback_to_original_auth(self) -> bool:
        return bool(self.service_account_path or self._runtime_paths.env_value("GOOGLE_SERVICE_ACCOUNT_FILE"))

    def _service_account_auth(self) -> Any:  # noqa: ANN401
        """Return Google credentials built from the configured service-account file."""
        from google.oauth2 import service_account  # noqa: PLC0415

        if not self.service_account_path:
            msg = (
                f"{self._oauth_provider.display_name} service-account authentication "
                "requires GOOGLE_SERVICE_ACCOUNT_FILE"
            )
            raise RuntimeError(msg)
        creds = service_account.Credentials.from_service_account_file(
            self.service_account_path,
            scopes=self._oauth_provider.scopes,
        )
        if self.delegated_user:
            creds = creds.with_subject(self.delegated_user)
        return creds

    def _google_api_service(self) -> Any:  # noqa: ANN401
        """Return the per-thread authenticated Google API service."""
        self._authenticate()
        if self.service is None:
            self.service = build(
                self._google_api_name,
                self._google_api_version,
                http=self._google_authorized_http(self.creds),
                cache_discovery=False,
            )
        return self.service
