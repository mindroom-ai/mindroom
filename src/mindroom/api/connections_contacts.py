"""The Connections portal's Contacts page: the signed-in owner's contacts, and the changes they make to them."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime  # noqa: TC003 - pydantic resolves the field type at runtime
from typing import TYPE_CHECKING, Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict

from mindroom.api import config_lifecycle
from mindroom.api.auth import require_connections_user
from mindroom.api.connection_agents import CONNECTIONS_HEADERS, EmptyMutation, require_connections_same_origin
from mindroom.background_tasks import run_blocking_until_complete
from mindroom.contacts import flow
from mindroom.contacts.parties import agent_party, owner_contact_agents, party_name
from mindroom.contacts.store import (
    ContactStatus,
    PartyKind,
    SideName,
    owner_contacts,
    read_contacts,
)
from mindroom.contacts.turns import contact_tool_choices, contact_turn_tools
from mindroom.oauth.service import oauth_provider_service_account_configured
from mindroom.requester_identity import resolve_human_requester_alias
from mindroom.tool_system.declarations import ToolFileAccess
from mindroom.tool_system.plugins import config_plugins

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from mindroom.config.contacts import ContactsConfig
    from mindroom.config.main import Config
    from mindroom.config.models import FileAccess
    from mindroom.constants import RuntimePaths
    from mindroom.contacts.store import ContactParty, ContactRecord
    from mindroom.tool_system.declarations import ToolMetadata

router = APIRouter(prefix="/api/connections/contacts", tags=["connections"])

type _ToolReach = Literal["code", "unconfined", "files", "none"]

_UNAVAILABLE = "Contacts are unavailable"
_RESTARTING = "MindRoom is restarting its agents; try again in a moment."


class ContactTerms(BaseModel):
    """The rules and tools an owner sets for their side; a field left out keeps what is set."""

    model_config = ConfigDict(extra="forbid")

    rules: str | None = None
    tools: list[str] | None = None


class ContactToolChoice(BaseModel):
    """One tool a contact of the agent may use, with what it lets the other side reach.

    `reach` is `code` for a tool that runs code, `unconfined` for one that `file_access` does not confine to the
    agent's files, `files` for one it confines there, and `none` otherwise. `provider` is the OAuth provider whose
    connected account the tool acts with, if any, and `shared_account` says a shared service account serves the
    tool's provider instead, which the other side can then drive.
    """

    name: str
    display_name: str
    provider: str | None
    shared_account: bool
    reach: _ToolReach


class ContactAgent(BaseModel):
    """One of the owner's personal agents with contacts, and what applies to every contact it has.

    `configured_rules` come from its configuration, and `judged` says whether a judge checks its messages.
    `tool_choices` leaves out `matrix_message`, which every contact offers.
    """

    agent_name: str
    configured_rules: list[str]
    judged: bool
    tool_choices: list[ContactToolChoice]


class ContactPartyView(BaseModel):
    """One end of a contact, named for people: an agent's display name, or a person's Matrix ID."""

    kind: PartyKind
    id: str
    owner: str
    display_name: str


class ContactView(BaseModel):
    """One contact as its owner sees it: the owner's own side only, and never a removed contact.

    `rules` are what the owner set for their side, and `effective_tools` is what a contact turn offers now. Both are
    None for a person's side and for an agent that no longer has contacts. `room_id` names the room of an active
    contact only.
    """

    id: str
    status: ContactStatus
    side: SideName
    you: ContactPartyView
    other: ContactPartyView
    reason: str
    room_id: str | None
    rules: str | None
    effective_tools: list[str] | None
    requested_at: datetime | None
    decided_at: datetime | None


class ContactsPage(BaseModel):
    """The owner's agents with contacts, and their contacts that are not removed, in the order they were asked."""

    agents: list[ContactAgent]
    contacts: list[ContactView]


