"""Router-sync lifecycle for the shared reply-membership index."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import nio

from mindroom.matrix.sync_loop import (
    OwnRoomMembership,
    own_membership_from_sliding_sync,
    own_membership_from_sync,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

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
        """Request an authoritative rebuild without resetting active backoff."""
        if self._refresh_pending:
            return
        self._refresh_pending = True
        self._refresh_attempt = 0
        self._refresh_retry_at = 0.0

    def invalidate(self, config: Config, *, reason: str) -> None:
        """Fail every room grant closed and request an authoritative rebuild."""
        self._memberships.invalidate(config, reason=reason)
        self._request_refresh()

    async def refresh_if_needed(
        self,
        config: Config,
        refresh: Callable[[], Awaitable[None]],
    ) -> None:
        """Refresh once when due and apply bounded retry backoff on failure."""
        if not self._refresh_pending and not self._memberships.needs_refresh(config.authorization):
            return
        if time.monotonic() < self._refresh_retry_at:
            return
        await refresh()
        self._refresh_pending = self._memberships.needs_refresh(config.authorization)
        if not self._refresh_pending:
            self._refresh_attempt = 0
            self._refresh_retry_at = 0.0
            return
        self._refresh_attempt += 1
        backoff_seconds = min(
            _REFRESH_BACKOFF_INITIAL_SECONDS * (2 ** (self._refresh_attempt - 1)),
            _REFRESH_BACKOFF_MAX_SECONDS,
        )
        self._refresh_retry_at = time.monotonic() + backoff_seconds

    def pre_admit_response(
        self,
        config: Config,
        runtime_paths: RuntimePaths,
        response: nio.SyncResponse | nio.SlidingSyncResponse,
        *,
        control_user_id: str,
    ) -> ReplyMembershipPreAdmission:
        """Fence uncertain state and control-client departures before nio fanout."""
        if _sync_response_has_uncertain_membership(response):
            return ReplyMembershipPreAdmission(invalidate_reason="uncertain_sync_response")

        membership = (
            own_membership_from_sync(response, self_user_id=control_user_id)
            if isinstance(response, nio.SyncResponse)
            else own_membership_from_sliding_sync(response, self_user_id=control_user_id)
        )
        authorization_changed = self._apply_control_membership(config, runtime_paths, membership)
        return ReplyMembershipPreAdmission(authorization_changed=authorization_changed)

    def _apply_control_membership(
        self,
        config: Config,
        runtime_paths: RuntimePaths,
        membership: OwnRoomMembership,
    ) -> bool:
        """Fail grant rooms closed when the router loses membership continuity."""
        authorization_changed = False
        for room_id in membership.continuity_lost_room_ids:
            changed = self._memberships.mark_control_room_unready(
                config,
                runtime_paths,
                room_id,
                reason="control_client_departed",
            )
            authorization_changed = changed or authorization_changed
        if authorization_changed:
            self._request_refresh()
        return authorization_changed

    def apply_live_transition(
        self,
        config: Config,
        runtime_paths: RuntimePaths,
        room_id: str,
        event: nio.RoomMemberEvent,
        *,
        control_user_id: str,
    ) -> bool:
        """Apply one accepted durable LIVE membership transition."""
        return self._memberships.apply_member_event(
            config,
            runtime_paths,
            room_id,
            event,
            control_user_id=control_user_id,
        )


def _sync_response_has_uncertain_membership(
    response: nio.SyncResponse | nio.SlidingSyncResponse,
) -> bool:
    """Return whether a sync response cannot prove a complete membership view."""
    if response.unrecovered_room_ids:
        return True
    if isinstance(response, nio.SyncResponse):
        return any(join_info.timeline.limited for join_info in response.rooms.join.values())
    return any(room.limited for room in response.rooms.values())
