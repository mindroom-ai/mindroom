"""Four conversation-aware Matrix message actions for agents."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import replace
from functools import partial
from pathlib import Path  # noqa: TC003 - tool config sync evaluates constructor type hints at runtime.
from threading import Lock
from typing import TYPE_CHECKING, ClassVar, Literal

from agno.tools import Toolkit
from pydantic import BaseModel, ConfigDict, TypeAdapter

from mindroom.custom_tools import matrix_conversation_operations
from mindroom.custom_tools.attachment_helpers import (
    normalize_str_list,
    resolve_context_thread_id,
    resolve_requested_room_id,
    room_access_allowed,
)
from mindroom.custom_tools.matrix_agent_discovery import available_room_agents
from mindroom.custom_tools.matrix_helpers import check_rate_limit
from mindroom.custom_tools.matrix_message_idempotency import (
    MatrixMessageIdempotencyError,
    claim_matrix_message_send,
)
from mindroom.custom_tools.tool_payloads import custom_tool_payload
from mindroom.matrix.message_extras import parse_message_extra_sections
from mindroom.requester_identity import is_human_requester_id
from mindroom.tool_system.runtime_context import ToolRuntimeContext, get_tool_runtime_context

if TYPE_CHECKING:
    from mindroom.custom_tools.matrix_message_idempotency import MatrixMessageSendClaim
    from mindroom.matrix.message_extras import MessageExtraSection


class MatrixMessageExtra(BaseModel):
    """Model-facing fields for one optional collapsible section."""

    model_config = ConfigDict(strict=True, extra="forbid")

    title: str
    content: str
    content_type: Literal["text/plain", "text/markdown", "text/html"] = "text/markdown"
    collapsed: bool = True


def _optional_string(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        msg = f"{name} must be a non-empty string when provided."
        raise ValueError(msg)
    return value.strip()


class MatrixMessageTools(Toolkit):
    """Send, read, edit, and react in the current or an explicitly selected conversation."""

    _rate_limit_lock: ClassVar[Lock] = Lock()
    _recent_actions: ClassVar[dict[tuple[str, str, str], deque[float]]] = defaultdict(deque)
    _RATE_LIMIT_WINDOW_SECONDS: ClassVar[float] = 30.0
    _RATE_LIMIT_MAX_ACTIONS: ClassVar[int] = 12
    _MAX_ATTACHMENTS_PER_CALL: ClassVar[int] = 5
    _DEFAULT_READ_LIMIT: ClassVar[int] = 20
    _MAX_READ_LIMIT: ClassVar[int] = 50
    _VALID_ACTIONS: ClassVar[frozenset[str]] = frozenset({"send", "read", "edit", "react"})

    def __init__(self, *, tool_output_workspace_root: Path | None = None) -> None:
        self._operations = matrix_conversation_operations.MatrixMessageOperations(
            tool_output_workspace_root=tool_output_workspace_root,
        )
        super().__init__(name="matrix_message", tools=[self.matrix_message])

    @staticmethod
    def _payload(status: str, **kwargs: object) -> str:
        return custom_tool_payload("matrix_message", status, **kwargs)

    @classmethod
    def _check_rate_limit(cls, context: ToolRuntimeContext, room_id: str, *, weight: int = 1) -> str | None:
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

    async def matrix_message(  # noqa: C901, PLR0911, PLR0912
        self,
        action: Literal["send", "read", "edit", "react"] = "send",
        message: str | None = None,
        recipient: str | None = None,
        room_id: str | None = None,
        thread_id: str | None = None,
        new_thread: bool = False,
        event_id: str | None = None,
        attachments: list[str] | None = None,
        message_extras: list[MatrixMessageExtra] | None = None,
        limit: int | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        """Send, read, edit, or react to Matrix messages.

        `send` and `read` use the current conversation. Set new_thread=True to start a separate thread, thread_id to continue another thread, or thread_id="room" for the room timeline. Another room never inherits the current thread.

        Set recipient to an agent/team name from matrix_room(action="agents") to request a response, including from yourself for a human requester. Only that recipient is dispatched; without recipient, names in the body do not start agents. Room-mode recipients use the room timeline and cannot accept new_thread or an explicit thread. Sending returns immediately; run_subagent waits for an answer.

        Send text, attachments, or both. attachments is an ordered list of att_* IDs or local file paths (max 5); relative paths use the workspace. Use ./ for a filename starting with att_. Files arrive before recipient dispatch.

        edit/react require event_id; react uses message as emoji (default 👍). message_extras adds optional collapsible sections to send/edit. Interactive prompts require normal response delivery. Use matrix_room for room details and thread discovery.

        Args:
            action: send, read, edit, or react.
            message: Text to send/edit, or reaction emoji.
            recipient: Agent/team name to request a response from; send only.
            room_id: Room ID or configured room name; current room by default.
            thread_id: Thread root ID; current conversation by default, "room" for room timeline.
            new_thread: Start a separate thread; send only, cannot combine with thread_id.
            event_id: Message event ID to edit or react to.
            attachments: Ordered att_* IDs or file paths; send only, maximum 5.
            message_extras: Collapsible sections with title/content; optional content_type and collapsed.
            limit: Messages to read, 1-50; default 20.
            idempotency_key: Nonblank key (max 256 characters) for durable text-only send retries.
                Same requester, agent, room, and key replay the first prepared message and target.
                Completed receipts last eight days; pending sends never expire.

        """
        context = get_tool_runtime_context()
        if context is None:
            return self._payload("error", message="Matrix messaging tool context is unavailable in this runtime path.")
        if idempotency_key is None:
            context = replace(context, config=context.current_config, config_provider=None)
        if not isinstance(action, str) or action.strip().lower() not in self._VALID_ACTIONS:
            return self._payload("error", message="Unsupported action. Use send, read, edit, or react.")
        normalized_action = action.strip().lower()
        try:
            recipient = _optional_string(recipient, "recipient")
            thread_id = _optional_string(thread_id, "thread_id")
            event_id = _optional_string(event_id, "event_id")
        except ValueError as exc:
            return self._payload("error", action=normalized_action, message=str(exc))
        if not isinstance(new_thread, bool):
            return self._payload("error", message="new_thread must be a boolean.")
        if message is not None and not isinstance(message, str):
            return self._payload("error", message="message must be a string.")
        if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool)):
            return self._payload("error", message="limit must be an integer.")
        if attachments is not None and not isinstance(attachments, list):
            return self._payload("error", message="attachments must be a list of attachment IDs or file paths.")
        references, attachment_error = normalize_str_list(attachments, field_name="attachments")
        if attachment_error is not None:
            return self._payload("error", message=attachment_error)
        if len(references) > self._MAX_ATTACHMENTS_PER_CALL:
            return self._payload("error", message="attachments cannot exceed 5 per call.")
        if normalized_action != "send" and (recipient is not None or new_thread or references):
            return self._payload("error", message="recipient, new_thread, and attachments are only supported for send.")
        if idempotency_key is not None:
            if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 256:
                return self._payload("error", message="idempotency_key must be nonblank and at most 256 characters.")
            if normalized_action != "send" or attachments:
                return self._payload(
                    "error",
                    message="idempotency_key is only supported for text sends without attachments.",
                )
        if new_thread and thread_id is not None:
            return self._payload("error", message="Use either new_thread=True or thread_id, not both.")
        if event_id is not None and normalized_action not in {"edit", "react"}:
            return self._payload("error", message="event_id is only supported for edit and react.")
        parsed_extras = None
        if message_extras:
            if normalized_action not in {"send", "edit"}:
                return self._payload("error", message="message_extras is only supported for send and edit.")
            try:
                sections = TypeAdapter(list[MatrixMessageExtra]).validate_python(message_extras)
                parsed_extras = parse_message_extra_sections([section.model_dump() for section in sections])
            except (TypeError, ValueError) as exc:
                return self._payload("error", message=str(exc))
        resolved_room_id, room_error = resolve_requested_room_id(context, room_id)
        if room_error is not None or resolved_room_id is None:
            return self._payload("error", message=room_error)
        if not room_access_allowed(context, resolved_room_id):
            return self._payload("error", room_id=resolved_room_id, message="Not authorized to access the target room.")
        dispatch = partial(
            self._dispatch_action,
            action=normalized_action,
            message=message,
            recipient=recipient,
            room_id=resolved_room_id,
            thread_id=thread_id,
            new_thread=new_thread,
            event_id=event_id,
            attachments=references,
            message_extras=parsed_extras,
            limit=limit,
        )
        if idempotency_key is None:
            return await dispatch(context)
        try:
            async with claim_matrix_message_send(context, resolved_room_id, idempotency_key) as claim:
                return await dispatch(claim.context, send_claim=claim)
        except (MatrixMessageIdempotencyError, OSError, ValueError) as exc:
            return self._payload("error", action="send", room_id=resolved_room_id, message=str(exc))

    async def _dispatch_action(
        self,
        context: ToolRuntimeContext,
        *,
        action: str,
        message: str | None,
        recipient: str | None,
        room_id: str,
        thread_id: str | None,
        new_thread: bool,
        event_id: str | None,
        attachments: list[str],
        message_extras: list[MessageExtraSection] | None,
        limit: int | None,
        send_claim: MatrixMessageSendClaim | None = None,
    ) -> str:
        stored_intent = send_claim.intent if send_claim is not None else None
        if stored_intent is not None:
            recipient = stored_intent.recipient
            thread_id = stored_intent.thread_id
            new_thread = stored_intent.starts_thread
        if (rate_error := self._check_rate_limit(context, room_id, weight=1 + len(attachments))) is not None:
            return self._payload("error", room_id=room_id, message=rate_error)
        recipient_user_id = None
        room_mode = (
            context.config.get_entity_thread_mode(
                context.agent_name,
                context.runtime_paths,
                room_id=room_id,
            )
            == "room"
        )
        if recipient is not None:
            candidates = await available_room_agents(context, room_id)
            selected = next((candidate for candidate in candidates if candidate.name == recipient), None)
            if selected is None:
                names = ", ".join(candidate.name for candidate in candidates) or "none"
                return self._payload(
                    "error",
                    message=f"Recipient '{recipient}' is not available in this room. Available recipients: {names}.",
                )
            recipient_user_id = selected.matrix_user_id
            if (
                send_claim is not None
                and send_claim.intent is not None
                and recipient_user_id != send_claim.intent.recipient_user_id
            ):
                return self._payload("error", message="The original Matrix recipient is no longer available.")
            if recipient_user_id == context.client.user_id and not is_human_requester_id(
                context.requester_id,
                context.config,
                context.runtime_paths,
            ):
                return self._payload(
                    "error",
                    message="Self-messaging requires a human requester. Use run_subagent for a fresh self-run in this runtime.",
                )
            room_mode = selected.thread_mode == "room"
            if room_mode and stored_intent is None:
                if new_thread or thread_id not in {None, "room"}:
                    return self._payload(
                        "error",
                        message=f"Recipient '{recipient}' uses room conversations. Omit new_thread/thread_id or use thread_id=\"room\".",
                    )
                thread_id = "room"
        effective_thread_id = resolve_context_thread_id(
            context,
            room_id=room_id,
            thread_id=thread_id,
            allow_context_fallback=not new_thread,
            room_timeline_sentinel="room",
        )
        result = await self._operations.dispatch_action(
            context,
            action=action,
            message=message,
            attachments=attachments,
            room_id=room_id,
            event_id=event_id,
            thread_id=effective_thread_id,
            recipient_user_id=recipient_user_id,
            room_mode=(room_mode or thread_id == "room") and not new_thread,
            new_thread=new_thread or (recipient is not None and not room_mode and effective_thread_id is None),
            message_extras=message_extras,
            send_claim=send_claim,
            recipient_name=recipient,
            read_limit=max(1, min(limit if limit is not None else self._DEFAULT_READ_LIMIT, self._MAX_READ_LIMIT)),
        )
        return self._payload(result.status, **result.fields)
