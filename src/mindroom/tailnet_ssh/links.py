"""Discover tailnet SSH links, reconcile access, and connect to the exact worker."""

from __future__ import annotations

import asyncio
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

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
from mindroom.tool_system import worker_routing
from mindroom.tool_system.sandbox_proxy import worker_call, worker_runner_address
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.models import WorkerSpec
from mindroom.workers.runtime import lease_configured_primary_worker_manager

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tailnet_ssh.store import Link, LinkBook
    from mindroom.tool_system.sandbox_proxy import WorkerCall
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget
    from mindroom.workers.models import WorkerHandle, WorkerSshEndpoint

    type ReadConfig = Callable[[], tuple[Config, RuntimePaths]]

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


def _resolve_target(config: Config, paths: RuntimePaths, requester: str, agent: str) -> ResolvedWorkerTarget:
    identity = worker_routing.build_requester_execution_identity(
        agent_name=agent,
        runtime_paths=paths,
        requester_id=requester,
    )
    return resolve_agent_execution(agent, config, paths, execution_identity=identity).worker_target


def _worker_key(paths: RuntimePaths, requester_id: str, agent_name: str) -> str:
    # Derive from the ledger pair even after the agent is deleted or its scope changes.
    identity = worker_routing.build_requester_execution_identity(
        agent_name=agent_name,
        runtime_paths=paths,
        requester_id=requester_id,
    )
    key = worker_routing.resolve_worker_key("user_agent", identity)
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


def _candidate_pairs(handles: Mapping[str, WorkerHandle]) -> set[tuple[str, str]]:
    return {
        (agent, requester)
        for key in handles
        if (agent := worker_routing.worker_key_agent_name(key))
        and (requester := worker_routing.user_agent_worker_key_requester(key))
    }


def _sync_keys(link: Link, keys: dict[str, str], handle: WorkerHandle | None, cleared: list[WorkerHandle]) -> None:
    """Refresh an existing admission, or clear it and request a stop. Connect alone enables admission."""
    if handle is None or handle.ssh_endpoint is None:
        return
    projection = handle.ssh_endpoint.projection_dir
    try:
        old = (projection / "authorized_keys").read_bytes().strip()
    except FileNotFoundError:
        return
    if not old:
        return
    if not keys:
        clear_authorized_keys(projection)
        cleared.append(handle)
        return
    lines = authorized_key_lines(keys)
    if old != "\n".join(lines).encode():
        write_projection(projection, link, lines)


def _reconcile(
    read_config: ReadConfig,
    paths: RuntimePaths,
    memberships: AgentReplyMembershipIndex,
    status: GatewayStatus,
    monotonic: float,
) -> tuple[list[dict[str, object]], list[WorkerHandle]]:
    config, _ = read_config()
    discover = monotonic - status.discovered_at >= 60
    listed = _list_workers(paths) if discover or read_links(paths).links else None
    handles = {h.worker_key: h for h in listed or [] if h.ssh_endpoint is not None}
    cleared: list[WorkerHandle] = []

    def reconcile(book: LinkBook) -> list[dict[str, object]]:
        # Don't use a list taken before a different config opened or revoked a connection.
        current, current_paths = read_config()
        if current is not config or paths != current_paths:
            raise ConnectRefused(503, "Configuration changed; retry the link list.")
        directories = load_shared_directories(paths)
        stored = {(link.agent_name, link.requester_id): link for link in book.links}
        reserved = {link.hostname for link in book.links}
        candidates = set()
        if listed is not None and monotonic - status.discovered_at >= 60:
            candidates = _candidate_pairs(handles) - stored.keys()
            status.discovered_at = monotonic
        rows = []
        for agent, requester in (*stored, *sorted(candidates)):
            link = stored.get((agent, requester))
            key = _worker_key(paths, requester, agent)
            # Only the exact normal key is a discovery candidate; compute eligibility once per pair.
            denied = link_ineligibility(config, paths, requester, agent, key, memberships, directories)
            if link is None:
                if denied is not None or key not in handles:
                    continue
                link = new_link(requester, agent, reserved)
                book.links.append(link)
                reserved.add(link.hostname)
            if agent not in config.agents:
                book.retiring_ids.add(link.id)
            retiring = link.id in book.retiring_ids
            login = config.tailnet_ssh.logins.get(requester, "")
            eligible = not retiring and denied is None
            allowed_login = login if eligible and login.strip() else None
            keys = signin_keys(config, requester) if eligible else {}
            try:
                _sync_keys(link, keys if allowed_login else {}, handles.get(key), cleared)
            except OSError as error:
                logger.warning("tailnet_ssh_projection_failed", link_id=link.id, error=str(error))
                continue
            if eligible or (retiring and listed is not None):
                revision = (
                    hashlib.sha256(json.dumps([allowed_login, sorted(keys)]).encode()).hexdigest() if eligible else ""
                )
                rows.append(
                    {
                        "id": link.id,
                        "hostname": link.hostname,
                        "desired": "on" if eligible else "removing",
                        "allowed_login": allowed_login,
                        "access_revision": revision,
                    },
                )
        return rows

    return update_links(paths, reconcile), cleared


