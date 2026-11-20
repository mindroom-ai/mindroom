"""Tailnet SSH on the Connections portal, mounted under `/api/connections/tailnet-ssh`.

It lists the person's SSH public keys and every agent they may use that opts in to SSH, each with whether it can
serve SSH now and the person's link to it; it replaces the keys or adds their GitHub account's, and connects, signs in
again, or disconnects a link. The person comes from the session and the worker from the agent, so no body, path, or
query names a Matrix ID or worker key. A node's sign-in URL leaves the primary only in the listing, to its owner.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Annotated
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict

from mindroom.api import config_lifecycle
from mindroom.api.auth import require_connections_user
from mindroom.api.connection_agents import (
    CONNECTIONS_HEADERS,
    ConnectionUserContext,
    require_connections_same_origin,
    resolve_connection_agent,
    resolve_connection_user,
)
from mindroom.api.tailnet_ssh_gateway import tailnet_ssh_status
from mindroom.config.tailnet_ssh import normalize_public_key
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.logging_config import get_logger
from mindroom.oauth.credential_lifecycle import (
    load_oauth_credentials_status,
    oauth_verified_claim,
    resolve_oauth_credential_context,
)
from mindroom.oauth.github import github_oauth_provider
from mindroom.shared_directories.store import is_shareable_agent, load_shared_directories, shared_directory_access
from mindroom.tailnet_ssh.eligibility import link_ineligibility
from mindroom.tailnet_ssh.links import fresh_link_statuses, link_state, listed_workers, refresh_projections
from mindroom.tailnet_ssh.projection import authorized_key_lines
from mindroom.tailnet_ssh.store import (
    HOSTNAME,
    default_hostname,
    new_link,
    read_links,
    remove_links,
    set_keys,
    update_links,
)
from mindroom.tool_system.worker_routing import build_requester_execution_identity

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.tailnet_ssh.links import GatewayStatus, LinkStatus
    from mindroom.tailnet_ssh.store import Link, LinkBook

logger = get_logger(__name__)

router = APIRouter(prefix="/api/connections/tailnet-ssh", tags=["tailnet-ssh"])

_GITHUB_KEYS_MAX_BYTES = 64 * 1024
_GITHUB_FAILED = "GitHub did not answer with the account's SSH keys; try again later."
_NOT_REFRESHED = (
    "Your SSH keys are saved, but a worker's SSH files could not be updated; "
    "they are updated before its next SSH connection. Save again to retry now."
)
_BAD_HOSTNAME = "A hostname is 1 to 63 lowercase letters, digits, and hyphens, and starts and ends with no hyphen."
# How the card names a worker by its handle's status; a worker the backend does not list is asleep.
_WORKER_STATES = {"ready": "ready", "starting": "waking"}


class KeysRequest(BaseModel):
    """The person's SSH public keys, as pasted: each entry may hold several lines."""

    model_config = ConfigDict(extra="forbid")

    keys: list[str]


class ConnectRequest(BaseModel):
    """The hostname the link's node takes in the person's tailnet."""

    model_config = ConfigDict(extra="forbid")

    hostname: str


@dataclass(frozen=True)
class _Person:
    """The signed-in person with the agents they may use, and what the gateway last reported."""

    user: ConnectionUserContext
    memberships: AgentReplyMembershipIndex
    status: GatewayStatus

    @property
    def requester_id(self) -> str:
        return self.user.owner.requester_id


def _refusal(status_code: int, detail: str) -> HTTPException:
    return HTTPException(status_code, detail, headers=CONNECTIONS_HEADERS)


async def _signed_in(request: Request, response: Response) -> str:
    """Authenticate the person and refuse every query parameter; return their Matrix ID."""
    response.headers.update(CONNECTIONS_HEADERS)
    auth_user = await require_connections_user(request)
    if request.query_params:
        raise _refusal(400, "Query parameters are not accepted")
    return auth_user.matrix_user_id


_SignedIn = Annotated[str, Depends(_signed_in)]


