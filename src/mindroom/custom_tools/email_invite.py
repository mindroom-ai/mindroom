"""Invite people to the current room by email, after a person who may invite approves."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from mindroom.approval.auto_approve_windows import auto_approve_expires_at
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
    from email.message import EmailMessage

    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.tool_function import ToolFunction

_NO_CONTEXT = "Error: Matrix room context unavailable."
_NO_PERSON = "Error: only a person can ask for an email invite."
_AUTO_APPROVE = (
    'Error: auto-approve is on in this conversation. Send "!auto_approve off" and ask again, '
    "so the invitation gets its own approval."
)
_BAD_ADDRESS = "Error: that is not an email address this tool accepts (one plain ASCII address)."
_ROUTER_MAY_NOT = "Error: the router may not invite people to this room."
_UNREADABLE = "Error: room state unreadable; try again in a moment."
_CHANGED = "Error: the invite changed meanwhile; ask again."
_LIMIT_TEXTS = {
    "asker": "at most 5 invites per person per day",
    "room": "at most 20 open invites per room",
    "address": "at most 3 mails per address per day",
    "deployment": "at most 50 mails per day",
}
_ROOM_KIND_TEXTS = {"personal": "a personal room", "onboarding": "an onboarding room"}


def _date(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d")


def _limit(exc: store.EmailInviteLimitError) -> str:
    return f"Error: limit reached: {_LIMIT_TEXTS[exc.limit]}."


def _router_id(context: ToolRuntimeContext) -> str:
    identities = entity_identity_registry(context.current_config, context.runtime_paths)
    return identities.current_id(ROUTER_AGENT_NAME).full_id


class EmailInviteTools(Toolkit):
    """Invite an email address to the current room with a single-use link."""

    def __init__(self) -> None:
        super().__init__(
            name="email_invite",
            tools=[self.invite_by_email, self.list_email_invites, self.revoke_email_invite],
        )

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Require a person's confirmation of every invitation."""
        if function.name == "invite_by_email":
            return replace(function, gate=ToolGate.CONFIRM)
        return function

    async def invite_by_email(self, email: str) -> str:
        """Invite one email address to this room.

        If the person asking may invite, a single-use link is mailed (or returned to forward by hand when no mail
        server is configured). Otherwise the request waits until someone who may invite asks for the same address.

        Args:
            email: One plain email address, such as alice@example.com.

        """
        context = get_tool_runtime_context()
        if context is None:
            return _NO_CONTEXT
        config = context.current_config
        paths = context.runtime_paths
        room_id = context.room_id
        approver = resolve_tool_approval_approver(config, paths, context.requester_id)
        if approver is None:
            return _NO_PERSON
        thread_id = context.target.resolved_thread_id
        if auto_approve_expires_at(paths, room_id=room_id, thread_id=thread_id, requester_id=approver) is not None:
            return _AUTO_APPROVE
        address = normalize_email(email)
        if address is None:
            return _BAD_ADDRESS
        if (kind := refused_room_kind(config, paths, room_id)) is not None:
            return f"Error: people cannot be invited by email to {_ROOM_KIND_TEXTS[kind]}."
        router_id = _router_id(context)
        if await may_invite(context.client, room_id, router_id) is not True:
            return _ROUTER_MAY_NOT
        rights = await may_invite(context.client, room_id, approver, config.email_invites.approvers)
        if rights is None:
            return _UNREADABLE
        settings = config.email_invites
        existing = store.open_invite(paths, room_id=room_id, email=address)
        if not rights:
            if existing is not None:
                return _already(existing)
            try:
                store.create_request(
                    paths,
                    room_id=room_id,
                    thread_id=thread_id,
                    email=address,
                    requested_by=approver,
                    agent_name=context.agent_name,
                    expiry_days=settings.expiry_days,
                )
            except store.EmailInviteLimitError as exc:
                return _limit(exc)
            except ValueError:
                return _CHANGED
            return (
                f"Stored, nothing sent: {approver} may not invite people to this room. Someone who may invite can "
                f'approve it by asking an agent here: "approve the invite for {address}".'
            )

        mailing = smtp_configured(paths)
        message_inputs = await _mail_inputs(context, approver) if mailing else None
        if existing is not None and existing.inviter_id is not None:
            # A new send replaces the old one, so the old link stops working.
            store.revoke(paths, existing.id)
            existing = None
        try:
            if existing is None:
                existing = store.create_request(
                    paths,
                    room_id=room_id,
                    thread_id=thread_id,
                    email=address,
                    requested_by=approver,
                    agent_name=context.agent_name,
                    expiry_days=settings.expiry_days,
                )
            code = store.issue_code(paths, existing.id, inviter_id=approver, expiry_days=settings.expiry_days)
        except store.EmailInviteLimitError as exc:
            return _limit(exc)
        except ValueError:
            return _CHANGED
        invite = store.find_by_code(paths, code)
        if invite is None:
            return _CHANGED
        link = invite_link(settings.chat_url, router_id, code)
        date = _date(invite.expires_at)
        if message_inputs is None:
            return (
                f"No mail server is configured, so nothing was mailed. Give this link to {address} yourself. "
                f"It works once, expires on {date}, and lets whoever opens it join this room: {link}"
            )
        inviter_name, room_name, encrypted = message_inputs
        message = build_invite_mail(
            sender=paths.env_value(SMTP_FROM_ENV, default=""),
            recipient=address,
            inviter_name=inviter_name,
            inviter_id=approver,
            room_name=room_name,
            link=link,
            expires_at=invite.expires_at,
            encrypted=encrypted,
        )
        return await _send(context, invite, message, address, date)

    async def list_email_invites(self) -> str:
        """List the open email invites of this room with their state, asker, approver and expiry."""
        context = get_tool_runtime_context()
        if context is None:
            return _NO_CONTEXT
        invites = store.open_invites(context.runtime_paths, context.room_id)
        if not invites:
            return "No open email invites in this room."
        lines = [
            f"- {invite.email}: {'sent' if invite.inviter_id else 'waiting'}, asked by {invite.requested_by}, "
            f"approved by {invite.inviter_id or 'nobody yet'}, expires {_date(invite.expires_at)}"
            for invite in invites
        ]
        return "Open email invites in this room:\n" + "\n".join(lines)

    async def revoke_email_invite(self, email: str) -> str:
        """Revoke the open email invite for one address in this room.

        Args:
            email: The invited email address.

        """
        context = get_tool_runtime_context()
        if context is None:
            return _NO_CONTEXT
        config = context.current_config
        paths = context.runtime_paths
        address = normalize_email(email)
        if address is None:
            return _BAD_ADDRESS
        caller = resolve_tool_approval_approver(config, paths, context.requester_id)
        if caller is None:
            return _NO_PERSON
        invite = store.open_invite(paths, room_id=context.room_id, email=address)
        if invite is None:
            return _not_open(context, address)
        if caller not in (invite.requested_by, invite.inviter_id) and (
            await may_invite(context.client, context.room_id, caller, config.email_invites.approvers) is not True
        ):
            return "Error: only the asker, the approver, or someone who may invite can revoke this."
        if not store.revoke(paths, invite.id):
            return _not_open(context, address)
        return f"Revoked the invite for {address}."


