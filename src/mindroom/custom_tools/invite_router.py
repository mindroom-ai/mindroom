"""Agent tool for adding the router to the current Matrix room."""

from __future__ import annotations

import asyncio

import nio
from agno.tools import Toolkit

from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.entity_resolution import entity_identity_registry
from mindroom.tool_system.declarations import MATRIX_ROOM_RUNTIME_APPROVAL_TYPE
from mindroom.tool_system.runtime_context import get_tool_runtime_context

_ROUTER_JOIN_POLL_SECONDS = 0.25
_ROUTER_JOIN_TIMEOUT_SECONDS = 5.0


def _membership(response: object) -> str | None:
    if not isinstance(response, nio.RoomGetStateEventResponse):
        return None
    membership = response.content.get("membership")
    return membership if isinstance(membership, str) else None


async def _wait_for_join(client: nio.AsyncClient, room_id: str, router_id: str) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _ROUTER_JOIN_TIMEOUT_SECONDS
    while loop.time() < deadline:
        response = await client.room_get_state_event(room_id, "m.room.member", router_id)
        if _membership(response) == "join":
            return True
        await asyncio.sleep(_ROUTER_JOIN_POLL_SECONDS)
    return False


async def _invite_and_wait(client: nio.AsyncClient, room_id: str, router_id: str) -> str:
    response = await client.room_invite(room_id, router_id)
    if not isinstance(response, nio.RoomInviteResponse):
        return "Error: Router invite failed; current agent may lack invite permission."
    if await _wait_for_join(client, room_id, router_id):
        return "Router joined."
    return "Router invited; join still pending. Retry after it joins."


class InviteRouterTools(Toolkit):
    """Expose explicit router invite recovery to agents."""

    def __init__(self) -> None:
        super().__init__(name="invite_router", tools=[self.invite_router])
        self.async_functions["invite_router"].approval_type = MATRIX_ROOM_RUNTIME_APPROVAL_TYPE

    async def invite_router(self) -> str:
        """Invite router to current Matrix room."""
        context = get_tool_runtime_context()
        if context is None:
            return "Error: Matrix room context unavailable."

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
        membership = _membership(membership_response)
        if membership == "join":
            return "Router already joined."
        if not context.current_config.router.accept_invites:
            return "Error: Router auto-accept is disabled."
        if membership == "invite":
            return "Router invite pending; retry after it joins."

        return await _invite_and_wait(context.client, context.room_id, router_id)
