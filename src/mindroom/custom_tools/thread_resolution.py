"""Thread resolution tool for AI agents."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from agno.tools import Toolkit

from mindroom.custom_tools.attachment_helpers import room_access_allowed
from mindroom.thread_resolution import (
    ThreadResolutionError,
    clear_thread_resolution,
    normalize_thread_root_event_id,
    set_thread_resolved,
)
from mindroom.tool_system.runtime_context import ToolRuntimeContext, get_tool_runtime_context


class ThreadResolutionTools(Toolkit):
    """Tools for marking Matrix threads as resolved or unresolved."""

    def __init__(self) -> None:
        super().__init__(
            name="thread_resolution",
            tools=[self.resolve_thread, self.unresolve_thread],
        )

    @staticmethod
    def _payload(status: str, **kwargs: object) -> str:
        payload: dict[str, object] = {"status": status, "tool": "thread_resolution"}
        payload.update(kwargs)
        return json.dumps(payload, sort_keys=True)

    @classmethod
    def _context_error(cls) -> str:
        return cls._payload(
            "error",
            message="Thread resolution tool context is unavailable in this runtime path.",
        )

    @staticmethod
    def _safe_thread_id(
        context: ToolRuntimeContext,
        *,
        room_id: str,
        thread_id: str | None,
    ) -> str | None:
        """Return a thread ID only when it is valid for the target room."""
        if thread_id is not None:
            return thread_id
        if room_id == context.room_id:
            return context.resolved_thread_id or context.event_id
        return None

    async def resolve_thread(
        self,
        thread_id: str | None = None,
        room_id: str | None = None,
    ) -> str:
        """Mark the current or specified Matrix thread as resolved."""
        context = get_tool_runtime_context()
        if context is None:
            return self._context_error()

        resolved_room_id = room_id or context.room_id
        if not room_access_allowed(context, resolved_room_id):
            return self._payload(
                "error",
                action="resolve",
                room_id=resolved_room_id,
                message="Not authorized to access the target room.",
            )

        effective_thread_id = self._safe_thread_id(
            context,
            room_id=resolved_room_id,
            thread_id=thread_id,
        )
        if effective_thread_id is None:
            return self._payload(
                "error",
                action="resolve",
                room_id=resolved_room_id,
                message="thread_id is required when no active thread context is available for the target room.",
            )
        normalized_thread_id = await normalize_thread_root_event_id(
            context.client,
            resolved_room_id,
            effective_thread_id,
        )
        if normalized_thread_id is None:
            return self._payload(
                "error",
                action="resolve",
                room_id=resolved_room_id,
                thread_id=effective_thread_id,
                message="Failed to resolve a canonical thread root for the target event.",
            )

        try:
            record = await set_thread_resolved(
                context.client,
                resolved_room_id,
                normalized_thread_id,
                context.requester_id,
            )
        except ThreadResolutionError as exc:
            return self._payload(
                "error",
                action="resolve",
                room_id=resolved_room_id,
                thread_id=normalized_thread_id,
                message=str(exc),
            )

        return self._payload(
            "ok",
            action="resolve",
            room_id=resolved_room_id,
            thread_id=record.thread_root_id,
            resolved=True,
            resolved_by=record.resolved_by,
            resolved_at=record.resolved_at.isoformat(),
            updated_by=record.resolved_by,
            updated_at=record.updated_at.isoformat(),
        )

    async def unresolve_thread(
        self,
        thread_id: str | None = None,
        room_id: str | None = None,
        canonical: bool = False,
    ) -> str:
        """Clear the resolved marker for the current or specified Matrix thread.

        When *canonical* is True, *thread_id* is treated as an already-normalized
        state key and no live event fetch is attempted.  This allows clearing
        orphaned resolution markers whose original thread event has been deleted.
        """
        context = get_tool_runtime_context()
        if context is None:
            return self._context_error()

        resolved_room_id = room_id or context.room_id
        if not room_access_allowed(context, resolved_room_id):
            return self._payload(
                "error",
                action="unresolve",
                room_id=resolved_room_id,
                message="Not authorized to access the target room.",
            )

        effective_thread_id = self._safe_thread_id(
            context,
            room_id=resolved_room_id,
            thread_id=thread_id,
        )
        if effective_thread_id is None:
            return self._payload(
                "error",
                action="unresolve",
                room_id=resolved_room_id,
                message="thread_id is required when no active thread context is available for the target room.",
            )

        if canonical:
            target_thread_id = effective_thread_id
        else:
            target_thread_id = await normalize_thread_root_event_id(
                context.client,
                resolved_room_id,
                effective_thread_id,
            )
            if target_thread_id is None:
                return self._payload(
                    "error",
                    action="unresolve",
                    room_id=resolved_room_id,
                    thread_id=effective_thread_id,
                    message="Failed to resolve a canonical thread root for the target event.",
                )

        try:
            await clear_thread_resolution(
                context.client,
                resolved_room_id,
                target_thread_id,
                requester_user_id=context.requester_id,
            )
        except ThreadResolutionError as exc:
            return self._payload(
                "error",
                action="unresolve",
                room_id=resolved_room_id,
                thread_id=target_thread_id,
                message=str(exc),
            )

        updated_at = datetime.now(UTC).isoformat()
        return self._payload(
            "ok",
            action="unresolve",
            room_id=resolved_room_id,
            thread_id=target_thread_id,
            resolved=False,
            updated_by=context.requester_id,
            updated_at=updated_at,
        )
