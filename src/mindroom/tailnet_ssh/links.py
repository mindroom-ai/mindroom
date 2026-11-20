"""The tailnet SSH link service: what the gateway is told to run, what its reports change, and every connect.

The gateway is a pipe. It asks here which nodes to run (``desired_links``), says what it sees (``record_status``),
and asks for each accepted connection where to send it (``connect``), which is where a worker is woken and its sshd
started. What the gateway reports is kept in memory only, in a ``GatewayStatus``, and is empty until its first post.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import aiohttp

from mindroom.agent_cli.shell_contract import SHELL_FUNCTION, unbound_agent_cli_shell_env
from mindroom.bounded_bytes import ByteLimitExceededError, collect_bounded_bytes
from mindroom.logging_config import get_logger
from mindroom.runtime_resolution import resolve_agent_execution
from mindroom.tailnet_ssh.eligibility import link_ineligibility
from mindroom.tailnet_ssh.projection import authorized_key_lines, write_projection
from mindroom.tailnet_ssh.store import (
    default_hostname,
    finish_removal,
    new_link,
    read_links,
    remove_links,
    update_links,
)
from mindroom.terminal.resolution import agent_terminals
from mindroom.tool_system.sandbox_proxy import worker_call
from mindroom.tool_system.worker_routing import (
    build_requester_execution_identity,
    normalize_worker_key_part,
    user_agent_worker_key_requester,
    worker_key_agent_name,
)
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.models import WorkerSpec
from mindroom.workers.runtime import lease_configured_primary_worker_manager

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tailnet_ssh.store import Link, LinkBook
    from mindroom.tool_system.sandbox_proxy import WorkerCall
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget
    from mindroom.workers.models import WorkerHandle, WorkerSshEndpoint

logger = get_logger(__name__)

# A link nobody signed in to is dropped after this long.
_UNENROLLED_SECONDS = 24 * 60 * 60.0
# How often an operator key's links are added for the workers that serve SSH.
_OPERATOR_SYNC_SECONDS = 60.0
# A gateway report older than this says nothing about open connections.
_STATUS_SECONDS = 90.0
_START_TIMEOUT_SECONDS = 30.0
_MAX_ANSWER_BYTES = 16384
_NO_SSH_PORT = (
    "This worker has no SSH port; with Docker, MINDROOM_DOCKER_WORKER_PUBLISH_HOST must not be a wildcard address."
)
_NO_SHELL = "This agent has no shell tool."


@dataclass
class LinkStatus:
    """What the gateway last reported of one link's node, and the worker of that link."""

    state: Literal["needs_login", "needs_approval", "running"]
    login_url: str | None
    tailnet: str | None
    dns_name: str | None
    login_name: str | None
    key_expiry: str | None
    open_connections: int
    last_error: str | None
    worker_key: str


@dataclass
class GatewayStatus:
    """The gateway's last report, when it came (monotonic), and when an operator key's links were last added."""

    enrollment: Literal["login", "auth_key"] | None = None
    received_at: float = 0.0
    links: dict[str, LinkStatus] = field(default_factory=dict)
    operator_sync_at: float = 0.0


