"""Explicit thread lifecycle tools for AI agents."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

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
from mindroom.tools.native_helpers import validate_action_params

if TYPE_CHECKING:
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

logger = get_logger(__name__)

_ResolutionAction = Literal["resolve", "reopen"]
_ALLOWED_PARAMS: dict[str, frozenset[str]] = {
    "resolve": frozenset({"thread_id"}),
    "reopen": frozenset({"thread_id"}),
}


class ThreadResolutionTools(Toolkit):
    """Tools for resolving or reopening Matrix threads in the current room."""

    offer_output_path = False

    def __init__(self) -> None:
        super().__init__(
            name="thread_resolution",
            tools=[self.thread_resolution],
        )

    @staticmethod
    def _payload(status: str, **kwargs: object) -> str:
        return custom_tool_payload("thread_resolution", status, **kwargs)

    @classmethod
    async def _thread_context(
        cls,
        action: _ResolutionAction,
        thread_id: str | None,
    ) -> tuple[ToolRuntimeContext, str] | str:
        if isinstance(context := tool_context_or_error(cls._payload, "Thread resolution", action=action), str):
            return context
        root_id, error = await resolve_current_room_thread_root(context, thread_id, allow_standalone_root=True)
        if error is not None:
            if thread_id is None:
                return cls._payload("error", action=action, message=error)
            return cls._payload("error", action=action, thread_id=thread_id, message=error)
        assert root_id is not None
        return context, root_id

    @staticmethod
    async def _end_snooze(context: ToolRuntimeContext, thread_id: str) -> None:
        """Drop the snooze of a thread that was just resolved; the resolve stands if that fails."""
        try:
            state = await remove_thread_tag(
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
        # A snooze written again meanwhile survives the removal and must keep waking.
        if context.orchestrator is not None and SNOOZED_THREAD_TAG not in state.tags:
            context.orchestrator.thread_snooze.unsnoozed(context.room_id, thread_id)

    async def thread_resolution(
        self,
        action: _ResolutionAction,
        thread_id: str | None = None,
    ) -> str:
        """Resolve or reopen the current or specified Matrix thread in the current room.

        - `resolve`: mark `thread_id` (omit for active thread) as resolved and end any active snooze.
        - `reopen`: remove resolved state from `thread_id` (omit for active thread).
        """
        if (
            error := validate_action_params("thread_resolution", action, _ALLOWED_PARAMS, {"thread_id": thread_id})
        ) is not None:
            return self._payload("error", **({"action": action} if action in _ALLOWED_PARAMS else {}), message=error)
        resolved = await self._thread_context(action, thread_id)
        if isinstance(resolved, str):
            return resolved
        context, thread_id = resolved
        if action == "resolve":
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
            context.resolved_thread_ids.add(thread_id)
            if state is not None and SNOOZED_THREAD_TAG in state.tags:
                await self._end_snooze(context, thread_id)
            return self._payload(
                "ok",
                action="resolve",
                room_id=context.room_id,
                thread_id=thread_id,
                resolved=True,
            )
        try:
            state = await remove_thread_tag(
                context.client,
                context.room_id,
                thread_id,
                RESOLVED_THREAD_TAG,
                requester_user_id=context.requester_id,
            )
        except ThreadTagsError as exc:
            return self._payload("error", action="reopen", thread_id=thread_id, message=str(exc))
        is_resolved = RESOLVED_THREAD_TAG in state.tags
        if not is_resolved:
            context.resolved_thread_ids.discard(thread_id)
        return self._payload(
            "ok",
            action="reopen",
            room_id=context.room_id,
            thread_id=thread_id,
            resolved=is_resolved,
        )
