# ruff: noqa: INP001
"""Agent-facing tools for the MindRoom thread-snooze plugin."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from agno.tools import Toolkit

from mindroom.logging_config import get_logger
from mindroom.thread_tags import (
    ThreadTagRecord,
    ThreadTagsError,
    get_thread_tags,
    remove_thread_tag,
    set_thread_tag,
)
from mindroom.tool_system.metadata import (
    SetupType,
    ToolCategory,
    ToolStatus,
    register_tool_with_metadata,
)
from mindroom.tool_system.runtime_context import (
    ToolRuntimeContext,
    get_tool_runtime_context,
    resolve_tool_runtime_hook_bindings,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import ModuleType

LOGGER = get_logger(__name__)


def _payload(status: str, **kwargs: object) -> str:
    payload: dict[str, object] = {"status": status, "tool": "thread_snooze"}
    payload.update(kwargs)
    return json.dumps(payload, sort_keys=True)


def _serialized_tags(tags: Mapping[str, ThreadTagRecord]) -> dict[str, dict[str, object]]:
    return {tag: record.model_dump(mode="json", exclude_none=True) for tag, record in tags.items()}


def _resolved_scope(
    context: ToolRuntimeContext,
) -> tuple[str, str]:
    thread_root_id = context.resolved_thread_id or context.thread_id
    if thread_root_id is None:
        msg = "An active thread is required to snooze or unsnooze a thread."
        raise ValueError(msg)
    return context.room_id, thread_root_id


def _context_error(action: str, *, message: str) -> str:
    return _payload("error", action=action, message=message)


def _hooks_module() -> ModuleType:
    """Import the hooks module lazily so tools share the live hook state."""
    from . import hooks as hooks_module  # noqa: PLC0415

    return hooks_module


def _build_wake_bindings(context: ToolRuntimeContext) -> tuple[Any, Any, Any]:
    bindings = resolve_tool_runtime_hook_bindings(context)

    async def send_message(room_id: str, text: str, thread_id: str | None) -> str | None:
        if bindings.message_sender is None:
            LOGGER.warning("No message sender available for snooze wake", room_id=room_id, thread_id=thread_id)
            return None
        return await bindings.message_sender(
            room_id,
            text,
            thread_id,
            "thread-snooze:wake",
            None,
            trigger_dispatch=False,
        )

    async def put_room_state(room_id: str, event_type: str, state_key: str, content: dict[str, Any]) -> bool:
        if bindings.room_state_putter is None:
            LOGGER.warning("No room state putter available for snooze wake", room_id=room_id, state_key=state_key)
            return False
        return await bindings.room_state_putter(room_id, event_type, state_key, content)

    async def query_room_state(
        room_id: str,
        event_type: str,
        state_key: str | None = None,
    ) -> dict[str, Any] | None:
        if bindings.room_state_querier is None:
            LOGGER.warning("No room state querier available for snooze wake", room_id=room_id, state_key=state_key)
            return None
        return await bindings.room_state_querier(room_id, event_type, state_key)

    return send_message, put_room_state, query_room_state


def _schedule_snooze_wake(
    context: ToolRuntimeContext,
    *,
    room_id: str,
    thread_root_id: str,
    until: datetime,
) -> None:
    hooks_module = _hooks_module()
    send_message, put_room_state, query_room_state = _build_wake_bindings(context)

    async def wake() -> None:
        await hooks_module._wake_thread(
            room_id=room_id,
            thread_root_id=thread_root_id,
            expected_until=until,
            query_room_state=query_room_state,
            send_message=send_message,
            put_room_state=put_room_state,
            logger=LOGGER,
        )

    hooks_module._spawn_snooze_task(
        room_id,
        thread_root_id,
        until,
        wake=wake,
        logger=LOGGER,
    )


class ThreadSnoozeTools(Toolkit):
    """Toolkit for snoozing and waking Matrix threads."""

    def __init__(self) -> None:
        super().__init__(
            name="thread_snooze",
            instructions=(
                "Use these tools to snooze the current thread until an exact ISO-8601 datetime. "
                "Naive datetimes are treated as UTC, and snoozes must be in the future."
            ),
            tools=[self.snooze_thread, self.unsnooze_thread],
        )

    async def snooze_thread(self, until: str, note: str | None = None) -> str:  # noqa: PLR0911
        """Snooze the current thread until an exact future datetime."""
        context = get_tool_runtime_context()
        if context is None:
            return _context_error("snooze", message="Thread snooze tool context is unavailable in this runtime path.")

        try:
            room_id, thread_root_id = _resolved_scope(context)
        except ValueError as exc:
            return _context_error("snooze", message=str(exc))

        hooks_module = _hooks_module()
        parsed_until = hooks_module.parse_snooze_until(until)
        snooze_tag = hooks_module.SNOOZE_TAG
        if parsed_until is None:
            return _context_error("snooze", message="until must be an ISO-8601 datetime.")
        if parsed_until <= datetime.now(UTC):
            return _context_error("snooze", message="until must be in the future.")

        snooze_data = {"until": parsed_until.isoformat()}

        try:
            final_state = await set_thread_tag(
                context.client,
                room_id,
                thread_root_id,
                snooze_tag,
                set_by=context.requester_id,
                note=note,
                data=snooze_data,
            )
        except ThreadTagsError as exc:
            return _context_error("snooze", message=str(exc))

        try:
            _schedule_snooze_wake(
                context,
                room_id=room_id,
                thread_root_id=thread_root_id,
                until=parsed_until,
            )
        except Exception as exc:
            try:
                await remove_thread_tag(
                    context.client,
                    room_id,
                    thread_root_id,
                    snooze_tag,
                    requester_user_id=context.requester_id,
                )
            except ThreadTagsError:
                LOGGER.exception(
                    "Failed to roll back snooze tag after wake scheduling failure",
                    room_id=room_id,
                    thread_id=thread_root_id,
                )
            return _context_error("snooze", message=f"Failed to schedule snooze wake: {exc}")

        return _payload(
            "ok",
            action="snooze",
            room_id=room_id,
            thread_id=thread_root_id,
            until=parsed_until.isoformat(),
            tags=_serialized_tags(final_state.tags),
        )

    async def unsnooze_thread(self) -> str:
        """Remove the snooze state from the current thread."""
        context = get_tool_runtime_context()
        if context is None:
            return _context_error("unsnooze", message="Thread snooze tool context is unavailable in this runtime path.")

        try:
            room_id, thread_root_id = _resolved_scope(context)
        except ValueError as exc:
            return _context_error("unsnooze", message=str(exc))

        try:
            existing_state = await get_thread_tags(context.client, room_id, thread_root_id)
        except ThreadTagsError as exc:
            return _context_error("unsnooze", message=str(exc))

        hooks_module = _hooks_module()
        snooze_tag = hooks_module.SNOOZE_TAG
        if existing_state is None or snooze_tag not in existing_state.tags:
            return _context_error("unsnooze", message="Thread is not snoozed.")

        try:
            final_state = await remove_thread_tag(
                context.client,
                room_id,
                thread_root_id,
                snooze_tag,
                requester_user_id=context.requester_id,
            )
        except ThreadTagsError as exc:
            return _context_error("unsnooze", message=str(exc))

        hooks_module._cancel_snooze_task(room_id, thread_root_id)

        return _payload(
            "ok",
            action="unsnooze",
            room_id=room_id,
            thread_id=thread_root_id,
            tags=_serialized_tags(final_state.tags),
        )


@register_tool_with_metadata(
    name="thread_snooze",
    display_name="Thread Snooze",
    description="Snooze the current thread until an exact future datetime and wake it automatically.",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
)
def thread_snooze_factory() -> type[ThreadSnoozeTools]:
    """Factory function for the thread-snooze toolkit."""
    return ThreadSnoozeTools


__all__ = ["ThreadSnoozeTools", "thread_snooze_factory"]
