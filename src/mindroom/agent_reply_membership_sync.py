"""Router-sync lifecycle for the shared reply-membership index."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.event_journal import (
    DepartureSource,
    IngestionBatchAdmission,
    IngestionRecordDisposition,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import nio

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

_REFRESH_BACKOFF_INITIAL_SECONDS = 5.0
_REFRESH_BACKOFF_MAX_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class ReplyMembershipPreAdmission:
    """Effects that the sync boundary must publish before timeline admission."""

    invalidate_reason: str | None = None
    authorization_changed: bool = False


class AgentReplyMembershipSync:
    """Own router receive-generation and sync-batch membership state."""

    def __init__(self, memberships: AgentReplyMembershipIndex) -> None:
        self._memberships = memberships
        self._refresh_pending = False
        self._refresh_attempt = 0
        self._refresh_retry_at = 0.0
        self._preserve_on_next_sync_start = False
        self._live_transition_lock = asyncio.Lock()
        self._live_effects_pending = False
        self._revocation_wave_issued = False

    @property
    def memberships(self) -> AgentReplyMembershipIndex:
        """Return the shared atomic index controlled by this sync lifecycle."""
        return self._memberships

    def preserve_on_next_sync_start(self) -> None:
        """Carry a pre-sync authoritative snapshot into exactly one receive loop."""
        self._preserve_on_next_sync_start = True

    def sync_loop_started(self) -> bool:
        """Return whether a new receive generation must invalidate its snapshot."""
        preserve = self._preserve_on_next_sync_start
        self._preserve_on_next_sync_start = False
        if preserve:
            self._request_refresh()
        return not preserve

    def reset_receive_generation(self) -> None:
        """Forget one stopped receive generation's preservation state."""
        self._preserve_on_next_sync_start = False

    def _request_refresh(self) -> None:
        """Request an authoritative rebuild."""
        if self._refresh_pending:
            return
        self._refresh_pending = True
        self._refresh_attempt = 0
        self._refresh_retry_at = 0.0

    def invalidate(self, config: Config, *, reason: str) -> bool:
        """Fail every room grant closed and request one revocation wave per gap."""
        self._memberships.invalidate(config, reason=reason)
        self._request_refresh()
        if self._revocation_wave_issued:
            return False
        self._revocation_wave_issued = True
        return True

    def record_authoritative_refresh(self, config: Config) -> None:
        """Allow a future invalidation to issue a new revocation wave."""
        if not self._memberships.needs_refresh(config):
            self._revocation_wave_issued = False

    async def refresh_if_needed(
        self,
        config: Config,
        refresh: Callable[[], Awaitable[None]],
    ) -> None:
        """Refresh once when due and apply bounded retry backoff on failure."""
        if not self._refresh_pending and not self._memberships.needs_refresh(config):
            return
        if time.monotonic() < self._refresh_retry_at:
            return
        await refresh()
        self._refresh_pending = self._memberships.needs_refresh(config)
        if not self._refresh_pending:
            self.record_authoritative_refresh(config)
            self._refresh_attempt = 0
            self._refresh_retry_at = 0.0
            return
        self._refresh_attempt += 1
        backoff_seconds = min(
            _REFRESH_BACKOFF_INITIAL_SECONDS * (2 ** (self._refresh_attempt - 1)),
            _REFRESH_BACKOFF_MAX_SECONDS,
        )
        self._refresh_retry_at = time.monotonic() + backoff_seconds

    def pre_admit_ingestion(
        self,
        config: Config,
        runtime_paths: RuntimePaths,
        admission: IngestionBatchAdmission,
    ) -> ReplyMembershipPreAdmission:
        """Fence uncertainty and reported control departures before admission."""
        if admission.disposition is IngestionRecordDisposition.HISTORY_LOSS:
            return ReplyMembershipPreAdmission(invalidate_reason="uncertain_sync_response")
        if not (
            admission.disposition is IngestionRecordDisposition.ROOM_LIFECYCLE
            and admission.source is DepartureSource.REPORTED
            and admission.previous_membership == "join"
            and admission.membership != "join"
            and admission.room_id is not None
        ):
            return ReplyMembershipPreAdmission()
        authorization_changed = self._memberships.mark_control_room_unready(
            config,
            runtime_paths,
            admission.room_id,
            reason="control_client_departed",
        )
        if authorization_changed:
            self._request_refresh()
        return ReplyMembershipPreAdmission(authorization_changed=authorization_changed)

    async def apply_live_transition(
        self,
        config: Config,
        runtime_paths: RuntimePaths,
        room_id: str,
        event: nio.RoomMemberEvent,
        *,
        control_user_id: str,
        reconcile_effects: Callable[[], Awaitable[None]],
    ) -> None:
        """Apply one durable LIVE transition and finish its dependent effects."""
        async with self._live_transition_lock:
            changed = self._memberships.apply_member_event(
                config,
                runtime_paths,
                room_id,
                event,
                control_user_id=control_user_id,
            )
            if changed:
                self._live_effects_pending = True
            if not self._live_effects_pending:
                return
            await reconcile_effects()
            self._live_effects_pending = False
