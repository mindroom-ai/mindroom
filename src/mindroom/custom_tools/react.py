"""A reaction-only tool: add an emoji to a message in the current room and nothing else."""

from __future__ import annotations

import nio

from mindroom.custom_tools.matrix_conversation_operations import MatrixMessageOperations
from mindroom.custom_tools.matrix_message import MatrixMessageTools
from mindroom.custom_tools.tool_payloads import custom_tool_payload, tool_context_or_error
from mindroom.tool_system.authoring import Toolkit

# A cap of 8 characters keeps a reaction key short, not text-free: a short word still fits.
# A few long emoji sequences, such as a couple with skin tones, are refused.
_MAX_KEY_CHARACTERS = 8


def _payload(status: str, **kwargs: object) -> str:
    return custom_tool_payload("react", status, **kwargs)


def _key_error(emoji: str) -> str | None:
    """Return why ``emoji`` cannot be a reaction key, or None when it can.

    A reaction acknowledges a message, so a key longer than the cap, or with spaces in it, is refused.
    """
    if not emoji:
        return "emoji must be a non-empty string."
    if len(emoji) > _MAX_KEY_CHARACTERS or any(character.isspace() for character in emoji):
        return f"emoji must be one emoji, at most {_MAX_KEY_CHARACTERS} characters and without spaces."
    return None


async def _event_error(client: nio.AsyncClient, room_id: str, event_id: str) -> str | None:
    """Return why ``event_id`` cannot be reacted to in ``room_id``, or None when the room holds it.

    Only a definite not-found says the event is elsewhere; any other failure may pass on a retry.
    """
    found = await client.room_get_event(room_id, event_id)
    if isinstance(found, nio.RoomGetEventResponse):
        return None
    if isinstance(found, nio.RoomGetEventError) and found.status_code == "M_NOT_FOUND":
        return f"Event {event_id} is not in this room."
    return "Could not verify the event; try again."


class ReactTools(Toolkit):
    """React to messages in the current room; unlike matrix_message, it cannot send, edit, or read."""

    def __init__(self) -> None:
        self._operations = MatrixMessageOperations()
        super().__init__(name="react", tools=[self.react])

    async def react(self, event_id: str, emoji: str) -> str:
        """React to a message in the current room with an emoji.

        Args:
            event_id: Event ID of the message to react to; it must be in the current room.
            emoji: The reaction emoji, such as 👍, at most 8 characters and without spaces.

        """
        if isinstance(context := tool_context_or_error(_payload, "React"), str):
            return context
        event_id = event_id.strip()
        emoji = emoji.strip()
        if not event_id:
            return _payload("error", message="event_id must be a non-empty string.")
        if (key_error := _key_error(emoji)) is not None:
            return _payload("error", message=key_error)
        room_id = context.room_id
        # Shares matrix_message's per-room budget, so reacting is no way around it.
        if (rate_error := MatrixMessageTools._check_rate_limit(context, room_id)) is not None:
            return _payload("error", room_id=room_id, message=rate_error)
        if (lookup_error := await _event_error(context.client, room_id, event_id)) is not None:
            return _payload("error", room_id=room_id, message=lookup_error)
        result = await self._operations._message_react(
            context,
            message=emoji,
            room_id=room_id,
            thread_id=None,
            event_id=event_id,
        )
        return _payload(result.status, **result.fields)
