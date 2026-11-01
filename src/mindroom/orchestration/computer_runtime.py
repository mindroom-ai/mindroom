"""Orchestrator-owned live authorization for isolated worker computers."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

import nio

from mindroom.api.app_state import ensure_app_state
from mindroom.authorization import ReplyMembershipPendingError, is_sender_allowed_for_responder
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.entity_resolution import MissingManagedEntityAccountError, entity_identity_registry
from mindroom.runtime_env_policy import WORKER_COMPUTER_ENABLED_ENV
from mindroom.tool_system.worker_routing import build_agent_toolkit_worker_target, build_tool_execution_identity
from mindroom.worker_computer.sessions import ComputerError, ComputerTarget
from mindroom.workers.backend_kinds import worker_backend_kind
from mindroom.workers.models import WorkerSpec
from mindroom.workers.runtime import configured_primary_worker_manager_identity

if TYPE_CHECKING:
    from collections.abc import Mapping

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
) -> ComputerTarget:
    """Require exact live room membership, responder access, and ordinary browser routing."""
    try:
        name = entity_identity_registry(config, runtime_paths).current_entity_name_for_user_id(
            agent_user_id,
            include_router=False,
        )
    except MissingManagedEntityAccountError:
        raise ComputerError(503, "Agent identities are not ready.") from None
    if name is None or name not in config.agents:
        raise ComputerError(403, "The selected user is not a current agent.")
    response = await client.joined_members(room_id)
    if not isinstance(response, nio.JoinedMembersResponse):
        raise ComputerError(503, "Authoritative room membership is unavailable.")
    joined = {member.user_id for member in response.members}
    if requester_id not in joined or agent_user_id not in joined:
        raise ComputerError(403, "Requester and agent must both be joined to this room.")
    await memberships.refresh(config, runtime_paths, client)
    try:
        allowed = is_sender_allowed_for_responder(
            requester_id,
            name,
            room_id,
            config,
            runtime_paths,
            memberships,
            require_resolved_membership=True,
        )
    except ReplyMembershipPendingError:
        raise ComputerError(503, "Responder membership policy is unresolved.") from None
    if not allowed:
        raise ComputerError(403, "Requester cannot use this agent.")
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

    # Match normal toolkit placement without adding heavy tool imports to API startup.
    from mindroom.tool_system.agent_tools import resolve_agent_tools  # noqa: PLC0415
    from mindroom.tool_system.plugins import config_plugins  # noqa: PLC0415
    from mindroom.tool_system.sandbox_proxy import tool_placement  # noqa: PLC0415

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
    identity = build_tool_execution_identity(
        channel="matrix",
        agent_name=name,
        runtime_paths=runtime_paths,
        requester_id=requester_id,
        room_id=room_id,
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
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
