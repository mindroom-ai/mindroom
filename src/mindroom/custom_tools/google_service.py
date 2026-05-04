"""Shared helpers for Google API-backed tools."""

from __future__ import annotations

import threading
from typing import Any, cast


class _GoogleServiceThreadState(threading.local):
    def __init__(self) -> None:
        self.service: Any | None = None


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
