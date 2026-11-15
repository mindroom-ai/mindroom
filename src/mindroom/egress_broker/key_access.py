"""Who may use and manage an agent's Agent Keys."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.authorization import is_sender_allowed_for_agent_credential_management, is_sender_allowed_for_responder
from mindroom.egress_broker.env import egress_agent_scope
from mindroom.requester_identity import is_platform_administrator

if TYPE_CHECKING:
    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

__all__ = ["egress_key_access"]


def egress_key_access(
    requester_id: str,
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    memberships: AgentReplyMembershipIndex,
) -> tuple[bool, bool] | None:
    """Return ``(is_shared, can_manage)`` for an eligible agent, else ``None``.

    Eligibility requires an agent that reaches the broker (`egress_agent_scope`) and permission to use it.
    Administrators manage every shared or unscoped agent's keys, including a personal agent they may not use.
    Listing, portal catalog, mutations, and the request log's shared-agent rows all decide through this one rule.
    """
    scope = egress_agent_scope(config, agent_name, runtime_paths)
    if scope is None:
        return None
    manages_shared_keys = scope in {"shared", "unscoped"} and is_platform_administrator(
        requester_id,
        config,
        runtime_paths,
    )
    if not manages_shared_keys and not is_sender_allowed_for_responder(
        requester_id,
        agent_name,
        None,
        config,
        runtime_paths,
        memberships,
    ):
        return None
    is_shared = scope in {"shared", "unscoped"}
    can_manage = not is_shared or is_sender_allowed_for_agent_credential_management(
        requester_id,
        agent_name,
        config,
        runtime_paths,
    )
    return is_shared, can_manage
