"""Fence historical Matrix callbacks using nio's per-event provenance."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Protocol

import nio

from mindroom.dispatch_admission import DispatchCallbackKind, DispatchSourceAdmission


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


def _event_provenance_context() -> ContextVar[tuple[str, nio.TimelineEventProvenance] | None]:
    return ContextVar("mindroom_timeline_event_provenance", default=None)


@dataclass(slots=True)
class ColdHistoryFence:
    """Admit live events and exact durable retries of historical events."""

    obligations: _PendingDispatchObligations
    decrypt_notice_is_fenced: _DecryptNoticeFence = _decrypt_not_fenced
    _event_provenance: ContextVar[tuple[str, nio.TimelineEventProvenance] | None] = field(
        default_factory=_event_provenance_context,
        init=False,
        repr=False,
    )

    def observe_event_provenance(
        self,
        source_event_id: str,
        provenance: nio.TimelineEventProvenance,
    ) -> None:
        """Expose one nio delivery's provenance to later callback fanout."""
        self._event_provenance.set((source_event_id, provenance))

    def event_is_live(self, source_event_id: str) -> bool:
        """Return whether the current nio fanout belongs to this live event."""
        return self._event_provenance.get() == (
            source_event_id,
            nio.TimelineEventProvenance.LIVE,
        )

    async def admit_source(
        self,
        room_id: str,
        source_event_id: str,
        callback_kind: DispatchCallbackKind,
        provenance: nio.TimelineEventProvenance | None = None,
    ) -> DispatchSourceAdmission:
        """Apply invite, decrypt-notice, and event-provenance policy."""
        if callback_kind is DispatchCallbackKind.INVITE:
            return DispatchSourceAdmission.ACCEPTED
        if callback_kind is DispatchCallbackKind.DECRYPTION_FAILURE and self.decrypt_notice_is_fenced(room_id):
            return DispatchSourceAdmission.DECRYPT_NOTICE_FENCED
        if provenance is not nio.TimelineEventProvenance.HISTORY:
            return DispatchSourceAdmission.ACCEPTED
        if await asyncio.to_thread(
            self.obligations.has_pending,
            source_event_id,
            callback_kind,
        ):
            return DispatchSourceAdmission.ACCEPTED
        return DispatchSourceAdmission.COLD_HISTORY_FENCED
