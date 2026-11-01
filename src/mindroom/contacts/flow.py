"""The contact operations an agent's tools run: request, decline, update, remove, and list.

Each operation first checks that the requester owns the agent it runs for, then that they own a side of the contact.
Every refusal is a ``ValueError`` whose text is meant for the agent that called.
"""

from __future__ import annotations

import time
from functools import partial
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

import yaml

from mindroom.background_tasks import run_blocking_until_complete
from mindroom.config.contacts import CONTACT_CONTROL_TOOLS
from mindroom.config.resolution import authored_tool_entries
from mindroom.constants import SKIP_MENTIONS_KEY
from mindroom.contacts.parties import agent_party, person_party, personal_room_of, resolve_target
from mindroom.contacts.policy import effective_rules
from mindroom.contacts.store import (
    ContactBook,
    ContactParty,
    ContactRecord,
    ContactSide,
    read_contacts,
    update_contacts,
)
from mindroom.logging_config import get_logger
from mindroom.matrix.client_delivery import send_message_result
from mindroom.matrix.client_room_admin import leave_room
from mindroom.matrix.invited_rooms_store import forget_invited_room, invited_rooms_path
from mindroom.matrix.message_builder import build_message_content
from mindroom.requester_identity import resolve_human_requester_alias
from mindroom.tool_system.registry import builtin_tool_registry

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.contacts.store import SideName
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

logger = get_logger(__name__)

CONTACT_REQUEST_KEY = "io.mindroom.contact_request"
_SOURCE_HOOK = "native:contacts"
_PERSON_HAS_NO_SIDE = "A person has no rules or tools; only an agent's side can have them"


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


def _refused_tool(name: str) -> bool:
    """Return whether a contact never offers this tool: a control tool, or a Matrix room tool other than matrix_message."""
    if name in CONTACT_CONTROL_TOOLS:
        return True
    metadata = builtin_tool_registry().get(name)
    return name != "matrix_message" and metadata is not None and metadata.requires_room_context


def validate_side_tools(config: Config, agent_name: str, tools: list[str] | None) -> list[str] | None:
    """Return the tools one side of a contact offers, refusing any the agent lacks or a contact may never offer."""
    if tools is None:
        return None
    authored = config.expand_tool_names([entry.name for entry in authored_tool_entries(config, agent_name)])
    allowed = [name for name in authored if not config.is_tool_preset(name) and not _refused_tool(name)]
    if outside := [name for name in tools if name not in allowed]:
        msg = f"Not among the tools a contact may use: {', '.join(outside)}; allowed: {', '.join(allowed)}"
        raise ValueError(msg)
    return list(tools)


def _same_pair(record: ContactRecord, first: ContactParty, second: ContactParty) -> bool:
    ends = {(party.kind, party.id) for party in (record.requester, record.target)}
    return ends == {(first.kind, first.id), (second.kind, second.id)}


def _label(config: Config, party: ContactParty) -> str:
    if party.kind == "person":
        return party.id
    return f"{config.agents[party.id].display_name} ({party.id}), the agent of {party.owner}"


def _notice_body(config: Config, record: ContactRecord) -> str:
    """Return the request as the target owner reads it, with the rules that would apply to their side."""
    target = record.target
    asks = "you" if target.kind == "person" else f"your agent {config.agents[target.id].display_name} ({target.id})"
    lines = [f"{_label(config, record.requester)} asks to contact {asks}.", "", f"Reason: {record.reason}", ""]
    if target.kind == "agent":
        rules = effective_rules(config, target.id, record.target_side)
        lines += ["Rules that would apply to your agent by default:", rules or "None.", ""]
    lines += [f"Contact ID: {record.id}", "Reply in this thread to accept, decline, or set rules."]
    return "\n".join(lines)


def _by_id(book: ContactBook, contact_id: str) -> ContactRecord:
    """Return a contact this process just stored; contacts are never deleted, only given a status."""
    return next(record for record in book.contacts if record.id == contact_id)


