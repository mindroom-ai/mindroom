"""Router-only receiver of email-invite claims sent as plain to-device events."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, TypeGuard

import aiohttp
import nio

from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.email_invites import store
from mindroom.email_invites.policy import may_invite, refused_room_kind
from mindroom.entity_resolution import entity_identity_registry
from mindroom.logging_config import get_logger
from mindroom.matrix.client_delivery import send_message_result
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
        if invite is None or invite.claimed_by not in (None, sender):
            return "rejected", None
        if invite.invited_at is not None:
            return "invited", None
        room_id, config = invite.room_id, self.config_getter()
        membership = await self._membership(room_id, sender)
        if membership in ("join", "invite"):
            # A bound claim whose invite went through before a restart is completed here, notice included.
            return "invited", self._used(invite, sender) if invite.claimed_by == sender else None
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
        if not store.bind_claim(self.paths, invite.id, sender):  # A revoke or expiry during the reads wins.
            return "rejected", None
        return await self._invite(invite, sender)

    async def _invite(self, invite: store.EmailInvite, sender: str) -> tuple[_Status, str | None]:
        """Send the Matrix invite; an unknown outcome kills the link, and the call that revoked it posts the notice."""
        try:
            response = await self.client.room_invite(invite.room_id, sender)
        except (aiohttp.ClientError, TimeoutError):
            response = None
        if isinstance(response, nio.RoomInviteResponse):
            return "invited", self._used(invite, sender)
        if getattr(response, "status_code", None) == "M_FORBIDDEN":
            return "rejected", None
        # An unknown outcome kills the link; a new approved invite for the address is not blocked by it.
        unknown = (
            f"The invite for {invite.email} may or may not have gone out, so its link no longer works."
            f" {invite.inviter_id} can send a new link if needed."
        )
        return "rejected", unknown if store.revoke(self.paths, invite.id) else None

    def _used(self, invite: store.EmailInvite, sender: str) -> str | None:
        """Record the invite as used and return its notice, only for the call that recorded it."""
        first = store.mark_invited(self.paths, invite.id)
        return f"Invite for {invite.email} (approved by {invite.inviter_id}) was used by {sender}." if first else None

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
