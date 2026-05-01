"""Native Matrix messaging toolkit for send/read/react/reply actions."""

from __future__ import annotations

import json
from collections import defaultdict, deque
from threading import Lock
from typing import ClassVar

from agno.tools import Toolkit

from mindroom.custom_tools import matrix_conversation_operations
from mindroom.custom_tools.attachment_helpers import normalize_str_list, resolve_context_thread_id, room_access_allowed
from mindroom.custom_tools.matrix_helpers import check_rate_limit
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
    _VALID_ACTIONS: ClassVar[frozenset[str]] = frozenset(
        {"send", "thread-reply", "reply", "react", "read", "room-threads", "thread-list", "edit", "context"},
    )
    _operations: ClassVar[matrix_conversation_operations.MatrixMessageOperations] = (
        matrix_conversation_operations.MatrixMessageOperations()
    )

    def __init__(self) -> None:
        super().__init__(
            name="matrix_message",
            tools=[self.matrix_message],
        )

    @staticmethod
    def _payload(status: str, **kwargs: object) -> str:
        payload: dict[str, object] = {"status": status, "tool": "matrix_message"}
        payload.update(kwargs)
        return json.dumps(payload, sort_keys=True)

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
                    f"attachment_ids plus attachment_file_paths cannot exceed {self._MAX_ATTACHMENTS_PER_CALL} per call."
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
        resolved_room_id = room_id or context.room_id
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
        limit: int | None = None,
        page_token: str | None = None,
    ) -> str:
        """Send, reply, react to, read, edit, or inspect Matrix messages using current room and thread context defaults.

        Actions:
        - send: Send text and optional attachments to a room.
          It defaults to the current room.
          When the effective target is room-level, text+attachment sends post the text to the room timeline and thread attachments under that text event.
          When the effective target is room-level and you send multiple attachments without text, the first attachment is posted to the room timeline and the remaining attachments are threaded under it.
          In `thread_mode: room`, room-level sends stay plain room messages and do not auto-thread attachments unless you pass an explicit `thread_id`.
        - reply: Send text and optional attachments into a thread.
          It defaults to the current thread when one can be resolved and errors if no thread is available.
        - thread-reply: Same threading behavior as `reply`, kept as a separate action name for agent convenience.
        - react: React to `target` with `message` as the emoji, defaulting to thumbs-up when `message` is empty.
        - read: Read recent messages from the current thread when one is active, otherwise from the room timeline.
        - room-threads: List thread roots in a room with pagination support via `page_token`.
        - thread-list: List messages in a thread and include edit options keyed by event ID.
          It uses the current thread when one is active, otherwise you must pass `thread_id`.
        - edit: Edit a previously sent message identified by `target`.
          It uses the current thread by default when editing from threaded context.
        - context: Return room, thread, reply target, requester, and agent metadata so you can plan a later tool call.

        Thread targeting:
        - `send` is room-level by default even if the current conversation is inside a thread.
        - `send` only creates a new attachment thread when its effective thread target is room-level.
          If you pass an explicit `thread_id`, both text and attachments stay in that existing thread.
        - `thread_mode: room` disables implicit attachment auto-threading for room-level sends.
          Pass an explicit `thread_id` when you intentionally want threaded output from the tool.
        - `reply` and `thread-reply` inherit the current thread when possible.
        - `read`, `edit`, and `context` also inherit the current thread when possible.
        - `thread_id="room"` is a sentinel meaning "force room-level scope and do not inherit the current thread."
          Use it when you want the room timeline instead of the active thread.

        Mention handling with `ignore_mentions`:
        - This flag only affects text sends for `send`, `reply`, and `thread-reply`.
        - Default `True`: the tool writes `com.mindroom.skip_mentions=True` into the outgoing event content.
          The bot runtime checks that flag and suppresses mention-triggered agent dispatch, so visible mentions do not page agents.
        - `False`: the tool does not set the skip flag, so normal mention handling stays active.
          When the requester is a human rather than the sending bot, the tool also writes `com.mindroom.original_sender=<human requester id>`, not the bot ID.
          Downstream authorization and reply-permission checks then treat the event as coming from the original human requester.
        - self-trigger: an agent can mention itself with `ignore_mentions=False` to intentionally create a new turn.
          Use the same pattern for deliberate cross-agent handoffs when another agent should actually wake up and respond.

        Safety:
        - The default `ignore_mentions=True` exists to prevent accidental infinite loops and noisy mutual paging between agents.
        - Set `ignore_mentions=False` only for intentional dispatch.
          Prefer one deliberate handoff message over repeated self-mentions or agent-to-agent pings.

        Attachments:
        - Attachments are only supported for `send`, `reply`, and `thread-reply`.
        - `attachment_ids` are context-scoped `att_*` IDs.
        - `attachment_file_paths` are local file paths that will be registered into the current attachment context before sending.
        - The combined limit of `attachment_ids` plus `attachment_file_paths` is 5 per call.
        - A send or reply call may include text, attachments, or both, but not neither.

        Args:
            action (str): Supported actions are `send`, `reply`, `thread-reply`, `react`, `read`, `room-threads`, `thread-list`, `edit`, and `context`; they send text or attachments, react to an event, read messages, list room thread roots or thread messages, edit a prior event, or return targeting metadata.
            message (str | None): Text body for `send`, `reply`, `thread-reply`, and `edit`; reaction emoji for `react` with a thumbs-up default when empty; use `None` for `read`, `room-threads`, `thread-list`, and `context`.
            attachment_ids (list[str] | None): Context-scoped `att_*` attachment IDs; only valid for `send`, `reply`, and `thread-reply`, and the combined total with `attachment_file_paths` cannot exceed 5.
            attachment_file_paths (list[str] | None): Local file paths to register and send in the current context; only valid for `send`, `reply`, and `thread-reply`, and the combined total with `attachment_ids` cannot exceed 5.
            room_id (str | None): Optional target room ID or alias; defaults to the current room context when omitted.
            target (str | None): Event ID to react to for `react` or to edit for `edit`.
            thread_id (str | None): Optional explicit thread target; `thread_id="room"` forces room-level scope instead of inheriting the current thread.
            ignore_mentions (bool): Text-send safety flag for `send`, `reply`, and `thread-reply`; default `True` writes `com.mindroom.skip_mentions=True` to suppress mention-triggered agent dispatch, while `False` keeps mentions active and also writes `com.mindroom.original_sender=<human requester id>` when the requester is not the sending bot.
            limit (int | None): Maximum messages returned for `read` or `thread-list`, or thread roots returned for `room-threads`; defaults to 20 and is capped at 50.
            page_token (str | None): Pagination token for `room-threads`, returned by a previous `room-threads` call to fetch the next page of thread roots.

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
        resolved_room_id = room_id or context.room_id
        attachment_count = len(normalized_attachment_ids) + len(normalized_attachment_file_paths)
        validation_error = self._validate_matrix_message_request(
            context,
            action=normalized_action,
            room_id=resolved_room_id,
            attachment_count=attachment_count,
        )
        if validation_error is not None:
            return validation_error

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
            read_limit=self._read_limit(limit),
            page_token=page_token,
            room_timeline_sentinel=self._ROOM_TIMELINE_SENTINEL,
        )
        return self._operation_result_payload(result)
