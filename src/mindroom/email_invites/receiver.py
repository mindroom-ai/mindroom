"""Router-only receiver of email-invite claims sent as plain to-device events."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from contextlib import closing
from typing import TYPE_CHECKING, Literal, TypeGuard

import aiohttp
import nio

from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.email_invites import store
from mindroom.email_invites.policy import may_invite, refused_room_kind
from mindroom.entity_resolution import entity_identity_registry
from mindroom.logging_config import get_logger
from mindroom.matrix.client_delivery import send_message_result
from mindroom.matrix.client_session import olm_store_dir
from mindroom.matrix.message_builder import build_message_content
from mindroom.mcp_gateway.admission import OnboardingRateLimiter
from mindroom.requester_identity import is_human_requester_id

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

_CLAIM_EVENT_TYPE = "io.mindroom.email_invite.claim"
_RESULT_EVENT_TYPE = "io.mindroom.email_invite.result"
_CLAIM_KEYS = {"version", "request_id", "code"}
# A dispatch older than this whose outcome no state proves is treated as spent: the link fails closed.
_DISPATCH_SECONDS = 60

_Status = Literal["invited", "rejected", "retry"]
logger = get_logger(__name__)


class _UnreadableError(Exception):
    """Room state needed for the claim could not be read."""


def _text(value: object, limit: int) -> TypeGuard[str]:
    return isinstance(value, str) and 0 < len(value) <= limit


def _claim(event: nio.UnknownToDeviceEvent) -> tuple[str, str] | None:
    """Return `(request_id, code)` of a well-formed version 1 claim, or None."""
    content = event.source.get("content")
    if event.type != _CLAIM_EVENT_TYPE or not isinstance(content, dict) or content.keys() != _CLAIM_KEYS:
        return None
    version, request_id, code = content["version"], content["request_id"], content["code"]
    valid = type(version) is int and version == 1 and _text(request_id, 64) and _text(code, 128)
    return (request_id, code) if valid else None


def _scrub_sync_store(path: str) -> None:
    """Move the acknowledged sync records, which held the plain code, out of the durable store's WAL."""
    try:
        with closing(sqlite3.connect(path, timeout=1)) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        logger.warning("email_invite_sync_store_scrub_failed")


