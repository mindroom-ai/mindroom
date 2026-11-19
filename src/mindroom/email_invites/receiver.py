"""Router-only receiver of email-invite claims sent as plain to-device events."""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from typing import TYPE_CHECKING, Literal

import aiohttp
import nio

from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.email_invites import store
from mindroom.email_invites.policy import may_invite, refused_room_kind
from mindroom.entity_resolution import entity_identity_registry
from mindroom.logging_config import get_logger
from mindroom.matrix.client_delivery import send_message_result
from mindroom.matrix.client_room_admin import get_room_membership
from mindroom.matrix.message_builder import build_message_content
from mindroom.requester_identity import is_human_requester_id

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

_CLAIM_EVENT_TYPE = "io.mindroom.email_invite.claim"
_RESULT_EVENT_TYPE = "io.mindroom.email_invite.result"
_WINDOW_SECONDS = 60
_SENDER_LIMIT, _TOTAL_LIMIT, _TRACKED_SENDERS = 10, 60, 1024

_Status = Literal["invited", "rejected", "retry"]
logger = get_logger(__name__)


def _claim(event: nio.UnknownToDeviceEvent) -> tuple[str, str] | None:
    """Return `(request_id, code)` of a well-formed version 1 claim, or None."""
    content = event.source.get("content")
    if (
        event.type != _CLAIM_EVENT_TYPE
        or not isinstance(content, dict)
        or set(content) != {"version", "request_id", "code"}
    ):
        return None
    request_id, code = content["request_id"], content["code"]
    if type(content["version"]) is not int or content["version"] != 1:
        return None
    if (
        not isinstance(request_id, str)
        or not isinstance(code, str)
        or not (0 < len(request_id) <= 64 and 0 < len(code) <= 128)
    ):
        return None
    return request_id, code


class _Receiver:
    def __init__(self, client: nio.AsyncClient, paths: RuntimePaths, config_getter: Callable[[], Config]) -> None:
        self.client, self.paths, self.config_getter = client, paths, config_getter
        self.total: deque[float] = deque()
        self.senders: OrderedDict[str, deque[float]] = OrderedDict()

    def admit(self, event: nio.UnknownToDeviceEvent) -> bool:
        """Admit a well-formed claim from a person within the rate limits, before any task or store read."""
        if _claim(event) is None or not is_human_requester_id(event.sender, self.config_getter(), self.paths):
            return False
        now = time.monotonic()
        cutoff = now - _WINDOW_SECONDS
        while self.total and self.total[0] <= cutoff:
            self.total.popleft()
        while self.senders and next(iter(self.senders.values()))[-1] <= cutoff:
            self.senders.popitem(last=False)
        stamps = self.senders.get(event.sender, deque())
        while stamps and stamps[0] <= cutoff:
            stamps.popleft()
        tracked = event.sender in self.senders or len(self.senders) < _TRACKED_SENDERS
        if len(self.total) >= _TOTAL_LIMIT or len(stamps) >= _SENDER_LIMIT or not tracked:
            logger.info("email_invite_claim_rate_limited", sender=event.sender)
            return False
        stamps.append(now)
        self.total.append(now)
        self.senders[event.sender] = stamps
        self.senders.move_to_end(event.sender)
        return True

    async def on_event(self, event: nio.UnknownToDeviceEvent) -> None:
        claim = _claim(event)
        if claim is None:
            return
        request_id, code = claim
        invite = store.find_by_code(self.paths, code)
        try:
            status = await self._handle(invite, event.sender)
        except (aiohttp.ClientError, TimeoutError):
            status = "retry"
        content: dict[str, object] = {"version": 1, "request_id": request_id, "status": status}
        if status == "invited" and invite is not None:
            content["room_id"] = invite.room_id
        logger.info("email_invite_claim", sender=event.sender, invite_id=invite and invite.id, status=status)
        message = nio.ToDeviceMessage(_RESULT_EVENT_TYPE, event.sender, "*", content)
        response = await self.client.to_device(message)
        if isinstance(response, nio.ToDeviceError):
            logger.warning("email_invite_result_send_failed", sender=event.sender, error=str(response))

    async def _handle(self, invite: store.EmailInvite | None, sender: str) -> _Status:  # noqa: PLR0911
        """Bind the code to the sender and invite them, as the claim steps of the spec say."""
        if invite is None or invite.claimed_by not in (None, sender):
            return "rejected"
        if invite.invited_at is not None:
            return "invited"
        room_id, config = invite.room_id, self.config_getter()
        membership = await get_room_membership(self.client, room_id, sender)
        if membership == "ban":
            return "rejected"
        if membership in ("join", "invite"):
            if invite.claimed_by == sender:
                store.mark_invited(self.paths, invite.id)
            return "invited"
        if not store.bind_claim(self.paths, invite.id, sender):
            return "rejected"
        approver_right = await may_invite(self.client, room_id, invite.inviter_id or "", config.email_invites.approvers)
        if refused_room_kind(config, self.paths, room_id) is not None or approver_right is False:
            store.revoke(self.paths, invite.id)
            return "rejected"
        router_id = entity_identity_registry(config, self.paths).current_id(ROUTER_AGENT_NAME).full_id
        if approver_right is None or await may_invite(self.client, room_id, router_id) is not True:
            return "retry"
        return await self._invite(invite, sender)

    async def _invite(self, invite: store.EmailInvite, sender: str) -> _Status:
        response = await self.client.room_invite(invite.room_id, sender)
        if not isinstance(response, nio.RoomInviteResponse):
            return "rejected" if getattr(response, "status_code", None) == "M_FORBIDDEN" else "retry"
        store.mark_invited(self.paths, invite.id)
        body = f"Invite for {invite.email} (approved by {invite.inviter_id}) was used by {sender}."
        content = build_message_content(
            body,
            thread_event_id=invite.thread_id,
            latest_thread_event_id=invite.thread_id,
            extra_content={"msgtype": "m.notice"},
        )
        try:
            delivered = await send_message_result(self.client, invite.room_id, content)
        except (aiohttp.ClientError, TimeoutError):
            delivered = None
        if delivered is None:
            logger.warning("email_invite_notice_failed", invite_id=invite.id)
        return "invited"


def register_email_invite_receiver(
    *,
    client: nio.AsyncClient,
    agent_name: str,
    runtime_paths: RuntimePaths,
    config_getter: Callable[[], Config],
    callback_wrapper: Callable[[Callable[[nio.UnknownToDeviceEvent], Awaitable[None]]], Callable[..., Awaitable[None]]],
) -> None:
    """Register the claim receiver on the router only, admitting claims before a task is created."""
    if agent_name != ROUTER_AGENT_NAME:
        return
    receiver = _Receiver(client, runtime_paths, config_getter)
    wrapped = callback_wrapper(receiver.on_event)

    async def on_event(event: nio.UnknownToDeviceEvent) -> None:
        if receiver.admit(event):
            await wrapped(event)

    client.add_to_device_callback(on_event, nio.UnknownToDeviceEvent)
