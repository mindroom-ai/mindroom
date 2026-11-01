"""Router-sync lifecycle for the shared reply-membership index and what each grant change asks of every bot."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Protocol

import nio

from mindroom.event_journal import (
    DepartureSource,
    EventKind,
    IngestionRecordAdmission,
    IngestionRecordDisposition,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

_REFRESH_BACKOFF_INITIAL_SECONDS = 5.0
_REFRESH_BACKOFF_MAX_SECONDS = 300.0


class _ReplyGrantEffects(Protocol):
    """What a change of room-backed reply grants asks of one bot."""

    def schedule_pending_invite_reconciliation(self) -> None:
        """Recheck cached invites in the background."""
        ...

    async def revoke_reply_authorized_calls(self) -> None:
        """End active calls whose requester no longer has reply access."""
        ...

    def schedule_reply_authorized_call_reconciliation(self) -> None:
        """Recheck active calls and start the ones that became allowed, in the background."""
        ...

    def schedule_reply_authorized_call_revocation(self) -> None:
        """End denied active calls in the background."""
        ...


class AgentReplyMembershipSync:
    """Own router receive-generation and sync-batch membership state, and apply every grant change to the fleet."""

    def __init__(
        self,
        memberships: AgentReplyMembershipIndex,
        bots: Callable[[], Iterable[_ReplyGrantEffects]],
    ) -> None:
        self._memberships = memberships
        self._bots = bots
        self._refresh_attempt = 0
        self._refresh_retry_at = 0.0
        self._live_transition_lock = asyncio.Lock()
        self._live_effects_pending = False

    def invalidate(self, config: Config, *, reason: str) -> None:
        """Fail every room grant closed, request a refresh, and end the calls that lost reply access."""
        self._memberships.invalidate(config, reason=reason)
        self._grants_lost()

    def pre_admit_ingestion(
        self,
        config: Config,
        runtime_paths: RuntimePaths,
        admission: IngestionRecordAdmission,
        timeline_provenance: nio.TimelineEventProvenance | None,
    ) -> None:
        """Fence uncertainty and reported control departures before admission, revoking the calls they deny.

        A router join instead lets the next due check rebuild without an earlier failure's backoff.
        """
        if admission.disposition is IngestionRecordDisposition.HISTORY_LOSS:
            self.invalidate(config, reason="uncertain_sync_response")
            return
        if (
            timeline_provenance is nio.TimelineEventProvenance.RECOVERED
            and admission.event is not None
            and admission.event.kind is EventKind.ROOM_LIFECYCLE
        ):
            room_id = admission.event.room_id
            reason = "recovered_membership"
        elif (
            admission.membership is not None
            and admission.source is DepartureSource.REPORTED
            and admission.previous_membership == "join"
            and admission.membership != "join"
            and admission.room_id is not None
        ):
            room_id = admission.room_id
            reason = "control_client_departed"
        else:
            if admission.membership == "join" and admission.previous_membership != "join":
                # The router's join is what can make a grant room it could not query queryable again.
                self._reset_refresh_backoff()
            return
        if self._memberships.mark_room_unready(config, runtime_paths, room_id, reason=reason):
            self._grants_lost()

    def _reset_refresh_backoff(self) -> None:
        """Let the next due check rebuild without waiting out an earlier failure's backoff."""
        self._refresh_attempt = 0
        self._refresh_retry_at = 0.0

    def _grants_lost(self) -> None:
        """Rebuild at the next due check without an earlier backoff, and end every bot's denied calls without waiting.

        This never starts a call.
        """
        self._reset_refresh_backoff()
        for bot in self._bots():
            bot.schedule_reply_authorized_call_revocation()

    async def refresh_if_needed(
        self,
        config: Config,
        refresh: Callable[[], Awaitable[None]],
    ) -> None:
        """Refresh once when due and apply bounded retry backoff on failure."""
        # A sync gap cannot invalidate explicit-user grants. Do not turn a
        # no-op rebuild into downstream room and call reconciliation scans.
        if not self._memberships.needs_refresh(config):
            return
        if time.monotonic() < self._refresh_retry_at:
            return
        await refresh()
        if not self._memberships.needs_refresh(config):
            self._reset_refresh_backoff()
            return
        self._refresh_attempt += 1
        backoff_seconds = min(
            _REFRESH_BACKOFF_INITIAL_SECONDS * (2 ** (self._refresh_attempt - 1)),
            _REFRESH_BACKOFF_MAX_SECONDS,
        )
        self._refresh_retry_at = time.monotonic() + backoff_seconds

    async def apply_live_transition(
        self,
        config: Config,
        room_id: str,
        event: nio.RoomMemberEvent,
        *,
        control_user_id: str,
    ) -> None:
        """Apply one durable LIVE transition and run its effects.

        Effects that a failed or cancelled run left pending run again with the next transition, such as this event's
        replay.
        """
        async with self._live_transition_lock:
            changed = self._memberships.apply_member_event(
                config,
                room_id,
                event,
                control_user_id=control_user_id,
            )
            if changed:
                self._live_effects_pending = True
            if not self._live_effects_pending:
                return
            await self.apply_grant_change()
            self._live_effects_pending = False

    async def apply_grant_change(self) -> None:
        """Recheck every bot's invites and calls after a grant change: denied calls end before any allowed call starts.

        An authoritative refresh attempt runs this whether it published or was voided. Calls start even while a grant
        room still awaits a refresh, because each call start rechecks its requester against the current grants.
        Invite acceptance does not depend on grants; the invite recheck rides here because the authoritative refresh
        that room setup runs at startup and on reload is what retries invites recorded before a bot started or whose
        join failed.
        """
        bots = tuple(self._bots())
        for bot in bots:
            bot.schedule_pending_invite_reconciliation()
        await asyncio.gather(*(bot.revoke_reply_authorized_calls() for bot in bots))
        for bot in bots:
            bot.schedule_reply_authorized_call_reconciliation()
