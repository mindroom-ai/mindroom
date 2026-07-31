"""Fence Matrix callbacks until the server establishes sync continuity."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from mindroom.dispatch_admission import DispatchCallbackKind, DispatchSourceAdmission
from mindroom.matrix.sync_token_values import normalize_sync_token


class _PendingDispatchObligations(Protocol):
    """Exact durable read used to admit failed work while continuity is absent."""

    def has_pending(
        self,
        source_event_id: str,
        callback_kind: DispatchCallbackKind,
    ) -> bool:
        """Return whether one exact callback remains durably pending."""
        ...


class _DecryptNoticeFence(Protocol):
    """Room-scoped join fence queried during callback admission."""

    def __call__(self, room_id: str, /) -> bool:
        """Return whether decrypt notices remain fenced for one room."""
        ...


def _decrypt_not_fenced(_room_id: str) -> bool:
    return False


@dataclass(slots=True)
class ColdHistoryFence:
    """Admit only exact durable retries during a continuity-less sync window."""

    obligations: _PendingDispatchObligations
    decrypt_notice_is_fenced: _DecryptNoticeFence = _decrypt_not_fenced
    _has_trusted_continuation: bool = False

    @property
    def is_cold(self) -> bool:
        """Return whether arbitrary Matrix callbacks remain fenced."""
        return not self._has_trusted_continuation

    def observe_continuation(self, continuation: object) -> None:
        """Set admission from one transport-compatible continuation."""
        self._has_trusted_continuation = normalize_sync_token(continuation) is not None

    def observe_recovery(
        self,
        *,
        continuation: object,
        unrecovered_room_ids: frozenset[str],
    ) -> bool:
        """Apply one transport recovery outcome and report whether it completed."""
        if unrecovered_room_ids:
            self.reset()
            return False
        self.observe_continuation(continuation)
        return True

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

    async def admit_source(
        self,
        room_id: str,
        source_event_id: str,
        callback_kind: DispatchCallbackKind,
    ) -> DispatchSourceAdmission:
        """Apply invite, decrypt-notice, and cold-history admission policy."""
        if callback_kind is DispatchCallbackKind.INVITE:
            return DispatchSourceAdmission.ACCEPTED
        if callback_kind is DispatchCallbackKind.DECRYPTION_FAILURE and self.decrypt_notice_is_fenced(room_id):
            return DispatchSourceAdmission.DECRYPT_NOTICE_FENCED
        if await self.admit(source_event_id, callback_kind):
            return DispatchSourceAdmission.ACCEPTED
        return DispatchSourceAdmission.COLD_HISTORY_FENCED
