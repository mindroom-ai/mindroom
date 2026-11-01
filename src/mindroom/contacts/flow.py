"""The contact operations an agent's tools run: request, accept, decline, update, remove, and list.

Each operation first checks that the requester owns the agent it runs for, then that they own a side of the contact.
Every refusal is a ``ValueError`` whose text is meant for the agent that called.
"""

from __future__ import annotations

import time
from functools import partial
from typing import TYPE_CHECKING, Any, Literal
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
    read_contacts,
    update_contacts,
)
from mindroom.contacts.turns import contact_tool_choices
from mindroom.entity_resolution import current_entity_id
from mindroom.logging_config import get_logger
from mindroom.matrix.client_delivery import send_message_result
from mindroom.matrix.client_room_admin import create_room, invite_to_room
from mindroom.matrix.message_builder import build_message_content
from mindroom.requester_identity import resolve_human_requester_alias

if TYPE_CHECKING:
    import nio

    from mindroom.bot import AgentBot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.contacts.store import SideName
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

logger = get_logger(__name__)

_CONTACT_REQUEST_KEY = "io.mindroom.contact_request"
_SOURCE_HOOK = "native:contacts"
_PERSON_HAS_NO_SIDE = "A person has no rules or tools; only an agent's side can have them"
_NOT_PENDING = "Only a pending contact can be accepted"
# Invite-only like a personal room; a member who joins later still reads the whole conversation.
_CONTACT_ROOM_STATE = [
    {"type": "m.room.join_rules", "state_key": "", "content": {"join_rule": "invite"}},
    {"type": "m.room.history_visibility", "state_key": "", "content": {"history_visibility": "shared"}},
]


def _own_party(context: ToolRuntimeContext, agent_name: str) -> ContactParty:
    """Return the party of ``agent_name`` when it is a personal agent with contacts that the requester owns."""
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
    return party


def _validate_side_tools(config: Config, agent_name: str, tools: list[str] | None) -> list[str] | None:
    """Return the tools one side of a contact offers, refusing any the agent lacks or a contact may never offer."""
    if tools is None:
        return None
    allowed = contact_tool_choices(config, agent_name)
    if outside := [name for name in tools if name not in allowed]:
        msg = f"Not among the tools a contact may use: {', '.join(outside)}; allowed: {', '.join(allowed)}"
        raise ValueError(msg)
    return list(tools)


def _same_pair(record: ContactRecord, first: ContactParty, second: ContactParty) -> bool:
    ends = {(party.kind, party.id) for party in (record.requester, record.target)}
    return ends == {(first.kind, first.id), (second.kind, second.id)}


def _notice_body(config: Config, record: ContactRecord) -> str:
    """Return the request as the target owner reads it, with the rules that would apply to their side."""
    target = record.target
    asks = "you" if target.kind == "person" else f"your agent {party_name(config, target)} ({target.id})"
    lines = [f"{party_label(config, record.requester)} asks to contact {asks}.", "", f"Reason: {record.reason}", ""]
    if target.kind == "agent":
        rules = effective_rules(config, target.id, record.target_side)
        lines += ["Rules that would apply to your agent by default:", rules or "None.", ""]
    lines += [f"Contact ID: {record.id}", "Reply in this thread to accept, decline, or set rules."]
    return "\n".join(lines)


def _by_id(book: ContactBook, contact_id: str) -> ContactRecord:
    """Return a contact this process just stored; contacts are never deleted, only given a status."""
    return next(record for record in book.contacts if record.id == contact_id)


def _set_notice(contact_id: str, event_id: str, book: ContactBook) -> None:
    _by_id(book, contact_id).notice_event_id = event_id


def _request_poster(config: Config, runtime_paths: RuntimePaths, owner: str) -> tuple[str, str]:
    """Return the agent and room of the owner's personal room, where requests to the owner are posted and accepted.

    That agent and not the target agent asks, because a target agent of another template is not in that room.
    """
    if (personal := personal_room_of(config, runtime_paths, owner)) is None:
        msg = f"{owner} has no personal room yet, so the request cannot reach them; ask again once they have one"
        raise ValueError(msg)
    return personal


async def _post_request_notice(context: ToolRuntimeContext, record: ContactRecord) -> None:
    """Post the request as a new thread in the target owner's personal room and store its event ID."""
    config, paths = context.current_config, context.runtime_paths
    poster, room_id = _request_poster(config, paths, record.target.owner)
    client = None if context.orchestrator is None else context.orchestrator.running_entity_client(poster)
    if client is None:
        msg = f"{poster} is not running, so it cannot post the request; ask again later"
        raise ValueError(msg)
    content = build_message_content(
        _notice_body(config, record),
        extra_content={"msgtype": "m.notice", SKIP_MENTIONS_KEY: True, _CONTACT_REQUEST_KEY: record.id},
    )
    delivered = await send_message_result(client, room_id, content, transaction_id=f"contact-request-{record.id}")
    if delivered is None:
        msg = f"The request to {record.target.id} could not be posted to its owner's personal room; ask again to retry"
        raise ValueError(msg)
    await run_blocking_until_complete(
        update_contacts,
        paths,
        partial(_set_notice, record.id, delivered.event_id),
    )


