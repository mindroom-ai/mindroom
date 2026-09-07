"""Canonical personal-agent access and credential ownership for API callers."""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Literal

from fastapi import HTTPException

from mindroom.access_policy import resolve_responder_access
from mindroom.matrix.identity import try_parse_historical_matrix_user_id
from mindroom.requester_identity import resolve_human_requester_alias
from mindroom.tool_system.worker_routing import build_agent_toolkit_worker_target, build_tool_execution_identity

if TYPE_CHECKING:
    from mindroom.api.config_lifecycle import ApiSnapshot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget, ToolExecutionIdentity

PERSONAL_RESPONSE_HEADERS = {"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"}


@dataclass(frozen=True)
class PersonalAgentContext:
    """One authorized personal agent with an explicit private credential owner."""

    agent_name: str
    requester_id: str
    config: Config
    runtime_paths: RuntimePaths
    execution_identity: ToolExecutionIdentity
    worker_target: ResolvedWorkerTarget


def resolve_personal_agent(
    snapshot: ApiSnapshot,
    requester_id: str,
    *,
    expected_agent_name: str | None = None,
    channel: Literal["matrix", "mcp"] = "mcp",
) -> PersonalAgentContext:
    """Authorize an authenticated requester against the current operator-selected agent.

    Authentication belongs to the transport boundary. This resolver never reads
    browser selectors, cookies, owner defaults, or ambient execution identity.
    """
    paths = snapshot.runtime_paths
    agent_name = (paths.env_value("MINDROOM_CONNECTIONS_AGENT") or "").strip()
    if not agent_name:
        raise HTTPException(404, "Personal connections are not enabled", headers=PERSONAL_RESPONSE_HEADERS)
    if expected_agent_name is not None and agent_name != expected_agent_name:
        raise HTTPException(403, "Personal agent access has changed", headers=PERSONAL_RESPONSE_HEADERS)
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
        raise HTTPException(403, "Personal agent access is required", headers=PERSONAL_RESPONSE_HEADERS)
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
        is_private=True,
        execution_identity=identity,
        runtime_paths=paths,
    )
    return PersonalAgentContext(agent_name, requester_id, config, paths, identity, target)