def _already(invite: store.EmailInvite) -> str:
    if invite.inviter_id is None:
        return f"An invite for {invite.email} is already waiting for someone who may invite."
    return f"An invite for {invite.email} was already sent; it is valid until {_date(invite.expires_at)}."


def _not_open(context: ToolRuntimeContext, address: str) -> str:
    used = store.used_invite(context.runtime_paths, room_id=context.room_id, email=address)
    if used is not None:
        return f"The invite for {address} was already used by {used.claimed_by}; nothing was changed."
    return f"No open invite for {address} in this room."


async def _mail_inputs(context: ToolRuntimeContext, approver: str) -> tuple[str, str, bool]:
    """Read the inviter's name, the room name, and whether the room is encrypted, before a code exists."""
    room_name = await get_room_name(context.client, context.room_id)
    encrypted = await room_encryption_enabled(context.client, context.room_id) is True
    inviter_name = (context.room.user_name(approver) if context.room is not None else None) or approver
    return inviter_name, room_name, encrypted


async def _send(
    context: ToolRuntimeContext,
    invite: store.EmailInvite,
    message: EmailMessage,
    address: str,
    date: str,
) -> str:
    try:
        await send_invite_mail(context.runtime_paths, message)
    except EmailInviteMailError as exc:
        store.revoke(context.runtime_paths, invite.id)
        return f"Error: the mail could not be sent ({exc}). The invite was cancelled."
    return f"Invitation mailed to {address}. The link works once and expires on {date}."
