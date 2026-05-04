"""Shared helpers for Google API-backed tools."""

from __future__ import annotations

import threading
from typing import Any


class ThreadLocalGoogleServiceMixin:
    """Cache googleapiclient service objects per worker thread."""

    def _google_service_state(self) -> threading.local:
        state = self.__dict__.get("_google_service_thread_state")
        if state is None:
            state = threading.local()
            self.__dict__["_google_service_thread_state"] = state
        return state

    @property
    def service(self) -> Any | None:  # noqa: ANN401
        """Return the Google API service cached for the current worker thread."""
        return getattr(self._google_service_state(), "service", None)

    @service.setter
    def service(self, value: Any | None) -> None:  # noqa: ANN401
        self._google_service_state().service = value
