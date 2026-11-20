"""Invite people to the current room by email, after a person who may invite approves."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from mindroom.approval.tool_approval import resolve_tool_approval_approver
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.email_invites import store
from mindroom.email_invites.mail import (
    SMTP_FROM_ENV,
    EmailInviteMailError,
    build_invite_mail,
    invite_link,
    normalize_email,
    send_invite_mail,
    smtp_configured,
)
from mindroom.email_invites.policy import may_invite, refused_room_kind
from mindroom.entity_resolution import entity_identity_registry
from mindroom.matrix.client_room_admin import get_room_name, room_encryption_enabled
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.runtime_context import get_tool_runtime_context
from mindroom.tool_system.tool_function import ToolGate

if TYPE_CHECKING:
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.tool_function import ToolFunction

_BAD_ADDRESS = "Error: that is not an email address this tool accepts (one plain ASCII address)."
_CHANGED = "Error: the invite changed meanwhile; ask again."
_LIMITS = {
    "asker": "at most 5 invites per person per day",
    "room": "at most 20 open invites per room",
    "address": "at most 3 mails per address per day",
    "deployment": "at most 50 mails per day",
}
_ROOM_KINDS = {"personal": "a personal room", "onboarding": "an onboarding room", "space": "a space"}
_UNREADABLE = "Error: room state unreadable; try again in a moment."


def _date(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d")


def _caller(context: ToolRuntimeContext | None) -> tuple[ToolRuntimeContext, str] | str:
    """Return the context and the person it acts for, or the error text."""
    if context is None:
        return "Error: Matrix room context unavailable."
    person = resolve_tool_approval_approver(context.current_config, context.runtime_paths, context.requester_id)
    return (context, person) if person is not None else "Error: only a person can ask for an email invite."


async def _refusal(context: ToolRuntimeContext, address: str | None) -> str | None:
    """Return why the invite is refused before the rights are read, or None."""
    if address is None:
        return _BAD_ADDRESS
    kind = await refused_room_kind(context.client, context.current_config, context.runtime_paths, context.room_id)
    if kind == "unknown":
        return _UNREADABLE
    return None if kind is None else f"Error: people cannot be invited by email to {_ROOM_KINDS[kind]}."


class EmailInviteTools(Toolkit):
    """Invite an email address to the current room with a single-use link."""

    def __init__(self) -> None:
        super().__init__(
            name="email_invite",
            tools=[self.invite_by_email, self.list_email_invites, self.revoke_email_invite],
        )

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Require a person's confirmation of every invitation."""
        return replace(function, gate=ToolGate.CONFIRM) if function.name == "invite_by_email" else function

    async def invite_by_email(self, email: str) -> str:
        """Invite one email address to this room.

        If the person asking may invite, a single-use link is mailed (or returned to forward by hand when no mail
        server is configured). Otherwise the request waits until someone who may invite asks for the same address.

        Args:
            email: One plain email address, such as alice@example.com.

        """
        caller = _caller(get_tool_runtime_context())
        if isinstance(caller, str):
            return caller
        context, approver = caller
        config, paths, room_id = context.current_config, context.runtime_paths, context.room_id
        address = normalize_email(email)
        if (refusal := await _refusal(context, address)) or address is None:
            return refusal or _BAD_ADDRESS
        router_id = entity_identity_registry(config, paths).current_id(ROUTER_AGENT_NAME).full_id
        if await may_invite(context.client, room_id, router_id) is not True:
            return "Error: the router may not invite people to this room."
        rights = await may_invite(context.client, room_id, approver, config.email_invites.approvers)
        if rights is None:
            return _UNREADABLE
        return await _request(context, approver, address, router_id, rights=rights)

    async def list_email_invites(self) -> str:
        """List the open email invites of this room with their state, asker, approver and expiry."""
        context = get_tool_runtime_context()
        if context is None:
            return "Error: Matrix room context unavailable."
        invites = store.open_invites(context.runtime_paths, context.room_id)
        lines = [
            f"- {invite.email}: {'sent' if invite.inviter_id else 'waiting'}, asked by {invite.requested_by}, "
            f"approved by {invite.inviter_id or 'nobody yet'}, expires {_date(invite.expires_at)}"
            for invite in invites
        ]
        return (
            "\n".join(["Open email invites in this room:", *lines]) if lines else "No open email invites in this room."
        )

    async def revoke_email_invite(self, email: str) -> str:
        """Revoke the open email invite for one address in this room.

        Args:
            email: The invited email address.

        """
        caller = _caller(get_tool_runtime_context())
        if isinstance(caller, str):
            return caller
        context, person = caller
        if (address := normalize_email(email)) is None:
            return _BAD_ADDRESS
        invite = store.open_invite(context.runtime_paths, room_id=context.room_id, email=address)
        approvers = context.current_config.email_invites.approvers
        if (
            invite is not None
            and person not in (invite.requested_by, invite.inviter_id)
            and await may_invite(context.client, context.room_id, person, approvers) is not True
        ):
            return "Error: only the asker, the approver, or someone who may invite can revoke this."
        if invite is not None and store.revoke(context.runtime_paths, invite.id):
            return f"Revoked the invite for {address}."
        # A revoke that loses to a claim reports the account that claimed.
        used = store.used_invite(context.runtime_paths, room_id=context.room_id, email=address) or (
            invite and store.get(context.runtime_paths, invite.id)
        )
        if used is not None and used.claimed_by is not None:
            return f"The invite for {address} was already used by {used.claimed_by}; nothing was changed."
        return f"No open invite for {address} in this room."


