"""The contact operations: request, accept, decline, update, remove, and list.

It also withdraws the stale invitations to a removed contact's room that an agent leaves at a reload or start.

An agent's contacts tools run every operation, and the Connections page runs accept, decline, update, and remove.
Each runs for a ``ContactActor``: a tool call first checks that the requester owns the agent it runs for, and the
page acts for its signed-in user; each operation then checks that the owner owns a side of the contact.
Every refusal is a ``ValueError`` whose text is meant for whoever acted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

import yaml

from mindroom.background_tasks import run_blocking_until_complete
from mindroom.constants import ORIGINAL_SENDER_KEY, SKIP_MENTIONS_KEY
from mindroom.contacts.parties import (
    agent_party,
    party_label,
    party_matrix_ids,
    party_name,
    person_party,
    personal_room_agent,
    personal_room_of,
    resolve_target,
)
from mindroom.contacts.policy import effective_rules
from mindroom.contacts.store import (
    ContactBook,
    ContactParty,
    ContactRecord,
    ContactSide,
    SideName,
    agent_contacts,
    owner_contacts,
    read_contacts,
    update_contacts,
)
from mindroom.contacts.turns import contact_tool_choices, contact_turn_tools
from mindroom.entity_resolution import current_entity_id
from mindroom.logging_config import bound_log_context, get_logger
from mindroom.matrix.client_delivery import send_message_result
from mindroom.matrix.client_room_admin import create_room, get_room_members, invite_to_room, kick_from_room
from mindroom.matrix.message_builder import build_message_content
from mindroom.requester_identity import resolve_human_requester_alias

if TYPE_CHECKING:
    from collections.abc import Callable, Collection

    import nio

    from mindroom.bot import AgentBot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

logger = get_logger(__name__)

type _Decision = Literal["accept", "decline"]

_SOURCE_HOOK = "native:contacts"
_PERSON_HAS_NO_SIDE = "A person has no rules or tools; only an agent's side can have them"
_NOT_PENDING: dict[_Decision, str] = {
    "accept": "Only a pending contact can be accepted",
    "decline": "Only a pending contact can be declined",
}
# Invite-only like a personal room; a member who joins later still reads the whole conversation.
_CONTACT_ROOM_STATE = [
    {"type": "m.room.join_rules", "state_key": "", "content": {"join_rule": "invite"}},
    {"type": "m.room.history_visibility", "state_key": "", "content": {"history_visibility": "shared"}},
]


@dataclass(frozen=True)
class ContactActor:
    """The owner a contact operation acts for, and the runtime it acts in.

    ``agent_name`` is the owner's agent whose contacts tools run the operation, or None when the owner acts on the
    Connections page. ``agent_bot`` returns the bot of a configured agent, running or not, or None.
    """

    owner: str
    agent_name: str | None
    config: Config
    runtime_paths: RuntimePaths
    agent_bot: Callable[[str], AgentBot | None]


def tool_actor(context: ToolRuntimeContext, agent_name: str) -> ContactActor:
    """Return the owner of ``agent_name`` acting through its contacts tools, refusing anyone else.

    ``agent_name`` must be a personal agent with contacts.
    """
    config = context.current_config
    if config.personal_agent_instance(agent_name) is None:
        msg = f"{agent_name} is not a personal agent; only personal agents can have contacts"
        raise ValueError(msg)
    party = agent_party(config, agent_name)
    if party is None:
        msg = f"contacts are not enabled for {agent_name}"
        raise ValueError(msg)
    if resolve_human_requester_alias(context.requester_id, config, context.runtime_paths) != party.owner:
        msg = f"Only {party.owner} may use the contacts of {agent_name}"
        raise ValueError(msg)
    orchestrator = context.orchestrator
    return ContactActor(
        owner=party.owner,
        agent_name=agent_name,
        config=config,
        runtime_paths=context.runtime_paths,
        agent_bot=(lambda _name: None) if orchestrator is None else orchestrator.agent_bots.get,
    )


def _running_bot(actor: ContactActor, agent_name: str) -> tuple[AgentBot, nio.AsyncClient] | None:
    """Return a running agent's bot and client, or None when it is not running."""
    bot = actor.agent_bot(agent_name)
    if bot is None or not bot.running or bot.client is None:
        return None
    return bot, bot.client