def _person(request: Request, matrix_user_id: str, status: GatewayStatus) -> _Person:
    memberships = config_lifecycle.app_state(request.app).runtime.agent_reply_memberships
    snapshot = config_lifecycle.bind_current_request_snapshot(request)
    user = resolve_connection_user(snapshot, matrix_user_id, membership_index=memberships)
    return _Person(user, memberships, status)


async def _changing(request: Request, matrix_user_id: _SignedIn) -> _Person:
    """Refuse a change from another origin, or any change while tailnet SSH is off, and return who makes it."""
    snapshot = config_lifecycle.bind_current_request_snapshot(request)
    require_connections_same_origin(
        request,
        snapshot.runtime_paths,
        detail="Tailnet SSH changes require a same-origin request",
    )
    status = tailnet_ssh_status(request.app)
    if status is None:
        raise _refusal(404, "Tailnet SSH is off.")
    return _person(request, matrix_user_id, status)


_Changing = Annotated[_Person, Depends(_changing)]


def _link_row(link: Link, node: LinkStatus | None, *, suspended: bool) -> dict[str, object]:
    """Return the card's view of a link that is not ``off``, with ``worker`` left for the caller."""
    state = link_state(link, node, suspended=suspended)
    return {
        "hostname": link.hostname,
        "state": state,
        # A node that is suspended or going away is not to be signed in to.
        "login_url": node.login_url if node is not None and state in {"pending_login", "needs_login"} else None,
        "tailnet": None if node is None else node.tailnet,
        "dns_name": None if node is None else node.dns_name,
        "login_name": None if node is None else node.login_name,
        "key_expiry": None if node is None else node.key_expiry,
        "worker": None,
        "host_public_key": link.host_public_key,
        "last_error": None if node is None else node.last_error,
    }


def _overview(person: _Person, nodes: Mapping[str, LinkStatus]) -> dict[str, object]:
    """Return the gateway's enrolment mode, the person's keys, and their agents that opt in to SSH, with links. Blocking.

    An agent with a link of theirs that is not ``off`` is listed too, also when it no longer opts in or they may not
    use it now, because the gateway still holds that link's node in their tailnet; it shows why, and can be
    disconnected. The backend is asked for its workers only when one of the links is enrolled, the only state that
    shows its worker.
    """
    config, runtime_paths, requester_id = person.user.config, person.user.runtime_paths, person.requester_id
    book = read_links(runtime_paths)
    directories = load_shared_directories(runtime_paths)
    is_shareable = partial(is_shareable_agent, config)
    agents: list[dict[str, object]] = []
    enrolled: list[tuple[dict[str, object], str]] = []
    for name, agent in config.agents.items():
        link = book.link(requester_id, name)
        if link is not None and link.desired == "off":
            link = None
        usable = name in person.user.agent_names
        if not (usable and agent.tailnet_ssh) and link is None:
            continue
        if usable:
            worker_key = resolve_connection_agent(person.user, name).worker_target.worker_key
        else:
            # The agent's target is built only for a person who may use it; their link names its worker.
            worker_key = None if link is None else link.worker_key
        ineligible = link_ineligibility(
            config,
            runtime_paths,
            requester_id,
            name,
            worker_key,
            person.memberships,
            directories,
        )
        row = None
        if link is not None:
            row = _link_row(link, nodes.get(link.id), suspended=ineligible is not None)
            if row["state"] == "enrolled":
                enrolled.append((row, link.worker_key))
        agents.append(
            {
                "name": name,
                "display_name": agent.display_name,
                "eligible": ineligible is None,
                "reason": None if ineligible is None else ineligible.reason,
                "detail": None if ineligible is None else ineligible.detail,
                "default_hostname": default_hostname(name),
                "knowledge_bases": list(agent.knowledge_bases),
                "shared_directories": [
                    f"{directory.owner}/{directory.name}"
                    for directory in directories
                    if shared_directory_access(directory, (name,), is_shareable) is True
                ],
                "link": row,
            },
        )
    if enrolled:
        statuses = {handle.worker_key: handle.status for handle in listed_workers(runtime_paths)}
        for row, worker_key in enrolled:
            row["worker"] = _WORKER_STATES.get(statuses.get(worker_key, ""), "asleep")
    return {"enrollment": book.enrollment, "keys": book.keys.get(requester_id, []), "agents": agents}