def _owned(book: ContactBook, contact_id: str, owner: str) -> tuple[ContactRecord, SideName]:
    """Return the contact and the side ``owner`` holds; a contact of two other owners looks like no contact."""
    for record in book.contacts:
        if record.id == contact_id and (side := record.owner_side(owner)) is not None:
            return record, side
    msg = f"There is no contact {contact_id}"
    raise ValueError(msg)


def _reload(context: ToolRuntimeContext, contact_id: str) -> ContactRecord:
    return _by_id(read_contacts(context.runtime_paths), contact_id).model_copy(deep=True)


def _clean_rules(rules: str | None) -> str | None:
    return None if rules is None else rules.strip() or None


def _change_side(side: ContactSide, rules: str | None, tools: list[str] | None) -> None:
    """Set the rules and checked tools given; a field left None stays, and blank rules clear."""
    if rules is not None:
        side.rules = _clean_rules(rules)
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


async def request_contact(
    context: ToolRuntimeContext,
    agent_name: str,
    *,
    target: str,
    reason: str,
    rules: str | None,
    tools: list[str] | None,
    party: Literal["agent", "me"],
) -> ContactRecord:
    """Record a contact request from ``agent_name`` or its owner and post it to the target owner.

    A pending or active contact between the same two parties, in either direction, is returned instead, and its
    request notice is posted again when it never landed.
    """
    config, paths = context.current_config, context.runtime_paths
    own = _own_party(context, agent_name)
    target_party = resolve_target(config, paths, target)
    if not reason.strip():
        msg = "A reason is required, so the other owner knows why you ask"
        raise ValueError(msg)
    if party == "me":
        if rules is not None or tools is not None:
            raise ValueError(_PERSON_HAS_NO_SIDE)
        requester, side = person_party(config, paths, context.requester_id), ContactSide()
    else:
        requester = own
        side = ContactSide(rules=_clean_rules(rules), tools=_validate_side_tools(config, agent_name, tools))
    _check_pair(requester, target_party)
    # The agent of the owner's personal room asks them and accepts for them, which takes the contacts tools.
    poster = personal_room_agent(config, target_party.owner)
    if poster is not None and agent_party(config, poster) is None:
        msg = f"{target_party.owner}'s personal agent {poster} does not take contact requests"
        raise ValueError(msg)

    def create(book: ContactBook) -> ContactRecord:
        for existing in book.contacts:
            if existing.status == "removed" or not _same_pair(existing, requester, target_party):
                continue
            if existing.status == "declined":
                msg = f"{existing.target.owner} declined this contact; they can remove it to allow a new request"
                raise ValueError(msg)
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
            created_at=time.time(),
        )
        book.contacts.append(record)
        return record.model_copy(deep=True)

    record = await run_blocking_until_complete(update_contacts, paths, create)
    if record.status == "pending" and record.notice_event_id is None:
        await _post_request_notice(context, record)
        return _reload(context, record.id)
    return record


def _bot(context: ToolRuntimeContext, agent_name: str) -> AgentBot | None:
    return None if context.orchestrator is None else context.orchestrator.agent_bots.get(agent_name)


async def _post_to_origin(
    context: ToolRuntimeContext,
    record: ContactRecord,
    agent_name: str,
    body: str,
    extra_content: dict[str, Any],
    *,
    trigger_dispatch: bool = False,
) -> None:
    """Post in the thread a request came from as one agent; a post that fails is logged, because the decision stands."""
    bot = _bot(context, agent_name)
    sent = None
    if bot is not None:
        sent = await bot.hook_context.send_message(
            record.origin_room_id,
            body,
            record.origin_thread_id,
            _SOURCE_HOOK,
            extra_content,
            trigger_dispatch=trigger_dispatch,
        )
    if sent is None:
        logger.warning("contact_origin_post_failed", contact_id=record.id, agent_name=agent_name)


def _room_creator(record: ContactRecord) -> str:
    """Return the agent party that creates the contact room: the target agent, or else the requesting agent.

    The creator stays in the room, so it must be a party; a contact between two people does not exist.
    """
    return record.target.id if record.target.kind == "agent" else record.requester.id