def _validate_side_tools(
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    tools: list[str] | None,
) -> list[str] | None:
    """Return the tools one side of a contact offers, refusing any the agent lacks or a contact may never offer."""
    if tools is None:
        return None
    allowed = contact_tool_choices(config, runtime_paths, agent_name)
    if outside := [name for name in tools if name not in allowed]:
        msg = f"Not among the tools a contact may use: {', '.join(outside)}; allowed: {', '.join(allowed)}"
        raise ValueError(msg)
    return tools


def _side_terms(config: Config, runtime_paths: RuntimePaths, record: ContactRecord, side: SideName) -> tuple[str, str]:
    """Return the rules and the tools of one agent side of a contact, as its owner reads them."""
    agent_name = record.party(side).id
    rules = effective_rules(config, agent_name, record.side(side)) or "None."
    return rules, ", ".join(contact_turn_tools(config, runtime_paths, agent_name, record, side))


def own_side_terms(actor: ContactActor, record: ContactRecord) -> str:
    """Return the rules and tools the owner's agent has in a contact, or nothing when the owner's side is a person.

    An approval card shows only the call's arguments, and a call that leaves them out applies the configured defaults.
    """
    side = record.owner_side(actor.owner)
    if side is None or (party := record.party(side)).kind != "agent":
        return ""
    rules, tools = _side_terms(actor.config, actor.runtime_paths, record, side)
    return (
        f"\nRules {party.id} follows in this contact:\n{rules}\n"
        f"Tools it may use there: {tools}. Change them with update_contact."
    )


def _notice_body(config: Config, runtime_paths: RuntimePaths, record: ContactRecord) -> str:
    """Return the request as the target owner reads it, with the rules and tools that would apply to their side."""
    target = record.target
    asks = "you" if target.kind == "person" else f"your agent {party_name(config, target)} ({target.id})"
    lines = [f"{party_label(config, record.requester)} asks to contact {asks}.", "", f"Reason: {record.reason}", ""]
    # A person's side has no rules to set.
    answers = "accept or decline"
    if target.kind == "agent":
        rules, tools = _side_terms(config, runtime_paths, record, "target")
        lines += [
            "Rules that would apply to your agent by default:",
            rules,
            f"Tools it would use by default: {tools}",
            "",
        ]
        answers = "accept, decline, or set rules"
    lines += [f"Contact ID: {record.id}", f"Reply in this thread to {answers}."]
    return "\n".join(lines)


def _by_id(book: ContactBook, contact_id: str) -> ContactRecord:
    """Return a contact already found in the store; contacts are never deleted, only given a status."""
    return next(record for record in book.contacts if record.id == contact_id)


def _request_poster(config: Config, runtime_paths: RuntimePaths, owner: str) -> tuple[str, str]:
    """Return the agent and room of the owner's personal room, where requests to the owner are posted and accepted.

    That agent and not the target agent asks, because a target agent of another template is not in that room.
    """
    if (personal := personal_room_of(config, runtime_paths, owner)) is None:
        msg = f"{owner} has no personal room yet, so the request cannot reach them; ask again once they have one"
        raise ValueError(msg)
    return personal


