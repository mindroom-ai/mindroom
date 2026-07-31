"""Fence Matrix callbacks until the server establishes sync continuity."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from mindroom.matrix.sync_token_values import normalize_sync_token

if TYPE_CHECKING:
    from mindroom.dispatch_obligations import DispatchCallbackKind


class _PendingDispatchObligations(Protocol):
    """Exact durable read used to admit failed work while continuity is absent."""

    def has_pending(
        self,
        source_event_id: str,
        callback_kind: DispatchCallbackKind,
    ) -> bool:
        """Return whether one exact callback remains durably pending."""
        ...


@dataclass(slots=True)
class ColdHistoryFence:
    """Admit only exact durable retries during a continuity-less sync window."""

    obligations: _PendingDispatchObligations
    _has_trusted_continuation: bool = False

    @property
    def is_cold(self) -> bool:
        """Return whether arbitrary Matrix callbacks remain fenced."""
        return not self._has_trusted_continuation

    def start(self, *, trusted_continuation: object) -> None:
        """Set startup admission from a transport-compatible continuation."""
        self._has_trusted_continuation = normalize_sync_token(trusted_continuation) is not None

    def observe_continuation(self, continuation: object) -> None:
        """Set ordinary admission from the continuation in one Matrix response."""
        self._has_trusted_continuation = normalize_sync_token(continuation) is not None

    def reset(self) -> None:
        """Rearm exact-only admission after transport continuity is rejected."""
        self._has_trusted_continuation = False

    async def admit(
        self,
        source_event_id: str,
        callback_kind: DispatchCallbackKind,
    ) -> bool:
        """Return whether one source callback may enter durable dispatch."""
        if self._has_trusted_continuation:
            return True
        return await asyncio.to_thread(
            self.obligations.has_pending,
            source_event_id,
            callback_kind,
        )
