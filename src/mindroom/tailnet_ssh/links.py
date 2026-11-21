"""Discover tailnet SSH links, reconcile access, and connect to the exact worker."""

from __future__ import annotations

import asyncio
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Literal

import aiohttp
from pydantic import BaseModel, ConfigDict, NonNegativeInt

from mindroom.agent_cli.shell_contract import SHELL_FUNCTION, unbound_agent_cli_shell_env
from mindroom.bounded_bytes import ByteLimitExceededError, collect_bounded_bytes
from mindroom.logging_config import get_logger
from mindroom.runtime_resolution import resolve_agent_execution
from mindroom.shared_directories.store import load_shared_directories
from mindroom.tailnet_ssh.eligibility import link_ineligibility
from mindroom.tailnet_ssh.projection import authorized_key_lines, clear_authorized_keys, signin_keys, write_projection
from mindroom.tailnet_ssh.store import new_link, read_links, update_links
from mindroom.terminal.resolution import agent_terminals
from mindroom.tool_system.sandbox_proxy import worker_call, worker_runner_address
from mindroom.tool_system.worker_routing import (
    build_requester_execution_identity,
    resolve_worker_key,
    user_agent_worker_key_requester,
    worker_key_agent_name,
    worker_log_id,
)
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.models import WorkerSpec
from mindroom.workers.runtime import lease_configured_primary_worker_manager

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.shared_directories.store import SharedDirectory
    from mindroom.tailnet_ssh.store import Link, LinkBook
    from mindroom.tool_system.sandbox_proxy import WorkerCall
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget
    from mindroom.workers.models import WorkerHandle, WorkerSshEndpoint

logger = get_logger(__name__)

_NO_SHELL = "This agent has no shell tool."
# Long wake requests must not occupy the primary's default pool.
_CONNECT_EXECUTOR = ThreadPoolExecutor(max_workers=32, thread_name_prefix="mindroom-tailnet-ssh-connect")
_STOPPING: set[asyncio.Future[list[None]]] = set()


class StatusReport(BaseModel):
    """Connection counts by link ID, and completed retirements."""

    model_config = ConfigDict(extra="forbid", strict=True)

    open_connections: dict[str, NonNegativeInt]
    removed: list[str]


@dataclass
class GatewayStatus:
    """Whole last activity report (monotonic time and active worker keys), and discovery timestamp."""

    report: tuple[float, frozenset[str]] = (0.0, frozenset())
    discovered_at: float = float("-inf")


class ConnectRefused(Exception):  # noqa: N818 - a refusal the gateway is answered with, not a failure
    """Why a connection gets no worker."""

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
    identity = build_requester_execution_identity(agent_name=agent_name, runtime_paths=runtime_paths, requester_id=requester_id)
    return resolve_agent_execution(agent_name, config, runtime_paths, execution_identity=identity).worker_target


def _worker_key(paths: RuntimePaths, requester_id: str, agent_name: str) -> str:
    # Derive from the ledger pair even after the agent is deleted or its scope changes.
    identity = build_requester_execution_identity(agent_name=agent_name, runtime_paths=paths, requester_id=requester_id)
    key = resolve_worker_key("user_agent", identity)
    assert key is not None
    return key


def _list_workers(runtime_paths: RuntimePaths) -> list[WorkerHandle] | None:
    try:
        lease = lease_configured_primary_worker_manager(runtime_paths)
        if lease is None:
            return []
        with lease as manager:
            return manager.list_workers()
    except WorkerBackendError as error:
        logger.warning("tailnet_ssh_workers_not_listed", error=str(error))
        return None


def _discover(
    book: LinkBook, handles: Mapping[str, WorkerHandle], config: Config, paths: RuntimePaths,
    memberships: AgentReplyMembershipIndex, directories: list[SharedDirectory],
) -> None:
    pairs = {(link.agent_name, link.requester_id) for link in book.links}
    reserved = {link.hostname for link in book.links}
    candidates = {}
    for key in handles:
        requester, agent = user_agent_worker_key_requester(key), worker_key_agent_name(key)
        if requester and agent and (agent, requester) not in pairs and key == _worker_key(paths, requester, agent):
            candidates[agent, requester] = key
    for (agent, requester), key in sorted(candidates.items()):
        if link_ineligibility(config, paths, requester, agent, key, memberships, directories) is None:
            link = new_link(requester, agent, reserved)
            book.links.append(link)
            reserved.add(link.hostname)


def _sync_keys(endpoint: WorkerSshEndpoint, link: Link, keys: dict[str, str]) -> bool:
    """Refresh an existing admission, or clear it and request a stop. Connect alone enables admission."""
    projection = endpoint.projection_dir
    try:
        old = (projection / "authorized_keys").read_bytes().strip()
    except FileNotFoundError:
        return False
    if not old:
        return False
    if not keys:
        clear_authorized_keys(projection)
        return True
    lines = authorized_key_lines(keys)
    if old != "\n".join(lines).encode():
        write_projection(projection, link, lines)
    return False