async def _post_request_notice(actor: ContactActor, record: ContactRecord) -> ContactRecord:
    """Post the request as a new thread in the target owner's personal room and store its event ID.

    Return the contact as stored, notice event ID included.
    """
    config, paths = actor.config, actor.runtime_paths
    poster, room_id = _request_poster(config, paths, record.target.owner)
    if (running := _running_bot(actor, poster)) is None:
        msg = f"{poster} is not running, so it cannot post the request; ask again later"
        raise ValueError(msg)
    _, client = running
    content = build_message_content(
        _notice_body(config, paths, record),
        extra_content={"msgtype": "m.notice", SKIP_MENTIONS_KEY: True},
    )
    delivered = await send_message_result(client, room_id, content, transaction_id=f"contact-request-{record.id}")
    if delivered is None:
        msg = f"The request to {record.target.id} could not be posted to its owner's personal room; ask again to retry"
        raise ValueError(msg)
    event_id = delivered.event_id

    def set_notice(book: ContactBook) -> ContactRecord:
        stored = _by_id(book, record.id)
        stored.notice_event_id = event_id
        return stored.model_copy(deep=True)

    return await run_blocking_until_complete(update_contacts, paths, set_notice)


def _owned(book: ContactBook, contact_id: str, owner: str) -> tuple[ContactRecord, SideName]:
    """Return the contact and the side ``owner`` holds; a contact of two other owners looks like no contact."""
    for record in book.contacts:
        if record.id == contact_id and (side := record.owner_side(owner)) is not None:
            return record, side
    msg = f"There is no contact {contact_id}"
    raise ValueError(msg)


def _change_side(side: ContactSide, rules: str | None, tools: list[str] | None) -> None:
    """Set the rules and checked tools given; a field left None stays, and blank rules clear."""
    if rules is not None:
        side.rules = rules.strip() or None
    if tools is not None:
        side.tools = tools


def _check_pair(requester: ContactParty, target: ContactParty) -> None:
    """Refuse a contact between two people, or between parties of one owner."""
    if requester.kind == "person" and target.kind == "person":
        msg = "A contact needs an agent on one side"
        raise ValueError(msg)
    if requester.owner == target.owner:
        msg = "A contact needs two different owners"
        raise ValueError(msg)


def _existing_contact(
    book: ContactBook,
    requester: ContactParty,
    target: ContactParty,
    *,
    rules: str | None,
    tools: list[str] | None,
) -> ContactRecord | None:
    """Return the pending or active contact of this pair, refusing a declined pair and a request that changes a side.

    Rules or tools equal to the requester's stored side make a repeat, as when the first request's notice failed.
    """
    pair = {(party.kind, party.id) for party in (requester, target)}
    for existing in book.contacts:
        ends = {(party.kind, party.id) for party in (existing.requester, existing.target)}
        if existing.status == "removed" or ends != pair:
            continue
        if existing.status == "declined":
            msg = f"{existing.target.owner} declined this contact; they can remove it to allow a new request"
            raise ValueError(msg)
        stored = existing.side("requester" if existing.requester.id == requester.id else "target")
        asked = stored.model_copy(deep=True)
        _change_side(asked, rules, tools)
        if asked != stored:
            msg = (
                f"Contact {existing.id} with {target.id} already exists and is {existing.status}; "
                "change your side's rules or tools with update_contact"
            )
            raise ValueError(msg)
        return existing
    return None


