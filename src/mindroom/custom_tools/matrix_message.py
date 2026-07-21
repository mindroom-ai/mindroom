"""Native Matrix messaging toolkit for send/read/react/reply actions."""

from __future__ import annotations

from collections import defaultdict, deque
from pathlib import Path  # noqa: TC003 - tool config sync evaluates constructor type hints at runtime.
from threading import Lock
from typing import ClassVar

from agno.tools import Toolkit

from mindroom.custom_tools import matrix_conversation_operations
from mindroom.custom_tools.attachment_helpers import (
    normalize_str_list,
    resolve_context_thread_id,
    resolve_optional_room_id,
    room_access_allowed,
)
from mindroom.custom_tools.matrix_helpers import check_rate_limit
from mindroom.custom_tools.tool_payloads import custom_tool_payload
from mindroom.matrix.message_extras import MessageExtraSection, parse_message_extra_sections
from mindroom.tool_system.runtime_context import ToolRuntimeContext, get_tool_runtime_context


class MatrixMessageTools(Toolkit):
    """Native Matrix messaging actions for general agents."""

    _rate_limit_lock: ClassVar[Lock] = Lock()
    _recent_actions: ClassVar[dict[tuple[str, str, str], deque[float]]] = defaultdict(deque)
    _RATE_LIMIT_WINDOW_SECONDS: ClassVar[float] = 30.0
    _RATE_LIMIT_MAX_ACTIONS: ClassVar[int] = 12
    _MAX_ATTACHMENTS_PER_CALL: ClassVar[int] = 5
    _DEFAULT_READ_LIMIT: ClassVar[int] = 20
    _MAX_READ_LIMIT: ClassVar[int] = 50
    _ROOM_TIMELINE_SENTINEL: ClassVar[str] = "room"
    _MESSAGE_EXTRAS_ACTIONS: ClassVar[frozenset[str]] = frozenset({"send", "thread-reply", "reply", "edit"})
    _VALID_ACTIONS: ClassVar[frozenset[str]] = frozenset(
        {"send", "thread-reply", "reply", "react", "read", "room-threads", "thread-list", "edit", "context"},
    )

    def __init__(self, *, tool_output_workspace_root: Path | None = None) -> None:
        self._operations = matrix_conversation_operations.MatrixMessageOperations(
            tool_output_workspace_root=tool_output_workspace_root,
        )
        super().__init__(
            name="matrix_message",
            tools=[self.matrix_message],
        )

    @staticmethod
    def _payload(status: str, **kwargs: object) -> str:
        return custom_tool_payload("matrix_message", status, **kwargs)

    @classmethod
    def _operation_result_payload(
        cls,
        result: matrix_conversation_operations.MatrixMessageOperationResult,
    ) -> str:
        return cls._payload(result.status, **result.fields)

    @classmethod
    def _context_error(cls) -> str:
        return cls._payload(
            "error",
            message="Matrix messaging tool context is unavailable in this runtime path.",
        )

    @classmethod
    def _read_limit(cls, limit: int | None) -> int:
        if limit is None:
            return cls._DEFAULT_READ_LIMIT
        return max(1, min(limit, cls._MAX_READ_LIMIT))

    @staticmethod
    def _action_supports_attachments(action: str) -> bool:
        return action in {"send", "thread-reply", "reply"}

    @classmethod
    def _action_supports_message_extras(cls, action: str) -> bool:
        return action in cls._MESSAGE_EXTRAS_ACTIONS

    def _validate_matrix_message_request(
        self,
        context: ToolRuntimeContext,
        *,
        action: str,
        room_id: str,
        attachment_count: int,
    ) -> str | None:
        supports_attachments = self._action_supports_attachments(action)
        if action not in self._VALID_ACTIONS:
            return self._payload(
                "error",
                action=action,
                message=(
                    "Unsupported action. Use send, reply, thread-reply, react, read, room-threads, thread-list, edit, or context."
                ),
            )
        if attachment_count and not supports_attachments:
            return self._payload(
                "error",
                action=action,
                message="attachment_ids and attachment_file_paths are only supported for send, reply, and thread-reply actions.",
            )
        if supports_attachments and attachment_count > self._MAX_ATTACHMENTS_PER_CALL:
            return self._payload(
                "error",
                action=action,
                message=(
                    f"attachment_ids plus attachment_file_paths cannot exceed "
                    f"{self._MAX_ATTACHMENTS_PER_CALL} per call."
                ),
            )
        if action != "context" and not room_access_allowed(context, room_id):
            return self._payload(
                "error",
                action=action,
                room_id=room_id,
                message="Not authorized to access the target room.",
            )
        return None

    def _validate_message_extras(
        self,
        *,
        action: str,
        message_extras: list[dict[str, object]] | None,
    ) -> tuple[list[MessageExtraSection] | None, str | None]:
        if not message_extras:
            return None, None
        if not self._action_supports_message_extras(action):
            allowed_actions = ", ".join(sorted(self._MESSAGE_EXTRAS_ACTIONS))
            return None, self._payload(
                "error",
                action=action,
                message=f"message_extras is only supported for {allowed_actions} actions.",
            )
        try:
            return parse_message_extra_sections(message_extras), None
        except (TypeError, ValueError) as exc:
            return None, self._payload(
                "error",
                action=action,
                message=str(exc),
            )

    def _validate_optional_room_id(self, *, action: str, room_id: object) -> str | None:
        if room_id is not None and not isinstance(room_id, str):
            return self._payload(
                "error",
                action=action,
                message="room_id must be a string.",
            )
        return None

    @classmethod
    def _check_rate_limit(
        cls,
        context: ToolRuntimeContext,
        room_id: str,
        *,
        weight: int = 1,
    ) -> str | None:
        return check_rate_limit(
            lock=cls._rate_limit_lock,
            recent_actions=cls._recent_actions,
            window_seconds=cls._RATE_LIMIT_WINDOW_SECONDS,
            max_actions=cls._RATE_LIMIT_MAX_ACTIONS,
            tool_name="matrix_message",
            context=context,
            room_id=room_id,
            weight=weight,
        )

    def _message_context(
        self,
        context: ToolRuntimeContext,
        *,
        room_id: str | None,
        thread_id: str | None,
        normalized_action: str,
    ) -> str:
        resolved_room_id = resolve_optional_room_id(context, room_id)
        resolved_thread_id = resolve_context_thread_id(
            context,
            room_id=resolved_room_id,
            thread_id=thread_id,
            allow_context_fallback=True,
            room_timeline_sentinel=self._ROOM_TIMELINE_SENTINEL,
        )
        if room_id and not room_access_allowed(context, resolved_room_id):
            return self._payload(
                "error",
                action=normalized_action,
                room_id=resolved_room_id,
                message="Not authorized to access the target room.",
            )
        reply_to = context.reply_to_event_id if resolved_room_id == context.room_id else None
        return self._payload(
            "ok",
            action="context",
            room_id=resolved_room_id,
            thread_id=resolved_thread_id,
            reply_to_event_id=reply_to,
            requester_id=context.requester_id,
            agent_name=context.agent_name,
        )

    async def matrix_message(  # noqa: PLR0911
        self,
        action: str = "send",
        message: str | None = None,
        attachment_ids: list[str] | None = None,
        attachment_file_paths: list[str] | None = None,
        room_id: str | None = None,
        target: str | None = None,
        thread_id: str | None = None,
        ignore_mentions: bool = True,
        message_extras: list[dict[str, object]] | None = None,
        limit: int | None = None,
        page_token: str | None = None,
    ) -> str:
        """Send, reply, react to, read, edit, or inspect Matrix messages.

        Actions:
        - `send`: Send text/attachments; defaults to the current room timeline.
        - `reply`: Send text/attachments to the current or explicit thread; errors without one.
        - `thread-reply`: Alias of `reply` with the same thread behavior.
        - `react`: React to `target` with `message`, defaulting to 👍.
        - `read`: Read the active thread, or the room timeline without one.
        - `room-threads`: Page through room thread roots with `page_token`.
        - `thread-list`: List current/explicit thread messages and edit options.
        - `edit`: Edit the `target` event, inheriting the current thread.
        - `context`: Return targeting, requester, and agent metadata.

        Threading: `send` is room-level even inside a thread; an explicit thread ID targets that thread. `reply` and `thread-reply` inherit the current thread. `thread_id="room"` forces room scope and prevents thread inheritance.

        Mention safety for text send/reply/thread-reply: default `ignore_mentions=True` sets `com.mindroom.skip_mentions` and suppresses dispatch to prevent loops. Set `False` ONLY for an intentional handoff or self-trigger; then human requesters use `com.mindroom.original_sender` for authorization.

        Attachments: only `send`, `reply`, and `thread-reply` accept context-scoped `att_*` IDs or local file paths, combined maximum 5. Include text, attachments, or both, but not neither. Relative paths resolve from the agent workspace.

        `message_extras` adds collapsible sections to send/reply/thread-reply/edit. Each uses `title`, `content`, optional `collapsed`, and `content_type`: `text/plain`, `text/markdown` (default), or `text/html`; basic fragments only: no scripts/styles/forms/media/SVG/math/interactive elements; links only `http`/`https`/`mailto`.

        Full semantics: https://docs.mindroom.chat/tools/matrix-message/

        Args:
            action (str): Action.
            message (str | None): Text/edit body or reaction emoji.
            attachment_ids (list[str] | None): `att_*` IDs; writes only, combined max 5.
            attachment_file_paths (list[str] | None): Local paths; same limits.
            room_id (str | None): Target room; current by default.
            target (str | None): Event ID for react/edit.
            thread_id (str | None): Thread; `"room"` forces room scope.
            ignore_mentions (bool): `True` except intentional dispatch.
            message_extras (list[dict[str, object]] | None): Collapsible sections.
            limit (int | None): 1-50; default 20.
            page_token (str | None): Next threads page.

        """
        context = get_tool_runtime_context()
        if context is None:
            return self._context_error()

        normalized_action = action.strip().lower()
        normalized_attachment_ids, attachment_ids_error = normalize_str_list(
            attachment_ids,
            field_name="attachment_ids",
        )
        if attachment_ids_error is not None:
            return self._payload(
                "error",
                action=normalized_action or action,
                message=attachment_ids_error,
            )
        normalized_attachment_file_paths, attachment_file_paths_error = normalize_str_list(
            attachment_file_paths,
            field_name="attachment_file_paths",
        )
        if attachment_file_paths_error is not None:
            return self._payload(
                "error",
                action=normalized_action or action,
                message=attachment_file_paths_error,
            )
        if (room_id_error := self._validate_optional_room_id(action=normalized_action, room_id=room_id)) is not None:
            return room_id_error
        resolved_room_id = resolve_optional_room_id(context, room_id)
        attachment_count = len(normalized_attachment_ids) + len(normalized_attachment_file_paths)
        validation_error = self._validate_matrix_message_request(
            context,
            action=normalized_action,
            room_id=resolved_room_id,
            attachment_count=attachment_count,
        )
        if validation_error is not None:
            return validation_error
        parsed_message_extras, extras_error = self._validate_message_extras(
            action=normalized_action,
            message_extras=message_extras,
        )
        if extras_error is not None:
            return extras_error

        if normalized_action == "context":
            return self._message_context(
                context,
                room_id=room_id,
                thread_id=thread_id,
                normalized_action=normalized_action,
            )

        action_weight = 1 + attachment_count if self._action_supports_attachments(normalized_action) else 1
        if (limit_error := self._check_rate_limit(context, resolved_room_id, weight=action_weight)) is not None:
            return self._payload(
                "error",
                action=normalized_action,
                room_id=resolved_room_id,
                message=limit_error,
            )

        result = await self._operations.dispatch_action(
            context,
            action=normalized_action,
            message=message,
            attachment_ids=normalized_attachment_ids,
            attachment_file_paths=normalized_attachment_file_paths,
            room_id=resolved_room_id,
            target=target,
            thread_id=thread_id,
            ignore_mentions=ignore_mentions,
            message_extras=parsed_message_extras,
            read_limit=self._read_limit(limit),
            page_token=page_token,
            room_timeline_sentinel=self._ROOM_TIMELINE_SENTINEL,
        )
        return self._operation_result_payload(result)
