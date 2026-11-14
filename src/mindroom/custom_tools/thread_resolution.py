"""Explicit thread lifecycle tools for AI agents."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.custom_tools.attachment_helpers import resolve_current_room_thread_root
from mindroom.custom_tools.tool_payloads import custom_tool_payload, tool_context_or_error
from mindroom.logging_config import get_logger
from mindroom.thread_tags import (
    RESOLVED_THREAD_TAG,
    SNOOZED_THREAD_TAG,
    ThreadTagsError,
    remove_thread_tag,
    set_thread_tag,
)
from mindroom.tool_system.authoring import Toolkit

if TYPE_CHECKING:
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

logger = get_logger(__name__)


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
        if isinstance(context := tool_context_or_error(cls._payload, "Thread resolution"), str):
            return context
        root_id, error = await resolve_current_room_thread_root(context, thread_id)
        if error is not None:
            if thread_id is None:
                return cls._payload("error", message=error)
            return cls._payload("error", thread_id=thread_id, message=error)
        assert root_id is not None
        return context, root_id

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
            state = await set_thread_tag(
                context.client,
                context.room_id,
                thread_id,
                RESOLVED_THREAD_TAG,
                set_by=context.requester_id,
            )
        except ThreadTagsError as exc:
            return self._payload("error", action="resolve", thread_id=thread_id, message=str(exc))
        if state is not None and SNOOZED_THREAD_TAG in state.tags:
            await self._end_snooze(context, thread_id)
        return self._payload(
            "ok",
            action="resolve",
            room_id=context.room_id,
            thread_id=thread_id,
            resolved=True,
        )

    @staticmethod
    async def _end_snooze(context: ToolRuntimeContext, thread_id: str) -> None:
        """Drop the snooze of a thread that was just resolved; the resolve stands if that fails."""
        try:
            await remove_thread_tag(
                context.client,
                context.room_id,
                thread_id,
                SNOOZED_THREAD_TAG,
                requester_user_id=context.requester_id,
            )
        except ThreadTagsError as exc:
            logger.warning(
                "thread_resolution_snooze_not_removed",
                room_id=context.room_id,
                thread_id=thread_id,
                error=str(exc),
            )
            return
        if context.orchestrator is not None:
            context.orchestrator.thread_snooze.unsnoozed(context.room_id, thread_id)

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