class _Receiver:
    def __init__(self, client: nio.AsyncClient, paths: RuntimePaths, config_getter: Callable[[], Config]) -> None:
        self.client, self.paths, self.config_getter = client, paths, config_getter
        self.limiter = OnboardingRateLimiter(60, 10)

    def admit(self, event: nio.UnknownToDeviceEvent) -> tuple[str, str] | None:
        """Admit a well-formed claim from a person within the rate limits, before any task or store read."""
        claim = _claim(event)
        if claim is None or not self.limiter.allow(event.sender):
            return None
        return claim if is_human_requester_id(event.sender, self.config_getter(), self.paths) else None

    async def on_event(self, event: nio.UnknownToDeviceEvent, request_id: str, code: str) -> None:
        invite = store.find_by_code(self.paths, code)
        try:
            status, notice = await self._handle(invite, event.sender)
        except (aiohttp.ClientError, TimeoutError, _UnreadableError):
            status, notice = "retry", None
        content: dict[str, object] = {"version": 1, "request_id": request_id, "status": status}
        if status == "invited" and invite is not None:
            content["room_id"] = invite.room_id
        logger.info("email_invite_claim", sender=event.sender, invite_id=invite and invite.id, status=status)
        response = await self.client.to_device(nio.ToDeviceMessage(_RESULT_EVENT_TYPE, event.sender, "*", content))
        if isinstance(response, nio.ToDeviceError):
            logger.warning("email_invite_result_send_failed", sender=event.sender, status_code=response.status_code)
        if notice is not None and invite is not None:
            await self._notice(invite, notice)
        database = olm_store_dir(self.client.user_id, self.paths) / f"{self.client.user_id}_{self.client.device_id}.db"
        if database.is_file():
            await asyncio.to_thread(_scrub_sync_store, str(database))

    async def _membership(self, room_id: str, user_id: str) -> str | None:
        """Return the user's membership, None when they have no member event, or raise when unreadable."""
        response = await self.client.room_get_state_event(room_id, "m.room.member", user_id)
        if isinstance(response, nio.RoomGetStateEventResponse):
            return response.content.get("membership")
        if getattr(response, "status_code", None) == "M_NOT_FOUND":
            return None
        raise _UnreadableError

    async def _handle(self, invite: store.EmailInvite | None, sender: str) -> tuple[_Status, str | None]:  # noqa: PLR0911
        """Bind the code to the sender, recheck the rights, and win the one Matrix invite before sending it."""
        if invite is None or invite.claimed_by not in (None, sender):
            return "rejected", None
        if invite.invited_at is not None:
            return "invited", None
        notice = f"Invite for {invite.email} (approved by {invite.inviter_id}) was used by {sender}."
        room_id, config = invite.room_id, self.config_getter()
        membership = await self._membership(room_id, sender)
        if membership in ("join", "invite"):
            # A bound claim whose invite went through before a restart is completed here, notice included.
            first = invite.claimed_by == sender and store.mark_invited(self.paths, invite.id)
            return "invited", notice if first else None
        if membership == "ban" or not store.bind_claim(self.paths, invite.id, sender):
            return "rejected", None
        kind = await refused_room_kind(self.client, config, self.paths, room_id)
        approver_right = await may_invite(self.client, room_id, invite.inviter_id or "", config.email_invites.approvers)
        if kind not in (None, "unknown") or approver_right is False:
            store.revoke(self.paths, invite.id)
            return "rejected", None
        router_id = entity_identity_registry(config, self.paths).current_id(ROUTER_AGENT_NAME).full_id
        if kind == "unknown" or approver_right is None or await may_invite(self.client, room_id, router_id) is not True:
            return "retry", None
        if not store.start_dispatch(self.paths, invite.id, sender):
            current = store.get(self.paths, invite.id)
            if current is None or current.revoked:
                return "rejected", None
            if current.invited_at is not None:
                return "invited", None
            fresh = current.dispatched_at is not None and time.time() - current.dispatched_at < _DISPATCH_SECONDS
            return ("retry" if fresh else "rejected"), None
        status = await self._invite(invite, sender)
        return status, notice if status == "invited" else None

    async def _invite(self, invite: store.EmailInvite, sender: str) -> _Status:
        """Send the Matrix invite this claim won; an unanswered request keeps the dispatch, so it fails closed."""
        response = await self.client.room_invite(invite.room_id, sender)
        if isinstance(response, nio.RoomInviteResponse):
            store.mark_invited(self.paths, invite.id)
            return "invited"
        store.end_dispatch(self.paths, invite.id)
        return "rejected" if getattr(response, "status_code", None) == "M_FORBIDDEN" else "retry"

    async def _notice(self, invite: store.EmailInvite, body: str) -> None:
        thread, notice = invite.thread_id, {"msgtype": "m.notice"}
        content = build_message_content(
            body,
            thread_event_id=thread,
            latest_thread_event_id=thread,
            extra_content=notice,
        )
        try:
            delivered = await send_message_result(self.client, invite.room_id, content)
        except (aiohttp.ClientError, TimeoutError):
            delivered = None
        if delivered is None:
            logger.warning("email_invite_notice_failed", invite_id=invite.id)


def register_email_invite_receiver(
    *,
    client: nio.AsyncClient,
    agent_name: str,
    runtime_paths: RuntimePaths,
    config_getter: Callable[[], Config],
    callback_wrapper: Callable[[Callable[..., Awaitable[None]]], Callable[..., Awaitable[None]]],
) -> None:
    """Register the claim receiver on the router only, admitting claims before a task is created."""
    if agent_name != ROUTER_AGENT_NAME:
        return
    receiver = _Receiver(client, runtime_paths, config_getter)
    wrapped = callback_wrapper(receiver.on_event)

    async def on_event(event: nio.UnknownToDeviceEvent) -> None:
        if (claim := receiver.admit(event)) is not None:
            await wrapped(event, *claim)

    client.add_to_device_callback(on_event, nio.UnknownToDeviceEvent)
