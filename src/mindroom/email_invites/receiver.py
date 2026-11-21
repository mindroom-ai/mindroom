"""Receiver of email-invite claims, sent as plain to-device events to the account whose link is used."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, TypeGuard

import aiohttp
import nio

from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.email_invites import store
from mindroom.email_invites.policy import may_invite, refused_room_kind
from mindroom.logging_config import get_logger
from mindroom.matrix.client_delivery import send_message_result
from mindroom.matrix.joined_members import read_joined_members
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


class _Receiver:
    def __init__(self, client: nio.AsyncClient, name: str, paths: RuntimePaths, config: Callable[[], Config]) -> None:
        self.client, self.agent_name, self.paths, self.config_getter = client, name, paths, config
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

    async def _membership(self, room_id: str, user_id: str) -> str | None:
        """Return the user's membership, None when they have no member event, or raise when unreadable."""
        response = await self.client.room_get_state_event(room_id, "m.room.member", user_id)
        if isinstance(response, nio.RoomGetStateEventResponse):
            return response.content.get("membership")
        if getattr(response, "status_code", None) == "M_NOT_FOUND":
            return None
        raise _UnreadableError

    async def _handle(self, invite: store.EmailInvite | None, sender: str) -> tuple[_Status, str | None]:  # noqa: PLR0911
        """Bind the code to the sender, recheck the rights, and send the Matrix invite; a notice comes with a won update."""
        # A link is answered only by the account it names, the one that sent it.
        if invite is None or invite.agent_name != self.agent_name or invite.claimed_by not in (None, sender):
            return "rejected", None
        if invite.invited_at is not None:
            return "invited", None
        if invite.sending_at is not None:  # A link causes at most one Matrix invite, ever.
            return "rejected", None
        room_id, config = invite.room_id, self.config_getter()
        membership = await self._membership(room_id, sender)
        if membership in ("join", "invite"):
            return "invited", None
        if membership == "ban" or not store.bind_claim(self.paths, invite.id, sender):
            return "rejected", None
        kind = await refused_room_kind(self.client, config, self.paths, room_id)
        approver_right = await may_invite(self.client, room_id, invite.inviter_id or "", config.email_invites.approvers)
        if kind not in (None, "unknown") or approver_right is False:
            store.revoke(self.paths, invite.id)
            return "rejected", None
        own_right = await may_invite(self.client, room_id, self.client.user_id)
        if kind == "unknown" or approver_right is None or own_right is not True:
            return "retry", None
        if not store.start_sending(self.paths, invite.id, sender):  # The one winner; a revoke or expiry wins first.
            return "rejected", None
        return await self._invite(invite, sender)

    async def _invite(self, invite: store.EmailInvite, sender: str) -> tuple[_Status, str | None]:
        """Send the one Matrix invite; any other outcome revokes the link, and the call that revoked it posts the notice."""
        try:
            response = await self.client.room_invite(invite.room_id, sender)
        except (aiohttp.ClientError, TimeoutError):
            response = None
        if isinstance(response, nio.RoomInviteResponse):
            used = f"Invite for {invite.email} (approved by {invite.inviter_id}) was used by {sender}."
            return "invited", used if store.mark_invited(self.paths, invite.id) else None
        await self.abandoned(store.abandon_sending(self.paths, invite_id=invite.id, agent_name=self.agent_name))
        return "rejected", None

    async def abandoned(self, invites: list[store.EmailInvite]) -> None:
        """Post the one notice of each link revoked while sending, by a failed invite or, at startup, a crash."""
        for invite in invites:
            body = f"The invite for {invite.email} may or may not have gone out, so its link no longer works."
            await self._notice(invite, f"{body} {invite.inviter_id} can send a new link if needed.")

    async def _notice(self, invite: store.EmailInvite, body: str) -> None:
        thread, notice = invite.thread_id, {"msgtype": "m.notice"}
        content = build_message_content(
            body,
            thread_event_id=thread,
            latest_thread_event_id=thread,
            extra_content=notice,
        )
        try:
            if (room := self.client.rooms.get(invite.room_id)) is not None and room.encrypted:
                # The claimant joins just after the result; this read outlasts that member change.
                await read_joined_members(self.client, invite.room_id)
            delivered = await send_message_result(self.client, invite.room_id, content)
        except (aiohttp.ClientError, TimeoutError, nio.LocalProtocolError):
            delivered = None
        if delivered is None:
            logger.warning("email_invite_notice_failed", invite_id=invite.id)


async def register_email_invite_receiver(
    *,
    client: nio.AsyncClient,
    agent_name: str,
    runtime_paths: RuntimePaths,
    config_getter: Callable[[], Config],
    callback_wrapper: Callable[[Callable[..., Awaitable[None]]], Callable[..., Awaitable[None]]],
) -> None:
    """Register this account's claim receiver, revoking its links a crash left sending, before any claim."""
    receiver = _Receiver(client, agent_name, runtime_paths, config_getter)
    if abandoned := store.abandon_sending(runtime_paths, agent_name=agent_name):
        await callback_wrapper(receiver.abandoned)(abandoned)
    wrapped = callback_wrapper(receiver.on_event)

    async def on_event(event: nio.UnknownToDeviceEvent) -> None:
        if (claim := receiver.admit(event)) is not None:
            await wrapped(event, *claim)

    client.add_to_device_callback(on_event, nio.UnknownToDeviceEvent)


async def revoke_orphaned_sends(client: nio.AsyncClient, runtime_paths: RuntimePaths, config: Config) -> None:
    """Revoke, with its one notice, each link a crash left sending for an entity that is no longer configured.

    No receiver starts for a removed or renamed entity, so an account revokes its links in the rooms that account's
    first sync lists, where it can post the notice. A configured entity's links are left to its own receiver.
    """
    configured = (ROUTER_AGENT_NAME, *config.agents, *config.teams)
    orphans = store.abandon_sending(runtime_paths, configured=configured, rooms=client.rooms)
    await _Receiver(client, "", runtime_paths, lambda: config).abandoned(orphans)
