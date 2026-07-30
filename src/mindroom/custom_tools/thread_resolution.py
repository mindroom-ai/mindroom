"""Explicit thread lifecycle tools for AI agents."""

from __future__ import annotations

from agno.tools import Toolkit

from mindroom.custom_tools.tool_payloads import custom_tool_payload
from mindroom.thread_tags import RESOLVED_THREAD_TAG, ThreadTagsError, remove_thread_tag, set_thread_tag
from mindroom.tool_system.runtime_context import ToolRuntimeContext, get_tool_runtime_context


class ThreadResolutionTools(Toolkit):
    """Tools for resolving or reopening the active Matrix thread."""

    def __init__(self) -> None:
        super().__init__(
            name="thread_resolution",
            tools=[self.resolve_thread, self.reopen_thread],
        )

    @staticmethod
    def _payload(status: str, **kwargs: object) -> str:
        return custom_tool_payload("thread_resolution", status, **kwargs)

    @classmethod
    def _thread_context(cls) -> tuple[ToolRuntimeContext, str] | str:
        context = get_tool_runtime_context()
        if context is None:
            return cls._payload("error", message="Thread resolution tool context is unavailable in this runtime path.")
        if context.resolved_thread_id is None:
            return cls._payload("error", message="Thread resolution requires an active thread context.")
        return context, context.resolved_thread_id

    async def resolve_thread(self) -> str:
        """Mark the active Matrix thread as resolved."""
        resolved = self._thread_context()
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

    async def reopen_thread(self) -> str:
        """Remove resolved state from the active Matrix thread."""
        resolved = self._thread_context()
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