async def request_contact(
    context: ToolRuntimeContext,
    agent_name: str,
    *,
    target: str,
    reason: str,
    rules: str | None,
    tools: list[str] | None,
    party: Literal["agent", "me"],
) -> tuple[ContactActor, ContactRecord, bool]:
    """Record a contact request from ``agent_name`` or its owner, post it to the target owner, and return it.

    It also returns the owner it acted for, and a flag that says whether this call posted a request notice. A pending or active contact between the same two parties,
    in either direction, is returned instead of a new one; a pending one's request notice is posted when it never landed,
    and a repeat by its requester then replaces its reason and the room and thread its answer goes to. Asking again with other
    rules or tools is refused, because only ``update_contact`` changes them.
    """
    actor = tool_actor(context, agent_name)
    config, paths = actor.config, actor.runtime_paths
    own = ContactParty(kind="agent", id=agent_name, owner=actor.owner)
    target_party = resolve_target(config, paths, target)
    if not reason.strip():
        msg = "A reason is required, so the other owner knows why you ask"
        raise ValueError(msg)
    if party == "me":
        if rules is not None or tools is not None:
            raise ValueError(_PERSON_HAS_NO_SIDE)
        requester, side = person_party(config, paths, actor.owner), ContactSide()
    else:
        requester, side = own, ContactSide()
        _change_side(side, rules, _validate_side_tools(config, paths, agent_name, tools))
    _check_pair(requester, target_party)
    # The agent of the owner's personal room asks them and accepts for them, which takes the contacts tools.
    poster = personal_room_agent(config, target_party.owner)
    if poster is None:
        msg = f"{target_party.owner} has no personal agent here, so a request cannot reach them"
        raise ValueError(msg)
    if agent_party(config, poster) is None:
        msg = f"{target_party.owner}'s personal agent {poster} does not take contact requests"
        raise ValueError(msg)

    def create(book: ContactBook) -> ContactRecord:
        existing = _existing_contact(book, requester, target_party, rules=rules, tools=tools)
        if existing is not None:
            # Nobody has read a request whose notice never landed, so its requester's new reason replaces the old one,
            # and the thread asking now is the one the answer goes to.
            if existing.status == "pending" and existing.notice_event_id is None and existing.requester == requester:
                existing.reason = reason.strip()
                existing.origin_room_id = context.room_id
                existing.origin_thread_id = context.resolved_thread_id
            return existing.model_copy(deep=True)
        record = ContactRecord(
            id=uuid4().hex[:12],
            status="pending",
            requester=requester,
            target=target_party,
            reason=reason.strip(),
            requester_side=side,
            target_side=ContactSide(),
            origin_room_id=context.room_id,
            origin_thread_id=context.resolved_thread_id,
            requested_at=datetime.now(UTC),
        )
        book.contacts.append(record)
        return record.model_copy(deep=True)

    record = await run_blocking_until_complete(update_contacts, paths, create)
    if record.status == "pending" and record.notice_event_id is None:
        return actor, await _post_request_notice(actor, record), True
    return actor, record, False


async def _post_in_thread(
    actor: ContactActor,
    record: ContactRecord,
    agent_name: str,
    room_id: str,
    thread_id: str | None,
    body: str,
) -> None:
    """Post a notice in one thread as one agent; one that fails is logged, as the decision it reports stands.

    The notice mentions nobody, so a Matrix ID it names stays plain text and pings no one.
    """
    bot = actor.agent_bot(agent_name)
    sent = None
    if bot is not None and bot.client is not None:
        latest_thread_event_id = await bot.conversation_reader.latest_thread_event_id(
            room_id=room_id,
            thread_id=thread_id,
        )
        content = build_message_content(
            body,
            thread_event_id=thread_id,
            latest_thread_event_id=latest_thread_event_id,
            extra_content={SKIP_MENTIONS_KEY: True, "m.mentions": {}},
        )
        sent = await send_message_result(bot.client, room_id, content)
    if sent is None:
        logger.warning("contact_thread_post_failed", contact_id=record.id, agent_name=agent_name, room_id=room_id)


async def _answer_request_thread(
    actor: ContactActor,
    record: ContactRecord,
    personal: tuple[str, str],
    body: str,
) -> None:
    """Answer the request's thread in the owner's personal room, as its agent, after a decision made on the page.

    A decision through a tool is answered in the conversation that made it, and a request whose notice never landed
    has no thread.
    """
    if actor.agent_name is not None or record.notice_event_id is None:
        return
    poster, room_id = personal
    await _post_in_thread(actor, record, poster, room_id, record.notice_event_id, body)


def _room_creator(record: ContactRecord) -> str:
    """Return the agent party that creates the contact room: the target agent, or else the requesting agent.

    The creator stays in the room, so it must be a party; a contact between two people does not exist.
    """
    return record.target.id if record.target.kind == "agent" else record.requester.id


