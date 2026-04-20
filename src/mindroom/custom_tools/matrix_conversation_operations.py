"""Conversation-level Matrix operations used by model-facing Matrix tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import nio

from mindroom.constants import ORIGINAL_SENDER_KEY
from mindroom.custom_tools.attachment_helpers import resolve_context_thread_id
from mindroom.custom_tools.attachments import (
    _resolve_send_attachments,
    _send_attachment_paths,
    send_context_attachments,
)
from mindroom.interactive import (
    add_reaction_buttons,
    clear_interactive_question,
    parse_and_format_interactive,
    register_interactive_question,
    should_create_interactive_question,
)
from mindroom.logging_config import get_logger
from mindroom.matrix.client_delivery import edit_message_result, send_file_message, send_message_result
from mindroom.matrix.client_thread_history import RoomThreadsPageError, get_room_threads_page
from mindroom.matrix.client_visible_messages import extract_visible_message as extract_and_resolve_message
from mindroom.matrix.client_visible_messages import (
    message_preview,
    thread_root_body_preview,
    trusted_visible_sender_ids,
)
from mindroom.matrix.mentions import format_message_with_mentions

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

logger = get_logger(__name__)


@dataclass(frozen=True)
class MatrixMessageOperationResult:
    """Structured result produced before tool-specific JSON serialization."""

    status: Literal["ok", "error"]
    fields: dict[str, object]


class MatrixMessageOperations:
    """Run Matrix message operations below the model-facing tool adapter."""

    _VISIBLE_ROOM_MESSAGE_EVENT_TYPES: tuple[type[nio.RoomMessageText], type[nio.RoomMessageNotice]] = (
        nio.RoomMessageText,
        nio.RoomMessageNotice,
    )

    @staticmethod
    def _result(status: Literal["ok", "error"], **kwargs: object) -> MatrixMessageOperationResult:
        return MatrixMessageOperationResult(status=status, fields=kwargs)

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
        if response.interactive_metadata is None:
            return

        register_interactive_question(
            event_id,
            room_id,
            thread_id,
            response.interactive_metadata.option_map,
            context.agent_name,
        )
        await add_reaction_buttons(
            context.client,
            room_id,
            event_id,
            response.interactive_metadata.options_as_list(),
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
    ) -> MatrixMessageOperationResult:
        if action in {"thread-reply", "reply"} and effective_thread_id is None:
            return self._result("error", action=action, message="thread_id is required for replies.")

        text = message.strip() if isinstance(message, str) and message.strip() else None
        if text is None and not attachment_ids and not attachment_file_paths:
            return self._result(
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
            return self._result(
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
                    return self._result(
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
                    return self._result(
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
                    return self._result(
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
                        return self._result(
                            "error",
                            action=action,
                            room_id=room_id,
                            thread_id=effective_thread_id,
                            attachment_thread_id=attachment_thread_id,
                            event_id=event_id,
                            message=send_error,
                        )
                    return self._result(
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

        return self._result(
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
    ) -> MatrixMessageOperationResult:
        if target is None:
            return self._result("error", action="react", message="target event_id is required.")

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
            return self._result(
                "ok",
                action="react",
                room_id=room_id,
                target=target,
                reaction=reaction,
                event_id=response.event_id,
            )
        return self._result(
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
    ) -> MatrixMessageOperationResult:
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
            return self._result(
                "error",
                action="read",
                room_id=room_id,
                response=str(response),
            )

        trusted_sender_ids = trusted_visible_sender_ids(context.config, context.runtime_paths)
        resolved = [
            await extract_and_resolve_message(
                event,
                context.client,
                config=context.config,
                runtime_paths=context.runtime_paths,
                trusted_sender_ids=trusted_sender_ids,
            )
            for event in reversed(response.chunk)
            if isinstance(event, self._VISIBLE_ROOM_MESSAGE_EVENT_TYPES)
        ]
        return self._result(
            "ok",
            action="read",
            room_id=room_id,
            limit=read_limit,
            messages=resolved,
        )

    @staticmethod
    def _build_edit_options(
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
        trusted_sender_ids: frozenset[str],
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
        body_preview = await thread_root_body_preview(
            event,
            client=context.client,
            config=context.config,
            runtime_paths=context.runtime_paths,
            trusted_sender_ids=trusted_sender_ids,
        )

        payload: dict[str, object] = {
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
    ) -> MatrixMessageOperationResult:
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
            return self._result("error", **error_payload)

        threads: list[dict[str, object]] = []
        trusted_sender_ids = trusted_visible_sender_ids(context.config, context.runtime_paths)
        for event in thread_roots:
            thread = await self._serialize_thread_root(
                context,
                event=event,
                trusted_sender_ids=trusted_sender_ids,
            )
            if thread is not None:
                threads.append(thread)
        return self._result(
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
    ) -> MatrixMessageOperationResult:
        thread_messages = await context.conversation_cache.get_thread_messages(
            room_id,
            thread_id,
            full_history=True,
            dispatch_safe=False,
            caller_label="matrix_message_tool",
        )
        recent_messages = thread_messages[-read_limit:]
        return self._result(
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
    ) -> MatrixMessageOperationResult:
        if thread_id is None:
            return self._result(
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
    ) -> MatrixMessageOperationResult:
        if target is None:
            return self._result("error", action="edit", message="target event_id is required for edit.")
        new_text = message.strip() if isinstance(message, str) and message.strip() else None
        if new_text is None:
            return self._result("error", action="edit", message="message is required for edit.")

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
            return self._result(
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

        if interactive_response.interactive_metadata is not None:
            register_interactive_question(
                target,
                room_id,
                thread_id,
                interactive_response.interactive_metadata.option_map,
                context.agent_name,
            )
            await add_reaction_buttons(
                context.client,
                room_id,
                target,
                interactive_response.interactive_metadata.options_as_list(),
            )

        return self._result(
            "ok",
            action="edit",
            room_id=room_id,
            thread_id=thread_id,
            target=target,
            event_id=delivered.event_id,
        )

    async def dispatch_action(  # noqa: PLR0911
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
        read_limit: int,
        page_token: str | None,
        room_timeline_sentinel: str,
    ) -> MatrixMessageOperationResult:
        """Dispatch one normalized Matrix message tool action."""
        if action in {"send", "thread-reply", "reply"}:
            allow_context_fallback = action in {"thread-reply", "reply"}
            effective_thread_id = resolve_context_thread_id(
                context,
                room_id=room_id,
                thread_id=thread_id,
                allow_context_fallback=allow_context_fallback,
                room_timeline_sentinel=room_timeline_sentinel,
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
                room_timeline_sentinel=room_timeline_sentinel,
            )
            return await self._message_read(
                context,
                room_id=room_id,
                effective_thread_id=safe_thread,
                read_limit=read_limit,
            )
        if action == "room-threads":
            return await self._room_threads(
                context,
                room_id=room_id,
                read_limit=read_limit,
                page_token=page_token,
            )
        if action == "thread-list":
            safe_thread = resolve_context_thread_id(
                context,
                room_id=room_id,
                thread_id=thread_id,
                room_timeline_sentinel=room_timeline_sentinel,
            )
            return await self._message_thread_list(
                context,
                room_id=room_id,
                thread_id=safe_thread,
                read_limit=read_limit,
            )
        if action == "edit":
            safe_thread = resolve_context_thread_id(
                context,
                room_id=room_id,
                thread_id=thread_id,
                room_timeline_sentinel=room_timeline_sentinel,
            )
            return await self._message_edit(
                context,
                room_id=room_id,
                thread_id=safe_thread,
                target=target,
                message=message,
            )
        return self._result(
            "error",
            action=action,
            message=(
                "Unsupported action. Use send, reply, thread-reply, react, read, room-threads, thread-list, edit, or context."
            ),
        )