def _set_notice(contact_id: str, room_id: str, event_id: str, book: ContactBook) -> None:
    record = _by_id(book, contact_id)
    record.notice_room_id = room_id
    record.notice_event_id = event_id


async def post_request_notice(context: ToolRuntimeContext, record: ContactRecord) -> None:
    """Post the request as a new thread in the target owner's personal room and store where it landed."""
    config, paths = context.current_config, context.runtime_paths
    owner = record.target.owner
    if (personal := personal_room_of(config, paths, owner)) is None:
        msg = f"{owner} has no personal room yet, so the request cannot reach them; ask again once they have one"
        raise ValueError(msg)
    personal_agent, room_id = personal
    poster = record.target.id if record.target.kind == "agent" else personal_agent
    client = None if context.orchestrator is None else context.orchestrator.running_entity_client(poster)
    if client is None:
        msg = f"{poster} is not running, so it cannot post the request; ask again later"
        raise ValueError(msg)
    content = build_message_content(
        _notice_body(config, record),
        extra_content={"msgtype": "m.notice", SKIP_MENTIONS_KEY: True, CONTACT_REQUEST_KEY: record.id},
    )
    delivered = await send_message_result(client, room_id, content, transaction_id=f"contact-request-{record.id}")
    if delivered is None:
        msg = f"The request to {record.target.id} could not be posted to its owner's personal room; ask again to retry"
        raise ValueError(msg)
    await run_blocking_until_complete(
        update_contacts,
        paths,
        partial(_set_notice, record.id, room_id, delivered.event_id),
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
        side = ContactSide(rules=_clean_rules(rules), tools=validate_side_tools(config, agent_name, tools))
    _check_pair(requester, target_party)

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
        await post_request_notice(context, record)
        return _reload(context, record.id)
    return record


async def _tell_origin(context: ToolRuntimeContext, record: ContactRecord, text: str) -> None:
    """Tell the thread a request came from, as the requesting agent; a person who asked needs no notice."""
    if record.requester.kind != "agent":
        return
    bot = None if context.orchestrator is None else context.orchestrator.agent_bots.get(record.requester.id)
    if bot is None:
        logger.warning("contact_origin_notice_skipped", contact_id=record.id, agent_name=record.requester.id)
        return
    await bot.hook_context.send_message(
        record.origin_room_id,
        text,
        record.origin_thread_id,
        _SOURCE_HOOK,
        {SKIP_MENTIONS_KEY: True},
    )


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
        record.decided_at = time.time()
        return record.model_copy(deep=True)

    record = await run_blocking_until_complete(update_contacts, context.runtime_paths, decline)
    await _tell_origin(
        context,
        record,
        f"{record.target.owner} declined contact {record.id} with {record.target.id}; "
        "they must remove it before you can ask again.",
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
        checked = validate_side_tools(config, party.id, tools)
        changed = record.side(side)
        if rules is not None:
            changed.rules = _clean_rules(rules)
        if checked is not None:
            changed.tools = checked
        return record.model_copy(deep=True)

    return await run_blocking_until_complete(update_contacts, context.runtime_paths, update)


async def remove_contact(context: ToolRuntimeContext, agent_name: str, contact_id: str) -> ContactRecord:
    """Remove a contact for either owner; its agents leave the contact room and stop keeping it across restarts."""
    own = _own_party(context, agent_name)

    def remove(book: ContactBook) -> ContactRecord:
        record, _ = _owned(book, contact_id, own.owner)
        if record.status == "removed":
            msg = "The contact is already removed"
            raise ValueError(msg)
        record.status = "removed"
        return record.model_copy(deep=True)

    record = await run_blocking_until_complete(update_contacts, context.runtime_paths, remove)
    if record.room_id is not None:
        for party in (record.requester, record.target):
            if party.kind != "agent":
                continue
            client = None if context.orchestrator is None else context.orchestrator.running_entity_client(party.id)
            if client is not None:
                await leave_room(client, record.room_id)
            path = invited_rooms_path(context.runtime_paths, party.id)
            await run_blocking_until_complete(forget_invited_room, path, record.room_id)
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