def _running_agents(actor: ContactActor, record: ContactRecord) -> dict[str, tuple[AgentBot, nio.AsyncClient]]:
    """Return the bot and client of each agent party, refusing before anything changes when one is not running.

    An agent party that lost its contacts since the request is refused too, because its contacts read as removed.
    """
    agents: dict[str, tuple[AgentBot, nio.AsyncClient]] = {}
    for party in (record.requester, record.target):
        if party.kind != "agent":
            continue
        if agent_party(actor.config, party.id) is None:
            msg = f"contacts are not enabled for {party.id}, so the contact room cannot be set up"
            raise ValueError(msg)
        if (running := _running_bot(actor, party.id)) is None:
            msg = f"{party.id} is not running, so the contact room cannot be set up; accept again later"
            raise ValueError(msg)
        agents[party.id] = running
    return agents


async def _seat_parties(
    actor: ContactActor,
    record: ContactRecord,
    room_id: str,
    agents: dict[str, tuple[AgentBot, nio.AsyncClient]],
    invited: list[str],
) -> None:
    """Invite everyone who speaks for either party, then make each agent party join the room.

    Each user goes into ``invited`` before being asked, so a setup that stops, cancelled mid-request included, knows
    whose invitation may stand.
    """
    paths = actor.runtime_paths
    creator = _room_creator(record)
    _, client = agents[creator]
    speakers = party_matrix_ids(record.requester, paths) | party_matrix_ids(record.target, paths)
    for user_id in sorted(speakers - {current_entity_id(creator, paths).full_id}):
        invited.append(user_id)
        if not await invite_to_room(client, room_id, user_id):
            msg = f"{user_id} could not be invited to the contact room; accept again to retry"
            raise ValueError(msg)
    # The creator joins too, so its runtime knows the room and can send there at once.
    for name, (bot, _) in agents.items():
        if not await bot.change_local_membership(room_id, "join"):
            msg = f"{name} could not join the contact room; accept again to retry"
            raise ValueError(msg)


async def _withdraw_owner_invites(
    client: nio.AsyncClient,
    record: ContactRecord,
    room_id: str,
    invited: list[str],
) -> None:
    """Withdraw the owners' invitations among ``invited`` to a contact room its agents leave, so none leads to a dead room.

    Only the creating agent, which is still in the room, can withdraw them; invited agents leave on their own.
    A withdrawal that fails or raises is only logged, so the agents still leave.
    """
    owners = {record.requester.owner, record.target.owner}
    for user_id in invited:
        if user_id not in owners:
            continue
        try:
            await kick_from_room(client, room_id, user_id)
        except Exception:
            logger.exception(
                "contact_room_invite_withdrawal_failed",
                contact_id=record.id,
                user_id=user_id,
                room_id=room_id,
            )


async def _withdraw_unjoined_owner_invites(client: nio.AsyncClient, record: ContactRecord, room_id: str) -> None:
    """Withdraw the invitations of the owners who never joined a removed contact's room, as its creating agent.

    Once its agents leave, such an invitation leads to a room nobody is left to let them into, while owners who joined
    keep the room and its history. The member list is read again just before each owner's withdrawal, so an owner who
    joins while the other's invitation is withdrawn stays; one who joins between that read and the withdrawal does not.
    A member list that cannot be read withdraws nothing for that owner.
    """
    for owner in (record.requester.owner, record.target.owner):
        joined = await get_room_members(client, room_id)
        if joined is not None and owner not in joined:
            await _withdraw_owner_invites(client, record, room_id, [owner])


async def withdraw_removed_contact_invites(
    client: nio.AsyncClient,
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    leaving: Collection[str],
) -> None:
    """Withdraw unjoined owners' invitations to the removed contacts' rooms this agent created and is about to leave.

    An agent leaves such a room on a reload that takes its contacts away or removes the agent, and at its next start
    when the removal did not make it leave, as when the other agent lost its contacts.
    """
    for record, _side in await run_blocking_until_complete(agent_contacts, config, runtime_paths, agent_name):
        room_id = record.room_id
        if (
            room_id is not None
            and room_id in leaving
            and record.status == "removed"
            and _room_creator(record) == agent_name
        ):
            await _withdraw_unjoined_owner_invites(client, record, room_id)


