"""Native Matrix messaging toolkit for send/read/react/reply actions."""

from __future__ import annotations

import json
import time
from collections import defaultdict, deque
from threading import Lock
from typing import ClassVar

import nio
from agno.tools import Toolkit

from mindroom.authorization import is_authorized_sender
from mindroom.matrix.client import fetch_thread_history, get_latest_thread_event_id_if_needed, send_message
from mindroom.matrix.mentions import format_message_with_mentions
from mindroom.matrix.message_content import extract_and_resolve_message
from mindroom.matrix_tool_context import MatrixMessageToolContext, get_matrix_message_tool_context


class MatrixMessageTools(Toolkit):
    """Native Matrix messaging actions for general agents."""

    _rate_limit_lock: ClassVar[Lock] = Lock()
    _recent_actions: ClassVar[dict[tuple[str, str, str], deque[float]]] = defaultdict(deque)
    _RATE_LIMIT_WINDOW_SECONDS: ClassVar[float] = 30.0
    _RATE_LIMIT_MAX_ACTIONS: ClassVar[int] = 12
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
    def _room_access_allowed(context: MatrixMessageToolContext, room_id: str) -> bool:
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
    def _check_rate_limit(cls, context: MatrixMessageToolContext, room_id: str) -> str | None:
        key = (context.agent_name, context.requester_id, room_id)
        now = time.monotonic()
        cutoff = now - cls._RATE_LIMIT_WINDOW_SECONDS

        with cls._rate_limit_lock:
            history = cls._recent_actions[key]
            while history and history[0] < cutoff:
                history.popleft()
            if len(history) >= cls._RATE_LIMIT_MAX_ACTIONS:
                return (
                    "Rate limit exceeded for matrix_message actions "
                    f"({cls._RATE_LIMIT_MAX_ACTIONS} per {int(cls._RATE_LIMIT_WINDOW_SECONDS)}s)."
                )
            history.append(now)

            # Prune keys whose deques are empty to avoid unbounded dict growth
            stale_keys = [k for k, v in cls._recent_actions.items() if not v]
            for k in stale_keys:
                del cls._recent_actions[k]

        return None

    async def _send_matrix_text(
        self,
        context: MatrixMessageToolContext,
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
        context: MatrixMessageToolContext,
        *,
        action: str,
        message: str | None,
        room_id: str,
        effective_thread_id: str | None,
    ) -> str:
        if message is None or not message.strip():
            return self._payload("error", action=action, message="Message cannot be empty.")
        if action in {"thread-reply", "reply"} and effective_thread_id is None:
            return self._payload("error", action=action, message="thread_id is required for replies.")

        event_id = await self._send_matrix_text(
            context,
            room_id=room_id,
            text=message.strip(),
            thread_id=effective_thread_id,
        )
        if event_id is None:
            return self._payload(
                "error",
                action=action,
                room_id=room_id,
                message="Failed to send message to Matrix.",
            )
        return self._payload(
            "ok",
            action=action,
            room_id=room_id,
            thread_id=effective_thread_id,
            event_id=event_id,
        )

    async def _message_react(
        self,
        context: MatrixMessageToolContext,
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
        context: MatrixMessageToolContext,
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

    def _message_context(
        self,
        context: MatrixMessageToolContext,
        *,
        room_id: str | None,
        thread_id: str | None,
        normalized_action: str,
    ) -> str:
        resolved_room_id = room_id or context.room_id
        resolved_thread_id = thread_id or context.thread_id
        if room_id and not self._room_access_allowed(context, resolved_room_id):
            return self._payload(
                "error",
                action=normalized_action,
                room_id=resolved_room_id,
                message="Not authorized to access the target room.",
            )
        return self._payload(
            "ok",
            action="context",
            room_id=resolved_room_id,
            thread_id=resolved_thread_id,
            reply_to_event_id=context.reply_to_event_id,
            requester_id=context.requester_id,
            agent_name=context.agent_name,
        )

    async def _dispatch_action(
        self,
        context: MatrixMessageToolContext,
        *,
        action: str,
        message: str | None,
        room_id: str,
        target: str | None,
        thread_id: str | None,
        limit: int | None,
    ) -> str:
        if action in {"send", "thread-reply", "reply"}:
            effective_thread_id = thread_id
            if action in {"thread-reply", "reply"} and effective_thread_id is None:
                effective_thread_id = context.thread_id
            return await self._message_send_or_reply(
                context,
                action=action,
                message=message,
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
            return await self._message_read(
                context,
                room_id=room_id,
                effective_thread_id=thread_id or context.thread_id,
                read_limit=self._read_limit(limit),
            )
        return self._payload(
            "error",
            action=action,
            message="Unsupported action. Use send, thread-reply, react, read, or context.",
        )

    async def matrix_message(
        self,
        action: str = "send",
        message: str | None = None,
        room_id: str | None = None,
        target: str | None = None,
        thread_id: str | None = None,
        limit: int | None = None,
    ) -> str:
        """Send/read/react/reply in Matrix with current room/thread defaults.

        Actions:
        - send: Send message text. Defaults to current room and room-level scope.
        - reply/thread-reply: Send message text in a thread. Defaults to current thread.
        - react: React to target event ID with message text as emoji (defaults to 👍).
        - read: Read latest messages from room or current thread.
        - context: Return runtime room/thread/event metadata for tool targeting.

        """
        context = get_matrix_message_tool_context()
        if context is None:
            return self._context_error()

        normalized_action = action.strip().lower()
        resolved_room_id = room_id or context.room_id

        if normalized_action == "context":
            return self._message_context(
                context,
                room_id=room_id,
                thread_id=thread_id,
                normalized_action=normalized_action,
            )

        if normalized_action not in self._VALID_ACTIONS:
            return self._payload(
                "error",
                action=normalized_action,
                message="Unsupported action. Use send, thread-reply, react, read, or context.",
            )

        if not self._room_access_allowed(context, resolved_room_id):
            return self._payload(
                "error",
                action=normalized_action,
                room_id=resolved_room_id,
                message="Not authorized to access the target room.",
            )

        if (limit_error := self._check_rate_limit(context, resolved_room_id)) is not None:
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
            room_id=resolved_room_id,
            target=target,
            thread_id=thread_id,
            limit=limit,
        )
