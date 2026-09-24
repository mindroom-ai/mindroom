"""Conversation-level Matrix operations used by model-facing Matrix tools."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import nio

from mindroom.attachments import AttachmentRecord
from mindroom.constants import ATTACHMENT_IDS_KEY, ORIGINAL_SENDER_KEY, SKIP_MENTIONS_KEY, SOURCE_KIND_KEY
from mindroom.custom_tools.attachments import (
    resolve_send_attachments,
    send_resolved_attachments,
)
from mindroom.dispatch_source import TRUSTED_INTERNAL_RELAY_SOURCE_KIND
from mindroom.interactive import parse_and_format_interactive
from mindroom.matrix.client_delivery import edit_message_result, send_message_result, send_room_event_result
from mindroom.matrix.client_visible_messages import (
    is_visible_room_message,
    message_preview,
    resolve_latest_visible_messages,
    trusted_visible_sender_ids,
)
from mindroom.matrix.conversation_reads import complete_thread_history
from mindroom.matrix.mentions import format_message_with_mentions
from mindroom.matrix.message_builder import build_reaction_content
from mindroom.matrix.message_extras import build_message_extras_content
from mindroom.requester_identity import is_human_requester_id

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mindroom.custom_tools.matrix_message_idempotency import MatrixMessageSendClaim
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.matrix.message_extras import MessageExtraSection
    from mindroom.matrix.runtime_media import RuntimeEncryptedMediaAttachment
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

_DIRECT_INTERACTIVE_ERROR = "Interactive prompts are only supported in normal agent responses."


@dataclass(frozen=True)
class MatrixMessageOperationResult:
    """Structured result produced before tool-specific JSON serialization."""

    status: Literal["ok", "error"]
    fields: dict[str, object]


@dataclass
class _MessageSendState:
    """Delivered event IDs retained even when a later part of one send fails."""

    room_id: str
    thread_id: str | None
    event_id: str | None = None
    attachment_event_ids: list[str] = field(default_factory=list)
    resolved_attachment_ids: list[str] = field(default_factory=list)
    newly_registered_attachment_ids: list[str] = field(default_factory=list)


def _format_direct_text(text: str) -> str | None:
    """Format plain direct-tool text, rejecting prompts without durable ownership."""
    response = parse_and_format_interactive(text, extract_mapping=True)
    return response.formatted_text if response.interactive_metadata is None else None


class MatrixMessageOperations:
    """Run Matrix message operations below the model-facing tool adapter."""

    def __init__(self, *, tool_output_workspace_root: Path | None = None) -> None:
        self._tool_output_workspace_root = tool_output_workspace_root

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
        recipient_user_id: str | None,
        message_extras: list[MessageExtraSection] | None,
        known_latest_thread_event_id: str | None = None,
        attachment_ids: list[str] | None = None,
        send_claim: MatrixMessageSendClaim | None = None,
        recipient_name: str | None = None,
        starts_thread: bool = False,
    ) -> str | None:
        latest_thread_event_id = await context.conversation_reader.latest_thread_event_id(
            room_id=room_id,
            thread_id=thread_id,
            known_latest_thread_event_id=known_latest_thread_event_id,
        )
        extra_content: dict[str, Any] = {}
        if recipient_user_id is None:
            extra_content[SKIP_MENTIONS_KEY] = True
        elif context.requester_id != context.client.user_id and is_human_requester_id(
            context.requester_id,
            context.config,
            context.runtime_paths,
        ):
            extra_content[ORIGINAL_SENDER_KEY] = context.requester_id
            extra_content[SOURCE_KIND_KEY] = TRUSTED_INTERNAL_RELAY_SOURCE_KIND
        if attachment_ids:
            extra_content[ATTACHMENT_IDS_KEY] = attachment_ids
        if message_extras:
            extra_content.update(build_message_extras_content(message_extras))
        content = format_message_with_mentions(
            context.config,
            context.runtime_paths,
            f"{recipient_user_id} {text}".strip() if recipient_user_id else text,
            thread_event_id=thread_id,
            latest_thread_event_id=latest_thread_event_id,
            extra_content=extra_content or None,
        )
        if recipient_user_id is not None:
            # Formatting also finds names in quoted task text. Only the explicit
            # recipient should dispatch, regardless of those incidental mentions.
            content["m.mentions"] = {"user_ids": [recipient_user_id]}
        if send_claim is not None:
            await send_claim.prepare(
                content,
                recipient=recipient_name,
                recipient_user_id=recipient_user_id,
                thread_id=thread_id,
                starts_thread=starts_thread,
            )
            event_id, _ = await send_claim.deliver()
            return event_id
        delivered = await send_message_result(context.client, room_id, content)
        return delivered.event_id if delivered is not None else None

    async def _send_message_attachments(
        self,
        context: ToolRuntimeContext,
        state: _MessageSendState,
        attachments: list[AttachmentRecord | RuntimeEncryptedMediaAttachment],
        *,
        room_mode: bool,
        needs_thread: bool,
    ) -> str | None:
        thread_id = state.thread_id
        if thread_id is None and not room_mode:
            if state.event_id is not None:
                thread_id = state.event_id
            elif needs_thread or len(attachments) > 1:
                first_ids, error = await send_resolved_attachments(
                    context,
                    room_id=state.room_id,
                    thread_id=None,
                    attachments=attachments[:1],
                )
                state.attachment_event_ids.extend(first_ids)
                if error is not None or not first_ids:
                    return error or "Failed to send the first attachment."
                thread_id = first_ids[0]
                attachments = attachments[1:]
        state.thread_id = thread_id
        if attachments:
            sent_ids, error = await send_resolved_attachments(
                context,
                room_id=state.room_id,
                thread_id=thread_id,
                attachments=attachments,
                known_latest_thread_event_id=(
                    state.attachment_event_ids[-1] if state.attachment_event_ids else state.event_id
                ),
            )
            state.attachment_event_ids.extend(sent_ids)
            return error
        return None

    async def _message_send(  # noqa: C901, PLR0911, PLR0912
        self,
        context: ToolRuntimeContext,
        *,
        message: str | None,
        attachments: list[str],
        room_id: str,
        thread_id: str | None,
        recipient_user_id: str | None,
        room_mode: bool,
        new_thread: bool,
        message_extras: list[MessageExtraSection] | None,
        send_claim: MatrixMessageSendClaim | None = None,
        recipient_name: str | None = None,
    ) -> MatrixMessageOperationResult:
        if send_claim is not None and send_claim.intent is not None:
            event_id, resolved_thread_id = await send_claim.deliver()
            return self._result(
                "ok",
                action="send",
                **asdict(
                    _MessageSendState(
                        room_id=room_id,
                        thread_id=resolved_thread_id,
                        event_id=event_id,
                    ),
                ),
            )
        text = message.strip() if message and message.strip() else None
        if text is None and message_extras:
            return self._result("error", action="send", message="message_extras requires a non-empty message body.")
        if text is None and not attachments:
            return self._result("error", action="send", message="Provide message, attachments, or both.")
        if text is not None:
            text = _format_direct_text(text)
            if text is None:
                return self._result("error", action="send", room_id=room_id, message=_DIRECT_INTERACTIVE_ERROR)

        state = _MessageSendState(room_id=room_id, thread_id=thread_id)
        resolved: list[AttachmentRecord | RuntimeEncryptedMediaAttachment] = []
        for reference in attachments:
            is_id = reference.startswith("att_")
            files, ids, registered_ids, error = resolve_send_attachments(
                context,
                attachment_ids=[reference] if is_id else [],
                attachment_file_paths=[] if is_id else [reference],
                workspace_root=self._tool_output_workspace_root,
            )
            if error is not None:
                return self._result("error", action="send", message=error, **asdict(state))
            resolved.extend(files)
            state.resolved_attachment_ids.extend(ids)
            state.newly_registered_attachment_ids.extend(registered_ids)

        if (
            recipient_user_id is not None
            and room_mode
            and any(not isinstance(file, AttachmentRecord) for file in resolved)
        ):
            return self._result(
                "error",
                action="send",
                message="Turn-scoped media requires a threaded recipient conversation. Use new_thread=True with a thread-mode recipient, or send a registered local file.",
                **asdict(state),
            )

        # An agent must receive the files before the message that dispatches it.
        if recipient_user_id is not None and resolved:
            error = await self._send_message_attachments(
                context,
                state,
                resolved,
                room_mode=room_mode,
                needs_thread=True,
            )
            if error is not None:
                return self._result("error", action="send", message=error, **asdict(state))
        if text is not None or recipient_user_id is not None:
            state.event_id = await self._send_matrix_text(
                context,
                room_id=room_id,
                text=text or "",
                send_claim=send_claim,
                recipient_name=recipient_name,
                starts_thread=state.thread_id is None
                and (new_thread or (not room_mode and recipient_user_id is not None)),
                thread_id=state.thread_id,
                recipient_user_id=recipient_user_id,
                attachment_ids=state.resolved_attachment_ids if recipient_user_id is not None else None,
                message_extras=message_extras,
                known_latest_thread_event_id=(state.attachment_event_ids[-1] if state.attachment_event_ids else None),
            )
            if state.event_id is None:
                return self._result(
                    "error",
                    action="send",
                    message="Failed to send message to Matrix.",
                    **asdict(state),
                )
            if state.thread_id is None and (new_thread or (not room_mode and recipient_user_id is not None)):
                state.thread_id = state.event_id
        if recipient_user_id is None and resolved:
            error = await self._send_message_attachments(
                context,
                state,
                resolved,
                room_mode=room_mode,
                needs_thread=new_thread,
            )
            if error is not None:
                return self._result("error", action="send", message=error, **asdict(state))
        if state.event_id is None and state.attachment_event_ids:
            state.event_id = state.attachment_event_ids[0]
        return self._result("ok", action="send", **asdict(state))

    async def _message_react(
        self,
        context: ToolRuntimeContext,
        *,
        message: str | None,
        room_id: str,
        event_id: str | None,
    ) -> MatrixMessageOperationResult:
        if event_id is None:
            return self._result("error", action="react", message="event_id is required.")

        reaction = message.strip() if message and message.strip() else "👍"
        response = await send_room_event_result(
            context.client,
            room_id,
            "m.reaction",
            build_reaction_content(event_id, reaction),
            operation="matrix_message_react",
        )
        if isinstance(response, nio.RoomSendResponse):
            return self._result(
                "ok",
                action="react",
                room_id=room_id,
                reacted_event_id=event_id,
                reaction=reaction,
                event_id=response.event_id,
            )
        return self._result(
            "error",
            action="react",
            room_id=room_id,
            reacted_event_id=event_id,
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

        # Include encrypted wire events so the owned client can decrypt them
        # before visible-message projection and edit folding.
        response = await context.client.room_messages(
            room_id,
            limit=read_limit,
            direction=nio.MessageDirection.back,
            message_filter={"types": ["m.room.message", "m.room.encrypted"]},
        )
        if not isinstance(response, nio.RoomMessagesResponse):
            return self._result(
                "error",
                action="read",
                room_id=room_id,
                response=str(response),
            )

        # Edits are folded onto the messages they revise, the same thing the
        # thread read gets for free: it reads the projection, which holds one
        # row per logical message. A raw timeline has no such row, so a message
        # edited three times is three `m.room.message` events, and a model
        # handed all three reads one corrected sentence as three near-identical
        # ones. Ordered by the original's timestamp, because an edit corrects a
        # message rather than moving it to the end of the room.
        resolved = await resolve_latest_visible_messages(
            [event for event in reversed(response.chunk) if is_visible_room_message(event)],
            context.client,
            trusted_sender_ids=trusted_visible_sender_ids(context.config, context.runtime_paths),
        )
        messages = sorted(resolved.values(), key=lambda message: message.timestamp)
        return self._result(
            "ok",
            action="read",
            room_id=room_id,
            limit=read_limit,
            messages=[message.to_dict() for message in messages],
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
                option["edit_action"] = {"action": "edit", "event_id": event_id}
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
    ) -> MatrixMessageOperationResult:
        thread_messages = await complete_thread_history(context.conversation_reader, room_id, thread_id)
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

    async def _message_edit(
        self,
        context: ToolRuntimeContext,
        *,
        room_id: str,
        thread_id: str | None,
        event_id: str | None,
        message: str | None,
        message_extras: list[MessageExtraSection] | None,
    ) -> MatrixMessageOperationResult:
        if event_id is None:
            return self._result("error", action="edit", message="event_id is required for edit.")
        new_text = message.strip() if isinstance(message, str) and message.strip() else None
        if new_text is None:
            return self._result("error", action="edit", message="message is required for edit.")

        formatted_text = _format_direct_text(new_text)
        if formatted_text is None:
            return self._result("error", action="edit", room_id=room_id, message=_DIRECT_INTERACTIVE_ERROR)
        extras_content = build_message_extras_content(message_extras) if message_extras else {}
        content = format_message_with_mentions(
            context.config,
            context.runtime_paths,
            formatted_text,
            extra_content=extras_content or None,
        )
        delivered = await edit_message_result(
            context.client,
            room_id,
            event_id,
            content,
            formatted_text,
            extra_content=extras_content or None,
        )
        if delivered is None:
            return self._result(
                "error",
                action="edit",
                room_id=room_id,
                thread_id=thread_id,
                edited_event_id=event_id,
                message="Failed to edit message in Matrix.",
            )
        return self._result(
            "ok",
            action="edit",
            room_id=room_id,
            thread_id=thread_id,
            edited_event_id=event_id,
            event_id=delivered.event_id,
        )

    async def dispatch_action(
        self,
        context: ToolRuntimeContext,
        *,
        action: str,
        message: str | None,
        attachments: list[str],
        room_id: str,
        event_id: str | None,
        thread_id: str | None,
        recipient_user_id: str | None,
        room_mode: bool,
        new_thread: bool,
        message_extras: list[MessageExtraSection] | None,
        read_limit: int,
        send_claim: MatrixMessageSendClaim | None = None,
        recipient_name: str | None = None,
    ) -> MatrixMessageOperationResult:
        """Dispatch one authorized action with an already resolved conversation."""
        if action == "send":
            return await self._message_send(
                context,
                message=message,
                attachments=attachments,
                send_claim=send_claim,
                recipient_name=recipient_name,
                room_id=room_id,
                thread_id=thread_id,
                recipient_user_id=recipient_user_id,
                room_mode=room_mode,
                new_thread=new_thread,
                message_extras=message_extras,
            )
        if action == "read":
            return await self._message_read(
                context,
                room_id=room_id,
                effective_thread_id=thread_id,
                read_limit=read_limit,
            )
        if action == "react":
            return await self._message_react(context, message=message, room_id=room_id, event_id=event_id)
        return await self._message_edit(
            context,
            room_id=room_id,
            thread_id=thread_id,
            event_id=event_id,
            message=message,
            message_extras=message_extras,
        )
