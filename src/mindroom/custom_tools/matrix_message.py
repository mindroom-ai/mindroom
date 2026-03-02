"""Native Matrix messaging toolkit for send/read/react/reply actions."""

from __future__ import annotations

import json
import time
from collections import defaultdict, deque
from threading import Lock
from typing import TYPE_CHECKING, ClassVar

import nio
from agno.tools import Toolkit

from mindroom.attachments import load_attachment
from mindroom.authorization import is_authorized_sender
from mindroom.matrix.client import (
    fetch_thread_history,
    get_latest_thread_event_id_if_needed,
    send_file_message,
    send_message,
)
from mindroom.matrix.mentions import format_message_with_mentions
from mindroom.matrix.message_content import extract_and_resolve_message
from mindroom.tool_runtime_context import ToolRuntimeContext, get_tool_runtime_context

if TYPE_CHECKING:
    from pathlib import Path


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
    def _normalize_attachment_references(attachments: list[str] | None) -> tuple[list[str], str | None]:
        if attachments is None:
            return [], None

        normalized: list[str] = []
        for raw_reference in attachments:
            if not isinstance(raw_reference, str):
                return [], "attachments entries must be strings."
            reference = raw_reference.strip()
            if not reference:
                continue
            normalized.append(reference)
        return normalized, None

    @staticmethod
    def _resolve_context_attachment_path(
        context: ToolRuntimeContext,
        attachment_id: str,
    ) -> tuple[Path | None, str | None]:
        if context.storage_path is None:
            return None, "Attachment storage path is unavailable in this runtime path."
        if attachment_id not in context.attachment_ids:
            return None, f"Attachment ID is not available in this context: {attachment_id}"

        attachment = load_attachment(context.storage_path, attachment_id)
        if attachment is None:
            return None, f"Attachment metadata not found: {attachment_id}"
        if not attachment.local_path.is_file():
            return None, f"Attachment file is missing on disk: {attachment_id}"
        return attachment.local_path, None

    @classmethod
    def _resolve_attachment_paths(
        cls,
        context: ToolRuntimeContext,
        attachments: list[str],
    ) -> tuple[list[Path], list[str], str | None]:
        if not attachments:
            return [], [], None

        attachment_paths: list[Path] = []
        resolved_attachment_ids: list[str] = []
        for reference in attachments:
            if not reference.startswith("att_"):
                return [], [], "attachments entries must be context attachment IDs (att_*)."
            attachment_path, error = cls._resolve_context_attachment_path(context, reference)
            if error is not None:
                return [], [], error
            if attachment_path is None:
                continue
            attachment_paths.append(attachment_path)
            resolved_attachment_ids.append(reference)
        return attachment_paths, resolved_attachment_ids, None

    @staticmethod
    def _action_supports_attachments(action: str) -> bool:
        return action in {"send", "thread-reply", "reply"}

    def _validate_matrix_message_request(
        self,
        context: ToolRuntimeContext,
        *,
        action: str,
        room_id: str,
        attachments: list[str],
    ) -> str | None:
        supports_attachments = self._action_supports_attachments(action)
        if action not in self._VALID_ACTIONS:
            return self._payload(
                "error",
                action=action,
                message="Unsupported action. Use send, reply, thread-reply, react, read, or context.",
            )
        if attachments and not supports_attachments:
            return self._payload(
                "error",
                action=action,
                message="attachments are only supported for send, reply, and thread-reply actions.",
            )
        if supports_attachments and len(attachments) > self._MAX_ATTACHMENTS_PER_CALL:
            return self._payload(
                "error",
                action=action,
                message=f"attachments cannot exceed {self._MAX_ATTACHMENTS_PER_CALL} per call.",
            )
        if action != "context" and not self._room_access_allowed(context, room_id):
            return self._payload(
                "error",
                action=action,
                room_id=room_id,
                message="Not authorized to access the target room.",
            )
        return None

    @staticmethod
    def _room_access_allowed(context: ToolRuntimeContext, room_id: str) -> bool:
        if room_id == context.room_id:
            return True
        room_alias = room_id if room_id.startswith("#") else None
        return is_authorized_sender(
            context.requester_id,
            context.config,
            room_id,
            room_alias=room_alias,
        )

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

    async def _message_send_or_reply(
        self,
        context: ToolRuntimeContext,
        *,
        action: str,
        message: str | None,
        attachments: list[str],
        room_id: str,
        effective_thread_id: str | None,
    ) -> str:
        if action in {"thread-reply", "reply"} and effective_thread_id is None:
            return self._payload("error", action=action, message="thread_id is required for replies.")

        text = message.strip() if isinstance(message, str) and message.strip() else None
        attachment_paths, resolved_attachment_ids, attachment_error = self._resolve_attachment_paths(
            context,
            attachments,
        )
        if attachment_error is not None:
            return self._payload("error", action=action, room_id=room_id, message=attachment_error)

        if text is None and not attachment_paths:
            return self._payload(
                "error",
                action=action,
                room_id=room_id,
                message="At least one of message or attachments must be provided.",
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
        if room_id and not self._room_access_allowed(context, resolved_room_id):
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
        attachments: list[str],
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
                attachments=attachments,
                room_id=room_id,
                effective_thread_id=thread_id,
            )
        if action in {"thread-reply", "reply"}:
            effective_thread_id = self._safe_thread_id(context, room_id=room_id, thread_id=thread_id)
            return await self._message_send_or_reply(
                context,
                action=action,
                message=message,
                attachments=attachments,
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

    async def matrix_message(
        self,
        action: str = "send",
        message: str | None = None,
        attachments: list[str] | None = None,
        room_id: str | None = None,
        target: str | None = None,
        thread_id: str | None = None,
        limit: int | None = None,
    ) -> str:
        """Send/read/react/reply in Matrix with current room/thread defaults.

        Actions:
        - send: Send message text. Defaults to current room and room-level scope.
        - reply/thread-reply: Send message text in a thread. Defaults to current thread.
          Optional attachments accept context-scoped IDs (att_*).
        - react: React to target event ID with message text as emoji (defaults to 👍).
        - read: Read latest messages from room or current thread.
        - context: Return runtime room/thread/event metadata for tool targeting.

        """
        context = get_tool_runtime_context()
        if context is None:
            return self._context_error()

        normalized_action = action.strip().lower()
        normalized_attachments, attachment_error = self._normalize_attachment_references(attachments)
        if attachment_error is not None:
            return self._payload(
                "error",
                action=normalized_action or action,
                message=attachment_error,
            )
        resolved_room_id = room_id or context.room_id
        validation_error = self._validate_matrix_message_request(
            context,
            action=normalized_action,
            room_id=resolved_room_id,
            attachments=normalized_attachments,
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

        action_weight = 1 + len(normalized_attachments) if self._action_supports_attachments(normalized_action) else 1
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
            attachments=normalized_attachments,
            room_id=resolved_room_id,
            target=target,
            thread_id=thread_id,
            limit=limit,
        )
