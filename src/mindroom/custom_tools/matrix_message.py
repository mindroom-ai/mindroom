"""Native Matrix messaging toolkit for send/read/react/reply actions."""

from __future__ import annotations

import json
import time
from collections import defaultdict, deque
from threading import Lock
from typing import Any, ClassVar

import nio
from agno.tools import Toolkit

from mindroom.constants import ORIGINAL_SENDER_KEY
from mindroom.custom_tools.attachment_helpers import (
    normalize_str_list,
    resolve_context_thread_id,
    room_access_allowed,
)
from mindroom.custom_tools.attachments import send_context_attachments
from mindroom.interactive import (
    add_reaction_buttons,
    parse_and_format_interactive,
    register_interactive_question,
    should_create_interactive_question,
)
from mindroom.matrix.client import (
    edit_message,
    fetch_thread_history,
    get_latest_thread_event_id_if_needed,
    send_message,
)
from mindroom.matrix.mentions import format_message_with_mentions
from mindroom.matrix.message_content import extract_and_resolve_message
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
        {"send", "thread-reply", "reply", "react", "read", "thread-list", "edit", "context"},
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
                message=(
                    "Unsupported action. Use send, reply, thread-reply, react, read, thread-list, edit, or context."
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
        ignore_mentions: bool,
    ) -> str | None:
        formatted_text = parse_and_format_interactive(text, extract_mapping=False).formatted_text
        latest_thread_event_id = await get_latest_thread_event_id_if_needed(
            context.client,
            room_id,
            thread_id,
        )
        extra_content: dict[str, Any] = {}
        if ignore_mentions:
            extra_content["com.mindroom.skip_mentions"] = True
        elif context.requester_id != context.client.user_id:
            extra_content[ORIGINAL_SENDER_KEY] = context.requester_id
        content = format_message_with_mentions(
            context.config,
            context.runtime_paths,
            formatted_text,
            sender_domain=context.config.get_domain(context.runtime_paths),
            thread_event_id=thread_id,
            latest_thread_event_id=latest_thread_event_id,
            extra_content=extra_content or None,
        )
        return await send_message(context.client, room_id, content)

    async def _maybe_add_interactive_question(
        self,
        context: ToolRuntimeContext,
        *,
        original_text: str | None,
        event_id: str | None,
        room_id: str,
        thread_id: str | None,
    ) -> None:
        if original_text is None or event_id is None or not should_create_interactive_question(original_text):
            return

        response = parse_and_format_interactive(original_text, extract_mapping=True)
        if not response.option_map or not response.options_list:
            return

        register_interactive_question(
            event_id,
            room_id,
            thread_id,
            response.option_map,
            context.agent_name,
        )
        await add_reaction_buttons(
            context.client,
            room_id,
            event_id,
            response.options_list,
        )

    async def _message_send_or_reply(
        self,
        context: ToolRuntimeContext,
        *,
        action: str,
        message: str | None,
        attachment_ids: list[str],
        attachment_file_paths: list[str],
        room_id: str,
        effective_thread_id: str | None,
        ignore_mentions: bool,
    ) -> str:
        if action in {"thread-reply", "reply"} and effective_thread_id is None:
            return self._payload("error", action=action, message="thread_id is required for replies.")

        text = message.strip() if isinstance(message, str) and message.strip() else None
        if text is None and not attachment_ids and not attachment_file_paths:
            return self._payload(
                "error",
                action=action,
                room_id=room_id,
                message="At least one of message, attachment_ids, or attachment_file_paths must be provided.",
            )

        original_text = text
        event_id: str | None = None
        if text is not None:
            event_id = await self._send_matrix_text(
                context,
                room_id=room_id,
                text=text,
                thread_id=effective_thread_id,
                ignore_mentions=ignore_mentions,
            )
        if text is not None and event_id is None:
            return self._payload(
                "error",
                action=action,
                room_id=room_id,
                message="Failed to send message to Matrix.",
            )
        await self._maybe_add_interactive_question(
            context,
            original_text=original_text,
            event_id=event_id,
            room_id=room_id,
            thread_id=effective_thread_id,
        )

        attachment_event_ids: list[str] = []
        resolved_attachment_ids: list[str] = []
        newly_registered_attachment_ids: list[str] = []
        if attachment_ids or attachment_file_paths:
            send_result, send_error = await send_context_attachments(
                context,
                attachment_ids=attachment_ids,
                attachment_file_paths=attachment_file_paths,
                room_id=room_id,
                thread_id=effective_thread_id,
                require_joined_room=False,
                inherit_context_thread=False,
            )
            if send_error is not None:
                if send_result is None:
                    return self._payload(
                        "error",
                        action=action,
                        room_id=room_id,
                        thread_id=effective_thread_id,
                        event_id=event_id,
                        message=send_error,
                    )
                return self._payload(
                    "error",
                    action=action,
                    room_id=send_result.room_id,
                    thread_id=send_result.thread_id,
                    event_id=event_id,
                    attachment_event_ids=send_result.attachment_event_ids,
                    resolved_attachment_ids=send_result.resolved_attachment_ids,
                    newly_registered_attachment_ids=send_result.newly_registered_attachment_ids,
                    message=send_error,
                )
            assert send_result is not None
            attachment_event_ids = send_result.attachment_event_ids
            resolved_attachment_ids = send_result.resolved_attachment_ids
            newly_registered_attachment_ids = send_result.newly_registered_attachment_ids

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
            return await self._thread_read_payload(
                context,
                action="read",
                room_id=room_id,
                thread_id=effective_thread_id,
                read_limit=read_limit,
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
    def _message_preview(body: object, max_length: int = 120) -> str:
        if not isinstance(body, str):
            return ""
        compact = " ".join(body.split())
        if len(compact) <= max_length:
            return compact
        return f"{compact[: max_length - 3].rstrip()}..."

    def _build_edit_options(
        self,
        context: ToolRuntimeContext,
        *,
        messages: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        current_user_id = context.client.user_id
        options: list[dict[str, object]] = []
        for message in reversed(messages):
            event_id = message.get("event_id")
            sender = message.get("sender")
            if not isinstance(event_id, str) or not isinstance(sender, str):
                continue
            can_edit = current_user_id is not None and sender == current_user_id
            option: dict[str, object] = {
                "event_id": event_id,
                "sender": sender,
                "can_edit": can_edit,
                "body_preview": self._message_preview(message.get("body")),
            }
            if can_edit:
                option["edit_action"] = {"action": "edit", "target": event_id}
            options.append(option)
        return options

    async def _thread_read_payload(
        self,
        context: ToolRuntimeContext,
        *,
        action: str,
        room_id: str,
        thread_id: str,
        read_limit: int,
    ) -> str:
        thread_messages = await fetch_thread_history(context.client, room_id, thread_id)
        recent_messages = thread_messages[-read_limit:]
        return self._payload(
            "ok",
            action=action,
            room_id=room_id,
            thread_id=thread_id,
            limit=read_limit,
            messages=recent_messages,
            edit_options=self._build_edit_options(context, messages=recent_messages),
        )

    async def _message_thread_list(
        self,
        context: ToolRuntimeContext,
        *,
        room_id: str,
        thread_id: str | None,
        read_limit: int,
    ) -> str:
        if thread_id is None:
            return self._payload(
                "error",
                action="thread-list",
                room_id=room_id,
                message="thread_id is required for thread-list when no thread context is active.",
            )
        return await self._thread_read_payload(
            context,
            action="thread-list",
            room_id=room_id,
            thread_id=thread_id,
            read_limit=read_limit,
        )

    async def _message_edit(
        self,
        context: ToolRuntimeContext,
        *,
        room_id: str,
        thread_id: str | None,
        target: str | None,
        message: str | None,
    ) -> str:
        if target is None:
            return self._payload("error", action="edit", message="target event_id is required for edit.")
        new_text = message.strip() if isinstance(message, str) and message.strip() else None
        if new_text is None:
            return self._payload("error", action="edit", message="message is required for edit.")

        latest_thread_event_id: str | None = None
        if thread_id is not None:
            thread_messages = await fetch_thread_history(context.client, room_id, thread_id)
            if thread_messages:
                maybe_latest = thread_messages[-1].get("event_id")
                if isinstance(maybe_latest, str) and maybe_latest:
                    latest_thread_event_id = maybe_latest
            if latest_thread_event_id is None:
                latest_thread_event_id = target

        content = format_message_with_mentions(
            context.config,
            context.runtime_paths,
            new_text,
            sender_domain=context.config.get_domain(context.runtime_paths),
            thread_event_id=thread_id,
            latest_thread_event_id=latest_thread_event_id,
        )
        edit_event_id = await edit_message(context.client, room_id, target, content, new_text)
        if edit_event_id is None:
            return self._payload(
                "error",
                action="edit",
                room_id=room_id,
                thread_id=thread_id,
                target=target,
                message="Failed to edit message in Matrix.",
            )

        return self._payload(
            "ok",
            action="edit",
            room_id=room_id,
            thread_id=thread_id,
            target=target,
            event_id=edit_event_id,
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
        ignore_mentions: bool,
        limit: int | None,
    ) -> str:
        if action in {"send", "thread-reply", "reply"}:
            allow_context_fallback = action in {"thread-reply", "reply"}
            effective_thread_id = resolve_context_thread_id(
                context,
                room_id=room_id,
                thread_id=thread_id,
                allow_context_fallback=allow_context_fallback,
                room_timeline_sentinel=self._ROOM_TIMELINE_SENTINEL,
            )
            return await self._message_send_or_reply(
                context,
                action=action,
                message=message,
                attachment_ids=attachment_ids,
                attachment_file_paths=attachment_file_paths,
                room_id=room_id,
                effective_thread_id=effective_thread_id,
                ignore_mentions=ignore_mentions,
            )
        if action == "react":
            return await self._message_react(
                context,
                message=message,
                room_id=room_id,
                target=target,
            )
        if action == "read":
            safe_thread = resolve_context_thread_id(
                context,
                room_id=room_id,
                thread_id=thread_id,
                room_timeline_sentinel=self._ROOM_TIMELINE_SENTINEL,
            )
            return await self._message_read(
                context,
                room_id=room_id,
                effective_thread_id=safe_thread,
                read_limit=self._read_limit(limit),
            )
        if action == "thread-list":
            safe_thread = resolve_context_thread_id(
                context,
                room_id=room_id,
                thread_id=thread_id,
                room_timeline_sentinel=self._ROOM_TIMELINE_SENTINEL,
            )
            return await self._message_thread_list(
                context,
                room_id=room_id,
                thread_id=safe_thread,
                read_limit=self._read_limit(limit),
            )
        if action == "edit":
            safe_thread = resolve_context_thread_id(
                context,
                room_id=room_id,
                thread_id=thread_id,
                room_timeline_sentinel=self._ROOM_TIMELINE_SENTINEL,
            )
            return await self._message_edit(
                context,
                room_id=room_id,
                thread_id=safe_thread,
                target=target,
                message=message,
            )
        return self._payload(
            "error",
            action=action,
            message=("Unsupported action. Use send, reply, thread-reply, react, read, thread-list, edit, or context."),
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
    ) -> str:
        """Send, reply, react to, read, edit, or inspect Matrix messages using current room and thread context defaults.

        Actions:
        - send: Send text and optional attachments to a room.
          It defaults to the current room and stays room-level unless you explicitly pass `thread_id`.
        - reply: Send text and optional attachments into a thread.
          It defaults to the current thread when one can be resolved and errors if no thread is available.
        - thread-reply: Same threading behavior as `reply`, kept as a separate action name for agent convenience.
        - react: React to `target` with `message` as the emoji, defaulting to thumbs-up when `message` is empty.
        - read: Read recent messages from the current thread when one is active, otherwise from the room timeline.
        - thread-list: List messages in a thread and include edit options keyed by event ID.
          It uses the current thread when one is active, otherwise you must pass `thread_id`.
        - edit: Edit a previously sent message identified by `target`.
          It uses the current thread by default when editing from threaded context.
        - context: Return room, thread, reply target, requester, and agent metadata so you can plan a later tool call.

        Thread targeting:
        - `send` is room-level by default even if the current conversation is inside a thread.
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
            action (str): Supported actions are `send`, `reply`, `thread-reply`, `react`, `read`, `thread-list`, `edit`, and `context`; they send text or attachments, react to an event, read messages, list a thread, edit a prior event, or return targeting metadata.
            message (str | None): Text body for `send`, `reply`, `thread-reply`, and `edit`; reaction emoji for `react` with a thumbs-up default when empty; use `None` for `read` and `context`.
            attachment_ids (list[str] | None): Context-scoped `att_*` attachment IDs; only valid for `send`, `reply`, and `thread-reply`, and the combined total with `attachment_file_paths` cannot exceed 5.
            attachment_file_paths (list[str] | None): Local file paths to register and send in the current context; only valid for `send`, `reply`, and `thread-reply`, and the combined total with `attachment_ids` cannot exceed 5.
            room_id (str | None): Optional target room ID or alias; defaults to the current room context when omitted.
            target (str | None): Event ID to react to for `react` or to edit for `edit`.
            thread_id (str | None): Optional explicit thread target; `thread_id="room"` forces room-level scope instead of inheriting the current thread.
            ignore_mentions (bool): Text-send safety flag for `send`, `reply`, and `thread-reply`; default `True` writes `com.mindroom.skip_mentions=True` to suppress mention-triggered agent dispatch, while `False` keeps mentions active and also writes `com.mindroom.original_sender=<human requester id>` when the requester is not the sending bot.
            limit (int | None): Maximum messages returned for `read` or `thread-list`; defaults to 20 and is capped at 50.

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
            ignore_mentions=ignore_mentions,
            limit=limit,
        )
