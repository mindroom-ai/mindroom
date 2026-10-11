"""Discover agents eligible to answer a requester in a Matrix room."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

import nio

from mindroom.agent_descriptions import describe_agent
from mindroom.authorization import responder_candidate_entities_with_membership_refresh
from mindroom.entity_resolution import entity_identity_registry
from mindroom.responder_availability import (
    filter_materializable_responders,
    live_responder_entity_names,
    materializable_agent_names_for_orchestrator,
)

if TYPE_CHECKING:
    from mindroom.tool_system.runtime_context import ToolRuntimeContext


@dataclass(frozen=True)
class _MatrixRoomAgent:
    """A current responder and the information needed to message it."""

    name: str
    matrix_user_id: str
    description: str
    thread_mode: str


async def available_room_agents(context: ToolRuntimeContext, room_id: str) -> list[_MatrixRoomAgent]:
    """Apply current ingress authorization, room scope, and runtime availability."""
    context = replace(context, config=context.current_config, config_provider=None)
    target_room = context.room if room_id == context.room_id else None
    target_room = target_room or context.client.rooms.get(room_id)
    if room_id == context.room_id:
        candidates = await context.responder_candidates_for_current_room(
            target_room or nio.MatrixRoom(room_id, own_user_id=""),
            context.requester_id,
        )
    else:
        candidates = await responder_candidate_entities_with_membership_refresh(
            context.client,
            target_room or nio.MatrixRoom(room_id, own_user_id=""),
            context.requester_id,
            context.config,
            context.runtime_paths,
            context.require_agent_reply_memberships(),
        )
    candidates = filter_materializable_responders(
        candidates,
        context.config,
        context.runtime_paths,
        materializable_agent_names=materializable_agent_names_for_orchestrator(context.orchestrator, context.config),
        live_entity_names=live_responder_entity_names(context.orchestrator, context.config),
    )
    registry = entity_identity_registry(context.config, context.runtime_paths)
    agents: dict[str, _MatrixRoomAgent] = {}
    for candidate in candidates:
        name = registry.current_entity_name_for_user_id(candidate.full_id, include_router=False)
        if name in context.config.agents or name in context.config.teams:
            agents[name] = _MatrixRoomAgent(
                name=name,
                matrix_user_id=candidate.full_id,
                description=describe_agent(name, context.config),
                thread_mode=context.config.get_entity_thread_mode(name, context.runtime_paths, room_id=room_id),
            )
    return [agents[name] for name in sorted(agents)]
