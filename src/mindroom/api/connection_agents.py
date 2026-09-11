"""Canonical user access and agent tool targets for Connections and MCP callers."""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Literal

from fastapi import HTTPException

from mindroom.access_policy import resolve_responder_access
from mindroom.authorization import is_sender_allowed_for_agent_credential_management
from mindroom.matrix.identity import try_parse_historical_matrix_user_id
from mindroom.mcp_gateway.types import GatewayOwner
from mindroom.requester_identity import resolve_human_requester_alias
from mindroom.tool_system.worker_routing import build_agent_toolkit_worker_target, build_tool_execution_identity

if TYPE_CHECKING:
    from mindroom.api.config_lifecycle import ApiSnapshot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget, ToolExecutionIdentity

PERSONAL_RESPONSE_HEADERS = {"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"}


class _PersonalAgentAccessDeniedError(HTTPException):
    """The configured personal agent is valid but unavailable to this requester."""


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


def _build_agent_context(
    agent_name: str,
    requester_id: str,
    config: Config,
    paths: RuntimePaths,
    channel: Literal["matrix", "mcp"],
) -> AgentToolContext:
    identity = build_tool_execution_identity(
        channel=channel,
        agent_name=agent_name,
        runtime_paths=paths,
        requester_id=requester_id,
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    target = build_agent_toolkit_worker_target(
        config.resolve_entity(agent_name).execution_scope,
        agent_name,
        is_private=config.agents[agent_name].private is not None,
        execution_identity=identity,
        runtime_paths=paths,
    )
    return AgentToolContext(agent_name, requester_id, config, paths, identity, target)


def _resolve_personal_agent(
    snapshot: ApiSnapshot,
    requester_id: str,
    *,
    channel: Literal["matrix", "mcp"] = "mcp",
) -> AgentToolContext:
    """Authorize an authenticated requester against the current operator-selected agent.

    Authentication belongs to the transport boundary. This resolver never reads
    browser selectors, cookies, owner defaults, or ambient execution identity.
    """
    paths = snapshot.runtime_paths
    agent_name = (paths.env_value("MINDROOM_CONNECTIONS_AGENT") or "").strip()
    if not agent_name:
        raise HTTPException(404, "Personal connections are not enabled", headers=PERSONAL_RESPONSE_HEADERS)
    config = snapshot.runtime_config
    if config is None:
        raise HTTPException(503, "Personal connections are unavailable", headers=PERSONAL_RESPONSE_HEADERS)
    agent = config.agents.get(agent_name)
    if agent is None or agent.private is None or agent.private.per not in {"user", "user_agent"}:
        raise HTTPException(403, "Personal connections require a private agent", headers=PERSONAL_RESPONSE_HEADERS)
    if try_parse_historical_matrix_user_id(requester_id) is None:
        raise HTTPException(
            403,
            "Personal connections require a verified Matrix identity",
            headers=PERSONAL_RESPONSE_HEADERS,
        )
    requester_id = resolve_human_requester_alias(requester_id, config, paths)
    access = resolve_responder_access(config, agent_name)
    # API callers have no conversation membership context. Require explicit grants.
    if requester_id not in config.administrators and not any(
        fnmatchcase(requester_id, pattern) for pattern in access.users
    ):
        raise _PersonalAgentAccessDeniedError(
            403,
            "Personal agent access is required",
            headers=PERSONAL_RESPONSE_HEADERS,
        )
    return _build_agent_context(agent_name, requester_id, config, paths, channel)


def resolve_connection_user(
    snapshot: ApiSnapshot,
    authenticated_user_id: str,
    *,
    account_id: str | None = None,
) -> ConnectionUserContext:
    """Resolve eligible personal/shared agents without granting authority from saved selections."""
    try:
        personal = _resolve_personal_agent(snapshot, authenticated_user_id)
    except _PersonalAgentAccessDeniedError:
        personal = None
    config = snapshot.runtime_config
    assert config is not None  # The personal resolver validates the publication even when access is denied.
    requester_id = resolve_human_requester_alias(authenticated_user_id, config, snapshot.runtime_paths)
    shared = tuple(
        name
        for name, agent in config.agents.items()
        if agent.private is None
        and is_sender_allowed_for_agent_credential_management(requester_id, name, config, snapshot.runtime_paths)
    )
    return ConnectionUserContext(
        GatewayOwner(authenticated_user_id, requester_id, account_id),
        config,
        snapshot.runtime_paths,
        ((personal.agent_name,) if personal is not None else ()) + shared,
        personal.agent_name if personal is not None else None,
    )


def resolve_connection_agent(
    user: ConnectionUserContext,
    agent_name: str,
) -> AgentToolContext:
    """Build one eligible agent target using its actual privacy and execution scope."""
    if agent_name not in user.agent_names:
        raise HTTPException(404, "Agent is not available", headers=PERSONAL_RESPONSE_HEADERS)
    return _build_agent_context(agent_name, user.owner.requester_id, user.config, user.runtime_paths, "mcp")