def _running_agents(context: ToolRuntimeContext, record: ContactRecord) -> dict[str, tuple[AgentBot, nio.AsyncClient]]:
    """Return the bot and client of each agent party, refusing before anything changes when one is not running."""
    agents: dict[str, tuple[AgentBot, nio.AsyncClient]] = {}
    for party in (record.requester, record.target):
        if party.kind != "agent":
            continue
        bot = _bot(context, party.id)
        client = None if bot is None or not bot.running else bot.client
        if bot is None or client is None:
            msg = f"{party.id} is not running, so the contact room cannot be set up; accept again later"
            raise ValueError(msg)
        agents[party.id] = (bot, client)
    return agents


async def _seat_parties(
    context: ToolRuntimeContext,
    record: ContactRecord,
    room_id: str,
    agents: dict[str, tuple[AgentBot, nio.AsyncClient]],
) -> None:
    """Invite everyone who speaks for either party, then make each agent party join the room."""
    paths = context.runtime_paths
    creator = _room_creator(record)
    _, client = agents[creator]
    speakers = party_matrix_ids(record.requester, paths) | party_matrix_ids(record.target, paths)
    for user_id in sorted(speakers - {current_entity_id(creator, paths).full_id}):
        if not await invite_to_room(client, room_id, user_id):
            msg = f"{user_id} could not be invited to the contact room; accept again to retry"
            raise ValueError(msg)
    # The creator joins too, so its runtime knows the room and can send there at once.
    for name, (bot, _) in agents.items():
        if not await bot.change_local_membership(room_id, "join"):
            msg = f"{name} could not join the contact room; accept again to retry"
            raise ValueError(msg)


async def _leave_contact_room(context: ToolRuntimeContext, record: ContactRecord, room_id: str) -> None:
    """Make the running agents of a contact leave its room; they keep only the rooms of active contacts across restarts."""
    for party in (record.requester, record.target):
        if party.kind == "agent" and (bot := _bot(context, party.id)) is not None and bot.running:
            await bot.change_local_membership(room_id, "leave")


async def _announce_acceptance(context: ToolRuntimeContext, record: ContactRecord, poster: str) -> None:
    """Start the requesting agent's turn in the thread it asked from, as its owner, so it continues its task.

    A person who asked has no agent to continue, so the accepting agent tells their thread, when it is in that room.
    """
    # A display name is the other owner's to choose, so a message the requesting agent acts on names the ID.
    target = record.target.id
    requester = record.requester
    if requester.kind == "agent":
        await _post_to_origin(
            context,
            record,
            requester.id,
            # Only a mentioned agent answers a message in a room with other responders.
            f"@{requester.id} {target} accepted your contact request. "
            f"Continue the task in the contact room {record.room_id}: {record.reason}",
            {ORIGINAL_SENDER_KEY: requester.owner},
            trigger_dispatch=True,
        )
        return
    bot = _bot(context, poster)
    joined = None if bot is None else await bot.current_joined_room_ids()
    if joined is None or record.origin_room_id not in joined:
        logger.info("contact_acceptance_notice_skipped", contact_id=record.id, agent_name=poster)
        return
    await _post_to_origin(
        context,
        record,
        poster,
        f"{target} accepted your contact request. The contact room is {record.room_id}.",
        {SKIP_MENTIONS_KEY: True},
    )


async def accept_contact(
    context: ToolRuntimeContext,
    agent_name: str,
    contact_id: str,
    *,
    rules: str | None,
    tools: list[str] | None,
) -> ContactRecord:
    """Accept a pending request for its target owner: open the contact room, seat both parties, and tell the requester.

    Only the agent of the target owner's personal room, which asked them there, accepts. The target side keeps any
    rules or tools left out. Both agent parties keep the room while the contact is active. A room that cannot be fully
    set up is left again and the request stays pending.
    """
    config, paths = context.current_config, context.runtime_paths
    own = _own_party(context, agent_name)
    book = await run_blocking_until_complete(read_contacts, paths)
    record, side = _owned(book, contact_id, own.owner)
    if side != "target":
        msg = f"Only {record.target.owner} may accept this contact"
        raise ValueError(msg)
    if record.status != "pending":
        raise ValueError(_NOT_PENDING)
    poster, _ = _request_poster(config, paths, own.owner)
    if agent_name != poster:
        msg = f"Only {poster}, the agent of {own.owner}'s personal room, may accept this contact"
        raise ValueError(msg)
    target = record.target
    if target.kind == "person" and (rules is not None or tools is not None):
        raise ValueError(_PERSON_HAS_NO_SIDE)
    checked = _validate_side_tools(config, target.id, tools) if target.kind == "agent" else None
    agents = _running_agents(context, record)
    _, client = agents[_room_creator(record)]
    room_name = f"{party_name(config, record.requester)} ↔ {party_name(config, target)}"
    room_id = await create_room(client, room_name, initial_state=_CONTACT_ROOM_STATE)
    if room_id is None:
        msg = "The contact room could not be created; accept again to retry"
        raise ValueError(msg)

    def activate(book: ContactBook) -> ContactRecord:
        current = _by_id(book, contact_id)
        if current.status != "pending":
            raise ValueError(_NOT_PENDING)
        current.status = "active"
        current.room_id = room_id
        _change_side(current.target_side, rules, checked)
        return current.model_copy(deep=True)

    try:
        await _seat_parties(context, record, room_id, agents)
        accepted = await run_blocking_until_complete(update_contacts, paths, activate)
    except ValueError:
        await _leave_contact_room(context, record, room_id)
        raise
    await _announce_acceptance(context, accepted, poster)
    return accepted


