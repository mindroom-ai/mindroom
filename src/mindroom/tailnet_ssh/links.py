"""The tailnet SSH link service: what the gateway is told to run, what its reports change, and every connect.

The gateway is a pipe. It asks here which nodes to run (``desired_links``), says what it sees (``record_status``),
and asks for each accepted connection where to send it (``connect``), which is where a worker is woken and its sshd
started. What the gateway reports of its nodes is kept in memory only, in a ``GatewayStatus``, and is empty until its
first post; how it enrols them is stored with the links, so it is known again as soon as the primary restarts.
"""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Literal

import aiohttp
import httpx
from pydantic import BaseModel

from mindroom.agent_cli.shell_contract import SHELL_FUNCTION, unbound_agent_cli_shell_env
from mindroom.bounded_bytes import ByteLimitExceededError, collect_bounded_bytes
from mindroom.logging_config import get_logger
from mindroom.runtime_resolution import resolve_agent_execution
from mindroom.shared_directories.store import load_shared_directories
from mindroom.tailnet_ssh.eligibility import link_ineligibility
from mindroom.tailnet_ssh.projection import authorized_key_lines, signin_keys, write_projection
from mindroom.tailnet_ssh.store import (
    close_sessions_of_gone_keys,
    default_hostname,
    finish_removal,
    new_link,
    read_links,
    remove_links,
    update_links,
)
from mindroom.terminal.resolution import agent_terminals
from mindroom.tool_system.sandbox_proxy import sandbox_proxy_config, worker_call, worker_proxy_client_config
from mindroom.tool_system.worker_proxy_client import worker_request_headers
from mindroom.tool_system.worker_routing import (
    build_requester_execution_identity,
    normalize_worker_key_part,
    user_agent_worker_key_requester,
    worker_key_agent_name,
)
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.models import WorkerSpec, worker_api_root
from mindroom.workers.runtime import lease_configured_primary_worker_manager

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.shared_directories.store import SharedDirectory
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
_STOP_TIMEOUT_SECONDS = 10.0
_MAX_ANSWER_BYTES = 16384
_NO_SSH_PORT = (
    "This worker has no SSH port; with Docker, MINDROOM_DOCKER_WORKER_PUBLISH_HOST must not be a wildcard address."
)
_NO_SHELL = "This agent has no shell tool."
_MEMBERSHIP_PENDING = "Whether you may use this agent is not known yet; try again in a few seconds."
# A connect may wait as long as the worker ready timeout (600 s hosted) for its worker to start. Those waits get
# threads of their own, so a few sleeping or failing workers cannot fill the default pool the rest of the primary
# shares; a connect beyond this many waits for a free connect thread.
_CONNECT_EXECUTOR = ThreadPoolExecutor(max_workers=32, thread_name_prefix="mindroom-tailnet-ssh-connect")


class LinkReport(BaseModel):
    """What the gateway sees of one link's node, as its ``POST /status`` says it."""

    state: Literal["needs_login", "needs_approval", "running", "removed"]
    login_url: str | None = None
    tailnet: str | None = None
    dns_name: str | None = None
    login_name: str | None = None
    key_expiry: str | None = None
    open_connections: int = 0
    last_error: str | None = None


class StatusReport(BaseModel):
    """The gateway's ``POST /status`` body: how its nodes enrol, and every node it holds by link id."""

    enrollment: Literal["login", "auth_key"]
    links: dict[str, LinkReport]


@dataclass(frozen=True)
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


@dataclass(frozen=True)
class _GatewayReport:
    """One report of the gateway, whole: when it came (monotonic), each link's node, and how the gateway enrols.

    ``enrollment`` is None until the gateway reports in this primary session.
    """

    received_at: float = 0.0
    links: Mapping[str, LinkStatus] = field(default_factory=dict)
    enrollment: Literal["login", "auth_key"] | None = None


@dataclass
class GatewayStatus:
    """The gateway's last report, and when an operator key's links were last added.

    A report comes in on a worker thread while the event loop reads the last one. So ``report`` is replaced whole and
    never changed, and whoever reads more than one of its fields takes it once.
    """

    report: _GatewayReport = field(default_factory=_GatewayReport)
    operator_sync_at: float = 0.0


