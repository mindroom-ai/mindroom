"""Sync lifecycle of the shared reply-membership index, driven by every bot, and what each grant change asks of them."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Protocol

import nio

from mindroom.background_tasks import create_background_task, retry_delay_seconds
from mindroom.event_journal import (
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
    """Apply each bot's sync to the rooms its account observes, and every grant change to the fleet."""

    def __init__(
        self,
        memberships: AgentReplyMembershipIndex,
        bots: Callable[[], Iterable[_ReplyGrantEffects]],
    ) -> None:
        self._memberships = memberships
        self._bots = bots
        self._refresh_attempt = 0
        self._refresh_retry_at = 0.0
        self._refresh_task: asyncio.Task[None] | None = None
        # Each account's receive loop: absent while closed, False once armed by its start, True once open.
        self._observer_open: dict[str, bool] = {}
        self._live_transition_lock = asyncio.Lock()
        self._live_effects_pending = False

    def invalidate(self, config: Config, *, reason: str) -> None:
        """Fail every room grant closed, request a refresh, and end the calls that lost reply access."""
        self._memberships.invalidate(config, reason=reason)
        self._grants_lost()

    def invalidate_observer(self, config: Config, observer_user_id: str, *, reason: str) -> None:
        """Fail closed the rooms one account observes, request a refresh, and end the calls that lost reply access."""
        self._memberships.invalidate_observer(config, observer_user_id, reason=reason)
        self._grants_lost()

    def observer_loop_started(self, config: Config, observer_user_id: str) -> None:
        """Arm one account's receive loop, whose first settled frame opens it."""
        self.observer_closed(config, observer_user_id, reason="sync_loop_started")
        self._observer_open[observer_user_id] = False

    def observer_frame_settled(self, observer_user_id: str) -> None:
        """Open an armed account as an observer and ask for its rooms' read; a closed loop's late frame opens nothing."""
        if self._observer_open.get(observer_user_id) is False:
            self._observer_open[observer_user_id] = True
            self._grants_lost(revoke=False)

    def observer_closed(self, config: Config, observer_user_id: str, *, reason: str) -> None:
        """End one account's observation; only an account that was open loses its rooms' grants."""
        if self._observer_open.pop(observer_user_id, False):
            self.invalidate_observer(config, observer_user_id, reason=reason)

    def observer_is_open(self, observer_user_id: str) -> bool:
        """Return whether the account's receive loop has caught up and still runs."""
        return self._observer_open.get(observer_user_id, False)

    def pre_admit_ingestion(
        self,
        config: Config,
        runtime_paths: RuntimePaths,
        admission: IngestionRecordAdmission,
        timeline_provenance: nio.TimelineEventProvenance | None,
        *,
        observer_user_id: str,
    ) -> None:
        """Fence one account's sync uncertainty and its own departures before admission, revoking the calls they deny.

        A departure counts whether the account's sync reported it or its own leave command made it, whose record
        has no timeline event. The account's own join never grants: it asks for the room's members read, so a new
        DM's first message waits for it instead of being denied by a room list taken before the join.
        Lost history and a recovered event count only from an open account: no other one's sync vouched for a row.
        """
        observing = self.observer_is_open(observer_user_id)
        if admission.disposition is IngestionRecordDisposition.HISTORY_LOSS:
            if observing:
                self.invalidate_observer(config, observer_user_id, reason="uncertain_sync_response")
            return
        if (
            observing
            and timeline_provenance is nio.TimelineEventProvenance.RECOVERED
            and admission.event is not None
            and admission.event.kind is EventKind.ROOM_LIFECYCLE
        ):
            room_id = admission.event.room_id
            reason = "recovered_membership"
        elif (
            admission.membership is not None
            and admission.room_id is not None
            and (admission.previous_membership == "join") != (admission.membership == "join")
        ):
            room_id = admission.room_id
            reason = "observer_joined" if admission.membership == "join" else "control_client_departed"
        else:
            return
        if self._memberships.mark_room_unready(
            config,
            runtime_paths,
            room_id,
            observer_user_id=observer_user_id,
            reason=reason,
            joined=reason == "observer_joined",
        ):
            self._grants_lost(revoke=reason != "observer_joined")

    def _grants_lost(self, *, revoke: bool = True) -> None:
        """Rebuild at the next due check without an earlier backoff, and end every bot's denied calls without waiting.

        This never starts a call. An own join and an account that opened only ask for the rebuild: they revoke nothing.
        """
        self._refresh_attempt = 0
        self._refresh_retry_at = 0.0
        for bot in self._bots() if revoke else ():
            bot.schedule_reply_authorized_call_revocation()

    def request_refresh(self, config: Config, refresh: Callable[[], Awaitable[object]]) -> None:
        """Start the refresh when it is due and none is in flight, without waiting for it.

        Every bot's sync frame calls this and goes on: grants the refresh has not rebuilt yet stay pending.
        No bot owns the refresh, so a stopping one does not cancel it: its invalidation keeps it from publishing.
        """
        self._refresh_task_when_needed(config, refresh, after_backoff=True)

    async def refresh_if_needed(self, config: Config, refresh: Callable[[], Awaitable[object]]) -> None:
        """Request the refresh as above and wait for it at most 10 s, for the startup publication gate alone.

        A slower refresh keeps running in the background, and one that raises leaves the grants as they were.
        """
        task = self._refresh_task_when_needed(config, refresh, after_backoff=True)
        if task is not None:
            await asyncio.wait([task], timeout=10)

    async def close(self) -> None:
        """Cancel the refresh in flight and wait for its end; the runtime calls this once, at process shutdown."""
        task = self._refresh_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait([task])

    async def refresh_for_access_check(self, config: Config, refresh: Callable[[], Awaitable[object]]) -> None:
        """Refresh at once when grants need it, even during backoff, and wait until the refresh ends.

        Access checks outside a sync frame call this, so they refresh only then, share a refresh in flight with
        the bots' sync frames, and update its backoff. Waiting never cancels the refresh, and one that raises or is
        cancelled leaves the grants as they were.
        """
        task = self._refresh_task_when_needed(config, refresh, after_backoff=False)
        if task is not None:
            await asyncio.wait([task])

    def _refresh_task_when_needed(
        self,
        config: Config,
        refresh: Callable[[], Awaitable[object]],
        *,
        after_backoff: bool,
    ) -> asyncio.Task[None] | None:
        """When grants need it, return the refresh in flight, else start one if ``after_backoff`` allows."""
        # A sync gap cannot invalidate explicit-user grants. Do not turn a
        # no-op rebuild into downstream room and call reconciliation scans.
        if not self._memberships.needs_refresh(config):
            return None
        if self._refresh_task is None or self._refresh_task.done():
            if after_backoff and time.monotonic() < self._refresh_retry_at:
                return None
            self._refresh_task = create_background_task(
                self._refresh(config, refresh),
                name="agent_reply_membership_refresh",
            )
        return self._refresh_task

    async def _refresh(self, config: Config, refresh: Callable[[], Awaitable[object]]) -> None:
        """Refresh, once more at once when an invalidation voided the attempt; what is still unanswered backs off."""
        if await refresh() == "voided":
            await refresh()
        if not self._memberships.needs_refresh(config):
            self._refresh_attempt = 0
            self._refresh_retry_at = 0.0
            return
        self._refresh_attempt += 1
        self._refresh_retry_at = time.monotonic() + retry_delay_seconds(
            self._refresh_attempt,
            initial_delay_seconds=_REFRESH_BACKOFF_INITIAL_SECONDS,
            max_delay_seconds=_REFRESH_BACKOFF_MAX_SECONDS,
        )

    async def apply_live_transition(
        self,
        config: Config,
        room_id: str,
        event: nio.RoomMemberEvent,
        *,
        control_user_id: str,
    ) -> None:
        """Apply one durable LIVE transition from ``control_user_id``'s sync and run its effects.

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

    async def apply_grant_change(self, *, published: bool = True) -> None:
        """Recheck every bot's invites and calls after a grant change: denied calls end before any allowed call starts.

        A refresh that was voided changed no grant and passes ``published`` False, which rechecks the invites only.
        Calls start even while a grant
        room still awaits a refresh, because each call start rechecks its requester against the current grants.
        Invite acceptance does not depend on grants; the invite recheck rides here because the authoritative refresh
        that room setup runs at startup and on reload is what retries invites recorded before a bot started or whose
        join failed.
        """
        bots = tuple(self._bots())
        for bot in bots:
            bot.schedule_pending_invite_reconciliation()
        if not published:
            return
        await asyncio.gather(*(bot.revoke_reply_authorized_calls() for bot in bots))
        for bot in bots:
            bot.schedule_reply_authorized_call_reconciliation()