async def _request(context: ToolRuntimeContext, approver: str, address: str, router_id: str, *, rights: bool) -> str:
    """Store a waiting request, or send one when the person may invite."""
    paths, room_id, config = context.runtime_paths, context.room_id, context.current_config
    existing = store.open_invite(paths, room_id=room_id, email=address)
    if not rights and existing is not None:
        if existing.inviter_id is None:
            return f"An invite for {address} is already waiting for someone who may invite."
        return f"An invite for {address} was already sent; it is valid until {_date(existing.expires_at)}."
    request = store.Request(room_id, context.target.resolved_thread_id, address, approver, context.agent_name)
    try:
        if not rights:
            store.create_request(paths, request, expiry_days=config.email_invites.expiry_days)
            return (
                f"Stored, nothing sent: {approver} may not invite people to this room. Someone who may invite "
                f'can approve it by asking an agent here: "approve the invite for {address}".'
            )
        return await _send(context, request, router_id)
    except store.EmailInviteLimitError as exc:
        return f"Error: limit reached: {_LIMITS[exc.limit]}."
    except ValueError:
        return _CHANGED


async def _send(context: ToolRuntimeContext, request: store.Request, router_id: str) -> str:
    """Issue the code and mail the link, or return it when no mail server is configured."""
    paths, settings = context.runtime_paths, context.current_config.email_invites
    if mailing := smtp_configured(paths):
        # Read before the code exists, so a failed read leaves no unsent code.
        room_name = await get_room_name(context.client, context.room_id)
        encrypted = await room_encryption_enabled(context.client, context.room_id) is True
    invite, code = store.issue_code(paths, request, expiry_days=settings.expiry_days)
    address, approver = request.email, request.person
    link, date = invite_link(settings.chat_url, router_id, code), _date(invite.expires_at)
    if not mailing:
        return (
            f"No mail server is configured, so nothing was mailed. Give this link to {address} yourself. "
            f"It works once, expires on {date}, and lets whoever opens it join this room: {link}"
        )
    try:
        message = build_invite_mail(
            sender=paths.env_value(SMTP_FROM_ENV, default=""),
            recipient=address,
            inviter_name=(context.room.user_name(approver) if context.room is not None else None) or approver,
            inviter_id=approver,
            room_name=room_name,
            link=link,
            expires_at=invite.expires_at,
            encrypted=encrypted,
        )
        await send_invite_mail(paths, message)
    except (EmailInviteMailError, ValueError) as exc:
        store.revoke(paths, invite.id)
        reason = exc if isinstance(exc, EmailInviteMailError) else "the sender address is invalid"
        return f"Error: the mail could not be sent ({reason}). The invite was cancelled."
    return f"Invitation mailed to {address}. The link works once and expires on {date}."
