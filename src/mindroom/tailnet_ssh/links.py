"""List shell-reach links and connect to the exact dedicated worker."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

import aiohttp
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, NonNegativeInt

from mindroom.agent_cli.shell_contract import SHELL_FUNCTION
from mindroom.authorization import ReplyMembershipPendingError, is_sender_allowed_for_responder
from mindroom.bounded_bytes import ByteLimitExceededError, collect_bounded_bytes
from mindroom.logging_config import get_logger
from mindroom.requester_identity import resolve_human_requester_alias
from mindroom.runtime_resolution import resolve_agent_execution, resolve_agent_runtime
from mindroom.tailnet_ssh.projection import write_projection
from mindroom.tailnet_ssh.store import read_links, update_links
from mindroom.terminal.resolution import agent_terminals
from mindroom.tool_system.agent_tools import resolve_agent_tools
from mindroom.tool_system.metadata import offered_functions, resolve_tool_settings
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.sandbox_proxy import tool_placement, worker_call
from mindroom.tool_system.worker_routing import build_requester_execution_identity, normal_worker_key
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.backend_kinds import worker_backend_kind
from mindroom.workers.models import WorkerSpec
from mindroom.workers.runtime import lease_configured_primary_worker_manager

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.sandbox_proxy import WorkerCall
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget
    from mindroom.workers.models import WorkerSshEndpoint

    type ReadConfig = Callable[[], tuple[Config, RuntimePaths]]

logger = get_logger(__name__)

_NO_SHELL = "This agent has no shell tool."
# Long wake requests must not occupy the primary's default pool.
_CONNECT_EXECUTOR = ThreadPoolExecutor(max_workers=32, thread_name_prefix="mindroom-tailnet-ssh-connect")


class StatusReport(BaseModel):
    """Connection counts by link ID."""

    model_config = ConfigDict(extra="forbid", strict=True)

    open_connections: dict[str, NonNegativeInt]


@dataclass
class GatewayStatus:
    """Whole last activity report (monotonic time and active worker keys)."""

    report: tuple[float, frozenset[str]] = (0.0, frozenset())


class ConnectRefused(HTTPException):
    """An SSH connect was refused; the gateway receives only its safe detail."""


def _roster(  # noqa: C901 - keep the ordered route checks together
    config: Config,
    paths: RuntimePaths,
    memberships: AgentReplyMembershipIndex,
) -> dict[str, dict[str, ResolvedWorkerTarget]]:
    """Group admitted shell targets by worker key and then by tailnet login."""
    if not worker_backend_kind(paths).dedicated:
        return {}
    agents = sorted(name for name, agent in config.agents.items() if agent.tailnet_ssh)
    requesters = sorted(
        (resolve_human_requester_alias(requester, config, paths), login)
        for requester, login in config.tailnet_ssh.logins.items()
        if config.tailnet_ssh.authorized_keys.get(requester)
    )
    if not agents or not requesters:
        return {}
    tools = config_plugins(config, paths).tools
    shell = tools.get("shell")
    if shell is None:
        return {}
    placement = tool_placement(paths, tools)
    roster: dict[str, dict[str, ResolvedWorkerTarget]] = {}
    for agent in agents:
        agent_tools = resolve_agent_tools(config, agent, tools)
        entry = next((entry for entry in agent_tools.candidates() if entry.name == "shell"), None)
        if entry is None or not agent_tools.runs_in_worker("shell", placement):
            continue
        for requester, login in requesters:
            try:
                if not is_sender_allowed_for_responder(
                    requester,
                    agent,
                    None,
                    config,
                    paths,
                    memberships,
                    require_resolved_membership=True,
                ):
                    continue
            except ReplyMembershipPendingError:
                continue
            identity = build_requester_execution_identity(agent_name=agent, runtime_paths=paths, requester_id=requester)
            try:
                execution = resolve_agent_execution(agent, config, paths, identity)
            except ValueError:
                # The ordinary shell route can refuse an unresolved execution identity.
                continue
            target = execution.worker_target
            if (key := normal_worker_key(target)) is None:
                continue
            settings = resolve_tool_settings(
                "shell",
                paths,
                tools=tools,
                worker_target=target,
                allowed_shared_services=(
                    config.get_worker_grantable_credentials() if execution.execution_scope is not None else None
                ),
                tool_config_overrides=entry.tool_config_overrides,
            )
            functions, _ = offered_functions(shell, settings.values)
            if functions is not None and SHELL_FUNCTION in functions:
                roster.setdefault(key, {}).setdefault(login, target)
    return roster


def desired_links(
    config: Config,
    paths: RuntimePaths,
    memberships: AgentReplyMembershipIndex,
) -> list[dict[str, object]]:
    """List currently admitted routes without looking at or starting workers."""
    roster = _roster(config, paths, memberships)
    if not roster:
        return []

    rows: list[dict[str, object]] = []
    with update_links(paths) as book:
        for key, routes in sorted(roster.items()):
            link = book.ensure(key)
            rows.append({"id": link.id, "hostname": link.hostname, "allowed_logins": sorted(routes)})
    return rows


def record_status(paths: RuntimePaths, status: GatewayStatus, payload: StatusReport, *, monotonic: float) -> None:
    """Count activity for known IDs, including omitted links whose sessions are draining."""
    status.report = (
        monotonic,
        frozenset(link.worker_key for link in read_links(paths).links if payload.open_connections.get(link.id, 0) > 0),
    )


def _shell_call(config: Config, paths: RuntimePaths, worker_key: str, target: ResolvedWorkerTarget) -> WorkerCall:
    identity = target.execution_identity
    assert identity is not None
    assert identity.agent_name is not None
    terminals = agent_terminals(identity.agent_name, config, paths, identity, create=False)
    if terminals is None:
        raise ConnectRefused(409, _NO_SHELL)
    try:
        call = worker_call(terminals.stand_ins, SHELL_FUNCTION, start=False, include_execution_env=False)
    except (RuntimeError, OSError):
        raise ConnectRefused(503, "The agent's worker is unavailable.") from None
    if call is None:
        raise ConnectRefused(409, _NO_SHELL)
    if call.request.worker_key != worker_key:
        raise ConnectRefused(409, "This agent's shell runs in another worker.")
    return call


def _admit(  # noqa: C901 - keep the before/after wake and projection checks together
    read_config: ReadConfig,
    memberships: AgentReplyMembershipIndex,
    link_id: str,
    peer_login: str,
) -> tuple[WorkerSshEndpoint, WorkerCall]:
    config, paths = read_config()
    link = read_links(paths).by_id(link_id)
    if link is None:
        raise ConnectRefused(404, "No such link.")
    key = link.worker_key
    routes = _roster(config, paths, memberships).get(key, {})
    target = routes.get(peer_login)
    if target is None:
        raise ConnectRefused(403, "This login is not authorized for this link.")
    try:
        if (lease := lease_configured_primary_worker_manager(paths)) is None:
            raise ConnectRefused(503, "A dedicated worker backend is required.")
        with lease as manager:
            # Use a stable workspace for this worker, independent of the connecting peer.
            launch_target = next(iter(routes.values()))
            launch_agent = launch_target.routing_agent_name
            assert launch_agent is not None
            resolve_agent_runtime(launch_agent, config, paths, launch_target.execution_identity, create=True)
            ready = manager.ensure_worker(WorkerSpec(key, private_agent_names=target.private_agent_names))
    except (WorkerBackendError, OSError):
        raise ConnectRefused(503, "The worker did not start.") from None
    if ready.status != "ready":
        raise ConnectRefused(503, "The worker is not ready.")
    endpoint = ready.ssh_endpoint
    if ready.worker_key != key or endpoint is None:
        raise ConnectRefused(409, "This worker has no marked SSH endpoint.")

    # Don't build against an already stale snapshot or hold the ledger lock across a worker touch.
    snapshot, snapshot_paths = read_config()
    call = _shell_call(config, paths, key, launch_target) if snapshot is config and snapshot_paths == paths else None

    # Serialize the recheck and projection with other connects.
    with update_links(paths) as book:
        current, current_paths = read_config()
        if current_paths != paths or book.by_id(link_id) != link:
            raise ConnectRefused(409, "The worker runtime changed during wake.")
        routes = _roster(current, paths, memberships).get(key, {})
        if (current_target := routes.get(peer_login)) is None:
            raise ConnectRefused(403, "This login is not authorized for this link.")
        if (
            call is None
            or current is not config
            or current_target != target
            or next(iter(routes.values())) != launch_target
        ):
            raise ConnectRefused(409, "This link's shell route changed during wake.")
        handle = call.handle
        # Touching the worker changes its last-use timestamp, not its generation or addresses.
        if handle is None or replace(handle, last_used_at=ready.last_used_at) != ready:
            raise ConnectRefused(409, "This link's worker changed during wake.")
        try:
            write_projection(
                endpoint.projection_dir,
                link,
                (
                    public_key
                    for requester, login in sorted(current.tailnet_ssh.logins.items())
                    if login in routes
                    for public_key in current.tailnet_ssh.authorized_keys[requester]
                ),
            )
        except OSError as error:
            logger.warning("tailnet_ssh_projection_not_written", link_id=link_id, error=str(error))
            raise ConnectRefused(503, "The worker's SSH files could not be written.") from None
    return endpoint, call


async def _start_sshd(call: WorkerCall) -> None:
    try:
        async with (
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as client,
            client.post(
                f"{call.api_root}/ssh/start",
                headers=call.headers,
                json={"request": call.request.model_dump(mode="json")},
                allow_redirects=False,
            ) as response,
        ):
            await collect_bounded_bytes(response.content.iter_chunked(16384), max_bytes=16384)
            status = response.status
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
    try:
        args = (read_config, membership_index, link_id, peer_login)
        loop = asyncio.get_running_loop()
        endpoint, call = await loop.run_in_executor(_CONNECT_EXECUTOR, copy_context().run, _admit, *args)
        await _start_sshd(call)
    except ConnectRefused as refused:
        logger.info("tailnet_ssh_connect_refused", **peer, status_code=refused.status_code)
        raise
    logger.info("tailnet_ssh_connect", **peer)
    return endpoint.host, endpoint.port


def active_worker_keys(status: GatewayStatus, *, monotonic: float) -> frozenset[str]:
    """Return the workers with live SSH activity in a report no more than 90 seconds old."""
    received_at, keys = status.report
    return keys if monotonic - received_at <= 90 else frozenset()