@dataclass(frozen=True)
class _Viewer:
    owner: str
    config: Config
    runtime_paths: RuntimePaths


async def _viewer(request: Request, response: Response) -> _Viewer:
    """Authenticate the portal user; the owner is their canonical Matrix ID, so a bridge alias sees the same contacts."""
    response.headers.update(CONNECTIONS_HEADERS)
    auth_user = await require_connections_user(request)
    if request.query_params:
        raise HTTPException(400, "Query parameters are not accepted", headers=CONNECTIONS_HEADERS)
    snapshot = config_lifecycle.bind_current_request_snapshot(request)
    config = snapshot.runtime_config
    if config is None:
        raise HTTPException(503, _UNAVAILABLE, headers=CONNECTIONS_HEADERS)
    paths = snapshot.runtime_paths
    owner = resolve_human_requester_alias(auth_user.matrix_user_id, config, paths)
    return _Viewer(owner, config, paths)


_ViewerContext = Annotated[_Viewer, Depends(_viewer)]


def _reach(metadata: ToolMetadata, agent_file_access: FileAccess) -> _ToolReach:
    """Return what the other side can ask a tool to reach; `unrestricted` file access confines no tool to the files."""
    if metadata.executes_code:
        return "code"
    if metadata.file_access is ToolFileAccess.UNCONFINED or (
        metadata.file_access is ToolFileAccess.AGENT and agent_file_access == "unrestricted"
    ):
        return "unconfined"
    return "files" if metadata.file_access is ToolFileAccess.AGENT else "none"


def _agent_view(config: Config, runtime_paths: RuntimePaths, agent_name: str, contacts: ContactsConfig) -> ContactAgent:
    plugins = config_plugins(config, runtime_paths)
    file_access = config.resolve_agent(agent_name).file_access
    choices = []
    for name in contact_tool_choices(config, runtime_paths, agent_name):
        if name == "matrix_message":
            continue
        if (metadata := plugins.tools.get(name)) is None:
            choices.append(
                ContactToolChoice(name=name, display_name=name, provider=None, shared_account=False, reach="none"),
            )
            continue
        provider = metadata.auth_provider
        oauth_provider = None if provider is None else plugins.oauth_providers.get(provider)
        shared = oauth_provider is not None and oauth_provider_service_account_configured(oauth_provider, runtime_paths)
        choices.append(
            ContactToolChoice(
                name=name,
                display_name=metadata.display_name,
                provider=None if shared else provider,
                shared_account=shared,
                reach=_reach(metadata, file_access),
            ),
        )
    return ContactAgent(
        agent_name=agent_name,
        configured_rules=list(contacts.rules),
        judged=contacts.judgment is not None,
        tool_choices=choices,
    )


def _party_view(config: Config, party: ContactParty) -> ContactPartyView:
    return ContactPartyView(kind=party.kind, id=party.id, owner=party.owner, display_name=party_name(config, party))


def _contact_view(config: Config, runtime_paths: RuntimePaths, record: ContactRecord, side: SideName) -> ContactView:
    you = record.party(side)
    own = record.side(side)
    with_terms = you.kind == "agent" and agent_party(config, you.id) is not None
    return ContactView(
        id=record.id,
        status=record.status,
        side=side,
        you=_party_view(config, you),
        other=_party_view(config, record.counterparty(side)),
        reason=record.reason,
        room_id=record.room_id if record.status == "active" else None,
        rules=own.rules if with_terms else None,
        effective_tools=list(contact_turn_tools(config, runtime_paths, you.id, record, side)) if with_terms else None,
        requested_at=record.requested_at,
        decided_at=record.decided_at,
    )