async def _leave_contact_room(actor: ContactActor, record: ContactRecord, room_id: str) -> None:
    """Make the running agents of a contact leave its room.

    Each agent leaves even when another's leave raises, which is only logged, so the caller's own outcome stands.
    """
    for party in (record.requester, record.target):
        if party.kind != "agent" or (bot := actor.agent_bot(party.id)) is None or not bot.running:
            continue
        try:
            await bot.change_local_membership(room_id, "leave")
        except Exception:
            logger.exception("contact_room_leave_failed", contact_id=record.id, agent_name=party.id, room_id=room_id)


async def _announce_acceptance(actor: ContactActor, record: ContactRecord, agent_name: str) -> None:
    """Start the requesting agent's turn in the thread it asked from, as its owner, so it continues its task.

    A person who asked has no agent to continue, so the accepting agent tells their thread, when it is in that room.
    A post that fails is logged, because the acceptance stands.
    """
    # A display name is the other owner's to choose, so a message the requesting agent acts on names the ID.
    target = record.target.id
    requester = record.requester
    if requester.kind == "agent":
        bot = actor.agent_bot(requester.id)
        kickoff = None
        if bot is not None:
            # Its logs name the requester's thread it goes to, not the accepting turn's.
            with bound_log_context(room_id=record.origin_room_id, thread_id=record.origin_thread_id):
                kickoff = await bot.hook_context.send_message(
                    record.origin_room_id,
                    # Only a mentioned agent answers a message in a room with other responders.
                    f"@{requester.id} {target} accepted your contact request. "
                    f"Continue the task in the contact room {record.room_id}: {record.reason}",
                    record.origin_thread_id,
                    _SOURCE_HOOK,
                    {ORIGINAL_SENDER_KEY: requester.owner},
                    trigger_dispatch=True,
                )
        if kickoff is None:
            logger.warning("contact_origin_post_failed", contact_id=record.id, agent_name=requester.id)
        return
    bot = actor.agent_bot(agent_name)
    joined = None if bot is None else await bot.current_joined_room_ids()
    if joined is None or record.origin_room_id not in joined:
        logger.info("contact_acceptance_notice_skipped", contact_id=record.id, agent_name=agent_name)
        return
    await _post_in_thread(
        actor,
        record,
        agent_name,
        record.origin_room_id,
        record.origin_thread_id,
        f"{target} accepted your contact request. The contact room is {record.room_id}.",
    )


async def _pending_request(
    actor: ContactActor,
    contact_id: str,
    decision: _Decision,
) -> tuple[ContactRecord, tuple[str, str]]:
    """Return a pending request to the owner, and the agent and room of their personal room, where it is answered.

    The request asked the owner in their personal room, so through a tool only that agent accepts or declines it.
    """
    book = await run_blocking_until_complete(read_contacts, actor.runtime_paths)
    record, side = _owned(book, contact_id, actor.owner)
    if side != "target":
        msg = f"Only {record.target.owner} may {decision} this contact"
        raise ValueError(msg)
    if record.status != "pending":
        raise ValueError(_NOT_PENDING[decision])
    if (personal := personal_room_of(actor.config, actor.runtime_paths, actor.owner)) is None:
        msg = f"{actor.owner} has no personal room to answer this request in"
        raise ValueError(msg)
    poster, _ = personal
    if actor.agent_name not in {None, poster}:
        msg = f"Only {poster}, the agent of {actor.owner}'s personal room, may {decision} this contact"
        raise ValueError(msg)
    return record, personal


