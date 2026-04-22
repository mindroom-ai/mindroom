"""Native Matrix messaging toolkit for send/read/react/reply actions."""

from __future__ import annotations

import json
from collections import defaultdict, deque
from threading import Lock
from typing import TYPE_CHECKING, Any, ClassVar

import nio
from agno.tools import Toolkit

from mindroom.constants import ORIGINAL_SENDER_KEY
from mindroom.custom_tools.attachment_helpers import (
    normalize_str_list,
    resolve_context_thread_id,
    room_access_allowed,
)
from mindroom.custom_tools.attachments import (
    # Intentional cross-module reuse: matrix_message controls attachment auto-threading
    # directly here without widening the higher-level attachments tool API.
    _resolve_send_attachments,
    _send_attachment_paths,
    send_context_attachments,
)
from mindroom.custom_tools.matrix_helpers import (
    check_rate_limit,
    message_preview,
    thread_root_body_preview,
)
from mindroom.interactive import (
    add_reaction_buttons,
    clear_interactive_question,
    parse_and_format_interactive,
    register_interactive_question,
    should_create_interactive_question,
)
from mindroom.logging_config import get_logger
from mindroom.matrix.client_delivery import (
    edit_message_result,
    send_file_message,
    send_message_result,
)
from mindroom.matrix.client_thread_history import RoomThreadsPageError, get_room_threads_page
from mindroom.matrix.mentions import format_message_with_mentions
from mindroom.matrix.message_content import extract_and_resolve_message
from mindroom.matrix.visible_body import configured_visible_body_sender_ids
from mindroom.tool_system.runtime_context import (
    ToolRuntimeContext,
    get_tool_runtime_context,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage

logger = get_logger(__name__)


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
    _VISIBLE_ROOM_MESSAGE_EVENT_TYPES: ClassVar[tuple[type[nio.RoomMessageText], type[nio.RoomMessageNotice]]] = (
        nio.RoomMessageText,
        nio.RoomMessageNotice,
    )
    _VALID_ACTIONS: ClassVar[frozenset[str]] = frozenset(
        {"send", "thread-reply", "reply", "react", "read", "room-threads", "thread-list", "edit", "context"},
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
        latest_thread_event_id = await context.conversation_cache.get_latest_thread_event_id_if_needed(
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
        delivered = await send_message_result(context.client, room_id, content)
        if delivered is not None:
            context.conversation_cache.notify_outbound_message(
                room_id,
                delivered.event_id,
                delivered.content_sent,
            )
        if delivered is not None:
            return delivered.event_id
        return None

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

    async def _message_send_or_reply(  # noqa: C901, PLR0911, PLR0912
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
        attachment_thread_id: str | None = None
        if attachment_ids or attachment_file_paths:
            room_mode = (
                context.config.get_entity_thread_mode(
                    context.agent_name,
                    context.runtime_paths,
                    room_id=room_id,
                )
                == "room"
            )
            attachment_count = len(attachment_ids) + len(attachment_file_paths)
            if text is None and attachment_count > 1 and effective_thread_id is None and not room_mode:
                attachment_paths, resolved_attachment_ids, newly_registered_attachment_ids, resolve_error = (
                    _resolve_send_attachments(
                        context,
                        attachment_ids=attachment_ids,
                        attachment_file_paths=attachment_file_paths,
                    )
                )
                if resolve_error is not None:
                    return self._payload(
                        "error",
                        action=action,
                        room_id=room_id,
                        thread_id=effective_thread_id,
                        attachment_thread_id=attachment_thread_id,
                        event_id=event_id,
                        message=resolve_error,
                    )

                first_attachment_path = attachment_paths[0]
                remaining_attachment_paths = attachment_paths[1:]
                latest_thread_event_id = await context.conversation_cache.get_latest_thread_event_id_if_needed(
                    room_id,
                    effective_thread_id,
                )
                first_attachment_event_id = await send_file_message(
                    context.client,
                    room_id,
                    first_attachment_path,
                    thread_id=effective_thread_id,
                    latest_thread_event_id=latest_thread_event_id,
                    conversation_cache=context.conversation_cache,
                )
                if first_attachment_event_id is None:
                    return self._payload(
                        "error",
                        action=action,
                        room_id=room_id,
                        thread_id=effective_thread_id,
                        attachment_thread_id=attachment_thread_id,
                        event_id=event_id,
                        attachment_event_ids=[],
                        resolved_attachment_ids=resolved_attachment_ids,
                        newly_registered_attachment_ids=newly_registered_attachment_ids,
                        message=f"Failed to send attachment: {first_attachment_path}",
                    )

                attachment_event_ids = [first_attachment_event_id]
                attachment_thread_id = first_attachment_event_id
                remaining_attachment_event_ids, send_error = await _send_attachment_paths(
                    context,
                    room_id=room_id,
                    thread_id=attachment_thread_id,
                    attachment_paths=remaining_attachment_paths,
                )
                attachment_event_ids.extend(remaining_attachment_event_ids)
                if send_error is not None:
                    return self._payload(
                        "error",
                        action=action,
                        room_id=room_id,
                        thread_id=effective_thread_id,
                        attachment_thread_id=attachment_thread_id,
                        event_id=event_id,
                        attachment_event_ids=attachment_event_ids,
                        resolved_attachment_ids=resolved_attachment_ids,
                        newly_registered_attachment_ids=newly_registered_attachment_ids,
                        message=send_error,
                    )
            else:
                attachment_thread_id = effective_thread_id
                if event_id is not None and not room_mode:
                    attachment_thread_id = effective_thread_id or event_id

                send_result, send_error = await send_context_attachments(
                    context,
                    attachment_ids=attachment_ids,
                    attachment_file_paths=attachment_file_paths,
                    room_id=room_id,
                    thread_id=attachment_thread_id,
                    require_joined_room=False,
                    inherit_context_thread=False,
                )
                if send_result is not None:
                    attachment_thread_id = send_result.thread_id
                if send_error is not None:
                    if send_result is None:
                        return self._payload(
                            "error",
                            action=action,
                            room_id=room_id,
                            thread_id=effective_thread_id,
                            attachment_thread_id=attachment_thread_id,
                            event_id=event_id,
                            message=send_error,
                        )
                    return self._payload(
                        "error",
                        action=action,
                        room_id=send_result.room_id,
                        thread_id=effective_thread_id,
                        attachment_thread_id=attachment_thread_id,
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
            attachment_thread_id=attachment_thread_id,
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

        trusted_sender_ids = configured_visible_body_sender_ids(context.config, context.runtime_paths)
        resolved = [
            await extract_and_resolve_message(
                event,
                context.client,
                trusted_sender_ids=trusted_sender_ids,
            )
            for event in reversed(response.chunk)
            if isinstance(event, self._VISIBLE_ROOM_MESSAGE_EVENT_TYPES)
        ]
        return self._payload(
            "ok",
            action="read",
            room_id=room_id,
            limit=read_limit,
            messages=resolved,
        )

    def _build_edit_options(
        self,
        context: ToolRuntimeContext,
        *,
        messages: Sequence[ResolvedVisibleMessage],
    ) -> list[dict[str, object]]:
        current_user_id = context.client.user_id
        options: list[dict[str, object]] = []
        for message in reversed(messages):
            event_id = message.event_id
            sender = message.sender
            can_edit = current_user_id is not None and sender == current_user_id
            option: dict[str, object] = {
                "event_id": event_id,
                "sender": sender,
                "can_edit": can_edit,
                "body_preview": message_preview(message.body),
            }
            if can_edit:
                option["edit_action"] = {"action": "edit", "target": event_id}
            options.append(option)
        return options

    @staticmethod
    def _thread_reply_count(event: nio.Event) -> int:
        unsigned = event.source.get("unsigned", {})
        if not isinstance(unsigned, dict):
            return 0
        relations = unsigned.get("m.relations", {})
        if not isinstance(relations, dict):
            return 0
        thread_metadata = relations.get("m.thread", {})
        if not isinstance(thread_metadata, dict):
            return 0
        count = thread_metadata.get("count")
        return count if isinstance(count, int) and not isinstance(count, bool) else 0

    @staticmethod
    def _thread_latest_activity_ts(event: nio.Event) -> int | None:
        unsigned = event.source.get("unsigned", {})
        if not isinstance(unsigned, dict):
            return None
        relations = unsigned.get("m.relations", {})
        if not isinstance(relations, dict):
            return None
        thread_metadata = relations.get("m.thread", {})
        if not isinstance(thread_metadata, dict):
            return None
        latest_event = thread_metadata.get("latest_event")
        if not isinstance(latest_event, dict):
            return None
        latest_activity_ts = latest_event.get("origin_server_ts")
        if not isinstance(latest_activity_ts, int) or isinstance(latest_activity_ts, bool):
            return None
        return latest_activity_ts

    async def _serialize_thread_root(
        self,
        context: ToolRuntimeContext,
        *,
        event: nio.Event,
    ) -> dict[str, object] | None:
        event_id = event.event_id
        sender = event.sender
        timestamp = event.server_timestamp
        source = event.source
        if (
            not isinstance(event_id, str)
            or not isinstance(sender, str)
            or not isinstance(timestamp, int)
            or isinstance(timestamp, bool)
            or not isinstance(source, dict)
        ):
            logger.warning(
                "Skipping malformed room thread root",
                room_id=context.room_id,
                event_type=type(event).__name__,
            )
            return None
        trusted_sender_ids = configured_visible_body_sender_ids(context.config, context.runtime_paths)
        body_preview = await thread_root_body_preview(
            event,
            client=context.client,
            trusted_sender_ids=trusted_sender_ids,
        )

        payload = {
            "thread_id": event_id,
            "sender": sender,
            "timestamp": timestamp,
            "body_preview": body_preview,
            "reply_count": self._thread_reply_count(event),
        }
        latest_activity_ts = self._thread_latest_activity_ts(event)
        if latest_activity_ts is not None:
            payload["latest_activity_ts"] = latest_activity_ts
        return payload

    async def _room_threads(
        self,
        context: ToolRuntimeContext,
        *,
        room_id: str,
        read_limit: int,
        page_token: str | None,
    ) -> str:
        try:
            thread_roots, next_token = await get_room_threads_page(
                context.client,
                room_id,
                limit=read_limit,
                page_token=page_token,
            )
        except RoomThreadsPageError as exc:
            error_payload: dict[str, object] = {
                "action": "room-threads",
                "response": exc.response,
                "room_id": room_id,
            }
            if exc.errcode is not None:
                error_payload["errcode"] = exc.errcode
            if exc.retry_after_ms is not None:
                error_payload["retry_after_ms"] = exc.retry_after_ms
            return self._payload("error", **error_payload)

        threads: list[dict[str, object]] = []
        for event in thread_roots:
            thread = await self._serialize_thread_root(context, event=event)
            if thread is not None:
                threads.append(thread)
        return self._payload(
            "ok",
            action="room-threads",
            room_id=room_id,
            count=len(threads),
            threads=threads,
            next_token=next_token,
            has_more=next_token is not None,
        )

    async def _thread_read_payload(
        self,
        context: ToolRuntimeContext,
        *,
        action: str,
        room_id: str,
        thread_id: str,
        read_limit: int,
    ) -> str:
        thread_messages = await context.conversation_cache.get_thread_history(room_id, thread_id)
        recent_messages = thread_messages[-read_limit:]
        return self._payload(
            "ok",
            action=action,
            room_id=room_id,
            thread_id=thread_id,
            limit=read_limit,
            messages=[message.to_dict() for message in recent_messages],
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
            latest_thread_event_id = await context.conversation_cache.get_latest_thread_event_id_if_needed(
                room_id,
                thread_id,
            )
            if latest_thread_event_id is None:
                latest_thread_event_id = target

        clear_interactive_question(target)
        interactive_response = parse_and_format_interactive(new_text, extract_mapping=True)
        formatted_text = interactive_response.formatted_text
        content = format_message_with_mentions(
            context.config,
            context.runtime_paths,
            formatted_text,
            sender_domain=context.config.get_domain(context.runtime_paths),
            thread_event_id=thread_id,
            latest_thread_event_id=latest_thread_event_id,
        )
        delivered = await edit_message_result(context.client, room_id, target, content, formatted_text)
        if delivered is None:
            return self._payload(
                "error",
                action="edit",
                room_id=room_id,
                thread_id=thread_id,
                target=target,
                message="Failed to edit message in Matrix.",
            )
        context.conversation_cache.notify_outbound_message(
            room_id,
            delivered.event_id,
            delivered.content_sent,
        )

        if interactive_response.option_map and interactive_response.options_list:
            register_interactive_question(
                target,
                room_id,
                thread_id,
                interactive_response.option_map,
                context.agent_name,
            )
            await add_reaction_buttons(
                context.client,
                room_id,
                target,
                interactive_response.options_list,
            )

        return self._payload(
            "ok",
            action="edit",
            room_id=room_id,
            thread_id=thread_id,
            target=target,
            event_id=delivered.event_id,
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

    async def _dispatch_action(  # noqa: PLR0911
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
        page_token: str | None,
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
        if action == "room-threads":
            return await self._room_threads(
                context,
                room_id=room_id,
                read_limit=self._read_limit(limit),
                page_token=page_token,
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
            message=(
                "Unsupported action. Use send, reply, thread-reply, react, read, room-threads, thread-list, edit, or context."
            ),
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
            page_token=page_token,
        )
