"""Fence historical Matrix callbacks using nio's per-event provenance."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Protocol

import nio

from mindroom.dispatch_admission import DispatchSourceAdmission
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


type _DecryptNoticeFence = Callable[[str], bool]

_EVENT_PROVENANCE: ContextVar[tuple[str, nio.TimelineEventProvenance] | None] = ContextVar(
    "mindroom_timeline_event_provenance",
    default=None,
)


def _decrypt_not_fenced(_room_id: str) -> bool:
    return False


@dataclass(slots=True)
class ColdHistoryFence:
    """Admit live events and exact durable retries of historical events."""

    obligations: _PendingDispatchObligations
    decrypt_notice_is_fenced: _DecryptNoticeFence = _decrypt_not_fenced

    def observe_event_provenance(
        self,
        source_event_id: str,
        provenance: nio.TimelineEventProvenance,
    ) -> None:
        """Expose one nio delivery's provenance to later callback fanout."""
        _EVENT_PROVENANCE.set((source_event_id, provenance))

    def event_is_live(self, source_event_id: str) -> bool:
        """Return whether the current nio fanout belongs to this live event."""
        return _EVENT_PROVENANCE.get() == (
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
