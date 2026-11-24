"""Manual thread summary tool for AI agents."""

from __future__ import annotations

from mindroom.custom_tools.attachment_helpers import (
    resolve_canonical_tool_thread_target,
    resolve_requested_room_id,
    room_access_allowed,
)
from mindroom.custom_tools.tool_payloads import custom_tool_payload, tool_context_or_error
from mindroom.thread_summary.notices import ThreadSummaryWriteError, set_manual_thread_summary
from mindroom.tool_system.authoring import Toolkit


class ThreadSummaryTools(Toolkit):
    """Tools for manually setting Matrix thread summaries."""

    def __init__(self) -> None:
        super().__init__(
            name="thread_summary",
            tools=[self.set_thread_summary],
        )

    @staticmethod
    def _payload(status: str, **kwargs: object) -> str:
        return custom_tool_payload("thread_summary", status, **kwargs)

    async def set_thread_summary(  # noqa: PLR0911
        self,
        summary: str,
        thread_id: str | None = None,
        room_id: str | None = None,
        pin: bool = True,
    ) -> str:
        """Write a plain-text summary notice into the current or specified Matrix thread.

        Summary must be plain text (no markdown), maximum 300 characters.
        Pinning stops automatic re-summarization from overwriting the title, and
        is the default. Pass pin=False for a routine update that later automatic
        summaries may replace; that also releases an earlier pin.
        To retitle many threads, start from matrix_room(action="threads",
        include_summaries=True): each row shows the current summary and
        summary_pinned, so there is no need to read whole threads to find the
        titles or the threads already pinned.
        """
        if isinstance(context := tool_context_or_error(self._payload, "Thread summary", action="set"), str):
            return context
        resolved_room_id, room_error = await resolve_requested_room_id(context, room_id)
        if room_error is not None:
            return self._payload(
                "error",
                action="set",
                room_id=room_id,
                message=room_error,
            )
        assert resolved_room_id is not None

        if not await room_access_allowed(context, resolved_room_id):
            return self._payload(
                "error",
                action="set",
                room_id=resolved_room_id,
                message="Not authorized to access the target room.",
            )

        if not summary.strip():
            return self._payload(
                "error",
                action="set",
                room_id=resolved_room_id,
                message="summary must be a non-empty string.",
            )
        thread_target = await resolve_canonical_tool_thread_target(
            context,
            room_id=resolved_room_id,
            thread_id=thread_id,
            fail_closed_on_normalization_error=True,
        )
        if thread_target.error is not None:
            return self._payload(
                "error",
                action="set",
                room_id=resolved_room_id,
                thread_id=thread_target.requested_thread_id,
                message=thread_target.error,
            )
        assert thread_target.canonical_thread_id is not None
        normalized_thread_id = thread_target.canonical_thread_id

        try:
            result = await set_manual_thread_summary(
                context.client,
                resolved_room_id,
                normalized_thread_id,
                summary,
                config=context.config,
                runtime_paths=context.runtime_paths,
                conversation_reader=context.conversation_reader,
                entity_name=context.agent_name,
                membership_index=context.require_agent_reply_memberships(),
                pin=pin,
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
            pinned=pin,
        )
