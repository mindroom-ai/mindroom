"""Manual thread summary tool for AI agents."""

from __future__ import annotations

import json

from agno.tools import Toolkit

from mindroom.custom_tools.attachment_helpers import (
    resolve_context_thread_id,
    resolve_requested_room_id,
    room_access_allowed,
)
from mindroom.matrix.thread_membership import resolve_thread_root_event_id_for_client
from mindroom.thread_summary import (
    ThreadSummaryWriteError,
    set_manual_thread_summary,
)
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

    async def set_thread_summary(  # noqa: PLR0911
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
        conversation_cache = context.conversation_cache

        resolved_room_id, room_error = resolve_requested_room_id(context, room_id)
        if room_error is not None:
            return self._payload(
                "error",
                action="set",
                room_id=room_id,
                message="room_id must be a non-empty string when provided.",
            )
        assert resolved_room_id is not None

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
        effective_thread_id = resolve_context_thread_id(
            context,
            room_id=resolved_room_id,
            thread_id=thread_id,
        )
        if effective_thread_id is None:
            return self._payload(
                "error",
                action="set",
                room_id=resolved_room_id,
                thread_id=None,
                message="thread_id is required when no active thread context is available for the target room.",
            )

        try:
            normalized_thread_id = await resolve_thread_root_event_id_for_client(
                context.client,
                resolved_room_id,
                effective_thread_id,
                conversation_cache=context.conversation_cache,
            )
        except Exception:
            normalized_thread_id = None

        if normalized_thread_id is None:
            return self._payload(
                "error",
                action="set",
                room_id=resolved_room_id,
                thread_id=effective_thread_id,
                message="Failed to resolve a canonical thread root for the target event.",
            )

        try:
            result = await set_manual_thread_summary(
                context.client,
                resolved_room_id,
                normalized_thread_id,
                summary,
                conversation_cache=conversation_cache,
            )
        except ThreadSummaryWriteError as exc:
            return self._payload(
                "error",
                action="set",
                room_id=resolved_room_id,
                thread_id=normalized_thread_id,
                message=str(exc),
            )

        return self._payload(
            "ok",
            action="set",
            room_id=resolved_room_id,
            thread_id=normalized_thread_id,
            event_id=result.event_id,
            message_count=result.message_count,
            summary=result.summary,
        )