async def _github_login(user: ConnectionUserContext) -> str | None:
    """Return the verified login of the person's connected GitHub account, the one the `github` tool uses, or None."""
    runtime_paths = user.runtime_paths
    # A requester-scoped provider keeps one connection per person whatever the agent; the name only fills the identity.
    identity = build_requester_execution_identity(
        agent_name="oauth",
        runtime_paths=runtime_paths,
        requester_id=user.owner.requester_id,
    )
    context = resolve_oauth_credential_context(
        github_oauth_provider(),
        runtime_paths,
        get_runtime_credentials_manager(runtime_paths),
        None,
        execution_identity=identity,
        config=user.config,
    )
    if context.worker_target is None:
        return None
    credentials = (await load_oauth_credentials_status(context, create_store=False)).credentials
    return None if credentials is None else oauth_verified_claim(credentials, "login")


async def _fetch_github_keys(login: str) -> list[str]:
    """Return the public key lines GitHub publishes for ``login``, read up to 64 KiB, or answer 502.

    The answer must come uncompressed, so the bytes read are the bytes kept.
    """
    content = b""
    try:
        async with (
            httpx.AsyncClient(follow_redirects=False, timeout=10) as client,
            client.stream(
                "GET",
                f"https://github.com/{quote(login, safe='')}.keys",
                headers={"Accept-Encoding": "identity"},
            ) as response,
        ):
            encoding = response.headers.get("Content-Encoding", "identity")
            if response.status_code != 200 or encoding != "identity":
                logger.warning("tailnet_ssh_github_keys_refused", status=response.status_code, encoding=encoding)
                raise _refusal(502, _GITHUB_FAILED)
            # The body as it arrives, never decompressed, so no more than one network read past the limit is taken.
            async for chunk in response.aiter_raw():
                content += chunk[: _GITHUB_KEYS_MAX_BYTES - len(content)]
                if len(content) == _GITHUB_KEYS_MAX_BYTES:
                    break
    except httpx.HTTPError as error:
        logger.warning("tailnet_ssh_github_keys_failed", error_type=type(error).__name__)
        raise _refusal(502, _GITHUB_FAILED) from None
    lines = content.decode(errors="replace").split("\n")
    if len(content) == _GITHUB_KEYS_MAX_BYTES:
        # The answer was cut, so its last line may be part of a key.
        lines.pop()
    keys: list[str] = []
    for line in lines:
        with contextlib.suppress(ValueError):
            keys.append(normalize_public_key(line))
    return keys


async def _change_keys(person: _Person, keys: Callable[[list[str]], list[str]]) -> int:
    """Store as the person's keys what ``keys`` makes of their stored ones, then rewrite their workers' SSH files.

    Return how many more keys are stored than before. A key refused stores nothing and answers 422. When a worker's
    files cannot be rewritten the keys stay stored and the answer is a 503 saying so: sending the same keys again
    retries, and the worker's next connect writes its files from the stored keys before its sshd gets the connection.
    """
    requester_id = person.requester_id

    def change(book: LinkBook) -> int:
        stored = book.keys.get(requester_id, [])
        try:
            set_keys(book, requester_id, keys(stored), now=time.time())
        except ValueError as error:
            raise _refusal(422, str(error)) from None
        return len(book.keys.get(requester_id, [])) - len(stored)

    def store() -> int:
        return update_links(person.user.runtime_paths, change)

    added = await asyncio.to_thread(store)
    try:
        await asyncio.to_thread(refresh_projections, person.user.config, person.user.runtime_paths, requester_id)
    except OSError as error:
        logger.warning("tailnet_ssh_projections_not_refreshed", error=str(error))
        raise _refusal(503, _NOT_REFRESHED) from None
    return added


def _own_link(book: LinkBook, requester_id: str, agent_name: str) -> Link:
    """Return the person's link to the agent, or answer 404 without one or for one parked ``off``."""
    link = book.link(requester_id, agent_name)
    if link is None or link.desired == "off":
        raise _refusal(404, "You have no SSH link to this agent.")
    return link