async def desired_links(
    read_config: Callable[[], tuple[Config, RuntimePaths]], paths: RuntimePaths,
    memberships: AgentReplyMembershipIndex, status: GatewayStatus, *, monotonic: float,
) -> list[dict[str, object]]:
    """Reconcile the ledger and send best-effort stops without blocking the response on runners."""

    def reconcile(book: LinkBook) -> list[dict[str, object]]:
        discover = monotonic - status.discovered_at >= 60
        listed = _list_workers(paths) if discover or book.links else []
        config, current_paths = read_config()
        if current_paths != paths:
            raise ConnectRefused(503, "The worker runtime changed; retry the link list.")
        handles = {h.worker_key: h for h in listed or [] if h.ssh_endpoint is not None}
        directories = load_shared_directories(paths)
        if discover and listed is not None:
            _discover(book, handles, config, paths, memberships, directories)
            status.discovered_at = monotonic
        rows = []
        for link in book.links:
            if link.agent_name not in config.agents:
                book.retiring_ids.add(link.id)
            retiring = link.id in book.retiring_ids
            key = _worker_key(paths, link.requester_id, link.agent_name)
            login = config.tailnet_ssh.logins.get(link.requester_id, "")
            keys = signin_keys(config, link.requester_id)
            if retiring or not login.strip() or link_ineligibility(
                config, paths, link.requester_id, link.agent_name, key, memberships, directories,
            ) is not None:
                keys = {}
            if (handle := handles.get(key)) is not None and handle.ssh_endpoint is not None:
                try:
                    if _sync_keys(handle.ssh_endpoint, link, keys):
                        cleared.append(handle)
                except OSError as error:
                    logger.warning("tailnet_ssh_projection_failed", link_id=link.id, error=str(error))
                    continue
            if keys:
                rev = hashlib.sha256(json.dumps([login, sorted(keys)]).encode()).hexdigest()
                rows.append({"id": link.id, "hostname": link.hostname, "desired": "on", "allowed_login": login, "access_revision": rev})
            elif retiring and listed is not None:
                rows.append({"id": link.id, "hostname": link.hostname, "desired": "removing", "allowed_login": None, "access_revision": ""})
        return rows

    cleared: list[WorkerHandle] = []
    rows = await asyncio.to_thread(update_links, paths, reconcile)
    if cleared:
        task = asyncio.gather(*(_stop_sshd(paths, h) for h in cleared))
        _STOPPING.add(task)
        task.add_done_callback(_STOPPING.discard)
    return rows


def record_status(paths: RuntimePaths, status: GatewayStatus, payload: StatusReport, *, monotonic: float) -> None:
    """Retire only acknowledged retiring IDs and publish one whole activity report for known active links."""
    def settle(book: LinkBook) -> LinkBook:
        removed = book.retiring_ids.intersection(payload.removed)
        book.links = [link for link in book.links if link.id not in removed]
        book.retiring_ids.difference_update(removed)
        return book

    book = update_links(paths, settle) if payload.removed else read_links(paths)
    status.report = (monotonic, frozenset(
        _worker_key(paths, link.requester_id, link.agent_name)
        for link in book.links
        if link.id not in book.retiring_ids and payload.open_connections.get(link.id, 0) > 0
    ))


def _ready_endpoint(runtime_paths: RuntimePaths, target: ResolvedWorkerTarget) -> WorkerSshEndpoint:
    worker_key = target.worker_key
    assert worker_key is not None
    try:
        lease = lease_configured_primary_worker_manager(runtime_paths)
        if lease is None:
            raise ConnectRefused(503, "A dedicated worker backend is required.")
        with lease as manager:
            handle = manager.ensure_worker(WorkerSpec(worker_key, private_agent_names=target.private_agent_names))
    except WorkerBackendError:
        raise ConnectRefused(503, "The worker did not start.") from None
    if handle.status != "ready":
        raise ConnectRefused(503, "The worker is not ready.")
    if handle.worker_key != worker_key or handle.ssh_endpoint is None:
        raise ConnectRefused(409, "This worker has no marked SSH endpoint.")
    return handle.ssh_endpoint


def _shell_call(config: Config, paths: RuntimePaths, link: Link, target: ResolvedWorkerTarget) -> WorkerCall:
    terminals = agent_terminals(link.agent_name, config, paths, target.execution_identity, create=True)
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
    if call.request.worker_key != target.worker_key:
        raise ConnectRefused(409, "This agent's shell runs in another worker.")
    return call