class ConnectRefused(Exception):  # noqa: N818 - a refusal the gateway is answered with, not a failure
    """Why a connection gets no worker; the gateway keeps ``detail`` as the link's last error."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def resolve_target(
    config: Config,
    runtime_paths: RuntimePaths,
    requester_id: str,
    agent_name: str,
) -> ResolvedWorkerTarget:
    """Return the worker a turn of the agent for this person runs in, outside any conversation."""
    identity = build_requester_execution_identity(
        agent_name=agent_name,
        runtime_paths=runtime_paths,
        requester_id=requester_id,
    )
    return resolve_agent_execution(agent_name, config, runtime_paths, execution_identity=identity).worker_target


def _allowed_login(config: Config, status: GatewayStatus, link: Link) -> str | None:
    """Return the one Tailscale login the link admits, or None when it admits nobody.

    With an operator key that is the login the config names for the person, else the login that enrolled the node.
    """
    if status.enrollment == "auth_key":
        return config.tailnet_ssh.logins.get(link.requester_id)
    return link.enrolled_login


def _desired(
    config: Config,
    runtime_paths: RuntimePaths,
    membership_index: AgentReplyMembershipIndex,
    link: Link,
    *,
    now: float,
) -> Literal["on", "suspended", "removing"]:
    """Return what the gateway is to do with a link stored ``on``.

    It is removed when its agent is gone, its person may no longer use the agent, its worker is no longer the one
    that person's turns run in, or nobody enrolled it within a day; failing any other condition suspends it.
    """
    if link.agent_name not in config.agents:
        return "removing"
    ineligible = link_ineligibility(
        config,
        runtime_paths,
        link.requester_id,
        link.agent_name,
        link.worker_key,
        membership_index,
    )
    if (
        (ineligible is not None and ineligible.reason == "not_authorized")
        or resolve_target(config, runtime_paths, link.requester_id, link.agent_name).worker_key != link.worker_key
        or (link.enrolled_at is None and now - link.created_at >= _UNENROLLED_SECONDS)
    ):
        return "removing"
    return "on" if ineligible is None else "suspended"


def _listed_workers(runtime_paths: RuntimePaths) -> list[WorkerHandle]:
    """Return the backend's workers, or none while it has no dedicated backend or the backend fails. Blocking."""
    try:
        lease = lease_configured_primary_worker_manager(runtime_paths)
        if lease is None:
            return []
        with lease as manager:
            return manager.list_workers()
    except WorkerBackendError as error:
        logger.warning("tailnet_ssh_workers_not_listed", error=str(error))
        return []


def desired_links(
    config: Config,
    runtime_paths: RuntimePaths,
    membership_index: AgentReplyMembershipIndex,
    status: GatewayStatus,
    *,
    now: float,
) -> list[dict[str, object]]:
    """Bring the stored links in line with the config, and return the gateway's ``GET /links`` rows. Blocking.

    With an operator key, at most once a minute, every worker that serves SSH and has no link gets one, if the link
    would be on; a link its person parked as ``off`` is a link, so it is not added again.
    """
    workers: list[WorkerHandle] = []
    if status.enrollment == "auth_key" and now - status.operator_sync_at >= _OPERATOR_SYNC_SECONDS:
        status.operator_sync_at = now
        workers = _listed_workers(runtime_paths)
    agent_names = {normalize_worker_key_part(name): name for name in config.agents}

    def desired(link: Link) -> Literal["on", "suspended", "removing"]:
        return _desired(config, runtime_paths, membership_index, link, now=now)

    def reconcile(book: LinkBook) -> list[dict[str, object]]:
        for handle in workers:
            requester_id = user_agent_worker_key_requester(handle.worker_key)
            agent_name = agent_names.get(worker_key_agent_name(handle.worker_key) or "")
            if handle.ssh_endpoint is None or requester_id is None or agent_name is None:
                continue
            if book.link(requester_id, agent_name) is None:
                link = new_link(requester_id, agent_name, handle.worker_key, default_hostname(agent_name), now=now)
                if desired(link) == "on":
                    book.links.append(link)
        verdicts = {link.id: desired(link) for link in book.links if link.desired == "on"}
        remove_links(book, lambda link: verdicts.get(link.id) == "removing", now=now)
        return [
            {
                "id": link.id,
                "hostname": link.hostname,
                "desired": verdicts.get(link.id, "removing"),
                "allowed_login": _allowed_login(config, status, link),
                "closed_before": link.closed_before,
                "login_after": link.login_after,
            }
            for link in book.links
            if link.desired != "off"
        ]

    return update_links(runtime_paths, reconcile)