class ConnectRefused(Exception):  # noqa: N818 - a refusal the gateway is answered with, not a failure
    """Why a connection gets no worker; the gateway keeps ``detail`` as the link's last error."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _resolve_target(
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


def _allowed_login(config: Config, enrollment: Literal["login", "auth_key"] | None, link: Link) -> str | None:
    """Return the one Tailscale login the link admits, or None when it admits nobody.

    With an operator key that is the login the config names for the person, else the login that enrolled the node.
    """
    if enrollment == "auth_key":
        return config.tailnet_ssh.logins.get(link.requester_id)
    return link.enrolled_login


def _desired(
    config: Config,
    runtime_paths: RuntimePaths,
    membership_index: AgentReplyMembershipIndex,
    link: Link,
    *,
    now: float,
    reported: bool,
    node: LinkStatus | None,
    directories: Sequence[SharedDirectory],
) -> Literal["on", "suspended", "removing"]:
    """Return what the gateway is to do with a link stored ``on``, whose node a fresh report shows as ``node``.

    It is removed when its agent is gone, its person may no longer use the agent, its worker is no longer the one
    that person's turns run in, or nobody enrolled it within a day and it is not waiting for an admin's approval;
    failing any other condition suspends it, as does room membership that is not known yet. Whether a node waits for
    approval only a fresh report says, so without one (``reported`` false) no link is dropped for its age.
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
        directories,
    )
    # A node waiting for an admin's approval was signed in to, however long the approval takes.
    expires = reported and link.enrolled_at is None and (node is None or node.state != "needs_approval")
    if (
        (ineligible is not None and ineligible.reason == "not_authorized" and not ineligible.pending)
        or _resolve_target(config, runtime_paths, link.requester_id, link.agent_name).worker_key != link.worker_key
        or (expires and now - link.created_at >= _UNENROLLED_SECONDS)
    ):
        return "removing"
    return "on" if ineligible is None else "suspended"


def listed_workers(runtime_paths: RuntimePaths) -> list[WorkerHandle]:
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
    monotonic: float,
) -> list[dict[str, object]]:
    """Bring the stored links in line with the config, and return the gateway's ``GET /links`` rows. Blocking.

    With an operator key, at most once a minute, every worker that serves SSH and has no link gets one, unless the
    link would be removed at once; one that is not eligible now is added and suspended. Only a report of this primary
    session tells an operator key: the stored mode may be one the gateway left while the primary restarted. A link its person parked as
    ``off`` is a link, so it is not added again. A key gone from a person's keys, as when the operator removes one
    from the config, closes their open sessions. A link it removes leaves its worker's sshd admitting nobody, and
    stopped, once the store is updated (``close_removed_links``).
    """
    workers: list[WorkerHandle] = []
    # The backend and the shared directories are read outside the store's lock, which connects and the portal wait for.
    report = status.report
    if report.enrollment == "auth_key" and now - status.operator_sync_at >= _OPERATOR_SYNC_SECONDS:
        status.operator_sync_at = now
        workers = listed_workers(runtime_paths)
    directories = load_shared_directories(runtime_paths)
    reported = _is_fresh(report, monotonic=monotonic)
    nodes = report.links if reported else {}
    agent_names: dict[str, str | None] = {}
    for name in config.agents:
        part = normalize_worker_key_part(name)
        # A key part that two configured names give is no one agent's.
        agent_names[part] = None if part in agent_names else name

    def desired(link: Link) -> Literal["on", "suspended", "removing"]:
        node = nodes.get(link.id)
        return _desired(
            config,
            runtime_paths,
            membership_index,
            link,
            now=now,
            reported=reported,
            node=node,
            directories=directories,
        )

    def reconcile(book: LinkBook) -> tuple[list[dict[str, object]], list[Link]]:
        for handle in workers if book.enrollment == "auth_key" else []:
            requester_id = user_agent_worker_key_requester(handle.worker_key)
            agent_name = agent_names.get(worker_key_agent_name(handle.worker_key) or "")
            if handle.ssh_endpoint is None or requester_id is None or agent_name is None:
                continue
            if book.link(requester_id, agent_name) is None:
                link = new_link(requester_id, agent_name, handle.worker_key, default_hostname(agent_name), now=now)
                if desired(link) != "removing":
                    book.links.append(link)
        people = {link.requester_id for link in book.links}
        close_sessions_of_gone_keys(
            book,
            {person: signin_keys(config, book, person).keys() for person in people},
            now=now,
        )
        verdicts = {link.id: desired(link) for link in book.links if link.desired == "on"}
        removed = remove_links(book, lambda link: verdicts.get(link.id) == "removing", now=now)
        rows = [
            {
                "id": link.id,
                "hostname": link.hostname,
                "desired": verdicts.get(link.id, "removing"),
                "allowed_login": _allowed_login(config, book.enrollment, link),
                "closed_before": link.closed_before,
                "login_after": link.login_after,
            }
            for link in book.links
            if link.desired != "off"
        ]
        return rows, removed

    rows, removed = update_links(runtime_paths, reconcile)
    close_removed_links(runtime_paths, removed)
    return rows