async def decline_contact(context: ToolRuntimeContext, agent_name: str, contact_id: str) -> ContactRecord:
    """Decline a pending request for its target owner and tell the thread it came from."""
    own = _own_party(context, agent_name)

    def decline(book: ContactBook) -> ContactRecord:
        record, side = _owned(book, contact_id, own.owner)
        if side != "target":
            msg = f"Only {record.target.owner} may decline this contact"
            raise ValueError(msg)
        if record.status != "pending":
            msg = "Only a pending contact can be declined"
            raise ValueError(msg)
        record.status = "declined"
        return record.model_copy(deep=True)

    record = await run_blocking_until_complete(update_contacts, context.runtime_paths, decline)
    # A person who asked has no agent to tell.
    if record.requester.kind == "agent":
        await _post_to_origin(
            context,
            record,
            record.requester.id,
            f"{record.target.owner} declined contact {record.id} with {record.target.id}; "
            "they must remove it before you can ask again.",
            {SKIP_MENTIONS_KEY: True},
        )
    return record


async def update_contact(
    context: ToolRuntimeContext,
    agent_name: str,
    contact_id: str,
    *,
    rules: str | None,
    tools: list[str] | None,
) -> ContactRecord:
    """Change the rules or tools of the caller's own side of a contact; a field left None stays, and blank rules clear."""
    own = _own_party(context, agent_name)
    if rules is None and tools is None:
        msg = "Nothing to change: give rules, tools, or both"
        raise ValueError(msg)
    config = context.current_config

    def update(book: ContactBook) -> ContactRecord:
        record, side = _owned(book, contact_id, own.owner)
        if record.status in {"declined", "removed"}:
            msg = f"A {record.status} contact cannot be changed"
            raise ValueError(msg)
        if (party := record.party(side)).kind != "agent":
            raise ValueError(_PERSON_HAS_NO_SIDE)
        _change_side(record.side(side), rules, _validate_side_tools(config, party.id, tools))
        return record.model_copy(deep=True)

    return await run_blocking_until_complete(update_contacts, context.runtime_paths, update)


async def remove_contact(context: ToolRuntimeContext, agent_name: str, contact_id: str) -> ContactRecord:
    """Remove a contact for either owner, except that only the owner who declined a contact may remove it.

    The contact's agents leave its room and stop keeping it across restarts.
    """
    own = _own_party(context, agent_name)

    def remove(book: ContactBook) -> ContactRecord:
        record, side = _owned(book, contact_id, own.owner)
        if record.status == "removed":
            msg = "The contact is already removed"
            raise ValueError(msg)
        # A declined contact blocks new requests until the owner who declined it removes it.
        if record.status == "declined" and side != "target":
            msg = f"Only {record.target.owner} may remove a declined contact"
            raise ValueError(msg)
        record.status = "removed"
        return record.model_copy(deep=True)

    record = await run_blocking_until_complete(update_contacts, context.runtime_paths, remove)
    if record.room_id is not None:
        await _leave_contact_room(context, record, record.room_id)
    return record


def list_contacts(context: ToolRuntimeContext, agent_name: str) -> str:
    """Return the caller-owner's live contacts with their own side, and the personal agents they can ask, as YAML."""
    own = _own_party(context, agent_name)
    config = context.current_config
    contacts = []
    for record in read_contacts(context.runtime_paths).contacts:
        side = record.owner_side(own.owner)
        if side is None or record.status == "removed":
            continue
        yours = record.side(side)
        contacts.append(
            {
                "id": record.id,
                "status": record.status,
                "you": side,
                "with": record.party(record.other(side)).model_dump(),
                "reason": record.reason,
                "your_rules": yours.rules,
                "your_tools": yours.tools,
            },
        )
    contactable = [
        {"agent": name, "owner": party.owner}
        for name in config.agents
        if (party := agent_party(config, name)) is not None and party.owner != own.owner
    ]
    return yaml.safe_dump({"contacts": contacts, "contactable_agents": contactable}, sort_keys=False)