async def accept_contact(
    actor: ContactActor,
    contact_id: str,
    *,
    rules: str | None,
    tools: list[str] | None,
) -> ContactRecord:
    """Accept a pending request for its target owner: open the contact room, seat both parties, and tell the requester.

    The target side keeps any rules or tools left out. Both agent parties keep the room while the contact is active.
    The pending contact names the room before anyone is invited, so the room refuses every turn until it is active.
    A room that cannot be fully set up is left again, with the owners' invitations withdrawn, and the request stays
    pending, to be replaced by the next room; the room it replaces stays a removed contact's, because an agent whose
    leave failed may still be in it.
    """
    config, paths = actor.config, actor.runtime_paths
    record, personal = await _pending_request(actor, contact_id, "accept")
    target = record.target
    if target.kind == "person" and (rules is not None or tools is not None):
        raise ValueError(_PERSON_HAS_NO_SIDE)
    # It refuses an agent without contacts first, so the tools are checked only against a configured agent.
    agents = _running_agents(actor, record)
    checked = _validate_side_tools(config, paths, target.id, tools)
    _, client = agents[_room_creator(record)]
    room_name = f"{party_name(config, record.requester)} ↔ {party_name(config, target)}"
    room_id = await create_room(client, room_name, initial_state=_CONTACT_ROOM_STATE)
    if room_id is None:
        msg = "The contact room could not be created; accept again to retry"
        raise ValueError(msg)

    def register(book: ContactBook) -> None:
        current = _by_id(book, contact_id)
        if current.status != "pending":
            raise ValueError(_NOT_PENDING["accept"])
        if current.room_id is not None:
            # A removed contact's room starts no turns and takes no messages, and its agents leave it.
            book.contacts.append(current.model_copy(update={"id": uuid4().hex[:12], "status": "removed"}, deep=True))
        current.room_id = room_id

    def activate(book: ContactBook) -> ContactRecord:
        current = _by_id(book, contact_id)
        # Another acceptance that registered its own room meanwhile wins, and this one leaves its room.
        if current.status != "pending" or current.room_id != room_id:
            raise ValueError(_NOT_PENDING["accept"])
        current.status = "active"
        current.decided_at = datetime.now(UTC)
        _change_side(current.target_side, rules, checked)
        return current.model_copy(deep=True)

    invited: list[str] = []
    try:
        await run_blocking_until_complete(update_contacts, paths, register)
        await _seat_parties(actor, record, room_id, agents, invited)
        accepted = await run_blocking_until_complete(update_contacts, paths, activate)
    # Any failure, a cancellation included, leaves the room unless the store already made it the active contact's:
    # the store write finishes before a cancellation propagates, and agents never rejoin a contact room on their own.
    except BaseException:
        current = _by_id(await run_blocking_until_complete(read_contacts, paths), contact_id)
        if current.status != "active" or current.room_id != room_id:
            await _withdraw_owner_invites(client, record, room_id, invited)
            await _leave_contact_room(actor, record, room_id)
        raise
    await _announce_acceptance(actor, accepted, personal[0])
    await _answer_request_thread(
        actor,
        accepted,
        personal,
        f"Accepted on the Connections page. The contact room is {accepted.room_id}.",
    )
    return accepted


async def decline_contact(actor: ContactActor, contact_id: str) -> ContactRecord:
    """Decline a pending request for its target owner and tell the thread it came from."""
    _, personal = await _pending_request(actor, contact_id, "decline")

    def decline(book: ContactBook) -> ContactRecord:
        record = _by_id(book, contact_id)
        if record.status != "pending":
            raise ValueError(_NOT_PENDING["decline"])
        record.status = "declined"
        record.decided_at = datetime.now(UTC)
        return record.model_copy(deep=True)

    record = await run_blocking_until_complete(update_contacts, actor.runtime_paths, decline)
    # A person who asked has no agent to tell.
    if record.requester.kind == "agent":
        await _post_in_thread(
            actor,
            record,
            record.requester.id,
            record.origin_room_id,
            record.origin_thread_id,
            f"{record.target.owner} declined contact {record.id} with {record.target.id}; "
            "they must remove it before you can ask again.",
        )
    await _answer_request_thread(actor, record, personal, "Declined on the Connections page.")
    return record


