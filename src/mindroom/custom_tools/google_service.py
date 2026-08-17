"""Shared helpers for Google API-backed tools."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any, cast

from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.http import build_http

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths

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


def google_service_account_configured(service_account_path: str | None, runtime_paths: RuntimePaths) -> bool:
    """Return whether Google upstream service-account auth is configured."""
    return bool(service_account_path or runtime_paths.env_value("GOOGLE_SERVICE_ACCOUNT_FILE"))


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
