"""MindRoom compatibility adapter for the Gemini API."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agno.models.google import Gemini
from agno.utils.message import normalize_tool_messages

if TYPE_CHECKING:
    from agno.models.message import Message


@dataclass
class MindRoomGoogleGemini(Gemini):
    """Gemini model that preserves provider call IDs across tool loops."""

    def _format_messages(
        self,
        messages: list[Message],
        compress_tool_results: bool = False,
    ) -> tuple[list[object], object]:
        normalized_messages = normalize_tool_messages(messages)
        tool_call_ids = [
            tool_call.get("id") for message in normalized_messages for tool_call in (message.tool_calls or [])
        ]
        tool_response_ids = [
            message.tool_call_id
            for message in normalized_messages
            if message.role == "tool" and message.tool_call_id is not None and message.tool_name is not None
        ]

        formatted_messages, system_message = super()._format_messages(
            normalized_messages,
            compress_tool_results=compress_tool_results,
        )
        tool_call_id_iter = iter(tool_call_ids)
        tool_response_id_iter = iter(tool_response_ids)
        for message in formatted_messages:
            for part in message.parts:
                if part.function_call is not None:
                    part.function_call.id = next(tool_call_id_iter)
                if part.function_response is not None:
                    part.function_response.id = next(tool_response_id_iter)

        return formatted_messages, system_message
