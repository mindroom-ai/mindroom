"""Orchestrator-owned live authorization for isolated worker computers."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

import nio
from fastapi import HTTPException

from mindroom.api.app_state import ensure_app_state
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.room_agent_access import admit_room_agent, room_execution_identity
from mindroom.runtime_env_policy import WORKER_COMPUTER_ENABLED_ENV
from mindroom.tool_system.agent_tools import resolve_agent_tools
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.sandbox_proxy import tool_placement
from mindroom.tool_system.worker_routing import build_agent_toolkit_worker_target
from mindroom.worker_computer.sessions import ComputerError, ComputerTarget
from mindroom.workers.backend_kinds import worker_backend_kind
from mindroom.workers.models import WorkerSpec
from mindroom.workers.runtime import configured_primary_worker_manager_identity

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.bot import AgentBot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths


async def _authorize_computer(
    requester_id: str,
    room_id: str,
    agent_user_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    client: nio.AsyncClient,
    memberships: AgentReplyMembershipIndex,
    refresh_memberships: Callable[[bool], Awaitable[None]],
) -> ComputerTarget:
    """Require exact live room membership, responder access, and ordinary browser routing."""

    async def joined_members() -> frozenset[str]:
        response = await client.joined_members(room_id)
        if not isinstance(response, nio.JoinedMembersResponse):
            raise ComputerError(503, "Authoritative room membership is unavailable.")
        return frozenset(member.user_id for member in response.members)

    try:
        name = await admit_room_agent(
            requester_id,
            room_id,
            agent_user_id,
            joined_members,
            config=config,
            runtime_paths=runtime_paths,
            memberships=memberships,
            refresh_memberships=refresh_memberships,
        )
    except HTTPException as error:
        raise ComputerError(error.status_code, error.detail) from None
    return _resolve_target(requester_id, room_id, agent_user_id, name, config, runtime_paths)


def _computer_browser_provider(agent_name: str, config: Config, runtime_paths: RuntimePaths) -> str:
    """Return the one browser tool that gives this agent a worker computer, or raise the API's error."""
    if not worker_backend_kind(runtime_paths).dedicated:
        raise ComputerError(503, "Computer requires a dedicated Docker or Kubernetes worker backend.")
    providers = [
        provider
        for provider in ("browser", "browser_mcp")
        if config.agent_has_tool_at_execution_scope(agent_name, provider, "user_agent")
    ]
    if len(providers) != 1:
        raise ComputerError(409, "Computer requires exactly one browser provider and explicit user_agent worker scope.")
    provider = providers[0]

    # Match normal toolkit placement.
    tools = config_plugins(config, runtime_paths).tools
    if not resolve_agent_tools(config, agent_name, tools).runs_in_worker(
        provider,
        tool_placement(runtime_paths, tools),
    ):
        raise ComputerError(503, "Computer requires browser tools routed to the worker.")
    return provider


def worker_computer_provider(agent_name: str, config: Config, runtime_paths: RuntimePaths) -> str | None:
    """Return the browser tool that gives this agent a worker computer, or None when it has none."""
    if agent_name not in config.agents or not runtime_paths.env_flag(WORKER_COMPUTER_ENABLED_ENV):
        return None
    try:
        return _computer_browser_provider(agent_name, config, runtime_paths)
    except ComputerError:
        return None


def _resolve_target(
    requester_id: str,
    room_id: str,
    agent_user_id: str,
    name: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> ComputerTarget:
    _computer_browser_provider(name, config, runtime_paths)
    identity = room_execution_identity(name, requester_id, room_id, config, runtime_paths)
    target = build_agent_toolkit_worker_target(
        "user_agent",
        name,
        is_private=config.agents[name].private is not None,
        execution_identity=identity,
        runtime_paths=runtime_paths,
    )
    if target.worker_key is None:
        raise ComputerError(503, "Could not resolve an isolated computer worker.")
    manager_identity = configured_primary_worker_manager_identity(runtime_paths)
    fingerprint = hashlib.sha256((config.model_dump_json() + repr(manager_identity)).encode()).hexdigest()
    return ComputerTarget(
        requester_id,
        room_id,
        agent_user_id,
        WorkerSpec(target.worker_key, private_agent_names=target.private_agent_names),
        fingerprint,
    )


@dataclass
class ComputerRuntimeCoordinator:
    """Publish a narrow callable only while orchestrator entities are ready."""

    runtime_paths: RuntimePaths
    agent_reply_memberships: AgentReplyMembershipIndex
    refresh_agent_reply_memberships_when_needed: Callable[[bool], Awaitable[None]]
    api_enabled: bool = True

    def bind_if_ready(self, config: Config, bots: Mapping[str, AgentBot]) -> None:
        """Capture live clients within the orchestrator; routes never receive bots."""
        if not self.api_enabled:
            return
        if not self.runtime_paths.env_flag(WORKER_COMPUTER_ENABLED_ENV):
            self.unbind()
            return
        router_bot = bots.get(ROUTER_AGENT_NAME)
        if router_bot is None or router_bot.client is None or not router_bot.running:
            self.unbind()
            return

        async def authorize(requester_id: str, room_id: str, agent_user_id: str) -> ComputerTarget:
            if router_bot.client is None or not router_bot.running:
                raise ComputerError(503, "Computer authorization runtime is unavailable.")
            return await _authorize_computer(
                requester_id,
                room_id,
                agent_user_id,
                config=config,
                runtime_paths=self.runtime_paths,
                client=router_bot.client,
                memberships=self.agent_reply_memberships,
                refresh_memberships=self.refresh_agent_reply_memberships_when_needed,
            )

        from mindroom.api import config_lifecycle, main  # noqa: PLC0415
        from mindroom.api.computers import ComputerRuntime  # noqa: PLC0415

        state = ensure_app_state(main.app)
        state.computer_runtime = ComputerRuntime(
            authorize,
            config_lifecycle.require_api_state(main.app).snapshot.generation,
            config,
        )

    def unbind(self) -> None:
        """Revoke active capabilities before entity reload or orchestrator shutdown."""
        if not self.api_enabled:
            return
        from mindroom.api import main  # noqa: PLC0415
        from mindroom.api.computers import withdraw_computer_runtime  # noqa: PLC0415

        withdraw_computer_runtime(main.app)
