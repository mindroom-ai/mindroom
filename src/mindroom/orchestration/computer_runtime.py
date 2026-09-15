"""Orchestrator-owned live authorization for isolated worker computers."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

import nio

from mindroom.authorization import ReplyMembershipPendingError, is_sender_allowed_for_responder
from mindroom.constants import ROUTER_AGENT_NAME, runtime_env_flag
from mindroom.entity_resolution import MissingManagedEntityAccountError, entity_identity_registry
from mindroom.tool_system.worker_routing import build_agent_toolkit_worker_target, build_tool_execution_identity
from mindroom.worker_computer.sessions import ComputerError, ComputerTarget
from mindroom.workers.models import WorkerSpec
from mindroom.workers.runtime import configured_primary_worker_manager_identity, primary_worker_backend_name

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.bot import AgentBot, TeamBot
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


def _resolve_target(
    requester_id: str,
    room_id: str,
    agent_user_id: str,
    name: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> ComputerTarget:
    if primary_worker_backend_name(runtime_paths) not in {"docker", "kubernetes"}:
        raise ComputerError(503, "Computer requires a dedicated Docker or Kubernetes worker backend.")
    if not config.agent_has_tool_at_execution_scope(name, "browser", "user_agent"):
        raise ComputerError(503, "Computer requires browser tools and explicit user_agent worker scope.")

    # Match normal toolkit materialization without adding heavy tool imports to API startup.
    from mindroom.agents import resolve_runtime_worker_tools  # noqa: PLC0415
    from mindroom.tool_system.sandbox_proxy import sandbox_proxy_enabled_for_tool  # noqa: PLC0415

    worker_tools = resolve_runtime_worker_tools(
        name,
        config,
        runtime_paths,
        list(config.resolve_entity(name).available_tools),
    )
    if not sandbox_proxy_enabled_for_tool("browser", runtime_paths=runtime_paths, worker_tools_override=worker_tools):
        raise ComputerError(503, "Computer requires browser tools routed to the worker.")
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
    manager_identity = configured_primary_worker_manager_identity(runtime_paths, runtime_config=config)
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

    def bind_if_ready(self, config: Config | None, bots: Mapping[str, AgentBot | TeamBot]) -> None:
        """Capture live clients within the orchestrator; routes never receive bots."""
        if not self.api_enabled or config is None:
            return
        if not runtime_env_flag("MINDROOM_WORKER_COMPUTER_ENABLED", runtime_paths=self.runtime_paths):
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

        state = config_lifecycle.app_state(main.app)
        state.computer_runtime = ComputerRuntime(
            authorize,
            config_lifecycle.require_api_state(main.app).snapshot.generation,
            config,
        )

    def unbind(self) -> None:
        """Revoke active capabilities before entity reload or orchestrator shutdown."""
        if not self.api_enabled:
            return
        from mindroom.api import config_lifecycle, main  # noqa: PLC0415

        state = config_lifecycle.ensure_app_state(main.app)
        state.computer_runtime = None
        if state.computer_sessions is not None:
            state.computer_sessions.close_all()

    def unbind_for_entity_changes(self, entity_names: Iterable[str]) -> None:
        """Fail closed while any managed entity changes."""
        if tuple(entity_names):
            self.unbind()