def record_status(
    runtime_paths: RuntimePaths,
    status: GatewayStatus,
    payload: Mapping[str, Any],
    *,
    now: float,
    monotonic: float,
) -> None:
    """Keep the gateway's ``POST /status`` report in ``status``, and store what it settles. Blocking.

    A node reported ``removed`` ends its link's removal. The first ``running`` report enrols a link: the login that
    signed in is the one it admits from then on, whoever signs in later. A node an operator key enrolled pins no
    login, so it admits nobody except through the config. A report for a link the store does not hold is ignored.
    """
    operator = payload["enrollment"] == "auth_key"

    def settle(book: LinkBook) -> dict[str, LinkStatus]:
        reported: dict[str, LinkStatus] = {}
        for link_id, report in payload["links"].items():
            link = book.by_id(link_id)
            if link is None:
                continue
            if report["state"] == "removed":
                finish_removal(book, link_id)
                continue
            login = None if operator else report.get("login_name")
            if report["state"] == "running" and link.enrolled_at is None and (operator or login):
                link.enrolled_login, link.enrolled_at = login, now
            reported[link_id] = LinkStatus(
                state=report["state"],
                login_url=report.get("login_url"),
                tailnet=report.get("tailnet"),
                dns_name=report.get("dns_name"),
                login_name=report.get("login_name"),
                key_expiry=report.get("key_expiry"),
                open_connections=report.get("open_connections", 0),
                last_error=report.get("last_error"),
                worker_key=link.worker_key,
            )
        return reported

    status.links = update_links(runtime_paths, settle)
    status.enrollment = payload["enrollment"]
    status.received_at = monotonic


def link_state(link: Link, status: LinkStatus | None, *, suspended: bool) -> str:
    """Return the state the card shows for a link that is not ``off``.

    Before the gateway's first report a link is what the store says of it: enrolled, or waiting for a sign-in.
    """
    if link.desired == "removing":
        return "removing"
    if suspended:
        return "suspended"
    enrolled = link.enrolled_at is not None
    if status is None:
        return "enrolled" if enrolled else "pending_login"
    if status.state == "running":
        return "enrolled"
    if status.state == "needs_approval":
        return "needs_approval"
    return "needs_login" if enrolled else "pending_login"


def _on_link(runtime_paths: RuntimePaths, link_id: str) -> tuple[LinkBook, Link]:
    """Return the stored book and its link with this id, which must be on."""
    book = read_links(runtime_paths)
    link = book.by_id(link_id)
    if link is None:
        raise ConnectRefused(404, "No such link.")
    if link.desired != "on":
        raise ConnectRefused(409, "This link is not connected.")
    return book, link


def _ready_endpoint(runtime_paths: RuntimePaths, target: ResolvedWorkerTarget, worker_key: str) -> WorkerSshEndpoint:
    """Start the worker as a tool call does, and return where its sshd would listen."""
    try:
        lease = lease_configured_primary_worker_manager(runtime_paths)
        if lease is None:
            raise ConnectRefused(503, "A dedicated worker backend is required.")
        with lease as manager:
            handle = manager.ensure_worker(WorkerSpec(worker_key, private_agent_names=target.private_agent_names))
    except WorkerBackendError:
        # The backend's own words may name its hosts and images, and the refusal reaches the card.
        raise ConnectRefused(503, "The worker did not start.") from None
    if handle.status != "ready":
        raise ConnectRefused(503, "The worker is not ready.")
    if handle.ssh_endpoint is None:
        raise ConnectRefused(409, _NO_SSH_PORT)
    return handle.ssh_endpoint


def _shell_call(config: Config, runtime_paths: RuntimePaths, link: Link) -> WorkerCall:
    """Return the call of the agent's shell for the link's person, as a terminal's is built, unsent."""
    identity = build_requester_execution_identity(
        agent_name=link.agent_name,
        runtime_paths=runtime_paths,
        requester_id=link.requester_id,
    )
    terminals = agent_terminals(link.agent_name, config, runtime_paths, identity, create=True)
    if terminals is None:
        raise ConnectRefused(409, _NO_SHELL)
    try:
        # A session outlives any response, so its shell gets no response's CLI grant.
        with unbound_agent_cli_shell_env():
            call = worker_call(terminals.stand_ins, SHELL_FUNCTION, start=False)
    except (RuntimeError, OSError):
        raise ConnectRefused(503, "The agent's worker is unavailable.") from None
    if call is None:
        raise ConnectRefused(409, _NO_SHELL)
    if call.request.worker_key != link.worker_key:
        raise ConnectRefused(409, "This agent's shell runs in another worker.")
    return call


