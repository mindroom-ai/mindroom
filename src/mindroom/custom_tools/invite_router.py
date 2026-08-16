"""Agent tool for adding the router to the current Matrix room."""

from __future__ import annotations

import nio
from agno.tools import Toolkit

from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.entity_resolution import entity_identity_registry
from mindroom.tool_system.runtime_context import get_tool_runtime_context


class InviteRouterTools(Toolkit):
    """Expose explicit router invite recovery to agents."""

    def __init__(self) -> None:
        super().__init__(name="invite_router", tools=[self.invite_router])

    async def invite_router(self) -> str:
        """Invite router to current Matrix room."""
        context = get_tool_runtime_context()
        if context is None:
            return "Error: Matrix room context unavailable."
        if not context.current_config.router.accept_invites:
            return "Error: Router auto-accept is disabled."

        router_id = (
            entity_identity_registry(
                context.current_config,
                context.runtime_paths,
            )
            .current_id(ROUTER_AGENT_NAME)
            .full_id
        )
        membership_response = await context.client.room_get_state_event(
            context.room_id,
            "m.room.member",
            router_id,
        )
        if isinstance(membership_response, nio.RoomGetStateEventResponse):
            membership = membership_response.content.get("membership")
            if membership == "join":
                return "Router already joined."
            if membership == "invite":
                return "Router already invited; it will auto-join."

        response = await context.client.room_invite(context.room_id, router_id)
        if isinstance(response, nio.RoomInviteResponse):
            return "Router invited; it will auto-join."
        return "Error: Router invite failed; current agent may lack invite permission."
