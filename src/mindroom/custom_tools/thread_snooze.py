"""Thread snooze tools for AI agents."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from mindroom.custom_tools.attachment_helpers import resolve_current_room_thread_root
from mindroom.custom_tools.tool_payloads import custom_tool_payload, tool_context_or_error
from mindroom.thread_tags import (
    SNOOZED_THREAD_TAG,
    ThreadTagsError,
    get_thread_tag_content,
    remove_thread_tag,
    set_thread_tag,
)
from mindroom.tool_system.authoring import Toolkit
from mindroom.tools.native_helpers import validate_action_params

if TYPE_CHECKING:
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

_NOT_A_DATETIME = "until must be an ISO-8601 datetime."
_SnoozeAction = Literal["snooze", "unsnooze"]
_ALLOWED_PARAMS: dict[str, frozenset[str]] = {
    "snooze": frozenset({"until", "note", "thread_id"}),
    "unsnooze": frozenset({"thread_id"}),
}


def _parse_until(until: object) -> datetime | str:
    """Return the wake time in UTC, or the error message to give the agent."""
    if not isinstance(until, str) or "T" not in (text := until.strip()).upper():
        return _NOT_A_DATETIME
    try:
        parsed = datetime.fromisoformat(text)
        parsed = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        return _NOT_A_DATETIME
    if parsed <= datetime.now(UTC):
        return "until must be in the future."
    return parsed


class ThreadSnoozeTools(Toolkit):
    """Tools for snoozing Matrix threads in the current room until a set time."""

    offer_output_path = False

    def __init__(self) -> None:
        super().__init__(
            name="thread_snooze",
            tools=[self.thread_snooze],
        )

    @staticmethod
    def _payload(status: str, **kwargs: object) -> str:
        return custom_tool_payload("thread_snooze", status, **kwargs)

    @classmethod
    async def _thread_context(cls, thread_id: str | None) -> tuple[ToolRuntimeContext, str] | str:
        if isinstance(context := tool_context_or_error(cls._payload, "Thread snooze"), str):
            return context
        root_id, error = await resolve_current_room_thread_root(context, thread_id)
        if error is not None:
            if thread_id is None:
                return cls._payload("error", message=error)
            return cls._payload("error", thread_id=thread_id, message=error)
        assert root_id is not None
        return context, root_id

    async def thread_snooze(
        self,
        action: _SnoozeAction,
        until: str | None = None,
        note: str | None = None,
        thread_id: str | None = None,
    ) -> str:
        """Snooze or unsnooze the current or specified Matrix thread in the current room.

        - `snooze`: snooze until `until` (required future ISO-8601 datetime with time; naive = UTC) and wake it with a message; optional `note` quoted on wake, `thread_id` (omit for active thread).
        - `unsnooze`: cancel the snooze of `thread_id` (omit for active thread).
        """
        if (
            error := validate_action_params(
                "thread_snooze",
                action,
                _ALLOWED_PARAMS,
                {"until": until, "note": note, "thread_id": thread_id},
            )
        ) is not None:
            return self._payload("error", **({"action": action} if action in _ALLOWED_PARAMS else {}), message=error)
        if action == "snooze" and until is None:
            return self._payload("error", action="snooze", message="until is required for action='snooze'.")
        resolved = await self._thread_context(thread_id)
        if isinstance(resolved, str):
            return resolved
        context, thread_id = resolved
        if action == "snooze":
            return await self._snooze(context, thread_id, until, note)
        return await self._unsnooze(context, thread_id)

    async def _snooze(self, context: ToolRuntimeContext, thread_id: str, until: object, note: str | None) -> str:
        wake_at = _parse_until(until)
        if isinstance(wake_at, str):
            return self._payload("error", action="snooze", thread_id=thread_id, message=wake_at)
        try:
            await set_thread_tag(
                context.client,
                context.room_id,
                thread_id,
                SNOOZED_THREAD_TAG,
                set_by=context.requester_id,
                note=note,
                data={"until": wake_at.isoformat()},
            )
        except ThreadTagsError as exc:
            return self._payload("error", action="snooze", thread_id=thread_id, message=str(exc))
        if context.orchestrator is not None:
            context.orchestrator.thread_snooze.snoozed(
                context.room_id,
                thread_id,
                until=wake_at,
                note=note,
                set_by=context.requester_id,
            )
        return self._payload(
            "ok",
            action="snooze",
            room_id=context.room_id,
            thread_id=thread_id,
            until=wake_at.isoformat(),
            snoozed=True,
        )

    async def _unsnooze(self, context: ToolRuntimeContext, thread_id: str) -> str:
        try:
            if await get_thread_tag_content(context.client, context.room_id, thread_id, SNOOZED_THREAD_TAG) is None:
                return self._payload("error", action="unsnooze", thread_id=thread_id, message="Thread is not snoozed.")
            state = await remove_thread_tag(
                context.client,
                context.room_id,
                thread_id,
                SNOOZED_THREAD_TAG,
                requester_user_id=context.requester_id,
            )
        except ThreadTagsError as exc:
            return self._payload("error", action="unsnooze", thread_id=thread_id, message=str(exc))
        # A snooze written again meanwhile survives the removal and must keep waking.
        snoozed = SNOOZED_THREAD_TAG in state.tags
        if context.orchestrator is not None and not snoozed:
            context.orchestrator.thread_snooze.unsnoozed(context.room_id, thread_id)
        return self._payload(
            "ok",
            action="unsnooze",
            room_id=context.room_id,
            thread_id=thread_id,
            snoozed=snoozed,
        )
