"""Helpers for team history scope identifiers."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from mindroom.config.agent import AgentConfig


def ad_hoc_team_has_private_member(
    member_names: Iterable[str],
    agent_configs: Mapping[str, AgentConfig],
) -> bool:
    """Return whether one ad hoc member set includes a private agent."""
    return any(
        (agent_config := agent_configs.get(member_name)) is not None and agent_config.private is not None
        for member_name in member_names
    )


def ad_hoc_team_scope_id(
    member_names: Iterable[str | None],
    agent_configs: Mapping[str, AgentConfig],
    *,
    requester_user_id: str | None = None,
    # TODO(simplify): Callers choose the missing-requester error text, so one failure reads "requester identity" or
    # "requester_user_id" by path. Raise one message and drop the parameter. Operators reading errors would notice.
    missing_requester_message: str = "Private ad hoc team history scope requires requester identity",
) -> str | None:
    """Return the stable history scope id for one exact ad hoc team, partitioned by requester when a member is private."""
    stable_member_names = tuple(sorted(name for name in member_names if name))
    if not stable_member_names:
        return None

    scope_id = f"team_{'+'.join(stable_member_names)}"
    if not ad_hoc_team_has_private_member(stable_member_names, agent_configs):
        return scope_id
    if not requester_user_id:
        raise ValueError(missing_requester_message)
    return f"{scope_id}_requester_{hashlib.sha256(requester_user_id.encode('utf-8')).hexdigest()[:12]}"
