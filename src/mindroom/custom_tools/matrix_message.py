"""Native Matrix messaging toolkit for send/read/react/reply actions."""

from __future__ import annotations

import json
import time
from collections import defaultdict, deque
from threading import Lock
from typing import ClassVar

import nio
from agno.tools import Toolkit

from mindroom.custom_tools.attachment_helpers import (
    normalize_str_list,
    resolve_attachment_file_paths,
    resolve_attachment_ids,
    room_access_allowed,
)
from mindroom.matrix.client import (
    fetch_thread_history,
    get_latest_thread_event_id_if_needed,
    send_file_message,
    send_message,
)
from mindroom.matrix.mentions import format_message_with_mentions
from mindroom.matrix.message_content import extract_and_resolve_message
from mindroom.tool_runtime_context import (
    ToolRuntimeContext,
    get_tool_runtime_context,
)


class MatrixMessageTools(Toolkit):
    """Native Matrix messaging actions for general agents."""

    _rate_limit_lock: ClassVar[Lock] = Lock()
    _recent_actions: ClassVar[dict[tuple[str, str, str], deque[float]]] = defaultdict(deque)
    _RATE_LIMIT_WINDOW_SECONDS: ClassVar[float] = 30.0
    _RATE_LIMIT_MAX_ACTIONS: ClassVar[int] = 12
    _MAX_ATTACHMENTS_PER_CALL: ClassVar[int] = 5
    _DEFAULT_READ_LIMIT: ClassVar[int] = 20
    _MAX_READ_LIMIT: ClassVar[int] = 50
    _VALID_ACTIONS: ClassVar[frozenset[str]] = frozenset(
        {"send", "thread-reply", "reply", "react", "read", "context"},
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
                message="Unsupported action. Use send, reply, thread-reply, react, read, or context.",
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

    @classmethod
    def _check_rate_limit(
        cls,
        context: ToolRuntimeContext,
        room_id: str,
        *,
        weight: int = 1,
    ) -> str | None:
        key = (context.agent_name, context.requester_id, room_id)
        now = time.monotonic()
        cutoff = now - cls._RATE_LIMIT_WINDOW_SECONDS
        action_weight = max(1, weight)

        with cls._rate_limit_lock:
            history = cls._recent_actions[key]
            while history and history[0] < cutoff:
                history.popleft()
            if len(history) + action_weight > cls._RATE_LIMIT_MAX_ACTIONS:
                return (
                    "Rate limit exceeded for matrix_message actions "
                    f"({cls._RATE_LIMIT_MAX_ACTIONS} per {int(cls._RATE_LIMIT_WINDOW_SECONDS)}s)."
                )
            history.extend(now for _ in range(action_weight))

            # Time-prune all keys and remove empty ones to avoid unbounded dict growth
            stale_keys: list[tuple[str, str, str]] = []
            for k, v in cls._recent_actions.items():
                if k == key:
                    continue  # already pruned above
                while v and v[0] < cutoff:
                    v.popleft()
                if not v:
                    stale_keys.append(k)
            for k in stale_keys:
                del cls._recent_actions[k]

        return None

    async def _send_matrix_text(
        self,
        context: ToolRuntimeContext,
        *,
        room_id: str,
        text: str,
        thread_id: str | None,
    ) -> str | None:
        latest_thread_event_id = await get_latest_thread_event_id_if_needed(
            context.client,
            room_id,
            thread_id,
        )
        content = format_message_with_mentions(
            context.config,
            text,
            sender_domain=context.config.domain,
            thread_event_id=thread_id,
            latest_thread_event_id=latest_thread_event_id,
        )
        return await send_message(context.client, room_id, content)

    async def _message_send_or_reply(  # noqa: PLR0911
        self,
        context: ToolRuntimeContext,
        *,
        action: str,
        message: str | None,
        attachment_ids: list[str],
        attachment_file_paths: list[str],
        room_id: str,
        effective_thread_id: str | None,
    ) -> str:
        if action in {"thread-reply", "reply"} and effective_thread_id is None:
            return self._payload("error", action=action, message="thread_id is required for replies.")

        text = message.strip() if isinstance(message, str) and message.strip() else None
        attachment_paths, resolved_attachment_ids, attachment_error = resolve_attachment_ids(
            context,
            attachment_ids,
        )
        if attachment_error is not None:
            return self._payload("error", action=action, room_id=room_id, message=attachment_error)
        file_path_attachments, newly_registered_attachment_ids, file_path_error = resolve_attachment_file_paths(
            context,
            attachment_file_paths,
        )
        if file_path_error is not None:
            return self._payload("error", action=action, room_id=room_id, message=file_path_error)
        attachment_paths.extend(file_path_attachments)
        resolved_attachment_ids.extend(newly_registered_attachment_ids)

        if text is None and not attachment_paths:
            return self._payload(
                "error",
                action=action,
                room_id=room_id,
                message="At least one of message, attachment_ids, or attachment_file_paths must be provided.",
            )

        event_id: str | None = None
        if text is not None:
            event_id = await self._send_matrix_text(
                context,
                room_id=room_id,
                text=text,
                thread_id=effective_thread_id,
            )
        if text is not None and event_id is None:
            return self._payload(
                "error",
                action=action,
                room_id=room_id,
                message="Failed to send message to Matrix.",
            )

        attachment_event_ids: list[str] = []
        for attachment_path in attachment_paths:
            attachment_event_id = await send_file_message(
                context.client,
                room_id,
                attachment_path,
                thread_id=effective_thread_id,
            )
            if attachment_event_id is None:
                return self._payload(
                    "error",
                    action=action,
                    room_id=room_id,
                    thread_id=effective_thread_id,
                    event_id=event_id,
                    attachment_event_ids=attachment_event_ids,
                    resolved_attachment_ids=resolved_attachment_ids,
                    newly_registered_attachment_ids=newly_registered_attachment_ids,
                    message=f"Failed to send attachment: {attachment_path}",
                )
            attachment_event_ids.append(attachment_event_id)

        return self._payload(
            "ok",
            action=action,
            room_id=room_id,
            thread_id=effective_thread_id,
            event_id=event_id,
            attachment_event_ids=attachment_event_ids,
            resolved_attachment_ids=resolved_attachment_ids,
            newly_registered_attachment_ids=newly_registered_attachment_ids,
        )

    async def _message_react(
        self,
        context: ToolRuntimeContext,
        *,
        message: str | None,
        room_id: str,
        target: str | None,
    ) -> str:
        if target is None:
            return self._payload("error", action="react", message="target event_id is required.")

        reaction = message.strip() if message and message.strip() else "👍"
        content = {
            "m.relates_to": {
                "rel_type": "m.annotation",
                "event_id": target,
                "key": reaction,
            },
        }
        response = await context.client.room_send(
            room_id=room_id,
            message_type="m.reaction",
            content=content,
        )
        if isinstance(response, nio.RoomSendResponse):
            return self._payload(
                "ok",
                action="react",
                room_id=room_id,
                target=target,
                reaction=reaction,
                event_id=response.event_id,
            )
        return self._payload(
            "error",
            action="react",
            room_id=room_id,
            target=target,
            reaction=reaction,
            response=str(response),
        )

    async def _message_read(
        self,
        context: ToolRuntimeContext,
        *,
        room_id: str,
        effective_thread_id: str | None,
        read_limit: int,
    ) -> str:
        if effective_thread_id is not None:
            thread_messages = await fetch_thread_history(context.client, room_id, effective_thread_id)
            return self._payload(
                "ok",
                action="read",
                room_id=room_id,
                thread_id=effective_thread_id,
                limit=read_limit,
                messages=thread_messages[-read_limit:],
            )

        response = await context.client.room_messages(
            room_id,
            limit=read_limit,
            direction=nio.MessageDirection.back,
            message_filter={"types": ["m.room.message"]},
        )
        if not isinstance(response, nio.RoomMessagesResponse):
            return self._payload(
                "error",
                action="read",
                room_id=room_id,
                response=str(response),
            )

        resolved = [
            await extract_and_resolve_message(event, context.client)
            for event in reversed(response.chunk)
            if isinstance(event, nio.RoomMessageText)
        ]
        return self._payload(
            "ok",
            action="read",
            room_id=room_id,
            limit=read_limit,
            messages=resolved,
        )

    @staticmethod
    def _safe_thread_id(
        context: ToolRuntimeContext,
        *,
        room_id: str,
        thread_id: str | None,
    ) -> str | None:
        """Return thread_id only when it belongs to the target room.

        When the caller targets a different room, the current context's
        thread_id is invalid there, so we only fall back to it for the
        same room.
        """
        if thread_id is not None:
            return thread_id
        if room_id == context.room_id:
            return context.resolved_thread_id
        return None

    def _message_context(
        self,
        context: ToolRuntimeContext,
        *,
        room_id: str | None,
        thread_id: str | None,
        normalized_action: str,
    ) -> str:
        resolved_room_id = room_id or context.room_id
        resolved_thread_id = self._safe_thread_id(
            context,
            room_id=resolved_room_id,
            thread_id=thread_id,
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

    async def _dispatch_action(
        self,
        context: ToolRuntimeContext,
        *,
        action: str,
        message: str | None,
        attachment_ids: list[str],
        attachment_file_paths: list[str],
        room_id: str,
        target: str | None,
        thread_id: str | None,
        limit: int | None,
    ) -> str:
        if action == "send":
            return await self._message_send_or_reply(
                context,
                action=action,
                message=message,
                attachment_ids=attachment_ids,
                attachment_file_paths=attachment_file_paths,
                room_id=room_id,
                effective_thread_id=thread_id,
            )
        if action in {"thread-reply", "reply"}:
            effective_thread_id = self._safe_thread_id(context, room_id=room_id, thread_id=thread_id)
            return await self._message_send_or_reply(
                context,
                action=action,
                message=message,
                attachment_ids=attachment_ids,
                attachment_file_paths=attachment_file_paths,
                room_id=room_id,
                effective_thread_id=effective_thread_id,
            )
        if action == "react":
            return await self._message_react(
                context,
                message=message,
                room_id=room_id,
                target=target,
            )
        if action == "read":
            safe_thread = self._safe_thread_id(context, room_id=room_id, thread_id=thread_id)
            return await self._message_read(
                context,
                room_id=room_id,
                effective_thread_id=safe_thread,
                read_limit=self._read_limit(limit),
            )
        return self._payload(
            "error",
            action=action,
            message="Unsupported action. Use send, reply, thread-reply, react, read, or context.",
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
        limit: int | None = None,
    ) -> str:
        """Send/read/react/reply in Matrix with current room/thread defaults.

        Actions:
        - send: Send message text. Defaults to current room and room-level scope.
        - reply/thread-reply: Send message text in a thread. Defaults to current thread.
          Optional attachments accept context-scoped IDs (`attachment_ids`) and/or
          local file paths (`attachment_file_paths`).
        - react: React to target event ID with message text as emoji (defaults to 👍).
        - read: Read latest messages from room or current thread.
        - context: Return runtime room/thread/event metadata for tool targeting.

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

        return await self._dispatch_action(
            context,
            action=normalized_action,
            message=message,
            attachment_ids=normalized_attachment_ids,
            attachment_file_paths=normalized_attachment_file_paths,
            room_id=resolved_room_id,
            target=target,
            thread_id=thread_id,
            limit=limit,
        )
