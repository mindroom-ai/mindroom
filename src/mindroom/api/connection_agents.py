"""Canonical user access and agent tool targets for Connections and MCP callers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import HTTPException

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.authorization import is_sender_allowed_for_agent_credential_management, is_sender_allowed_for_responder
from mindroom.matrix.identity import try_parse_historical_matrix_user_id
from mindroom.mcp_gateway.types import GatewayOwner
from mindroom.requester_identity import resolve_human_requester_alias
from mindroom.tool_system.worker_routing import build_agent_toolkit_worker_target, build_tool_execution_identity

if TYPE_CHECKING:
    from mindroom.api.config_lifecycle import ApiSnapshot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget, ToolExecutionIdentity

CONNECTIONS_HEADERS = {"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"}


@dataclass(frozen=True)
class AgentToolContext:
    """One authorized agent with an explicit requester and execution target."""

    agent_name: str
    requester_id: str
    config: Config
    runtime_paths: RuntimePaths
    execution_identity: ToolExecutionIdentity
    worker_target: ResolvedWorkerTarget


@dataclass(frozen=True)
class ConnectionUserContext:
    """A verified user and the agent choices allowed by the current configuration."""

    owner: GatewayOwner
    config: Config
    runtime_paths: RuntimePaths
    agent_names: tuple[str, ...]
    personal_agent_name: str | None
    credential_agent_names: tuple[str, ...]

    @property
    def visible_agent_names(self) -> tuple[str, ...]:
        """Show agents the user can execute or whose credentials they can manage."""
        eligible = set(self.agent_names) | set(self.credential_agent_names)
        personal = (self.personal_agent_name,) if self.personal_agent_name is not None else ()
        return personal + tuple(name for name in self.config.agents if name in eligible and name not in personal)


def resolve_connection_user(
    snapshot: ApiSnapshot,
    authenticated_user_id: str,
    *,
    account_id: str | None = None,
    membership_index: AgentReplyMembershipIndex | None = None,
) -> ConnectionUserContext:
    """Resolve current agent eligibility without constructing execution targets."""
    paths = snapshot.runtime_paths
    agent_name = (paths.env_value("MINDROOM_CONNECTIONS_AGENT") or "").strip()
    if not agent_name:
        raise HTTPException(404, "Connections are not enabled", headers=CONNECTIONS_HEADERS)
    config = snapshot.runtime_config
    if config is None:
        raise HTTPException(503, "Connections are unavailable", headers=CONNECTIONS_HEADERS)
    agent = config.agents.get(agent_name)
    if agent is None or agent.private is None or agent.private.per not in {"user", "user_agent"}:
        raise HTTPException(403, "Connections require a configured private agent", headers=CONNECTIONS_HEADERS)
    if try_parse_historical_matrix_user_id(authenticated_user_id) is None:
        raise HTTPException(
            403,
            "Connections require a verified Matrix identity",
            headers=CONNECTIONS_HEADERS,
        )
    requester_id = resolve_human_requester_alias(authenticated_user_id, config, paths)
    memberships = membership_index if membership_index is not None else AgentReplyMembershipIndex()
    candidates = (agent_name, *(name for name, candidate in config.agents.items() if candidate.private is None))
    usable = tuple(
        name
        for name in candidates
        if is_sender_allowed_for_responder(requester_id, name, None, config, paths, memberships)
    )
    personal_agent_name = agent_name if agent_name in usable else None
    managed = tuple(
        name
        for name in candidates
        if name == personal_agent_name
        or (
            config.agents[name].private is None
            and is_sender_allowed_for_agent_credential_management(requester_id, name, config, paths)
        )
    )
    return ConnectionUserContext(
        GatewayOwner(authenticated_user_id, requester_id, account_id),
        config,
        paths,
        usable,
        personal_agent_name,
        managed,
    )


def resolve_connection_agent(
    user: ConnectionUserContext,
    agent_name: str,
) -> AgentToolContext:
    """Build one eligible agent target using its actual privacy and execution scope."""
    if agent_name not in user.agent_names:
        raise HTTPException(404, "Agent is not available", headers=CONNECTIONS_HEADERS)
    identity = build_tool_execution_identity(
        channel="mcp",
        agent_name=agent_name,
        runtime_paths=user.runtime_paths,
        requester_id=user.owner.requester_id,
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    target = build_agent_toolkit_worker_target(
        user.config.resolve_entity(agent_name).execution_scope,
        agent_name,
        is_private=user.config.agents[agent_name].private is not None,
        execution_identity=identity,
        runtime_paths=user.runtime_paths,
    )
    return AgentToolContext(
        agent_name,
        user.owner.requester_id,
        user.config,
        user.runtime_paths,
        identity,
        target,
    )