def record_status(
    runtime_paths: RuntimePaths,
    status: GatewayStatus,
    payload: StatusReport,
    *,
    now: float,
    monotonic: float,
) -> None:
    """Keep the gateway's ``POST /status`` report in ``status``, whole and at once, and store what it settles. Blocking.

    How the gateway enrols its nodes is stored. A node reported ``removed`` ends its link's removal. The first
    ``running`` report enrols a link: the login that signed in is the one it admits from then on, whoever signs in
    later. A node an operator key enrolled pins no login, so it admits nobody except through the config. A report
    for a link the store does not hold is ignored.
    """
    operator = payload.enrollment == "auth_key"

    def settle(book: LinkBook) -> dict[str, LinkStatus]:
        book.enrollment = payload.enrollment
        reported: dict[str, LinkStatus] = {}
        for link_id, report in payload.links.items():
            link = book.by_id(link_id)
            if link is None:
                continue
            if report.state == "removed":
                finish_removal(book, link_id)
                continue
            login = None if operator else report.login_name
            if report.state == "running" and link.enrolled_at is None and (operator or login):
                link.enrolled_login, link.enrolled_at = login, now
            reported[link_id] = LinkStatus(
                state=report.state,
                login_url=report.login_url,
                tailnet=report.tailnet,
                dns_name=report.dns_name,
                login_name=report.login_name,
                key_expiry=report.key_expiry,
                open_connections=report.open_connections,
                last_error=report.last_error,
                worker_key=link.worker_key,
            )
        return reported

    status.report = _GatewayReport(monotonic, update_links(runtime_paths, settle), payload.enrollment)


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


def _on_link(book: LinkBook, link_id: str) -> Link:
    """Return the book's link with this id, which must be on."""
    link = book.by_id(link_id)
    if link is None:
        raise ConnectRefused(404, "No such link.")
    if link.desired != "on":
        raise ConnectRefused(409, "This link is not connected.")
    return link


def _projected[T](runtime_paths: RuntimePaths, project: Callable[[LinkBook], T]) -> T:
    """Write sshd's files from the stored book, the read and the write as one step under the store's lock.

    Every write of those files goes through here, and keys change under the same lock. So the files written last
    hold the keys stored at that time, and a connect that read a key cannot write it after its removal was written.
    """
    return update_links(runtime_paths, project)


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
    link_id: str,
    peer_login: str,
) -> tuple[WorkerSshEndpoint, WorkerCall]:
    """Check a connection, wake the link's worker, and write sshd's files; all that precedes starting sshd. Blocking."""
    book = read_links(runtime_paths)
    link = _on_link(book, link_id)
    allowed_login = _allowed_login(config, book.enrollment, link)
    if not allowed_login or peer_login != allowed_login:
        raise ConnectRefused(403, "This link admits another Tailscale login.")
    ineligible = link_ineligibility(
        config,
        runtime_paths,
        link.requester_id,
        link.agent_name,
        link.worker_key,
        membership_index,
    )
    if ineligible is not None and ineligible.pending:
        raise ConnectRefused(503, _MEMBERSHIP_PENDING)
    if ineligible is not None:
        raise ConnectRefused(409, ineligible.reason)
    target = _resolve_target(config, runtime_paths, link.requester_id, link.agent_name)
    if target.worker_key != link.worker_key:
        raise ConnectRefused(409, "This link's worker is no longer the agent's.")
    endpoint = _ready_endpoint(runtime_paths, target, link.worker_key)

    def project(book: LinkBook) -> Link:
        # The worker may have taken minutes to start: sshd gets the keys stored now, and none for a link removed since.
        current = _on_link(book, link_id)
        try:
            write_projection(endpoint.projection_dir, current, authorized_key_lines(config, book, current.requester_id))
        except OSError as error:
            # The error names the primary's path to the worker's files, and the refusal reaches the card.
            logger.warning("tailnet_ssh_projection_not_written", link_id=link_id, error=str(error))
            raise ConnectRefused(503, "The worker's SSH files could not be written.") from None
        return current

    return endpoint, _shell_call(config, runtime_paths, _projected(runtime_paths, project))


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
    link_id: str,
    peer_login: str,
    peer_node: str,
) -> tuple[str, int]:
    """Return the host and port of the sshd that serves one connection the gateway accepted, or raise ``ConnectRefused``.

    In order: the link is on and admits the peer's login, it is eligible now, its worker starts and has an SSH port,
    sshd's files are written, and the runner starts sshd. It may take as long as a worker takes to start. Each
    connect and each refusal is logged with the link and the peer.
    """
    admit = partial(_admit, config, runtime_paths, membership_index, link_id, peer_login)
    try:
        endpoint, call = await asyncio.get_running_loop().run_in_executor(_CONNECT_EXECUTOR, copy_context().run, admit)
        await _start_sshd(call)
    except ConnectRefused as refused:
        logger.info(
            "tailnet_ssh_connect_refused",
            link_id=link_id,
            peer_login=peer_login,
            peer_node=peer_node,
            status_code=refused.status_code,
        )
        raise
    logger.info("tailnet_ssh_connect", link_id=link_id, peer_login=peer_login, peer_node=peer_node)
    return endpoint.host, endpoint.port