async def update_contact(
    actor: ContactActor,
    contact_id: str,
    *,
    rules: str | None,
    tools: list[str] | None,
) -> ContactRecord:
    """Change the rules or tools of the owner's own side of a contact; a field left None stays, and blank rules clear.

    The side's agent must still have contacts, since it may be another agent than the one whose tools ask.
    """
    if rules is None and tools is None:
        msg = "Nothing to change: give rules, tools, or both"
        raise ValueError(msg)
    config, paths = actor.config, actor.runtime_paths

    def update(book: ContactBook) -> ContactRecord:
        record, side = _owned(book, contact_id, actor.owner)
        if record.status in {"declined", "removed"}:
            msg = f"A {record.status} contact cannot be changed"
            raise ValueError(msg)
        if (party := record.party(side)).kind != "agent":
            raise ValueError(_PERSON_HAS_NO_SIDE)
        if agent_party(config, party.id) is None:
            msg = f"contacts are not enabled for {party.id}"
            raise ValueError(msg)
        _change_side(record.side(side), rules, _validate_side_tools(config, paths, party.id, tools))
        return record.model_copy(deep=True)

    return await run_blocking_until_complete(update_contacts, paths, update)


async def remove_contact(actor: ContactActor, contact_id: str) -> ContactRecord:
    """Remove a contact for either owner, except that only the owner who declined a contact may remove it.

    The contact's agents leave its room and stop keeping it across restarts, its creating agent first withdrawing the
    invitations of owners who never joined; a creator that is not running does so when it leaves at its next start.
    """

    def remove(book: ContactBook) -> ContactRecord:
        record, side = _owned(book, contact_id, actor.owner)
        if record.status == "removed":
            msg = "The contact is already removed"
            raise ValueError(msg)
        # A declined contact blocks new requests until the owner who declined it removes it.
        if record.status == "declined" and side != "target":
            msg = f"Only {record.target.owner} may remove a declined contact"
            raise ValueError(msg)
        record.status = "removed"
        return record.model_copy(deep=True)

    record = await run_blocking_until_complete(update_contacts, actor.runtime_paths, remove)
    if record.room_id is not None:
        if (running := _running_bot(actor, _room_creator(record))) is not None:
            _, client = running
            await _withdraw_unjoined_owner_invites(client, record, record.room_id)
        await _leave_contact_room(actor, record, record.room_id)
    return record


def _listed(config: Config, runtime_paths: RuntimePaths, record: ContactRecord, side: SideName) -> dict[str, object]:
    """Return one contact as its owner's listing shows it, with an active contact's room and their side's terms.

    A person's side has no rules or tools, and neither has an agent no longer configured.
    """
    party = record.party(side)
    configured = party.kind == "agent" and party.id in config.agents
    rules, tools = _side_terms(config, runtime_paths, record, side) if configured else (None, None)
    return {
        "id": record.id,
        "status": record.status,
        "room_id": record.room_id if record.status == "active" else None,
        "you": side,
        "with": record.counterparty(side).model_dump(mode="json"),
        "reason": record.reason,
        "your_rules": rules,
        "your_tools": tools,
    }


def list_contacts(context: ToolRuntimeContext, agent_name: str) -> str:
    """Return the caller-owner's live contacts with their own side, and the personal agents they can ask, as YAML."""
    actor = tool_actor(context, agent_name)
    config, paths = actor.config, actor.runtime_paths
    contacts = [_listed(config, paths, record, side) for record, side in owner_contacts(paths, actor.owner)]
    contactable = [
        {"agent": name, "owner": party.owner}
        for name in config.agents
        if (party := agent_party(config, name)) is not None and party.owner != actor.owner
    ]
    return yaml.safe_dump({"contacts": contacts, "contactable_agents": contactable}, sort_keys=False)
