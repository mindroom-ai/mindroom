"""Tool for stopping another agent's reply in the current conversation."""

from __future__ import annotations

import nio
from agno.tools import Toolkit

from mindroom.constants import ORIGINAL_SENDER_KEY, SOURCE_KIND_KEY
from mindroom.custom_tools.tool_payloads import custom_tool_payload
from mindroom.dispatch_source import TRUSTED_INTERNAL_RELAY_SOURCE_KIND
from mindroom.matrix.client_delivery import send_room_event_result
from mindroom.matrix.message_builder import build_reaction_content
from mindroom.requester_identity import is_access_checked_requester_id
from mindroom.tool_system.runtime_context import get_tool_runtime_context


class StopReplyTools(Toolkit):
    """Stop the reply another agent or team is writing."""

    def __init__(self) -> None:
        super().__init__(name="stop_reply", tools=[self.stop_agent_reply])

    @staticmethod
    def _payload(status: str, **kwargs: object) -> str:
        return custom_tool_payload("stop_reply", status, **kwargs)

    async def stop_agent_reply(self, agent: str) -> str:
        """Stop the reply another agent or team is currently writing in this room and thread.

        The stop acts for the user you are working for, exactly as if they reacted 🛑 on that reply,
        so it only takes effect when that user may use the target agent.

        Args:
            agent: Name of the agent or team whose reply to stop.

        """
        context = get_tool_runtime_context()
        if context is None:
            return self._payload("error", message="Stop reply tool context is unavailable in this runtime path.")
        if agent in {context.agent_name, context.transport_agent_name}:
            return self._payload("error", agent=agent, message="You cannot stop your own reply.")
        # The relayed requester is whose access the target checks, as for any user's 🛑.
        if not is_access_checked_requester_id(context.requester_id, context.config, context.runtime_paths):
            return self._payload("error", agent=agent, message="A reply can only be stopped on behalf of a user.")
        room_id = context.target.room_id
        thread_id = context.target.resolved_thread_id
        orchestrator = context.orchestrator
        reply_event_id = (
            await orchestrator.open_reply_event_id(agent, room_id, thread_id) if orchestrator is not None else None
        )
        if reply_event_id is None:
            return self._payload(
                "error",
                agent=agent,
                message=f"{agent} has no reply in progress in this conversation.",
            )

        content = build_reaction_content(reply_event_id, "🛑")
        content[ORIGINAL_SENDER_KEY] = context.requester_id
        content[SOURCE_KIND_KEY] = TRUSTED_INTERNAL_RELAY_SOURCE_KIND
        response = await send_room_event_result(
            context.client,
            room_id,
            "m.reaction",
            content,
            operation="stop_agent_reply",
        )
        if not isinstance(response, nio.RoomSendResponse):
            return self._payload(
                "error",
                agent=agent,
                reply_event_id=reply_event_id,
                message=f"Failed to send the stop: {response}",
            )
        return self._payload(
            "ok",
            agent=agent,
            room_id=room_id,
            thread_id=thread_id,
            reply_event_id=reply_event_id,
            event_id=response.event_id,
        )
