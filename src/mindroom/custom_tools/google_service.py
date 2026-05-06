"""Shared helpers for Google API-backed tools."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths


class _GoogleServiceThreadState(threading.local):
    def __init__(self) -> None:
        self.service: Any | None = None


def google_service_account_configured(service_account_path: str | None, runtime_paths: RuntimePaths) -> bool:
    """Return whether Google upstream service-account auth is configured."""
    return bool(service_account_path or runtime_paths.env_value("GOOGLE_SERVICE_ACCOUNT_FILE"))


class ThreadLocalGoogleServiceMixin:
    """Cache googleapiclient service objects per worker thread."""

    def _google_service_state(self) -> _GoogleServiceThreadState:
        state = self.__dict__.setdefault("_google_service_thread_state", _GoogleServiceThreadState())
        return cast("_GoogleServiceThreadState", state)

    @property
    def service(self) -> Any | None:  # noqa: ANN401
        """Return the Google API service cached for the current worker thread."""
        return self._google_service_state().service

    @service.setter
    def service(self, value: Any | None) -> None:  # noqa: ANN401
        self._google_service_state().service = value
