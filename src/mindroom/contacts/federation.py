"""Contacts with agents of other MindRoom deployments, which reach this one only through Matrix membership.

The invite into a contact room is the request, joining it accepts, rejecting it declines, and leaving it ends the
contact. Each deployment keeps its own record and enforces only its own owner's decisions, learning the other side's
from the membership of the contact room.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING
from uuid import uuid4

from mindroom.background_tasks import run_blocking_until_complete
from mindroom.contacts.flow import (
    ContactActor,
    announce_acceptance,
    answerer_problem,
    existing_contact,
    leave_contact_room,
    post_in_thread,
    post_request_notice,
    reject_contact_invite,
    withdraw_unjoined_owner_invites,
)
from mindroom.contacts.parties import agent_party, remote_party, remote_target
from mindroom.contacts.store import (
    ContactBook,
    ContactParty,
    ContactRecord,
    ContactSide,
    ContactStatus,
    SideName,
    agent_contacts,
    update_contacts,
)
from mindroom.logging_config import get_logger
from mindroom.matrix.client_room_admin import ensure_managed_room_power_levels, get_room_membership, invite_to_room

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.bot import AgentBot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)


def is_contact_invite(config: Config, runtime_paths: RuntimePaths, agent_name: str, inviter: str) -> bool:
    """Return whether an invite to this agent is another deployment's contact request.

    It is when the agent is a personal agent with contacts and the inviter is an agent of another deployment that the
    agent's ``contacts.remote_agents`` name; such an invite never reaches the generic invite handling.
    """
    return (
        agent_party(config, agent_name) is not None
        and remote_target(config, runtime_paths, agent_name, inviter) is not None
    )


def _agent(
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    agent_bot: Callable[[str], AgentBot | None],
) -> tuple[ContactActor, ContactParty] | None:
    """Return the owner of a personal agent with contacts acting through it, and the agent's party, or None."""
    party = agent_party(config, agent_name)
    if party is None:
        return None
    actor = ContactActor(
        owner=party.local_owner,
        agent_name=agent_name,
        config=config,
        runtime_paths=runtime_paths,
        agent_bot=agent_bot,
    )
    return actor, party


async def _reject(actor: ContactActor, agent_name: str, room_id: str, inviter: str, why: str) -> None:
    """Reject the invite, which the other side reads as a decline."""
    logger.info("contact_request_rejected", agent_name=agent_name, room_id=room_id, inviter=inviter, reason=why)
    await reject_contact_invite(actor, agent_name, room_id)


def _store_request(book: ContactBook, *, record: ContactRecord, agent_name: str) -> tuple[ContactRecord, bool] | None:
    """Store a new request and return it with True, or return the one this room already holds with False.

    Return None, storing no new request, for a room whose request was declined or removed, for a pair with an active contact
    or this agent's own pending request in another room, or one this side declined, which blocks the pair until the
    owner removes the declined contact. A decline of this agent's own request by the other side blocks only that side,
    and the other side keeps one pending request per pair, so the other agent asking now clears that decline and
    replaces its own earlier request, whose withdrawal this side does not see.
    """
    for stored in book.contacts:
        if stored.room_id == record.room_id and stored.agent_side(agent_name) is not None:
            # A room whose request was answered or withdrawn is no new request.
            return (stored.model_copy(deep=True), False) if stored.status in {"pending", "active"} else None
    for stored in book.contacts:
        ours_declined = stored.requester.id == agent_name and stored.target.id == record.requester.id
        theirs = stored.requester.id == record.requester.id and stored.target.id == agent_name
        if (stored.status == "declined" and ours_declined) or (stored.status == "pending" and theirs):
            stored.status = "removed"
    try:
        if existing_contact(book, record.requester, record.target, rules=None, tools=None) is not None:
            return None
    except ValueError:
        return None
    book.contacts.append(record)
    return record.model_copy(deep=True), True


@dataclass(frozen=True)
class _InviteAnswer:
    """What answering one invite from another deployment still has to send, once its request is stored."""

    room_id: str
    inviter: str
    # Why the invite is rejected, or None when it is a stored request.
    rejection: str | None
    # The request whose notice the owner still needs, or None when nothing is posted.
    notice: ContactRecord | None