def _admission(
    config: Config, paths: RuntimePaths, memberships: AgentReplyMembershipIndex, book: LinkBook, link_id: str, peer_login: str,
) -> tuple[Link, ResolvedWorkerTarget, list[str]]:
    link = book.by_id(link_id)
    if link is None:
        raise ConnectRefused(404, "No such link.")
    if link.id in book.retiring_ids:
        raise ConnectRefused(409, "This link is retiring.")
    login = config.tailnet_ssh.logins.get(link.requester_id)
    if not login or not login.strip() or peer_login != login:
        raise ConnectRefused(403, "This link admits another Tailscale login.")
    key = _worker_key(paths, link.requester_id, link.agent_name)
    denied = link_ineligibility(config, paths, link.requester_id, link.agent_name, key, memberships)
    if denied is not None:
        if denied.pending:
            raise ConnectRefused(503, "Agent membership is not yet known; retry shortly.")
        raise ConnectRefused(409, denied.reason)
    target = _resolve_target(config, paths, link.requester_id, link.agent_name)
    if target.worker_key != key:
        raise ConnectRefused(409, "This link's worker is no longer the agent's.")
    lines = authorized_key_lines(signin_keys(config, link.requester_id))
    if not lines:
        raise ConnectRefused(409, "No SSH public keys are configured for this requester.")
    return link, target, lines


def _admit(
    read_config: Callable[[], tuple[Config, RuntimePaths]],
    memberships: AgentReplyMembershipIndex,
    link_id: str,
    peer_login: str,
) -> tuple[WorkerSshEndpoint, WorkerCall]:
    config, paths = read_config()
    link, target, _ = _admission(config, paths, memberships, read_links(paths), link_id, peer_login)
    endpoint = _ready_endpoint(paths, target)

    def project(book: LinkBook) -> WorkerCall:
        # A slow wake can span a committed config replacement. Recheck while serialized with revocation.
        current, current_paths = read_config()
        if current_paths != paths:
            raise ConnectRefused(409, "The worker runtime changed during wake.")
        current_link, current_target, lines = _admission(current, paths, memberships, book, link_id, peer_login)
        if current_link != link or current_target != target:
            raise ConnectRefused(409, "This link's worker changed during wake.")
        call = _shell_call(current, paths, current_link, current_target)
        try:
            write_projection(endpoint.projection_dir, current_link, lines)
        except OSError as error:
            logger.warning("tailnet_ssh_projection_not_written", link_id=link_id, error=str(error))
            raise ConnectRefused(503, "The worker's SSH files could not be written.") from None
        return call

    return endpoint, update_links(paths, project)


async def _post_ssh(
    api_root: str,
    headers: Mapping[str, str],
    operation: Literal["start", "stop"],
    body: object,
    *,
    seconds: float,
) -> int:
    async with (
        aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=seconds)) as client,
        client.post(f"{api_root}/ssh/{operation}", headers=headers, json=body, allow_redirects=False) as response,
    ):
        await collect_bounded_bytes(response.content.iter_chunked(16384), max_bytes=16384)
        return response.status


async def _start_sshd(call: WorkerCall) -> None:
    body = {"request": call.request.model_dump(mode="json")}
    try:
        status = await _post_ssh(call.api_root, call.headers, "start", body, seconds=30)
    except (aiohttp.ClientError, TimeoutError, ByteLimitExceededError) as error:
        raise ConnectRefused(503, f"The worker's SSH server is unreachable: {type(error).__name__}") from None
    if status != 200:
        raise ConnectRefused(503, "The worker's SSH server did not start.")


async def connect(
    read_config: Callable[[], tuple[Config, RuntimePaths]],
    membership_index: AgentReplyMembershipIndex,
    link_id: str,
    peer_login: str,
    peer_node: str,
) -> tuple[str, int]:
    """Wake and admit this peer, project current keys, and start sshd before returning its endpoint."""
    admit = partial(_admit, read_config, membership_index, link_id, peer_login)
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


async def _stop_sshd(runtime_paths: RuntimePaths, handle: WorkerHandle) -> None:
    worker = worker_log_id(handle.worker_key)
    try:
        api_root, headers = worker_runner_address(runtime_paths, handle)
        status = await _post_ssh(api_root, headers, "stop", None, seconds=10)
    except (aiohttp.ClientError, TimeoutError, ByteLimitExceededError, RuntimeError) as error:
        logger.warning("tailnet_ssh_sshd_not_stopped", worker=worker, error_type=type(error).__name__)
        return
    if status != 204:
        logger.warning("tailnet_ssh_sshd_not_stopped", worker=worker, status_code=status)


def active_worker_keys(status: GatewayStatus, *, monotonic: float) -> frozenset[str]:
    """Return the workers with live SSH activity in a report no more than 90 seconds old."""
    received_at, keys = status.report
    return keys if monotonic - received_at <= 90 else frozenset()