def _admit(
    config: Config,
    runtime_paths: RuntimePaths,
    membership_index: AgentReplyMembershipIndex,
    status: GatewayStatus,
    link_id: str,
    peer_login: str,
) -> tuple[WorkerSshEndpoint, WorkerCall]:
    """Check a connection, wake the link's worker, and write sshd's files; all that precedes starting sshd. Blocking."""
    _, link = _on_link(runtime_paths, link_id)
    allowed_login = _allowed_login(config, status, link)
    if allowed_login is None or peer_login != allowed_login:
        raise ConnectRefused(403, "This link admits another Tailscale login.")
    ineligible = link_ineligibility(
        config,
        runtime_paths,
        link.requester_id,
        link.agent_name,
        link.worker_key,
        membership_index,
    )
    if ineligible is not None:
        raise ConnectRefused(409, ineligible.reason)
    target = resolve_target(config, runtime_paths, link.requester_id, link.agent_name)
    if target.worker_key != link.worker_key:
        raise ConnectRefused(409, "This link's worker is no longer the agent's.")
    endpoint = _ready_endpoint(runtime_paths, target, link.worker_key)
    # The worker may have taken minutes to start: sshd gets the keys stored now, and none for a link removed since.
    book, link = _on_link(runtime_paths, link_id)
    write_projection(endpoint.projection_dir, link, authorized_key_lines(config, book, link.requester_id))
    return endpoint, _shell_call(config, runtime_paths, link)


def _refusal_detail(answer: bytes) -> str:
    """Return the runner's reason for not starting sshd."""
    try:
        detail = json.loads(answer)["detail"]
    except (ValueError, LookupError, TypeError):
        detail = None
    return detail if isinstance(detail, str) else "The worker's SSH server did not start."


async def _start_sshd(call: WorkerCall) -> None:
    """Have the worker's runner start sshd for sessions of the shell ``call`` describes, unless it runs."""
    try:
        async with (
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=_START_TIMEOUT_SECONDS)) as client,
            client.post(
                f"{call.api_root}/ssh/start",
                headers=call.headers,
                json={"request": call.request.model_dump(mode="json")},
                allow_redirects=False,
            ) as response,
        ):
            answer = await collect_bounded_bytes(response.content.iter_chunked(65536), max_bytes=_MAX_ANSWER_BYTES)
    except (aiohttp.ClientError, TimeoutError, ByteLimitExceededError) as error:
        raise ConnectRefused(503, f"The worker's SSH server is unreachable: {type(error).__name__}") from None
    if response.status != 200:
        raise ConnectRefused(503, _refusal_detail(answer))


async def connect(
    config: Config,
    runtime_paths: RuntimePaths,
    membership_index: AgentReplyMembershipIndex,
    status: GatewayStatus,
    link_id: str,
    peer_login: str,
    peer_node: str,
) -> tuple[str, int]:
    """Return the host and port of the sshd that serves one connection the gateway accepted, or raise ``ConnectRefused``.

    In order: the link is on and admits the peer's login, it is eligible now, its worker starts and has an SSH port,
    sshd's files are written, and the runner starts sshd. It may take as long as a worker takes to start.
    """
    endpoint, call = await asyncio.to_thread(
        _admit,
        config,
        runtime_paths,
        membership_index,
        status,
        link_id,
        peer_login,
    )
    await _start_sshd(call)
    logger.info("tailnet_ssh_connect", link_id=link_id, peer_login=peer_login, peer_node=peer_node)
    return endpoint.host, endpoint.port


def refresh_projections(config: Config, runtime_paths: RuntimePaths, requester_id: str) -> None:
    """Rewrite sshd's files in each of the person's workers that serves SSH, after their keys changed. Blocking.

    A worker that is not listed with an SSH port gets them at its next connect, before its sshd starts.
    """
    endpoints = {handle.worker_key: handle.ssh_endpoint for handle in _listed_workers(runtime_paths)}
    book = read_links(runtime_paths)
    key_lines = authorized_key_lines(config, book, requester_id)
    for link in book.links:
        endpoint = endpoints.get(link.worker_key)
        if link.requester_id == requester_id and endpoint is not None:
            write_projection(endpoint.projection_dir, link, key_lines)


def active_worker_keys(status: GatewayStatus, *, monotonic: float) -> frozenset[str]:
    """Return the workers with an open SSH connection, by the gateway's last report unless it is too old."""
    if monotonic - status.received_at > _STATUS_SECONDS:
        return frozenset()
    return frozenset(link.worker_key for link in status.links.values() if link.open_connections > 0)