async def store_contact_invite(
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    *,
    room_id: str,
    inviter: str,
    reason: object,
) -> _InviteAnswer | None:
    """Store another deployment's request to this agent, or decide to reject it, and return what answering still sends.

    The request is stored before the invite's handling returns, so a process that stops before the network work still
    has it. It is rejected without a reason, without a personal-room agent that can answer it, and for a pair that
    already has a pending or active contact or whose last request this side declined. The same invite seen again
    stores nothing new and posts the notice only while it is missing.
    """
    target = agent_party(config, agent_name)
    if target is None:
        return None
    text = reason.strip() if isinstance(reason, str) else ""
    # The answerer's problem says which of its owner's agents is missing or lacks contacts, for an operator to fix.
    rejection = "no_reason" if not text else answerer_problem(config, target.local_owner)
    if rejection is not None:
        return _InviteAnswer(room_id, inviter, rejection, None)
    record = ContactRecord(
        id=uuid4().hex[:12],
        status="pending",
        requester=remote_party(inviter),
        target=target,
        reason=text,
        requester_side=ContactSide(),
        target_side=ContactSide(),
        origin_room_id=None,
        origin_thread_id=None,
        room_id=room_id,
        requested_at=datetime.now(UTC),
    )
    store = partial(_store_request, record=record, agent_name=agent_name)
    stored = await run_blocking_until_complete(update_contacts, runtime_paths, store)
    if stored is None:
        return _InviteAnswer(room_id, inviter, "pair_has_a_contact", None)
    record, new = stored
    unposted = new or (record.status == "pending" and record.notice_event_id is None)
    return _InviteAnswer(room_id, inviter, None, record if unposted else None)


async def answer_contact_invite(
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    agent_bot: Callable[[str], AgentBot | None],
    answer: _InviteAnswer,
) -> None:
    """Reject the invite, or ask the owner in their personal room; a notice that cannot be posted is only logged.

    The request still waits on the Contacts page when its notice fails.
    """
    found = _agent(config, runtime_paths, agent_name, agent_bot)
    if found is None:
        return
    actor, _party = found
    if answer.rejection is not None:
        await _reject(actor, agent_name, answer.room_id, answer.inviter, answer.rejection)
        return
    if answer.notice is None:
        return
    try:
        await post_request_notice(actor, answer.notice)
    except ValueError as exc:
        logger.warning(
            "contact_request_notice_failed",
            contact_id=answer.notice.id,
            agent_name=agent_name,
            error=str(exc),
        )


def _settle(
    record_id: str,
    before: ContactStatus,
    after: ContactStatus,
) -> Callable[[ContactBook], ContactRecord | None]:
    """Return a store change that moves a contact from ``before`` to ``after`` and returns it, or None if it moved on."""

    def change(book: ContactBook) -> ContactRecord | None:
        current = next(record for record in book.contacts if record.id == record_id)
        if current.status != before:
            return None
        current.status = after
        if after in {"active", "declined"}:
            current.decided_at = datetime.now(UTC)
        return current.model_copy(deep=True)

    return change


def _remote_contact_in(
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    room_id: str,
) -> tuple[ContactRecord, SideName] | None:
    """Return this agent's pending or active contact with another deployment's agent in the room, with its side."""
    for record, side in agent_contacts(config, runtime_paths, agent_name):
        if record.room_id == room_id and record.status in {"pending", "active"}:
            return (record, side) if record.counterparty(side).kind == "remote_agent" else None
    return None


async def contact_member_event(
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    agent_bot: Callable[[str], AgentBot | None],
    *,
    room_id: str,
    user_id: str,
    membership: str,
) -> None:
    """Apply the other agent's membership change in the room of this agent's contact with another deployment's agent.

    For a request this agent sent, the other agent joining accepts it and leaving or being banned declines it. Once
    active, the other agent leaving or being banned ends the contact here too. Every other change, and the same change
    seen again, changes nothing.
    """
    found = _agent(config, runtime_paths, agent_name, agent_bot)
    if found is None:
        return
    actor, _party = found
    match = await run_blocking_until_complete(_remote_contact_in, config, runtime_paths, agent_name, room_id)
    if match is None or user_id != match[0].counterparty(match[1]).id:
        return
    record, side = match
    await _catch_up(
        actor,
        record,
        agent_name,
        membership,
        pending_request=record.status == "pending" and side == "requester",
    )


async def contact_room_left(
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    agent_bot: Callable[[str], AgentBot | None],
    *,
    room_id: str,
) -> None:
    """End this agent's contact with another deployment's agent in a room it no longer is in, as after being removed.

    An agent's own departure reaches only its membership bookkeeping, not the room's member events. One it made itself
    finds the contact already ended, and a request it was asked, whose room it left again after a failed acceptance,
    stays pending.
    """
    found = _agent(config, runtime_paths, agent_name, agent_bot)
    if found is None:
        return
    actor, _party = found
    match = await run_blocking_until_complete(_remote_contact_in, config, runtime_paths, agent_name, room_id)
    if match is None or (match[0].status == "pending" and match[1] != "requester"):
        return
    record, _side = match
    await _end(actor, record, agent_name, record.status, leave=False)


