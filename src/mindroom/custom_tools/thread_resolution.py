"""Explicit thread lifecycle tools for AI agents."""

from __future__ import annotations

from agno.tools import Toolkit

from mindroom.custom_tools.attachment_helpers import resolve_canonical_tool_thread_target
from mindroom.custom_tools.tool_payloads import custom_tool_payload
from mindroom.matrix.thread_room_scan import resolve_thread_root_event_id_for_client
from mindroom.thread_tags import RESOLVED_THREAD_TAG, ThreadTagsError, remove_thread_tag, set_thread_tag
from mindroom.tool_system.runtime_context import ToolRuntimeContext, get_tool_runtime_context


class ThreadResolutionTools(Toolkit):
    """Tools for resolving or reopening Matrix threads in the current room."""

    def __init__(self) -> None:
        super().__init__(
            name="thread_resolution",
            tools=[self.resolve_thread, self.reopen_thread],
        )

    @staticmethod
    def _payload(status: str, **kwargs: object) -> str:
        return custom_tool_payload("thread_resolution", status, **kwargs)

    @classmethod
    async def _thread_context(cls, thread_id: str | None) -> tuple[ToolRuntimeContext, str] | str:
        context = get_tool_runtime_context()
        if context is None:
            return cls._payload("error", message="Thread resolution tool context is unavailable in this runtime path.")
        if thread_id is None:
            if context.resolved_thread_id is None:
                return cls._payload(
                    "error",
                    message="thread_id is required when no active thread context is available.",
                )
            return context, context.resolved_thread_id

        target = await resolve_canonical_tool_thread_target(
            context,
            room_id=context.room_id,
            thread_id=thread_id,
            normalize_thread_id=lambda room_id, event_id: resolve_thread_root_event_id_for_client(
                context.client,
                room_id,
                event_id,
                relations=context.relations,
            ),
            fail_closed_on_normalization_error=True,
        )
        if target.error is not None:
            return cls._payload("error", thread_id=target.requested_thread_id, message=target.error)
        assert target.canonical_thread_id is not None
        return context, target.canonical_thread_id

    async def resolve_thread(self, thread_id: str | None = None) -> str:
        """Mark the current or specified Matrix thread in the current room as resolved.

        Args:
            thread_id: Thread root or reply event ID in the current room.
                Omit to use the active thread.

        """
        resolved = await self._thread_context(thread_id)
        if isinstance(resolved, str):
            return resolved
        context, thread_id = resolved
        try:
            await set_thread_tag(
                context.client,
                context.room_id,
                thread_id,
                RESOLVED_THREAD_TAG,
                set_by=context.requester_id,
            )
        except ThreadTagsError as exc:
            return self._payload("error", action="resolve", thread_id=thread_id, message=str(exc))
        return self._payload(
            "ok",
            action="resolve",
            room_id=context.room_id,
            thread_id=thread_id,
            resolved=True,
        )

    async def reopen_thread(self, thread_id: str | None = None) -> str:
        """Remove resolved state from the current or specified Matrix thread in the current room.

        Args:
            thread_id: Thread root or reply event ID in the current room.
                Omit to use the active thread.

        """
        resolved = await self._thread_context(thread_id)
        if isinstance(resolved, str):
            return resolved
        context, thread_id = resolved
        try:
            await remove_thread_tag(
                context.client,
                context.room_id,
                thread_id,
                RESOLVED_THREAD_TAG,
                requester_user_id=context.requester_id,
            )
        except ThreadTagsError as exc:
            return self._payload("error", action="reopen", thread_id=thread_id, message=str(exc))
        return self._payload(
            "ok",
            action="reopen",
            room_id=context.room_id,
            thread_id=thread_id,
            resolved=False,
        )