async def desired_links(
    read_config: ReadConfig,
    paths: RuntimePaths,
    memberships: AgentReplyMembershipIndex,
    status: GatewayStatus,
    *,
    monotonic: float,
) -> list[dict[str, object]]:
    """Publish lists after projection sync, without waiting on best-effort runner stops."""
    rows, cleared = await asyncio.to_thread(_reconcile, read_config, paths, memberships, status, monotonic)
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
    status.report = (
        monotonic,
        frozenset(
            _worker_key(paths, link.requester_id, link.agent_name)
            for link in book.links
            if link.id not in book.retiring_ids and payload.open_connections.get(link.id, 0) > 0
        ),
    )


def _ready_worker(runtime_paths: RuntimePaths, target: ResolvedWorkerTarget) -> WorkerHandle:
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
    return handle


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
    book: LinkBook,
    config: Config,
    paths: RuntimePaths,
    memberships: AgentReplyMembershipIndex,
    link_id: str,
    peer_login: str,
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
        detail = "Agent membership is not yet known; retry shortly." if denied.pending else denied.reason
        raise ConnectRefused(503 if denied.pending else 409, detail)
    target = _resolve_target(config, paths, link.requester_id, link.agent_name)
    if target.worker_key != key:
        raise ConnectRefused(409, "This link's worker is no longer the agent's.")
    lines = authorized_key_lines(signin_keys(config, link.requester_id))
    if not lines:
        raise ConnectRefused(409, "No SSH public keys are configured for this requester.")
    return link, target, lines


def _admit(
    read_config: ReadConfig,
    memberships: AgentReplyMembershipIndex,
    link_id: str,
    peer_login: str,
) -> tuple[WorkerSshEndpoint, WorkerCall]:
    config, paths = read_config()

    link, target, _ = _admission(read_links(paths), config, paths, memberships, link_id, peer_login)
    ready = _ready_worker(paths, target)

    def project(book: LinkBook) -> tuple[WorkerSshEndpoint, WorkerCall]:
        # A slow wake can span a committed config replacement. Recheck while serialized with revocation.
        current, current_paths = read_config()
        if current_paths != paths:
            raise ConnectRefused(409, "The worker runtime changed during wake.")
        current_link, current_target, lines = _admission(book, current, paths, memberships, link_id, peer_login)
        if current_link != link or current_target != target:
            raise ConnectRefused(409, "This link's worker changed during wake.")
        call = _shell_call(current, paths, current_link, current_target)
        handle = call.handle
        # Touching the worker changes its last-use timestamp, not its generation or addresses.
        if handle is None or handle.ssh_endpoint is None or replace(handle, last_used_at=ready.last_used_at) != ready:
            raise ConnectRefused(409, "This link's worker changed during wake.")
        endpoint = handle.ssh_endpoint
        try:
            write_projection(endpoint.projection_dir, current_link, lines)
        except OSError as error:
            logger.warning("tailnet_ssh_projection_not_written", link_id=link_id, error=str(error))
            raise ConnectRefused(503, "The worker's SSH files could not be written.") from None
        return endpoint, call

    return update_links(paths, project)


async def _post_ssh(root: str, headers: Mapping[str, str], route: str, body: object, seconds: float) -> int:
    async with (
        aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=seconds)) as client,
        client.post(f"{root}/ssh/{route}", headers=headers, json=body, allow_redirects=False) as response,
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
    read_config: ReadConfig,
    membership_index: AgentReplyMembershipIndex,
    link_id: str,
    peer_login: str,
    peer_node: str,
) -> tuple[str, int]:
    """Wake and admit this peer, project current keys, and start sshd before returning its endpoint."""
    peer = {"link_id": link_id, "peer_login": peer_login, "peer_node": peer_node}
    loop = asyncio.get_running_loop()
    try:
        context = copy_context()
        args = (read_config, membership_index, link_id, peer_login)
        endpoint, call = await loop.run_in_executor(_CONNECT_EXECUTOR, context.run, _admit, *args)
        await _start_sshd(call)
    except ConnectRefused as refused:
        logger.info("tailnet_ssh_connect_refused", **peer, status_code=refused.status_code)
        raise
    logger.info("tailnet_ssh_connect", **peer)
    return endpoint.host, endpoint.port


async def _stop_sshd(runtime_paths: RuntimePaths, handle: WorkerHandle) -> None:
    worker = worker_routing.worker_log_id(handle.worker_key)
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