def refresh_projections(config: Config, runtime_paths: RuntimePaths, requester_id: str) -> None:
    """Rewrite sshd's files in each of the person's workers that serves SSH, after their keys changed. Blocking.

    A worker that is not listed with an SSH port gets them at its next connect, before its sshd starts. The worker of a
    link being removed or parked gets none: its removal left it admitting nobody.
    Call it after the keys are stored: the workers are listed first, so one a connect made ready before is among them.
    """
    endpoints = {handle.worker_key: handle.ssh_endpoint for handle in listed_workers(runtime_paths)}

    def project(book: LinkBook) -> None:
        key_lines = authorized_key_lines(config, book, requester_id)
        for link in book.links:
            endpoint = endpoints.get(link.worker_key)
            if link.requester_id == requester_id and link.desired == "on" and endpoint is not None:
                write_projection(endpoint.projection_dir, link, key_lines)

    _projected(runtime_paths, project)


def _stop_sshd(runtime_paths: RuntimePaths, link_id: str, handle: WorkerHandle) -> None:
    """Have the worker's runner stop sshd, as a shell call reaches it. Blocking; a failure is logged, never raised."""
    try:
        config = worker_proxy_client_config(sandbox_proxy_config(runtime_paths), runtime_paths)
        response = httpx.post(
            f"{worker_api_root(handle)}/ssh/stop",
            headers=worker_request_headers(handle, config=config),
            timeout=_STOP_TIMEOUT_SECONDS,
            follow_redirects=False,
            # A worker is reached directly, never through the primary's egress proxy.
            trust_env=False,
        )
    except (httpx.HTTPError, RuntimeError) as error:
        # The error's words name the worker's address.
        logger.warning("tailnet_ssh_sshd_not_stopped", link_id=link_id, error_type=type(error).__name__)
        return
    if response.status_code != 204:
        logger.warning("tailnet_ssh_sshd_not_stopped", link_id=link_id, status_code=response.status_code)


def close_removed_links(runtime_paths: RuntimePaths, removed: Sequence[Link]) -> None:
    """Leave the worker of each link ``remove_links`` returned admitting nobody, then have its runner stop sshd. Blocking.

    Call it once the removal is stored, never under the store's lock. The empty ``authorized_keys`` is written under
    that lock, so no connect or key change writes keys after it; the stop is best effort, as the empty file already
    admits nobody. A worker that is not listed with an SSH port is left alone: its sshd does not run, and only a connect
    of a link that is on starts it again, which writes the keys first. A worker a link that is on serves by now keeps
    its keys and sshd.
    """
    if not removed:
        return
    handles = {handle.worker_key: handle for handle in listed_workers(runtime_paths)}

    def clear(book: LinkBook) -> list[tuple[str, WorkerHandle]]:
        served = {link.worker_key for link in book.links if link.desired == "on"}
        cleared: list[tuple[str, WorkerHandle]] = []
        for link in removed:
            handle = handles.get(link.worker_key)
            if handle is None or handle.ssh_endpoint is None or link.worker_key in served:
                continue
            try:
                write_projection(handle.ssh_endpoint.projection_dir, link, ())
            except OSError as error:
                logger.warning("tailnet_ssh_keys_not_cleared", link_id=link.id, error=str(error))
            cleared.append((link.id, handle))
        return cleared

    for link_id, handle in _projected(runtime_paths, clear):
        _stop_sshd(runtime_paths, link_id, handle)


def _is_fresh(report: _GatewayReport, *, monotonic: float) -> bool:
    """Return whether the gateway reported in this primary session, recently enough to say what its nodes are."""
    return report.enrollment is not None and monotonic - report.received_at <= _STATUS_SECONDS


def fresh_link_statuses(report: _GatewayReport, *, monotonic: float) -> Mapping[str, LinkStatus]:
    """Return what a gateway report says of each link's node, or nothing when the report is too old to say it."""
    return report.links if _is_fresh(report, monotonic=monotonic) else {}


def active_worker_keys(status: GatewayStatus, *, monotonic: float) -> frozenset[str]:
    """Return the workers with an open SSH connection, by the gateway's last report unless it is too old."""
    nodes = fresh_link_statuses(status.report, monotonic=monotonic)
    return frozenset(node.worker_key for node in nodes.values() if node.open_connections > 0)