@router.get("")
async def tailnet_ssh_overview(request: Request, matrix_user_id: _SignedIn) -> dict[str, object]:
    """Return the person's keys, GitHub login, and agents with their links; ``enabled`` is false while SSH is off."""
    status = tailnet_ssh_status(request.app)
    if status is None:
        return {"enabled": False, "enrollment": None, "keys": [], "github_login": None, "agents": []}
    person = _person(request, matrix_user_id, status)
    nodes = fresh_link_statuses(status.report, monotonic=time.monotonic())
    overview = await asyncio.to_thread(_overview, person, nodes)
    return {"enabled": True, **overview, "github_login": await _github_login(person.user)}


@router.put("/keys", status_code=204)
async def put_keys(body: KeysRequest, person: _Changing) -> None:
    """Replace the person's keys; a removed key closes their open sessions."""
    await _change_keys(person, lambda _stored: body.keys)


@router.post("/keys/github")
async def import_github_keys(person: _Changing) -> dict[str, int]:
    """Add the keys GitHub publishes for the person's connected account, and answer how many were new."""
    login = await _github_login(person.user)
    if login is None:
        raise _refusal(409, "Connect a GitHub account first.")
    github_keys = await _fetch_github_keys(login)
    return {"added": await _change_keys(person, lambda stored: [*stored, *github_keys])}


@router.put("/agents/{agent_name}", status_code=204)
async def connect_agent(agent_name: str, body: ConnectRequest, person: _Changing) -> None:
    """Create the person's link to the agent, or turn a parked one back on; one that is on keeps its hostname."""
    user, requester_id = person.user, person.requester_id
    worker_key = resolve_connection_agent(user, agent_name).worker_target.worker_key
    if HOSTNAME.fullmatch(body.hostname) is None:
        raise _refusal(422, _BAD_HOSTNAME)
    ineligible = await asyncio.to_thread(
        link_ineligibility,
        user.config,
        user.runtime_paths,
        requester_id,
        agent_name,
        worker_key,
        person.memberships,
    )
    if ineligible is not None or worker_key is None:
        # A target without a worker key is ineligible for its scope.
        raise _refusal(409, "scope" if ineligible is None else ineligible.reason)

    def connect(book: LinkBook) -> None:
        if not authorized_key_lines(user.config, book, requester_id):
            raise _refusal(409, "Add an SSH public key first.")
        link = book.link(requester_id, agent_name)
        now = time.time()
        if link is None:
            book.links.append(new_link(requester_id, agent_name, worker_key, body.hostname, now=now))
        elif link.desired == "removing":
            raise _refusal(409, "Still disconnecting; try again in a few seconds.")
        elif link.desired == "off":
            # It comes back as a new link would: for this worker, with a day from now to enrol.
            link.desired, link.worker_key, link.hostname = "on", worker_key, body.hostname
            link.created_at = link.login_after = now
        elif link.hostname != body.hostname:
            raise _refusal(409, "Disconnect first to change the hostname.")

    await asyncio.to_thread(update_links, user.runtime_paths, connect)


@router.post("/agents/{agent_name}/login", status_code=204)
async def request_login(agent_name: str, person: _Changing) -> None:
    """Have the gateway get the link's node a fresh sign-in URL."""

    def login(book: LinkBook) -> None:
        _own_link(book, person.requester_id, agent_name).login_after = time.time()

    await asyncio.to_thread(update_links, person.user.runtime_paths, login)


@router.delete("/agents/{agent_name}", status_code=204)
async def disconnect_agent(agent_name: str, person: _Changing) -> None:
    """Disconnect the link: its sessions close and the gateway logs its node out.

    With an operator key the link is parked ``off``, so the gateway does not add it again; the stored mode decides,
    so a disconnect right after a primary restart parks too.
    """

    def disconnect(book: LinkBook) -> None:
        link = _own_link(book, person.requester_id, agent_name)
        park = book.enrollment == "auth_key"
        remove_links(book, lambda candidate: candidate is link, now=time.time(), park=park)

    await asyncio.to_thread(update_links, person.user.runtime_paths, disconnect)
