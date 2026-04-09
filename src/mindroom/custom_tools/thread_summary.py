"""Manual thread summary tool for AI agents."""

from __future__ import annotations

import json

from agno.tools import Toolkit

from mindroom.custom_tools.attachment_helpers import resolve_context_thread_id, room_access_allowed
from mindroom.thread_summary import (
    THREAD_SUMMARY_MAX_LENGTH,
    _count_non_summary_messages,
    normalize_thread_summary_text,
    send_thread_summary_event,
    thread_summary_lock,
    update_last_summary_count,
)
from mindroom.thread_tags import normalize_thread_root_event_id
from mindroom.tool_system.runtime_context import get_tool_runtime_context

_MAX_THREAD_SUMMARY_LENGTH = THREAD_SUMMARY_MAX_LENGTH


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

    async def set_thread_summary(  # noqa: C901, PLR0911, PLR0912, PLR0915
        self,
        summary: str,
        thread_id: str | None = None,
        room_id: str | None = None,
    ) -> str:
        """Write a plain-text summary notice into the current or specified Matrix thread.

        Summary must be plain text (no markdown), maximum 300 characters.
        """
        context = get_tool_runtime_context()
        if context is None:
            return self._context_error()
        conversation_access = context.conversation_access
        if conversation_access is None:
            return self._context_error()

        if room_id is None:
            resolved_room_id = context.room_id
        elif not isinstance(room_id, str) or not room_id.strip():
            return self._payload(
                "error",
                action="set",
                room_id=room_id,
                message="room_id must be a non-empty string when provided.",
            )
        else:
            resolved_room_id = room_id.strip()

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
        normalized_summary = normalize_thread_summary_text(summary)
        if not normalized_summary:
            return self._payload(
                "error",
                action="set",
                room_id=resolved_room_id,
                message="summary must be a non-empty string.",
            )
        if len(normalized_summary) > _MAX_THREAD_SUMMARY_LENGTH:
            return self._payload(
                "error",
                action="set",
                room_id=resolved_room_id,
                message=f"summary must be {_MAX_THREAD_SUMMARY_LENGTH} characters or fewer after whitespace normalization.",
            )

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
            try:
                normalized_thread_id = await normalize_thread_root_event_id(
                    context.client,
                    resolved_room_id,
                    effective_thread_id,
                    access=conversation_access,
                )
            except Exception:
                error_thread_id = effective_thread_id
                error_message = "Failed to resolve a canonical thread root for the target event."
            else:
                if normalized_thread_id is None:
                    error_thread_id = effective_thread_id
                    error_message = "Failed to resolve a canonical thread root for the target event."
                else:
                    async with thread_summary_lock(resolved_room_id, normalized_thread_id):
                        try:
                            thread_history = await conversation_access.get_thread_history(
                                resolved_room_id,
                                normalized_thread_id,
                            )
                        except Exception:
                            error_thread_id = normalized_thread_id
                            error_message = "Failed to fetch thread history for the target thread."
                        else:
                            message_count = _count_non_summary_messages(thread_history)
                            try:
                                event_id = await send_thread_summary_event(
                                    context.client,
                                    resolved_room_id,
                                    normalized_thread_id,
                                    normalized_summary,
                                    message_count,
                                    "manual",
                                )
                            except Exception:
                                error_thread_id = normalized_thread_id
                                error_message = "Failed to send thread summary event."
                            else:
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