def _page(viewer: _Viewer) -> ContactsPage:
    """Build the owner's page; an unreadable contacts file is unavailable rather than an error naming the file."""
    config, paths = viewer.config, viewer.runtime_paths
    try:
        owned = owner_contacts(paths, viewer.owner)
    except ValueError as exc:
        raise HTTPException(503, _UNAVAILABLE, headers=CONNECTIONS_HEADERS) from exc
    return ContactsPage(
        agents=[
            _agent_view(config, paths, name, settings)
            for name in owner_contact_agents(config, viewer.owner)
            if (settings := config.agents[name].contacts) is not None
        ],
        contacts=[_contact_view(config, paths, record, side) for record, side in owned],
    )


@router.get("")
async def contacts(viewer: _ViewerContext) -> ContactsPage:
    """List the signed-in owner's contacts with their own side's rules and tools."""
    return await run_blocking_until_complete(_page, viewer)


@asynccontextmanager
async def _agents_admitted(request: Request, *, admit: bool) -> AsyncIterator[None]:
    """Hold a response slot while a change makes agents post, join, or leave, so no reload swaps them out meanwhile."""
    if not admit:
        yield
        return
    gate = config_lifecycle.app_state(request.app).runtime.response_admission_gate
    if not gate.admit():
        raise HTTPException(503, _RESTARTING, headers=CONNECTIONS_HEADERS)
    try:
        yield
    finally:
        gate.release()


async def _change(
    request: Request,
    viewer: _Viewer,
    change: Callable[[flow.ContactActor], Awaitable[object]],
    *,
    moves_agents: bool,
) -> ContactsPage:
    """Run one change for the signed-in owner and answer with their page; a refusal is a 409 saying why.

    The store is read first, so an unreadable file is unavailable rather than a refusal naming the file.
    """
    require_connections_same_origin(request, viewer.runtime_paths)
    try:
        await run_blocking_until_complete(read_contacts, viewer.runtime_paths)
    except ValueError as exc:
        raise HTTPException(503, _UNAVAILABLE, headers=CONNECTIONS_HEADERS) from exc
    actor = flow.ContactActor(
        owner=viewer.owner,
        agent_name=None,
        config=viewer.config,
        runtime_paths=viewer.runtime_paths,
        agent_bot=config_lifecycle.app_state(request.app).runtime.agent_bot,
    )
    async with _agents_admitted(request, admit=moves_agents):
        try:
            await change(actor)
        except ValueError as exc:
            raise HTTPException(409, str(exc), headers=CONNECTIONS_HEADERS) from exc
    return await run_blocking_until_complete(_page, viewer)


@router.post("/{contact_id}/accept")
async def accept(contact_id: str, terms: ContactTerms, request: Request, viewer: _ViewerContext) -> ContactsPage:
    """Accept a request to the owner with the terms they chose, opening the contact room."""
    return await _change(
        request,
        viewer,
        lambda actor: flow.accept_contact(actor, contact_id, rules=terms.rules, tools=terms.tools),
        moves_agents=True,
    )


@router.post("/{contact_id}/decline")
async def decline(contact_id: str, _body: EmptyMutation, request: Request, viewer: _ViewerContext) -> ContactsPage:
    """Decline a request to the owner; its thread and the thread an asking agent asked from are told."""
    return await _change(request, viewer, lambda actor: flow.decline_contact(actor, contact_id), moves_agents=True)


@router.post("/{contact_id}/update")
async def update(contact_id: str, terms: ContactTerms, request: Request, viewer: _ViewerContext) -> ContactsPage:
    """Change the rules or tools of the owner's side; it only writes the store, so it needs no agent."""
    return await _change(
        request,
        viewer,
        lambda actor: flow.update_contact(actor, contact_id, rules=terms.rules, tools=terms.tools),
        moves_agents=False,
    )


@router.post("/{contact_id}/remove")
async def remove(contact_id: str, _body: EmptyMutation, request: Request, viewer: _ViewerContext) -> ContactsPage:
    """Remove a contact, withdraw a request the owner sent, or clear a request the owner declined."""
    return await _change(request, viewer, lambda actor: flow.remove_contact(actor, contact_id), moves_agents=True)
