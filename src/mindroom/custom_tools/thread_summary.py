"""Manual thread summary tool for AI agents."""

from __future__ import annotations

import json

from agno.tools import Toolkit

from mindroom.custom_tools.attachment_helpers import resolve_context_thread_id, room_access_allowed
from mindroom.matrix.client import fetch_thread_history
from mindroom.thread_summary import send_thread_summary_event, thread_summary_lock, update_last_summary_count
from mindroom.thread_tags import normalize_thread_root_event_id
from mindroom.tool_system.runtime_context import get_tool_runtime_context


class ThreadSummaryTools(Toolkit):
    """Tools for manually setting Matrix thread summaries."""

    def __init__(self) -> None:
        super().__init__(
            name="thread_summary",
            tools=[self.set_thread_summary],
        )

    @staticmethod
    def _payload(status: str, **kwargs: object) -> str:
        payload: dict[str, object] = {"status": status, "tool": "thread_summary"}
        payload.update(kwargs)
        return json.dumps(payload, sort_keys=True)

    @classmethod
    def _context_error(cls) -> str:
        return cls._payload(
            "error",
            action="set",
            message="Thread summary tool context is unavailable in this runtime path.",
        )

    async def set_thread_summary(
        self,
        summary: str,
        thread_id: str | None = None,
        room_id: str | None = None,
    ) -> str:
        """Write a manual summary notice into the current or specified Matrix thread."""
        context = get_tool_runtime_context()
        if context is None:
            return self._context_error()

        resolved_room_id = room_id or context.room_id
        if not room_access_allowed(context, resolved_room_id):
            return self._payload(
                "error",
                action="set",
                room_id=resolved_room_id,
                message="Not authorized to access the target room.",
            )

        if not isinstance(summary, str) or not summary.strip():
            return self._payload(
                "error",
                action="set",
                room_id=resolved_room_id,
                message="summary must be a non-empty string.",
            )
        normalized_summary = summary.strip()

        error_message: str | None = None
        error_thread_id: str | None = None
        effective_thread_id = resolve_context_thread_id(
            context,
            room_id=resolved_room_id,
            thread_id=thread_id,
            room_timeline_fallback_event_id=context.reply_to_event_id,
        )
        if effective_thread_id is None:
            error_message = "thread_id is required when no active thread context is available for the target room."
        else:
            normalized_thread_id = await normalize_thread_root_event_id(
                context.client,
                resolved_room_id,
                effective_thread_id,
            )
            if normalized_thread_id is None:
                error_thread_id = effective_thread_id
                error_message = "Failed to resolve a canonical thread root for the target event."
            else:
                async with thread_summary_lock(resolved_room_id, normalized_thread_id):
                    thread_history = await fetch_thread_history(
                        context.client,
                        resolved_room_id,
                        normalized_thread_id,
                    )
                    message_count = len(thread_history)
                    event_id = await send_thread_summary_event(
                        context.client,
                        resolved_room_id,
                        normalized_thread_id,
                        normalized_summary,
                        message_count,
                        "manual",
                    )
                    if event_id is None:
                        error_thread_id = normalized_thread_id
                        error_message = "Failed to send thread summary event."
                    else:
                        update_last_summary_count(resolved_room_id, normalized_thread_id, message_count)
                        return self._payload(
                            "ok",
                            action="set",
                            room_id=resolved_room_id,
                            thread_id=normalized_thread_id,
                            event_id=event_id,
                            message_count=message_count,
                            summary=normalized_summary,
                        )

        assert error_message is not None
        return self._payload(
            "error",
            action="set",
            room_id=resolved_room_id,
            thread_id=error_thread_id,
            message=error_message,
        )