async def _accepted(actor: ContactActor, record: ContactRecord, agent_name: str) -> None:
    """Make the request active, give the other agent the power to seat its owner, invite this owner, and continue."""
    change = _settle(record.id, "pending", "active")
    accepted = await run_blocking_until_complete(update_contacts, actor.runtime_paths, change)
    if accepted is None or accepted.room_id is None:
        return
    bot = actor.agent_bot(agent_name)
    client = None if bot is None else bot.client
    # The other agent then invites and withdraws its own owner; where creators have no unlimited power, neither agent can remove the other.
    if client is None or not await ensure_managed_room_power_levels(client, accepted.room_id, [accepted.target.id]):
        logger.warning("contact_room_power_grant_failed", contact_id=record.id, user_id=accepted.target.id)
    if client is None or not await invite_to_room(client, accepted.room_id, actor.owner):
        logger.warning("contact_room_owner_invite_failed", contact_id=record.id, user_id=actor.owner)
    await announce_acceptance(actor, accepted, agent_name)


async def _declined(actor: ContactActor, record: ContactRecord, agent_name: str) -> None:
    """Record the decline, tell the thread the request came from, and leave the room nobody else joined."""
    change = _settle(record.id, "pending", "declined")
    declined = await run_blocking_until_complete(update_contacts, actor.runtime_paths, change)
    if declined is None or declined.room_id is None or declined.origin_room_id is None:
        return
    await post_in_thread(
        actor,
        declined,
        agent_name,
        declined.origin_room_id,
        declined.origin_thread_id,
        f"{declined.target.id} declined contact {declined.id}; remove it with remove_contact to ask again.",
    )
    await leave_contact_room(actor, declined, declined.room_id)


async def _end(
    actor: ContactActor,
    record: ContactRecord,
    agent_name: str,
    before: ContactStatus,
    *,
    leave: bool,
) -> None:
    """End the contact; with ``leave``, withdraw the owner's unjoined invitation and leave the room too."""
    change = _settle(record.id, before, "removed")
    ended = await run_blocking_until_complete(update_contacts, actor.runtime_paths, change)
    if ended is None or ended.room_id is None or not leave:
        return
    bot = actor.agent_bot(agent_name)
    if bot is not None and bot.client is not None:
        await withdraw_unjoined_owner_invites(bot.client, ended, ended.room_id)
    await leave_contact_room(actor, ended, ended.room_id)


async def reconcile_remote_contacts(
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    agent_bot: Callable[[str], AgentBot | None],
) -> None:
    """Catch up on membership changes of contact rooms with other deployments' agents that no live event delivered.

    A request whose room the other agent joined becomes active, one whose invite it rejected is declined, and an active
    contact whose room the other agent left ends. A membership that cannot be read changes nothing, and neither does an
    invite still pending.
    """
    found = _agent(config, runtime_paths, agent_name, agent_bot)
    bot = agent_bot(agent_name)
    if found is None or bot is None or bot.client is None:
        return
    actor, _party = found
    for record, side in await run_blocking_until_complete(agent_contacts, config, runtime_paths, agent_name):
        other = record.counterparty(side)
        pending_request = record.status == "pending" and side == "requester"
        if other.kind != "remote_agent" or record.room_id is None or not (pending_request or record.status == "active"):
            continue
        try:
            membership = await get_room_membership(bot.client, record.room_id, other.id)
            await _catch_up(actor, record, agent_name, membership, pending_request=pending_request)
        except Exception:
            # Each contact moves only from the status it is in, so the next start retries one whose status did not move.
            logger.exception("remote_contact_reconcile_failed", contact_id=record.id, agent_name=agent_name)


async def _catch_up(
    actor: ContactActor,
    record: ContactRecord,
    agent_name: str,
    membership: str | None,
    *,
    pending_request: bool,
) -> None:
    """Apply the other agent's membership of one contact room, as the room's state reports it."""
    gone = membership in {"leave", "ban"}
    if pending_request and membership == "join":
        await _accepted(actor, record, agent_name)
    elif pending_request and gone:
        await _declined(actor, record, agent_name)
    elif record.status == "active" and gone:
        await _end(actor, record, agent_name, "active", leave=True)
